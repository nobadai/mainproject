"""한 Finance 실행 동안 살아 있는 상태와 capability 판정.

★ capability 는 **결과 키와 실행한 Tool 을 함께** 본다. 결과 키만 보면 정책 출처가
  없어 claim 을 뺀 실행에서 capability 가 영영 안 채워진 것처럼 보여, 답을 못 내는
  것도 아닌데 Planner 를 Tool 호출 상한까지 다시 부른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.finance.evidence import missing_source_name
from app.finance.schemas import CashEvent, FinancePolicy
from app.master.envelope import AgentRequest


@dataclass(frozen=True)
class ScenarioPayment:
    seq: int
    purchase_date: date
    payment_date: date
    qty_kg: Decimal | None
    amount_krw: Decimal
    amount_max_krw: Decimal
    basis: str


@dataclass
class FinanceAgentState:
    request: AgentRequest
    branch_id: str = "PRE_PURCHASE"
    observations: list[dict[str, Any]] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    replans: int = 0
    context_cache: tuple[dict[str, Any], FinancePolicy, list[CashEvent]] | None = None
    projection: Any = None
    scenario_projection: Any = None
    scenario_cap: Decimal | None = None
    scenario_schedule: tuple[ScenarioPayment, ...] = ()
    base_state_violated: bool = False
    missing_sources: list[str] = field(default_factory=list)

    def note_missing_source(self, key: str) -> None:
        """근거를 달지 못해 뺀 정책값을 기록한다. 어댑터 경계와 같은 이름을 쓴다."""
        self.note_missing_source_name(missing_source_name(key))

    def note_missing_source_name(self, name: str) -> None:
        """이미 이름이 정해진 항목을 담는다. 같은 것을 두 번 적지 않는다."""
        if name not in self.missing_sources:
            self.missing_sources.append(name)


_CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    "finance_position": frozenset({"assess_finance_position"}),
    "cashflow_projection": frozenset(
        {"project_cashflow", "calculate_purchase_finance_cap", "analyze_payment_pressure"}
    ),
    "finance_cap": frozenset({"calculate_purchase_finance_cap"}),
    "payment_pressure": frozenset({"analyze_payment_pressure"}),
    "scenario_evaluation": frozenset({"evaluate_purchase_scenario"}),
    "amount_adjustment_validation": frozenset({"validate_amount_adjustment"}),
}

_PRE_REQUIRED_CAPABILITIES = frozenset(
    {"finance_position", "cashflow_projection", "finance_cap", "payment_pressure"}
)
_SCENARIO_REQUIRED_CAPABILITIES = frozenset({"scenario_evaluation"})


def _satisfied_capabilities(state: FinanceAgentState) -> set[str]:
    keys = {
        key
        for observation in state.observations
        for key in observation.get("result", {})
    }
    # ★ **실행한 Tool 로도 센다.** 결과 키만 보면, 정책 출처가 없어 claim 을 뺀 실행에서
    #   capability 가 영영 안 채워진 것처럼 보여 Planner 를 계속 다시 부른다 — 답을
    #   못 내는 것도 아닌데 Tool 호출 상한까지 돌다 죽는다. Tool 이 성공적으로 돌았으면
    #   그 capability 는 조사된 것이다 (`tool_order` 는 성공한 실행만 담는다).
    out: set[str] = {
        capability
        for capability, tools in _CAPABILITY_TOOLS.items()
        if tools & set(state.tool_order)
        and (capability != "finance_cap" or state.request.mode == "PRE_PURCHASE")
    }
    if {"available_cash", "payroll_payment_day"} <= keys:
        out.add("finance_position")
    if "base_projected_cash_min" in keys:
        out.add("cashflow_projection")
    if "finance_cap_amount_krw" in keys and state.request.mode == "PRE_PURCHASE":
        out.add("finance_cap")
    if {"payment_pressure", "critical_payment_dates"} <= keys:
        out.add("payment_pressure")
    if "verdict" in keys:
        out.add("scenario_evaluation")
    if "validation_status" in keys:
        out.add("amount_adjustment_validation")
    return out


def _scenario_verdict(state: FinanceAgentState) -> str | None:
    return next(
        (
            observation["result"]["verdict"]
            for observation in reversed(state.observations)
            if "verdict" in observation.get("result", {})
        ),
        None,
    )
