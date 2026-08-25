from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from app.logistics.repository import (
    get_active_logistics_policy,
    get_active_logistics_runtime_fixture,
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


def _fixture_row(**updates) -> dict[str, object]:
    row = {
        "fixture_id": "LOG-RUNTIME-SIM-BURNIN-202512-DAY30",
        "sim_run_id": "SIM-BURNIN-202512",
        "as_of": date(2025, 12, 31),
        "in_transit_status": "CONFIRMED_ZERO",
        "in_transit_json": [],
        "confirmed_inbound_status": "CONFIRMED_ZERO",
        "confirmed_inbound_json": [],
        "confirmed_outbound_status": "CONFIRMED_ZERO",
        "confirmed_outbound_json": [],
        "usage_scope": "AGENT_MVP_DEMO",
        "evidence_grade": "SIM_FIXED",
        "source_ref": "MVP-DECISION-20260825:LOG-RUNTIME-DAY30",
        "approved_by": "HUMAN",
    }
    row.update(updates)
    return row


def _inventory_rows() -> list[dict[str, object]]:
    return [
        {
            "lot_id": "LOT-KIMCHI-015-BAECHU",
            "item_name": "배추",
            "grade": "상",
            "received_at": date(2025, 12, 31),
            "remaining_qty_kg": Decimal("286.92"),
            "status": "ACTIVE",
            "storage_zone": "COLD_HUMID_0_3",
            "operational_limit_days": 10,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "lot_id": "LOT-KIMCHI-015-MU",
            "item_name": "무",
            "grade": "상",
            "received_at": date(2025, 12, 30),
            "remaining_qty_kg": Decimal("61.76"),
            "status": "ACTIVE",
            "storage_zone": "COLD_HUMID_0_4",
            "operational_limit_days": 12,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "lot_id": "LOT-KIMCHI-015-PIMANUL",
            "item_name": "피마늘",
            "grade": "상",
            "received_at": date(2025, 12, 31),
            "remaining_qty_kg": Decimal("8.88"),
            "status": "ACTIVE",
            "storage_zone": "FROZEN_DRY_-3",
            "operational_limit_days": 30,
            "medium_grade_factor": Decimal("0.8"),
        },
        {
            "lot_id": "LOT-KIMCHI-015-YANGPA",
            "item_name": "양파",
            "grade": "상",
            "received_at": date(2025, 12, 31),
            "remaining_qty_kg": Decimal("5.72"),
            "status": "ACTIVE",
            "storage_zone": "COLD_DRY_0_1",
            "operational_limit_days": 14,
            "medium_grade_factor": Decimal("0.8"),
        },
    ]


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


def test_runtime_fixture_loads_confirmed_zero_schedules():
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row()]) as fetch,
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.fixture_id == "LOG-RUNTIME-SIM-BURNIN-202512-DAY30"
    assert fixture.sim_run_id == "SIM-BURNIN-202512"
    assert fixture.in_transit == []
    assert fixture.confirmed_inbound_schedule == []
    assert fixture.confirmed_outbound_schedule == []
    assert fetch.call_args.args[1] == ["AGENT_MVP_DEMO", date(2025, 12, 31)]


@pytest.mark.parametrize("rows", [[], [_fixture_row(), _fixture_row(fixture_id="duplicate")]])
def test_runtime_fixture_requires_exactly_one_active_row(rows):
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=rows),
        pytest.raises(LookupError, match="exactly one"),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"usage_scope": "wrong"}, "usage_scope mismatch"),
        ({"as_of": date(2025, 12, 30)}, "as_of mismatch"),
        (
            {
                "in_transit_json": [
                    {
                        "item": "배추",
                        "quantity_kg": 1,
                        "expected_arrival_date": "2026-01-02",
                    }
                ]
            },
            "CONFIRMED_ZERO",
        ),
        ({"confirmed_inbound_json": {}}, "list"),
        ({"in_transit_status": "INVALID"}, "Input should be"),
    ],
)
def test_invalid_runtime_fixture_fails_closed(updates, message):
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[_fixture_row(**updates)]),
        pytest.raises((ValueError, ValidationError), match=message),
    ):
        get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))


def test_unresolved_runtime_source_preserves_none():
    row = _fixture_row(in_transit_status="UNRESOLVED", in_transit_json=None)
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch("app.logistics.repository.fetch_all", return_value=[row]),
    ):
        fixture = get_active_logistics_runtime_fixture(as_of=date(2025, 12, 31))

    assert fixture.in_transit is None


