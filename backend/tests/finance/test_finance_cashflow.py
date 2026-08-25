from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.scenario_engine import run_finance_procurement_scenario
from app.finance.schemas import (
    CashEvent,
    FinanceRuntimeContext,
    FinanceSalesRequest,
    PurchaseAgentOutput,
)
from app.finance.service import run_finance_procurement_with_context, run_finance_sales_with_context
from app.finance.tools import (
    build_debt_service_schedule,
    build_payroll_schedule,
    calculate_finance_cap,
    derive_cash_priority,
    project_cashflow,
)


def _event(day, amount, direction="OUTFLOW", ref="E1"):
    return CashEvent(
        event_date=day,
        event_type="COMMITTED_OUTFLOW" if direction == "OUTFLOW" else "RECEIVABLE",
        amount_krw=Decimal(amount),
        direction=direction,
        ref_id=ref,
    )


def test_cashflow_no_event_keeps_current_cash_and_as_of_minimum():
    projection = project_cashflow(
        as_of=date(2025, 12, 31),
        current_cash_krw=Decimal("10.25"),
        horizon_end=date(2026, 1, 30),
        cash_events=(),
    )
    assert projection.projected_cash_min == Decimal("10.25")
    assert projection.projected_cash_min_date == date(2025, 12, 31)


def test_cashflow_orders_dates_sums_same_date_and_excludes_horizon_outside():
    as_of = date(2025, 12, 31)
    events = [
        _event(as_of + timedelta(days=3), "0.03", ref="out"),
        _event(as_of + timedelta(days=1), "0.01", "INFLOW", "in1"),
        _event(as_of + timedelta(days=1), "0.02", "INFLOW", "in2"),
        _event(as_of + timedelta(days=31), "999", ref="outside"),
    ]
    before = [event.model_dump() for event in events]
    projection = project_cashflow(
        as_of=as_of,
        current_cash_krw=Decimal("1.00"),
        horizon_end=as_of + timedelta(days=30),
        cash_events=events,
    )
    assert [point.projection_date for point in projection.projected_cash_by_date] == [
        as_of,
        as_of + timedelta(days=1),
        as_of + timedelta(days=3),
    ]
    assert projection.projected_cash_by_date[-1].cash_balance_krw == Decimal("1.00")
    assert [event.model_dump() for event in events] == before


def test_payroll_schedule_handles_month_boundary_today_and_policy_amount(finance_policy):
    events = build_payroll_schedule(
        as_of=date(2025, 12, 31), horizon_end=date(2026, 1, 30), policy=finance_policy
    )
    assert [(item.event_date, item.amount_krw) for item in events] == [
        (date(2026, 1, 25), Decimal(12941280))
    ]
    changed = finance_policy.model_copy(update={"monthly_labor_cost_krw": Decimal(1)})
    assert (
        build_payroll_schedule(
            as_of=date(2026, 1, 25), horizon_end=date(2026, 2, 24), policy=changed
        )
        == ()
    )


@pytest.mark.parametrize(
    ("cash", "expected"),
    [
        ("12941279.999999", "HIGH"),
        ("12941280", "MEDIUM"),
        ("15000000", "MEDIUM"),
        ("19411920", "LOW"),
        ("20000000", "LOW"),
    ],
)
def test_cash_priority_decimal_boundaries(finance_policy, cash, expected):
    assert derive_cash_priority(projected_cash_min=Decimal(cash), policy=finance_policy) == expected


@pytest.mark.parametrize("minimum", [None, Decimal(0), Decimal(-1)])
def test_cash_priority_invalid_minimum_fails_closed(finance_policy, minimum):
    invalid = finance_policy.model_copy(update={"minimum_cash_balance_krw": minimum})
    with pytest.raises((TypeError, ValueError)):
        derive_cash_priority(projected_cash_min=Decimal(1), policy=invalid)


def test_cash_priority_unsupported_reference_fails_closed(finance_policy):
    invalid = finance_policy.model_copy(update={"cash_priority_reference": "legacy"})
    with pytest.raises(ValueError, match="Unsupported"):
        derive_cash_priority(projected_cash_min=Decimal(1), policy=invalid)


