from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.logistics.schemas import (
    LogisticsProcurementResponse,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScenarioAdjustment,
)


def _procurement_response(**overrides) -> LogisticsProcurementResponse:
    base = {
        "as_of": "2026-08-21",
        "snapshot_id": None,
        "runtime_status": "READY",
        "verdict": "PASS",
        "band": {"cap_by_date": {}},
        "inbound_constraints": {
            "inbound_lead_days": 2,
            "daily_inbound_capacity_kg": None,
            "inbound_transport_capacity_kg": None,
        },
        "hard_constraints": [],
        "soft_warnings": [],
        "evidences": [],
    }
    return LogisticsProcurementResponse(**{**base, **overrides})


def test_logistics_procurement_accepts_purchase_v04(logistics_purchase_payload):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    assert request.scenarios[0].total_qty_kg == 4500
    assert request.scenarios[0].split_plan[0].qty_kg == 4500


def test_logistics_procurement_rejects_legacy_ton_fields(logistics_purchase_payload):
    scenario = logistics_purchase_payload["scenarios"][0]
    scenario["total_quantity_ton"] = scenario.pop("total_qty_kg")

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(logistics_purchase_payload)


def test_logistics_procurement_rejects_duplicate_kg_names(logistics_purchase_payload):
    scenario = logistics_purchase_payload["scenarios"][0]
    scenario["total_quantity_kg"] = scenario.pop("total_qty_kg")
    scenario["split_plan"][0]["quantity_kg"] = scenario["split_plan"][0].pop("qty_kg")

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(logistics_purchase_payload)


def test_logistics_sales_contract_keeps_approved_quantity_decimal(logistics_sales_payload):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    assert request.approved_purchase.total_qty_kg == Decimal(4500)


def test_logistics_sales_rejects_sales_agent_candidates(logistics_sales_payload):
    logistics_sales_payload["candidates"] = [{"channel": "KIMCHI_FACTORY"}]

    with pytest.raises(ValidationError):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


def test_logistics_sales_rejects_arrival_total_mismatch(logistics_sales_payload):
    logistics_sales_payload["approved_purchase"]["total_qty_kg"] = 4000

    with pytest.raises(ValidationError):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


def test_logistics_sales_rejects_arrival_before_as_of(logistics_sales_payload):
    logistics_sales_payload["approved_purchase"]["expected_arrival_date"] = "2026-08-20"
    logistics_sales_payload["approved_purchase"]["arrival_schedule"][0]["date"] = "2026-08-20"

    with pytest.raises(ValidationError, match="on or after as_of"):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


@pytest.mark.parametrize("arrival_date", ["2026-08-21", "2026-08-23"])
def test_logistics_sales_accepts_arrival_on_or_after_as_of(logistics_sales_payload, arrival_date):
    logistics_sales_payload["approved_purchase"]["expected_arrival_date"] = arrival_date
    logistics_sales_payload["approved_purchase"]["arrival_schedule"][0]["date"] = arrival_date

    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    assert request.approved_purchase.expected_arrival_date.isoformat() == arrival_date


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("approved_purchase", "total_qty_kg"), True),
        (("approved_purchase", "arrival_schedule", 0, "quantity_kg"), True),
    ],
)
def test_logistics_sales_rejects_boolean_numbers(logistics_sales_payload, path, value):
    target = logistics_sales_payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


def test_uncomputable_inventory_by_item_is_omitted_not_empty():
    """None(계산 불가)은 키 생략, [](0건 확인)은 그대로 나간다 — 0 != null."""
    omitted = _procurement_response(inventory_by_item=None).model_dump(mode="json")
    kept = _procurement_response(inventory_by_item=[]).model_dump(mode="json")

    assert "inventory_by_item" not in omitted
    assert kept["inventory_by_item"] == []


def test_inventory_by_item_round_trips():
    response = _procurement_response(inventory_by_item=[{"item": "배추", "available_qty_kg": 3000}])

    assert response.inventory_by_item is not None
    assert response.inventory_by_item[0].available_qty_kg == Decimal(3000)


def test_scenario_adjustment_allows_only_quantity_and_timing():
    """물류 Adjustment 축은 quantity/timing뿐 — amount/channel_mix 금지."""
    ScenarioAdjustment(axis="quantity", split_date=date(2026, 8, 21))
    ScenarioAdjustment(axis="timing", split_date=date(2026, 8, 21))
    for forbidden in ("amount", "channel_mix"):
        with pytest.raises(ValidationError):
            ScenarioAdjustment(axis=forbidden, split_date=date(2026, 8, 21))
