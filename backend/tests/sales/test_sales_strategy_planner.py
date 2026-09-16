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
) -> SalesProposalInput:
    payload: dict[str, Any] = {
        "business_mode": "SPOT_SALES",
        "user_request": {
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


def test_신선도_위험이_소진_압력으로_읽힌다():
    """★★ **되먹임 없이 1차 입력에서 읽는다** — 여기가 끊겨 있던 자리다.

    종전에는 `sell_priority` 를 `domain_replies` 에서만 읽었는데 그 칸은 물류
    `PRE_SALES` payload 에 없다. 실측에서 판매 안 17,364 건 전부 NULL 이었다.
    """
    signals = derive_signals(_request(soft_warnings=_FRESHNESS))

    assert signals.depletion_pressure is True
    assert signals.freshness_risk_codes == ("FRESHNESS_QUALITY_RISK",)
    assert signals.sell_priority is None, "되먹임에 없던 값을 지어냈다"


def test_로트_신선도_한계도_소진_압력이다():
    """물류가 낸 두 숫자를 비교만 한다 — 새 임계값을 만들지 않는다."""
    signals = derive_signals(_request(lots=[_lot("LOT-1", remaining=2, limit=3)]))

    assert signals.depletion_pressure is True
    assert signals.freshness_risk_lot_ids == ("LOT-1",)


def test_한계를_모르는_로트는_위험이_아니다():
    """🔴 분모를 모르면 *"며칠 남았으면 위험"* 을 판매가 정하게 된다."""
    signals = derive_signals(_request(lots=[_lot("LOT-1", remaining=2, limit=None)]))

    assert signals.depletion_pressure is False


def test_신선도_위험에서_공격안이_실제_소진_전략이_된다():
    """🔴 **BALANCED 로 접히지 않는다** — 그 접힘이 C 안을 죽여 온 자리다."""
    plan, _signals = plan_strategies(_request(soft_warnings=_FRESHNESS))

    assert plan.of("AGGRESSIVE").price_posture == "DEPLETION"
    assert plan.of("AGGRESSIVE").inventory_posture == "FRESHNESS_RISK_FIRST"


def test_소진_전략이_시장_하단_단가로_옮겨진다():
    scenarios = {
        s.scenario_type: s for s in _generate_scenarios(_request(soft_warnings=_FRESHNESS))
    }
    aggressive = scenarios["AGGRESSIVE"]

    assert aggressive.unit_price_krw < scenarios["BALANCED"].unit_price_krw
    assert any("INVENTORY_DEPLETION" in line for line in aggressive.rationale)


def test_소진_전략도_마진_최저선_아래로는_안_간다():
    """🔴 자세가 가드레일을 넘지 못한다."""
    # 원가를 올려 마진 최저선이 시장 하단보다 높아지게 만든다.
    scenarios = {
        s.scenario_type: s
        for s in _generate_scenarios(
            _request(soft_warnings=_FRESHNESS, cost_amount=7_000_000 + 2_000_000)
        )
    }

    assert scenarios["AGGRESSIVE"].unit_price_krw >= Decimal(1350)


def test_세_안_모두_소진_전략에서도_단가가_세_가지다():
    """실측에서 A/B/C 단가가 세 가지였던 실행은 0 건이었다."""
    scenarios = _generate_scenarios(_request(soft_warnings=_FRESHNESS))

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
        s.scenario_type: s
        for s in _generate_scenarios(
            _request(soft_warnings=_FRESHNESS, cost_amount=11_000_000)
        )
    }
    단가 = {t: s.unit_price_krw for t, s in scenarios.items()}

    assert 단가["BALANCED"] == 단가["AGGRESSIVE"], "제약이 같은데 숫자가 갈렸다"
    assert any(
        "MARGIN_FLOOR" in line for line in scenarios["AGGRESSIVE"].rationale
    ), "수렴 원인이 근거에 안 남았다"


def test_수렴해도_자세는_기록에_남는다():
    """숫자가 같아도 **무엇을 하려 했는지**는 다르다."""
    scenarios = {
        s.scenario_type: s
        for s in _generate_scenarios(
            _request(soft_warnings=_FRESHNESS, cost_amount=11_000_000)
        )
    }

    assert scenarios["AGGRESSIVE"].strategy_profile.price_posture == "DEPLETION"
    assert scenarios["CONSERVATIVE"].strategy_profile.price_posture == "MARGIN_DEFENSE"


# ---------------------------------------------------------------------------
# 재무 선행 사실이 전략에 영향을 준다
# ---------------------------------------------------------------------------


def _finance(*, pressure: str, credit_available: float | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available_cash": 31_000_000.0,
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

    plan, _signals = plan_strategies(_request(soft_warnings=_FRESHNESS))

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


def test_모델은_금액을_보지_않는다(모델을_켠다, monkeypatch):
    """🔴 금액을 보여 주면 모델이 그것을 문장에 옮기고, 사실의 주인이 둘이 된다."""
    본것: list[Any] = []

    def 잡는다(context, settings):
        본것.append(context.model_dump())
        return _llm_plan()

    monkeypatch.setattr("app.sales.llm.runtime._call_gemini_planner", 잡는다)

    plan_strategies(
        _request(finance_context=_finance(pressure="HIGH", credit_available=0.0))
    )

    보낸것 = 본것[0]
    assert 보낸것["credit_state"] == "EXHAUSTED"
    금액_같은_값 = [
        value
        for value in 보낸것.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    assert 금액_같은_값 == [], f"모델에 숫자가 나갔다: {보낸것}"


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
