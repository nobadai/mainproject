"""발화문 입구 — 분류하고, 확인이 필요 없을 때만 실행하고, 사람 말로 답한다.

```text
발화문 → [LLM ①분류] → 확인 필요? ─예→ 되묻고 끝 (아무것도 안 돈다)
                                 └아니오→ 조회 실행 → [LLM ⑥문장] → 답
```

★ **LLM 이 둘이고 역할이 다르다.** ①이 죽으면 되물어야 하지만(분류를 못 하면 실행할
  수 없다), **⑥이 죽어도 답은 나간다** — 숫자는 규칙이 만들고 LLM 은 앞머리 문장만
  얹기 때문이다 (`answer.py`).

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

from dataclasses import replace
from datetime import date

from app.master import wiring
from app.master.answer import (
    AnswerFacts,
    Fact,
    facts_from_decision,
    facts_from_procurement,
    facts_from_status,
    render_answer,
)
from app.master.ask_schemas import (
    AnswerOut,
    AskExecuteRequest,
    AskRequest,
    AskResponse,
    StatusAnswer,
)
from app.master.budget import CallBudget
from app.master.decision import DecisionIn, DecisionRejected
from app.master.decision_repository import link_follow_up
from app.master.decision_service import record_decision
from app.master.envelope import ExecutionContext
from app.master.llm.answer_runtime import NarrativeService, get_narrative_service
from app.master.llm.runtime import IntentService, get_intent_service
from app.master.llm.schemas import Intent, IntentResult
from app.master.runner import MasterRunner
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse
from app.master.service import get_run_history, make_request_id, run_procurement
from app.master.status_flow import StatusFlow, StatusOutcome

#: 확인 없이 바로 도는 종류. 조회뿐이다.
_AUTO_RUN = frozenset({"STATUS_QUERY"})


def ask(
    request: AskRequest,
    service: IntentService | None = None,
    narrator: NarrativeService | None = None,
) -> AskResponse:
    """발화문을 분류하고, 확인이 필요 없으면 조회까지 돌린 뒤 **사람 말로 답한다.**

    ★ `service`(①분류) · `narrator`(⑥응답)를 주지 않으면 `.env` 설정으로 만든다.
      테스트가 갈아 끼운다. **둘을 나눠 받는 이유는 역할마다 모델 등급이 달라질
      것이기 때문이다** — 분류는 소형이면 되고, 응답 문장도 마찬가지지만 판정 검증은
      아니다.
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
        answer=_write_answer(facts_from_status(outcome), narrator),
    )


def execute(
    request: AskExecuteRequest,
    narrator: NarrativeService | None = None,
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
            answer=_write_answer(facts_from_status(outcome), narrator),
            # ①은 안 부른다 (이미 분류된 의도다). ⑥의 상태는 answer 안에 있다.
            llm_status="SKIPPED_TEMPLATE",
        )

    if intent.action == "PROCUREMENT_RUN":
        response = run_procurement(
            ProcurementRunRequest(
                as_of=request.as_of,
                policy_version=request.policy_version,
                request_id=request_id,
                item=intent.item,
                budget=request.budget,
            )
        )
        # ★ **여기에는 ⑥ 을 붙이지 않는다.** 매입 리포트의 머리말은 이미 완결된 판단
        #   문장이라(`"매입안을 제시합니다. 고르시면 진행합니다."`) LLM 이 얹으면
        #   **같은 말을 두 번** 한다. 실측에서 *"매입안을 제시합니다."* 가 그대로
        #   중복됐다. 더할 것이 없는 자리에 모델을 부르는 것은 비용과 위험만 는다.
        #
        #   조회·결정은 다르다 — 거기 머리말은 문장이 아니라 머리글이라 얹을 자리가 있다.
        return response

    if intent.action == "SELECT_SCENARIO":
        return _record_selection(request)

    if intent.action == "RERUN_WITH_CONDITION":
        return _record_rerun(request)

    if intent.action == "UNKNOWN":
        # **"아직 안 만들었다" 가 아니라 "실행할 것이 없다" 다.** 501 로 답하면 언젠가
        # 되는 것처럼 읽힌다 — `UNKNOWN` 은 분류에 실패했다는 뜻이라 영영 실행되지 않는다.
        raise DecisionRejected(
            "UNKNOWN 은 실행할 수 없다 — 무엇을 할지 정해지지 않았다. 다시 물어라."
        )

    # 종류가 늘어났는데 여기 배선을 안 한 경우. **조용히 통과시키지 않는다.**
    raise NotImplementedError(f"{intent.action} 실행 경로가 배선되지 않았다.")


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


