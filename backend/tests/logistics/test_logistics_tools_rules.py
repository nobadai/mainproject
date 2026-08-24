from datetime import date
from decimal import Decimal

import pytest

from app.logistics.rules import evaluate_procurement_rules, evaluate_sales_rules
from app.logistics.schemas import LogisticsSalesRequest, PurchaseAgentOutput
from app.logistics.tools import (
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_expected_arrival_dates,
    calculate_future_occupancy_by_date,
    overlay_approved_purchase,
    ton_to_kg,
)


def test_ton_to_kg_and_expected_arrival_date(logistics_purchase_payload):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    assert ton_to_kg(request.scenarios[0].total_quantity_ton) == Decimal(4500)
    assert calculate_expected_arrival_dates(request, 2) == [date(2026, 8, 23)]


def test_cap_by_date_uses_minimum_confirmed_capacity(complete_logistics_snapshot):
    result = calculate_cap_by_date(complete_logistics_snapshot, [date(2026, 8, 23)])

    assert result == {date(2026, 8, 23): Decimal(2500)}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("daily_inbound_capacity_kg", Decimal(1200), Decimal(1200)),
        ("inbound_transport_capacity_kg", Decimal(900), Decimal(900)),
        ("guaranteed_capacity_kg", Decimal(1500), Decimal(500)),
    ],
)
def test_cap_by_date_respects_each_hard_capacity(
    complete_logistics_snapshot, field, value, expected
):
    snapshot = complete_logistics_snapshot.model_copy(update={field: value})

    assert calculate_cap_by_date(snapshot, [date(2026, 8, 23)]) == {
        date(2026, 8, 23): expected
    }


def test_unresolved_capacity_is_not_zero_or_unlimited(unresolved_logistics_snapshot):
    with pytest.raises(ValueError, match="LOGISTICS_CAPACITY_INPUT_MISSING"):
        calculate_cap_by_date(unresolved_logistics_snapshot, [date(2026, 8, 23)])


def test_procurement_rule_keeps_unresolved_constraints_null(unresolved_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 21),
        snapshot=unresolved_logistics_snapshot,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["calculation_ready"] is False
    assert all(item.passed is None for item in result["hard_constraints"])
    assert "PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT" in result["soft_warnings"]


def test_procurement_rule_can_be_ready_without_zone_capacity(complete_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 21),
        snapshot=complete_logistics_snapshot,
    )

    assert result["runtime_status"] == "READY"
    zone = next(item for item in result["hard_constraints"] if item.code == "LOG-H02")
    assert zone.passed is None


def test_h1_overlay_stays_future_and_does_not_change_on_hand(
    complete_logistics_snapshot, logistics_sales_payload
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    original_lots = build_lot_constraints(complete_logistics_snapshot)

    inbound = overlay_approved_purchase(complete_logistics_snapshot, request.approved_purchase)
    assert inbound is not None
    occupancy = calculate_future_occupancy_by_date(complete_logistics_snapshot, inbound)

    assert occupancy == {date(2026, 8, 23): Decimal(5500)}
    assert build_lot_constraints(complete_logistics_snapshot) == original_lots
    assert len(original_lots) == 1


def test_sales_rule_marks_warehouse_over_capacity(complete_logistics_snapshot):
    result = evaluate_sales_rules(
        as_of=date(2026, 8, 21),
        snapshot=complete_logistics_snapshot,
        future_occupancy_by_date={date(2026, 8, 23): Decimal(9000)},
    )

    warehouse = next(item for item in result["hard_constraints"] if item.code == "LOG-H01")
    assert warehouse.passed is False


def test_sales_rule_requires_n17(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={"shared_daily_outbound_capacity_kg": None}
    )
    result = evaluate_sales_rules(
        as_of=date(2026, 8, 21),
        snapshot=snapshot,
        future_occupancy_by_date={date(2026, 8, 23): Decimal(5500)},
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    n17 = next(item for item in result["hard_constraints"] if item.code == "N17")
    assert n17.passed is None


def test_logistics_rules_fail_closed_on_as_of_mismatch(complete_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 22),
        snapshot=complete_logistics_snapshot,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["hard_constraints"][0].code == "AS_OF_MISMATCH"
