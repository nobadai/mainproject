"""Finance P0 요청 및 응답 스키마."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.finance.llm.schemas import LLMResponseFields
from app.purchase_agent.schemas import PurchaseProposal

FinalVerdict = Literal["PASS", "REVIEW_REQUIRED", "FAIL"]


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
    verdict: FinalVerdict
    max_feasible_amount_krw: Decimal | None = Field(ge=0)
    hard_constraints: list[str]
    soft_warnings: list[str]
    reasoning: list[str]
    evidences: list[Evidence]
    suggested_adjustment: SuggestedAdjustment | None


PurchaseAgentOutput = PurchaseProposal


RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
CashPriority = Literal["LOW", "MEDIUM", "HIGH"]
FinanceCycle = Literal["PROCUREMENT", "SALES"]


class FinancePolicy(BaseModel):
    """Finance MVP 실행에 사용하는 회사/Agent 운영 정책."""

    model_config = ConfigDict(extra="forbid")

    purchase_payment_days: int = Field(ge=0)
    payroll_date: int = Field(ge=1, le=31)
    monthly_labor_cost_krw: Decimal = Field(ge=0)
    minimum_cash_balance_krw: Decimal = Field(ge=0)
    cashflow_projection_days: int = Field(gt=0)
    cash_priority_reference: Literal["minimum_cash_balance_krw"]
    cash_priority_high_ratio: Decimal = Field(ge=0)
    cash_priority_medium_ratio: Decimal = Field(ge=0)
    policy_version: Literal["v1.3-PROVISIONAL"]
    usage_scope: Literal["AGENT_MVP_DEMO"]
    source_refs: dict[str, str]

    @field_validator(
        "purchase_payment_days",
        "payroll_date",
        "monthly_labor_cost_krw",
        "minimum_cash_balance_krw",
        "cashflow_projection_days",
        "cash_priority_high_ratio",
        "cash_priority_medium_ratio",
        mode="before",
    )
    @classmethod
    def reject_boolean_policy_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class FinanceDebtPolicy(BaseModel):
    """AGENT_MVP_DEMO 전용 SIM_FIXED 실행 대출 계약."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    debt_runtime_status: Literal["SIM_FIXED_EXECUTED"]
    debt_principal_krw: Decimal = Field(gt=0)
    debt_execution_date: date
    debt_annual_rate: Decimal = Field(gt=0)
    debt_term_months: int = Field(gt=0)
    debt_grace_months: int = Field(ge=0)
    debt_grace_payment_mode: Literal["INTEREST_ONLY"]
    debt_repayment_method: Literal["EQUAL_PRINCIPAL_AFTER_GRACE"]
    debt_payment_frequency: Literal["MONTHLY"]
    debt_payment_day_rule: Literal["MONTH_END"]
    debt_first_payment_rule: Literal["EXECUTION_MONTH_END"]
    debt_interest_method: Literal["OUTSTANDING_PRINCIPAL_ANNUAL_RATE_DIV_12"]
    policy_version: Literal["v1.3-PROVISIONAL"]
    usage_scope: Literal["AGENT_MVP_DEMO"]
    source_refs: dict[str, str]

    @field_validator(
        "debt_principal_krw",
        "debt_annual_rate",
        "debt_term_months",
        "debt_grace_months",
        mode="before",
    )
    @classmethod
    def reject_boolean_debt_numbers(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def validate_repayment_period(self) -> "FinanceDebtPolicy":
        if self.debt_grace_months >= self.debt_term_months:
            raise ValueError("debt_grace_months must be less than debt_term_months")
        return self


CashEventDirection = Literal["INFLOW", "OUTFLOW"]
CashEventType = Literal[
    "PURCHASE_PAYABLE",
    "COMMITTED_OUTFLOW",
    "RECEIVABLE",
    "PAYROLL",
    "DEBT_SERVICE",
    "EXTRA_PURCHASE",
    "H1_PURCHASE_PAYMENT",
]


class CashEvent(BaseModel):
    """T0에 확정된 날짜별 현금 유입/유출."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_date: date
    event_type: CashEventType
    amount_krw: Decimal = Field(ge=0)
    direction: CashEventDirection
    ref_id: str = Field(min_length=1)
    source_ref: str | None = None
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
    current_debt_krw: Decimal = Decimal(0)
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


class FinanceAgentRunResponse(BaseModel):
    """UI 조회용 Finance Agent 실행이력 응답."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    cycle: FinanceCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime
