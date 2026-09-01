"""Finance capability **디스패처**.

Controller 는 무엇을 부를지 정하고, 여기서는 그 이름을 실제 capability 로 넘긴다.

★ **여기에 업무 로직을 두지 않는다.** 예전에는 이 파일 하나가 Tool 디스패치 · 컨텍스트
  적재 · PRE_PURCHASE · SCENARIO_VALIDATION · 지급 일정 재구성 · BASE/STRESS overlay ·
  Evidence 조립을 전부 들고 있었다 — 어느 규칙이 어디 사는지 보이지 않았고, 같은 규칙을
  두 번 고칠 위험이 컸다. 구현은 `app.finance.capabilities` 로 옮겼다.

★ 아래 재노출은 **호환을 위한 것**이다. `app.finance.tool_registry` 를 통해 들어오던
  기존 import(재무 내부·재무 테스트)를 그대로 살려 둔다.
"""

from __future__ import annotations

from typing import Any

from app.finance.capabilities import pre_purchase as _pre
from app.finance.capabilities import scenario_validation as _scn
from app.finance.capabilities.payment_schedule import (
    _calculate_schedule_cap,
    _payment_row,
    _positive_decimal,
    _reconstructed_payment,
    _scenario_schedule,
    _schedule_events,
)
from app.finance.capabilities.runtime_context import load_context
from app.finance.llm.contracts import FinanceMode
from app.finance.repository import FinanceAsOfDataPort
from app.finance.state import FinanceAgentState

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

__all__ = [
    "PRE_PURCHASE_TOOLS",
    "SCENARIO_VALIDATION_TOOLS",
    "FinanceToolRegistry",
    "_calculate_schedule_cap",
    "_payment_row",
    "_positive_decimal",
    "_reconstructed_payment",
    "_scenario_schedule",
    "_schedule_events",
    "load_context",
]


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
