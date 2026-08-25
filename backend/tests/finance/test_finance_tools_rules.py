from datetime import date
from decimal import Decimal

from app.finance.rules import evaluate_finance_runtime_rules, evaluate_finance_sales_rules
from app.finance.schemas import ChannelTerm, PurchaseAgentOutput
from app.finance.tools import (
    calculate_purchase_scenario_amount,
    compare_reported_amount,
    rank_collection_preferences,
)


def test_purchase_total_recalculation_detects_contract_fixture_mismatch(purchase_payload):
    scenario = PurchaseAgentOutput.model_validate(purchase_payload).scenarios[0]

    amount = calculate_purchase_scenario_amount(scenario.sourcing_plan)
    comparison = compare_reported_amount(scenario.total_amount_krw, amount)

    assert amount == Decimal(7125000)
    assert comparison["is_match"] is False
    assert comparison["difference"] == Decimal(-3193995)


def test_procurement_runtime_returns_global_band(finance_state):
    result = evaluate_finance_runtime_rules(
        as_of=date(2025, 12, 31),
        finance_state=finance_state,
        projected_cash_min=Decimal("19052633.77"),
        minimum_cash_balance=Decimal(12941280),
        max_feasible_amount=Decimal(6111353),
    )

    assert result == {
        "runtime_status": "READY",
        "max_feasible_amount_krw": Decimal(6111353),
        "hard_constraints": [],
        "soft_warnings": [],
    }


def test_procurement_runtime_fails_closed_on_as_of_mismatch(finance_state):
    result = evaluate_finance_runtime_rules(
        as_of=date(2026, 8, 21),
        finance_state=finance_state,
        has_cost_mismatch=True,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["max_feasible_amount_krw"] is None
    assert result["hard_constraints"] == ["AS_OF_MISMATCH"]
    assert result["soft_warnings"] == ["COST_MISMATCH"]


def test_procurement_runtime_requires_finance_state():
    result = evaluate_finance_runtime_rules(
        as_of=date(2026, 8, 21),
        finance_state=None,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["hard_constraints"] == ["REQUIRED_FINANCE_STATE_MISSING"]


def test_sales_rule_keeps_priority_unresolved_without_policy(finance_state):
    result = evaluate_finance_sales_rules(
        as_of=date(2025, 12, 31),
        finance_state=finance_state,
        base_projected_cash_min=None,
        post_h1_projected_cash_min=None,
        minimum_cash_balance=None,
        policy_available=False,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["hard_constraints"] == ["REQUIRED_FINANCE_POLICY_MISSING"]


def test_collection_preferences_rank_shorter_settlement_first():
    terms = [
        ChannelTerm(channel_type="DIRECT_B2B", partner_id="A", settlement_days=30),
        ChannelTerm(channel_type="RETAIL", partner_id="B", settlement_days=7),
        ChannelTerm(channel_type="EXPORT", partner_id="C", settlement_days=30),
    ]

    preferences = rank_collection_preferences(terms)

    assert [item.liquidity_rank for item in preferences] == [2, 1, 2]
