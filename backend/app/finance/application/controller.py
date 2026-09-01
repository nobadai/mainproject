"""Finance Agent 수명주기.

이 파일이 소유하는 것
    준비/검증 → 분기 실행 → 업무 결과 확정 → 설명 → 메타데이터 → 회신 → 이력
    의 **순서**와 그 사이를 오가는 값

여기 **없는 것**
    Tool 선택 루프 (`planner_loop`) · 계약/상한 guard (`guards`)
    업무 결과 조립 (`finalization`) · 금액 공식 (`domain`) · Provider HTTP (`llm`)

★ 순서가 곧 계약이다. 설명은 결과가 확정된 뒤에만 만들 수 있고, 이력은 회신이
  확정된 뒤에 남는다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import uuid4

from app.finance import run_repository
from app.finance.application import finalization, guards, planner_loop
from app.finance.evidence import _evidence_from_dict
from app.finance.execution import _finance_dept_meta
from app.finance.llm.contracts import (
    FinanceFinalizer,
    FinancePlanner,
    FinancePlannerFailure,
)
from app.finance.llm.finalizer import DeterministicFinanceFinalizer
from app.finance.llm.planner import DeterministicFinancePlanner
from app.finance.llm.provider import _configured_finance_llms
from app.finance.repository import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.state import FinanceAgentState
from app.finance.tool_registry import FinanceToolRegistry
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_REPLANS = 2


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
        payload, evidences, business_status, adjustments = finalization.build_business_result(
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
            guards._validate_finance_payload(request)
            for branch_request in planner_loop.branch_requests(request):
                branch_id = str(branch_request.payload.get("scenario_id", "PRE_PURCHASE"))
                state = FinanceAgentState(
                    branch_request,
                    branch_id=branch_id,
                    context_cache=shared_context,
                )
                # ★ 루프 **전에** 담는다. 실패해도 그때까지의 observation 과 재계획
                #   횟수가 이력에 남아야 한다 — 실패한 실행일수록 흔적이 필요하다.
                outcome.states.append(state)
                total_calls, total_replans = planner_loop.execute_loop(
                        state,
                        planner=self.planner,
                        registry=self.registry,
                        max_tool_calls=self.max_tool_calls,
                        max_replans=self.max_replans,
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
            guards._validate_ready_reasoning(reasoning)
        except Exception:  # noqa: BLE001 - complete Evidence permits safe fallback.
            # ★ 답은 나간다 — 규칙이 만든 답이다. 검증된 Evidence 가 이미 있으므로
            #   설명을 못 골랐다고 업무 결과를 버릴 이유가 없다.
            return _Explanation(
                finalization.fallback_reasoning(request.mode, business_status),
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
        nested_findings = guards.validate_finance_scenario_output(reply)
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
            reasoning="재무 시나리오 산출이 계약 검증을 통과하지 못했습니다.",
            needs_followup=True,
        )

    @staticmethod
    def _persisted(
        request: AgentRequest, reply: AgentReply, metadata: ExecutionMetadata
    ) -> AgentReply:
        """이력을 남긴다. **정상 완료에는 해석 가능한 run_id 가 반드시 있어야 한다.**"""
        try:
            run_repository.save_finance_execution(
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
                reasoning="재무 실행이력을 저장하지 못했습니다.",
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
