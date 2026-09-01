"""한 run 동안 고정되는 Finance T0 상태 계약.

`FinanceRuntimeContext` 와 `FinanceSnapshot` 은 서로를 참조한다 — **같은 모듈에
둔다.** 나누면 forward reference 해석을 위한 `model_rebuild()` 가 필요해지고,
그 호출을 빠뜨린 임포트 순서에서만 터지는 결함이 생긴다.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.finance.contracts.cashflow import CashEvent
from app.finance.contracts.numeric_guards import _reject_boolean
from app.finance.contracts.policy import FinanceDebtPolicy, FinancePolicy


class FinanceRuntimeContext(BaseModel):
    """DB read 이후 한 run 동안 고정되는 Finance T0 입력."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: "FinanceSnapshot"
    policy: FinancePolicy
    debt_policy: FinanceDebtPolicy | None = None
    cash_events: tuple[CashEvent, ...]
    unresolved_sources: tuple[str, ...] = ()


class FinanceSnapshot(BaseModel):
    """한 Cycle 동안 고정해서 사용하는 T0 Finance Snapshot."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str | None
    finance_state_id: str = Field(min_length=1)
    sim_run_id: str = Field(min_length=1)
    state_date: date
    state_type: str = Field(min_length=1)
    financing_mode: str = Field(min_length=1)
    current_cash_krw: Decimal
    minimum_operating_cash_krw: Decimal
    committed_outflows_krw: Decimal
    unsettled_purchase_payables_krw: Decimal
    receivables_krw: Decimal = Decimal(0)
    #: 부채는 음수일 수 없다. 음수는 **"빚 없음"으로 오독되어** 부채 정책 검증과 상환
    #: 일정을 통째로 건너뛰게 한다 — 잘못된 상태가 정상 응답으로 둔갑한다.
    #: 원천 행 검증(`repository._reject_negative_debt`)과 **함께** 쓰는 이중 방어다.
    current_debt_krw: Decimal = Field(default=Decimal(0), ge=0)
    financial_limit_krw: Decimal

    @field_validator(
        "current_cash_krw",
        "minimum_operating_cash_krw",
        "committed_outflows_krw",
        "unsettled_purchase_payables_krw",
        "receivables_krw",
        "current_debt_krw",
        "financial_limit_krw",
        mode="before",
    )
    @classmethod
    def reject_boolean_amounts(cls, value: object) -> object:
        return _reject_boolean(value)
