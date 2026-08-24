"""Finance P0 요청 및 응답 스키마."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _reject_boolean(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid numeric inputs")  # noqa: TRY004
    return value


class PurchaseMeta(BaseModel):
    """매입 Agent 출력의 ``meta``와 1:1 대응한다."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    item: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    is_refeed: bool
    feedback_attempt: int = Field(ge=0)

    @field_validator("feedback_attempt", mode="before")
    @classmethod
    def reject_boolean_feedback_attempt(cls, value: object) -> object:
        return _reject_boolean(value)


class SplitPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    date: date
    quantity_ton: Decimal = Field(gt=0)

    @field_validator("seq", "quantity_ton", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SourcingPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1)
    grade: str = Field(min_length=1)
    quantity_ton: Decimal = Field(gt=0)
    unit_price: int = Field(gt=0)

    @field_validator("quantity_ton", "unit_price", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    claim: str = Field(min_length=1)


class PurchaseScenario(BaseModel):
    """매입 Agent 출력의 단일 ``scenarios`` 원소와 1:1 대응한다."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    total_quantity_ton: Decimal = Field(gt=0)
    max_price: int = Field(ge=0)
    timing: str = Field(min_length=1)
    split_plan: list[SplitPlanItem]
    sourcing_plan: list[SourcingPlanItem] = Field(min_length=1)
    expected_margin_rate: float = Field(ge=0, le=1)
    expected_cost: int = Field(ge=0)
    rationale: list[Evidence]
    risks: list[str]

    @field_validator(
        "total_quantity_ton",
        "max_price",
        "expected_margin_rate",
        "expected_cost",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def validate_quantity_totals(self) -> "PurchaseScenario":
        split_quantity = sum((item.quantity_ton for item in self.split_plan), start=Decimal(0))
        sourcing_quantity = sum(
            (item.quantity_ton for item in self.sourcing_plan), start=Decimal(0)
        )
        if self.total_quantity_ton != split_quantity:
            raise ValueError("total_quantity_ton must equal split_plan quantity total")
        if self.total_quantity_ton != sourcing_quantity:
            raise ValueError("total_quantity_ton must equal sourcing_plan quantity total")
        return self


class FinanceReviewRequest(BaseModel):
    """오케스트레이터가 Finance에 전달하는 단일 시나리오 검토 요청."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    purchase_meta: PurchaseMeta
    scenario: PurchaseScenario


class SuggestedAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: Literal["amount"]
    description: str = Field(min_length=1)
    evidences: list[Evidence]


class FinanceReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    agent: Literal["finance"] = "finance"
    verdict: Literal["ok", "conditional", "reject"]
    max_feasible_amount_krw: Decimal | None = Field(ge=0)
    hard_constraints: list[str]
    soft_warnings: list[str]
    reasoning: list[str]
    evidences: list[Evidence]
    suggested_adjustment: SuggestedAdjustment | None


class PurchaseSourcingPlanItem(BaseModel):
    """Purchase Agent v0.4 sourcing line."""

    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1)
    grade: str = Field(min_length=1)
    quantity_ton: Decimal = Field(gt=0)
    grade_unit_price: int = Field(gt=0)

    @field_validator("quantity_ton", "grade_unit_price", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class PurchaseAgentScenario(BaseModel):
    """Purchase Agent v0.4의 단일 매입 후보."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    strategy_type: str = Field(min_length=1)
    coverage_days: int = Field(gt=0)
    total_quantity_ton: Decimal = Field(gt=0)
    total_amount_krw: Decimal = Field(ge=0)
    split_plan: list[SplitPlanItem] = Field(min_length=1)
    sourcing_plan: list[PurchaseSourcingPlanItem] = Field(min_length=1)

    @field_validator(
        "coverage_days",
        "total_quantity_ton",
        "total_amount_krw",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def validate_quantity_totals(self) -> "PurchaseAgentScenario":
        split_quantity = sum((item.quantity_ton for item in self.split_plan), start=Decimal(0))
        sourcing_quantity = sum(
            (item.quantity_ton for item in self.sourcing_plan), start=Decimal(0)
        )
        if self.total_quantity_ton != split_quantity:
            raise ValueError("total_quantity_ton must equal split_plan quantity total")
        if self.total_quantity_ton != sourcing_quantity:
            raise ValueError("total_quantity_ton must equal sourcing_plan quantity total")
        return self


class PurchaseAgentOutput(BaseModel):
    """Finance A가 받는 Purchase Agent v0.4 전체 출력."""

    model_config = ConfigDict(extra="forbid")

    meta: PurchaseMeta
    scenarios: list[PurchaseAgentScenario] = Field(min_length=1)


RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
CashPriority = Literal["LOW", "MEDIUM", "HIGH"]


class FinanceBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_feasible_amount_krw: Decimal | None = Field(ge=0)
    scope: Literal["ALL_ITEMS_TOTAL"] = "ALL_ITEMS_TOTAL"


class ProcurementSuggestedAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: Literal["amount"] = "amount"
    action: Literal["cap"] = "cap"
    max_amount_krw: Decimal = Field(ge=0)


class FinanceProcurementResponse(BaseModel):
    """Finance A의 전사 매입 가능 금액 Band 응답."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["finance"] = "finance"
    cycle: Literal["PROCUREMENT"] = "PROCUREMENT"
    as_of: date
    snapshot_id: str | None
    policy_version: Literal["PROVISIONAL"] = "PROVISIONAL"
    runtime_status: RuntimeStatus
    band: FinanceBand
    base_projected_cash_min: Decimal | None
    base_cash_priority: CashPriority | None
    hard_constraints: list[str]
    soft_warnings: list[str]
    suggested_adjustment: ProcurementSuggestedAdjustment | None
    evidences: list[Evidence]
