"""마스터 API 서비스 — 요청을 Flow 로 옮기고 결과를 응답으로 옮긴다.

★ 여기에 판단을 두지 않는다. 판단은 `flow.py` 에 있다.
  서비스는 **경계 변환**만 한다 — 그래야 API 모양이 바뀌어도 Flow 가 안 흔들린다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.master import persistence, wiring
from app.master.answer import facts_from_procurement, render_answer
from app.master.budget import CallBudget
from app.master.decision import CommitmentOut
from app.master.decision_service import commitments_before, get_decisions
from app.master.envelope import ExecutionContext
from app.master.flow import ProcurementFlow, ProcurementOutcome, VerifierPort
from app.master.inputs import MasterInputs, collect_inputs
from app.master.ledger_repository import get_burn_in
from app.master.plan import ExecutionPlan
from app.master.report import render_report, report_filename
from app.master.run_repository import get_run_by_request_id
from app.master.runner import MasterRunner
from app.master.schemas import (
    AdjustmentOut,
    BlockedAgentOut,
    BurnInOut,
    DailyClosingOut,
    EvidenceOut,
    ProcurementRunRequest,
    ProcurementRunResponse,
    ReportOut,
    RunHistoryOut,
    StepOut,
)
from app.master.verifier import MasterVerifier

logger = logging.getLogger(__name__)


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
    commitments = _approved_commitments(request)
    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=request.budget))
    outcome = ProcurementFlow(
        runner,
        verifier=verifier,
        item=request.item,
        forecast=request.forecast or _payload(inputs, "forecast"),
        confirmed_orders=request.confirmed_orders or _payload(inputs, "confirmed_orders"),
        policy_values=request.policy_values or _payload(inputs, "policy_values"),
        prior_feedback=request.prior_feedback,
        approved_commitments=commitments.carried,
        # ★ 값과 출처를 **떼어 놓지 않는다.** 응답에만 싣던 것을 payload 에도 나른다.
        input_sources=inputs.sources() if inputs else {},
        # 🔴 실어 주기만 하지 않고 **막는 쪽까지** 잇는다 (2026-09-03).
        #   응답의 `mocked_inputs` 는 화면 경고용이고, 이것은 실행을 세우는 용도다.
        mocked_inputs=inputs.mocked if inputs else (),
    ).run(has_unmet_obligation=request.has_unmet_obligation)

    response = _to_response(context, outcome, inputs)
    response.concerns = [
        *response.concerns,
        *_decision_collision(request_id),
        *_evidence_contract_concerns(outcome),
        # ★ 약정을 못 읽었거나 못 실은 사실 (#185 후속). 재호출로 안 고쳐지므로
        #   findings 가 아니라 concerns 다.
        *commitments.concerns,
    ]
    response.report_text = render_answer(facts_from_procurement(response))
    # ★ 적재가 돌려준 행 id 를 **응답에 싣는다.** 화면이 승인할 때 이 값을 되돌려 줘야
    #   "내가 본 그것을 승인했다" 가 기록된다 (§DDL 안건 2026-08-30).
    response.history_run_id = persistence.record(request, response, elapsed_ms=_elapsed(started))
    return response


def _decision_collision(request_id: str) -> list[str]:
    """🔴 **이미 결정이 붙은 업무 키로 다시 도는가.**

    `master_agent_runs` 는 append-only 라 같은 키로 두 번 돌면 **행이 둘**이
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


@dataclass(frozen=True)
class _CommitmentLookup:
    """어제까지의 약정 조회 결과. **실은 것과 못 실은 이유를 같이 든다** (#185 후속).

    🔴 전에는 `list[dict]` 하나였다. 그래서 **셋이 전부 빈 목록**이었다.

    ```text
    승인이 없었다        정상
    조회가 깨졌다        사고
    승인은 있는데 못 만들었다  사고
    ```

    받는 쪽에서 셋이 구별되지 않으면 *"어제 승인이 없었나 보다"* 로 읽힌다.
    §1.2-10 의 **0 과 모름은 다르다**가 그대로 걸리는 자리다.
    """

    carried: list[dict[str, Any]] = field(default_factory=list)
    concerns: tuple[str, ...] = ()


