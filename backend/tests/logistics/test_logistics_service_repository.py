from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from app.logistics.repository import (
    get_active_logistics_policy,
    get_current_inventory_logistics_snapshot,
)
from app.logistics.schemas import LogisticsSalesRequest, PurchaseAgentOutput
from app.logistics.service import run_logistics_procurement, run_logistics_sales


def _policy_rows() -> list[dict[str, object]]:
    values = {
        "guaranteed_capacity_kg": ("NUMERIC", Decimal(8000)),
        "burst_capacity_kg": ("NUMERIC", Decimal(9600)),
        "inbound_lead_days": ("NUMERIC", Decimal(2)),
        "daily_inbound_capacity_kg": ("NUMERIC", Decimal(5000)),
        "inbound_transport_capacity_kg": ("NUMERIC", Decimal(5000)),
        "shared_daily_outbound_capacity_kg": ("NUMERIC", Decimal(5000)),
        "cap_by_date_policy": ("TEXT", "CONFIRMED_ONLY"),
    }
    return [
        {
            "policy_key": key,
            "value_kind": kind,
            "value_numeric": value if kind == "NUMERIC" else None,
            "value_text": value if kind == "TEXT" else None,
            "value_json": None,
            "source_ref": f"MVP-POLICY:{key}",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
        for key, (kind, value) in values.items()
    ]


def _load_policy(rows: list[dict[str, object]]):
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=rows) as fetch,
    ):
        policy = get_active_logistics_policy()
    assert fetch.call_args.args[1] == [
        "logistics",
        "v1.3-PROVISIONAL",
        "AGENT_MVP_DEMO",
    ]
    return policy


def test_logistics_policy_loads_typed_values_and_metadata():
    policy = _load_policy(_policy_rows())

    assert policy.guaranteed_capacity_kg == Decimal(8000)
    assert policy.burst_capacity_kg == Decimal(9600)
    assert policy.inbound_lead_days == 2
    assert policy.daily_inbound_capacity_kg == Decimal(5000)
    assert policy.inbound_transport_capacity_kg == Decimal(5000)
    assert policy.shared_daily_outbound_capacity_kg == Decimal(5000)
    assert policy.cap_by_date_policy == "CONFIRMED_ONLY"
    assert policy.policy_version == "v1.3-PROVISIONAL"
    assert policy.usage_scope == "AGENT_MVP_DEMO"
    assert policy.source_refs["guaranteed_capacity_kg"] == ("MVP-POLICY:guaranteed_capacity_kg")


def test_zero_numeric_policy_is_not_treated_as_missing():
    rows = _policy_rows()
    next(row for row in rows if row["policy_key"] == "inbound_lead_days")["value_numeric"] = (
        Decimal(0)
    )
    assert _load_policy(rows).inbound_lead_days == 0


@pytest.mark.parametrize("field", ["policy_version", "usage_scope"])
def test_logistics_policy_metadata_mismatch_fails_closed(field):
    rows = _policy_rows()
    rows[0][field] = "wrong"
    with pytest.raises(ValueError, match="mismatch"):
        _load_policy(rows)


def test_missing_or_inactive_required_logistics_policy_fails_closed():
    with pytest.raises(LookupError, match="guaranteed_capacity_kg"):
        _load_policy(_policy_rows()[1:])


@pytest.mark.parametrize(
    ("key", "mutation", "error"),
    [
        ("guaranteed_capacity_kg", {"value_kind": "TEXT"}, ValueError),
        ("guaranteed_capacity_kg", {"value_numeric": None}, ValueError),
        ("guaranteed_capacity_kg", {"value_numeric": "8000"}, TypeError),
        ("cap_by_date_policy", {"value_text": 1}, TypeError),
        ("cap_by_date_policy", {"value_json": {}}, ValueError),
    ],
)
def test_invalid_logistics_policy_value_fails_closed(key, mutation, error):
    rows = _policy_rows()
    next(row for row in rows if row["policy_key"] == key).update(mutation)
    with pytest.raises(error):
        _load_policy(rows)


def test_unsupported_cap_by_date_policy_fails_closed():
    rows = _policy_rows()
    next(row for row in rows if row["policy_key"] == "cap_by_date_policy")["value_text"] = (
        "FORECAST_ALLOWED"
    )
    with pytest.raises(ValidationError):
        _load_policy(rows)


def test_inactive_zone_policy_is_not_required_or_reconstructed():
    rows = _policy_rows()
    rows.append(
        {
            "policy_key": "guaranteed_capacity_by_zone_kg",
            "value_kind": "JSON",
            "value_numeric": None,
            "value_text": None,
            "value_json": {"GENERAL": 8000},
            "source_ref": "LEGACY:GENERAL",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
    )
    policy = _load_policy(rows)

    assert "guaranteed_capacity_by_zone_kg" not in policy.model_fields_set


def test_independent_sla_capacity_never_falls_back_to_legacy_6_4_ton():
    rows = _policy_rows()[1:]
    with pytest.raises(LookupError, match="guaranteed_capacity_kg"):
        _load_policy(rows)


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
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    saved = save_run.call_args.kwargs
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["runtime_status"] == "READY"
    assert saved["snapshot_id"] == "T0-20260821-001"
    assert saved["request_payload"]["scenarios"][0]["total_quantity_ton"] == "4.5"
    assert saved["response_payload"]["llm_status"] == "SKIPPED_TEMPLATE"


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
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
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
