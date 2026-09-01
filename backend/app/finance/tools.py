"""Finance P0의 결정론적 재무 계산 도구."""

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from app.finance.schemas import (
    CashEvent,
    CashflowPoint,
    CashflowProjection,
    ChannelTerm,
    CollectionPreference,
    FinanceDebtPolicy,
    FinancePolicy,
)
from app.purchase_agent.schemas import SourcingPlanItem as PurchaseSourcingPlanItem

KRW_QUANTUM = Decimal("0.000001")


class ExpectedCostComparison(TypedDict):
    is_match: bool
    expected_cost: Decimal
    recalculated_cost: Decimal
    difference: Decimal


class ReportedAmountComparison(TypedDict):
    is_match: bool
    reported_amount_krw: Decimal
    recalculated_amount_krw: Decimal
    difference: Decimal


def build_debt_service_schedule(
    *, debt_policy: FinanceDebtPolicy, as_of: date, horizon_end: date
) -> tuple[CashEvent, ...]:
    """SIM_FIXED 월말 계약으로 horizon 내 원리금 Event를 생성한다."""
    if horizon_end < as_of:
        raise ValueError("horizon_end must not precede as_of")
    repayment_periods = debt_policy.debt_term_months - debt_policy.debt_grace_months
    installment = (debt_policy.debt_principal_krw / Decimal(repayment_periods)).quantize(
        KRW_QUANTUM, rounding=ROUND_HALF_UP
    )
    outstanding = debt_policy.debt_principal_krw
    events: list[CashEvent] = []
    for period in range(1, debt_policy.debt_term_months + 1):
        payment_date = _month_end_after(debt_policy.debt_execution_date, period - 1)
        interest = (outstanding * debt_policy.debt_annual_rate / Decimal(12)).quantize(
            KRW_QUANTUM, rounding=ROUND_HALF_UP
        )
        principal = Decimal(0)
        if period > debt_policy.debt_grace_months:
            principal = outstanding if period == debt_policy.debt_term_months else installment
            principal = principal.quantize(KRW_QUANTUM, rounding=ROUND_HALF_UP)
        total = (principal + interest).quantize(KRW_QUANTUM, rounding=ROUND_HALF_UP)
        if as_of < payment_date <= horizon_end:
            events.append(
                CashEvent(
                    event_date=payment_date,
                    event_type="DEBT_SERVICE",
                    amount_krw=total,
                    direction="OUTFLOW",
                    ref_id=f"DEBT:{period:02d}:{payment_date.isoformat()}",
                    source_ref=debt_policy.source_refs["debt_runtime_status"],
                    principal_component_krw=principal,
                    interest_component_krw=interest,
                )
            )
        outstanding -= principal
    if outstanding != 0:
        raise ArithmeticError("Debt schedule did not fully repay principal")
    return tuple(events)


def _month_end_after(execution_date: date, month_offset: int) -> date:
    month_index = execution_date.year * 12 + execution_date.month - 1 + month_offset
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, monthrange(year, month)[1])


def build_payroll_schedule(
    *, as_of: date, horizon_end: date, policy: FinancePolicy
) -> tuple[CashEvent, ...]:
    """Projection 구간 안의 미래 급여일을 월 경계와 무관하게 생성한다."""
    if horizon_end < as_of:
        raise ValueError("horizon_end must not precede as_of")
    amount_source_ref = policy.source_refs.get("monthly_labor_cost_krw")
    date_source_ref = policy.source_refs.get("payroll_date")
    if not amount_source_ref:
        raise ValueError("monthly_labor_cost_krw source_ref is required for PAYROLL")
    if not date_source_ref:
        raise ValueError("payroll_date source_ref is required for PAYROLL")
    year, month = as_of.year, as_of.month
    events: list[CashEvent] = []
    while date(year, month, 1) <= horizon_end:
        try:
            payroll_day = date(year, month, policy.payroll_date)
        except ValueError as exc:
            raise ValueError("payroll_date is invalid for a projection month") from exc
        if as_of < payroll_day <= horizon_end:
            events.append(
                CashEvent(
                    event_date=payroll_day,
                    event_type="PAYROLL",
                    amount_krw=policy.monthly_labor_cost_krw,
                    direction="OUTFLOW",
                    ref_id=f"PAYROLL:{payroll_day.isoformat()}",
                    source_ref=amount_source_ref,
                    schedule_source_ref=date_source_ref,
                )
            )
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return tuple(events)


def derive_critical_payment_dates(
    *,
    current_cash_krw: Decimal,
    cash_events: tuple[CashEvent, ...] | list[CashEvent],
    minimum_cash_balance_krw: Decimal,
) -> tuple[date, ...]:
    """위험 지급일과 동률인 모든 최대 확정 유출일을 반환한다."""
    by_date: dict[date, list[CashEvent]] = defaultdict(list)
    for event in cash_events:
        by_date[event.event_date].append(event)

    balance = current_cash_krw
    violation_dates: set[date] = set()
    daily_outflows: dict[date, Decimal] = {}
    for event_date in sorted(by_date):
        daily = by_date[event_date]
        inflow = sum(
            (event.amount_krw for event in daily if event.direction == "INFLOW"), Decimal(0)
        )
        outflow = sum(
            (event.amount_krw for event in daily if event.direction == "OUTFLOW"), Decimal(0)
        )
        balance += inflow - outflow
        if outflow > 0:
            daily_outflows[event_date] = outflow
            if balance < minimum_cash_balance_krw:
                violation_dates.add(event_date)

    max_dates: set[date] = set()
    if daily_outflows:
        maximum = max(daily_outflows.values())
        max_dates = {day for day, amount in daily_outflows.items() if amount == maximum}
    return tuple(sorted(violation_dates | max_dates))