def _approved_commitments(request: ProcurementRunRequest) -> _CommitmentLookup:
    """어제까지 승인된 확정 입고 약정 (#185).

    🔴 **못 읽는 것을 없는 것으로 만들지 않는다.** 이력 DB 는 없어도 Flow 가 도는
      것이 계약이라(`history_enabled`) 예외를 올리지는 않는다. 대신 **못 읽었다는
      사실을 `concerns` 로 응답에 남긴다** — 조용히 비우면 어제 승인이 없었던 것과
      같아진다.

    ★ **재호출로 안 고쳐지므로 `findings` 가 아니다.** 매입을 몇 번 다시 불러도
      DB 가 안 읽히는 사실은 그대로다. 사람이 볼 것이지 재시도할 것이 아니다.

    ★ 품목이 없으면 묻지 않는다. 약정은 품목별이라 물을 대상이 없다.
    """
    if not request.item:
        return _CommitmentLookup()
    try:
        found = commitments_before(request.item, request.as_of)
    except Exception as exc:
        # ★ 이력 조회 실패가 매입 실행을 막지 않는다. 다만 조용히 넘어가지도 않는다.
        logger.exception("어제까지의 승인 약정 조회 실패 - 실행은 그대로 돈다")
        note = (
            f"어제까지 승인된 확정 입고 약정을 못 읽었다 (조회 실패: {type(exc).__name__})"
            " - 이번 실행은 어제 승인분을 모른 채 돌았다. '승인이 없었다' 와 다르다."
        )
        return _CommitmentLookup(concerns=(note,))
    return _CommitmentLookup(
        [c.model_dump(mode="json") for c in found if c.buildable],
        tuple(_commitment_gap(c) for c in found if _commitment_gap(c)),
    )


def _commitment_gap(commitment: CommitmentOut) -> str:
    """이 약정이 **온전히 실렸는가.** 아니면 그 사유. 온전하면 빈 문자열이다.

    🔴 **두 갈래를 다 본다.**

    ```text
    buildable=False              아예 안 실린다
    buildable + 빈 arrival_schedule  실리는데 도착 일정이 없다
    ```

      뒤가 더 위험하다 - 물류가 받기는 받고 **"입고 예정이 없다"** 로 읽는다.
      `CommitmentOut.notes` 가 그 사유를 이미 들고 있는데 아무도 안 봤다.
    """
    label = commitment.approval_id or commitment.scenario_label or "승인분"
    if not commitment.buildable:
        return (
            f"{label}: 어제 승인된 매입의 약정을 못 만들어 경계 호출에 안 실었다"
            f" - {commitment.reason}"
        )
    if not commitment.arrival_schedule:
        note = " / ".join(commitment.notes) or "사유 없음"
        return f"{label}: 약정은 섰는데 도착 일정이 비었다 - {note}"
    return ""


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
        evidences=_evidences_out(outcome),
        adjustments=_adjustments_out(outcome),
        verdicts={k: dict(v) for k, v in outcome.verdicts.items()},
        blocked_by=list(outcome.blocked_by),
        blocked_failures=_blocked_out(outcome),
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


def _evidence_contract_concerns(outcome: ProcurementOutcome) -> list[str]:
    """🔴 **근거의 값이 계약과 다른가.**

    `contracts_core.Evidence.value` 는 `float` 인데 `Evidence` 가 dataclass 라
    런타임 검증이 없다. 그래서 문자열을 넣어도 아무 데서도 안 걸리고, 실제로
    재무 `policy_version_used` 가 `"v1.3-PROVISIONAL"` 을 싣고 있다
    (`finance/capabilities/procurement.py:175`).

    ★ **값을 고치거나 버리지 않는다.** 고치면 남의 값을 덮어쓰는 것이고(§3.2.2),
      버리면 근거를 고르는 것이다. 원본은 그대로 나가고 여기서 **사실만 적는다.**

    ★ `findings` 가 아니라 `concerns` 다. **매입을 다시 불러도 안 고쳐진다** -
      남의 계약 문제라 사람이 봐야 한다 (§3.4). 재무 회신의 봉투 위반 때문에
      매입을 재호출하던 것과 같은 실수를 반복하지 않는다.

    ⚠️ 이 검사는 **증상만 잡는다.** 어느 쪽이 맞는지는 정하지 않는다 - 계약을
      넓힐지(문자열 근거를 허용) 재무가 다른 칸을 쓸지는 팀이 정할 일이다.
    """
    offenders = [
        f"{item.agent}:{item.evidence.claim}={item.evidence.value!r}"
        for item in outcome.evidences
        if not isinstance(item.evidence.value, (int, float))
        or isinstance(item.evidence.value, bool)
    ]
    if not offenders:
        return []
    message = (
        "EVIDENCE-VALUE-NOT-NUMERIC: 근거의 value 가 계약(float)과 다른 것이 "
        f"{len(offenders)}건이다 - {', '.join(offenders)}. 값은 그대로 나갔고 "
        "마스터가 고치지 않았다. 계약을 넓힐지 부서가 다른 칸을 쓸지는 팀이 정한다."
    )
    return [message]


