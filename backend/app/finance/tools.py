"""Finance P0의 결정론적 재무 계산 도구."""

from calendar import monthrange
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from app.finance.sales_models import (
    OPEN_RECEIVABLE_STATUSES,
    InventoryCostBasis,
    PartnerReceivable,
    PartnerReceivableFacts,
    SalesCostBasis,
    SalesScenarioCashflow,
    VerifiedDirectCost,
)
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


class ReportedAmountComparison(TypedDict):
    is_match: bool
    reported_amount_krw: Decimal
    recalculated_amount_krw: Decimal
    difference: Decimal


#: **당일(`as_of`) 만기가 유효한 사건**. 매입지급만이다 — N5=0 은 당일 지급이라는
#: 유효한 정책이고, 미결제(OPEN) 의무는 오늘 만기라도 아직 나가지 않았다.
#:
#: 🔴 채권·비용·판매 회수까지 같이 열지 않는다. 제안 회수의 horizon 계약
#:    (`collection_within_horizon = as_of < event_date`)이 여전히 `as_of` 를 제외하므로,
#:    투영만 열면 **투영과 그 플래그가 서로 다른 말을 한다.**
_SAME_DAY_DUE_EVENT_TYPES = frozenset({"PURCHASE_PAYABLE", "EXTRA_PURCHASE"})

#: 토·일에는 회사가 돈을 내보내지 않는다. **계약일이 아니라 현금이 나가는 날만** 민다.
_WEEKEND_SHIFT = {6: 2, 7: 1}  # ISO 요일: 토 → 월(+2), 일 → 월(+1)


def effective_cash_date(contractual_due_date: date) -> date:
    """계약 지급일이 주말이면 현금이 실제로 나가는 날은 다음 월요일이다.

    🔴 **계약일을 고쳐 쓰는 함수가 아니다.** 원장의 `due_date` 는 토·일 그대로
       남는다 (실제 원장에 이미 주말 만기가 4건 있다). 여기서 나오는 값은
       *현금흐름에 얹는 날*뿐이다 — 둘을 합치면 계약 사실이 사라진다.

    ★ 공휴일은 다루지 않는다. 토·일만이 현재 계약이다.
    """
    shift = _WEEKEND_SHIFT.get(contractual_due_date.isoweekday(), 0)
    return contractual_due_date + timedelta(days=shift)


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
        if not as_of <= event.event_date <= horizon_end:
            continue
        if event.event_date == as_of and event.event_type not in _SAME_DAY_DUE_EVENT_TYPES:
            continue
        signed_amount = event.amount_krw if event.direction == "INFLOW" else -event.amount_krw
        by_date[event.event_date] += signed_amount

    # ★ **당일 만기 매입지급은 시작점에 접어 넣는다.** N5=0 이면 지급일이 `as_of` 와
    #   같은데, 예전처럼 `as_of <` 로 걸러 내면 그 유출이 투영에서 통째로 사라졌다.
    #   별도 점으로 붙이면 같은 날짜 점이 둘이 되므로, 시작 잔액 자체를 그날 의무가
    #   빠진 값으로 연다 — 당일 사건이 없으면 예전과 같은 값이다.
    balance = current_cash_krw + by_date.pop(as_of, Decimal(0))
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
    # 계약 지급일과 **현금이 나가는 날**은 다른 값이다. 상한은 현금이 나가는 날로 잰다.
    contractual_due_date = base_projection.as_of + timedelta(days=policy.purchase_payment_days)
    payment_date = effective_cash_date(contractual_due_date)
    # 🔴 `as_of <` 가 아니라 `as_of <=` 다. N5=0 은 **당일 지급**이라는 유효한 정책이고,
    #    예전 경계는 그 정책을 값이 아니라 오류로 취급했다.
    if not base_projection.as_of <= payment_date <= base_projection.horizon_end:
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


# ---------------------------------------------------------------------------
# Sales Core Phase 1 — 결정론적 계산만 담당한다 (판정·정책 없음)
#
# 이 절의 함수들은 "계산 사실"만 만든다. PASS/FAIL·REVIEW_REQUIRED 같은 판정,
# Sales Margin 임계값, 회수 위험도는 Finance ↔ Master/Sales 계약이 정해진 뒤
# Rule 계층에서 다룬다 — 여기서 앞당겨 결정하지 않는다.
# ---------------------------------------------------------------------------


