from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.logistics.scenario_engine import (
    run_logistics_procurement_scenario,
    run_logistics_sales_scenario,
)
from app.logistics.schemas import (
    InTransitItem,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
)
from app.logistics.service import (
    run_logistics_procurement_with_snapshot,
    run_logistics_sales_with_snapshot,
)


def test_logistics_procurement_scenario_is_deterministic(
    complete_logistics_snapshot,
    logistics_purchase_payload,
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    first = run_logistics_procurement_scenario(request, complete_logistics_snapshot)
    second = run_logistics_procurement_scenario(request, complete_logistics_snapshot)

    assert first == second
    assert first["expected_arrival_dates"] == [date(2026, 8, 23)]
    assert first["cap_by_date"] == {date(2026, 8, 23): Decimal(2500)}


def test_logistics_external_snapshot_bypasses_repository_and_round_trips_id(
    complete_logistics_snapshot,
    logistics_purchase_payload,
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    with patch(
        "app.logistics.service.get_current_inventory_logistics_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_logistics_procurement_with_snapshot(
            request,
            complete_logistics_snapshot,
        )

    assert response.snapshot_id == "T0-20260821-001"
    assert response.runtime_status == "READY"


def test_logistics_sales_engine_keeps_h1_future_and_on_hand_unchanged(
    complete_logistics_snapshot,
    logistics_sales_payload,
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    before = complete_logistics_snapshot.model_dump()

    first = run_logistics_sales_scenario(request, complete_logistics_snapshot)
    second = run_logistics_sales_scenario(request, complete_logistics_snapshot)
    with patch(
        "app.logistics.service.get_current_inventory_logistics_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_logistics_sales_with_snapshot(
            request,
            complete_logistics_snapshot,
        )

    assert first == second
    assert first["future_occupancy_by_date"] == {date(2026, 8, 23): Decimal(5500)}
    assert [item.lot_id for item in first["lot_constraints"]] == ["LOT-001"]
    assert response.snapshot_id == "T0-20260821-001"
    assert complete_logistics_snapshot.model_dump() == before


def test_logistics_engine_preserves_inbound_completeness_fail_closed(
    complete_logistics_snapshot,
    logistics_sales_payload,
):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "in_transit": [
                InTransitItem(
                    item="배추",
                    quantity_kg=Decimal(4500),
                    expected_arrival_date=date(2026, 8, 23),
                )
            ]
        }
    )
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    result = run_logistics_sales_scenario(request, snapshot)
    response = run_logistics_sales_with_snapshot(request, snapshot)

    assert result["inbound_schedule"] is None
    assert result["future_occupancy_by_date"] is None
    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert any(item.code == "IN_TRANSIT_SCHEDULE_UNRESOLVED" for item in response.hard_constraints)
