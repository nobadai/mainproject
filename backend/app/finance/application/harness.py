"""Finance Harness — **합법 행동공간을 정하고 강제한다.**

이 파일이 소유하는 것
    capability 소유(1:1)와 선행 의존 계약 · mode 별 필수 capability
    지금 실행 가능한 Tool 집합 · Planner 요청의 승인/반려와 사유
    Tool 예산 · 중복 호출 차단 · Registry 실행 직전 재검증 · 실행 흔적(Trace)
    LangChain Tool 선언(이름 · 설명 · 인자 스키마) · Tool 디스패치(Registry)
    실행 계약 guard (payload 검증 · 재계획 상한 · 인자 원천 · 설명 규율)

여기 **없는 것**
    Finance Cap · 현금흐름 · BASE/STRESS · 압박도 · 판정 · 조정 금액
    → 전부 `capabilities` · `tools` · `rules` 결정론 코드 소유다. Harness 는 숫자를
      **하나도** 만들지 않는다.

★ LLM 의 Tool 호출은 **실행 요청**이지 실행 권한이 아니다. 승인은 여기서 난다.

★ 노출 제한(동적 Tool 노출)은 방어가 아니라 **편의**다. 모델이 못 보게 하는 것과
  못 하게 하는 것은 다르다 — 그래서 실행 직전에 같은 검사를 한 번 더 하고,
  Registry 가 mode 검사를 또 한 번 한다. 층이 겹치는 것은 의도다.

★ 네 조각(capability 정책 · Tool 선언 · 승인 · guard)이 한 파일에 있는 이유는
  **늘 같이 열리기 때문**이다. 하나를 고치면 나머지가 따라 바뀐다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.finance.capabilities import procurement as _pre
from app.finance.capabilities import scenario as _scn
from app.finance.db import FinanceAsOfDataPort, FinanceDataNotReady
from app.finance.execution import _assert_dependency_contract_is_complete
from app.finance.llm.planner import (
    FINALIZE_TOOL_NAME,
    FinancePlannerFailure,
    ToolAction,
)
from app.finance.schemas import FinanceMode
from app.finance.state import FinanceAgentState, _scenario_verdict
from app.master.envelope import AgentReply, AgentRequest

# ---------------------------------------------------------------------------
# capability 소유와 의존 계약
# ---------------------------------------------------------------------------

#: capability → 그 capability 를 **유일하게** 채우는 Tool.
CAPABILITY_OWNER: dict[str, str] = {
    "finance_position": "assess_finance_position",
    "cashflow_projection": "project_cashflow",
    "finance_cap": "calculate_purchase_finance_cap",
    "payment_pressure": "analyze_payment_pressure",
    "scenario_evaluation": "evaluate_purchase_scenario",
    "amount_adjustment_validation": "validate_amount_adjustment",
}

#: Tool 이 실행되기 전에 **이미 채워져 있어야 하는** capability.
#:
#: ★ Tool 이름 순서가 아니라 capability 조건이다. 조건만 맞으면 어느 것을 먼저
#:   골라도 된다 — Planner 의 선택 여지는 여기서 나온다.
TOOL_DEPENDENCIES: dict[str, frozenset[str]] = {
    "assess_finance_position": frozenset(),
    "project_cashflow": frozenset(),
    "calculate_purchase_finance_cap": frozenset({"cashflow_projection"}),
    "analyze_payment_pressure": frozenset({"cashflow_projection"}),
    "evaluate_purchase_scenario": frozenset(),
    "validate_amount_adjustment": frozenset({"scenario_evaluation"}),
}

PRE_REQUIRED_CAPABILITIES = frozenset(
    {"finance_position", "cashflow_projection", "finance_cap", "payment_pressure"}
)
SCENARIO_REQUIRED_CAPABILITIES = frozenset({"scenario_evaluation"})

#: 호환 재노출 — capability 하나가 한 Tool 만 갖는다.
CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    capability: frozenset({tool}) for capability, tool in CAPABILITY_OWNER.items()
}


def required_capabilities(mode: str) -> frozenset[str]:
    return (
        PRE_REQUIRED_CAPABILITIES
        if mode == "PRE_PURCHASE"
        else SCENARIO_REQUIRED_CAPABILITIES
    )


def completed_capabilities(executed_tools: Iterable[str]) -> set[str]:
    """실행된 Tool 이 채운 capability.

    ★ 결과 키를 보지 않는다. 소유가 1:1 이고 선행 실행을 Harness 가 강제하므로
      **실행 = capability** 가 그대로 성립한다. `tool_order` 에는 성공한 실행만 담기니
      실패한 Tool 이 capability 를 채우는 일도 없다.
    """
    executed = set(executed_tools)
    return {
        capability
        for capability, tool in CAPABILITY_OWNER.items()
        if tool in executed
    }


def dependencies_of(tool: str) -> frozenset[str]:
    """★ 계약이 없는 Tool 을 **의존 없음으로 읽지 않는다.**

    빈 집합으로 조용히 넘기면 새 Tool 이 아무 선행 조건 없이 실행 가능해진다 —
    모르는 것이 통과가 되는 구조다.
    """
    if tool not in TOOL_DEPENDENCIES:
        raise KeyError(f"Finance tool has no declared capability dependency: {tool}")
    return TOOL_DEPENDENCIES[tool]


# ---------------------------------------------------------------------------
# LangChain Tool 선언 — 얇은 통로
# ---------------------------------------------------------------------------

class _NoArguments(BaseModel):
    """인자를 받지 않는 Finance Tool. **모델이 숫자를 실을 자리가 없다.**

    ★ `extra="forbid"` 다. 모르는 인자를 조용히 버리면 *"모델이 무엇을 보냈는지"* 가
      기록에서 사라진다 — 값을 안 받는 Tool 에 값이 왔다는 사실 자체가 신호다.
    """

    model_config = ConfigDict(extra="forbid")


class _AmountAdjustmentArguments(BaseModel):
    """유일하게 값을 받는 Tool 의 인자.

    ★ 값은 받되 **쓰지 않는다.** 실제로 실행에 들어가는 금액은
      `guards.source_owned_arguments` 가 원천(payload · 결정론 Tool 결과)에서 다시
      고른다 — 모델이 만든 숫자는 여기서 끝난다.

    ★ `axis` 는 `Literal["amount"]` 다. 재무가 금액 축만 조정한다는 것은 이미
      `source_owned_arguments` 와 capability 양쪽이 막는 불변식이고, 여기서는 그것을
      **모델이 보는 스키마에도** 적어 둘 뿐이다.
    """

    model_config = ConfigDict(extra="forbid")

    axis: Literal["amount"] = Field(
        default="amount", description="Finance may adjust only the amount axis."
    )
    candidate_amount_krw: float | None = Field(
        default=None,
        description=(
            "Copy the exact finance_cap_amount_krw observed from a deterministic "
            "Finance tool. Never create a number."
        ),
    )


_ARGUMENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "assess_finance_position": _NoArguments,
    "project_cashflow": _NoArguments,
    "calculate_purchase_finance_cap": _NoArguments,
    "analyze_payment_pressure": _NoArguments,
    "evaluate_purchase_scenario": _NoArguments,
    "validate_amount_adjustment": _AmountAdjustmentArguments,
}

#: 모델이 읽는 설명. **업무 규칙을 여기 적지 않는다** — 계산과 판정은 결정론 코드가
#: 하고, 여기 있는 것은 *"이 Tool 이 어느 사실을 만든다"* 뿐이다.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "assess_finance_position": (
        "Read the deterministic Finance position: available cash, payroll day and "
        "the Finance policy values that carry evidence."
    ),
    "project_cashflow": (
        "Run the deterministic base cash-flow projection for the policy horizon. "
        "Every Finance capability that needs a projection depends on this."
    ),
    "calculate_purchase_finance_cap": (
        "Derive the deterministic purchase Finance Cap from the base projection."
    ),
    "analyze_payment_pressure": (
        "Derive the deterministic cash-pressure level and the critical payment dates "
        "from the base projection."
    ),
    "evaluate_purchase_scenario": (
        "Evaluate the submitted purchase scenario deterministically: BASE/STRESS "
        "projections, Finance Cap and the Finance verdict."
    ),
    "validate_amount_adjustment": (
        "Validate a source-owned amount alternative against the deterministic "
        "scenario Finance Cap."
    ),
}

_FINALIZE_DESCRIPTION = (
    "Finish the Finance review. Allowed only when no required Finance capability "
    "is missing; it produces no Finance number and no Finance verdict."
)


def finalize_tool() -> BaseTool:
    """capability 가 다 찼을 때만 바인딩되는 종료 Tool."""
    return StructuredTool.from_function(
        func=lambda: FINALIZE_TOOL_NAME,
        name=FINALIZE_TOOL_NAME,
        description=_FINALIZE_DESCRIPTION,
        args_schema=_NoArguments,
    )


def build_tool_adapter(name: str, run: Callable[[str, dict[str, Any]], Any]) -> BaseTool:
    """Finance Tool 하나를 LangChain Tool 로 감싼다.

    `run` 은 Harness 가 준다 — 어댑터는 무엇을 실행할지 정하지 않고, 정해진 통로로
    넘길 뿐이다.
    """
    if name not in _ARGUMENT_SCHEMAS:
        raise KeyError(f"Finance tool has no LangChain adapter contract: {name}")

    def _invoke(**arguments: Any) -> Any:
        return run(name, arguments)

    return StructuredTool.from_function(
        func=_invoke,
        name=name,
        description=_TOOL_DESCRIPTIONS[name],
        args_schema=_ARGUMENT_SCHEMAS[name],
    )


# ---------------------------------------------------------------------------
# Tool 디스패처 — mode 밖 Tool 은 실행하지 않는다
# ---------------------------------------------------------------------------

PRE_PURCHASE_TOOLS = frozenset(
    {
        "assess_finance_position",
        "project_cashflow",
        "calculate_purchase_finance_cap",
        "analyze_payment_pressure",
    }
)
SCENARIO_VALIDATION_TOOLS = frozenset({"evaluate_purchase_scenario", "validate_amount_adjustment"})

#: Tool 이름 → 구현. **이름은 Planner 계약이라 바뀌지 않는다.**
_CAPABILITIES = {
    "assess_finance_position": _pre.assess_finance_position,
    "project_cashflow": _pre.project_cashflow,
    "calculate_purchase_finance_cap": _pre.calculate_purchase_finance_cap,
    "analyze_payment_pressure": _pre.analyze_payment_pressure,
    "evaluate_purchase_scenario": _scn.evaluate_purchase_scenario,
    "validate_amount_adjustment": _scn.validate_amount_adjustment,
}

class FinanceToolRegistry:
    """mode 가 허용하는 capability 만 실행한다."""

    def __init__(self, data_port: FinanceAsOfDataPort):
        self.data_port = data_port

    def names_for(self, mode: FinanceMode) -> frozenset[str]:
        return PRE_PURCHASE_TOOLS if mode == "PRE_PURCHASE" else SCENARIO_VALIDATION_TOOLS

    def execute(
        self, name: str, arguments: dict[str, Any], state: FinanceAgentState
    ) -> dict[str, Any]:
        """★ mode 밖의 Tool 은 실행하지 않는다 — Planner 사후 검증과 겹치는 마지막 방어다."""
        if name not in self.names_for(state.request.mode):
            raise ValueError(f"Tool {name} is not allowed for {state.request.mode}")
        return _CAPABILITIES[name](self.data_port, arguments, state)


# ---------------------------------------------------------------------------
# 실행 계약 guard
# ---------------------------------------------------------------------------

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


def guard_replan(
    state: FinanceAgentState, total_replans: int, detail: dict[str, Any], *, max_replans: int
) -> int:
    if total_replans >= max_replans:
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


def source_owned_arguments(action: ToolAction, state: FinanceAgentState) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Harness — 승인과 강제
# ---------------------------------------------------------------------------

#: 반려 사유. **기술적 사유이지 업무 판정이 아니다** — 여기서 나온 값이 회신의
#: `business_status` 가 되는 일은 없다.
TOOL_NOT_EXECUTABLE = "TOOL_NOT_EXECUTABLE"
DEPENDENCY_NOT_SATISFIED = "DEPENDENCY_NOT_SATISFIED"
TOOL_PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
DUPLICATE_UNRESOLVED_TOOL_CALL = "DUPLICATE_UNRESOLVED_TOOL_CALL"
UNKNOWN_TOOL = "UNKNOWN_TOOL"


class FinanceToolDenied(RuntimeError):
    """Harness 가 Tool 실행을 막았다. **사유를 값으로 들고 다닌다.**"""

    def __init__(self, reason: str, tool: str | None, detail: str = "") -> None:
        self.reason = reason
        self.tool = tool
        super().__init__(detail or f"{reason}: {tool}")


@dataclass(frozen=True)
class CapabilityState:
    """지금 이 분기의 capability 지형. **Planner 입력이자 Trace 항목이다.**"""

    required: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    executable_tools: frozenset[str] = frozenset()
    dependency_status: dict[str, dict[str, Any]] = field(default_factory=dict)


class FinanceHarness:
    """Finance 실행 통제. **계산은 하지 않는다.**

    상한은 한 실행 전체에서 공유한다 — 분기(시나리오)가 늘어난다고 예산이 늘지 않는다.
    """

    def __init__(
        self,
        registry: FinanceToolRegistry,
        *,
        max_tool_calls: int,
        max_replans: int,
    ) -> None:
        self.registry = registry
        self.max_tool_calls = max_tool_calls
        self.max_replans = max_replans
        self.tool_calls = 0
        self.replans = 0
        self.llm_calls = 0
        self.denials: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        self._pending: tuple[FinanceAgentState, CapabilityState] | None = None
        self._adapters: dict[str, BaseTool] = {
            name: build_tool_adapter(name, self._execute_validated)
            for name in CAPABILITY_OWNER.values()
        }
        self._finalize_tool = finalize_tool()

    # ── capability 지형 ─────────────────────────────────────────

    def capability_state(self, state: FinanceAgentState) -> CapabilityState:
        """무엇이 찼고 무엇이 남았고 **지금 무엇을 부를 수 있는가.**"""
        mode = state.request.mode
        required = set(required_capabilities(mode))
        # 반려된 시나리오는 금액 대안 검증까지가 한 벌이다. 이 조건은 업무 규칙을
        # 만드는 것이 아니라 **이미 나온 결정론 판정을 읽는 것**이다.
        if _scenario_verdict(state) == "reject" and not state.base_state_violated:
            required.add("amount_adjustment_validation")
        completed = completed_capabilities(state.tool_order)
        missing = required - completed
        permitted = self.registry.names_for(mode)

        dependency_status: dict[str, dict[str, Any]] = {}
        executable: set[str] = set()
        for capability in sorted(missing):
            tool = CAPABILITY_OWNER[capability]
            if tool not in permitted:
                continue
            unmet = sorted(dependencies_of(tool) - completed)
            dependency_status[tool] = {
                "capability": capability,
                "requires": sorted(dependencies_of(tool)),
                "unmet": unmet,
                "satisfied": not unmet,
            }
            if not unmet:
                executable.add(tool)
        return CapabilityState(
            required=tuple(sorted(required)),
            completed=tuple(sorted(completed)),
            missing=tuple(sorted(missing)),
            executable_tools=frozenset(executable),
            dependency_status=dependency_status,
        )

    def langchain_tools(self, capability_state: CapabilityState) -> tuple[BaseTool, ...]:
        """이번 호출에서 **모델에게 실제로 보여 줄** Tool 객체.

        남은 capability 가 없을 때만 종료 Tool 이 보인다 — 필수 capability 를 건너뛴
        "재무 검토 완료" 는 고를 수 있는 선택지 자체가 아니다.
        """
        if not capability_state.missing:
            return (self._finalize_tool,)
        return tuple(
            self._adapters[name] for name in sorted(capability_state.executable_tools)
        )

    # ── 승인 ───────────────────────────────────────────────────

    def authorize(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        state: FinanceAgentState,
        capability_state: CapabilityState,
    ) -> str:
        """Planner 요청을 검사한다. 통과하면 실행 서명을, 아니면 `FinanceToolDenied`.

        순서에 뜻이 있다. **중복을 먼저 본다** — 같은 요청을 반복하는 Planner 는
        노출 밖 Tool 을 고른 것과 다른 문제이고, 되물어도 같은 답이 온다.
        """
        if tool_name not in self._adapters:
            raise FinanceToolDenied(UNKNOWN_TOOL, tool_name)
        signature = _signature(state.branch_id, tool_name, arguments)
        if signature in self._seen:
            raise FinanceToolDenied(
                DUPLICATE_UNRESOLVED_TOOL_CALL,
                tool_name,
                "duplicate unresolved Finance tool call blocked",
            )
        self._assert_executable(tool_name, state, capability_state)
        if self.tool_calls >= self.max_tool_calls:
            raise FinanceToolDenied(
                TOOL_BUDGET_EXHAUSTED, tool_name, "Finance tool call limit exceeded"
            )
        self._seen.add(signature)
        return signature

    def _assert_executable(
        self,
        tool_name: str,
        state: FinanceAgentState,
        capability_state: CapabilityState,
    ) -> None:
        """mode 권한 · 선행 capability · 필요 여부. **실행 직전에도 이것을 다시 본다.**"""
        if tool_name not in self.registry.names_for(state.request.mode):
            raise FinanceToolDenied(TOOL_PERMISSION_DENIED, tool_name)
        unmet = sorted(dependencies_of(tool_name) - set(capability_state.completed))
        if unmet:
            raise FinanceToolDenied(
                DEPENDENCY_NOT_SATISFIED,
                tool_name,
                f"{tool_name} requires {'; '.join(unmet)}",
            )
        if tool_name not in capability_state.executable_tools:
            raise FinanceToolDenied(TOOL_NOT_EXECUTABLE, tool_name)

    # ── 실행 ───────────────────────────────────────────────────

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        state: FinanceAgentState,
        capability_state: CapabilityState,
    ) -> dict[str, Any]:
        """승인된 Tool 을 **LangChain 어댑터를 통해** 돌린다.

        어댑터가 다시 `_execute_validated` 로 들어오고, 거기서 Harness 검사가 한 번 더
        돈다 — 노출 제한을 뚫고 들어온 호출이 Registry 에 닿지 않게 하는 마지막 문이다.
        """
        self._pending = (state, capability_state)
        try:
            observation = self._adapters[tool_name].invoke(dict(arguments))
        finally:
            self._pending = None
        self.tool_calls += 1
        return observation

    def _execute_validated(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """LangChain 어댑터가 부르는 자리. **Registry 직전의 재검증.**"""
        if self._pending is None:
            raise FinanceToolDenied(
                TOOL_NOT_EXECUTABLE,
                tool_name,
                "Finance tool executed outside Harness control",
            )
        state, capability_state = self._pending
        self._assert_executable(tool_name, state, capability_state)
        return self.registry.execute(tool_name, arguments, state)

    # ── 예산 · 반려 기록 ───────────────────────────────────────

    def count_llm_call(self) -> None:
        self.llm_calls += 1

    def note_denied(self, state: FinanceAgentState, tool: str | None, reason: str) -> None:
        self.denials.append(
            {"branch_id": state.branch_id, "denied_tool": tool, "denied_reason": reason}
        )

    # ── Trace ──────────────────────────────────────────────────

    def record_step(
        self,
        state: FinanceAgentState,
        *,
        capability_state: CapabilityState,
        requested_tool: str | None = None,
        executed_tool: str | None = None,
        denied_tool: str | None = None,
        denied_reason: str | None = None,
        finalize_requested: bool = False,
    ) -> None:
        """**LLM 이 무엇을 요청했고 Harness 가 무엇을 허락했고 무엇이 실제로 돌았는가.**

        셋을 한 항목에 같이 적는다. 따로 적으면 어느 것이 어느 것인지 나중에 붙일 수
        없고, 그때 사라지는 것이 이 계층의 존재 이유다.
        """
        state.trace.append(
            {
                "step": len(state.trace) + 1,
                "branch_id": state.branch_id,
                "completed_capabilities": list(capability_state.completed),
                "missing_capabilities": list(capability_state.missing),
                "executable_tools": sorted(capability_state.executable_tools),
                "dependency_status": capability_state.dependency_status,
                "requested_tool": requested_tool,
                "finalize_requested": finalize_requested,
                # 승인된 선택과 실제 실행을 나눠 둔다. 지금은 승인 직후에 실행하므로
                # 늘 같지만, **어긋나는 날을 읽을 수 있어야** 이 계층이 뜻을 갖는다.
                # 반려된 단계에서는 둘 다 비고 `denied_*` 만 남는다.
                "selected_tool": executed_tool,
                "executed_tool": executed_tool,
                "denied_tool": denied_tool,
                "denied_reason": denied_reason,
                "tool_calls": self.tool_calls,
                "llm_calls": self.llm_calls,
                "replans": self.replans,
            }
        )


def _signature(branch_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps([branch_id, tool_name, arguments], sort_keys=True, default=str)


# ★ 기동 시점 확인. Tool 을 새로 만들고 입력 계보를 안 적으면 **import 실패**로 즉시
#   드러난다 — 조용한 `inputs_used` 누락은 Critic 검사를 통과로 바꾼다.
_assert_dependency_contract_is_complete(PRE_PURCHASE_TOOLS)
