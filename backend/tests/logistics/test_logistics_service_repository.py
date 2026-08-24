from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from psycopg import OperationalError

from app.logistics.repository import get_current_inventory_logistics_snapshot
from app.logistics.schemas import LogisticsSalesRequest, PurchaseAgentOutput
from app.logistics.service import run_logistics_procurement, run_logistics_sales


def test_repository_excludes_provisional_capacity():
    dashboard = {"as_of": "2026-08-21", "used_capacity_kg": Decimal(375)}
    capacity = {
        "guaranteed_capacity_plt": Decimal(8),
        "effective_kg_per_pallet": Decimal(800),
        "equivalent_capacity_ton": Decimal("6.4"),
        "used_capacity_kg": Decimal(375),
    }
    contract = {
        "logistics_contract_id": "LOGI-BASE-5PL",
        "guaranteed_capacity_plt": Decimal(8),
        "effective_kg_per_pallet": Decimal(800),
        "equivalent_capacity_ton": Decimal("6.4"),
        "contract_status": "BASELINE_ONLY",
        "provisional": True,
    }
    with (
        patch("app.logistics.repository.get_db_schema", return_value="haetdeul"),
        patch(
            "app.logistics.repository.fetch_one",
            side_effect=[dashboard, capacity],
        ),
        patch("app.logistics.repository.fetch_all", side_effect=[[], [contract]]),
    ):
        snapshot = get_current_inventory_logistics_snapshot()

    assert snapshot.guaranteed_capacity_kg is None
    assert snapshot.used_capacity_kg == Decimal(375)


def test_logistics_a_ready_response_and_persistence(
    complete_logistics_snapshot, logistics_purchase_payload
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=complete_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_procurement(request)

    assert response.runtime_status == "READY"
    assert response.snapshot_id == "T0-20260821-001"
    assert response.band.cap_by_date == {date(2026, 8, 23): Decimal(2500)}
    saved = save_run.call_args.kwargs
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["runtime_status"] == "READY"
    assert saved["snapshot_id"] == "T0-20260821-001"
    assert saved["request_payload"]["scenarios"][0]["total_quantity_ton"] == "4.5"


def test_logistics_a_unresolved_response_is_saved(
    unresolved_logistics_snapshot, logistics_purchase_payload
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=unresolved_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_procurement(request)

    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.band.cap_by_date == {}
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"


def test_logistics_b_keeps_h1_out_of_on_hand_and_saves_run(
    complete_logistics_snapshot, logistics_sales_payload
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=complete_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_sales(request)

    assert response.runtime_status == "READY"
    assert response.approval_id == "H1-20260821-001"
    assert response.daily_outbound_capacity_kg == Decimal(1000)
    assert [item.lot_id for item in response.lot_constraints] == ["LOT-001"]
    assert save_run.call_args.kwargs["cycle"] == "SALES"


def test_logistics_b_unresolved_n17_is_saved(
    unresolved_logistics_snapshot, logistics_sales_payload
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=unresolved_logistics_snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_sales(request)

    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.daily_outbound_capacity_kg is None
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"


def test_logistics_persistence_failure_is_not_runtime_warning(
    complete_logistics_snapshot, logistics_purchase_payload
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=complete_logistics_snapshot,
        ),
        patch(
            "app.logistics.service.save_logistics_agent_run",
            side_effect=OperationalError("persistence unavailable"),
        ),
        pytest.raises(OperationalError, match="persistence unavailable"),
    ):
        run_logistics_procurement(request)
