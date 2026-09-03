"""부서가 낸 근거가 **화면까지 간다** — 그리고 가는 동안 안 변한다.

2026-09-02. 멘토 지적 *"매입 시나리오 관련에서 근거의 내용이 보일 수 있게"* 의 배선을
잠근다. 전에는 `flow` 가 근거를 모아 **검증에만** 넘기고 응답에서 끊겼다.

```text
잠그는 것 넷
  ① 근거가 응답에 실린다            안 실으면 화면이 출처를 못 보여준다
  ② 마스터가 고르거나 순서를 안 바꾼다  고르는 것이 곧 판단이다 (§3.2.2)
  ③ 검증이 본 것과 화면이 보는 것이 같다  갈리면 "검증은 통과인데 근거는 다른 값"
  ④ 계약과 다른 값을 감추지 않는다     고치지도 버리지도 않고 concerns 로 드러낸다
```
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contracts.core import Evidence
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _evidence_contract_concerns, _evidences_out

AS_OF = date(2025, 12, 31)


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _evidence(claim: str, value: Any = 100.0, unit: str = "krw") -> Evidence:
    return Evidence(
        claim=claim,
        source="finance",
        ref_ids=(f"REF-{claim}",),
        value=value,
        unit=unit,
        evidence_grade="OFFICIAL",
    )


def _port(evidences: tuple[Evidence, ...], *, payload: dict[str, Any] | None = None):
    """근거를 실어 주는 부서."""

    def port(request: AgentRequest):
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload or {},
            evidences=evidences,
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _flow(**ports) -> ProcurementFlow:
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="피마늘")


# ── ① 응답까지 간다 ─────────────────────────────────────────────────────────


def test_경계_근거가_결과에_실린다():
    """🔴 전에는 여기서 끊겼다 — `flow` 가 모아 검증에만 넘기고 응답에 자리가 없었다."""
    flow = _flow(
        finance=_port((_evidence("finance_cap_amount_krw", 31854627.0),)),
        inventory=_port((_evidence("warehouse_free_kg", 7637.0, "kg"),)),
        purchase=_port(()),
    )
    outcome = flow.run()

    claims = {(e.agent, e.evidence.claim) for e in outcome.evidences}
    assert ("finance", "finance_cap_amount_krw") in claims
    assert ("inventory", "warehouse_free_kg") in claims


def test_판정_근거도_버리지_않는다():
    """🔴 `_validate` 가 payload 만 꺼내고 evidences 를 통째로 흘리고 있었다.

    부서가 보내 준 것을 마스터가 버리는 모양이고, `replans` 에서 고친 것과 같은 종류다.
    """
    flow = _flow(
        finance=_port((_evidence("cap", 1.0),)),
        inventory=_port((_evidence("free", 2.0),)),
        purchase=_port((), payload={"scenarios": [{"label": "기본"}]}),
    )
    outcome = flow.run()

    modes = {e.mode for e in outcome.evidences}
    assert "SCENARIO_VALIDATION" in modes, "판정 단계 근거가 안 실렸다"


def test_안이_없는_날도_근거가_남는다():
    """★ **안이 안 나온 날이야말로 근거가 필요하다.**

    성공한 날만 근거를 보여주면 정작 설명이 필요한 날에 화면이 침묵한다.
    """
    flow = _flow(
        finance=_port((_evidence("cap", 1.0),)),
        inventory=_port((_evidence("free", 2.0),)),
        purchase=_port((), payload={"scenarios": []}),  # 안 0개
    )
    outcome = flow.run()

    assert outcome.end_code != "E1_APPROVED"
    assert outcome.evidences, "안이 없는 날에 근거가 통째로 사라졌다"


# ── ② 고르지도 바꾸지도 않는다 ──────────────────────────────────────────────


def test_순서를_바꾸지_않는다():
    """★ 부서가 낸 순서가 그 부서의 설명 순서다.

    마스터가 정렬하면 "이게 더 중요하다" 는 뜻이 생긴다 (§3.2.2).
    """
    order = ("zzz_last", "aaa_first", "mmm_middle")
    flow = _flow(
        finance=_port(tuple(_evidence(c) for c in order)),
        inventory=_port(()),
        purchase=_port(()),
    )
    outcome = flow.run()

    # ★ **응답 변환까지 보고 확인한다.** outcome 만 보면 `_evidences_out` 이
    #   정렬해도 안 걸린다 - 변이 테스트에서 실제로 안 걸렸다 (2026-09-02).
    #   순서를 바꾸는 자리는 마지막 변환이다.
    got = [e.claim for e in _evidences_out(outcome) if e.agent == "finance"]
    assert got == list(order), f"순서가 바뀌었다: {got}"

    raw = [e.evidence.claim for e in outcome.evidences if e.agent == "finance"]
    assert raw == list(order), f"수집 단계에서 이미 바뀌었다: {raw}"


def test_값을_손대지_않는다():
    """★ 원본 그대로. 반올림·단위 변환·정규화 전부 안 한다."""
    flow = _flow(
        finance=_port((_evidence("odd", 31854627.456, "krw"),)),
        inventory=_port(()),
        purchase=_port(()),
    )
    out = _evidences_out(_flow_outcome(flow))

    assert [e.value for e in out if e.claim == "odd"] == [31854627.456]
    assert [e.ref_ids for e in out if e.claim == "odd"] == [["REF-odd"]]


def _flow_outcome(flow: ProcurementFlow):
    return flow.run()


# ── ③ 검증이 본 것과 같은 값 ────────────────────────────────────────────────


def test_검증이_본_근거와_화면이_보는_근거가_같다():
    """🔴 **두 곳에서 각자 모으면 갈린다.**

    갈리면 "검증은 통과했는데 화면 근거는 다른 값" 이 되고, 그때 사람은 어느 쪽을
    믿어야 하는지 알 수 없다. `constraint_evidences`(검증행)와 `sourced_evidences`
    (화면행)가 **같은 객체**를 담는지 본다.
    """
    ev = _evidence("finance_cap_amount_krw", 31854627.0)
    flow = _flow(finance=_port((ev,)), inventory=_port(()), purchase=_port(()))
    flow.run()

    from_verifier = flow.constraint_evidences["finance"]
    to_screen = [
        e.evidence
        for e in flow.sourced_evidences
        if e.mode == "PRE_PURCHASE" and e.agent == "finance"
    ]

    assert list(from_verifier) == to_screen
    assert to_screen[0] is ev, "같은 객체가 아니다 - 어딘가에서 복사·변환됐다"


# ── ④ 계약과 다른 값을 감추지 않는다 ────────────────────────────────────────


def test_숫자가_아닌_값은_그대로_나가고_사실이_드러난다():
    """🔴 **실측에서 나왔다** (2026-09-02).

    `Evidence.value` 는 계약상 `float` 인데 재무 `policy_version_used` 가
    `"v1.3-PROVISIONAL"` 을 싣는다. `Evidence` 가 dataclass 라 런타임 검증이 없어
    지금까지 아무도 몰랐고, 근거를 화면으로 내보내려다 처음 드러났다.

    ★ 고치지도 버리지도 않는다. 값은 그대로 나가고 `concerns` 가 사실을 적는다.
    """
    flow = _flow(
        finance=_port((_evidence("policy_version_used", "v1.3-PROVISIONAL", "version"),)),
        inventory=_port(()),
        purchase=_port(()),
    )
    outcome = flow.run()

    out = _evidences_out(outcome)
    assert [e.value for e in out if e.claim == "policy_version_used"] == ["v1.3-PROVISIONAL"]

    concerns = _evidence_contract_concerns(outcome)
    assert concerns, "계약과 다른 값이 조용히 통과했다"
    assert "EVIDENCE-VALUE-NOT-NUMERIC" in concerns[0]
    assert "policy_version_used" in concerns[0]


def test_숫자만_있으면_경고하지_않는다():
    """★ 정상인 날에 경고가 뜨면 아무도 경고를 안 읽게 된다."""
    flow = _flow(
        finance=_port((_evidence("cap", 100.0),)),
        inventory=_port((_evidence("free", 200.0, "kg"),)),
        purchase=_port(()),
    )
    assert _evidence_contract_concerns(flow.run()) == []


def test_불리언은_숫자로_치지_않는다():
    """⚠️ 파이썬에서 `bool` 은 `int` 의 하위형이라 그냥 세면 통과한다.

    `True` 가 근거의 값으로 들어오면 화면에 `1` 로 보인다 — 숫자가 아니라 판정이다.
    """
    flow = _flow(
        finance=_port((_evidence("is_ok", True, "flag"),)),
        inventory=_port(()),
        purchase=_port(()),
    )
    assert _evidence_contract_concerns(flow.run()), "bool 이 숫자로 통과했다"
