"""Finance P0 결정론적 Core 실행 흐름."""

from datetime import date
from decimal import Decimal
from typing import TypedDict

from app.finance.repository import FinanceState, get_current_finance_state
from app.finance.rules import FinanceRuleResult, evaluate_finance_rules
from app.finance.schemas import FinanceReviewRequest
from app.finance.tools import (
    ExpectedCostComparison,
    calculate_financial_limit,
    calculate_post_purchase_cash,
    calculate_proposal_amount,
    compare_expected_cost,
)


class FinanceCoreResult(TypedDict):
    proposal_id: str
    scenario_id: str
    purchase_as_of: date
    finance_state: FinanceState | None
    proposal_amount_krw: Decimal
    expected_cost_comparison: ExpectedCostComparison
    db_financial_limit_krw: Decimal | None
    recalculated_financial_limit_krw: Decimal | None
    financial_limit_matches: bool | None
    post_purchase_cash_krw: Decimal | None
    rule_result: FinanceRuleResult


def run_finance_core(request: FinanceReviewRequest) -> FinanceCoreResult:
    """Repository, Tools, Rules를 순서대로 호출해 Finance Core 결과를 만든다."""
    try:
        finance_state = get_current_finance_state()
    except LookupError:
        finance_state = None

    proposal_amount = calculate_proposal_amount(request.scenario.sourcing_plan)
    expected_cost_comparison = compare_expected_cost(
        request.scenario.expected_cost,
        proposal_amount,
    )

    db_financial_limit = None
    recalculated_financial_limit = None
    financial_limit_matches = None
    post_purchase_cash = None
    if finance_state is not None:
        db_financial_limit = finance_state["financial_limit_krw"]
        recalculated_financial_limit = calculate_financial_limit(
            finance_state["current_cash_krw"],
            finance_state["minimum_operating_cash_krw"],
            finance_state["committed_outflows_krw"],
            finance_state["unsettled_purchase_payables_krw"],
        )
        financial_limit_matches = db_financial_limit == recalculated_financial_limit
        if not financial_limit_matches:
            raise ValueError("FINANCIAL_LIMIT_MISMATCH")
        post_purchase_cash = calculate_post_purchase_cash(
            finance_state["current_cash_krw"],
            proposal_amount,
            finance_state["committed_outflows_krw"],
            finance_state["unsettled_purchase_payables_krw"],
        )

    rule_result = evaluate_finance_rules(
        purchase_as_of=request.purchase_meta.as_of,
        proposal_amount=proposal_amount,
        expected_cost_comparison=expected_cost_comparison,
        finance_state=finance_state,
    )
    return {
        "proposal_id": request.proposal_id,
        "scenario_id": request.scenario_id,
        "purchase_as_of": request.purchase_meta.as_of,
        "finance_state": finance_state,
        "proposal_amount_krw": proposal_amount,
        "expected_cost_comparison": expected_cost_comparison,
        "db_financial_limit_krw": db_financial_limit,
        "recalculated_financial_limit_krw": recalculated_financial_limit,
        "financial_limit_matches": financial_limit_matches,
        "post_purchase_cash_krw": post_purchase_cash,
        "rule_result": rule_result,
    }
