"""마스터 API 서비스 — 요청을 Flow 로 옮기고 결과를 응답으로 옮긴다.

★ 여기에 판단을 두지 않는다. 판단은 `flow.py` 에 있다.
  서비스는 **경계 변환**만 한다 — 그래야 API 모양이 바뀌어도 Flow 가 안 흔들린다.
"""

from __future__ import annotations

import time

from app.master import persistence, wiring
from app.master.budget import CallBudget
from app.master.envelope import ExecutionContext
from app.master.flow import ProcurementFlow, ProcurementOutcome, VerifierPort
from app.master.plan import ExecutionPlan
from app.master.runner import MasterRunner
from app.master.schemas import (
    ProcurementRunRequest,
    ProcurementRunResponse,
    RunHistoryOut,
    StepOut,
)
from app.orchestrator.run_repository import get_run_by_request_id


def make_request_id(as_of: str, seq: int = 1) -> str:
    """`REQ-20260826-0001`.

    ★ 시각이 아니라 **날짜 + 순번**이다. 같은 날 재실행을 구분하되 재현 가능해야 한다
      (§1.2-11). 순번 관리는 호출자 몫이며, 명시적으로 주는 편이 낫다.
    """
    return f"REQ-{as_of.replace('-', '')}-{seq:04d}"


def run_procurement(
    request: ProcurementRunRequest,
    verifier: VerifierPort | None = None,
) -> ProcurementRunResponse:
    """매입 Flow 를 한 번 돌리고 실행 계획을 적재한다.

    ★ 적재는 계산이 끝난 뒤다. 실패해도 응답을 막지 않는다 (§persistence).
    """
    started = time.perf_counter()
    request_id = request.request_id or make_request_id(request.as_of.isoformat())
    context = ExecutionContext(
        request_id=request_id,
        as_of=request.as_of,
        trigger=request.trigger,
        policy_version=request.policy_version,
    )

    missing = wiring.missing()
    if missing:
        # 어댑터 미구현은 오류가 아니라 "그 부서가 오늘 돌지 않는다"와 같다 (§5.3)
        response = _empty_response(
            context,
            reason=f"어댑터 미등록: {', '.join(missing)}",
            missing_adapters=list(missing),
        )
        # 어댑터가 없어 못 돈 날도 이력에 남긴다 — 안 부른 것과 못 부른 것은 다르다
        persistence.record(request, response, elapsed_ms=_elapsed(started))
        return response

    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=request.budget))
    outcome = ProcurementFlow(
        runner,
        verifier=verifier,
        forecast=request.forecast,
        confirmed_orders=request.confirmed_orders,
        policy_values=request.policy_values,
    ).run(has_unmet_obligation=request.has_unmet_obligation)

    response = _to_response(context, outcome)
    persistence.record(request, response, elapsed_ms=_elapsed(started))
    return response


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# ---------------------------------------------------------------------------
# 변환
# ---------------------------------------------------------------------------


def _to_response(context: ExecutionContext, outcome: ProcurementOutcome) -> ProcurementRunResponse:
    return ProcurementRunResponse(
        request_id=context.request_id,
        as_of=context.as_of,
        end_code=outcome.end_code,
        reason=outcome.reason,
        scenarios=[dict(s) for s in outcome.scenarios],
        constraints={k: dict(v) for k, v in outcome.constraints.items()},
        verdicts={k: dict(v) for k, v in outcome.verdicts.items()},
        blocked_by=list(outcome.blocked_by),
        findings=list(outcome.findings),
        verification_skipped=outcome.verification_skipped,
        purchase_attempts=outcome.purchase_attempts,
        presentable=outcome.presentable,
        single_option=outcome.single_option,
        plan=_steps(outcome.plan),
        plan_signature=list(outcome.plan.signature),
    )


def _empty_response(
    context: ExecutionContext, reason: str, missing_adapters: list[str]
) -> ProcurementRunResponse:
    return ProcurementRunResponse(
        request_id=context.request_id,
        as_of=context.as_of,
        end_code="E4_NOT_STARTED",
        reason=reason,
        blocked_by=missing_adapters,
        missing_adapters=missing_adapters,
        verification_skipped=True,
    )


def _steps(plan: ExecutionPlan) -> list[StepOut]:
    return [
        StepOut(
            seq=s.seq,
            agent=s.agent,
            mode=s.mode,
            call_seq=s.call_seq,
            run_id=s.run_id,
            runtime_status=s.runtime_status,
            business_status=s.business_status,
            used_tools=list(s.used_tools),
            finding_codes=list(s.finding_codes),
            missing_data=list(s.missing_data),
        )
        for s in plan.steps
    ]


# ---------------------------------------------------------------------------
# 조회 — GET /master/runs/{request_id}
# ---------------------------------------------------------------------------


def get_run_history(request_id: str) -> RunHistoryOut:
    """업무 키로 실행 이력을 찾는다.

    ★ 재실행하면 같은 `request_id` 로 행이 여럿 생긴다. **최신을 돌려준다** —
      "그 요청 어떻게 됐냐"에는 마지막 결과가 답이다. 전체 이력이 필요하면
      `run_id` 로 목록을 훑는다.
    """
    row = get_run_by_request_id(request_id)
    plan = list(row.get("plan") or [])
    return RunHistoryOut(
        request_id=row.get("request_id") or request_id,
        as_of=row["as_of"],
        agent=row["agent"],
        cycle=row["cycle"],
        runtime_status=row["runtime_status"],
        elapsed_ms=row.get("elapsed_ms"),
        created_at=row["created_at"],
        plan=plan,
        plan_signature=[
            (str(s.get("agent")), str(s.get("mode")), int(s.get("call_seq", 1))) for s in plan
        ],
        request_payload=dict(row.get("request_payload") or {}),
        response_payload=dict(row.get("response_payload") or {}),
    )