class SalesCalculationFacts(TypedDict):
    """Finance 내부 전용 매출 계산 사실 — Master/Router 계약이 아니다."""

    recalculated_sales_amount_krw: Decimal
    reported_amount_comparison: ReportedAmountComparison | None
    contribution_margin_krw: Decimal | None
    contribution_margin_rate: Decimal | None
    collection_date: date | None


def calculate_sales_amount(
    *,
    quantity_kg: Decimal,
    unit_price_krw: Decimal,
) -> Decimal:
    """매출액을 수량 × 단가로 재계산한다.

    Purchase 재계산(`calculate_purchase_scenario_amount`)과 같은 규약이다 —
    곱만 하고 임의로 반올림하지 않는다. 0은 '없음'이 아니라 값 0으로 다룬다.
    """
    if quantity_kg < 0:
        raise ValueError("quantity_kg must not be negative")
    if unit_price_krw < 0:
        raise ValueError("unit_price_krw must not be negative")
    return quantity_kg * unit_price_krw


def compare_reported_sales_amount(
    *,
    reported_amount_krw: Decimal,
    recalculated_amount_krw: Decimal,
) -> ReportedAmountComparison:
    """보고 매출액 대비 비교 — 기존 `compare_reported_amount`를 그대로 쓴다.

    별도 허용오차(±1원, ±0.1%)를 두지 않는다. 현행 Finance 계약의 기본은
    정확한 항등이고, 이 wrapper는 키워드 호출만 얹는 얇은 층이다.
    """
    return compare_reported_amount(reported_amount_krw, recalculated_amount_krw)


def calculate_contribution_margin(
    *,
    sales_amount_krw: Decimal,
    sales_cost_basis_krw: Decimal,
) -> Decimal:
    """공헌이익 = 매출액 - 매출원가 기준액.

    원가 기준액은 이미 권위 있는 값으로 전달받는다 — 없을 때 0이나 추정치로
    대체하지 않는다(호출자가 아예 계산하지 않는다). 음수 공헌이익은 계산 사실로
    그대로 반환하며, 여기서 FAIL로 바꾸지 않는다.
    """
    if sales_amount_krw < 0:
        raise ValueError("sales_amount_krw must not be negative")
    if sales_cost_basis_krw < 0:
        raise ValueError("sales_cost_basis_krw must not be negative")
    return sales_amount_krw - sales_cost_basis_krw


def calculate_contribution_margin_rate(
    *,
    sales_amount_krw: Decimal,
    contribution_margin_krw: Decimal,
) -> Decimal | None:
    """공헌이익률 = 공헌이익 / 매출액. 매출액이 0이면 None(계산 불가)이다.

    0으로 나누지 않고, 계산할 수 없다는 사실을 None으로 명시한다 — 0.0 같은
    값을 지어내지 않는다. 반환값은 나눗셈 결과 그대로이며 표시용 반올림은
    호출자 몫이다(새 반올림 정책을 만들지 않는다).
    """
    if sales_amount_krw < 0:
        raise ValueError("sales_amount_krw must not be negative")
    if sales_amount_krw == 0:
        return None
    return contribution_margin_krw / sales_amount_krw


def calculate_collection_date(
    *,
    reference_date: date,
    payment_days: int,
) -> date:
    """회수일 = 기준일 + 결제일수(D+N).

    `reference_date`의 의미(납품일·송장일·계약일·출하일 중 무엇인지)는 호출자가
    가진다. Finance ↔ Master/Sales 계약에서 회수일 기준점이 아직 확정되지
    않았기 때문에, 이 함수는 의도적으로 기준점을 고르지 않는다.
    """
    if payment_days < 0:
        raise ValueError("payment_days must not be negative")
    return reference_date + timedelta(days=payment_days)


