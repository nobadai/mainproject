from datetime import date
from decimal import Decimal

import pytest

from app.logistics.rules import (
    derive_logistics_verdict,
    evaluate_procurement_rules,
    evaluate_sales_rules,
)
from app.logistics.schemas import (
    InTransitItem,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScheduledQuantity,
)
from app.logistics.tools import (
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_expected_arrival_dates,
    calculate_future_occupancy_by_date,
    is_inbound_schedule_complete,
    overlay_approved_purchase,
)


def test_expected_arrival_date_uses_canonical_kg_contract(logistics_purchase_payload):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    assert request.scenarios[0].total_quantity_kg == Decimal(4500)
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

    assert calculate_cap_by_date(snapshot, [date(2026, 8, 23)]) == {date(2026, 8, 23): expected}


def test_unresolved_capacity_is_not_zero_or_unlimited(unresolved_logistics_snapshot):
    snapshot = unresolved_logistics_snapshot.model_copy(
        update={
            "in_transit": [],
            "confirmed_inbound_schedule": [],
            "confirmed_outbound_schedule": [],
        }
    )
    with pytest.raises(ValueError, match="LOGISTICS_CAPACITY_INPUT_MISSING"):
        calculate_cap_by_date(snapshot, [date(2026, 8, 23)])


def test_procurement_rule_keeps_unresolved_constraints_null(unresolved_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 21),
        snapshot=unresolved_logistics_snapshot,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["calculation_ready"] is False
    assert derive_logistics_verdict(result) is None
    assert all(item.status == "UNRESOLVED" for item in result["hard_constraints"])
    assert "PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT" in result["soft_warnings"]


def test_procurement_rule_can_be_ready_without_zone_capacity(complete_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 21),
        snapshot=complete_logistics_snapshot,
    )

    assert result["runtime_status"] == "READY"
    zone = next(item for item in result["hard_constraints"] if item.code == "LOG-H02")
    assert zone.status == "UNRESOLVED"
    assert derive_logistics_verdict(result) == "REVIEW_REQUIRED"


def test_in_transit_none_blocks_procurement_and_sales(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(update={"in_transit": None})

    procurement = evaluate_procurement_rules(as_of=date(2026, 8, 21), snapshot=snapshot)
    sales = evaluate_sales_rules(
        as_of=date(2026, 8, 21),
        snapshot=snapshot,
        future_occupancy_by_date={date(2026, 8, 23): Decimal(5500)},
    )

    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"
    assert sales["runtime_status"] == "RUNTIME_NOT_READY"
    for result in (procurement, sales):
        constraint = next(
            item
            for item in result["hard_constraints"]
            if item.code == "IN_TRANSIT_SCHEDULE_UNRESOLVED"
        )
        assert constraint.skip_reason == "IN_TRANSIT_UNRESOLVED"


def test_empty_in_transit_is_known_zero(complete_logistics_snapshot):
    assert is_inbound_schedule_complete(complete_logistics_snapshot) is True

    procurement = evaluate_procurement_rules(
        as_of=date(2026, 8, 21), snapshot=complete_logistics_snapshot
    )
    inbound = overlay_approved_purchase(
        complete_logistics_snapshot,
        LogisticsSalesRequest.model_validate(
            {
                "cycle": "SALES",
                "as_of": "2026-08-21",
                "approved_purchase": {
                    "approval_id": "H1-KNOWN-ZERO",
                    "total_qty_kg": 500,
                    "expected_arrival_date": "2026-08-23",
                    "arrival_schedule": [{"date": "2026-08-23", "quantity_kg": 500}],
                },
            }
        ).approved_purchase,
    )

    assert procurement["runtime_status"] == "READY"
    assert inbound is not None
    assert sum((item.quantity_kg for item in inbound), start=Decimal(0)) == Decimal(500)


def test_nonempty_in_transit_without_identifiers_fails_closed_without_double_counting(
    complete_logistics_snapshot, logistics_sales_payload
):
    transit = InTransitItem(
        item="배추",
        quantity_kg=Decimal(4500),
        expected_arrival_date=date(2026, 8, 23),
    )
    confirmed = ScheduledQuantity(
        item="배추",
        quantity_kg=Decimal(4500),
        date=date(2026, 8, 23),
    )
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "in_transit": [transit],
            "confirmed_inbound_schedule": [confirmed],
        }
    )
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    assert is_inbound_schedule_complete(snapshot) is False
    assert overlay_approved_purchase(snapshot, request.approved_purchase) is None
    assert calculate_future_occupancy_by_date(snapshot, []) is None
    with pytest.raises(ValueError, match="IN_TRANSIT_SCHEDULE_UNRESOLVED"):
        calculate_cap_by_date(snapshot, [date(2026, 8, 23)])

    procurement = evaluate_procurement_rules(as_of=date(2026, 8, 21), snapshot=snapshot)
    sales = evaluate_sales_rules(
        as_of=date(2026, 8, 21),
        snapshot=snapshot,
        future_occupancy_by_date=None,
    )
    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"
    assert sales["runtime_status"] == "RUNTIME_NOT_READY"
    for result in (procurement, sales):
        constraint = next(
            item
            for item in result["hard_constraints"]
            if item.code == "IN_TRANSIT_SCHEDULE_UNRESOLVED"
        )
        assert constraint.skip_reason == "IN_TRANSIT_SCHEDULE_DEDUPLICATION_UNRESOLVED"


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
    assert warehouse.status == "FAIL"
    assert result["runtime_status"] == "READY"
    assert derive_logistics_verdict(result) == "FAIL"


def test_sales_rule_all_pass_aggregates_to_pass(complete_logistics_snapshot):
    result = evaluate_sales_rules(
        as_of=date(2026, 8, 21),
        snapshot=complete_logistics_snapshot,
        future_occupancy_by_date={date(2026, 8, 23): Decimal(5500)},
    )

    assert {item.status for item in result["hard_constraints"]} == {"PASS"}
    assert derive_logistics_verdict(result) == "PASS"


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
    assert n17.status == "UNRESOLVED"


def test_logistics_rules_fail_closed_on_as_of_mismatch(complete_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 22),
        snapshot=complete_logistics_snapshot,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["hard_constraints"][0].code == "AS_OF_MISMATCH"
    assert derive_logistics_verdict(result) is None
