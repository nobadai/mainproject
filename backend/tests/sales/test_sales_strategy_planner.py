"""LLM 전략 Planner와 A/B/C 차별화.

```text
Fixture A  정상 재고        세 자세가 의미상 갈린다
Fixture B  신선도 위험      공격안이 실제 소진 전략으로 선다
Fixture C  제약 수렴        숫자가 같아져도 강제로 벌리지 않고 이유를 남긴다
```

🔴 **이 파일의 중심은 «모델이 숫자를 못 만든다» 이다.** 모델이 고르는 것은 닫힌
  어휘의 자세뿐이고, 자세가 사실을 이기지 못한다 — 소진 신호가 없으면 모델이
  `DEPLETION` 을 골라도 내려간다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.sales.llm.runtime import (
    LlmStrategyPlanOutput,
    LlmStrategyProfileOutput,
    plan_strategy_profiles,
)
from app.sales.proposal import _generate_scenarios
from app.sales.schemas import SalesProposalInput
from app.sales.strategy import (
    StrategySignals,
    clamp_profiles,
    derive_signals,
    plan_strategies,
    template_profiles,
)

DELIVERY = date(2026, 9, 18)


def _lot(lot_id: str, *, remaining: int | None, limit: int | None) -> dict[str, Any]:
    return {
        "lot_id": lot_id,
        "item": "배추",
        "available_qty_kg": 1000,
        "remaining_freshness_days": remaining,
        "effective_freshness_limit_days": limit,
    }


def _request(
    *,
    lots: list[dict[str, Any]] | None = None,
    soft_warnings: list[dict[str, str]] | None = None,
    finance_context: dict[str, Any] | None = None,
    ml: bool = True,
    cost_amount: int = 7_000_000,
    raw_text: str | None = None,
) -> SalesProposalInput:
    payload: dict[str, Any] = {
        "business_mode": "SPOT_SALES",
        "user_request": {
            "raw_text": raw_text,
            "item": "배추",
            "partner_id": "CUST-1",
            "requested_quantity_kg": 7000,
            "preferred_unit_price_krw": 1400,
            "preferred_delivery_date": DELIVERY,
            "source_ref": "sim_runs/PLAN#sales_terms/ML_CURRENT_PRICE",
        },
        "logistics_context": {
            "query_scope": {"item": "배추"},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 7000}],
                "lot_constraints": lots or [],
                "supply_capacity_by_date": [
                    {"date": DELIVERY, "confirmed_sellable_quantity_kg": 7000}
                ],
                "inventory_cost_basis": {
                    "item": "배추",
                    "quantity_kg": 7000,
                    "amount_krw": cost_amount,
                    "allocation_method": "FEFO",
                    "cost_method": "ACTUAL",
                    "source_ref": "LOT-1",
                    "source_refs": ["LOT-1"],
                    "evidence_grade": "OFFICIAL",
                },
            },
            "delivery_feasibility": {"status": "READY", "earliest_delivery_date": DELIVERY},
            "soft_warnings": soft_warnings or [],
            "evidence_refs": ["LOG-SUPPLY"],
        },
    }
    if ml:
        payload["ml_context"] = {
            "as_of": "2026-09-16",
            "item": "배추",
            "target_kind": "WHSL",
            "unit": "원/kg",
            "current_price": 1400,
            "horizon_days": 2,
            "model_version": "PLAN-WHSL",
            "generated_at": datetime(2026, 9, 16, tzinfo=UTC),
            "use_recommended": True,
            "daily": [
                {"date": date(2026, 9, 17), "lower": 1340, "predicted": 1440, "upper": 1590},
                {"date": DELIVERY, "lower": 1350, "predicted": 1450, "upper": 1600},
            ],
        }
    if finance_context is not None:
        payload["finance_context"] = finance_context
    return SalesProposalInput.model_validate(payload)


@pytest.fixture(autouse=True)
def _planner_off(monkeypatch):
    """기본은 모델을 안 부른다 — 부르는 검사만 명시적으로 켠다."""
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


# ---------------------------------------------------------------------------
# Fixture A — 정상 재고
# ---------------------------------------------------------------------------


def test_정상_재고에서_세_자세가_갈린다():
    """세 전략이 같은 자세를 들고 있으면 A/B/C 를 나눈 의미가 없다."""
    plan, signals = plan_strategies(_request())

    assert signals.depletion_pressure is False
    자세 = {p.strategy: p.price_posture for p in plan.profiles}
    assert 자세["CONSERVATIVE"] == "MARGIN_DEFENSE"
    assert 자세["BALANCED"] == "MARKET_ALIGNED"
    수량 = {p.strategy: p.quantity_posture for p in plan.profiles}
    assert 수량["CONSERVATIVE"] == "LIMITED"
    assert 수량["AGGRESSIVE"] == "EXPANDED"


def test_정상_재고에서는_공격안이_시장_하단으로_안_간다():
    """🔴 근거 없이 싸게 파는 것을 막는 자리다."""
    plan, _signals = plan_strategies(_request())

    aggressive = plan.of("AGGRESSIVE")
    assert aggressive.price_posture == "MARKET_ALIGNED"
    assert "DEPLETION_SIGNAL_ABSENT" in aggressive.reason_codes


def test_보수안이_공격안보다_비싸다():
    """자세가 실제 단가로 옮겨졌는가 — 자세만 갈리고 숫자가 같으면 소용이 없다."""
    scenarios = {s.scenario_type: s for s in _generate_scenarios(_request())}

    assert scenarios["CONSERVATIVE"].unit_price_krw > scenarios["BALANCED"].unit_price_krw


# ---------------------------------------------------------------------------
# Fixture B — 신선도 위험
# ---------------------------------------------------------------------------

_FRESHNESS = [{"code": "FRESHNESS_QUALITY_RISK"}]

#: 그 품목의 로트가 창고에 있다는 사실. **위험 판정이 아니다** — 물류가 낸
#: `FRESHNESS_QUALITY_RISK` 가 이 품목에 걸리는지를 가르는 재료다.
_ITEM_LOTS = [_lot("LOT-배추-1", remaining=3, limit=10)]


def _freshness_request(**over) -> SalesProposalInput:
    """신선도 위험이 **이 품목에** 걸린 요청."""
    return _request(soft_warnings=_FRESHNESS, lots=_ITEM_LOTS, **over)


def test_신선도_위험이_소진_압력으로_읽힌다():
    """★★ **되먹임 없이 1차 입력에서 읽는다** — 여기가 끊겨 있던 자리다.

    종전에는 `sell_priority` 를 `domain_replies` 에서만 읽었는데 그 칸은 물류
    `PRE_SALES` payload 에 없다. 실측에서 판매 안 17,364 건 전부 NULL 이었다.
    """
    signals = derive_signals(_freshness_request())

    assert signals.depletion_pressure is True
    assert signals.freshness_risk_codes == ("FRESHNESS_QUALITY_RISK",)
    assert signals.sell_priority is None, "되먹임에 없던 값을 지어냈다"


def test_다른_품목_신선도_위험으로_이_품목을_싸게_팔지_않는다():
    """★★ **실제 실행에서 잡은 사고다** (`SIM-CHAIN-REH-0914` · 2026-09-14).

    ```text
    soft_warnings   [FRESHNESS_QUALITY_RISK]   ← 양파 로트에서 난 신호
    요청 품목        배추                        ← 그날 배추 로트가 하나도 없다
    → 배추 공격안이 시장 하단 1,321 원으로 내려갔다
    ```

    `soft_warnings` 는 코드만 있고 **어느 품목인지가 없다.** 품목을 특정할 수 없는
    신호로 가격을 내리는 것이 §7 이 막으려는 «근거 없이 싸게 파는 것» 이다.
    """
    signals = derive_signals(_request(soft_warnings=_FRESHNESS, lots=[]))

    assert signals.depletion_pressure is False
    assert signals.freshness_risk_codes == ("FRESHNESS_QUALITY_RISK",), (
        "신호가 있었다는 사실 자체는 남아야 한다"
    )


def test_신선도_신호_없이_로트만으로는_소진이_아니다():
    """🔴 **신선도 임계의 주인은 물류다** (실측으로 되돌린 자리).

    한때 판매가 `remaining_freshness_days <= effective_freshness_limit_days` 로
    «한계 도달» 을 스스로 판정했다. 그 비교는 거꾸로였다 — 뒤의 값은 임계가 아니라
    **분모**라서, 실제 실행에서 `remaining 10 / limit 10` 인 **갓 입고된 최상 로트**가
    위험으로 읽혔다 (`SIM-CHAIN-CHECK-0916` · 2026-09-10 · 배추 491kg).
    """
    signals = derive_signals(_request(lots=[_lot("LOT-1", remaining=10, limit=10)]))

    assert signals.depletion_pressure is False
    assert signals.item_lot_ids == ("LOT-1",), "로트가 있다는 사실은 남는다"


def test_그_품목_로트가_있을_때만_창고_신호를_소진으로_읽는다():
    signals = derive_signals(_freshness_request())

    assert signals.depletion_pressure is True
    assert signals.item_lot_ids == ("LOT-배추-1",)


def test_신선도_위험에서_공격안이_실제_소진_전략이_된다():
    """🔴 **BALANCED 로 접히지 않는다** — 그 접힘이 C 안을 죽여 온 자리다."""
    plan, _signals = plan_strategies(_freshness_request())

    assert plan.of("AGGRESSIVE").price_posture == "DEPLETION"
    assert plan.of("AGGRESSIVE").inventory_posture == "FRESHNESS_RISK_FIRST"


def test_소진_전략이_시장_하단_단가로_옮겨진다():
    scenarios = {s.scenario_type: s for s in _generate_scenarios(_freshness_request())}
    aggressive = scenarios["AGGRESSIVE"]

    assert aggressive.unit_price_krw < scenarios["BALANCED"].unit_price_krw
    assert any("INVENTORY_DEPLETION" in line for line in aggressive.rationale)


def test_소진_전략도_마진_최저선_아래로는_안_간다():
    """🔴 자세가 가드레일을 넘지 못한다."""
    # 원가를 올려 마진 최저선이 시장 하단보다 높아지게 만든다.
    scenarios = {
        s.scenario_type: s
        for s in _generate_scenarios(_freshness_request(cost_amount=7_000_000 + 2_000_000))
    }

    assert scenarios["AGGRESSIVE"].unit_price_krw >= Decimal(1350)


def test_세_안_모두_소진_전략에서도_단가가_세_가지다():
    """실측에서 A/B/C 단가가 세 가지였던 실행은 0 건이었다."""
    scenarios = _generate_scenarios(_freshness_request())

    assert len({s.unit_price_krw for s in scenarios}) == 3


# ---------------------------------------------------------------------------
# Fixture C — 제약 수렴
# ---------------------------------------------------------------------------


def test_마진_최저선_때문에_같아지면_강제로_벌리지_않는다():
    """🔴 숫자를 억지로 벌리면 그 차이는 근거 없는 값이 된다.

    원가가 높아 세 자세가 전부 마진 최저선에 붙으면 단가가 같아지는 것이 맞다.
    그 사실은 `rationale` 의 가격 전략으로 되짚을 수 있다.
    """
    scenarios = {
        s.scenario_type: s for s in _generate_scenarios(_freshness_request(cost_amount=11_000_000))
    }
    단가 = {t: s.unit_price_krw for t, s in scenarios.items()}

    assert 단가["BALANCED"] == 단가["AGGRESSIVE"], "제약이 같은데 숫자가 갈렸다"
    assert any("MARGIN_FLOOR" in line for line in scenarios["AGGRESSIVE"].rationale), (
        "수렴 원인이 근거에 안 남았다"
    )


def test_수렴하면_회신이_그_사실과_원인을_말한다():
    """§8 — 숫자를 억지로 벌리지 않는 대신 **무엇이 묶었는지**를 남긴다."""
    from app.sales.proposal import run_proposal

    reply = run_proposal(_freshness_request(cost_amount=11_000_000))

    assert reply.strategy_collapsed is True
    assert "MARGIN_FLOOR" in reply.strategy_collapse_reason_codes


def test_자세가_갈리고_숫자도_갈리면_수렴이_아니다():
    """세 안이 서로 다른 값에 닿았으면 묶인 것이 없다."""
    from app.sales.proposal import run_proposal

    reply = run_proposal(_freshness_request())

    assert reply.strategy_collapsed is False
    assert reply.strategy_collapse_reason_codes == []


def test_수렴_원인은_세_안을_다_묶은_코드만_적는다():
    """한 안에만 있는 코드는 수렴을 설명하지 못한다."""
    from app.sales.proposal import run_proposal

    reply = run_proposal(_freshness_request(cost_amount=11_000_000))

    assert "MARKET_UPPER" not in reply.strategy_collapse_reason_codes


def test_수렴해도_자세는_기록에_남는다():
    """숫자가 같아도 **무엇을 하려 했는지**는 다르다."""
    scenarios = {
        s.scenario_type: s for s in _generate_scenarios(_freshness_request(cost_amount=11_000_000))
    }

    assert scenarios["AGGRESSIVE"].strategy_profile.price_posture == "DEPLETION"
    assert scenarios["CONSERVATIVE"].strategy_profile.price_posture == "MARGIN_DEFENSE"


# ---------------------------------------------------------------------------
# 재무 선행 사실이 전략에 영향을 준다
# ---------------------------------------------------------------------------


def _finance(
    *,
    pressure: str,
    credit_available: float | None,
    payables_due_7d: float = 3_000_000.0,
    projected_cash_min: float = 25_000_000.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available_cash": 31_000_000.0,
        "base_projected_cash_min": projected_cash_min,
        "minimum_cash_balance_krw": 12_941_280.0,
        "payables_total_krw": 9_000_000.0,
        "payables_due_7d_krw": payables_due_7d,
        "payables_due_30d_krw": 9_000_000.0,
        "receivables_total_krw": 7_000_000.0,
        "payment_pressure": pressure,
        "partner_credit": {"partner_id": "CUST-1", "partner_receivable_krw": 0.0},
    }
    if credit_available is not None:
        payload["partner_credit"]["partner_credit_limit_krw"] = 10_000_000.0
        payload["partner_credit"]["partner_credit_available_krw"] = credit_available
    return payload


def test_자금이_넉넉하면_보수안_자세가_방어로_가지_않는다():
    plan, signals = plan_strategies(
        _request(finance_context=_finance(pressure="LOW", credit_available=9_000_000.0))
    )

    assert signals.cash_is_tight is False
    assert plan.of("CONSERVATIVE").cash_posture == "NORMAL"
    assert "CASH_PRESSURE" not in plan.of("CONSERVATIVE").reason_codes


def test_자금_압박이_높으면_같은_재고_같은_ML_에서도_자세가_바뀐다():
    """🔴 **가격 숫자를 모델이 바꾸는 방식으로 재지 않는다** (§25).

    재고·ML·사용자 요청은 그대로이고 재무 사실만 다르다. 달라지는 것은 자세다.
    """
    넉넉 = _request(finance_context=_finance(pressure="LOW", credit_available=9_000_000.0))
    빠듯 = _request(finance_context=_finance(pressure="HIGH", credit_available=0.0))

    a, _ = plan_strategies(넉넉)
    b, _ = plan_strategies(빠듯)

    assert a.of("CONSERVATIVE").cash_posture != b.of("CONSERVATIVE").cash_posture
    assert "CREDIT_EXHAUSTED" in b.of("CONSERVATIVE").reason_codes


def test_여신한도를_모르면_여력_없음으로_읽지_않는다():
    """모르는 것을 *"여력 없음"* 으로 읽으면 한도가 안 선 거래처가 전부 막힌다."""
    signals = derive_signals(
        _request(finance_context=_finance(pressure="LOW", credit_available=None))
    )

    assert signals.credit_available_krw is None
    assert signals.credit_is_exhausted is False
    assert signals.credit_limit_known is False


def test_임박_채무가_크고_투영_현금이_낮으면_자세가_방어로_간다():
    """§26 Case 1 vs Case 2 — 재고·ML·사용자 요청은 그대로이고 재무만 다르다.

    🔴 **가격 숫자를 모델이 바꾸는 방식으로 재지 않는다.** 달라지는 것은 자세이고,
      그 자세가 단가가 되는 것은 결정론 계산의 몫이다.

    ★ `payment_pressure` 라벨이 없어도 판단한다 — 급여 출처가 없는 실행에서는 재무가
      투영 라벨을 안 낸다. 그때는 투영 최저와 최소현금 두 값으로 본다.
    """
    case1 = _request(
        finance_context=_finance(
            pressure="LOW",
            credit_available=9_000_000.0,
            payables_due_7d=0.0,
            projected_cash_min=25_000_000.0,
        )
    )
    case2 = _request(
        finance_context=_finance(
            pressure="LOW",  # 라벨은 같다 — 숫자만 다르다
            credit_available=9_000_000.0,
            payables_due_7d=28_000_000.0,
            projected_cash_min=3_000_000.0,
        )
    )

    a, sa = plan_strategies(case1)
    b, sb = plan_strategies(case2)

    assert sa.cash_is_tight is False
    assert sb.cash_is_tight is True, "투영 최저가 최소현금 아래인데 빠듯하지 않다고 읽었다"
    assert a.of("CONSERVATIVE").cash_posture == "NORMAL"
    assert b.of("CONSERVATIVE").cash_posture == "DEFENSIVE"
    assert "CASH_PRESSURE" in b.of("CONSERVATIVE").reason_codes


def test_재무만_달라도_같은_재고_ML_에서_단가는_안_바뀐다():
    """🔴 자세는 재무를 보지만 **가격은 원가·시장·마진만 본다.**

    재무 사실이 단가를 직접 움직이면 판정 전에 재무가 값을 정하는 것이 된다.
    """
    case1 = _request(finance_context=_finance(pressure="LOW", credit_available=9_000_000.0))
    case2 = _request(
        finance_context=_finance(
            pressure="HIGH", credit_available=0.0, projected_cash_min=3_000_000.0
        )
    )

    assert [s.unit_price_krw for s in _generate_scenarios(case1)] == [
        s.unit_price_krw for s in _generate_scenarios(case2)
    ]


def test_재무_사실이_없으면_그_사실이_사유에_남는다():
    plan, signals = plan_strategies(_request())

    assert signals.has_finance_context is False
    assert "FINANCE_CONTEXT_ABSENT" in plan.of("CONSERVATIVE").reason_codes


# ---------------------------------------------------------------------------
# 모델의 권한 — 자세뿐이다
# ---------------------------------------------------------------------------


def _llm_plan(**over) -> LlmStrategyPlanOutput:
    base = {
        "CONSERVATIVE": {
            "price_posture": "MARGIN_DEFENSE",
            "quantity_posture": "LIMITED",
            "inventory_posture": "NORMAL",
            "credit_posture": "STRICT",
            "cash_posture": "DEFENSIVE",
        },
        "BALANCED": {
            "price_posture": "MARKET_ALIGNED",
            "quantity_posture": "NORMAL",
            "inventory_posture": "FIFO",
            "credit_posture": "NORMAL",
            "cash_posture": "NORMAL",
        },
        "AGGRESSIVE": {
            "price_posture": "DEPLETION",
            "quantity_posture": "EXPANDED",
            "inventory_posture": "FRESHNESS_RISK_FIRST",
            "credit_posture": "WITHIN_LIMIT",
            "cash_posture": "CASH_CONVERSION",
        },
    }
    base.update(over)
    return LlmStrategyPlanOutput(
        strategies=[
            LlmStrategyProfileOutput(strategy=name, **fields) for name, fields in base.items()
        ]
    )


@pytest.fixture
def 모델을_켠다(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("SALES_LLM_MODEL", "test-model")


def _stub(monkeypatch, output):
    monkeypatch.setattr(
        "app.sales.llm.runtime._call_gemini_planner",
        lambda context, settings: output() if callable(output) else output,
    )


def test_모델이_고른_자세가_후보_생성에_실제로_반영된다(모델을_켠다, monkeypatch):
    """★ 여기가 *"LLM 이 전략에 참여한다"* 의 전부다."""
    _stub(monkeypatch, _llm_plan())

    plan, _signals = plan_strategies(_freshness_request())

    assert plan.source == "LLM"
    assert plan.llm_status == "SUCCESS"
    assert plan.of("AGGRESSIVE").price_posture == "DEPLETION"


def test_소진_신호가_없으면_모델이_골라도_내려간다(모델을_켠다, monkeypatch):
    """🔴 **자세가 사실을 이기지 못한다.** 모델이 싸게 파는 길을 스스로 못 연다."""
    _stub(monkeypatch, _llm_plan())

    plan, signals = plan_strategies(_request())  # 신선도 위험 없음

    assert signals.depletion_pressure is False
    assert plan.of("AGGRESSIVE").price_posture == "MARKET_ALIGNED"
    assert "AGGRESSIVE:DEPLETION_SIGNAL_ABSENT" in plan.clamped_reason_codes


def test_모델이_숫자를_섞으면_계획을_통째로_버린다(모델을_켠다, monkeypatch):
    output = _llm_plan()
    output.strategies[0].reason_codes = ["단가 1450 으로 올린다"]
    _stub(monkeypatch, output)

    plan, _signals = plan_strategies(_request())

    assert plan.source == "TEMPLATE_FALLBACK"
    assert plan.llm_status == "FALLBACK"


def test_전략이_셋이_아니면_계획을_통째로_버린다(모델을_켠다, monkeypatch):
    output = _llm_plan()
    output.strategies = output.strategies[:2]
    _stub(monkeypatch, output)

    plan, _signals = plan_strategies(_request())

    assert plan.source == "TEMPLATE_FALLBACK"


def test_모델_호출이_터져도_세_전략은_선다(모델을_켠다, monkeypatch):
    """§10 — 외부 모델 하나 때문에 판매안이 안 나오면 안 된다."""

    def 터진다(context, settings):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.sales.llm.runtime._call_gemini_planner", 터진다)

    plan, _signals = plan_strategies(_request())

    assert plan.llm_status == "FALLBACK"
    assert [p.strategy for p in plan.profiles] == ["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]


def test_설정이_꺼져_있으면_FALLBACK_이_아니라_DISABLED_다():
    """둘을 섞으면 *"안 켰네"* 와 *"켰는데 실패했네"* 가 구분되지 않는다."""
    plan, _signals = plan_strategies(_request())

    assert plan.llm_status == "DISABLED"
    assert plan.source == "TEMPLATE_FALLBACK"


def _sent_to_model(monkeypatch, request) -> dict[str, Any]:
    """모델에 실제로 나간 context 를 잡는다."""
    본것: list[Any] = []

    def 잡는다(context, settings):
        본것.append(context.model_dump())
        return _llm_plan()

    monkeypatch.setattr("app.sales.llm.runtime._call_gemini_planner", 잡는다)
    plan_strategies(request)
    return 본것[0]


def test_모델이_재무_물류_ML_사실을_전부_본다(모델을_켠다, monkeypatch):
    """★ 자세를 고르려면 사실이 손에 있어야 한다 (§4).

    숫자를 **보여 주는** 것과 숫자를 **받는** 것은 다르다. 모델 출력에는 숫자를 담을
    칸이 없고(`LlmStrategyProfileOutput`), 단가·수량은 결정론 계산이 자세만 읽고
    만든다 — 모델이 본 숫자가 가격이 될 길이 없다.
    """
    보낸것 = _sent_to_model(
        monkeypatch,
        _freshness_request(finance_context=_finance(pressure="HIGH", credit_available=0.0)),
    )

    # 재무
    assert 보낸것["available_cash_krw"] == 31_000_000.0
    assert 보낸것["payables_due_7d_krw"] == 3_000_000.0
    assert 보낸것["receivables_total_krw"] == 7_000_000.0
    assert 보낸것["credit_state"] == "EXHAUSTED"
    assert 보낸것["payment_pressure"] == "HIGH"
    # 물류
    assert 보낸것["depletion_pressure"] is True
    assert 보낸것["freshness_risk_codes"] == ["FRESHNESS_QUALITY_RISK"]
    assert 보낸것["inventory_available_kg"] == 7000.0
    assert 보낸것["inventory_cost_basis_known"] is True
    assert 보낸것["delivery_status"] == "READY"
    # ML
    assert 보낸것["ml_band_available"] is True
    assert (보낸것["ml_lower"], 보낸것["ml_predicted"], 보낸것["ml_upper"]) == (
        1350.0,
        1450.0,
        1600.0,
    )
    assert 보낸것["ml_target_kind"] == "WHSL"


def test_모델은_판정을_보지_않는다(모델을_켠다, monkeypatch):
    """🔴 판정 라벨을 주면 모델이 그것을 따라 적을 자리가 생긴다.

    후보가 아직 없으므로 판정도 없다 — 그 사실이 계약에 드러나야 한다.
    """
    보낸것 = _sent_to_model(monkeypatch, _request())

    금지 = {"finance_verdict", "verdict", "status", "unit_price_krw", "qty_kg", "amount_krw"}
    assert 금지.isdisjoint(보낸것), f"판정·결정값이 모델에 나갔다: {금지 & set(보낸것)}"


def test_사용자가_말로_남긴_의도가_모델에_간다(모델을_켠다, monkeypatch):
    """§16 — raw_text 는 자세를 고르는 참고다. 가격·수량을 바꾸지 않는다."""
    보낸것 = _sent_to_model(monkeypatch, _request(raw_text="이번 주 안에 급하게 털고 싶다"))

    assert 보낸것["user_intent_text"] == "이번 주 안에 급하게 털고 싶다"
    assert 보낸것["business_mode"] == "SPOT_SALES"


def test_raw_text_가_단가와_수량을_바꾸지_않는다():
    """🔴 문장은 자세 입력이지 값 입력이 아니다."""
    없이 = _generate_scenarios(_request())
    있이 = _generate_scenarios(_request(raw_text="최대한 비싸게 팔아줘 2000원 이상"))

    assert [s.unit_price_krw for s in 없이] == [s.unit_price_krw for s in 있이]
    assert [s.quantity_kg for s in 없이] == [s.quantity_kg for s in 있이]


def test_되먹임_사유_코드가_모델에_간다(모델을_켠다, monkeypatch):
    """재계획에서 **무엇이 막았는지**를 모르면 같은 자세를 다시 고른다."""
    from app.sales.strategy import derive_signals

    class _Reply:
        def __init__(self) -> None:
            self.source_agent = "finance"
            self.payload = {"reason_codes": ["SALES_CREDIT_LIMIT_EXCEEDED"]}

    signals = derive_signals(_request(), [_Reply()])

    assert signals.feedback_reason_codes == ("SALES_CREDIT_LIMIT_EXCEEDED",)


def test_회신에_전략_출처가_실린다(monkeypatch):
    """장애를 숨기지 않는다 — 화면이 모델이 죽은 날을 알 수 있어야 한다."""
    from app.sales.proposal import run_proposal

    reply = run_proposal(_request())

    assert reply.strategy_source == "TEMPLATE_FALLBACK"
    assert reply.strategy_llm_status == "DISABLED"


# ---------------------------------------------------------------------------
# 깎기 자체
# ---------------------------------------------------------------------------


def test_깎기는_한_방향이다():
    """사실이 허락한다고 더 공격적인 자세를 강요하지 않는다."""
    보수적인_계획 = template_profiles(StrategySignals())
    깎인것, 사유 = clamp_profiles(보수적인_계획, StrategySignals(depletion_pressure=True))

    assert [p.price_posture for p in 깎인것] == [p.price_posture for p in 보수적인_계획]
    assert 사유 == []


def test_모델을_안_켜면_템플릿이_그대로_나온다():
    outcome = plan_strategy_profiles(
        signals=StrategySignals(), template=template_profiles(StrategySignals())
    )

    assert outcome.source == "TEMPLATE_FALLBACK"
    assert outcome.llm_status == "DISABLED"
