"""Finance Agent 수명주기 — **한 번의 실행이 어떻게 끝나는가.**

이 파일이 소유하는 것
    준비/검증 → 분기 실행 → Planner Tool 선택 루프 → 업무 결과 확정 → 설명
    → 메타데이터 → 회신 → 이력 의 **순서**와 그 사이를 오가는 값

여기 **없는 것**
    무엇이 합법인가 (`harness`) · 금액 공식 (`tools`) · 판정 (`rules`)
    · 사람이 읽는 문장 (`messages`) · Provider HTTP (`llm`)

★ 순서가 곧 계약이다. 설명은 결과가 확정된 뒤에만 만들 수 있고(Finalizer 는 검증된
  Evidence 만 본다), 이력은 회신이 확정된 뒤에 남는다.

★ 세 조각(수명주기 · 선택 루프 · 결과 확정)이 한 파일에 있는 이유는 **한 흐름**이기
  때문이다. *"재무 Agent 는 어떻게 실행되는가"* 를 알려면 이 파일 하나면 된다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from app.finance import execution, messages
from app.finance.application.harness import (
    DUPLICATE_UNRESOLVED_TOOL_CALL,
    TOOL_BUDGET_EXHAUSTED,
    CapabilityState,
    FinanceHarness,
    FinanceToolDenied,
    FinanceToolRegistry,
    _scenario_identity,
    _short_reason,
    _validate_finance_payload,
    _validate_ready_reasoning,
    guard_replan,
    source_owned_arguments,
    validate_finance_scenario_output,
)
from app.finance.db import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.execution import (
    _adjustment_from_dict,
    _branch_ref,
    _evidence,
    _evidence_dict,
    _evidence_from_dict,
    _finance_dept_meta,
    _indexed_verdict_evidence,
    _json_value,
    _tool_ref,
)
from app.finance.llm.finalizer import DeterministicFinanceFinalizer
from app.finance.llm.planner import (
    DeterministicFinancePlanner,
    FinanceFinalizer,
    FinancePlanner,
    FinancePlannerContractViolation,
    FinancePlannerFailure,
    ToolAction,
    _configured_finance_llms,
)
from app.finance.messages import explanation_for
from app.finance.state import FinanceAgentState
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

# ---------------------------------------------------------------------------
# 업무 결과 확정 — Tool 관측에서 payload/Evidence 로
# ---------------------------------------------------------------------------

#: Tool 결과에서 **업무 회신(payload)으로 올리지 않는** 키.
#:
#: `evidence` · `rules` 는 봉투의 다른 자리로 간다. `critical_cash_date` 는 성격이
#: 다르다 — 설계서가 **Trace/Run History 항목**으로 못박은 값이다.
#: 빼도 추적성은 잃지 않는다: Tool 결과 전체가 observation 으로 남는다.
_NON_PAYLOAD_RESULT_KEYS = frozenset({"evidence", "rules", "critical_cash_date"})


def build_business_result(
    request: AgentRequest, states: list[FinanceAgentState], runtime_status: str
) -> tuple[dict[str, Any], list[Evidence], str, list[SuggestedAdjustment]]:
    if runtime_status != "READY":
        return {}, [], "skipped", []
    if request.mode == "SCENARIO_VALIDATION":
        results = [scenario_result(state) for state in states]
        verdicts = [result["verdict"] for result in results]
        status = (
            "reject"
            if "reject" in verdicts
            else "conditional"
            if "conditional" in verdicts
            else "ok"
        )
        indexed_evidence = _indexed_verdict_evidence(results)
        if "scenarios" in request.payload:
            adjustments = [
                _adjustment_from_dict(adjustment)
                for result in results
                for adjustment in result["suggested_adjustments"]
            ]
            return {"verdicts": results}, indexed_evidence, status, adjustments
        result = results[0]
        branch_evidence = [_evidence_from_dict(item) for item in result.pop("evidences")]
        branch_adjustments = result.pop("suggested_adjustments")
        adjustments = [_adjustment_from_dict(item) for item in branch_adjustments]
        return (
            {"verdicts": [dict(result)], **result},
            [*indexed_evidence, *branch_evidence],
            status,
            adjustments,
        )

    state = states[0]
    payload: dict[str, Any] = {}
    evidences: list[Evidence] = []
    for observation in state.observations:
        result = observation.get("result", {})
        for key, value in result.items():
            if key not in _NON_PAYLOAD_RESULT_KEYS:
                payload[key] = _json_value(value)
        evidences.extend(result.get("evidence", []))
    evidence_by_claim = {item.claim: item for item in evidences}
    return payload, list(evidence_by_claim.values()), "ok", []


def scenario_result(state: FinanceAgentState) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    evidence: list[Evidence] = []
    for observation in state.observations:
        result = observation.get("result", {})
        for key, value in result.items():
            if key not in _NON_PAYLOAD_RESULT_KEYS:
                payload[key] = _json_value(value)
        evidence.extend(result.get("evidence", []))
    validation = next(
        (
            item["result"]
            for item in reversed(state.observations)
            if item.get("tool") == "validate_amount_adjustment"
            and item["result"]["validation_status"] == "PASS"
        ),
        None,
    )
    adjustments: list[dict[str, Any]] = []
    if payload["verdict"] == "ok":
        payload["adjustability"] = "NOT_NEEDED"
    elif validation and Decimal(str(validation["candidate_amount_krw"])) > 0:
        payload["adjustability"] = "ADJUSTABLE"
        adjustments.append(
            {
                "dept": "finance",
                "axis": "amount",
                "target_value": float(validation["candidate_amount_krw"]),
                "unit": "krw",
                "reason": "Verified Finance amount alternative.",
                "ref_ids": [_tool_ref("validate_amount_adjustment", state)],
            }
        )
    else:
        payload["adjustability"] = "NOT_ADJUSTABLE"
    evidence = [item for item in evidence if item.claim != "adjustability"]
    adjustability_code = {
        "NOT_NEEDED": 0,
        "ADJUSTABLE": 1,
        "NOT_ADJUSTABLE": 2,
    }[payload["adjustability"]]
    evidence.append(
        _evidence(
            "adjustability",
            adjustability_code,
            "enum_code",
            _branch_ref("adjustability", state),
        )
    )
    payload["evidences"] = [_evidence_dict(item) for item in evidence]
    payload["suggested_adjustments"] = adjustments
    return payload


def fallback_reasoning(mode: str, business: str) -> str:
    """LLM 이 설명을 못 골랐을 때 나가는 문장.

    🔴 **LLM 경로와 같은 정본을 쓴다.** 예전처럼 대체 경로를 따로 두면, 모델이 죽은
       날에만 사용자에게 다른 말투가 나간다 — 설명이 가장 필요한 날에 설명이 제일
       나빠진다.
    """
    return explanation_for(mode, business)


# ---------------------------------------------------------------------------
# Planner Tool 선택 루프
# ---------------------------------------------------------------------------

#: 되물어도 결과가 같은 반려. **재계획으로 숨기지 않는다.**
#:
#: 중복 요청은 Planner 가 같은 자리를 맴돈다는 뜻이고, 예산 소진은 더 부를 수 없다는
#: 뜻이다. 둘 다 다음 호출에서 달라질 것이 없다.
_TERMINAL_DENIALS = frozenset({DUPLICATE_UNRESOLVED_TOOL_CALL, TOOL_BUDGET_EXHAUSTED})


def branch_requests(request: AgentRequest) -> list[AgentRequest]:
    if request.mode != "SCENARIO_VALIDATION":
        return [request]
    scenarios = request.payload.get("scenarios")
    if scenarios is None:
        payload = dict(request.payload)
        payload["scenario_id"] = _scenario_identity(payload)
        return [replace(request, payload=payload)]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        raise ValueError("SCENARIO_VALIDATION requires one to three scenarios")
    branches: list[AgentRequest] = []
    for scenario in scenarios:
        payload = dict(scenario)
        payload["scenario_id"] = _scenario_identity(payload)
        branches.append(replace(request, payload=payload))
    return branches


def execute_loop(
    state: FinanceAgentState,
    *,
    planner: FinancePlanner,
    harness: FinanceHarness,
) -> None:
    """한 분기의 Tool 선택 루프. 상한 안에서만 되묻는다."""
    while True:
        capability_state = harness.capability_state(state)
        if capability_state.missing and harness.tool_calls >= harness.max_tool_calls:
            # 남은 capability 가 있는데 더 부를 수 없다. 못 낸 답을 낸 척하지 않는다.
            # (`authorize` 도 같은 상한을 보지만, 여기서 먼저 접어야 부를 수 없는
            #  단계에 Planner 호출을 한 번 더 쓰지 않는다.)
            _stop(
                state,
                harness,
                capability_state,
                reason=TOOL_BUDGET_EXHAUSTED,
                message="Finance tool call limit exceeded",
            )

        action = _decide(
            state, planner=planner, harness=harness, capability_state=capability_state
        )
        if action is None:
            continue

        if action.finalize:
            if not capability_state.missing:
                harness.record_step(
                    state, capability_state=capability_state, finalize_requested=True
                )
                return
            # 필수 capability 가 남은 채로는 끝낼 수 없다. 종료 Tool 은 애초에
            # 노출되지 않았고, 그래도 종료를 요청했다면 되묻는다.
            _replan(
                state,
                harness=harness,
                capability_state=capability_state,
                detail={"unresolved": list(capability_state.missing)},
                denied_reason="FINALIZE_BEFORE_REQUIRED_CAPABILITIES",
                finalize_requested=True,
            )
            continue

        if action.tool_name is None:
            raise RuntimeError("planner returned neither a tool nor finalize")

        # ★ **승인이 먼저다.** 인자 원천 확인(`source_owned_arguments`)은 실행 준비이지
        #   승인이 아니다. 순서를 뒤집으면 부를 수도 없는 Tool 의 인자를 먼저 찾다가
        #   실패하고, 실행 순서 오류가 `RUNTIME_NOT_READY` 로 잘못 보고된다.
        try:
            harness.authorize(action.tool_name, action.arguments, state, capability_state)
        except FinanceToolDenied as denied:
            if denied.reason in _TERMINAL_DENIALS:
                _stop(
                    state,
                    harness,
                    capability_state,
                    reason=denied.reason,
                    message=str(denied),
                    requested_tool=action.tool_name,
                    denied_tool=denied.tool,
                    cause=denied,
                )
            harness.note_denied(state, denied.tool, denied.reason)
            _replan(
                state,
                harness=harness,
                capability_state=capability_state,
                detail={
                    "rejected_tool": action.tool_name,
                    "denied_reason": denied.reason,
                    "unresolved": list(capability_state.missing),
                },
                requested_tool=action.tool_name,
                denied_tool=denied.tool,
                denied_reason=denied.reason,
            )
            continue

        arguments = source_owned_arguments(action, state)
        observation = harness.execute(action.tool_name, arguments, state, capability_state)
        state.tool_order.append(action.tool_name)
        state.observations.append(
            {
                "branch_id": state.branch_id,
                "tool": action.tool_name,
                "reason": _short_reason(action.reason),
                "result": observation,
            }
        )
        state.rules.extend(item["rule_id"] for item in observation.get("rules", []))
        harness.record_step(
            state,
            capability_state=capability_state,
            requested_tool=action.tool_name,
            executed_tool=action.tool_name,
        )


def _stop(
    state: FinanceAgentState,
    harness: FinanceHarness,
    capability_state: CapabilityState,
    *,
    reason: str,
    message: str,
    requested_tool: str | None = None,
    denied_tool: str | None = None,
    cause: Exception | None = None,
) -> None:
    """되물어도 같은 반려. **흔적을 남기고 실행을 접는다.**"""
    harness.note_denied(state, denied_tool, reason)
    harness.record_step(
        state,
        capability_state=capability_state,
        requested_tool=requested_tool,
        denied_tool=denied_tool,
        denied_reason=reason,
    )
    raise RuntimeError(message) from cause


def _decide(
    state: FinanceAgentState,
    *,
    planner: FinancePlanner,
    harness: FinanceHarness,
    capability_state: CapabilityState,
) -> ToolAction | None:
    """Planner 를 한 번 부른다. 계약 위반이면 되묻고 `None` 을 돌려준다.

    ★ 노출은 **이번 단계의 실행 가능 Tool 뿐**이다. 부를 수 없는 Tool 을 보여 주면
      모델이 그것을 고르고, 우리는 그 선택을 반려하느라 예산을 쓴다.
    """
    harness.count_llm_call()
    try:
        return planner.decide(
            request=state.request,
            allowed_tools=capability_state.executable_tools,
            observations=tuple(state.observations),
            missing_capabilities=capability_state.missing,
            langchain_tools=harness.langchain_tools(capability_state),
        )
    except FinancePlannerContractViolation as exc:
        # 모델이 계약을 어겼다 — **되물어 볼 가치가 있다.** 왜 반려됐는지를 GUARD 로
        # 남기면 다음 호출의 프롬프트에 그대로 들어간다.
        _replan(
            state,
            harness=harness,
            capability_state=capability_state,
            detail={
                "rejected_action": _short_reason(str(exc)),
                "unresolved": list(capability_state.missing),
            },
            denied_reason="PLANNER_CONTRACT_VIOLATION",
        )
        return None
    except FinancePlannerFailure:
        raise
    except Exception as exc:
        # Provider 장애·네트워크·구조화 출력 파싱 불가 — 다시 물어도 같다.
        raise FinancePlannerFailure(str(exc)) from exc


def _replan(
    state: FinanceAgentState,
    *,
    harness: FinanceHarness,
    capability_state: CapabilityState,
    detail: dict,
    denied_reason: str,
    requested_tool: str | None = None,
    denied_tool: str | None = None,
    finalize_requested: bool = False,
) -> None:
    """반려를 Trace 와 GUARD 에 남기고 상한 안에서 다시 묻는다."""
    harness.record_step(
        state,
        capability_state=capability_state,
        requested_tool=requested_tool,
        denied_tool=denied_tool,
        denied_reason=denied_reason,
        finalize_requested=finalize_requested,
    )
    harness.replans = guard_replan(
        state, harness.replans, detail, max_replans=harness.max_replans
    )


# ---------------------------------------------------------------------------
# Controller — 수명주기
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_REPLANS = 2

#: 답을 내지 못한 실행에서 **사용자가 받는 문장**.
#:
#: ★ 기계 갈래(`failure_kind`)와 사람 문장을 여기서 한 번만 잇는다. 부르는 쪽마다
#:   문장을 고르면 같은 실패가 자리에 따라 다르게 설명된다.
_FAILURE_EXPLANATIONS: dict[str, str] = {
    "INVALID_REQUEST": messages.INVALID_REQUEST,
    "NOT_READY": messages.NOT_READY,
    "INTERNAL": messages.INTERNAL_FAILURE,
}


@dataclass
class _BranchOutcome:
    """분기 실행이 남긴 것. **실패도 값으로 담는다.**"""

    states: list[FinanceAgentState] = field(default_factory=list)
    runtime_status: Literal["READY", "RUNTIME_NOT_READY", "ERROR"] = "READY"
    missing_data: tuple[str, ...] = ()
    #: 개발자가 읽는 기술적 사유. **사용자 문장이 아니다** — Trace 로만 나간다.
    error_reason: str = ""
    #: 사용자에게 무엇을 말해야 하는지 정하는 갈래.
    #:
    #: ★ 예외 타입이 아니라 **어디서 접혔는가**로 나눈다. 같은 `ValueError` 라도
    #:   요청 내용이 틀린 것과 우리 쪽 실행이 어긋난 것은 사용자가 할 일이 다르다.
    failure_kind: Literal["", "INVALID_REQUEST", "NOT_READY", "INTERNAL"] = ""
    planner_failed: bool = False
    #: 이번 실행의 Harness. 실패한 실행에서도 **예산과 반려 사유가 남아야** 한다.
    harness: FinanceHarness | None = None


@dataclass(frozen=True)
class _Explanation:
    """설명과 **그 설명이 어떻게 나왔는지.** 둘은 같이 다녀야 뜻이 통한다."""

    reasoning: str
    llm_status: str
    llm_fallback_used: bool


class FinanceAgentController:
    def __init__(
        self,
        data_port: FinanceAsOfDataPort,
        planner: FinancePlanner | None = None,
        finalizer: FinanceFinalizer | None = None,
        *,
        max_tool_calls: int | None = None,
        max_replans: int | None = None,
    ):
        self.registry = FinanceToolRegistry(data_port)
        if planner is None:
            configured_planner, configured_finalizer, provider_state = (
                _configured_finance_llms()
            )
            self.planner = configured_planner
            self.finalizer = finalizer or configured_finalizer
            self._provider_state = provider_state
            # 설정으로 껐을 때만 DISABLED 다. 주입된 Planner 는 설정과 무관하다.
            self.llm_enabled = provider_state is not None
        else:
            self.planner = planner
            self.finalizer = finalizer or DeterministicFinanceFinalizer()
            self._provider_state = None
            self.llm_enabled = not isinstance(planner, DeterministicFinancePlanner)
        # 🔴 `x or default` 를 쓰지 않는다. 0 은 "상한을 두지 않는다" 가 아니라
        #    **한 번도 부르지 말라**는 뜻이고, 그것을 기본값으로 덮으면 상한이 사라진다.
        self.max_tool_calls = (
            max_tool_calls
            if max_tool_calls is not None
            else int(os.getenv("FINANCE_MAX_TOOL_CALLS", str(DEFAULT_MAX_TOOL_CALLS)))
        )
        self.max_replans = (
            max_replans
            if max_replans is not None
            else int(os.getenv("FINANCE_MAX_REPLANS", str(DEFAULT_MAX_REPLANS)))
        )

    def run(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        """한 번의 재무 실행. **단계마다 무엇을 책임지는지가 이름에 있다.**

            준비/검증 → 분기 실행 → 업무 결과 확정 → 설명 → 메타데이터 → 회신 → 이력

        ★ 순서가 곧 계약이다. 설명은 결과가 확정된 뒤에만 만들 수 있고(Finalizer 는
          검증된 Evidence 만 본다), 이력은 회신이 확정된 뒤에 남는다.
        """
        if request.agent != "finance" or request.mode not in (
            "PRE_PURCHASE",
            "SCENARIO_VALIDATION",
        ):
            raise ValueError("Finance v2.2 supports only its two core modes")
        started = time.monotonic()
        run_id = str(uuid4())

        outcome = self._execute_branches(request)
        payload, evidences, business_status, adjustments = build_business_result(
            request, outcome.states, outcome.runtime_status
        )
        explanation = self._explain(request, outcome, payload, evidences, business_status)
        elapsed = int((time.monotonic() - started) * 1000)

        metadata = self._build_metadata(
            request,
            run_id=run_id,
            outcome=outcome,
            payload=payload,
            explanation=explanation,
            elapsed=elapsed,
        )
        reply = self._build_reply(
            request,
            run_id=run_id,
            outcome=outcome,
            payload=payload,
            evidences=evidences,
            business_status=business_status,
            adjustments=adjustments,
            reasoning=explanation.reasoning,
        )
        return self._persisted(request, reply, metadata), metadata

    # ── 단계 ────────────────────────────────────────────────────

    def _execute_branches(self, request: AgentRequest) -> _BranchOutcome:
        """분기를 돌리고 **실패를 값으로 접는다.**

        네 갈래를 구분해서 접는 것이 핵심이다.
          · 요청 계약 위반    → ERROR, 사용자가 **요청을 고치면** 되는 것
          · Planner 실패      → ERROR, 그리고 `llm_status` 는 FALLBACK 이 된다
          · 입력이 없어서 못 함 → RUNTIME_NOT_READY + missing_data (다시 불러도 같다)
          · 그 밖의 예외       → ERROR (프로그램 오류를 사실로 위장하지 않는다)

        ★ 기술적 사유는 `error_reason` 에 그대로 담는다. 그것은 Trace 로 가고,
          사용자에게는 `failure_kind` 가 고른 한국어 문장이 나간다 — 둘을 섞으면
          사용자가 스택 트레이스 조각을 읽게 된다.
        """
        harness = FinanceHarness(
            self.registry,
            max_tool_calls=self.max_tool_calls,
            max_replans=self.max_replans,
        )
        outcome = _BranchOutcome(harness=harness)
        shared_context = None
        try:
            # ★ 요청 계약 검증만 따로 감싼다. 여기서 나는 잘못은 **보내 주신 내용이
            #   맞지 않는다** 이고, 루프 안에서 나는 잘못은 우리 쪽 사정이다.
            _validate_finance_payload(request)
        except Exception as exc:  # noqa: BLE001 - 요청 계약 검증의 실패는 요청의 문제다.
            outcome.runtime_status = "ERROR"
            outcome.failure_kind = "INVALID_REQUEST"
            outcome.error_reason = str(exc)
            return outcome
        try:
            for branch_request in branch_requests(request):
                branch_id = str(branch_request.payload.get("scenario_id", "PRE_PURCHASE"))
                state = FinanceAgentState(
                    branch_request,
                    branch_id=branch_id,
                    context_cache=shared_context,
                )
                # ★ 루프 **전에** 담는다. 실패해도 그때까지의 observation 과 재계획
                #   횟수가 이력에 남아야 한다 — 실패한 실행일수록 흔적이 필요하다.
                outcome.states.append(state)
                execute_loop(state, planner=self.planner, harness=harness)
                shared_context = state.context_cache
        except FinancePlannerFailure as exc:
            outcome.planner_failed = True
            outcome.runtime_status, outcome.error_reason = "ERROR", str(exc)
            outcome.failure_kind = "INTERNAL"
        except FinanceDataNotReady as exc:
            outcome.runtime_status = "RUNTIME_NOT_READY"
            outcome.missing_data = (exc.key,)
            outcome.error_reason = str(exc)
            outcome.failure_kind = "NOT_READY"
        except (ValueError, TypeError) as exc:
            # 분기 분해·인자 원천 확인이 낸 계약 위반. 요청 내용이 원인이다.
            outcome.runtime_status = "ERROR"
            outcome.error_reason = str(exc)
            outcome.failure_kind = "INVALID_REQUEST"
        except Exception as exc:  # noqa: BLE001 - Agent boundary converts failures to ERROR.
            outcome.runtime_status, outcome.error_reason = "ERROR", str(exc)
            outcome.failure_kind = "INTERNAL"
        return outcome

    def _explain(
        self,
        request: AgentRequest,
        outcome: _BranchOutcome,
        payload: dict[str, Any],
        evidences: list[Evidence],
        business_status: str,
    ) -> _Explanation:
        """검증된 Evidence 로 설명을 **고른다.** 설명이 결과를 바꾸지는 않는다.

        🔴 LLMStatus 는 **이번 실행에서 실제로 무슨 일이 있었는가**다
           (envelope §LLMStatus). 예전에는 `SUCCESS if attempts else DISABLED` 였다.
           그러면 LLM 을 켜 두고도 Controller 가 첫 Tool 전에 접힌 실행이 전부
           `DISABLED` 로 남는다 — 이력에는 *"LLM 을 안 켰다"* 고 적히고, 실제로는
           **켜 뒀는데 부를 일이 없었다** 이다. 둘은 다음 조치가 다르다.
        """
        llm_status = self._llm_status(planner_failed=outcome.planner_failed)
        if outcome.runtime_status != "READY":
            # 못 낸 이유를 말한다. Finalizer 를 부르지 않는다 — 검증된 결과가 없다.
            #
            # 🔴 예전에는 예외 문자열을 그대로 실어 보냈다. 그러면 사용자가
            #    *"Finance tool call limit exceeded"* 같은 문장을 받는다 — 무슨 일이
            #    있었는지도, 다음에 무엇을 해야 하는지도 알 수 없다. 기술적 사유는
            #    Trace 에 그대로 남고(`failure_reason`), 여기서는 **할 일**을 말한다.
            return _Explanation(
                _FAILURE_EXPLANATIONS[outcome.failure_kind or "INTERNAL"],
                llm_status,
                outcome.planner_failed,
            )

        finalization_evidence = [*evidences]
        for verdict in payload.get("verdicts", []):
            finalization_evidence.extend(
                _evidence_from_dict(item) for item in verdict.get("evidences", [])
            )
        try:
            reasoning = self.finalizer.finalize(
                mode=request.mode,
                business_status=business_status,
                evidences=tuple(finalization_evidence),
            )
            _validate_ready_reasoning(reasoning)
        except Exception:  # noqa: BLE001 - complete Evidence permits safe fallback.
            # ★ 답은 나간다 — 규칙이 만든 답이다. 검증된 Evidence 가 이미 있으므로
            #   설명을 못 골랐다고 업무 결과를 버릴 이유가 없다.
            return _Explanation(
                fallback_reasoning(request.mode, business_status),
                "DISABLED" if not self.llm_enabled else "FALLBACK",
                self.llm_enabled,
            )
        return _Explanation(
            reasoning,
            self._llm_status(planner_failed=outcome.planner_failed),
            outcome.planner_failed,
        )

    def _build_metadata(
        self,
        request: AgentRequest,
        *,
        run_id: str,
        outcome: _BranchOutcome,
        payload: dict[str, Any],
        explanation: _Explanation,
        elapsed: int,
    ) -> ExecutionMetadata:
        """실행 흔적. **Business Reply 와 섞지 않는다.**"""
        states = outcome.states
        observations = [item for state in states for item in state.observations]
        dept_meta = _finance_dept_meta(request.mode, payload, states)
        if dept_meta is not None and outcome.runtime_status == "READY":
            observations.append(dept_meta)
        if self._provider_state is not None:
            # ★ Provider 대체는 `llm_status` 가 아니라 **여기서** 드러난다 (§17).
            observations.append(
                {
                    "observation_type": "finance_llm_provider",
                    "primary_provider": self._provider_state.primary_provider,
                    "effective_provider": self._provider_state.effective_provider,
                    "provider_fallback_used": self._provider_state.active,
                    "provider_fallback_reason": self._provider_state.reason,
                }
            )
        used_tools = [item for state in states for item in state.tool_order]
        rules = [f"{state.branch_id}:{rule}" for state in states for rule in state.rules]
        trace = self._harness_trace(
            outcome, used_tools=used_tools, rules=rules, explanation=explanation
        )
        if trace is not None:
            # ★ 맨 뒤에 붙인다. 앞자리는 Tool 관측이 쓰던 자리이고, 읽는 쪽이 그것을
            #   전제로 붙어 있다 — 흔적을 더하려다 기존 계약을 흔들지 않는다.
            observations.append(trace)
        return ExecutionMetadata(
            run_id=run_id,
            request_id=request.context.request_id,
            agent="finance",
            used_tools=tuple(used_tools),
            tool_order=tuple(range(1, len(used_tools) + 1)),
            observations=tuple(
                json.dumps(o, default=str, sort_keys=True) for o in observations
            ),
            rules_applied=tuple(rules),
            # 🔴 실행 지역변수가 아니라 상태에서 센다. 루프가 예외로 끝나면 지역
            #    변수는 갱신되지 않아 **실패한 실행의 재계획이 0 으로 남았다** — 가장
            #    알아야 할 실행에서 숫자가 사라진다.
            replans=sum(state.replans for state in states),
            llm_status=explanation.llm_status,
            llm_model=(
                self.finalizer.model if self.finalizer.attempts else self.planner.model
            ),
            llm_attempts=self.planner.attempts + self.finalizer.attempts,
            llm_fallback_used=explanation.llm_fallback_used,
            elapsed_ms=elapsed,
        )

    @staticmethod
    def _harness_trace(
        outcome: _BranchOutcome,
        *,
        used_tools: list[str],
        rules: list[str],
        explanation: _Explanation,
    ) -> dict[str, Any] | None:
        """실행 흔적 한 덩어리. **선언이 아니라 관측이다.**

        여기서 알 수 있어야 하는 것은 하나다 — *LLM 이 무엇을 요청했고, Harness 가
        무엇을 허락했고, 무엇이 실제로 돌았는가.* 셋이 어긋나면 그 자리가 보인다.
        """
        harness = outcome.harness
        if harness is None:
            return None
        return {
            "observation_type": "finance_harness_trace",
            "steps": [item for state in outcome.states for item in state.trace],
            "denials": harness.denials,
            "tool_calls": harness.tool_calls,
            "llm_calls": harness.llm_calls,
            "replans": sum(state.replans for state in outcome.states),
            "max_tool_calls": harness.max_tool_calls,
            "max_replans": harness.max_replans,
            "executed_tools": list(used_tools),
            "rules_applied": list(rules),
            "runtime_status": outcome.runtime_status,
            "llm_status": explanation.llm_status,
            # ★ 기술적 사유는 **여기에만** 산다. 사용자 회신에는 같은 사실을 사람
            #   말로 옮긴 문장이 나간다 — 둘 다 필요하고, 읽는 사람이 다르다.
            "failure_kind": outcome.failure_kind,
            "failure_reason": outcome.error_reason[:240],
        }

    def _build_reply(
        self,
        request: AgentRequest,
        *,
        run_id: str,
        outcome: _BranchOutcome,
        payload: dict[str, Any],
        evidences: list[Evidence],
        business_status: str,
        adjustments: list[SuggestedAdjustment],
        reasoning: str,
    ) -> AgentReply:
        # 근거가 없어 뺀 정책값을 밝힌다. 실행은 계속했지만 **못 낸 것을 낸 척하지
        # 않는다** (§3.7.6). 이미 담긴 missing_data 뒤에 붙이고 중복은 지운다.
        missing_data = tuple(
            dict.fromkeys(
                [
                    *outcome.missing_data,
                    *(item for state in outcome.states for item in state.missing_sources),
                ]
            )
        )
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="finance",
            mode=request.mode,
            run_id=run_id,
            runtime_status=outcome.runtime_status,
            business_status=business_status,
            payload=payload,
            evidences=tuple(evidences),
            suggested_adjustments=tuple(adjustments),
            reasoning=reasoning,
            missing_data=missing_data,
            needs_followup=(outcome.runtime_status == "RUNTIME_NOT_READY" or bool(adjustments)),
            additional_validation_required=False,
        )
        nested_findings = validate_finance_scenario_output(reply)
        if not nested_findings:
            return reply
        # 중첩 판정이 계약을 어겼으면 **그 결과를 내보내지 않는다.** 그럴듯한 판정이
        # 틀렸다는 사실만 아무도 모르는 것보다, 안 내는 편이 낫다.
        return replace(
            reply,
            runtime_status="ERROR",
            business_status="skipped",
            payload={},
            evidences=(),
            suggested_adjustments=(),
            reasoning=messages.RESULT_NOT_TRUSTWORTHY,
            needs_followup=True,
        )

    @staticmethod
    def _persisted(
        request: AgentRequest, reply: AgentReply, metadata: ExecutionMetadata
    ) -> AgentReply:
        """이력을 남긴다. **정상 완료에는 해석 가능한 run_id 가 반드시 있어야 한다.**"""
        try:
            execution.save_finance_execution(
                request=request, reply=reply, metadata=metadata
            )
        except Exception:  # noqa: BLE001 - persistence failure is an Agent ERROR value.
            return replace(
                reply,
                runtime_status="ERROR",
                business_status="skipped",
                payload={},
                evidences=(),
                suggested_adjustments=(),
                reasoning=messages.PERSISTENCE_FAILED,
                missing_data=(),
                needs_followup=True,
            )
        return reply

    def _llm_status(self, *, planner_failed: bool) -> str:
        """공용 `LLMStatus` 의미를 재무 실행에 그대로 적용한다.

            DISABLED          설정으로 껐다
            SKIPPED_TEMPLATE  켜져 있는데 **이번 실행에서는 부를 일이 없었다**
            SUCCESS           실제로 불렀고 쓸 수 있는 답을 받았다
            FALLBACK          불렀는데 실패해서 결정론이 대신 답했다

        ★ Gemini→Gemma **Provider 대체는 `FALLBACK` 이 아니다.** LLM 은 답을 냈다 —
          다른 Provider 가 냈을 뿐이다. 그 사실은 observations 로 따로 남긴다 (§17).
        """
        if not self.llm_enabled:
            return "DISABLED"
        if planner_failed:
            return "FALLBACK"
        if self.planner.attempts + self.finalizer.attempts == 0:
            return "SKIPPED_TEMPLATE"
        return "SUCCESS"