def build_sales_calculation_facts(
    *,
    quantity_kg: Decimal,
    unit_price_krw: Decimal,
    reported_amount_krw: Decimal | None = None,
    sales_cost_basis_krw: Decimal | None = None,
    reference_date: date | None = None,
    payment_days: int | None = None,
) -> SalesCalculationFacts:
    """위 원시 계산들을 Finance 내부 사실 묶음으로 모은다.

    없는 입력은 없는 채로 둔다 — 권위 있는 원가 기준액이 없으면 공헌이익을
    계산하지 않고, 회수일 기준점이나 결제일수가 없으면 회수일을 만들지 않는다.
    """
    sales_amount = calculate_sales_amount(
        quantity_kg=quantity_kg, unit_price_krw=unit_price_krw
    )

    comparison: ReportedAmountComparison | None = None
    if reported_amount_krw is not None:
        comparison = compare_reported_sales_amount(
            reported_amount_krw=reported_amount_krw,
            recalculated_amount_krw=sales_amount,
        )

    margin: Decimal | None = None
    margin_rate: Decimal | None = None
    if sales_cost_basis_krw is not None:
        margin = calculate_contribution_margin(
            sales_amount_krw=sales_amount,
            sales_cost_basis_krw=sales_cost_basis_krw,
        )
        margin_rate = calculate_contribution_margin_rate(
            sales_amount_krw=sales_amount,
            contribution_margin_krw=margin,
        )

    collection_date: date | None = None
    if reference_date is not None and payment_days is not None:
        collection_date = calculate_collection_date(
            reference_date=reference_date, payment_days=payment_days
        )

    return {
        "recalculated_sales_amount_krw": sales_amount,
        "reported_amount_comparison": comparison,
        "contribution_margin_krw": margin,
        "contribution_margin_rate": margin_rate,
        "collection_date": collection_date,
    }


# ---------------------------------------------------------------------------
# Sales Core Phase 2 — 판매 원가 기준 합성
#
#     sales_cost_basis
#     = 권위 있는 inventory_cost_basis 금액
#     + inventory_cost_basis 가 아직 품지 않은 검증된 직접비
#
# ★ 이 함수는 **후보를 고르지 않는다.** 어느 재고/매입 원가가 정본인지, 직접
#   물류비를 누가 소유하는지는 아직 도메인 간 계약이 없다. 선택은 바깥(권위 있는
#   입력을 만드는 쪽)이 하고, 여기서는 합성만 결정론적으로 한다.
# ---------------------------------------------------------------------------


def compose_sales_cost_basis(
    *,
    inventory_cost_basis: InventoryCostBasis | None,
    direct_costs: Sequence[VerifiedDirectCost] = (),
) -> SalesCostBasis | None:
    """재고원가에 아직 포함되지 않은 검증된 직접비만 더한다.

    권위 있는 재고원가가 없으면 ``None`` 이다 — 0으로 바꾸지 않고, 직접비만으로
    원가 기준을 만들지도 않는다. 뒤따르는 마진 계산은 원가 기준이 없으면 아예
    돌지 않는다(`build_sales_calculation_facts` 가 이미 그렇게 되어 있다).

    같은 `component` 가 직접비에 두 번 오면 fail closed 로 거절한다 — 어느 쪽이
    맞는지 Finance 가 조용히 고르면 그 선택이 곧 보이지 않는 정책이 된다.

    `cost_method` 가 UNKNOWN 이어도 여기서 막지 않는다 — 그 값을 판정에 쓸 수
    있는지는 **정책 판단**이라 Rule 계층 몫이다. 계산은 사실을 그대로 나르고,
    UNKNOWN 을 0으로 바꾸지 않는다.
    """
    components = [cost.component for cost in direct_costs]
    duplicated = sorted({name for name in components if components.count(name) > 1})
    if duplicated:
        raise ValueError(f"Duplicate direct cost components: {duplicated}")

    if inventory_cost_basis is None:
        return None

    already_included = tuple(
        cost.component
        for cost in direct_costs
        if cost.component in inventory_cost_basis.included_components
    )
    added = tuple(
        cost
        for cost in direct_costs
        if cost.component not in inventory_cost_basis.included_components
    )
    amount = sum((cost.amount_krw for cost in added), start=inventory_cost_basis.amount_krw)

    return SalesCostBasis(
        amount_krw=amount,
        inventory_amount_krw=inventory_cost_basis.amount_krw,
        inventory_cost_method=inventory_cost_basis.cost_method,
        inventory_source_ref=inventory_cost_basis.source_ref,
        inventory_evidence_grade=inventory_cost_basis.evidence_grade,
        added_direct_costs=added,
        already_included_components=already_included,
        included_components=(
            *inventory_cost_basis.included_components,
            *(cost.component for cost in added),
        ),
        source_refs=(
            inventory_cost_basis.source_ref,
            *(cost.source_ref for cost in added),
        ),
    )


