"""제안자(매입) 근거도 화면까지 간다.

2026-09-02 매입 실측에서 나왔다.

```text
evidences 63건   inventory 44 · finance 19 · purchase 🔴 0
```

🔴 **의도가 아니라 구조적 누락이었다.** 근거를 모으는 곳이 `_collect_constraints` 와
`_validate` 둘뿐이었고 **둘 다 `self.advisors` 를 돈다.** 매입은 조언자 목록에
없으니 들어갈 자리가 없었다.

**"왜 이 수량인가" 를 아는 쪽의 근거가 화면에 하나도 없었다.**
멘토 지시(*"매입 시나리오 근거를 보이게"* · 2026-09-01)의 주어가 매입인데
매입만 빠져 있었다.

★ **`ADVISORS` 에 매입을 넣어 고치지 않는다.** 그 목록은 *"경계를 내야 밴드가
  선다"* 는 뜻이라 매입을 넣으면 `band_is_formed` · `blocking_agents` 가 전부
  틀린다. 근거 수집만 조언자 목록에서 떼어낸다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ADVISORS, ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _evidences_out, _to_response
from app.orchestrator.contracts_core import Evidence

AS_OF = date(2025, 12, 31)

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _ev(claim: str, value: float = 1.0, source: str = "purchase") -> Evidence:
    return Evidence(
        claim=claim,
        source=source,
        ref_ids=(f"REF-{claim}",),
        value=value,
        unit="kg",
        evidence_grade="OFFICIAL",
    )


def _advisor(evidences: tuple[Evidence, ...] = ()):
    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload={"cap": 1} if request.mode == "PRE_PURCHASE" else {},
            evidences=evidences,
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _rejecting_advisor():
    """경계는 내고 시나리오 판정은 `reject` — 매입 재호출을 유발한다."""

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        pre = request.mode == "PRE_PURCHASE"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok" if pre else "reject",
            payload={"cap": 1} if pre else {},
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _purchaser(
    evidences: tuple[Evidence, ...] = (),
    *,
    scenarios: list[dict[str, Any]] | None = None,
    runtime: str = "READY",
    per_call: tuple[tuple[Evidence, ...], ...] | None = None,
):
    """`per_call` 을 주면 호출 회차마다 다른 근거를 낸다."""
    state = {"n": 0}

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        state["n"] += 1
        run_id = f"PURCHASE-{request.call_seq}"
        mine = evidences
        if per_call is not None:
            mine = per_call[min(state["n"], len(per_call)) - 1]
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="purchase",
            mode=request.mode,
            run_id=run_id,
            runtime_status=runtime,
            business_status="ok" if runtime == "READY" else "skipped",
            payload={"scenarios": list(SCN if scenarios is None else scenarios)},
            evidences=mine,
            missing_data=() if runtime != "RUNTIME_NOT_READY" else ("ml_forecast",),
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent="purchase"
        )
        return reply, meta

    return port


def _flow(**ports: Any) -> ProcurementFlow:
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="피마늘")


def _run(**over: Any):
    ports: dict[str, Any] = {
        "finance": _advisor(),
        "inventory": _advisor(),
        "purchase": _purchaser(),
    }
    ports.update(over)
    return _flow(**ports).run()


def _by_agent(evidences) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in evidences:
        out[e.agent] = out.get(e.agent, 0) + 1
    return out


# ── ① 매입 근거가 남는다 ────────────────────────────────────────────────────


def test_매입_근거가_결과에_실린다():
    """🔴 실측에서 여기가 0건이었다."""
    run = _run(purchase=_purchaser((_ev("recommended_qty_kg", 7120.0),)))

    assert _by_agent(run.evidences).get("purchase") == 1


def test_제안자와_조언자가_한_칸에_모이되_모드로_갈린다():
    """답하는 질문이 다르다 — 같은 칸에 두되 모드가 그것을 가른다."""
    run = _run(
        finance=_advisor((_ev("finance_cap_amount_krw", 3e7, "finance"),)),
        purchase=_purchaser((_ev("recommended_qty_kg", 7120.0),)),
    )

    modes = {(e.agent, e.mode) for e in run.evidences}
    assert ("purchase", "GENERATE_SCENARIOS") in modes
    assert ("finance", "PRE_PURCHASE") in modes


def test_세_파트가_다_보인다():
    """조언자만 보이던 화면이 실측의 문제였다."""
    run = _run(
        finance=_advisor((_ev("cap", 1.0, "finance"),)),
        inventory=_advisor((_ev("free_kg", 2.0, "inventory"),)),
        purchase=_purchaser((_ev("qty", 3.0),)),
    )

    assert set(_by_agent(run.evidences)) == {"finance", "inventory", "purchase"}


# ── ② 고치는 방식을 못 박는다 ───────────────────────────────────────────────


def test_ADVISORS_에_매입을_넣지_않았다():
    """🔴 넣으면 `band_is_formed` · `blocking_agents` 가 전부 틀린다.

    *"경계를 내야 밴드가 선다"* 는 목록이지 *"근거를 내는 부서"* 목록이 아니다.
    """
    assert "purchase" not in ADVISORS
    assert ADVISORS == ("finance", "inventory")


def test_매입이_경계를_막는_부서로_취급되지_않는다():
    """②의 실제 결과 — 근거를 모으게 했다고 밴드 판정이 바뀌면 안 된다."""
    run = _run(purchase=_purchaser((_ev("qty", 7120.0),)))

    assert run.end_code == "E1_APPROVED"
    assert run.blocked_by == ()


def test_매입_조정안은_모으지_않는다():
    """매입은 축 조정 권한이 없다 — 봉투가 막는다.

    여기서 모으면 *"올 수도 있다"* 로 읽힌다. 제안자와 조언자의 차이다.
    """
    run = _run(purchase=_purchaser((_ev("qty", 7120.0),)))

    assert all(a.dept != "purchase" for a in run.adjustments)


# ── ③ 버리지 않는다 ────────────────────────────────────────────────────────


def test_재호출이면_두_회차가_다_남는다():
    """매입을 두 번 불렀다는 사실이 근거에도 남아야 한다.

    1회차 근거가 2회차로 덮이면 *"한 번 불렀다"* 로 읽힌다 — 실행 계획에 같은
    단계가 두 줄로 남는 것과 같은 이유다 (조언자 재시도에서 정한 것).

    ★ 조언자가 `reject` 를 내면 `_acceptable` 이 거짓이라 매입을 다시 부른다.
    """
    run = _flow(
        finance=_advisor(),
        inventory=_rejecting_advisor(),
        purchase=_purchaser(per_call=((_ev("qty_1", 1.0),), (_ev("qty_2", 2.0),))),
    ).run()

    assert run.purchase_attempts == 2, "재호출이 안 일어나면 이 테스트가 무의미하다"
    claims = [e.evidence.claim for e in run.evidences if e.agent == "purchase"]
    assert claims == ["qty_1", "qty_2"], "회차가 덮였거나 순서가 바뀌었다"


def test_안이_안_나온_날에도_매입_근거를_싣는다():
    """*"왜 안이 없나"* 를 물을 때야말로 근거가 필요하다."""
    run = _run(purchase=_purchaser((_ev("max_price", 992.0),), scenarios=[]))

    assert run.end_code in ("E2_HELD", "E5_NO_FEASIBLE_PLAN")
    assert _by_agent(run.evidences).get("purchase") == 1


def test_매입이_미가동이어도_근거를_버리지_않는다():
    """같은 회신의 `judgment` 는 이미 싣는다 — 근거만 버리면 판정의 출처가 사라진다."""
    run = _run(purchase=_purchaser((_ev("missing_reason", 0.0),), runtime="RUNTIME_NOT_READY"))

    assert run.end_code == "E4_NOT_STARTED"
    assert _by_agent(run.evidences).get("purchase") == 1


def test_매입이_근거를_안_내면_0건이다():
    """없는 것을 만들지 않는다 — 0건은 그대로 0건이다."""
    run = _run(purchase=_purchaser(()))

    assert "purchase" not in _by_agent(run.evidences)


# ── ④ 응답 변환까지 간다 ────────────────────────────────────────────────────


def test_응답_변환이_매입_근거를_버리지_않는다():
    """🔴 결과만 검사하면 여기서 버려도 초록불이다 (#157 에서 밟은 함정)."""
    run = _run(
        finance=_advisor((_ev("cap", 1.0, "finance"),)),
        purchase=_purchaser((_ev("qty", 7120.0),)),
    )
    response = _to_response(_ctx(), run)

    assert any(e.agent == "purchase" for e in response.evidences)
    assert any(e.mode == "GENERATE_SCENARIOS" for e in response.evidences)


def test_응답_변환이_부서별로_정렬하지_않는다():
    """정렬하면 그것이 우선순위로 읽힌다.

    매입 호출이 조언자 경계보다 **뒤**라 순서가 그대로면 매입 근거가 뒤에 온다.
    """
    run = _run(
        finance=_advisor((_ev("cap", 1.0, "finance"),)),
        purchase=_purchaser((_ev("qty", 7120.0),)),
    )

    순서 = [e.agent for e in _evidences_out(run)]
    assert 순서.index("finance") < 순서.index("purchase"), "부른 차례가 아니다"
