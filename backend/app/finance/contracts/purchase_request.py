"""매입 제안 입력 계약 — Purchase 가 재무에 제출하는 사실."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.finance.contracts.numeric_guards import _reject_boolean
from app.purchase_agent.schemas import PurchaseProposal


class SplitPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    date: date
    quantity_kg: Decimal = Field(gt=0)

    @field_validator("seq", "quantity_kg", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SourcingPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1)
    grade: str = Field(min_length=1)
    quantity_kg: Decimal = Field(gt=0)
    unit_price: int = Field(gt=0)

    @field_validator("quantity_kg", "unit_price", mode="before")
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
    total_quantity_kg: Decimal = Field(gt=0)
    max_price: int = Field(ge=0)
    timing: str = Field(min_length=1)
    split_plan: list[SplitPlanItem]
    sourcing_plan: list[SourcingPlanItem] = Field(min_length=1)
    expected_margin_rate: float = Field(ge=0, le=1)
    expected_cost: int = Field(ge=0)
    rationale: list[Evidence]
    risks: list[str]

    @field_validator(
        "total_quantity_kg",
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
        split_quantity = sum((item.quantity_kg for item in self.split_plan), start=Decimal(0))
        sourcing_quantity = sum((item.quantity_kg for item in self.sourcing_plan), start=Decimal(0))
        if self.total_quantity_kg != split_quantity:
            raise ValueError("total_quantity_kg must equal split_plan quantity total")
        if self.total_quantity_kg != sourcing_quantity:
            raise ValueError("total_quantity_kg must equal sourcing_plan quantity total")
        return self


class SuggestedAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: Literal["amount"]
    description: str = Field(min_length=1)
    evidences: list[Evidence]


PurchaseAgentOutput = PurchaseProposal