def _record_selection(request: AskExecuteRequest) -> AskResponse:
    """사용자가 **말로 고른 안**을 결정 이력에 적는다 (역할 ⑦ 앞의 사람 게이트).

    ★ **여기서 새로 검사하지 않는다.** 라벨이 그 실행에 실제로 있었나 · 지금 승인할 수
      있는 상태인가는 전부 `decision_service` 가 한다. 발화문 경로라고 검사를 따로 두면
      **두 경로의 승인 기준이 조용히 갈라진다** — 화면에서 누른 승인과 말로 한 승인이
      다른 규칙을 타면 안 된다.

    ★ **마스터 Flow 는 이 경로를 부를 수 없다.** `flow.py` 는 `decision` 계열을
      임포트하지 않는다 (8/26 회의 — 승인 게이트는 툴 목록 바깥).

    🔴 **말에 없는 둘을 화면이 싣는다.** 어느 실행인지(`target_request_id`)와 누가
      승인하는지(`decided_by`)는 발화문에 없다. 없으면 **추측하지 않고 거절한다.**
    """
    intent = request.intent
    if not request.target_request_id:
        raise DecisionRejected(
            "어느 실행의 안인지 지정되지 않았다 — target_request_id 가 필요하다. "
            "발화문에는 그 정보가 없으므로 화면이 실어야 한다."
        )
    if not request.decided_by:
        raise DecisionRejected(
            "승인자가 없다 — decided_by 가 필요하다. 승인자가 없는 승인은 승인이 아니다."
        )

    decision = record_decision(
        request.target_request_id,
        DecisionIn(
            decision="APPROVE",
            scenario_label=intent.scenario_label,
            decided_by=request.decided_by,
            # ★ 화면이 본 실행을 그대로 넘긴다 — 여기서 고르지 않는다.
            history_run_id=request.target_history_run_id,
            note="발화문 경로에서 선택",
        ),
    )
    return AskResponse(
        request_id=decision.request_id,
        as_of=request.as_of,
        outcome="DECISION_RECORDED",
        intent=intent,
        decision=decision,
        answer=_rule_answer(facts_from_decision(decision)),
        # ①도 ⑥도 안 부른다 — 이미 분류된 의도이고, 머리말이 이미 완결 문장이다.
        llm_status="SKIPPED_TEMPLATE",
    )


