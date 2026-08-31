"""마스터 API 서비스 — 요청을 Flow 로 옮기고 결과를 응답으로 옮긴다.

★ 여기에 판단을 두지 않는다. 판단은 `flow.py` 에 있다.
  서비스는 **경계 변환**만 한다 — 그래야 API 모양이 바뀌어도 Flow 가 안 흔들린다.
"""

from __future__ import annotations

import time

from app.master import persistence, wiring
from app.master.answer import facts_from_procurement, render_answer
from app.master.budget import CallBudget
from app.master.decision_service import get_decisions
from app.master.envelope import ExecutionContext
from app.master.flow import ProcurementFlow, ProcurementOutcome, VerifierPort
from app.master.inputs import MasterInputs, collect_inputs
from app.master.plan import ExecutionPlan
from app.master.runner import MasterRunner
from app.master.ledger_repository import get_burn_in
from app.master.schemas import (
    BurnInOut,
    DailyClosingOut,
    ProcurementRunRequest,
    ProcurementRunResponse,
    RunHistoryOut,
    StepOut,
)
from app.master.verifier import MasterVerifier
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

    ★ `verifier` 를 주지 않으면 **마스터의 기본 검증 Tool** 이 붙는다 (§3.7.1).
      전에는 기본값이 `None` 이라 API 경로에서 검증이 통째로 건너뛰어졌다 —
      `verification_skipped: true` 로 드러나긴 했지만 아무도 안 봤다.
      **끄려면 명시적으로 꺼야 한다**(`NO_VERIFIER`)는 쪽이 안전하다.
    """
    verifier = MasterVerifier() if verifier is None else verifier
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
        # 못 돈 날도 사람이 읽을 수 있어야 한다 — 빈 응답을 그대로 내보내면 화면이 침묵한다
        response.report_text = render_answer(facts_from_procurement(response))
        # 어댑터가 없어 못 돈 날도 이력에 남긴다 — 안 부른 것과 못 부른 것은 다르다
        response.history_run_id = persistence.record(
            request, response, elapsed_ms=_elapsed(started)
        )
        return response

    inputs = _inputs_for(request)
    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=request.budget))
    outcome = ProcurementFlow(
        runner,
        verifier=verifier,
        item=request.item,
        forecast=request.forecast or _payload(inputs, "forecast"),
        confirmed_orders=request.confirmed_orders or _payload(inputs, "confirmed_orders"),
        policy_values=request.policy_values or _payload(inputs, "policy_values"),
        prior_feedback=request.prior_feedback,
    ).run(has_unmet_obligation=request.has_unmet_obligation)

    response = _to_response(context, outcome, inputs)
    response.concerns = [*response.concerns, *_decision_collision(request_id)]
    response.report_text = render_answer(facts_from_procurement(response))
    # ★ 적재가 돌려준 행 id 를 **응답에 싣는다.** 화면이 승인할 때 이 값을 되돌려 줘야
    #   "내가 본 그것을 승인했다" 가 기록된다 (§DDL 안건 2026-08-30).
    response.history_run_id = persistence.record(request, response, elapsed_ms=_elapsed(started))
    return response


def _decision_collision(request_id: str) -> list[str]:
    """🔴 **이미 결정이 붙은 업무 키로 다시 도는가.**

    `orchestrator_agent_runs` 는 append-only 라 같은 키로 두 번 돌면 **행이 둘**이
    되고, `get_run_by_request_id` 는 **최신 1건**을 돌려준다(그게 맞는 동작이다 —
    *"그 요청 어떻게 됐냐"* 에는 마지막 결과가 답이다).

    문제는 **결정과 결합할 때** 생긴다.

    ```text
    06:22  실행 A (안 3개)
    06:22  '기본' 승인          ← A 의 '기본' 을 골랐다
    06:23  실행 B (같은 키)      ← 이제 조회하면 B 가 나온다
           → 화면에는 "B 의 기본을 승인했다" 로 보인다
    ```

    두 실행의 같은 라벨이 다른 수량이면 **승인한 것과 다른 것이 승인된 것으로
    읽힌다.** 실측(2026-08-29 리허설)에서 재현했다.

    ★ **막지 않고 드러낸다.** 재실행 자체는 죄가 아니고, 승인 게이트를 마스터가
      들고 있으면 안 된다(8/26 회의). 사람이 보고 판단할 일이다.

    ★ **근본 해법이 들어왔다 (2026-08-30, `master_decisions.run_id`).** 결정이
      이제 실행 행을 가리키므로 *"그 결정이 이 실행을 가리키는 것처럼 보인다"* 는
      **더 이상 사실이 아니다.** 그래도 경고는 남긴다 — 이력 조회가 그 키의 **최신
      실행 하나**를 주기 때문에, 같은 키로 다시 돌리면 앞 결정이 보고 있던 계획이
      화면에서 사라진다. 바뀐 것은 **문구가 가리키는 위험**이다.

    ⚠️ 8/30 이전 결정은 `history_run_id` 가 `None` 이다 — 그때는 정말로 어느 실행인지
      모른다. 그 경우 문구가 옛말 그대로여야 맞다.
    """
    try:
        existing = get_decisions(request_id)
    except Exception:  # noqa: BLE001 — 경고를 못 만든다고 실행을 막지 않는다
        return []
    if not existing:
        return []
    current = next((row for row in existing if row.is_current), existing[-1])
    label = f" · {current.scenario_label}" if current.scenario_label else ""
    head = (
        f"DECISION-COLLISION: 이 업무 키에는 이미 결정이 있다 "
        f"({current.decision_seq}회차 {current.decision}{label}). "
    )
    if current.history_run_id:
        tail = (
            f"그 결정은 **다른 실행**({current.history_run_id[:8]}…)을 가리키므로 이 "
            "실행이 승인된 것은 아니다. 다만 이력 조회는 최신 실행 하나만 보여주므로 "
            "앞 결정이 보고 있던 계획이 화면에서 사라진다 — 조건을 바꿔 다시 만들려면 "
            "새 업무 키를 써라"
        )
    else:
        # 8/30 이전 결정 — 정말로 어느 실행인지 모른다
        tail = (
            "그 결정은 **어느 실행을 가리키는지 기록되지 않았다** (2026-08-30 이전). "
            "같은 키로 다시 돌면 그 결정이 이 실행을 가리키는 것처럼 보인다 — "
            "조건을 바꿔 다시 만들려면 새 업무 키를 써라"
        )
    return [head + tail]


def _inputs_for(request: ProcurementRunRequest) -> MasterInputs | None:
    """마스터가 실어 줄 셋을 모은다 (§3.2.5).

    ★ **요청이 직접 준 값이 이긴다.** 백테스트는 그날의 값을 그대로 넣어야 하므로
      적재층이 현재 DB 를 읽어 덮으면 안 된다.

    ★ 품목이 없으면 모으지 않는다 — 셋 다 품목 단위다. 매입이 `missing_data: ["item"]`
      으로 답하는 것이 정상 경로다.

    ★ **적재 실패가 Flow 를 막지 않는다.** 못 실으면 매입이 `missing_data` 로 답하고
      `E4` 가 된다 — 그것도 사실의 기록이다.
    """
    if not request.item:
        return None
    if request.forecast and request.confirmed_orders and request.policy_values:
        return None
    try:
        return collect_inputs(request.item, request.as_of)
    except Exception:  # noqa: BLE001
        return None


def _payload(inputs: MasterInputs | None, key: str):
    if inputs is None:
        return None
    sourced = getattr(inputs, key)
    return sourced.payload if sourced.usable else None


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# ---------------------------------------------------------------------------
# 변환
# ---------------------------------------------------------------------------


def _to_response(
    context: ExecutionContext,
    outcome: ProcurementOutcome,
    inputs: MasterInputs | None = None,
) -> ProcurementRunResponse:
    return ProcurementRunResponse(
        input_sources=inputs.sources() if inputs else {},
        mocked_inputs=list(inputs.mocked) if inputs else [],
        request_id=context.request_id,
        as_of=context.as_of,
        end_code=outcome.end_code,
        reason=outcome.reason,
        scenarios=[dict(s) for s in outcome.scenarios],
        judgment=dict(outcome.judgment),
        constraints={k: dict(v) for k, v in outcome.constraints.items()},
        verdicts={k: dict(v) for k, v in outcome.verdicts.items()},
        blocked_by=list(outcome.blocked_by),
        findings=list(outcome.findings),
        concerns=list(outcome.concerns),
        skipped_checks=list(outcome.skipped_checks),
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
        # 어댑터가 없어 못 돈 날도 **무엇을 못 봤는지**는 남긴다 (§3.7.6)
        skipped_checks=["전 검사: 어댑터 미등록으로 Flow 가 시작되지 않음"],
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
            llm_status=s.llm_status,
            llm_model=s.llm_model,
            llm_attempts=s.llm_attempts,
            llm_fallback_used=s.llm_fallback_used,
        )
        for s in plan.steps
    ]


# ---------------------------------------------------------------------------
# 조회 — GET /master/runs/{request_id}
# ---------------------------------------------------------------------------


def get_burn_in_history() -> BurnInOut:
    """번인 구간(에이전트 판단 전 30일)을 화면이 읽는 형태로.

    ★ **값을 만들지 않는다.** DB 에 심긴 것을 모양만 바꾼다 — 합계·증감률을 여기서
      계산하기 시작하면 재무가 내는 숫자와 갈릴 자리가 생긴다. 화면이 필요하면
      **가진 값으로** 그린다.
    """
    raw = get_burn_in()
    run = raw["run"]
    return BurnInOut(
        sim_run_id=run["sim_run_id"],
        run_type=run["run_type"],
        period_start=run["period_start"],
        period_end=run["period_end"],
        as_of=run["as_of"],
        status=run["status"],
        financing_mode=run.get("financing_mode"),
        note=run.get("note"),
        closings=[DailyClosingOut(**row) for row in raw["closings"]],
    )


def get_run_history(request_id: str) -> RunHistoryOut:
    """업무 키로 실행 이력을 찾는다.

    ★ 재실행하면 같은 `request_id` 로 행이 여럿 생긴다. **최신을 돌려준다** —
      "그 요청 어떻게 됐냐"에는 마지막 결과가 답이다. 전체 이력이 필요하면
      `run_id` 로 목록을 훑는다.

    ★ 결정은 **전부** 싣는다 (실행과 달리 최신 하나로 접지 않는다).
      번복이 있었다는 사실 자체가 답의 일부다 — `is_current` 로 최신만 표시한다.
    """
    row = get_run_by_request_id(request_id)
    plan = list(row.get("plan") or [])
    return RunHistoryOut(
        request_id=row.get("request_id") or request_id,
        run_id=(None if row.get("run_id") is None else str(row["run_id"])),
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
        decisions=get_decisions(request_id),
    )
