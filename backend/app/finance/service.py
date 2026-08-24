"""Finance P0 결정론적 Core 실행 흐름."""

from datetime import date
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from app.finance.repository import FinanceState, get_current_finance_state
from app.finance.rules import (
    FinanceRuleResult,
    evaluate_finance_rules,
    evaluate_finance_runtime_rules,
    evaluate_finance_sales_rules,
    has_required_finance_state,
)
from app.finance.run_repository import (
    get_finance_agent_run,
    list_finance_agent_runs,
    save_finance_agent_run,
)
from app.finance.schemas import (
    FinanceAgentRunResponse,
    FinanceBand,
    FinanceCycle,
    FinanceProcurementResponse,
    FinanceReviewRequest,
    FinanceSalesRequest,
    FinanceSalesResponse,
    ProcurementSuggestedAdjustment,
    PurchaseAgentOutput,
    RuntimeStatus,
)
from app.finance.tools import (
    ExpectedCostComparison,
    calculate_financial_limit,
    calculate_post_purchase_cash,
    calculate_proposal_amount,
    calculate_purchase_scenario_amount,
    compare_expected_cost,
    compare_reported_amount,
    rank_collection_preferences,
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


def _get_current_finance_state_or_none() -> FinanceState | None:
    try:
        return get_current_finance_state()
    except LookupError:
        return None


def _cross_check_financial_limit(finance_state: FinanceState | None) -> None:
    if not has_required_finance_state(finance_state):
        return
    assert finance_state is not None
    recalculated_financial_limit = calculate_financial_limit(
        finance_state["current_cash_krw"],
        finance_state["minimum_operating_cash_krw"],
        finance_state["committed_outflows_krw"],
        finance_state["unsettled_purchase_payables_krw"],
    )
    if finance_state["financial_limit_krw"] != recalculated_financial_limit:
        raise ValueError("FINANCIAL_LIMIT_MISMATCH")


def run_finance_procurement(request: PurchaseAgentOutput) -> FinanceProcurementResponse:
    """Finance State에서 모든 품목에 공통으로 적용할 매입 금액 Band를 산출한다."""
    finance_state = _get_current_finance_state_or_none()
    _cross_check_financial_limit(finance_state)

    has_cost_mismatch = False
    for scenario in request.scenarios:
        recalculated_amount = calculate_purchase_scenario_amount(scenario.sourcing_plan)
        comparison = compare_reported_amount(scenario.total_amount_krw, recalculated_amount)
        has_cost_mismatch = has_cost_mismatch or not comparison["is_match"]

    rule_result = evaluate_finance_runtime_rules(
        as_of=request.meta.as_of,
        finance_state=finance_state,
        has_cost_mismatch=has_cost_mismatch,
    )
    max_feasible_amount = rule_result["max_feasible_amount_krw"]
    suggested_adjustment = (
        ProcurementSuggestedAdjustment(max_amount_krw=max_feasible_amount)
        if max_feasible_amount is not None
        else None
    )
    response = FinanceProcurementResponse(
        as_of=request.meta.as_of,
        snapshot_id=finance_state["finance_state_id"] if finance_state is not None else None,
        runtime_status=rule_result["runtime_status"],
        band=FinanceBand(max_feasible_amount_krw=max_feasible_amount),
        base_projected_cash_min=None,
        base_cash_priority=None,
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=rule_result["soft_warnings"],
        suggested_adjustment=suggested_adjustment,
        evidences=[],
    )
    save_finance_agent_run(
        cycle="PROCUREMENT",
        as_of=request.meta.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def run_finance_sales(request: FinanceSalesRequest) -> FinanceSalesResponse:
    """승인 매입 지급 의무를 Overlay하고 판매 회수 우선도 입력을 구성한다."""
    finance_state = _get_current_finance_state_or_none()
    _cross_check_financial_limit(finance_state)

    post_approved_purchase_cash = None
    if has_required_finance_state(finance_state):
        assert finance_state is not None
        post_approved_purchase_cash = calculate_post_purchase_cash(
            finance_state["current_cash_krw"],
            request.approved_purchase.total_amount_krw,
            finance_state["committed_outflows_krw"],
            finance_state["unsettled_purchase_payables_krw"],
        )
    rule_result = evaluate_finance_sales_rules(
        as_of=request.as_of,
        finance_state=finance_state,
        post_approved_purchase_cash=post_approved_purchase_cash,
    )
    response = FinanceSalesResponse(
        snapshot_id=finance_state["finance_state_id"] if finance_state is not None else None,
        approval_id=request.approved_purchase.approval_id,
        runtime_status=rule_result["runtime_status"],
        base_cash_priority=None,
        sales_cash_priority=None,
        collection_preferences=rank_collection_preferences(request.channel_terms),
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=rule_result["soft_warnings"],
    )
    save_finance_agent_run(
        cycle="SALES",
        as_of=request.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def get_finance_run(run_id: UUID) -> FinanceAgentRunResponse:
    """UI 조회용 Finance Agent 실행이력 한 건을 반환한다."""
    return FinanceAgentRunResponse.model_validate(get_finance_agent_run(run_id))


def list_finance_runs(
    *,
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: int = 100,
) -> list[FinanceAgentRunResponse]:
    """UI 조회용 Finance Agent 실행이력 목록을 반환한다."""
    rows = list_finance_agent_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        limit=limit,
    )
    return [FinanceAgentRunResponse.model_validate(row) for row in rows]


def run_finance_core(request: FinanceReviewRequest) -> FinanceCoreResult:
    """Repository, Tools, Rules를 순서대로 호출해 Finance Core 결과를 만든다."""
    finance_state = _get_current_finance_state_or_none()

    proposal_amount = calculate_proposal_amount(request.scenario.sourcing_plan)
    expected_cost_comparison = compare_expected_cost(
        request.scenario.expected_cost,
        proposal_amount,
    )
    db_financial_limit = None
    recalculated_financial_limit = None
    financial_limit_matches = None
    post_purchase_cash = None
    if has_required_finance_state(finance_state):
        assert finance_state is not None
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

    rule_result = evaluate_finance_rules(
        purchase_as_of=request.purchase_meta.as_of,
        proposal_amount=proposal_amount,
        expected_cost_comparison=expected_cost_comparison,
        finance_state=finance_state,
    )

    if has_required_finance_state(finance_state):
        assert finance_state is not None
        if "AS_OF_MISMATCH" not in rule_result["hard_constraints"]:
            post_purchase_cash = calculate_post_purchase_cash(
                finance_state["current_cash_krw"],
                proposal_amount,
                finance_state["committed_outflows_krw"],
                finance_state["unsettled_purchase_payables_krw"],
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
