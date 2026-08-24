"""고정 Finance Snapshot에서 A/B 계산과 Rule 호출을 조립한다."""

from decimal import Decimal
from typing import TypedDict

from app.finance.schemas import (
    CollectionPreference,
    FinanceSalesRequest,
    FinanceSnapshot,
    PurchaseAgentOutput,
)
from app.finance.tools import (
    ReportedAmountComparison,
    calculate_financial_limit,
    calculate_post_purchase_cash,
    calculate_purchase_scenario_amount,
    compare_reported_amount,
    rank_collection_preferences,
)


class FinanceProcurementScenarioResult(TypedDict):
    finance_state: dict[str, object] | None
    financial_limit_matches: bool | None
    amount_comparisons: list[ReportedAmountComparison]
    has_cost_mismatch: bool


class FinanceSalesScenarioResult(TypedDict):
    finance_state: dict[str, object] | None
    financial_limit_matches: bool | None
    post_approved_purchase_cash_krw: Decimal | None
    collection_preferences: list[CollectionPreference]


def run_finance_procurement_scenario(
    request: PurchaseAgentOutput,
    snapshot: FinanceSnapshot | None,
) -> FinanceProcurementScenarioResult:
    """Finance A 계산을 기존 Tool로 수행하고 Runtime Rule을 호출한다."""
    finance_state = _snapshot_values(snapshot)
    financial_limit_matches = _cross_check_financial_limit(finance_state)
    comparisons = [
        compare_reported_amount(
            scenario.total_amount_krw,
            calculate_purchase_scenario_amount(scenario.sourcing_plan),
        )
        for scenario in request.scenarios
    ]
    return {
        "finance_state": finance_state,
        "financial_limit_matches": financial_limit_matches,
        "amount_comparisons": comparisons,
        "has_cost_mismatch": any(not comparison["is_match"] for comparison in comparisons),
    }


def run_finance_sales_scenario(
    request: FinanceSalesRequest,
    snapshot: FinanceSnapshot | None,
) -> FinanceSalesScenarioResult:
    """H1 금액 Overlay와 회수 순위를 계산하고 Finance B Rule을 호출한다."""
    finance_state = _snapshot_values(snapshot)
    financial_limit_matches = _cross_check_financial_limit(finance_state)
    post_approved_purchase_cash = None
    if finance_state is not None:
        assert finance_state is not None
        post_approved_purchase_cash = calculate_post_purchase_cash(
            finance_state["current_cash_krw"],
            request.approved_purchase.total_amount_krw,
            finance_state["committed_outflows_krw"],
            finance_state["unsettled_purchase_payables_krw"],
        )
    return {
        "finance_state": finance_state,
        "financial_limit_matches": financial_limit_matches,
        "post_approved_purchase_cash_krw": post_approved_purchase_cash,
        "collection_preferences": rank_collection_preferences(request.channel_terms),
    }


def _snapshot_values(snapshot: FinanceSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return snapshot.model_dump(exclude={"snapshot_id"})


def _cross_check_financial_limit(finance_state: dict[str, object] | None) -> bool | None:
    if finance_state is None:
        return None
    recalculated_financial_limit = calculate_financial_limit(
        finance_state["current_cash_krw"],
        finance_state["minimum_operating_cash_krw"],
        finance_state["committed_outflows_krw"],
        finance_state["unsettled_purchase_payables_krw"],
    )
    matches = finance_state["financial_limit_krw"] == recalculated_financial_limit
    if not matches:
        raise ValueError("FINANCIAL_LIMIT_MISMATCH")
    return matches