def _evidences_out(outcome: ProcurementOutcome) -> list[EvidenceOut]:
    """부서 근거를 응답 모양으로. **고르지도 요약하지도 않는다.**

    ★ 순서를 손대지 않는다 - 부서가 낸 순서가 그 부서의 설명 순서다.
      마스터가 정렬하면 "이게 더 중요하다" 는 뜻이 생긴다 (§3.2.2).

    ★ 빈 목록은 "근거가 완비됐다" 가 아니라 **부서가 근거를 안 냈다**는 사실이다.
      화면이 그렇게 읽도록 스키마 설명에 적어 두었다.
    """
    return [
        EvidenceOut(
            agent=item.agent,
            mode=item.mode,
            claim=item.evidence.claim,
            source=item.evidence.source,
            value=item.evidence.value,
            unit=item.evidence.unit,
            evidence_grade=item.evidence.evidence_grade,
            evidence_detail=item.evidence.evidence_detail,
            ref_ids=list(item.evidence.ref_ids),
        )
        for item in outcome.evidences
    ]


def _adjustments_out(outcome: ProcurementOutcome) -> list[AdjustmentOut]:
    """조정안을 표준형 그대로 옮긴다. **고르지도 정렬하지도 않는다.**

    ★ 순서는 부서가 보낸 차례다. 정렬하면 그것이 우선순위로 읽힌다 -
      근거(`_evidences_out`)와 같은 이유다 (§3.2.2).
    """
    return [
        AdjustmentOut(
            dept=a.dept,
            axis=a.axis,
            target_value=a.target_value,
            unit=a.unit,
            reason=a.reason,
            ref_ids=list(a.ref_ids),
            # 부서가 안 채우면 빈 목록·None 그대로 나간다 - 없는 것을 만들지 않는다
            scenario_labels=list(a.scenario_labels),
            split_date=a.split_date,
        )
        for a in outcome.adjustments
    ]


def _blocked_out(outcome: ProcurementOutcome) -> list[BlockedAgentOut]:
    """막은 부서를 사유째로 옮긴다. **순서를 바꾸지 않는다.**

    ★ `detail` 을 여기서 다시 만들지 않고 `AgentFailure.detail` 을 부른다 -
      `reason` 문장에 들어간 것과 **같은 함수**여야 둘이 갈리지 않는다.
    """
    return [
        BlockedAgentOut(
            agent=f.agent,
            runtime_status=f.runtime_status,
            reasoning=f.reasoning,
            missing_data=list(f.missing_data),
            detail=f.detail,
        )
        for f in outcome.blocked_failures
    ]


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
            reasoning=s.reasoning,
            llm_status=s.llm_status,
            llm_model=s.llm_model,
            llm_attempts=s.llm_attempts,
            llm_fallback_used=s.llm_fallback_used,
            replans=s.replans,
            # 마스터는 읽지 않고 나른다 - 순서도 부서가 낸 그대로다
            observations=list(s.observations),
        )
        for s in plan.steps
    ]


# ---------------------------------------------------------------------------
# 조회 — GET /master/runs/{request_id}
# ---------------------------------------------------------------------------


def get_run_report(request_id: str) -> ReportOut:
    """저장된 실행 하나를 보고서로.

    ★ **최신 실행을 쓴다** — `get_run_history` 와 같은 규칙이다. *"그 요청 어떻게
      됐냐"* 에는 마지막 결과가 답이다.

    ★ 매입안 보고서다. 조회(STATUS)는 안이 없어 보고서가 성립하지 않으므로
      사이클을 밝힌다 (2026-09-02).
    """
    row = get_run_by_request_id(request_id, cycle="PROCUREMENT")
    run = dict(row.get("response_payload") or {})
    if not run:
        raise LookupError(f"실행 원문이 없어 보고서를 만들 수 없습니다: {request_id}")
    run.setdefault("request_id", request_id)
    return ReportOut(
        request_id=request_id,
        filename=report_filename(run),
        markdown=render_report(run),
    )


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
    # ★ 조회(STATUS)가 같은 업무 키로 적재되므로 사이클을 밝힌다 (2026-09-02).
    #   이 화면은 매입 실행 이력이다 - 안 밝히면 최신 조회가 매입 자리에 뜬다.
    row = get_run_by_request_id(request_id, cycle="PROCUREMENT")
    plan = list(row.get("plan") or [])
    return RunHistoryOut(
        request_id=row.get("request_id") or request_id,
        run_id=(None if row.get("run_id") is None else str(row["run_id"])),
        as_of=row["as_of"],
        # ★ 표에 `agent` 컬럼이 없다 (2026-09-02, master_agent_runs 로 이전).
        #   마스터 전용 표라 늘 같은 값이었고, 상수를 컬럼으로 두면 "언젠가 다른 값이
        #   들어올 수 있다" 로 읽힌다. 응답 모양은 유지한다 - 화면이 쓰고 있다.
        agent="master",
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