# ---------------------------------------------------------------------------
# Sales Core Phase 3 — 판매 시나리오 현금흐름 오버레이
#
#     BASE     = 이미 확정된 Finance 현금 Event 만
#     SCENARIO = BASE + 제안된 판매 회수 유입
#
# ★ 제안 회수는 **확정 채권이 아니다.** BASE 에 섞이거나 실제 AR 로 적재되면
#   승인되지 않은 돈이 확정 현금처럼 읽힌다. 그래서 BASE 는 제안이 없을 때와
#   값이 같아야 하고, 두 투영은 같은 리스트를 공유하지 않는다.
#
# ★ 투영 엔진은 기존 `project_cashflow` 하나뿐이다 — 두 벌을 만들지 않는다.
# ---------------------------------------------------------------------------

PROPOSED_SALES_COLLECTION_EVENT_TYPE = "PROPOSED_SALES_COLLECTION"


def build_proposed_sales_collection_event(
    *,
    proposal_ref: str,
    collection_date: date,
    sales_amount_krw: Decimal,
    source_ref: str,
) -> CashEvent:
    """제안된 판매 회수를 유입 Event 하나로 만든다 (확정 채권이 아니다).

    회수일은 호출자가 `calculate_collection_date` 로 이미 구한 값을 넘긴다 —
    날짜 산술을 여기서 다시 구현하지 않는다.
    """
    if not proposal_ref.strip():
        raise ValueError("proposal_ref must not be blank")
    if not source_ref.strip():
        raise ValueError("source_ref must not be blank")
    if sales_amount_krw < 0:
        raise ValueError("sales_amount_krw must not be negative")
    return CashEvent(
        event_date=collection_date,
        event_type=PROPOSED_SALES_COLLECTION_EVENT_TYPE,
        amount_krw=sales_amount_krw,
        direction="INFLOW",
        ref_id=f"SALES-PROPOSAL:{proposal_ref}:{collection_date.isoformat()}",
        source_ref=source_ref,
    )


def project_sales_scenario_cashflow(
    *,
    as_of: date,
    current_cash_krw: Decimal,
    horizon_end: date,
    base_cash_events: Sequence[CashEvent],
    proposed_collection: CashEvent,
) -> SalesScenarioCashflow:
    """BASE 와 SCENARIO 를 각각 투영하고 둘을 나란히 보존한다.

    horizon 밖 회수일은 **날짜를 옮기지 않는다.** 현재 Finance 계약에 동적 horizon
    연장 규칙이 없으므로 7/14/30일 같은 연장을 지어내지 않고, horizon 밖이라는
    사실을 `collection_within_horizon=False` 로 드러낸다. 그때 SCENARIO 는 BASE 와
    같아지며 `depends_on_projected_inflow` 는 False 다 — 판정은 Rule 계층 몫이다.
    """
    if proposed_collection.event_type != PROPOSED_SALES_COLLECTION_EVENT_TYPE:
        raise ValueError("proposed_collection must be a PROPOSED_SALES_COLLECTION event")
    if proposed_collection.direction != "INFLOW":
        raise ValueError("proposed_collection must be an INFLOW event")
    if any(
        event.event_type == PROPOSED_SALES_COLLECTION_EVENT_TYPE for event in base_cash_events
    ):
        raise ValueError("base_cash_events must not contain a proposed sales collection")

    # 두 투영은 서로 다른 리스트를 본다 — 공유 리스트를 제자리에서 바꾸지 않는다.
    base_events = list(base_cash_events)
    scenario_events = [*base_cash_events, proposed_collection]

    base_projection = project_cashflow(
        as_of=as_of,
        current_cash_krw=current_cash_krw,
        horizon_end=horizon_end,
        cash_events=base_events,
    )
    scenario_projection = project_cashflow(
        as_of=as_of,
        current_cash_krw=current_cash_krw,
        horizon_end=horizon_end,
        cash_events=scenario_events,
    )

    return SalesScenarioCashflow(
        base_projection=base_projection,
        scenario_projection=scenario_projection,
        base_projected_cash_min=base_projection.projected_cash_min,
        base_projected_cash_min_date=base_projection.projected_cash_min_date,
        scenario_projected_cash_min=scenario_projection.projected_cash_min,
        scenario_projected_cash_min_date=scenario_projection.projected_cash_min_date,
        collection_date=proposed_collection.event_date,
        collection_amount_krw=proposed_collection.amount_krw,
        proposed_collection_ref_id=proposed_collection.ref_id,
        collection_within_horizon=as_of < proposed_collection.event_date <= horizon_end,
        depends_on_projected_inflow=(
            scenario_projection.projected_cash_min > base_projection.projected_cash_min
        ),
    )