def _record_rerun(request: AskExecuteRequest) -> AskResponse:
    """조건을 붙인 재요청 — **적고 · 다시 돌리고 · 둘을 잇는다.**

    ```text
    REQUEST_CHANGE 적재 → 새 업무 키로 재실행 → follow_up_request_id 로 연결
    ```

    ★ **조건을 숫자로 해석하지 않는다.** *"예산 2천만원으로 낮춰서"* 를 재무 cap 으로
      꽂으면 마스터가 부서 판단을 덮어쓰는 것이다. 사용자의 말을 그대로
      `prior_feedback` 으로 매입에 넘기고, **해석은 매입이 한다** (§3.2.2).

    🔴 **지금 매입은 그 조건으로 안을 바꾸지 않는다.** `prior_feedback` 을
      `is_refeed`·`attempt` 메타로만 읽는다(`self_check.py`). 그래서 재실행 결과에
      **그 사실을 적어 내보낸다** — 안 적으면 사용자는 조건이 반영된 줄 안다.
      값을 실어 주고 안 쓰는 것을 매입에 지적해 놓고 같은 일을 조용히 할 수는 없다.

    ★ **품목은 원 실행에서 가져온다.** *"예산 줄여서 다시 해줘"* 에는 품목이 없다.
      발화문에 없는 것을 지어내지 않고 **직전 실행이 무엇이었는지**를 본다.
    """
    intent = request.intent
    if not request.target_request_id:
        raise DecisionRejected(
            "어느 실행에 대한 재요청인지 지정되지 않았다 — target_request_id 가 필요하다."
        )
    if not request.decided_by:
        raise DecisionRejected("요청자가 없다 — decided_by 가 필요하다.")
    if not intent.condition:
        raise DecisionRejected("조건이 비어 있다 — 조건 없는 재요청은 그냥 거절이다.")

    decision = record_decision(
        request.target_request_id,
        DecisionIn(
            decision="REQUEST_CHANGE",
            condition_text=intent.condition,
            decided_by=request.decided_by,
            history_run_id=request.target_history_run_id,
            note="발화문 경로에서 조건부 재요청",
        ),
    )

    follow_up_id = make_request_id(request.as_of.isoformat(), seq=decision.decision_seq + 1)
    rerun = run_procurement(
        ProcurementRunRequest(
            as_of=request.as_of,
            policy_version=request.policy_version,
            request_id=follow_up_id,
            item=intent.item or _item_of(request.target_request_id),
            budget=request.budget,
            prior_feedback={
                "condition_text": intent.condition,
                "attempt": decision.decision_seq,
                "requested_by": request.decided_by,
                "origin_request_id": request.target_request_id,
            },
        )
    )
    linked = link_follow_up(decision_id=decision.decision_id, follow_up_request_id=follow_up_id)

    # ★ **답의 본체는 결정이 아니라 다시 만든 안이다.** 사용자가 *"다시 해줘"* 라고
    #   했으니 보고 싶은 것은 새 안이다 — 결정 기록은 그 위에 한 줄로 붙인다.
    passed_on = (
        f"조건 '{intent.condition}' 을 매입에 그대로 전달했습니다 — "
        "다만 매입은 아직 이 조건으로 안을 바꾸지 않습니다(재요청 표시로만 씁니다)"
    )
    unlinked = () if linked else ("이 결정에는 이미 후속 실행이 있어 링크를 잇지 않았습니다",)
    base = facts_from_procurement(rerun)
    facts = replace(
        base,
        facts=(
            *base.facts,
            Fact(label="조건 기록", value=f"{decision.decision_seq}회차 · {intent.condition}"),
            Fact(label="원 실행", value=request.target_request_id),
        ),
        gaps=(*base.gaps, passed_on, *unlinked),
    )
    rerun.report_text = render_answer(facts)
    return AskResponse(
        request_id=follow_up_id,
        as_of=request.as_of,
        outcome="DECISION_RECORDED",
        intent=intent,
        decision=decision.model_copy(update={"follow_up_request_id": follow_up_id}),
        run=rerun,
        answer=_rule_answer(facts),
        llm_status="SKIPPED_TEMPLATE",
    )


def _item_of(request_id: str) -> str | None:
    """직전 실행의 품목. **없으면 비운다** — 지어내지 않는다."""
    try:
        history = get_run_history(request_id)
    except LookupError:
        return None
    item = (history.request_payload or {}).get("item")
    return item if isinstance(item, str) else None


def _rule_answer(facts: AnswerFacts) -> AnswerOut:
    """⑥ 없이 규칙만으로 만드는 답.

    ★ **머리말이 이미 완결된 판단 문장인 곳에는 ⑥ 을 얹지 않는다.**
      *"'기본' 안으로 진행합니다"* · *"매입안을 제시합니다"* 위에 한 문장을 더 쓰면
      **같은 말을 두 번** 한다(실측에서 그랬다). 조회만 머리말이 **머리글**
      (*"조회 결과 — 물류"*)이라 얹을 자리가 있다.
    """
    return AnswerOut(text=render_answer(facts), llm_status="SKIPPED_TEMPLATE")


def _write_answer(facts: AnswerFacts, narrator: NarrativeService | None) -> AnswerOut:
    """⑥ — 문장을 얹어 사람이 읽는 답을 만든다.

    ★ **문장 생성이 실패해도 답은 나간다.** `narrative=None` 이면 규칙이 만든 사실
      줄만으로 완결된다 — LLM 을 답의 뼈대로 쓰지 않는 것이 이 설계의 요지다.
    """
    narrator = narrator or get_narrative_service()
    result = narrator.write(facts)
    return AnswerOut(
        text=render_answer(facts, result.narrative),
        narrative=result.narrative,
        llm_status=result.llm_status,
        llm_attempts=result.llm_attempts,
        llm_fallback_used=result.llm_fallback_used,
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
    answer: AnswerOut | None = None,
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
        answer=answer,
        llm_status=result.llm_status,
        llm_provider=result.llm_provider,
        llm_model=result.llm_model,
        llm_attempts=result.llm_attempts,
        llm_fallback_used=result.llm_fallback_used,
        note=note,
    )
