"""Finance 운영 정책 · 부채 계약."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.finance.contracts.numeric_guards import _reject_boolean


class FinancePolicy(BaseModel):
    """Finance MVP 실행에 사용하는 회사/Agent 운영 정책."""

    model_config = ConfigDict(extra="forbid")

    purchase_payment_days: int | None = Field(default=None, ge=0)
    payroll_date: int = Field(ge=1, le=31)
    margin_defense_floor_rate: Decimal | None = Field(default=None, ge=0, le=1)
    monthly_labor_cost_krw: Decimal | None = Field(default=None, ge=0)
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
        "margin_defense_floor_rate",
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
