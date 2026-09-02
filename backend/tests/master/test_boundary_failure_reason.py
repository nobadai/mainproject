"""경계 단계에서 부서가 죽으면 **왜 죽었는지가 응답까지 간다.**

2026-09-02 재현성 측정에서 드러난 구멍. 같은 입력으로 6회 돌려 2회가
`E4_NOT_STARTED` 로 떨어졌는데 **왜 떨어졌는지가 응답 어디에도 없었다.**

```text
남던 것    reason: "경계를 내지 못한 에이전트: finance"   ← 누구인지는 안다
          blocked_by: ["finance"]
안 남던 것  왜 실패했는지
          실행 계획을 파도 runtime_status=ERROR 까지만 나온다
```

🔴 **같은 파일 안에서 대칭이 깨져 있었다.** 매입이 죽으면
`"매입 에이전트 미가동: {reasoning}"` 으로 사유가 실리는데, 재무·물류가 죽으면
이름만 실렸다. 새 규칙을 만드는 것이 아니라 **매입 쪽에 맞추는 것**이다.

★ 8/31 에 고친 ADVISOR-NO-VERDICT 와 같은 종류인데 **거긴 판정 단계만 고쳤다.**
  경계에서 죽으면 `E4` 로 바로 끝나 `_check_advisor_answered` 가 발화할 기회가 없다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master.answer import facts_from_procurement
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import AgentFailure, ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _to_response
from tests.master.test_flow import advisor, purchaser

AS_OF = date(2025, 12, 31)

#: 어댑터가 실제로 터졌을 때 나오는 문장. 재현성 측정에서 재무가 이렇게 죽었다.
BOOM = "커넥션이 끊겼습니다"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _explodes(message: str = BOOM):
    """호출하면 터지는 부서.

    ★ **`ERROR` 회신을 손으로 만들지 않는다.** 예외 → `error_reply` 경로를 그대로
      밟아야 실제로 죽었을 때와 같은 값이 흐른다.
    """

    def port(request: AgentRequest):
        raise RuntimeError(message)

    return port


def _not_ready(*missing: str):
    """입력이 없어 답을 못 낸 부서. `reasoning` 은 비어 있고 `missing_data` 만 있다."""

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            missing_data=tuple(missing),
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _speaks(text: str):
    """`reasoning` 을 직접 쓰는 부서 — 봉투 검증 대조용."""

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="ERROR",
            business_status="skipped",
            reasoning=text,
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _flow(**ports: Any) -> ProcurementFlow:
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="피마늘")


def _blocked_run(**over: Any):
    ports: dict[str, Any] = {
        "finance": _explodes(),
        "inventory": advisor(),
        "purchase": purchaser(),
    }
    ports.update(over)
    return _flow(**ports).run()


# ── ① 사유가 결과에 남는다 ──────────────────────────────────────────────────


def test_경계에서_터진_사유가_결론_문장에_남는다():
    """🔴 이것이 없어서 사람이 할 수 있는 것이 "다시 돌려 본다" 뿐이었다."""
    run = _blocked_run()

    assert run.end_code == "E4_NOT_STARTED"
    assert "finance" in run.reason, "누구인지는 전부터 남았다"
    assert BOOM in run.reason, "왜인지가 안 남는다 — 고치려던 것이 이것이다"


def test_이름만_담긴_자리도_그대로_둔다():
    """`blocked_by` 를 읽던 코드가 있다 — 사유는 **덧붙이는 것**이지 대체가 아니다."""
    run = _blocked_run()

    assert run.blocked_by == ("finance",)
    assert [f.agent for f in run.blocked_failures] == ["finance"]


def test_두_부서가_동시에_막으면_둘_다_남는다():
    """한 부서만 적고 끝내면 나머지 하나가 조용히 사라진다."""
    run = _blocked_run(finance=_explodes("재무가 터졌다"), inventory=_explodes("물류가 터졌다"))

    assert [f.agent for f in run.blocked_failures] == ["finance", "inventory"]
    assert "재무가 터졌다" in run.reason
    assert "물류가 터졌다" in run.reason


def test_마스터가_부서_문장을_고쳐_쓰지_않는다():
    """§3.2.2 — 요약하거나 다시 쓰면 그 순간 마스터의 해석이 된다."""
    run = _blocked_run(finance=_speaks("담보 한도 산정에 실패했습니다"))

    failure = run.blocked_failures[0]
    assert failure.reasoning == "담보 한도 산정에 실패했습니다"


# ── ② 무엇이 없어서 못 냈는지 ───────────────────────────────────────────────


def test_입력이_없어_못_낸_경우_없는_입력이_남는다():
    """`RUNTIME_NOT_READY` 는 `reasoning` 이 비어 있다 — 그때 답은 `missing_data` 다."""
    run = _blocked_run(finance=_not_ready("credit_line", "as_of_balance"))

    failure = run.blocked_failures[0]
    assert failure.missing_data == ("credit_line", "as_of_balance")
    assert "credit_line" in run.reason, "실행 계획을 파야만 보이면 화면에서는 없는 것이다"


def test_사유도_없는_입력도_없으면_상태라도_남는다():
    """빈 문자열을 그대로 내보내면 화면이 "finance()" 를 보여준다."""
    failure = AgentFailure("finance", "ERROR")

    assert failure.detail == "ERROR"


def test_안_불린_것과_못_낸_것을_구분한다():
    """회신이 없으면 `RuntimeStatus` 로 적을 수 없다 — 상태를 지어내지 않는다."""
    failure = AgentFailure("finance", "NOT_CALLED")

    assert failure.detail == "NOT_CALLED"


# ── ③ 이력에도 남는다 ───────────────────────────────────────────────────────


def test_실행_계획에_사유가_남는다():
    """🔴 `replans` 와 같은 누락이었다 — 부서가 보내 준 것을 마스터가 버렸다."""
    run = _blocked_run()

    step = run.plan.last("finance", "PRE_PURCHASE")
    assert step is not None
    assert BOOM in step.reasoning, "이력만으로 조사할 수 없으면 실측이 추측이 된다"


def test_사유는_실행_계획에서_꺼낸다():
    """회신은 `contributes_to_band` 로 걸러져 사라진다 — 계획이 단일 출처다.

    `ERROR` 는 한 번 다시 부르므로 같은 단계가 두 줄로 남는다.
    """
    run = _blocked_run(finance=_explodes("두 번 다 같은 이유"))

    steps = [s for s in run.plan.steps if s.agent == "finance"]
    assert len(steps) == 2, "ERROR 는 한 번 다시 부른다"
    assert all("두 번 다 같은 이유" in s.reasoning for s in steps)


# ── ④ 매입과 대칭이다 ───────────────────────────────────────────────────────


def test_매입_미가동도_같은_자리에_담긴다():
    """전에는 매입만 사유를 실었다. 이제 **자리도 같다.**"""
    run = _flow(
        finance=advisor(),
        inventory=advisor(),
        purchase=purchaser(runtime="RUNTIME_NOT_READY"),
    ).run()

    assert run.end_code == "E4_NOT_STARTED"
    assert [f.agent for f in run.blocked_failures] == ["purchase"]
    assert "ml_forecast" in run.reason, "매입도 무엇이 없어서인지 남아야 한다"


def test_조언자와_매입의_사유_모양이_같다():
    """한 곳에서 만든다 — 각자 조립하면 화면과 문장이 갈린다."""
    advisor_failure = AgentFailure("finance", "ERROR", reasoning="터졌다", missing_data=("x",))
    purchase_failure = AgentFailure("purchase", "ERROR", reasoning="터졌다", missing_data=("x",))

    assert advisor_failure.detail == purchase_failure.detail


# ── ⑤ 봉투 검증을 우회하지 않는다 ───────────────────────────────────────────


def test_사유를_날라도_봉투_규칙은_그대로_걸린다():
    """🔴 이슈에서 "확인할 것" 으로 남겨 둔 항목.

    `check_reasoning` 은 `runner.call` 이 `plan.record` **전에** 돌린다. 값을
    나르기만 하고 판정에 쓰지 않으므로 규칙을 피해 가지 않는다.
    """
    run = _blocked_run(finance=_speaks("한도 12,000,000 원이 모자랍니다"))

    step = run.plan.last("finance", "PRE_PURCHASE")
    assert step is not None
    assert "E-REASONING-NUMERIC" in step.finding_codes, "봉투 검증이 조용해졌다"
    assert "12,000,000" in step.reasoning, "걸렸다고 값을 버리지도 않는다"


# ── ⑥ 화면까지 간다 ─────────────────────────────────────────────────────────


def test_응답_변환에서_사유가_안_사라진다():
    """`flow` 에만 있고 `_to_response` 가 안 옮기면 화면은 여전히 못 본다."""
    run = _blocked_run()
    response = _to_response(_ctx(), run)

    assert [f.agent for f in response.blocked_failures] == ["finance"]
    assert BOOM in response.blocked_failures[0].detail


def test_화면이_쓸_한_줄을_서버가_만든다():
    """`reason` 문장과 **같은 값**이어야 둘이 갈리지 않는다."""
    run = _blocked_run()
    response = _to_response(_ctx(), run)

    assert response.blocked_failures[0].detail in response.reason


def test_발화문에_막은_사유가_실린다():
    """전에는 "막은 부서: 재무" 한 줄이 전부였다."""
    run = _blocked_run()
    facts = facts_from_procurement(_to_response(_ctx(), run))

    blocked_lines = [g for g in facts.gaps if g.startswith("막은 부서")]
    assert blocked_lines, "막은 부서를 아예 안 적었다"
    assert any(BOOM in line for line in blocked_lines)