def test_runtime_snapshot_combines_fixture_direct_lots_and_policy():
    with (
        patch("app.logistics.repository.get_db_schema", return_value="configured_schema"),
        patch(
            "app.logistics.repository.fetch_all",
            side_effect=[[_fixture_row()], _policy_rows(), _inventory_rows()],
        ) as fetch,
    ):
        snapshot = get_current_inventory_logistics_snapshot(as_of=date(2025, 12, 31))

    assert snapshot.snapshot_id is None
    assert [lot.lot_id for lot in snapshot.on_hand_by_lot] == [
        "LOT-KIMCHI-015-BAECHU",
        "LOT-KIMCHI-015-MU",
        "LOT-KIMCHI-015-PIMANUL",
        "LOT-KIMCHI-015-YANGPA",
    ]
    assert all(lot.item != "건고추" for lot in snapshot.on_hand_by_lot)
    assert snapshot.used_capacity_kg == Decimal("363.28")
    assert snapshot.guaranteed_capacity_kg == Decimal(8000)
    assert snapshot.guaranteed_capacity_kg - snapshot.used_capacity_kg == Decimal("7636.72")
    assert snapshot.guaranteed_capacity_kg - snapshot.used_capacity_kg != Decimal("6036.72")
    assert snapshot.burst_capacity_kg == Decimal(9600)
    assert snapshot.in_transit == []
    assert snapshot.confirmed_inbound_schedule == []
    assert snapshot.confirmed_outbound_schedule == []
    assert snapshot.guaranteed_capacity_by_zone_kg is None
    inventory_call = fetch.call_args_list[2]
    assert inventory_call.args[1] == ["SIM-BURNIN-202512", date(2025, 12, 31)]
    query_text = str(inventory_call.args[0])
    assert "inventory_lots" in query_text
    assert "received_at <= %s" in query_text
    assert "status = 'ACTIVE'" in query_text
    assert "remaining_qty_kg > 0" in query_text
    assert "v_current_inventory" not in query_text
    assert "v_current_logistics_capacity" not in query_text


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
    assert response.verdict == "REVIEW_REQUIRED"
    assert response.snapshot_id == "T0-20260821-001"
    assert response.band.cap_by_date == {date(2026, 8, 23): Decimal(2500)}
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    saved = save_run.call_args.kwargs
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["runtime_status"] == "READY"
    assert saved["verdict"] == "REVIEW_REQUIRED"
    assert saved["response_payload"]["verdict"] == "REVIEW_REQUIRED"
    assert saved["snapshot_id"] == "T0-20260821-001"
    assert saved["request_payload"]["scenarios"][0]["total_quantity_kg"] == "4500"
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
    assert response.verdict is None
    assert response.band.cap_by_date == {}
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"
    assert save_run.call_args.kwargs["verdict"] is None
    assert save_run.call_args.kwargs["response_payload"]["verdict"] is None


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
    assert response.verdict == "PASS"
    assert response.approval_id == "H1-20260821-001"
    assert response.daily_outbound_capacity_kg == Decimal(1000)
    assert [item.lot_id for item in response.lot_constraints] == ["LOT-001"]
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    assert save_run.call_args.kwargs["cycle"] == "SALES"
    assert save_run.call_args.kwargs["verdict"] == "PASS"
    assert save_run.call_args.kwargs["response_payload"]["verdict"] == "PASS"


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
    assert response.verdict is None
    assert response.daily_outbound_capacity_kg is None
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"
    assert save_run.call_args.kwargs["verdict"] is None


def test_logistics_b_ready_blocking_constraint_persists_fail(
    complete_logistics_snapshot, logistics_sales_payload
):
    snapshot = complete_logistics_snapshot.model_copy(
        update={"guaranteed_capacity_kg": Decimal(5000)}
    )
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    with (
        patch(
            "app.logistics.service.get_current_inventory_logistics_snapshot",
            return_value=snapshot,
        ),
        patch("app.logistics.service.save_logistics_agent_run") as save_run,
    ):
        response = run_logistics_sales(request)

    assert response.runtime_status == "READY"
    assert response.verdict == "FAIL"
    assert save_run.call_args.kwargs["verdict"] == "FAIL"
    assert save_run.call_args.kwargs["response_payload"]["verdict"] == "FAIL"


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