# ---------------------------------------------------------------------------
# Sales Core Phase 5 — 매출채권 · 여신 산술
#
# ★ 채권 원장(`receivables`)은 실재한다. **여신한도는 실재하지 않는다** — 저장소
#   어디에도 credit_limit 이 없다. 그래서 여기서는 한도가 주어졌을 때의 산술만
#   두고, 한도를 회사 현금·판매이력·마진에서 역산하지 않는다.
# ---------------------------------------------------------------------------


def summarize_partner_receivables(
    *,
    partner_id: str,
    as_of: date,
    receivables: Sequence[PartnerReceivable],
) -> PartnerReceivableFacts:
    """거래처 채권 잔액과 연체 잔액을 집계한다 (사실만, 판정 없음).

    빈 목록은 **채권이 없다는 사실**이다 — 신규 거래처를 자료 미비로 취급하지
    않는다. 연체는 `due_date < as_of` 인 미회수 채권으로만 정의한다.
    """
    if not partner_id.strip():
        raise ValueError("partner_id must not be blank")
    open_items = [item for item in receivables if item.status in OPEN_RECEIVABLE_STATUSES]
    overdue_items = [item for item in open_items if item.due_date < as_of]
    return PartnerReceivableFacts(
        partner_id=partner_id,
        as_of=as_of,
        current_ar_krw=sum(
            (item.outstanding_amount_krw for item in open_items), start=Decimal(0)
        ),
        overdue_ar_krw=sum(
            (item.outstanding_amount_krw for item in overdue_items), start=Decimal(0)
        ),
        open_receivable_count=len(open_items),
        overdue_receivable_count=len(overdue_items),
        source_refs=tuple(item.source_ref for item in open_items),
    )


def calculate_available_credit(
    *,
    credit_limit_krw: Decimal,
    current_partner_ar_krw: Decimal,
) -> Decimal:
    """여신 여력 = 여신한도 - 현재 거래처 채권.

    음수를 0으로 깎지 않는다 — 한도를 이미 넘긴 상태는 그 자체로 드러나야 하는
    사실이고, 0으로 만들면 "딱 맞게 찼다"와 구분되지 않는다.
    """
    if credit_limit_krw < 0:
        raise ValueError("credit_limit_krw must not be negative")
    if current_partner_ar_krw < 0:
        raise ValueError("current_partner_ar_krw must not be negative")
    return credit_limit_krw - current_partner_ar_krw


def calculate_projected_partner_ar(
    *,
    current_partner_ar_krw: Decimal,
    proposed_sales_amount_krw: Decimal,
) -> Decimal:
    """제안이 성사됐을 때의 거래처 채권 = 현재 채권 + 제안 매출액."""
    if current_partner_ar_krw < 0:
        raise ValueError("current_partner_ar_krw must not be negative")
    if proposed_sales_amount_krw < 0:
        raise ValueError("proposed_sales_amount_krw must not be negative")
    return current_partner_ar_krw + proposed_sales_amount_krw
