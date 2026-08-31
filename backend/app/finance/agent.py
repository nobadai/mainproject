"""Tool을 사용하는 Finance Agent 진입 경로.

이 모듈은 의도적으로 ``FinanceSnapshot``을 import하지 않는다. 레거시
매입/영업 서비스는 호환성 용도로만 남기고, 이 경로는 M-1 실행 컨텍스트를
경계로 사용한다.

★ **여기 남는 것은 Agent 자신뿐이다.** LLM Provider·Planner·Finalizer 는
  `app.finance.llm`, capability 실행은 `tool_registry`, 실행 상태는 `state`,
  근거 규율은 `evidence`, 관측 사이드카는 `execution` 이 맡는다. 이 모듈이 하는 일은
  **그것들을 순서대로 부르는 것**이다.

★ 아래 재노출(re-export)은 **호환을 위한 것**이다. `app.finance.agent` 를 통해 들어오던
  기존 import 와 테스트의 patch 대상을 그대로 살려 둔다 — 옮기면서 부르는 쪽을
  깨뜨리지 않는 것이 이번 정리의 조건이었다.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from app.finance.evidence import (
    _adjustment_from_dict,
    _branch_ref,
    _evidence,
    _evidence_dict,
    _evidence_from_dict,
    _indexed_verdict_evidence,
    _json_value,
    _tool_ref,
)
from app.finance.execution import FINANCE_CAP_CHECK_ID, _finance_dept_meta
from app.finance.llm.config import finance_llm_enabled
from app.finance.llm.contracts import (
    FinanceFinalizer,
    FinanceMode,
    FinancePlanner,
    FinancePlannerContractViolation,
    FinancePlannerFailure,
    ToolAction,
)
from app.finance.llm.finalizer import _FINAL_EXPLANATIONS, DeterministicFinanceFinalizer
from app.finance.llm.planner import DeterministicFinancePlanner
from app.finance.llm.provider import _configured_finance_llms
from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.run_repository import save_finance_execution
from app.finance.state import (
    _CAPABILITY_TOOLS,
    _PRE_REQUIRED_CAPABILITIES,
    _SCENARIO_REQUIRED_CAPABILITIES,
    FinanceAgentState,
    ScenarioPayment,
    _satisfied_capabilities,
    _scenario_verdict,
)
from app.finance.tool_registry import (
    PRE_PURCHASE_TOOLS,
    SCENARIO_VALIDATION_TOOLS,
    FinanceToolRegistry,
    _scenario_schedule,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_REPLANS = 2

__all__ = [
    "DEFAULT_MAX_REPLANS",
    "DEFAULT_MAX_TOOL_CALLS",
    "FINANCE_CAP_CHECK_ID",
    "PRE_PURCHASE_TOOLS",
    "SCENARIO_VALIDATION_TOOLS",
    "DeterministicFinanceFinalizer",
    "DeterministicFinancePlanner",
    "FinanceAgentController",
    "FinanceAgentState",
    "FinanceFinalizer",
    "FinanceMode",
    "FinancePlanner",
    "FinancePlannerContractViolation",
    "FinancePlannerFailure",
    "FinanceToolRegistry",
    "ScenarioPayment",
    "ToolAction",
    # 아래 비공개 이름은 재무 안에서만 쓰는 호환 재노출이다.
    "_finance_dept_meta",
    "_indexed_verdict_evidence",
    "_scenario_schedule",
    "finance_llm_enabled",
    "validate_finance_scenario_output",
]


@dataclass
class _BranchOutcome:
    """분기 실행이 남긴 것. **실패도 값으로 담는다.**"""

    states: list[FinanceAgentState] = field(default_factory=list)
    runtime_status: Literal["READY", "RUNTIME_NOT_READY", "ERROR"] = "READY"
    missing_data: tuple[str, ...] = ()
    error_reason: str = ""
    planner_failed: bool = False


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
        self.max_tool_calls = max_tool_calls or int(
            os.getenv("FINANCE_MAX_TOOL_CALLS", str(DEFAULT_MAX_TOOL_CALLS))
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
        payload, evidences, business_status, adjustments = self._finalize(
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

        세 갈래를 구분해서 접는 것이 핵심이다.
          · Planner 실패      → ERROR, 그리고 `llm_status` 는 FALLBACK 이 된다
          · 입력이 없어서 못 함 → RUNTIME_NOT_READY + missing_data (다시 불러도 같다)
          · 그 밖의 예외       → ERROR (프로그램 오류를 사실로 위장하지 않는다)
        """
        outcome = _BranchOutcome()
        shared_context = None
        seen: set[str] = set()
        total_calls = 0
        total_replans = 0
        try:
            _validate_finance_payload(request)
            for branch_request in self._branch_requests(request):
                branch_id = str(branch_request.payload.get("scenario_id", "PRE_PURCHASE"))
                state = FinanceAgentState(
                    branch_request,
                    branch_id=branch_id,
                    context_cache=shared_context,
                )
                # ★ 루프 **전에** 담는다. 실패해도 그때까지의 observation 과 재계획
                #   횟수가 이력에 남아야 한다 — 실패한 실행일수록 흔적이 필요하다.
                outcome.states.append(state)
                total_calls, total_replans = self._execute_loop(
                    state,
                    seen=seen,
                    total_calls=total_calls,
                    total_replans=total_replans,
                )
                shared_context = state.context_cache
        except FinancePlannerFailure as exc:
            outcome.planner_failed = True
            outcome.runtime_status, outcome.error_reason = "ERROR", str(exc)
        except FinanceDataNotReady as exc:
            outcome.runtime_status = "RUNTIME_NOT_READY"
            outcome.missing_data = (exc.key,)
            outcome.error_reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - Agent boundary converts failures to ERROR.
            outcome.runtime_status, outcome.error_reason = "ERROR", str(exc)
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
            # 못 낸 이유가 곧 설명이다. Finalizer 를 부르지 않는다 — 검증된 결과가 없다.
            return _Explanation(outcome.error_reason[:240], llm_status, outcome.planner_failed)

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
                self._fallback_reasoning(request.mode, business_status),
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
            reasoning="Finance scenario output validation failed.",
            needs_followup=True,
        )

    @staticmethod
    def _persisted(
        request: AgentRequest, reply: AgentReply, metadata: ExecutionMetadata
    ) -> AgentReply:
        """이력을 남긴다. **정상 완료에는 해석 가능한 run_id 가 반드시 있어야 한다.**"""
        try:
            save_finance_execution(request=request, reply=reply, metadata=metadata)
        except Exception:  # noqa: BLE001 - persistence failure is an Agent ERROR value.
            return replace(
                reply,
                runtime_status="ERROR",
                business_status="skipped",
                payload={},
                evidences=(),
                suggested_adjustments=(),
                reasoning="Finance run history persistence failed.",
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

    def _branch_requests(self, request: AgentRequest) -> list[AgentRequest]:
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

    def _execute_loop(
        self,
        state: FinanceAgentState,
        *,
        seen: set[str],
        total_calls: int,
        total_replans: int,
    ) -> tuple[int, int]:
        required = set(
            _PRE_REQUIRED_CAPABILITIES
            if state.request.mode == "PRE_PURCHASE"
            else _SCENARIO_REQUIRED_CAPABILITIES
        )
        while total_calls < self.max_tool_calls:
            satisfied = _satisfied_capabilities(state)
            if _scenario_verdict(state) == "reject" and not state.base_state_violated:
                required.add("amount_adjustment_validation")
            missing = tuple(sorted(required - satisfied))
            planner_tools = frozenset().union(*(_CAPABILITY_TOOLS[name] for name in missing))
            if not planner_tools:
                planner_tools = self.registry.names_for(state.request.mode)
            try:
                action = self.planner.decide(
                    request=state.request,
                    allowed_tools=planner_tools,
                    observations=tuple(state.observations),
                    missing_capabilities=missing,
                )
            except FinancePlannerContractViolation as exc:
                # 모델이 계약을 어겼다 — **되물어 볼 가치가 있다.** 왜 반려됐는지를
                # GUARD 로 남기면 다음 호출의 프롬프트에 그대로 들어간다.
                total_replans = self._guard_replan(
                    state,
                    total_replans,
                    {"rejected_action": _short_reason(str(exc)), "unresolved": list(missing)},
                )
                continue
            except FinancePlannerFailure:
                raise
            except Exception as exc:
                # Provider 장애·네트워크·구조화 출력 파싱 불가 — 다시 물어도 같다.
                raise FinancePlannerFailure(str(exc)) from exc
            if action.finalize:
                if not missing:
                    return total_calls, total_replans
                total_replans = self._guard_replan(
                    state, total_replans, {"unresolved": list(missing)}
                )
                continue
            if action.tool_name is None:
                raise RuntimeError("planner returned neither a tool nor finalize")
            if action.tool_name not in planner_tools:
                total_replans = self._guard_replan(
                    state,
                    total_replans,
                    {"rejected_tool": action.tool_name, "unresolved": list(missing)},
                )
                continue
            signature = json.dumps(
                [state.branch_id, action.tool_name, action.arguments],
                sort_keys=True,
                default=str,
            )
            if signature in seen:
                raise RuntimeError("duplicate unresolved Finance tool call blocked")
            seen.add(signature)
            arguments = self._source_owned_arguments(action, state)
            observation = self.registry.execute(action.tool_name, arguments, state)
            total_calls += 1
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
        raise RuntimeError("Finance tool call limit exceeded")

    def _guard_replan(
        self, state: FinanceAgentState, total_replans: int, detail: dict[str, Any]
    ) -> int:
        if total_replans >= self.max_replans:
            # 되묻기에는 상한이 있다. 넘으면 최종 실패다 — 계약 위반을 무한히 숨기지
            # 않는다. `FinancePlannerFailure` 로 올려 이력에 FALLBACK 으로 남긴다.
            raise FinancePlannerFailure(
                "required Finance capability planning did not complete"
            )
        state.replans += 1
        state.observations.append(
            {"branch_id": state.branch_id, "type": "GUARD", **detail}
        )
        return total_replans + 1

    def _source_owned_arguments(
        self, action: ToolAction, state: FinanceAgentState
    ) -> dict[str, Any]:
        if action.tool_name != "validate_amount_adjustment":
            return action.arguments
        if action.arguments.get("axis", "amount") != "amount":
            raise ValueError("Finance may adjust only the amount axis")
        source_amount = next(
            (
                state.request.payload[key]
                for key in ("candidate_amount_krw", "proposed_amount_krw")
                if state.request.payload.get(key) is not None
            ),
            state.scenario_cap,
        )
        if source_amount is None:
            raise FinanceDataNotReady("amount_adjustment_source")
        return {"axis": "amount", "candidate_amount_krw": source_amount}

    def _finalize(
        self, request: AgentRequest, states: list[FinanceAgentState], runtime_status: str
    ) -> tuple[dict[str, Any], list[Evidence], str, list[SuggestedAdjustment]]:
        if runtime_status != "READY":
            return {}, [], "skipped", []
        if request.mode == "SCENARIO_VALIDATION":
            results = [self._scenario_result(state) for state in states]
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
                if key not in {"evidence", "rules"}:
                    payload[key] = _json_value(value)
            evidences.extend(result.get("evidence", []))
        evidence_by_claim = {item.claim: item for item in evidences}
        return payload, list(evidence_by_claim.values()), "ok", []

    def _scenario_result(self, state: FinanceAgentState) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        evidence: list[Evidence] = []
        for observation in state.observations:
            result = observation.get("result", {})
            for key, value in result.items():
                if key not in {"evidence", "rules"}:
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

    @staticmethod
    def _fallback_reasoning(mode: str, business: str) -> str:
        if mode == "PRE_PURCHASE":
            return _FINAL_EXPLANATIONS["PRE_BOUNDARY"]
        if business == "reject":
            return _FINAL_EXPLANATIONS["SCENARIO_REJECT"]
        return _FINAL_EXPLANATIONS["SCENARIO_ACCEPT"]


def _short_reason(reason: str) -> str:
    return " ".join(reason.split())[:160]


def _validate_ready_reasoning(reasoning: str) -> None:
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", reasoning.strip()) if part]
    if not reasoning.strip() or len(sentences) > 3:
        raise ValueError("Finance reasoning must contain one to three sentences")
    if re.search(r"\d", reasoning):
        raise ValueError("Finance reasoning must not introduce numeric claims")


def _validate_finance_payload(request: AgentRequest) -> None:
    if request.mode != "SCENARIO_VALIDATION":
        return
    raw_scenarios = request.payload.get("scenarios")
    scenarios = raw_scenarios if raw_scenarios is not None else [request.payload]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        raise ValueError("SCENARIO_VALIDATION requires one to three scenarios")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise TypeError("each Finance scenario must be an object")
        scenario_id = _scenario_identity(scenario)
        if scenario_id in scenario_ids:
            raise ValueError("scenario_id must be unique within the request")
        scenario_ids.add(scenario_id)
        try:
            amount = Decimal(str(scenario["total_amount_krw"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("total_amount_krw must be a valid number") from exc
        if (
            isinstance(scenario.get("total_amount_krw"), bool)
            or not amount.is_finite()
            or amount <= 0
        ):
            raise ValueError("total_amount_krw must be a positive finite number")
        schedule = scenario.get("payment_schedule")
        if schedule is None:
            continue
        if not isinstance(schedule, list) or not schedule:
            raise ValueError("payment_schedule must be a non-empty list")
        total = Decimal(0)
        for payment in schedule:
            if not isinstance(payment, dict):
                raise TypeError("each payment_schedule entry must be an object")
            try:
                date.fromisoformat(str(payment["payment_date"]))
                payment_amount = Decimal(str(payment["amount_krw"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("payment_schedule date and amount must be valid") from exc
            if (
                isinstance(payment.get("amount_krw"), bool)
                or not payment_amount.is_finite()
                or payment_amount <= 0
            ):
                raise ValueError("payment_schedule amount must be positive and finite")
            total += payment_amount
        if total != amount:
            raise ValueError("payment_schedule amount sum must equal total_amount_krw")


def _scenario_identity(scenario: dict[str, Any]) -> str:
    """scenario_id가 없으면 Purchase가 보장하는 non-empty label을 identity로 사용한다."""
    if "scenario_id" in scenario:
        scenario_id = scenario["scenario_id"]
        if isinstance(scenario_id, str) and scenario_id.strip():
            return scenario_id.strip()
        raise ValueError("scenario_id must be a non-empty string when present")
    label = scenario.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    raise ValueError("label must be a non-empty string when scenario_id is absent")


def validate_finance_scenario_output(reply: AgentReply) -> tuple[str, ...]:
    """공통 Envelope를 넘어 Finance가 소유한 중첩 시나리오 계보를 검증한다."""
    if reply.runtime_status != "READY" or reply.mode != "SCENARIO_VALIDATION":
        return ()
    scenarios = reply.payload.get("verdicts")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        return ("payload.verdicts must contain one to three results",)
    # 유지하는 단일 시나리오 호환 형식은 branch Evidence를 공통 Envelope 수준에 둔다.
    # 문서화된 복수 시나리오 계약은 이를 중첩한다.
    if reply.payload.get("scenario_id") is not None and len(scenarios) == 1:
        return ()
    findings: list[str] = []
    seen: set[str] = set()
    nested_adjustment_refs: set[str] = set()
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id") if isinstance(scenario, dict) else None
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen:
            findings.append("scenario result ids must be non-empty and unique")
            continue
        seen.add(scenario_id)
        if scenario.get("adjustability") not in {"NOT_NEEDED", "ADJUSTABLE", "NOT_ADJUSTABLE"}:
            findings.append(f"{scenario_id}: invalid adjustability")
        evidence = scenario.get("evidences")
        claims = {item.get("claim") for item in evidence} if isinstance(evidence, list) else set()
        required = {
            "finance_cap_amount_krw",
            "scenario_projected_cash_min",
            "payment_schedule",
            "verdict",
            "adjustability",
        }
        if not required <= claims:
            findings.append(f"{scenario_id}: nested Evidence is incomplete")
        for item in evidence if isinstance(evidence, list) else ():
            for ref in item.get("ref_ids", ()):
                if str(ref).startswith("FIN-AGENT:") and scenario_id not in str(ref):
                    findings.append(f"{scenario_id}: cross-branch Evidence ref")
        adjustments = scenario.get("suggested_adjustments", [])
        if scenario.get("adjustability") == "ADJUSTABLE" and not adjustments:
            findings.append(f"{scenario_id}: verified adjustment is missing")
        if scenario.get("adjustability") != "ADJUSTABLE" and adjustments:
            findings.append(f"{scenario_id}: unexpected adjustment")
        for adjustment in adjustments:
            refs = adjustment.get("ref_ids", ())
            if (
                adjustment.get("axis") != "amount"
                or not refs
                or not all(scenario_id in str(ref) for ref in refs)
            ):
                findings.append(f"{scenario_id}: adjustment lineage is invalid")
            nested_adjustment_refs.update(str(ref) for ref in refs)
        if adjustments and scenario.get("verdict") == "ok":
            findings.append(f"{scenario_id}: adjustment must not rewrite reject to ok")
    top_refs = {
        str(ref)
        for adjustment in reply.suggested_adjustments
        for ref in adjustment.ref_ids
    }
    if top_refs != nested_adjustment_refs:
        findings.append("top-level and nested Finance adjustments differ")
    return tuple(dict.fromkeys(findings))
