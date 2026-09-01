"""Planner Tool 선택 루프.

이 파일이 소유하는 것
    남은 capability 계산 → 허용 Tool 산출 → Planner 호출 → Tool 실행
    Observation 누적 · bounded 재계획 · 반려 사유 되먹임 · 분기 요청 생성

여기 **없는 것**
    무엇이 옳은 재무 답인가 — Planner 는 **고를 뿐** 계산하지 않는다.
    계산·판정은 `capabilities` 아래 결정론 코드가 한다.
"""

from __future__ import annotations

import json
from dataclasses import replace

from app.finance.application.guards import (
    _scenario_identity,
    _short_reason,
    guard_replan,
    source_owned_arguments,
)
from app.finance.llm.contracts import (
    FinancePlanner,
    FinancePlannerContractViolation,
    FinancePlannerFailure,
)
from app.finance.state import (
    _CAPABILITY_TOOLS,
    _PRE_REQUIRED_CAPABILITIES,
    _SCENARIO_REQUIRED_CAPABILITIES,
    FinanceAgentState,
    _satisfied_capabilities,
    _scenario_verdict,
)
from app.finance.tool_registry import FinanceToolRegistry
from app.master.envelope import AgentRequest


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
    registry: FinanceToolRegistry,
    max_tool_calls: int,
    max_replans: int,
    seen: set[str],
    total_calls: int,
    total_replans: int,
) -> tuple[int, int]:
    """한 분기의 Tool 선택 루프. 상한 안에서만 되묻는다."""
    required = set(
        _PRE_REQUIRED_CAPABILITIES
        if state.request.mode == "PRE_PURCHASE"
        else _SCENARIO_REQUIRED_CAPABILITIES
    )
    while total_calls < max_tool_calls:
        satisfied = _satisfied_capabilities(state)
        if _scenario_verdict(state) == "reject" and not state.base_state_violated:
            required.add("amount_adjustment_validation")
        missing = tuple(sorted(required - satisfied))
        planner_tools = frozenset().union(*(_CAPABILITY_TOOLS[name] for name in missing))
        if not planner_tools:
            planner_tools = registry.names_for(state.request.mode)
        try:
            action = planner.decide(
                request=state.request,
                allowed_tools=planner_tools,
                observations=tuple(state.observations),
                missing_capabilities=missing,
            )
        except FinancePlannerContractViolation as exc:
            # 모델이 계약을 어겼다 — **되물어 볼 가치가 있다.** 왜 반려됐는지를
            # GUARD 로 남기면 다음 호출의 프롬프트에 그대로 들어간다.
            total_replans = guard_replan(
                state,
                total_replans,
                {"rejected_action": _short_reason(str(exc)), "unresolved": list(missing)},
                max_replans=max_replans,
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
            total_replans = guard_replan(
                state,
                total_replans,
                {"unresolved": list(missing)},
                max_replans=max_replans,
            )
            continue
        if action.tool_name is None:
            raise RuntimeError("planner returned neither a tool nor finalize")
        if action.tool_name not in planner_tools:
            total_replans = guard_replan(
                state,
                total_replans,
                {"rejected_tool": action.tool_name, "unresolved": list(missing)},
                max_replans=max_replans,
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
        arguments = source_owned_arguments(action, state)
        observation = registry.execute(action.tool_name, arguments, state)
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
