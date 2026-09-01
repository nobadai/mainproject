"""Planner/Finalizer 계약과 출력 검증.

★ **Provider 마다 Tool 허용 수준이 달라지면 안 된다.** 스키마(`_planner_response_schema`)
  가 1차 방어이고 `_validate_planner_action` 이 2차다 — 구조화 출력을 무시하는 모델이
  있고, 그때 걸러야 할 곳은 우리 쪽이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.master.envelope import AgentRequest
from app.orchestrator.contracts_core import Evidence

FinanceMode = Literal["PRE_PURCHASE", "SCENARIO_VALIDATION"]


def _planner_response_schema(
    allowed_tools: frozenset[str], *, planning_required: bool
) -> dict[str, Any]:
    """이번 호출에서 Planner 가 낼 수 있는 형태를 그대로 스키마로 만든다.

    ★ **Provider 마다 Tool 허용 수준이 달라지면 안 된다.** Ollama 는 ``format`` 으로
      enum 을 강제하는데 Gemini 쪽만 자유 문자열이면, 같은 재무 판단이 Provider 에
      따라 다른 Tool 을 부를 수 있게 열린다. 두 Planner 가 같은 스키마를 쓴다.

    ★ 스키마는 **1차 방어**일 뿐이다. `_validate_planner_action` 사후 검증을 대체하지
      않는다 — 구조화 출력을 무시하는 모델이 있고, 그때 걸러야 할 곳은 우리 쪽이다.
    """
    return {
        "type": "object",
        "properties": {
            "tool_name": (
                {"type": "string", "enum": sorted(allowed_tools)}
                if planning_required
                else {"type": "null"}
            ),
            "arguments": {
                "type": "object",
                "properties": {
                    "axis": {"type": "string"},
                    "candidate_amount_krw": {"type": "number"},
                },
            },
            "reason": {"type": "string"},
            "finalize": {"type": "boolean", "enum": [not planning_required]},
        },
        "required": ["tool_name", "arguments", "reason", "finalize"],
    }


def _gemini_planner_response_schema(
    allowed_tools: frozenset[str], *, planning_required: bool
) -> dict[str, Any]:
    """같은 계약을 **Gemini 가 받아 주는 표현으로** 낮춘 스키마.

    🔴 엄격 스키마를 그대로 보내면 Gemini 가 **HTTP 400** 을 낸다. 재무 Planner 가 매
       호출 실패하고, 그것이 `FinancePlannerFailure` → Finance ERROR → 마스터
       `E4_NOT_STARTED` 로 이어졌다 — 재무가 아니라 **전송 형식**이 문제였다.

       Gemini responseSchema 는 OpenAPI 3.0 부분집합이라 두 가지를 못 받는다.
         · `enum` 은 STRING 에만 붙는다 → `finalize: {boolean, enum:[false]}` 가 400
         · 타입은 STRING/NUMBER/INTEGER/BOOLEAN/ARRAY/OBJECT 뿐 → `{"type": "null"}`
           도 400. finalize 국면의 `tool_name` 이 그 모양이었다.

    ★ **계약을 낮추는 것이 아니라 표현만 낮춘다.** 전송 스키마에서 빠진 강제는
      `_validate_planner_action` 이 그대로 잡는다 (사후 검증은 원래 2차 방어이고,
      구조화 출력을 무시하는 모델 때문에 어차피 필요하다).

        missing capability 있음 → finalize=False, 허용 Tool 중 정확히 하나
        missing capability 없음 → finalize=True, tool_name=None

    ★ 엄격 스키마에서 **파생**시킨다. 두 벌로 적으면 allowed_tools enum 이 갈린다.
    """
    schema = _planner_response_schema(allowed_tools, planning_required=planning_required)
    properties = dict(schema["properties"])
    properties["finalize"] = {"type": "boolean"}
    if not planning_required:
        # 값은 null 이어야 하지만, 그 강제는 사후 검증이 한다.
        properties["tool_name"] = {"type": "string", "nullable": True}
    return {**schema, "properties": properties}


_PLANNER_SYSTEM_PROMPT = (
    "You plan Finance capability calls. Select only an allowed tool. "
    "Never calculate or invent financial numbers or policy values. "
    "Use observations only. When missing_capabilities is non-empty, you MUST set "
    "finalize=false and select exactly one allowed tool that can satisfy a missing "
    "capability. You may set finalize=true only when missing_capabilities is empty; "
    "then tool_name must be null. For validate_amount_adjustment, copy the observed "
    "deterministic finance_cap_amount_krw exactly and set axis to amount."
)

_TOOL_ARGUMENT_CONTRACTS = {
    "assess_finance_position": {},
    "project_cashflow": {},
    "calculate_purchase_finance_cap": {},
    "analyze_payment_pressure": {},
    "evaluate_purchase_scenario": {},
    "validate_amount_adjustment": {
        "axis": "amount",
        "candidate_amount_krw": (
            "copy the exact finance_cap_amount_krw from a prior observation; "
            "never create a number"
        ),
    },
}


def _planner_prompt(
    *,
    request: AgentRequest,
    allowed_tools: frozenset[str],
    observations: tuple[dict[str, Any], ...],
    missing_capabilities: tuple[str, ...],
) -> dict[str, Any]:
    """두 Planner 가 같은 입력을 본다.

    직전 재계획 사유는 Controller 가 ``observations`` 에 남긴 GUARD 항목에서 뽑는다 —
    Planner 계약에 인자를 더하지 않고도 **왜 반려됐는지**를 모델에게 되돌려준다.
    """
    rejected = [
        {key: value for key, value in observation.items() if key != "branch_id"}
        for observation in observations
        if observation.get("type") == "GUARD"
    ]
    prompt: dict[str, Any] = {
        "mode": request.mode,
        "business_payload": dict(request.payload),
        "allowed_tools": sorted(allowed_tools),
        "observations": observations,
        "missing_capabilities": missing_capabilities,
        "tool_argument_contracts": _TOOL_ARGUMENT_CONTRACTS,
    }
    if rejected:
        prompt["previous_attempts_rejected"] = rejected
    return prompt


@dataclass(frozen=True)
class ToolAction:
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    finalize: bool = False


class FinancePlanner(Protocol):
    model: str
    attempts: int

    def decide(
        self,
        *,
        request: AgentRequest,
        allowed_tools: frozenset[str],
        observations: tuple[dict[str, Any], ...],
        missing_capabilities: tuple[str, ...],
    ) -> ToolAction: ...


class FinanceFinalizer(Protocol):
    model: str
    attempts: int

    def finalize(
        self,
        *,
        mode: FinanceMode,
        business_status: str,
        evidences: tuple[Evidence, ...],
    ) -> str: ...


class FinancePlannerFailure(RuntimeError):
    """되돌릴 수 없는 Planner 실패를 Controller 상태로 전달한다.

    Provider 장애·네트워크 오류·구조화 출력 파싱 불가처럼 **다시 물어도 같은 것**이
    여기로 온다. 모델이 계약을 어긴 것은 `FinancePlannerContractViolation` 이다.
    """


class FinancePlannerContractViolation(ValueError):
    """모델이 계약을 어긴 **회복 가능한** 잘못.

    ★ 이것을 `FinancePlannerFailure` 와 섞으면 재계획이 죽는다. 예전에는 검증 실패가
      `decide()` 안에서 예외로 올라와 Controller 가 통째로 ERROR 로 접었고, 그래서
      `_guard_replan` 은 있으나 마나였다 — `metadata.replans` 는 늘 0 이었다.
      허용되지 않은 Tool 선택 같은 잘못은 **왜 반려됐는지 알려주고 다시 묻는다.**
    """


def _validate_planner_action(
    action: ToolAction,
    allowed_tools: frozenset[str],
    missing_capabilities: tuple[str, ...],
) -> None:
    """Planner 출력 사후 검증 — 스키마를 무시한 모델을 여기서 잡는다.

    전부 `FinancePlannerContractViolation` 으로 올린다. Controller 가 이것만 bounded
    replan 으로 되묻고, 나머지 예외는 즉시 실패로 접는다.
    """
    if not isinstance(action.finalize, bool):
        raise FinancePlannerContractViolation("Finance Planner finalize must be boolean")
    if not isinstance(action.arguments, dict):
        raise FinancePlannerContractViolation("Finance Planner arguments must be an object")
    if missing_capabilities:
        if action.finalize or action.tool_name not in allowed_tools:
            raise FinancePlannerContractViolation(
                "Finance Planner must select one allowed tool while capabilities are missing"
            )
        return
    if not action.finalize or action.tool_name is not None:
        raise FinancePlannerContractViolation(
            "Finance Planner must finalize without a tool when capabilities are complete"
        )