def project_cashflow(
    *,
    as_of: date,
    current_cash_krw: Decimal,
    horizon_end: date,
    cash_events: tuple[CashEvent, ...] | list[CashEvent],
) -> CashflowProjection:
    """확정 Event만 날짜순으로 합산하여 현금 Projection을 만든다."""
    if horizon_end < as_of:
        raise ValueError("horizon_end must not precede as_of")
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    seen: set[tuple[date, str, str]] = set()
    for event in cash_events:
        identity = (event.event_date, event.event_type, event.ref_id)
        if identity in seen:
            raise ValueError(f"Duplicate cash event: {event.ref_id}")
        seen.add(identity)
        if not as_of < event.event_date <= horizon_end:
            continue
        signed_amount = event.amount_krw if event.direction == "INFLOW" else -event.amount_krw
        by_date[event.event_date] += signed_amount

    balance = current_cash_krw
    points = [CashflowPoint(projection_date=as_of, cash_balance_krw=balance)]
    minimum = balance
    minimum_date = as_of
    for event_date in sorted(by_date):
        balance += by_date[event_date]
        points.append(CashflowPoint(projection_date=event_date, cash_balance_krw=balance))
        if balance < minimum:
            minimum = balance
            minimum_date = event_date
    return CashflowProjection(
        as_of=as_of,
        horizon_end=horizon_end,
        projected_cash_by_date=tuple(points),
        projected_cash_min=minimum,
        projected_cash_min_date=minimum_date,
    )


def calculate_projected_cash_min(projection: CashflowProjection) -> Decimal:
    return projection.projected_cash_min


def derive_cash_priority(*, projected_cash_min: Decimal, policy: FinancePolicy) -> str:
    """Policy ratio 경계로 현금 회수 우선도를 결정한다."""
    minimum_cash = policy.minimum_cash_balance_krw
    if minimum_cash <= 0:
        raise ValueError("minimum_cash_balance_krw must be positive")
    if policy.cash_priority_reference != "minimum_cash_balance_krw":
        raise ValueError("Unsupported cash_priority_reference")
    ratio = projected_cash_min / minimum_cash
    if ratio < policy.cash_priority_high_ratio:
        return "HIGH"
    if ratio < policy.cash_priority_medium_ratio:
        return "MEDIUM"
    return "LOW"


def calculate_finance_cap(*, base_projection: CashflowProjection, policy: FinancePolicy) -> Decimal:
    """단일 D+N 매입 지급을 Overlay할 때의 보수적 원 단위 상한."""
    if policy.purchase_payment_days is None:
        raise ValueError("purchase_payment_days is required for Finance Cap")
    payment_date = base_projection.as_of + timedelta(days=policy.purchase_payment_days)
    if not base_projection.as_of < payment_date <= base_projection.horizon_end:
        raise ValueError("purchase payment date is outside projection horizon")
    balance_at_payment = [
        point.cash_balance_krw
        for point in base_projection.projected_cash_by_date
        if point.projection_date <= payment_date
    ][-1]
    balances_on_or_after = [balance_at_payment] + [
        point.cash_balance_krw
        for point in base_projection.projected_cash_by_date
        if point.projection_date > payment_date
    ]
    capacity = min(balances_on_or_after) - policy.minimum_cash_balance_krw
    return max(Decimal(0), capacity.quantize(Decimal(1), rounding="ROUND_FLOOR"))


def calculate_purchase_scenario_amount(
    sourcing_plan: list[PurchaseSourcingPlanItem],
) -> Decimal:
    """Purchase Agent v0.4 소싱 계획의 총 매입금액을 재계산한다."""
    return sum(
        (Decimal(item.qty_kg) * Decimal(item.grade_unit_price) for item in sourcing_plan),
        start=Decimal(0),
    )


def compare_reported_amount(
    reported_amount_krw: Decimal,
    recalculated_amount_krw: Decimal,
) -> ReportedAmountComparison:
    """Purchase Agent v0.4 보고 금액과 Finance 재계산 금액을 비교한다."""
    difference = recalculated_amount_krw - reported_amount_krw
    return {
        "is_match": difference == Decimal(0),
        "reported_amount_krw": reported_amount_krw,
        "recalculated_amount_krw": recalculated_amount_krw,
        "difference": difference,
    }


def rank_collection_preferences(
    channel_terms: list[ChannelTerm],
) -> list[CollectionPreference]:
    """정산일이 짧은 채널부터 동일 일수에 같은 유동성 순위를 부여한다."""
    settlement_days = sorted({term.settlement_days for term in channel_terms})
    ranks = {days: index + 1 for index, days in enumerate(settlement_days)}
    return [
        CollectionPreference(
            channel_type=term.channel_type,
            partner_id=term.partner_id,
            settlement_days=term.settlement_days,
            liquidity_rank=ranks[term.settlement_days],
        )
        for term in channel_terms
    ]
