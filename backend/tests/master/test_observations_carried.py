"""부서가 남긴 관측이 **마스터 밖으로 나간다.**

2026-09-02 · #165 에서 드러났다. 시작은 *"429 폴백인데 `llm_fallback_used` 가
False"* 였는데, 계약을 다시 읽으니 **재무가 어긴 것이 아니었다.**

```text
LLMStatus.FALLBACK   "불렀는데 실패했다 — 규칙이 대신 답했다"
```

429 로 Gemini → ollama 는 규칙이 아니라 **다른 모델이 답한 것**이라 `False` 가 맞다.
재무는 그 사실을 `observations` 에 계약대로 싣고 있었다
(`finance/application/orchestration.py:696` · `json.dumps` 로 직렬화).

🔴 **끊긴 곳은 마스터였다.**

```text
ExecutionMetadata.observations   재무가 싣는다              ✅
ExecutionStep.observations       마스터가 받는다             ✅
critic_bridge                    Critic 까지 간다            ✅
StepOut                          칸이 없었다                🔴
  → 응답 · 화면 · master_agent_runs 계획 행                  ❌
```

`replans` · `evidences` · 조정안에 이은 **네 번째**다.

★ **마스터도 화면도 파싱하지 않는다.** 부서마다 모양이 다른 JSON 이라, 뜻을 붙이면
  부서 스키마가 한 벌 더 생기고 부서가 필드를 바꾸는 날 이쪽만 옛말을 한다.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Any

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.persistence import plan_rows, status_plan_rows
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _steps, _to_response

AS_OF = date(2025, 12, 31)

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]

#: 재무가 실제로 싣는 모양 (orchestration.py:696).
PROVIDER_OBS = json.dumps(
    {
        "observation_type": "finance_llm_provider",
        "primary_provider": "gemini",
        "effective_provider": "ollama",
        "provider_fallback_used": True,
        "provider_fallback_reason": "HTTP_429",
    },
    sort_keys=True,
)


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _port(agent: str, observations: tuple[str, ...] = (), *, purchase: bool = False):
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
            payload=(
                {"scenarios": list(SCN)}
                if purchase
                else ({"cap": 1} if request.mode == "PRE_PURCHASE" else {})
            ),
        )
        meta = ExecutionMetadata(
            run_id=run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            observations=observations,
            # 🔴 429 폴백에서도 이 값은 False 다 — 규칙이 답한 게 아니라서 계약상 맞다
            llm_fallback_used=False,
            llm_status="SUCCESS",
            llm_model="gemma3:4b",
        )
        return reply, meta

    return port


def _run(**over: Any):
    ports: dict[str, Any] = {
        "finance": _port("finance", (PROVIDER_OBS,)),
        "inventory": _port("inventory"),
        "purchase": _port("purchase", purchase=True),
    }
    ports.update(over)
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="피마늘").run()


def _finance_steps(steps):
    return [s for s in steps if s.agent == "finance"]


# ── ① 응답까지 나간다 ───────────────────────────────────────────────────────


def test_관측이_응답에_실린다():
    """🔴 여기가 끊겨 있었다."""
    response = _to_response(_ctx(), _run())

    실린_것 = [o for s in response.plan for o in s.observations]
    assert PROVIDER_OBS in 실린_것


def test_폴백_사실이_이_칸에만_있다():
    """`llm_fallback_used` 는 False 인데(계약상 맞다) 모델은 바뀌었다.

    **그 사실을 담은 칸이 관측뿐이다.**
    """
    response = _to_response(_ctx(), _run())
    step = _finance_steps(response.plan)[0]

    assert step.llm_fallback_used is False, "규칙이 답한 게 아니므로 False 가 맞다"
    assert "provider_fallback_used" in " ".join(step.observations)


def test_관측이_없는_부서는_빈_목록이다():
    """없는 것을 만들지 않는다."""
    response = _to_response(_ctx(), _run())

    inventory = [s for s in response.plan if s.agent == "inventory"]
    assert inventory and all(s.observations == [] for s in inventory)


def test_순서를_바꾸지_않는다():
    """부서가 낸 차례가 그 부서의 설명 차례다."""
    first, second = json.dumps({"a": 1}), json.dumps({"b": 2})
    response = _to_response(_ctx(), _run(finance=_port("finance", (first, second))))

    assert _finance_steps(response.plan)[0].observations == [first, second]


def test_응답_변환이_관측을_버리지_않는다():
    """🔴 결과만 검사하면 `_steps` 에서 버려도 초록불이다 (#154 · #157 · #164 에서 밟음)."""
    outcome = _run()

    실린_것 = [o for s in _steps(outcome.plan) for o in s.observations]
    assert PROVIDER_OBS in 실린_것


# ── ② 이력에도 실린다 — 사이클 대칭 ─────────────────────────────────────────


def test_매입_사이클_이력에_실린다():
    """`plan_rows` 는 `StepOut` 덤프라, 스키마에 칸이 없으면 이력에도 안 남았다."""
    rows = plan_rows(_to_response(_ctx(), _run()))

    실린_것 = [o for r in rows for o in r.get("observations", [])]
    assert PROVIDER_OBS in 실린_것


def test_조회_사이클과_대칭이다():
    """🔴 전에는 갈렸다 — 조회는 `asdict` 라 실리고 매입은 `StepOut` 이라 빠졌다.

    같은 값이 사이클에 따라 갈리면 *"이력을 보라"* 가 어느 이력이냐에 달린다.
    """
    outcome = _run()
    매입 = {k for r in plan_rows(_to_response(_ctx(), outcome)) for k in r}
    조회 = {k for r in status_plan_rows(outcome.plan) for k in r}

    assert "observations" in 매입 and "observations" in 조회


# ── ③ 마스터는 읽지 않는다 ──────────────────────────────────────────────────


def test_마스터가_관측을_파싱하지_않는다():
    """나르게 만들면서 **읽기 시작하지 않았는지** 잠근다.

    문자열 그대로 나가야 한다 — 마스터가 `json.loads` 로 펴서 필드를 꺼내면
    부서 스키마가 마스터에 한 벌 더 생긴다.
    """
    response = _to_response(_ctx(), _run())
    실린_것 = _finance_steps(response.plan)[0].observations

    assert 실린_것 == [PROVIDER_OBS], "원문이 아니다 - 펴거나 고쳤다"
    assert all(isinstance(o, str) for o in 실린_것)


def test_모양을_모르는_관측도_그대로_나른다():
    """JSON 이 아니어도 버리지 않는다 — 부서가 무엇을 적을지 마스터가 정하지 않는다."""
    raw = "그냥 문장 관측"
    response = _to_response(_ctx(), _run(finance=_port("finance", (raw,))))

    assert _finance_steps(response.plan)[0].observations == [raw]


def test_실행_계획과_응답이_같은_값을_본다():
    """둘이 갈리면 *"이력과 화면이 다른 말을 한다"* 가 된다."""
    outcome = _run()
    response = _to_response(_ctx(), outcome)

    계획 = [asdict(s)["observations"] for s in outcome.plan.steps]
    응답 = [tuple(s.observations) for s in response.plan]
    assert [tuple(x) for x in 계획] == 응답
