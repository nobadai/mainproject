"""한 Finance 실행 동안 **살아 있는 값**.

여기 없는 것: capability 소유·의존 같은 정적 계약(`capability_graph`)과, 그것을
실행 시점에 강제하는 통제(`application.harness`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.finance.execution import missing_source_name
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
    #: Harness 가 남기는 실행 흔적. **관측이지 업무 결과가 아니다** — 회신
    #: payload 로 올라가지 않고 실행 metadata 로만 나간다.
    trace: list[dict[str, Any]] = field(default_factory=list)

    def note_missing_source(self, key: str) -> None:
        """근거를 달지 못해 뺀 정책값을 기록한다. 어댑터 경계와 같은 이름을 쓴다."""
        self.note_missing_source_name(missing_source_name(key))

    def note_missing_source_name(self, name: str) -> None:
        """이미 이름이 정해진 항목을 담는다. 같은 것을 두 번 적지 않는다."""
        if name not in self.missing_sources:
            self.missing_sources.append(name)


def _scenario_verdict(state: FinanceAgentState) -> str | None:
    return next(
        (
            observation["result"]["verdict"]
            for observation in reversed(state.observations)
            if "verdict" in observation.get("result", {})
        ),
        None,
    )
