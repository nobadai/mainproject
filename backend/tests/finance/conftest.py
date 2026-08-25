from datetime import date
from decimal import Decimal

import pytest

from app.finance.schemas import (
    FinanceDebtPolicy,
    FinancePolicy,
    FinanceRuntimeContext,
    FinanceSnapshot,
)


@pytest.fixture
def finance_state() -> dict[str, object]:
    return {
        "finance_state_id": "FIN-DAY30-LOAN",
        "sim_run_id": "SIM-BURNIN-202512",
        "state_date": date(2025, 12, 31),
        "state_type": "DAY30",
        "financing_mode": "LOAN_BASELINE",
        "current_cash_krw": Decimal("31993913.770000"),
        "minimum_operating_cash_krw": Decimal("15902640.000000"),
        "committed_outflows_krw": Decimal("0.000000"),
        "unsettled_purchase_payables_krw": Decimal("0.000000"),
        "financial_limit_krw": Decimal("16091273.770000"),
    }


@pytest.fixture
def finance_snapshot(finance_state) -> FinanceSnapshot:
    return FinanceSnapshot(snapshot_id=None, **finance_state)


@pytest.fixture
def finance_policy() -> FinancePolicy:
    return FinancePolicy(
        purchase_payment_days=7,
        payroll_date=25,
        monthly_labor_cost_krw=Decimal(12941280),
        minimum_cash_balance_krw=Decimal(12941280),
        cashflow_projection_days=30,
        cash_priority_reference="minimum_cash_balance_krw",
        cash_priority_high_ratio=Decimal("1.0"),
        cash_priority_medium_ratio=Decimal("1.5"),
        policy_version="v1.3-PROVISIONAL",
        usage_scope="AGENT_MVP_DEMO",
        source_refs={"fixture": "test"},
    )


@pytest.fixture
def finance_debt_policy() -> FinanceDebtPolicy:
    source_refs = {
        key: "MVP-DECISION-20260825:N9-DEMO"
        for key in (
            "debt_runtime_status",
            "debt_principal_krw",
            "debt_execution_date",
            "debt_annual_rate",
            "debt_term_months",
            "debt_grace_months",
            "debt_grace_payment_mode",
            "debt_repayment_method",
            "debt_payment_frequency",
            "debt_payment_day_rule",
            "debt_first_payment_rule",
            "debt_interest_method",
        )
    }
    return FinanceDebtPolicy(
        debt_runtime_status="SIM_FIXED_EXECUTED",
        debt_principal_krw=Decimal("45272104.184486"),
        debt_execution_date=date(2025, 12, 2),
        debt_annual_rate=Decimal("0.025"),
        debt_term_months=72,
        debt_grace_months=36,
        debt_grace_payment_mode="INTEREST_ONLY",
        debt_repayment_method="EQUAL_PRINCIPAL_AFTER_GRACE",
        debt_payment_frequency="MONTHLY",
        debt_payment_day_rule="MONTH_END",
        debt_first_payment_rule="EXECUTION_MONTH_END",
        debt_interest_method="OUTSTANDING_PRINCIPAL_ANNUAL_RATE_DIV_12",
        policy_version="v1.3-PROVISIONAL",
        usage_scope="AGENT_MVP_DEMO",
        source_refs=source_refs,
    )


@pytest.fixture
def finance_context(finance_snapshot, finance_policy, finance_debt_policy) -> FinanceRuntimeContext:
    return FinanceRuntimeContext(
        snapshot=finance_snapshot,
        policy=finance_policy,
        debt_policy=finance_debt_policy,
        cash_events=(),
    )


@pytest.fixture
def purchase_payload() -> dict[str, object]:
    return {
        "meta": {
            "as_of": "2025-12-31",
            "item": "배추",
            "agent_version": "v0.4",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": [
            {
                "label": "기본",
                "strategy_type": "quantity",
                "coverage_days": 5,
                "total_quantity_kg": 4500,
                "total_amount_krw": 7125000,
                "split_plan": [{"seq": 1, "date": "2025-12-31", "quantity_kg": 4500}],
                "sourcing_plan": [
                    {
                        "market": "가락",
                        "grade": "상",
                        "quantity_kg": 3000,
                        "grade_unit_price": 1650,
                    },
                    {
                        "market": "가락",
                        "grade": "중",
                        "quantity_kg": 1500,
                        "grade_unit_price": 1450,
                    },
                ],
            }
        ],
    }


@pytest.fixture
def sales_payload() -> dict[str, object]:
    return {
        "cycle": "SALES",
        "as_of": "2025-12-31",
        "approved_purchase": {
            "approval_id": "H1-20260821-001",
            "total_amount_krw": 10318995,
            "payment_date": "2026-08-28",
        },
        "channel_terms": [
            {
                "channel_type": "DIRECT_B2B",
                "partner_id": "KIMCHI_FACTORY_001",
                "settlement_days": 30,
            }
        ],
    }
