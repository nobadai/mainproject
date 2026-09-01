"""매입 Cycle 응답 계약 — 가능 금액 Band · 금액 조정 · 승인된 지급 의무."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.finance.contracts.numeric_guards import _reject_boolean
from app.finance.contracts.purchase_request import Evidence
from app.finance.contracts.vocabulary import CashPriority, FinalVerdict, RuntimeStatus
from app.finance.llm.schemas import LLMResponseFields


class FinanceBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_feasible_amount_krw: Decimal | None = Field(ge=0)
    scope: Literal["ALL_ITEMS_TOTAL"] = "ALL_ITEMS_TOTAL"


class ProcurementSuggestedAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: Literal["amount"] = "amount"
    action: Literal["cap"] = "cap"
    max_amount_krw: Decimal = Field(ge=0)


class FinanceProcurementResponse(LLMResponseFields):
    """Finance A의 전사 매입 가능 금액 Band 응답."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["finance"] = "finance"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None
    policy_version: Literal["v1.3-PROVISIONAL"] = "v1.3-PROVISIONAL"
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    band: FinanceBand
    base_projected_cash_min: Decimal | None
    base_cash_priority: CashPriority | None
    hard_constraints: list[str]
    soft_warnings: list[str]
    suggested_adjustment: ProcurementSuggestedAdjustment | None
    evidences: list[Evidence]


class ApprovedPurchaseCommitment(BaseModel):
    """H1에서 승인된 매입 지급 의무."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)
    total_amount_krw: Decimal = Field(gt=0)
    payment_date: date

    @field_validator("total_amount_krw", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        return _reject_boolean(value)
