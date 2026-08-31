"""마스터 호출 계층 — 예산 · 실패 처리 · 실행 계획 기록.

이슈 설계 원칙 ③ 대로 **이 층은 전부 결정론**이다. 같은 입력에 같은 계획이 나오는지도
여기서 고정한다.
"""

from __future__ import annotations

from datetime import date

from dataclasses import replace

import pytest

from app.master import (
    AgentNotRegistered,
    AgentRegistry,
    AgentReply,
    AgentRequest,
    BudgetExhausted,
    CallBudget,
    ExecutionContext,
    ExecutionMetadata,
    MasterError,
    MasterRunner,
)

AS_OF = date(2026, 8, 26)


def ctx(request_id: str = "REQ-1") -> ExecutionContext:
    return ExecutionContext(
        request_id=request_id,
        as_of=AS_OF,
        trigger="ML_COMPLETE",
        policy_version="v1.3-PROVISIONAL",
    )


def ok_port(tools: tuple[str, ...] = ("assess_finance_position",)):
    """정상 회신을 내는 포트."""

    def port(request: AgentRequest):
        run_id = f"{request.agent.upper()}-RUN-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
        )
        meta = ExecutionMetadata(
            run_id=run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=tools,
            tool_order=tuple(range(1, len(tools) + 1)),
        )
        return reply, meta

    return port


def not_ready_port(missing: tuple[str, ...] = ("payroll_schedule",)):
    def port(request: AgentRequest):
        run_id = f"{request.agent.upper()}-NR-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            missing_data=missing,
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    return port


def raising_port(exc: Exception):
    def port(request: AgentRequest):
        raise exc

    return port


def registry(**ports) -> AgentRegistry:
    reg = AgentRegistry()
    for name, port in ports.items():
        reg.register(name, port)
    return reg


def runner(budget: int = 8, **ports) -> MasterRunner:
    return MasterRunner(ctx(), registry(**ports), CallBudget(limit=budget))


# ---------------------------------------------------------------------------
# 레지스트리
# ---------------------------------------------------------------------------


def test_미등록_에이전트는_설정_오류로_올라간다():
    # 도메인 실패가 아니라 마스터 배선 실수다 — 값으로 삼키면 안 된다
    r = runner()
    with pytest.raises(AgentNotRegistered, match="finance"):
        r.call("finance", "PRE_PURCHASE")


def test_등록_목록이_정렬되어_보인다():
    reg = registry(finance=ok_port(), inventory=ok_port())
    assert reg.registered == ("finance", "inventory")


# ---------------------------------------------------------------------------
# 실패를 값으로 — 사이클을 죽이지 않는다
# ---------------------------------------------------------------------------


def test_예외는_ERROR_회신으로_바뀐다():
    r = runner(finance=raising_port(RuntimeError("DB 연결 실패")))
    reply = r.call("finance", "PRE_PURCHASE")
    assert reply.runtime_status == "ERROR"
    assert "DB 연결 실패" in reply.reasoning
    assert not reply.contributes_to_band


def test_타임아웃도_ERROR_다():
    # 이슈 초안의 RUNTIME_NOT_READY 에서 바꿨다 — 재시도 여지를 남긴다
    r = runner(finance=raising_port(TimeoutError("60s 초과")))
    reply = r.call("finance", "PRE_PURCHASE")
    assert reply.runtime_status == "ERROR"
    assert reply.worth_retry


