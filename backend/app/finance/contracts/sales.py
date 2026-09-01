"""판매 Cycle 계약 — 채널 정산 조건과 회수 우선도."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.finance.contracts.numeric_guards import _reject_boolean
from app.finance.contracts.procurement import ApprovedPurchaseCommitment
from app.finance.contracts.vocabulary import CashPriority, FinalVerdict, RuntimeStatus
from app.finance.llm.schemas import LLMResponseFields


class ChannelTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_type: str = Field(min_length=1)
    partner_id: str = Field(min_length=1)
    settlement_days: int = Field(ge=0)

    @field_validator("settlement_days", mode="before")
    @classmethod
    def reject_boolean_days(cls, value: object) -> object:
        return _reject_boolean(value)


class FinanceSalesRequest(BaseModel):
    """Finance B가 받는 승인 매입 Overlay와 판매 채널 조건."""

    model_config = ConfigDict(extra="forbid")

    cycle: Literal["SALES"]
    as_of: date
    approved_purchase: ApprovedPurchaseCommitment
    channel_terms: list[ChannelTerm] = Field(min_length=1)


class CollectionPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_type: str
    partner_id: str
    settlement_days: int = Field(ge=0)
    liquidity_rank: int = Field(ge=1)


class FinanceSalesResponse(LLMResponseFields):
    """Finance B의 공통 회수 우선도 및 정산 조건 응답."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["finance"] = "finance"
    cycle: Literal["SALES"] = "SALES"
    snapshot_id: str | None
    approval_id: str
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    base_cash_priority: CashPriority | None
    sales_cash_priority: CashPriority | None
    collection_preferences: list[CollectionPreference]
    hard_constraints: list[str]
    soft_warnings: list[str]
