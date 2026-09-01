"""Critic 배선 — 번역이 뜻을 바꾸지 않는가.

★ 여기서 보는 것은 Critic 의 판정이 아니라 **넘기기 전에 무엇을 지켰는가**다.
  판정 자체는 `tests/critic/` 이 본다. 두 번 검사하면 Critic 이 바뀔 때마다
  마스터 테스트가 깨진다.

★ 절반은 **안 넘기는 경우**다. 넘기면 안 되는 것을 넘기면 Critic 은 성실하게
  판정하고, 그 판정은 그럴듯하며, 틀렸다는 사실만 아무도 모른다.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.master import critic_bridge as bridge
from app.master.plan import ExecutionPlan
from app.master.verifier import MasterVerifier, VerificationContext
from app.orchestrator.contracts_core import Evidence

AS_OF = date(2025, 12, 31)


def _scenario(**over) -> dict:
    base = {
        "label": "기본",
        "strategy_type": "quantity",
        "coverage_days": 5,
        "total_qty_kg": 1000,
        "total_amount_krw": 1_650_000,
        "max_price": 1750,
        "margin_warning": False,
        "split_plan": [{"seq": 1, "date": "2026-01-01", "qty_kg": 1000}],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 1000, "grade_unit_price": 1650}
        ],
        "expected_margin_rate": 0.30,
        "rationale": [],
        "risks": [],
    }
    return {**base, **over}


def _proposal(*scenarios) -> dict:
    return {
        "scenarios": list(scenarios) or [_scenario()],
        "allowed_axes": ["quantity"],
        "situation": "stable",
        "confidence": "high",
    }


CONSTRAINTS = {
    "finance": {"finance_cap_amount_krw": 31_854_627.0, "available_cash": 31_993_913.77},
    "inventory": {
        "warehouse_free_kg": 7636.72,
        "inbound_lead_days": 2,
        "cap_by_date": {"2026-01-03": 5000.0},
    },
}

EVIDENCES = {
    "finance": (
        Evidence(
            claim="finance_cap_amount_krw",
            source="finance",
            ref_ids=("FIN-STATE-1",),
            value=31_854_627.0,
            unit="KRW",
        ),
        # ★ 이 근거는 cap 을 뒷받침하지 않는다 — 골라내는지 본다
        Evidence(
            claim="payment_pressure",
            source="tool_calc",
            ref_ids=("FIN-STATE-1",),
            value=3.2,
            unit="ratio",
        ),
    ),
    "inventory": (
        Evidence(
            claim="warehouse_free_kg",
            source="tool_calc",
            ref_ids=("DB:inventory_lots/x",),
            value=7636.72,
            unit="kg",
        ),
    ),
}


def _ctx(item: str | None = "배추") -> VerificationContext:
    return VerificationContext(as_of=AS_OF, item=item, evidences=EVIDENCES)


# ---------------------------------------------------------------------------
# 번역
# ---------------------------------------------------------------------------


def test_품목_단일가는_금액을_되돌린다():
    """🔴 **이 파일에서 가장 중요한 검사.**

    매입에는 등급별 단가만 있다. Critic 의 금액 축은 `qty × unit_price` 로 다시
    만들어지므로, 등급 단가 중 하나를 고르거나 평균을 내면 **매입이 주장한 금액과
    다른 금액을 검사**하게 된다. 숫자는 나오고 에러도 안 난다.
    """
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    scenario = req.scenarios[0]
    qty = scenario.qty_kg["배추"]
    price = scenario.unit_price_krw_per_kg["배추"]
    assert qty * price == pytest.approx(1_650_000)


def test_절대날짜를_offset_으로_되돌릴_수_있게_옮긴다():
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    leg = req.scenarios[0].split_plan[0]
    assert leg.offset_days == 1  # 2026-01-01 − 2025-12-31
    assert leg.expected_arrival_date == date(2026, 1, 3)  # + 리드타임 2


def test_리드타임이_없으면_도착일을_비운다():
    """0 일로 치면 **도착일 분해 검사가 통과해 버린다.**"""
    constraints = {**CONSTRAINTS, "inventory": {"warehouse_free_kg": 7636.72}}
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=constraints,
        evidences=EVIDENCES,
    )
    assert req.scenarios[0].split_plan[0].expected_arrival_date is None
    assert req.inbound_lead_days is None


def test_stance_는_매입_label_그대로다():
    """매입 `보수·기본·공격` 과 Critic `stance` 는 같은 어휘다 — 매핑표가 없다."""
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(_scenario(label="공격")),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    assert req.scenarios[0].stance == "공격"
    assert req.scenarios[0].scenario_id == "공격"


def test_cap_축을_뒷받침하는_근거만_붙인다():
    """근거는 많이 붙이는 것이 아니라 **가리키는 것이 맞아야** 한다."""
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    finance = next(r for r in req.replies if r.dept == "finance")
    claims = {e.claim for e in finance.checks[0].evidences}
    assert claims == {"finance_cap_amount_krw"}  # payment_pressure 는 빠진다


def test_dept_meta_를_보내지_않는다():
    """🔴 빈 dict 를 보내면 **모르는 것이 통과가 된다.**

    `inputs_used` 는 *"재무가 cap 을 낼 때 무엇을 읽었나"* 인데 마스터는 모른다.
    비워 보내면 Critic 의 등급 누출 검사가 "금지 입력 없음"으로 읽는다.
    안 보내면 Critic 이 `skipped` 에 남긴다.
    """
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    assert req.dept_meta is None


def test_rationale_을_비워_LLM_을_타지_않는다():
    """L5 판정 대상은 오케 selector 문장인데 1차 Flow 에 그 단계가 없다."""
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    assert req.rationale == ""


def test_sourcing_lot_에_ref_ids_를_지어내지_않는다():
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
    )
    assert req.scenarios[0].sourcing_plan[0].ref_ids == []


# ---------------------------------------------------------------------------
# 안 넘기는 경우
# ---------------------------------------------------------------------------


def test_품목을_모르면_넘기지_않는다():
    with pytest.raises(bridge.CriticSkipped):
        bridge.build_request(
            as_of=AS_OF,
            item=None,
            proposal=_proposal(),
            constraints=CONSTRAINTS,
            evidences=EVIDENCES,
        )


def test_밴드_축이_없으면_넘기지_않는다():
    """cap 없는 회신을 넘기면 Critic 밴드가 무한대가 되어 **무제한 매입이 통과**한다."""
    with pytest.raises(bridge.CriticSkipped):
        bridge.build_request(
            as_of=AS_OF,
            item="배추",
            proposal=_proposal(),
            constraints={"finance": {"available_cash": 1.0}, "inventory": {}},
            evidences=EVIDENCES,
        )


def test_수량이_0_이면_그_시나리오를_옮기지_않는다():
    """단가가 `금액/수량` 이라 0 으로 나눌 수 없다 — 0 을 채우지 않고 뺀다."""
    with pytest.raises(bridge.CriticSkipped):
        bridge.build_request(
            as_of=AS_OF,
            item="배추",
            proposal=_proposal(_scenario(total_qty_kg=0)),
            constraints=CONSTRAINTS,
            evidences=EVIDENCES,
        )


# ---------------------------------------------------------------------------
# 검증 Tool 과의 결합
# ---------------------------------------------------------------------------


def _plan() -> ExecutionPlan:
    return ExecutionPlan(request_id="REQ-T", as_of=AS_OF)


def test_항등식이_깨지면_Critic_을_돌리지_않는다():
    """🔴 어긋난 숫자 위의 56검사는 **통과해도 뜻이 없다.**

    금액 축이 `qty × (금액/수량)` 으로 복원되므로, 항등식이 깨진 채 넘기면 Critic 은
    성실하게 판정하고 그 판정은 그럴듯하다 — 틀렸다는 사실만 아무도 모른다.
    """
    called: list[object] = []
    broken = _scenario(total_amount_krw=999)  # Σ(qty × 단가) = 1,650,000 과 어긋난다
    verifier = MasterVerifier(critic=lambda req: called.append(req))
    result = verifier(_proposal(broken), CONSTRAINTS, {}, _plan(), _ctx())

    assert called == []
    assert any("항등식이 깨져" in s for s in result.skipped)
    assert any("L-IDENTITY-AMOUNT" in f for f in result.findings)


def test_커버리지를_항상_적는다():
    """`findings: []` 만 보면 *"56검사를 통과했다"* 로 읽힌다."""
    result = MasterVerifier()(_proposal(), CONSTRAINTS, {}, _plan(), _ctx())
    assert any(s.startswith("Critic 커버리지 ") for s in result.skipped)
    assert any("/56" in s for s in result.skipped)


def test_맥락이_없으면_돌리지_않고_밝힌다():
    result = MasterVerifier()(_proposal(), CONSTRAINTS, {}, _plan(), None)
    assert any("실행 맥락 미전달" in s for s in result.skipped)


def test_Critic_이_없으면_그_사실이_남는다():
    result = MasterVerifier(critic=None)(_proposal(), CONSTRAINTS, {}, _plan(), _ctx())
    assert any("주입되지 않음" in s for s in result.skipped)


def test_Critic_이_죽어도_Flow_는_산다():
    """검증 실패는 **매입 판단의 실패가 아니다.** 예외로 올리면 Flow 가 통째로 ERROR 다."""

    def explode(req):
        raise RuntimeError("boom")

    result = MasterVerifier(critic=explode)(_proposal(), CONSTRAINTS, {}, _plan(), _ctx())
    assert any("RuntimeError" in c for c in result.concerns)
    assert any("실행 중 오류로 미판정" in s for s in result.skipped)
    assert result.findings == ()  # 재호출을 유발하지 않는다


def test_Critic_findings_는_재호출을_유발한다():
    """매입이 다시 만들면 달라질 수 있는 것이다 — `concerns` 가 아니다.

    재무 cap 을 넘는 금액이면 Critic L3 밴드 축이 잡는다.
    """
    over_cap = _scenario(
        total_qty_kg=1000,
        total_amount_krw=99_000_000,  # 항등식은 지키고 cap 만 넘긴다
        sourcing_plan=[
            {"market": "가락", "grade": "상", "qty_kg": 1000, "grade_unit_price": 99_000}
        ],
    )
    result = MasterVerifier()(_proposal(over_cap), CONSTRAINTS, {}, _plan(), _ctx())
    assert any("cap_amount_krw" in f for f in result.findings), result.findings


def test_허용목록_밖_어휘는_통과로_치지_않는다():
    """🔴 `strategy_type` 이 어휘 밖이면 **Critic 요청 자체가 안 만들어진다.**

    이때 조용히 넘어가면 *"Critic 이 봤고 문제없었다"* 로 읽힌다. 어느 필드가
    걸렸는지 적어 `skipped` 에 남긴다.
    """
    result = MasterVerifier()(
        _proposal(_scenario(strategy_type="없는축")), CONSTRAINTS, {}, _plan(), _ctx()
    )
    assert any("Critic 계약에 맞지 않는다" in s and "strategy_type" in s for s in result.skipped)
    assert not any("돌지 못했다" in c for c in result.concerns)


# ---------------------------------------------------------------------------
# 부서 DeptMeta — 마스터는 나르고 Critic 이 검사한다
#
# 🔴 오래 `DeptMeta 미제출 — E-AUTHORITY·E-GRADE-LEAK 생략` 이 떴다. 마스터가
#    *"재무가 cap 을 낼 때 무엇을 읽었나"* 를 모르기 때문인데, 모르는 것을 빈 dict 로
#    보내면 **모르는 것이 통과가 된다.** 이제 재무가 자기 실행을 보고 적어 보내고
#    마스터는 그것을 해석 없이 옮긴다.
# ---------------------------------------------------------------------------


def _finance_meta_observation(
    *, inputs: list[str] | None = None, produced: list[str] | None = None
) -> str:
    """재무가 `ExecutionMetadata.observations` 에 넣는 것과 같은 모양."""
    return json.dumps(
        {
            "observation_type": "finance_dept_meta",
            "inputs_used": {
                "finance_cap_amount_krw": inputs
                if inputs is not None
                else [
                    "finance_state.current_cash_krw",
                    "finance_policy.minimum_cash_balance_krw",
                    "finance_policy.purchase_payment_days",
                ]
            },
            "produced_fields": produced
            if produced is not None
            else ["available_cash", "finance_cap_amount_krw", "payment_pressure"],
        }
    )


def _ctx_with_meta(observation: str) -> VerificationContext:
    return VerificationContext(
        as_of=AS_OF,
        item="배추",
        evidences=EVIDENCES,
        observations={"finance": (observation,)},
    )


def test_부서가_적어_보낸_dept_meta_를_그대로_옮긴다():
    """마스터는 **추측하지 않는다** — 부서가 적은 값을 Critic 어휘로 옮기기만 한다."""
    req = bridge.build_request(
        as_of=AS_OF,
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
        observations={"finance": (_finance_meta_observation(),)},
    )
    assert req.dept_meta is not None
    meta = req.dept_meta["finance"]
    assert meta.inputs_used["finance_cap_amount_krw"] == [
        "finance_state.current_cash_krw",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ]
    assert "finance_cap_amount_krw" in meta.produced_fields
    # 재무만 냈으면 재무만 있다 — 물류 것을 지어내지 않는다.
    assert set(req.dept_meta) == {"finance"}


def test_관측이_없거나_모양이_어긋나면_보내지_않는다():
    """부서가 잘못 적은 것을 마스터가 고쳐 주면 **고친 값이 근거가 된다.**"""
    for observations in (
        {"finance": ("not json at all",)},
        {"finance": (json.dumps({"observation_type": "finance_llm_provider"}),)},
        {"finance": (json.dumps({"observation_type": "finance_dept_meta"}),)},
        {},
    ):
        req = bridge.build_request(
            as_of=AS_OF,
            item="배추",
            proposal=_proposal(),
            constraints=CONSTRAINTS,
            evidences=EVIDENCES,
            observations=observations,
        )
        assert req.dept_meta is None, observations


def test_정상_dept_meta_는_미제출_경고를_없애고_두_검사를_돌린다():
    """단순히 문구가 사라지는 것이 아니라 **검사가 실제로 돈다.**"""
    result = MasterVerifier()(
        _proposal(), CONSTRAINTS, {}, _plan(), _ctx_with_meta(_finance_meta_observation())
    )
    assert not any("finance: DeptMeta 미제출" in s for s in result.skipped), result.skipped
    # 재무 소유 입력만 썼으므로 두 검사 모두 findings 를 내지 않는다.
    assert not any("E-GRADE-LEAK" in f for f in result.findings), result.findings
    assert not any("E-AUTHORITY" in f for f in result.findings), result.findings


def test_dept_meta_가_없으면_미제출_경고가_그대로_남는다():
    """반례 — 경고를 숨긴 것이 아니라 제출했을 때만 사라진다."""
    result = MasterVerifier()(_proposal(), CONSTRAINTS, {}, _plan(), _ctx())
    assert any("finance: DeptMeta 미제출" in s for s in result.skipped), result.skipped


@pytest.mark.parametrize("leaked", ["qty_kg", "grade_unit_price", "sourcing_plan"])
def test_재무_cap_에_매입_소유_입력이_섞이면_E_GRADE_LEAK(leaked):
    """§3.6.8 — 재무 상한이 등급·수량을 읽으면 하루 한 번 회신 계약이 깨진다."""
    observation = _finance_meta_observation(inputs=["finance_state.current_cash_krw", leaked])
    result = MasterVerifier()(_proposal(), CONSTRAINTS, {}, _plan(), _ctx_with_meta(observation))
    assert any("E-GRADE-LEAK" in f for f in result.findings), result.findings
    assert any(leaked in f for f in result.findings), result.findings


def test_S3_전속_필드를_산출하면_E_AUTHORITY():
    """§5.0 — `has_unmet_obligation` 은 오케 전속 판정이다. 부서가 내면 위반이다."""
    observation = _finance_meta_observation(
        produced=["finance_cap_amount_krw", "has_unmet_obligation"]
    )
    result = MasterVerifier()(_proposal(), CONSTRAINTS, {}, _plan(), _ctx_with_meta(observation))
    assert any("E-AUTHORITY" in f for f in result.findings), result.findings
    assert any("has_unmet_obligation" in f for f in result.findings), result.findings


def test_split_legs_는_매입_도착일을_그대로_옮긴다():
    """★ 매입 #141 — 여기가 "같은 사실을 두 곳에서 계산하는" 마지막 자리였다.

    매입 값(01-09)이 N4 계산(01-02)과 달라도 매입 값이 이긴다."""
    from app.master.critic_bridge import _split_legs

    scenario = {
        "split_plan": [
            {"date": "2025-12-31", "qty_kg": 44.0, "expected_arrival_date": "2026-01-09"}
        ]
    }
    legs = _split_legs(scenario, "피마늘", date(2025, 12, 31), lead=2)

    assert legs[0]["expected_arrival_date"] == "2026-01-09"


def test_split_legs_는_매입_값이_없으면_폴백_계산한다():
    from app.master.critic_bridge import _split_legs

    scenario = {"split_plan": [{"date": "2025-12-31", "qty_kg": 44.0}]}
    legs = _split_legs(scenario, "피마늘", date(2025, 12, 31), lead=2)

    assert legs[0]["expected_arrival_date"] == "2026-01-02"
