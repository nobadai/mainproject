"""발화문 입구 — 분류하고, 확인이 필요 없을 때만 실행한다.

```text
발화문 → [LLM 분류] → 확인 필요? ─예→ 되묻고 끝 (아무것도 안 돈다)
                                └아니오→ 조회 실행
```

★ **`flow.py` 는 이 모듈을 모른다.** 발화문 해석은 Flow 바깥 일이고, Flow 는 타입이
  붙은 요청만 받는다 — 그래야 백테스트에서 Flow 를 그대로 돌릴 수 있다.

★ **실행하는 것은 조회뿐이다.** 매입 실행(`PROCUREMENT_RUN`)은 확인을 받은 뒤
  `/master/ask/execute` 로 온다. 오분류 비용이 비대칭이기 때문이다 — 조회를 잘못
  고르면 다시 물으면 그만이지만, 매입은 예산 12회와 매입 LLM 을 태운다.

⚠️ **조회는 실행이력에 적재하지 않는다 (미결).**
  `orchestrator_agent_runs.cycle` 의 CHECK 어휘가 5값(`PROCUREMENT`·`SALES`·`DAY`·
  `A`·`B`)이라 `STATUS` 를 넣으려면 마이그레이션이 필요하고, `PROCUREMENT` 로 적으면
  **조회와 의사결정이 이력에서 같아 보인다.** 지금은 안 적는 쪽을 골랐다.

  다만 **조회도 예산을 쓰고 에이전트를 부른다.** 안 남기면 그 호출이 이력에 안 보인다 —
  M-16(실행 계획 온전성)이 막으려는 것이 정확히 "안 보이는 호출"이라, `cycle` 어휘에
  `STATUS` 를 더할지는 팀에 올릴 안건이다. 매입 실행은 기존 경로를 그대로 타므로
  지금도 적재된다.
"""

from __future__ import annotations

from datetime import date

from app.master import wiring
from app.master.ask_schemas import (
    AskExecuteRequest,
    AskRequest,
    AskResponse,
    StatusAnswer,
)
from app.master.budget import CallBudget
from app.master.envelope import ExecutionContext
from app.master.llm.runtime import IntentService, get_intent_service
from app.master.llm.schemas import Intent, IntentResult
from app.master.runner import MasterRunner
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse
from app.master.service import make_request_id, run_procurement
from app.master.status_flow import StatusFlow, StatusOutcome

#: 확인 없이 바로 도는 종류. 조회뿐이다.
_AUTO_RUN = frozenset({"STATUS_QUERY"})


def ask(request: AskRequest, service: IntentService | None = None) -> AskResponse:
    """발화문을 분류하고, 확인이 필요 없으면 조회까지 돌린다.

    ★ `service` 를 주지 않으면 `.env` 설정으로 만든다. 테스트가 갈아 끼운다.
    """
    service = service or get_intent_service()
    request_id = request.request_id or make_request_id(request.as_of.isoformat())
    result = service.classify(request.utterance)
    intent = result.intent

    if intent.action == "UNKNOWN":
        return _response(
            request_id,
            request,
            result,
            outcome="NEEDS_CLARIFICATION",
            note="발화문을 분류하지 못했다. 실행하지 않았다.",
        )

    if result.needs_confirmation or intent.action not in _AUTO_RUN:
        return _response(
            request_id,
            request,
            result,
            outcome="CLASSIFIED_ONLY",
            confirm_required=True,
            note="확인 후 /master/ask/execute 로 같은 intent 를 보내면 실행한다.",
        )

    outcome = _run_status(
        request_id=request_id,
        as_of=request.as_of,
        policy_version=request.policy_version,
        budget=request.budget,
        intent=intent,
    )
    return _response(
        request_id,
        request,
        result,
        outcome="STATUS_ANSWERED",
        status=_to_answer(outcome),
    )