def test_finance_cap_is_monotonic_and_legacy_limit_independent(finance_context, purchase_payload):
    request = PurchaseAgentOutput.model_validate(purchase_payload)
    result = run_finance_procurement_scenario(request, finance_context)
    projection = result["base_projection"]
    assert projection is not None
    cap = calculate_finance_cap(base_projection=projection, policy=finance_context.policy)
    assert cap == Decimal(6111353)
    assert cap != finance_context.snapshot.financial_limit_krw


def test_finance_band_is_candidate_independent(finance_context, purchase_payload):
    first = PurchaseAgentOutput.model_validate(purchase_payload)
    purchase_payload["scenarios"][0]["sourcing_plan"][0]["grade_unit_price"] = 1600
    purchase_payload["scenarios"][0]["sourcing_plan"][1]["grade_unit_price"] = 1400
    purchase_payload["scenarios"][0]["total_amount_krw"] = 6900000
    second = PurchaseAgentOutput.model_validate(purchase_payload)
    assert run_finance_procurement_with_context(first, finance_context).band == (
        run_finance_procurement_with_context(second, finance_context).band
    )


def test_fixed_context_never_reads_db(finance_context, purchase_payload, sales_payload):
    purchase = PurchaseAgentOutput.model_validate(purchase_payload)
    sales_payload["approved_purchase"]["payment_date"] = "2026-01-07"
    sales = FinanceSalesRequest.model_validate(sales_payload)
    with patch(
        "app.finance.repository.get_current_finance_runtime_context",
        side_effect=AssertionError("DB must not be read"),
    ):
        run_finance_procurement_with_context(purchase, finance_context)
        run_finance_sales_with_context(sales, finance_context)


def test_unresolved_source_fails_closed(finance_context, purchase_payload):
    context = FinanceRuntimeContext(
        snapshot=finance_context.snapshot,
        policy=finance_context.policy,
        cash_events=(),
        unresolved_sources=("DEBT_SERVICE",),
    )
    response = run_finance_procurement_with_context(
        PurchaseAgentOutput.model_validate(purchase_payload), context
    )
    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.hard_constraints == ["CASH_EVENT_SOURCE_UNRESOLVED"]


def test_debt_schedule_has_36_grace_and_36_equal_principal_periods(finance_debt_policy):
    before = finance_debt_policy.model_dump()
    events = build_debt_service_schedule(
        debt_policy=finance_debt_policy,
        as_of=date(2025, 12, 1),
        horizon_end=date(2031, 11, 30),
    )
    assert len(events) == 72
    assert events[0].event_date == date(2025, 12, 31)
    assert all(event.principal_component_krw == 0 for event in events[:36])
    assert events[36].event_date == date(2028, 12, 31)
    assert events[36].principal_component_krw == Decimal("1257558.449569")
    assert events[35].interest_component_krw == Decimal("94316.883718")
    assert events[36].interest_component_krw == Decimal("94316.883718")
    assert events[37].interest_component_krw < events[36].interest_component_krw
    assert sum(event.principal_component_krw for event in events) == Decimal("45272104.184486")
    assert events[-1].principal_component_krw == Decimal("1257558.449571")
    assert finance_debt_policy.model_dump() == before


@pytest.mark.parametrize(
    ("execution_date", "expected"),
    [
        (date(2026, 2, 2), date(2026, 2, 28)),
        (date(2028, 2, 2), date(2028, 2, 29)),
    ],
)
def test_debt_schedule_month_end_handles_february(finance_debt_policy, execution_date, expected):
    policy = finance_debt_policy.model_copy(update={"debt_execution_date": execution_date})
    events = build_debt_service_schedule(
        debt_policy=policy,
        as_of=execution_date - timedelta(days=1),
        horizon_end=expected,
    )
    assert [event.event_date for event in events] == [expected]


def test_debt_schedule_excludes_as_of_and_outside_horizon(finance_debt_policy):
    assert (
        build_debt_service_schedule(
            debt_policy=finance_debt_policy,
            as_of=date(2025, 12, 31),
            horizon_end=date(2026, 1, 30),
        )
        == ()
    )
    events = build_debt_service_schedule(
        debt_policy=finance_debt_policy,
        as_of=date(2025, 12, 31),
        horizon_end=date(2026, 1, 31),
    )
    assert [event.event_date for event in events] == [date(2026, 1, 31)]
    assert events[0].principal_component_krw == 0
    assert isinstance(events[0].amount_krw, Decimal)
