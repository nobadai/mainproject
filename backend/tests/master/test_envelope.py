"""M-1 공통 이벤트 규약 v0.2 — 봉투 계약 테스트.

계약이 **실제로 막는지**를 고정한다. 각 테스트는 규약 조항 하나에 대응한다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
    agent_allowed_modes,
    check_evidence_coverage,
    check_reasoning,
    validate_reply,
)
from app.orchestrator.contracts_core import (
    ContractViolation,
    Evidence,
    SuggestedAdjustment,
)

AS_OF = date(2026, 8, 26)


def ctx(request_id: str = "REQ-1", as_of: date = AS_OF) -> ExecutionContext:
    return ExecutionContext(
        request_id=request_id,
        as_of=as_of,
        trigger="ML_COMPLETE",
        policy_version="v1.3-PROVISIONAL",
    )


def req(agent="finance", mode="PRE_PURCHASE", **kw) -> AgentRequest:
    return AgentRequest(context=kw.pop("context", ctx()), agent=agent, mode=mode, **kw)


def reply(**kw) -> AgentReply:
    base = {
        "request_id": "REQ-1",
        "as_of": AS_OF,
        "agent": "finance",
        "mode": "PRE_PURCHASE",
        "run_id": "FIN-RUN-1",
        "runtime_status": "READY",
        "business_status": "ok",
    }
    base.update(kw)
    return AgentReply(**base)


def ev(claim: str, value: float = 1.0, unit: str = "KRW") -> Evidence:
    return Evidence(
        claim=claim,
        source="finance",
        ref_ids=(f"REF-{claim}",),
        value=value,
        unit=unit,
        evidence_grade="OFFICIAL",
    )


# ---------------------------------------------------------------------------
# 타입 레벨 — 봉투가 성립하지 않는 것은 보낼 수 없다
# ---------------------------------------------------------------------------


def test_request_id_는_비울_수_없다():
    with pytest.raises(ContractViolation, match="request_id"):
        ctx(request_id="  ")


def test_policy_version_은_비울_수_없다():
    with pytest.raises(ContractViolation, match="policy_version"):
        ExecutionContext(request_id="REQ-1", as_of=AS_OF, trigger="ML_COMPLETE", policy_version="")


def test_call_seq_는_1_이상():
    with pytest.raises(ContractViolation, match="call_seq"):
        req(call_seq=0)


def test_에이전트가_못_받는_mode_는_거부된다():
    # 매입은 경계를 제공하는 조언자가 아니다
    with pytest.raises(ContractViolation, match="PRE_PURCHASE"):
        req(agent="purchase", mode="PRE_PURCHASE")


def test_mode_허용_집합():
    assert "GENERATE_SCENARIOS" in agent_allowed_modes("purchase")
    assert "GENERATE_SCENARIOS" not in agent_allowed_modes("finance")


def test_run_id_는_비울_수_없다():
    # 검증 Tool 이 ExecutionMetadata 를 찾는 키다
    with pytest.raises(ContractViolation, match="run_id"):
        reply(run_id="")


def test_RUNTIME_NOT_READY_는_missing_data_를_밝혀야_한다():
    with pytest.raises(ContractViolation, match="missing_data"):
        reply(runtime_status="RUNTIME_NOT_READY", business_status="skipped")


def test_RUNTIME_NOT_READY_에_이름이_있으면_통과():
    r = reply(
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=("payroll_schedule",),
    )
    assert not r.contributes_to_band


def test_매입은_축_조정을_제안할_수_없다():
    adj = SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=1.0,
        unit="KRW",
        reason="r",
        ref_ids=("X",),
    )
    with pytest.raises(ContractViolation, match="제안자"):
        reply(agent="purchase", mode="GENERATE_SCENARIOS", suggested_adjustments=(adj,))


def test_남의_부서_조정안이_섞이면_거부된다():
    adj = SuggestedAdjustment(
        dept="inventory",
        axis="quantity",
        target_value=1.0,
        unit="kg",
        reason="r",
        ref_ids=("X",),
    )
    with pytest.raises(ContractViolation, match="섞였다"):
        reply(agent="finance", suggested_adjustments=(adj,))


def test_축_침범은_기존_계약이_이미_막는다():
    # 재무는 amount 축만 — 새 검사를 만들지 않고 기존 타입을 재사용한다
    with pytest.raises(ContractViolation, match="축"):
        SuggestedAdjustment(
            dept="finance",
            axis="quantity",
            target_value=1.0,
            unit="kg",
            reason="r",
            ref_ids=("X",),
        )


def test_tool_order_길이가_다르면_거부된다():
    with pytest.raises(ContractViolation, match="tool_order"):
        ExecutionMetadata(
            run_id="R",
            request_id="REQ-1",
            agent="finance",
            used_tools=("a", "b"),
            tool_order=(1,),
        )


# ---------------------------------------------------------------------------
# 상태 2종 — 재시도 정책이 갈린다
# ---------------------------------------------------------------------------


def test_ERROR_만_재시도_가치가_있다():
    assert reply(runtime_status="ERROR", business_status="skipped").worth_retry
    assert not reply(
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=("x",),
    ).worth_retry
    assert not reply().worth_retry


def test_reject_는_돌긴_돌았다():
    # 부서가 반대한 날 ≠ 부서가 죽은 날
    r = reply(business_status="reject")
    assert r.contributes_to_band
    assert not r.worth_retry


# ---------------------------------------------------------------------------
# 바인딩 — 스냅샷 폐지로 request_id·as_of 가 대조 기준이 됐다
# ---------------------------------------------------------------------------


def test_다른_요청의_회신은_걸린다():
    f = validate_reply(req(), reply(request_id="REQ-OTHER"))
    assert any(x.code == "E-BIND-REQUEST" for x in f)


def test_시점이_다르면_걸린다():
    f = validate_reply(req(), reply(as_of=date(2026, 8, 25)))
    assert any(x.code == "E-BIND-AS-OF" for x in f)


def test_다른_에이전트가_답하면_걸린다():
    f = validate_reply(req(), reply(agent="inventory"))
    assert any(x.code == "E-BIND-AGENT" for x in f)


def test_다른_mode_로_답하면_걸린다():
    f = validate_reply(req(), reply(mode="STATUS_QUERY"))
    assert any(x.code == "E-BIND-MODE" for x in f)


# ---------------------------------------------------------------------------
# Evidence — §1.2-3 의 집행 수단
# ---------------------------------------------------------------------------


def test_숫자에_근거가_없으면_걸린다():
    f = check_evidence_coverage(reply(payload={"available_cash": 42000000}))
    assert [x.code for x in f] == ["E-EVIDENCE-MISSING"]


def test_판정_라벨에도_근거가_필요하다():
    # payment_pressure="MEDIUM" 은 숫자가 아니지만 매입의 행동을 바꾼다
    f = check_evidence_coverage(reply(payload={"payment_pressure": "MEDIUM"}))
    assert [x.code for x in f] == ["E-EVIDENCE-MISSING"]


def test_자유_텍스트는_근거를_요구하지_않는다():
    f = check_evidence_coverage(reply(payload={"note": "여유가 좁다"}))
    assert f == []


def test_불리언은_근거를_요구하지_않는다():
    f = check_evidence_coverage(reply(payload={"is_ready": True}))
    assert f == []


def test_비어있지_않은_리스트는_근거가_필요하다():
    f = check_evidence_coverage(reply(payload={"critical_payment_dates": ["2026-09-05"]}))
    assert [x.code for x in f] == ["E-EVIDENCE-MISSING"]


def test_중첩_구조는_재귀하지_않는다():
    # verdicts[].verdict 의 근거 규칙은 도메인이 정한다
    payload = {"verdicts": [{"scenario_id": "SCN-1", "verdict": "reject"}]}
    f = check_evidence_coverage(reply(payload=payload, evidences=(ev("verdicts"),)))
    assert f == []


def test_근거가_붙으면_통과한다():
    f = check_evidence_coverage(
        reply(
            payload={"available_cash": 42000000, "payment_pressure": "MEDIUM"},
            evidences=(ev("available_cash", 42000000.0), ev("payment_pressure", 0.72, "ratio")),
        )
    )
    assert f == []


def test_대응_필드_없는_근거는_고아로_걸린다():
    f = check_evidence_coverage(reply(payload={}, evidences=(ev("ghost"),)))
    assert [x.code for x in f] == ["E-EVIDENCE-ORPHAN"]


def test_못_돈_회신에는_근거를_요구하지_않는다():
    r = reply(
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=("payroll_schedule",),
        payload={"partial": 1},
    )
    assert check_evidence_coverage(r) == []


# ---------------------------------------------------------------------------
# reasoning — LLM 이 쓰는 자리
# ---------------------------------------------------------------------------


def test_금액이_문장에_들어오면_걸린다():
    f = check_reasoning(reply(reasoning="재무 상한 38,000,000원을 초과한다."))
    assert [x.code for x in f] == ["E-REASONING-NUMERIC"]


def test_상대_표현은_통과한다():
    # "D+7" 은 실측에서 정상 문장에 자주 쓰인다
    f = check_reasoning(reply(reasoning="가용 자금은 확보되나 D+7 지급 예정이 있어 여유가 좁다."))
    assert f == []


def test_네_문장이면_걸린다():
    f = check_reasoning(reply(reasoning="하나. 둘. 셋. 넷."))
    assert [x.code for x in f] == ["E-REASONING-TOO-LONG"]


def test_빈_reasoning_은_통과한다():
    assert check_reasoning(reply(reasoning="")) == []


# ---------------------------------------------------------------------------
# ExecutionMetadata — 검증 Tool 이 실행 계획을 읽는다
# ---------------------------------------------------------------------------


def test_run_id_가_어긋나면_걸린다():
    meta = ExecutionMetadata(run_id="OTHER", request_id="REQ-1", agent="finance", used_tools=("a",))
    f = validate_reply(req(), reply(), meta)
    assert any(x.code == "E-BIND-RUN-ID" for x in f)


def test_정상_회신인데_쓴_Tool_이_없으면_걸린다():
    meta = ExecutionMetadata(run_id="FIN-RUN-1", request_id="REQ-1", agent="finance")
    f = validate_reply(req(), reply(), meta)
    assert any(x.code == "E-PLAN-EMPTY" for x in f)


def test_못_돈_회신은_Tool_이_없어도_통과():
    r = reply(
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=("payroll_schedule",),
    )
    meta = ExecutionMetadata(run_id="FIN-RUN-1", request_id="REQ-1", agent="finance")
    assert not any(x.code == "E-PLAN-EMPTY" for x in validate_reply(req(), r, meta))


# ---------------------------------------------------------------------------
# 통합 — 재무 PRE_PURCHASE 정상 경로
# ---------------------------------------------------------------------------


def test_재무_PRE_PURCHASE_정상_경로는_발견_0():
    request = req()
    r = reply(
        payload={
            "available_cash": 42000000,
            "finance_cap_amount_krw": 38000000,
            "projected_cash_min": 4200000,
            "payment_pressure": "MEDIUM",
            "critical_payment_dates": ["2026-09-05", "2026-09-12"],
        },
        evidences=(
            ev("available_cash", 42000000.0),
            ev("finance_cap_amount_krw", 38000000.0),
            ev("projected_cash_min", 4200000.0),
            ev("payment_pressure", 0.72, "ratio"),
            ev("critical_payment_dates", 2.0, "count"),
        ),
        reasoning="가용 자금은 확보되나 지급 예정이 있어 상한을 낮춰 제시한다.",
    )
    meta = ExecutionMetadata(
        run_id="FIN-RUN-1",
        request_id="REQ-1",
        agent="finance",
        used_tools=("assess_finance_position", "project_cashflow"),
        tool_order=(1, 2),
        llm_status="SUCCESS",
    )
    assert validate_reply(request, r, meta) == ()
