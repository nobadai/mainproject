from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.logistics.run_repository import save_logistics_agent_run
from app.logistics.schemas import LogisticsProcurementResponse, LogisticsSalesResponse
from app.main import app


def _run_row() -> dict[str, object]:
    return {
        "run_id": UUID("00000000-0000-0000-0000-000000000001"),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": None,
        "runtime_status": "RUNTIME_NOT_READY",
        "request_payload": {},
        "response_payload": {},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }


def test_run_repository_uses_jsonb_and_metadata():
    row = _run_row()
    with (
        patch("app.logistics.run_repository.get_db_schema", return_value="haetdeul"),
        patch(
            "app.logistics.run_repository.execute_returning_one",
            return_value=row,
        ) as execute,
    ):
        saved = save_logistics_agent_run(
            cycle="PROCUREMENT",
            as_of=date(2026, 8, 21),
            snapshot_id=None,
            runtime_status="RUNTIME_NOT_READY",
            request_payload={},
            response_payload={},
        )

    params = execute.call_args.args[1]
    assert saved == row
    assert params[1:5] == ("PROCUREMENT", date(2026, 8, 21), None, "RUNTIME_NOT_READY")
    assert isinstance(params[5], Jsonb)
    assert isinstance(params[6], Jsonb)


def test_logistics_post_endpoints(logistics_purchase_payload, logistics_sales_payload):
    procurement = LogisticsProcurementResponse(
        as_of="2026-08-21",
        snapshot_id=None,
        runtime_status="RUNTIME_NOT_READY",
        band={"cap_by_date": {}},
        inbound_constraints={
            "inbound_lead_days": None,
            "daily_inbound_capacity_kg": None,
            "inbound_transport_capacity_kg": None,
        },
        hard_constraints=[],
        soft_warnings=[],
        evidences=[],
    )
    sales = LogisticsSalesResponse(
        snapshot_id=None,
        approval_id="H1-20260821-001",
        runtime_status="RUNTIME_NOT_READY",
        daily_outbound_capacity_kg=None,
        lot_constraints=[],
        hard_constraints=[],
        soft_warnings=[],
    )
    client = TestClient(app)
    with patch("app.logistics.router.run_logistics_procurement", return_value=procurement):
        response = client.post("/logistics/procurement", json=logistics_purchase_payload)
    assert response.status_code == 200
    with patch("app.logistics.router.run_logistics_sales", return_value=sales):
        response = client.post("/logistics/sales", json=logistics_sales_payload)
    assert response.status_code == 200


def test_logistics_runs_api_filters_and_detail():
    row = _run_row()
    client = TestClient(app)
    with patch("app.logistics.router.list_logistics_runs", return_value=[row]) as list_runs:
        response = client.get(
            "/logistics/runs",
            params={
                "cycle": "PROCUREMENT",
                "as_of": "2026-08-21",
                "runtime_status": "RUNTIME_NOT_READY",
                "limit": 25,
            },
        )
    assert response.status_code == 200
    assert list_runs.call_args.kwargs["limit"] == 25

    with patch("app.logistics.router.get_logistics_run", return_value=row):
        response = client.get(f"/logistics/runs/{row['run_id']}")
    assert response.status_code == 200


def test_logistics_runs_api_404_and_422():
    client = TestClient(app)
    with patch("app.logistics.router.get_logistics_run", side_effect=LookupError):
        response = client.get("/logistics/runs/00000000-0000-0000-0000-000000000002")
    assert response.status_code == 404
    assert client.get("/logistics/runs", params={"cycle": "INVALID"}).status_code == 422
    assert client.get("/logistics/runs/not-a-uuid").status_code == 422


def test_logistics_openapi_paths_are_registered():
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "/logistics/procurement" in paths
    assert "/logistics/sales" in paths
    assert "/logistics/runs" in paths
    assert "/logistics/runs/{run_id}" in paths
