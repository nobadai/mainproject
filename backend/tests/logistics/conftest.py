from datetime import date
from decimal import Decimal

import pytest

from app.logistics.schemas import InventoryLogisticsSnapshot


@pytest.fixture
def logistics_purchase_payload() -> dict[str, object]:
    return {
        "meta": {
            "as_of": "2026-08-21",
            "item": "배추",
            "agent_version": "v0.4",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": [
            {
                "label": "기본",
                "strategy_type": "quantity",
                "coverage_days": 5,
                "total_quantity_kg": 4500,
                "total_amount_krw": 7125000,
                "split_plan": [{"seq": 1, "date": "2026-08-21", "quantity_kg": 4500}],
                "sourcing_plan": [
                    {
                        "market": "가락",
                        "grade": "상",
                        "quantity_kg": 3000,
                        "grade_unit_price": 1650,
                    },
                    {
                        "market": "가락",
                        "grade": "중",
                        "quantity_kg": 1500,
                        "grade_unit_price": 1450,
                    },
                ],
            }
        ],
    }


@pytest.fixture
def logistics_sales_payload() -> dict[str, object]:
    return {
        "cycle": "SALES",
        "as_of": "2026-08-21",
        "approved_purchase": {
            "approval_id": "H1-20260821-001",
            "total_qty_kg": 4500,
            "expected_arrival_date": "2026-08-23",
            "arrival_schedule": [{"date": "2026-08-23", "quantity_kg": 4500}],
        },
    }


@pytest.fixture
def complete_logistics_snapshot() -> InventoryLogisticsSnapshot:
    return InventoryLogisticsSnapshot(
        snapshot_id="T0-20260821-001",
        as_of=date(2026, 8, 21),
        on_hand_by_lot=[
            {
                "lot_id": "LOT-001",
                "item": "배추",
                "available_qty_kg": 1000,
                "remaining_freshness_days": 8,
                "status": "ACTIVE",
                "storage_zone": "COLD_HUMID",
            }
        ],
        in_transit=[],
        confirmed_inbound_schedule=[],
        confirmed_outbound_schedule=[],
        used_capacity_kg=Decimal(1000),
        guaranteed_capacity_kg=Decimal(8000),
        burst_capacity_kg=Decimal(2000),
        guaranteed_capacity_by_zone_kg=None,
        inbound_lead_days=2,
        daily_inbound_capacity_kg=Decimal(3000),
        inbound_transport_capacity_kg=Decimal(2500),
        shared_daily_outbound_capacity_kg=Decimal(1000),
        evidence_refs=["FIXTURE:T0-20260821-001"],
    )


@pytest.fixture
def unresolved_logistics_snapshot(
    complete_logistics_snapshot,
) -> InventoryLogisticsSnapshot:
    return complete_logistics_snapshot.model_copy(
        update={
            "snapshot_id": None,
            "in_transit": None,
            "confirmed_inbound_schedule": None,
            "confirmed_outbound_schedule": None,
            "guaranteed_capacity_kg": None,
            "burst_capacity_kg": None,
            "inbound_lead_days": None,
            "daily_inbound_capacity_kg": None,
            "inbound_transport_capacity_kg": None,
            "shared_daily_outbound_capacity_kg": None,
            "evidence_refs": ["DB:logistics_contracts/LOGI-BASE-5PL:provisional=true"],
        }
    )
