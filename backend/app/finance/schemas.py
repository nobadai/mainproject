"""Finance P0 요청 및 응답 스키마."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PurchaseMeta(BaseModel):
    """매입 Agent 출력의 ``meta``와 1:1 대응한다."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    item: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    is_refeed: bool
    feedback_attempt: int = Field(ge=0)


class SplitPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    date: date
    quantity_ton: float = Field(gt=0)


class SourcingPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1)
    grade: str = Field(min_length=1)
    quantity_ton: Decimal = Field(gt=0)
    unit_price: int = Field(ge=0)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    claim: str = Field(min_length=1)


class PurchaseScenario(BaseModel):
    """매입 Agent 출력의 단일 ``scenarios`` 원소와 1:1 대응한다."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    total_quantity_ton: float = Field(gt=0)
    max_price: int = Field(ge=0)
    timing: str = Field(min_length=1)
    split_plan: list[SplitPlanItem]
    sourcing_plan: list[SourcingPlanItem]
    expected_margin_rate: float = Field(ge=0, le=1)
    expected_cost: int = Field(ge=0)
    rationale: list[Evidence]
    risks: list[str]


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
    max_feasible_amount_krw: int = Field(ge=0)
    hard_constraints: list[str]
    soft_warnings: list[str]
    reasoning: list[str]
    evidences: list[Evidence]
    suggested_adjustment: SuggestedAdjustment | None
