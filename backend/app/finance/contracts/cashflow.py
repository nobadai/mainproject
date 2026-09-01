"""날짜별 현금 사건과 현금흐름 투영 계약."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.finance.contracts.numeric_guards import _reject_boolean
from app.finance.contracts.vocabulary import CashEventDirection, CashEventType


class CashEvent(BaseModel):
    """T0에 확정된 날짜별 현금 유입/유출."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_date: date
    event_type: CashEventType
    amount_krw: Decimal = Field(ge=0)
    direction: CashEventDirection
    ref_id: str = Field(min_length=1)
    source_ref: str | None = None
    schedule_source_ref: str | None = None
    principal_component_krw: Decimal | None = Field(default=None, ge=0)
    interest_component_krw: Decimal | None = Field(default=None, ge=0)

    @field_validator(
        "amount_krw", "principal_component_krw", "interest_component_krw", mode="before"
    )
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        return _reject_boolean(value)


class CashflowPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_date: date
    cash_balance_krw: Decimal


class CashflowProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: date
    horizon_end: date
    projected_cash_by_date: tuple[CashflowPoint, ...]
    projected_cash_min: Decimal
    projected_cash_min_date: date