def execute(
    request: AskExecuteRequest,
) -> AskResponse | ProcurementRunResponse:
    """사용자가 확인한 의도를 실행한다.

    ★ **발화문을 다시 분류하지 않는다.** 본 것을 실행한다.

    ★ 매입 실행은 기존 `run_procurement` 을 그대로 탄다 — 발화문 경로라고 다른 Flow 를
      두면 두 경로가 조용히 갈라진다 (구 백로그 B1-3 이 그 문제였다).
    """
    intent = request.intent
    request_id = request.request_id or make_request_id(request.as_of.isoformat())

    if intent.action == "STATUS_QUERY":
        outcome = _run_status(
            request_id=request_id,
            as_of=request.as_of,
            policy_version=request.policy_version,
            budget=request.budget,
            intent=intent,
        )
        return AskResponse(
            request_id=request_id,
            as_of=request.as_of,
            outcome="STATUS_ANSWERED",
            intent=intent,
            status=_to_answer(outcome),
            llm_status="SKIPPED_TEMPLATE",  # 이미 분류된 의도라 LLM 을 안 부른다
        )

    if intent.action == "PROCUREMENT_RUN":
        return run_procurement(
            ProcurementRunRequest(
                as_of=request.as_of,
                policy_version=request.policy_version,
                request_id=request_id,
                item=intent.item,
                budget=request.budget,
            )
        )

    # RERUN_WITH_CONDITION · SELECT_SCENARIO 는 아직 배선하지 않았다.
    raise NotImplementedError(
        f"{intent.action} 실행 경로는 아직 없다. "
        "SELECT_SCENARIO 는 /master/runs/{request_id}/decision 을, "
        "RERUN_WITH_CONDITION 은 조건을 반영한 /master/request 를 쓴다."
    )


# ── 내부 ────────────────────────────────────────────────────────────────


def _run_status(
    *,
    request_id: str,
    as_of: date,
    policy_version: str,
    budget: int,
    intent: Intent,
) -> StatusOutcome:
    """조회 Flow 를 돌린다. **어댑터 미등록도 결과로 접는다.**"""
    context = ExecutionContext(
        request_id=request_id,
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version=policy_version,
    )
    asked = tuple(intent.agents)
    missing = set(wiring.missing())
    registered = tuple(a for a in asked if a not in missing)

    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=budget))
    outcome = StatusFlow(runner, registered).run()

    unregistered = tuple(a for a in asked if a in missing)
    if not unregistered:
        return outcome

    # 미등록은 오류가 아니라 "그 부서가 오늘 돌지 않는다"와 같다 (§5.3).
    merged_missing = dict(outcome.missing_data)
    for agent in unregistered:
        merged_missing[agent] = ("ADAPTER_NOT_REGISTERED",)
    answered = len(outcome.answers)
    return StatusOutcome(
        status_code="S3_UNAVAILABLE" if answered == 0 else "S2_PARTIAL",
        reason=(
            f"{', '.join(unregistered)} 어댑터가 등록되지 않았다."
            if answered == 0
            else f"{outcome.reason} {', '.join(unregistered)} 는 어댑터 미등록이다."
        ),
        plan=outcome.plan,
        answers=outcome.answers,
        unavailable=outcome.unavailable + unregistered,
        missing_data=merged_missing,
        errors=outcome.errors,
    )


def _to_answer(outcome: StatusOutcome) -> StatusAnswer:
    return StatusAnswer(
        status_code=outcome.status_code,
        reason=outcome.reason,
        answers={k: dict(v) for k, v in outcome.answers.items()},
        unavailable=list(outcome.unavailable),
        missing_data={k: list(v) for k, v in outcome.missing_data.items()},
        errors=dict(outcome.errors),
    )


def _response(
    request_id: str,
    request: AskRequest,
    result: IntentResult,
    *,
    outcome,
    confirm_required: bool = False,
    status: StatusAnswer | None = None,
    note: str | None = None,
) -> AskResponse:
    return AskResponse(
        request_id=request_id,
        as_of=request.as_of,
        outcome=outcome,
        intent=result.intent,
        clarification=result.clarification,
        confirm_required=confirm_required,
        status=status,
        llm_status=result.llm_status,
        llm_provider=result.llm_provider,
        llm_model=result.llm_model,
        llm_attempts=result.llm_attempts,
        llm_fallback_used=result.llm_fallback_used,
        note=note,
    )
