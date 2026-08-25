"""Finance P0 결정론적 Core 실행 흐름."""

from datetime import date
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from app.finance.interpretation import enrich_finance_response
from app.finance.llm.runtime import InterpretationService
from app.finance.repository import (
    FinanceState,
    get_current_finance_runtime_context,
    get_current_finance_snapshot,
    get_current_finance_state,
)
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
from app.finance.scenario_engine import (
    run_finance_procurement_scenario,
    run_finance_sales_scenario,
)
from app.finance.schemas import (
    CashEvent,
    FinanceAgentRunResponse,
    FinanceBand,
    FinanceCycle,
    FinancePolicy,
    FinanceProcurementResponse,
    FinanceReviewRequest,
    FinanceRuntimeContext,
    FinanceSalesRequest,
    FinanceSalesResponse,
    FinanceSnapshot,
    ProcurementSuggestedAdjustment,
    PurchaseAgentOutput,
    RuntimeStatus,
)
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


def _get_current_finance_state_or_none() -> FinanceState | None:
    try:
        return get_current_finance_state()
    except LookupError:
        return None


def _get_current_finance_snapshot_or_none() -> FinanceSnapshot | None:
    try:
        return get_current_finance_snapshot()
    except LookupError:
        return None


def _get_current_finance_runtime_context_or_none() -> FinanceRuntimeContext | None:
    try:
        return get_current_finance_runtime_context()
    except LookupError:
        return None


def run_finance_procurement(request: PurchaseAgentOutput) -> FinanceProcurementResponse:
    """Finance State에서 모든 품목에 공통으로 적용할 매입 금액 Band를 산출한다."""
    context = _get_current_finance_runtime_context_or_none()
    response = run_finance_procurement_with_context(request, context)
    save_finance_agent_run(
        cycle="PROCUREMENT",
        as_of=request.meta.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def run_finance_procurement_with_snapshot(
    request: PurchaseAgentOutput,
    snapshot: FinanceSnapshot | None,
    interpretation_service: InterpretationService | None = None,
    *,
    policy: FinancePolicy | None = None,
    cash_events: tuple[CashEvent, ...] = (),
    unresolved_sources: tuple[str, ...] = (),
) -> FinanceProcurementResponse:
    """외부에서 고정한 Finance Snapshot으로 Finance A를 실행한다."""
    context = (
        FinanceRuntimeContext(
            snapshot=snapshot,
            policy=policy,
            cash_events=cash_events,
            unresolved_sources=unresolved_sources,
        )
        if snapshot is not None and policy is not None
        else None
    )
    return run_finance_procurement_with_context(request, context, interpretation_service)


def run_finance_procurement_with_context(
    request: PurchaseAgentOutput,
    context: FinanceRuntimeContext | None,
    interpretation_service: InterpretationService | None = None,
) -> FinanceProcurementResponse:
    """DB 재조회 없이 고정된 T0 Context로 Finance A를 실행한다."""
    scenario_result = run_finance_procurement_scenario(request, context)
    policy = context.policy if context is not None else None
    projection = scenario_result["base_projection"]
    rule_result = evaluate_finance_runtime_rules(
        as_of=request.meta.as_of,
        finance_state=scenario_result["finance_state"],
        has_cost_mismatch=scenario_result["has_cost_mismatch"],
        projected_cash_min=projection.projected_cash_min if projection else None,
        minimum_cash_balance=policy.minimum_cash_balance_krw if policy else None,
        max_feasible_amount=scenario_result["max_feasible_amount_krw"],
        policy_available=policy is not None,
        unresolved_sources=scenario_result["unresolved_sources"],
    )
    max_feasible_amount = rule_result["max_feasible_amount_krw"]
    suggested_adjustment = (
        ProcurementSuggestedAdjustment(max_amount_krw=max_feasible_amount)
        if max_feasible_amount is not None
        else None
    )
    response = FinanceProcurementResponse(
        as_of=request.meta.as_of,
        snapshot_id=context.snapshot.snapshot_id if context is not None else None,
        runtime_status=rule_result["runtime_status"],
        band=FinanceBand(max_feasible_amount_krw=max_feasible_amount),
        base_projected_cash_min=projection.projected_cash_min if projection else None,
        base_cash_priority=scenario_result["base_cash_priority"],
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=rule_result["soft_warnings"],
        suggested_adjustment=suggested_adjustment,
        evidences=[],
    )
    return enrich_finance_response(response, interpretation_service)


def run_finance_sales(request: FinanceSalesRequest) -> FinanceSalesResponse:
    """승인 매입 지급 의무를 Overlay하고 판매 회수 우선도 입력을 구성한다."""
    context = _get_current_finance_runtime_context_or_none()
    response = run_finance_sales_with_context(request, context)
    save_finance_agent_run(
        cycle="SALES",
        as_of=request.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def run_finance_sales_with_snapshot(
    request: FinanceSalesRequest,
    snapshot: FinanceSnapshot | None,
    interpretation_service: InterpretationService | None = None,
    *,
    policy: FinancePolicy | None = None,
    cash_events: tuple[CashEvent, ...] = (),
    unresolved_sources: tuple[str, ...] = (),
) -> FinanceSalesResponse:
    """외부에서 고정한 Finance Snapshot으로 Finance B를 실행한다."""
    context = (
        FinanceRuntimeContext(
            snapshot=snapshot,
            policy=policy,
            cash_events=cash_events,
            unresolved_sources=unresolved_sources,
        )
        if snapshot is not None and policy is not None
        else None
    )
    return run_finance_sales_with_context(request, context, interpretation_service)


def run_finance_sales_with_context(
    request: FinanceSalesRequest,
    context: FinanceRuntimeContext | None,
    interpretation_service: InterpretationService | None = None,
) -> FinanceSalesResponse:
    """DB 재조회 없이 고정된 T0 Context로 Finance B를 실행한다."""
    scenario_result = run_finance_sales_scenario(request, context)
    policy = context.policy if context is not None else None
    base_projection = scenario_result["base_projection"]
    post_h1_projection = scenario_result["post_h1_projection"]
    rule_result = evaluate_finance_sales_rules(
        as_of=request.as_of,
        finance_state=scenario_result["finance_state"],
        base_projected_cash_min=(base_projection.projected_cash_min if base_projection else None),
        post_h1_projected_cash_min=(
            post_h1_projection.projected_cash_min if post_h1_projection else None
        ),
        minimum_cash_balance=policy.minimum_cash_balance_krw if policy else None,
        policy_available=policy is not None,
        unresolved_sources=scenario_result["unresolved_sources"],
    )
    response = FinanceSalesResponse(
        snapshot_id=context.snapshot.snapshot_id if context is not None else None,
        approval_id=request.approved_purchase.approval_id,
        runtime_status=rule_result["runtime_status"],
        base_cash_priority=scenario_result["base_cash_priority"],
        sales_cash_priority=scenario_result["sales_cash_priority"],
        collection_preferences=scenario_result["collection_preferences"],
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=rule_result["soft_warnings"],
    )
    return enrich_finance_response(response, interpretation_service)


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
