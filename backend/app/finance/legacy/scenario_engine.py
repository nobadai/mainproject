"""고정 Finance Runtime Context에서 A/B 계산을 조립한다."""

from datetime import date, timedelta
from typing import TypedDict

from app.finance.schemas import (
    CashEvent,
    CashflowProjection,
    CashPriority,
    CollectionPreference,
    FinanceRuntimeContext,
    FinanceSalesRequest,
)
from app.finance.tools import (
    build_payroll_schedule,
    derive_cash_priority,
    project_cashflow,
    rank_collection_preferences,
)


class FinanceSalesScenarioResult(TypedDict):
    finance_state: dict[str, object] | None
    base_projection: CashflowProjection | None
    post_h1_projection: CashflowProjection | None
    base_cash_priority: CashPriority | None
    sales_cash_priority: CashPriority | None
    collection_preferences: list[CollectionPreference]
    unresolved_sources: tuple[str, ...]


def run_finance_sales_scenario(
    request: FinanceSalesRequest,
    context: FinanceRuntimeContext | None,
) -> FinanceSalesScenarioResult:
    """T0 Base에 H1의 authoritative payment_date Event를 Overlay한다."""
    finance_state = _snapshot_values(context)
    preferences = rank_collection_preferences(request.channel_terms)
    if context is None or context.unresolved_sources:
        return {
            "finance_state": finance_state,
            "base_projection": None,
            "post_h1_projection": None,
            "base_cash_priority": None,
            "sales_cash_priority": None,
            "collection_preferences": preferences,
            "unresolved_sources": context.unresolved_sources if context else (),
        }
    base_projection = _base_projection(request.as_of, context)
    h1_event = CashEvent(
        event_date=request.approved_purchase.payment_date,
        event_type="H1_PURCHASE_PAYMENT",
        amount_krw=request.approved_purchase.total_amount_krw,
        direction="OUTFLOW",
        ref_id=request.approved_purchase.approval_id,
    )
    post_h1 = project_cashflow(
        as_of=request.as_of,
        current_cash_krw=context.snapshot.current_cash_krw,
        horizon_end=base_projection.horizon_end,
        cash_events=(*_base_events(request.as_of, context), h1_event),
    )
    return {
        "finance_state": finance_state,
        "base_projection": base_projection,
        "post_h1_projection": post_h1,
        "base_cash_priority": derive_cash_priority(
            projected_cash_min=base_projection.projected_cash_min, policy=context.policy
        ),
        "sales_cash_priority": derive_cash_priority(
            projected_cash_min=post_h1.projected_cash_min, policy=context.policy
        ),
        "collection_preferences": preferences,
        "unresolved_sources": (),
    }


def _snapshot_values(context: FinanceRuntimeContext | None) -> dict[str, object] | None:
    if context is None:
        return None
    return context.snapshot.model_dump(exclude={"snapshot_id"})


def _base_events(as_of: date, context: FinanceRuntimeContext) -> tuple[CashEvent, ...]:
    horizon_end = as_of + timedelta(days=context.policy.cashflow_projection_days)
    payroll = build_payroll_schedule(as_of=as_of, horizon_end=horizon_end, policy=context.policy)
    return (*context.cash_events, *payroll)


def _base_projection(as_of: date, context: FinanceRuntimeContext) -> CashflowProjection:
    horizon_end = as_of + timedelta(days=context.policy.cashflow_projection_days)
    return project_cashflow(
        as_of=as_of,
        current_cash_krw=context.snapshot.current_cash_krw,
        horizon_end=horizon_end,
        cash_events=_base_events(as_of, context),
    )
