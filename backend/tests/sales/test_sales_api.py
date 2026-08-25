"""영업 Agent API 테스트.

실행이력 저장(save_sales_agent_run)은 monkeypatch로 대체해 PostgreSQL 없이
POST 계산·응답과 저장 호출 인자를 검증한다. 실제 DB 저장·조회는
운영 DB 환경이 준비된 뒤 별도 통합 테스트로 확인한다.
"""

from datetime import date, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _fake_run(**overrides) -> dict:
    row = {
        "run_id": uuid4(),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": "T0-20260821-01",
        "runtime_status": "READY",
        "request_payload": {"item": "배추"},
        "response_payload": {"stage": "T2"},
        "created_at": datetime(2026, 8, 21, 9, 0, 0),
    }
    row.update(overrides)
    return row


def test_procurement_computes_floor_and_saves(monkeypatch):
    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return kwargs

    monkeypatch.setattr("app.sales.service.save_sales_agent_run", fake_save)

    payload = {
        "as_of": "2026-08-21",
        "snapshot_id": "T0-20260821-01",
        "item": "배추",
        "inbound_lead_days": 4,
        "confirmed_orders": [
            {"order_id": "ORD-1", "delivery_date": "2026-08-23", "qty_kg": 200},
            {"order_id": "ORD-2", "delivery_date": "2026-08-25", "qty_kg": 1500},
        ],
        "inventory": {
            "on_hand": [
                {"lot_id": "LOT-A", "qty_kg": 600, "freshness_days_left": 8},
                {"lot_id": "LOT-B", "qty_kg": 100, "freshness_days_left": 2},
            ],
            "in_transit": [],
        },
    }

    response = client.post("/sales/procurement", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "T2"
    assert body["runtime_status"] == "READY"
    assert body["today_floor_kg"] == "1100"
    assert body["binding_delivery_date"] == "2026-08-25"
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["snapshot_id"] == "T0-20260821-01"


def test_procurement_not_ready_when_lead_days_missing(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)

    payload = {
        "as_of": "2026-08-21",
        "item": "배추",
        "confirmed_orders": [
            {"order_id": "ORD-1", "delivery_date": "2026-08-25", "qty_kg": 1500},
        ],
        "inventory": {
            "on_hand": [{"lot_id": "LOT-A", "qty_kg": 600, "freshness_days_left": 8}],
            "in_transit": [],
        },
    }

    response = client.post("/sales/procurement", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_status"] == "RUNTIME_NOT_READY"
    assert body["today_floor_kg"] is None
    assert body["binding_delivery_date"] is None


def test_allocation_computes_strategic_inventory_and_saves(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "app.sales.service.save_sales_agent_run",
        lambda **kwargs: saved.update(kwargs) or kwargs,
    )

    payload = {
        "as_of": "2026-08-21",
        "snapshot_id": "T0-20260821-01",
        "item": "배추",
        "inventory": {
            "on_hand": [
                {
                    "lot_id": "LOT-A",
                    "qty_kg": 600,
                    "freshness_days_left": 8,
                    "reserved_for_confirmed_kg": 200,
                },
                {"lot_id": "LOT-B", "qty_kg": 100, "freshness_days_left": 2},
            ],
            "in_transit": [],
        },
    }

    response = client.post("/sales/allocation", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "S1"
    assert body["strategic_inventory_by_date"][0]["kg"] == "500"
    assert saved["cycle"] == "SALES"


def test_procurement_rejects_unknown_field():
    payload = {
        "as_of": "2026-08-21",
        "item": "배추",
        "confirmed_orders": [],
        "inventory": {"on_hand": [], "in_transit": []},
        "unexpected": "x",
    }
    response = client.post("/sales/procurement", json=payload)
    assert response.status_code == 422


def test_in_transit_requires_arrival_date():
    payload = {
        "as_of": "2026-08-21",
        "snapshot_id": "T0-01",
        "item": "배추",
        "inventory": {
            "on_hand": [{"lot_id": "A", "qty_kg": 600, "freshness_days_left": 8}],
            "in_transit": [{"lot_id": "C", "qty_kg": 300}],
        },
    }
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_in_transit_key_is_required():
    payload = {
        "as_of": "2026-08-21",
        "item": "배추",
        "inventory": {"on_hand": [{"lot_id": "A", "qty_kg": 600, "freshness_days_left": 8}]},
    }
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_reserved_over_quantity_is_rejected():
    payload = {
        "as_of": "2026-08-21",
        "item": "배추",
        "inventory": {
            "on_hand": [
                {
                    "lot_id": "A",
                    "qty_kg": 100,
                    "freshness_days_left": 8,
                    "reserved_for_confirmed_kg": 200,
                }
            ],
            "in_transit": [],
        },
    }
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_list_runs_applies_filter(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return [_fake_run()]

    monkeypatch.setattr("app.sales.service.list_sales_agent_runs", fake_list)

    response = client.get("/sales/runs", params={"cycle": "PROCUREMENT", "limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["cycle"] == "PROCUREMENT"
    assert captured["cycle"] == "PROCUREMENT"
    assert captured["limit"] == 10


def test_get_run_single(monkeypatch):
    run = _fake_run()
    monkeypatch.setattr("app.sales.service.get_sales_agent_run", lambda run_id: run)

    response = client.get(f"/sales/runs/{run['run_id']}")

    assert response.status_code == 200
    assert response.json()["run_id"] == str(run["run_id"])


def test_get_run_missing_is_404(monkeypatch):
    def fake_get(run_id):
        raise LookupError("not found")

    monkeypatch.setattr("app.sales.service.get_sales_agent_run", fake_get)

    response = client.get(f"/sales/runs/{uuid4()}")
    assert response.status_code == 404


def test_list_runs_limit_out_of_range_is_422():
    response = client.get("/sales/runs", params={"limit": 999})
    assert response.status_code == 422