def test_KeyboardInterrupt_는_삼키지_않는다():
    r = runner(finance=raising_port(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        r.call("finance", "PRE_PURCHASE")


def test_터진_호출도_계획에_남는다():
    # "불렀는데 기록이 없다" 와 "아예 안 불렀다" 는 달라야 한다
    r = runner(finance=raising_port(RuntimeError("x")))
    r.call("finance", "PRE_PURCHASE")
    assert len(r.steps()) == 1
    assert r.plan.called("finance", "PRE_PURCHASE")


# ---------------------------------------------------------------------------
# 예산 — 순서가 자유로워지면 상한이 사라진다
# ---------------------------------------------------------------------------


def test_예산을_넘으면_끊는다():
    r = runner(budget=2, finance=ok_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("finance", "STATUS_QUERY")
    with pytest.raises(BudgetExhausted, match="2"):
        r.call("finance", "STATUS_QUERY")


def test_예산_소진_시_호출은_일어나지_않는다():
    calls = []

    def counting(request):
        calls.append(request.agent)
        return ok_port()(request)

    r = runner(budget=1, finance=counting)
    r.call("finance", "PRE_PURCHASE")
    with pytest.raises(BudgetExhausted):
        r.call("finance", "STATUS_QUERY")
    assert len(calls) == 1


def test_예산은_1_이상():
    with pytest.raises(MasterError, match="1 이상"):
        CallBudget(limit=0)


def test_예산_이력이_남는다():
    r = runner(finance=ok_port(), inventory=ok_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("inventory", "PRE_PURCHASE")
    assert r.budget.history == ("finance:PRE_PURCHASE", "inventory:PRE_PURCHASE")
    assert r.budget.remaining == 6


# ---------------------------------------------------------------------------
# 실행 계획 — §1.2-11
# ---------------------------------------------------------------------------


def test_call_seq_는_자동으로_증가한다():
    r = runner(finance=ok_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("finance", "PRE_PURCHASE")
    assert [s.call_seq for s in r.steps()] == [1, 2]


def test_mode_가_다르면_call_seq_는_따로_센다():
    r = runner(finance=ok_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("finance", "STATUS_QUERY")
    assert [s.call_seq for s in r.steps()] == [1, 1]


def test_사용한_Tool_이_계획에_남는다():
    r = runner(finance=ok_port(("assess_finance_position", "project_cashflow")))
    r.call("finance", "PRE_PURCHASE")
    assert r.steps()[0].used_tools == ("assess_finance_position", "project_cashflow")


def test_봉투_위반은_finding_코드로_남는다():
    def bad_binding(request):
        reply, meta = ok_port()(request)
        # 다른 요청의 회신을 흉내
        return (
            AgentReply(
                request_id="REQ-OTHER",
                as_of=request.context.as_of,
                agent=request.agent,
                mode=request.mode,
                run_id=reply.run_id,
                runtime_status="READY",
                business_status="ok",
            ),
            meta,
        )

    r = runner(finance=bad_binding)
    r.call("finance", "PRE_PURCHASE")
    assert "E-BIND-REQUEST" in r.steps()[0].finding_codes


def test_not_ready_목록이_드러난다():
    # 조용히 건너뛰면 상한이 무한대로 남아 무제한 매입이 통과한다
    r = runner(finance=ok_port(), inventory=not_ready_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("inventory", "PRE_PURCHASE")
    assert r.plan.not_ready == ("inventory",)


def test_계획에_실행_시각이_없다():
    """같은 입력에 같은 값이어야 한다 — 시각이 들어가면 재현성 비교가 불가능하다.

    필드를 늘릴 때 무심코 타임스탬프를 넣지 않도록 **집합을 통째로 고정**한다.

    ★ **결과값은 들어와도 된다.** `runtime_status`·`llm_status` 는 실행마다 달라질 수
      있지만, 재현성 비교는 `plan.signature`(agent · mode · call_seq)만 본다 —
      결과는 *"에이전트 쪽 사정이지 마스터의 계획이 아니다"* (`plan.signature` 주석).
      막아야 하는 것은 **시계**다.
    """
    r = runner(finance=ok_port())
    r.call("finance", "PRE_PURCHASE")
    fields = set(vars(r.steps()[0]))
    assert fields == {
        "seq",
        "agent",
        "mode",
        "call_seq",
        "run_id",
        "runtime_status",
        "business_status",
        "used_tools",
        "finding_codes",
        "missing_data",
        # 그 부서 안에서 LLM 이 돌았나 — 규칙으로 답한 것과 모델로 답한 것을 가른다
        "llm_status",
        "llm_model",
        "llm_attempts",
        "llm_fallback_used",
    }
    # 목록을 고쳐 가며 늘리다 시계를 흘려 넣지 않도록, 이름으로도 한 번 막는다.
    # 🔴 부분 문자열로 재지 않는다 — "time" 은 `runtime_status` 를, "ts" 는
    #    `llm_attempts` 를 오탐한다. **시계를 뜻하는 이름만** 정확히 본다.
    clockish = {"timestamp", "created", "started", "finished", "elapsed", "duration", "clock"}
    assert not [
        f for f in fields if f.endswith("_at") or any(w in f.split("_") for w in clockish)
    ]


def test_부서가_LLM_을_썼는지_계획에_남는다():
    """🔴 `ExecutionMetadata` 는 처음부터 이 넷을 담고 있었는데 계획으로 옮기지 않아,
    마스터는 **부서가 규칙으로 답했는지 모델로 답했는지 몰랐다.**

    재무가 Tool 선택을 Planner 에게 맡기는 구조로 가면(재무 2026-08-31 질의) 이 값이
    없으면 안 된다 — Planner 가 죽어 규칙으로 떨어져도 산출물은 멀쩡해 보인다.
    """

    def llm_port(request):
        reply, meta = ok_port()(request)
        return reply, replace(
            meta,
            llm_status="FALLBACK",
            llm_model="gemma3:4b",
            llm_attempts=2,
            llm_fallback_used=True,
        )

    r = runner(finance=llm_port)
    r.call("finance", "PRE_PURCHASE")
    step = r.steps()[0]

    assert step.llm_status == "FALLBACK"
    assert step.llm_model == "gemma3:4b"
    assert step.llm_attempts == 2
    assert step.llm_fallback_used is True


def test_LLM_상태는_재현성_지문에_안_들어간다():
    """같은 계획이 한 번은 SUCCESS 한 번은 FALLBACK 일 수 있다 — 그것으로 계획이
    달라졌다고 읽으면 안 된다."""

    def fallback_port(request):
        reply, meta = ok_port()(request)
        return reply, replace(meta, llm_status="FALLBACK", llm_fallback_used=True)

    a = runner(finance=ok_port())
    a.call("finance", "PRE_PURCHASE")
    b = runner(finance=fallback_port)
    b.call("finance", "PRE_PURCHASE")

    assert a.plan.signature == b.plan.signature


# ---------------------------------------------------------------------------
# 재현성 — 백테스트 성립 조건
# ---------------------------------------------------------------------------


def test_같은_입력에_같은_실행_계획():
    def run_once():
        r = runner(finance=ok_port(), inventory=ok_port(), purchase=ok_port())
        r.call("finance", "PRE_PURCHASE")
        r.call("inventory", "PRE_PURCHASE")
        r.call("purchase", "GENERATE_SCENARIOS")
        r.call("finance", "SCENARIO_VALIDATION")
        return r.plan.signature

    assert run_once() == run_once()


def test_지문은_누구를_어떤_목적으로_몇_번째로_불렀는가():
    r = runner(finance=ok_port(), inventory=ok_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("inventory", "PRE_PURCHASE")
    r.call("finance", "SCENARIO_VALIDATION")
    assert r.plan.signature == (
        ("finance", "PRE_PURCHASE", 1),
        ("inventory", "PRE_PURCHASE", 1),
        ("finance", "SCENARIO_VALIDATION", 1),
    )


# ---------------------------------------------------------------------------
# 부분 실패 정책 — 제약 하나가 빠진 시나리오를 만들지 않는다
# ---------------------------------------------------------------------------

REQUIRED = ("finance", "inventory")


def test_둘_다_경계를_내면_밴드가_선다():
    r = runner(finance=ok_port(), inventory=ok_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("inventory", "PRE_PURCHASE")
    assert r.band_is_formed(REQUIRED)
    assert r.blocking_agents(REQUIRED) == ()


def test_하나가_미가동이면_밴드가_안_선다():
    r = runner(finance=ok_port(), inventory=not_ready_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("inventory", "PRE_PURCHASE")
    assert not r.band_is_formed(REQUIRED)
    assert r.blocking_agents(REQUIRED) == ("inventory",)


def test_아예_안_부른_에이전트도_막는다():
    r = runner(finance=ok_port(), inventory=ok_port())
    r.call("finance", "PRE_PURCHASE")
    assert not r.band_is_formed(REQUIRED)
    assert r.blocking_agents(REQUIRED) == ("inventory",)


def test_ERROR_만_재시도_대상():
    r = runner(finance=raising_port(RuntimeError("x")), inventory=not_ready_port())
    r.call("finance", "PRE_PURCHASE")
    r.call("inventory", "PRE_PURCHASE")
    assert r.retryable("finance", "PRE_PURCHASE")
    assert not r.retryable("inventory", "PRE_PURCHASE")


def test_안_부른_에이전트는_재시도_대상이_아니다():
    r = runner(finance=ok_port())
    assert not r.retryable("finance", "PRE_PURCHASE")
