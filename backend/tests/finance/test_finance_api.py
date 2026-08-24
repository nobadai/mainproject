from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app.finance.schemas import (
    FinanceBand,
    FinanceProcurementResponse,
    FinanceSalesResponse,
)
from app.main import app


def test_finance_procurement_api(purchase_payload):
    result = FinanceProcurementResponse(
        as_of="2025-12-31",
        snapshot_id="FIN-DAY30-LOAN",
        runtime_status="READY",
        band=FinanceBand(max_feasible_amount_krw=Decimal("16091273.770000")),
        base_projected_cash_min=None,
        base_cash_priority=None,
        hard_constraints=[],
        soft_warnings=["COST_MISMATCH"],
        suggested_adjustment={"max_amount_krw": Decimal("16091273.770000")},
        evidences=[],
    )
    with patch("app.finance.router.run_finance_procurement", return_value=result):
        response = TestClient(app).post("/finance/procurement", json=purchase_payload)

    assert response.status_code == 200
    assert response.json()["policy_version"] == "v1.3-PROVISIONAL"
    assert response.json()["band"]["scope"] == "ALL_ITEMS_TOTAL"
    assert response.json()["interpretation"]["summary"]
    assert response.json()["llm_status"] == "DISABLED"
    assert "verdict" not in response.json()


def test_finance_sales_api(sales_payload):
    result = FinanceSalesResponse(
        snapshot_id="FIN-DAY30-LOAN",
        approval_id="H1-20260821-001",
        runtime_status="RUNTIME_NOT_READY",
        base_cash_priority=None,
        sales_cash_priority=None,
        collection_preferences=[
            {
                "channel_type": "DIRECT_B2B",
                "partner_id": "KIMCHI_FACTORY_001",
                "settlement_days": 30,
                "liquidity_rank": 1,
            }
        ],
        hard_constraints=[],
        soft_warnings=["CASH_PRIORITY_POLICY_UNRESOLVED"],
    )
    with patch("app.finance.router.run_finance_sales", return_value=result):
        response = TestClient(app).post("/finance/sales", json=sales_payload)

    assert response.status_code == 200
    assert response.json()["sales_cash_priority"] is None
    assert response.json()["interpretation"]["summary"]
    assert response.json()["llm_status"] == "DISABLED"


def test_finance_openapi_keeps_legacy_endpoint_deprecated():
    schema = TestClient(app).get("/openapi.json").json()

    assert "/finance/procurement" in schema["paths"]
    assert "/finance/sales" in schema["paths"]
    assert schema["paths"]["/finance/core-review"]["post"]["deprecated"] is True


def test_finance_runs_api_forwards_filters():
    run = {
        "run_id": UUID("00000000-0000-0000-0000-000000000001"),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": "FIN-DAY30-LOAN",
        "runtime_status": "RUNTIME_NOT_READY",
        "request_payload": {"meta": {"as_of": "2026-08-21"}},
        "response_payload": {"runtime_status": "RUNTIME_NOT_READY"},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    with patch("app.finance.router.list_finance_runs", return_value=[run]) as list_runs:
        response = TestClient(app).get(
            "/finance/runs",
            params={
                "cycle": "PROCUREMENT",
                "as_of": "2026-08-21",
                "runtime_status": "RUNTIME_NOT_READY",
                "limit": 25,
            },
        )

    assert response.status_code == 200
    assert response.json()[0]["run_id"] == str(run["run_id"])
    assert list_runs.call_args.kwargs == {
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "runtime_status": "RUNTIME_NOT_READY",
        "limit": 25,
    }


def test_finance_run_detail_and_not_found():
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    run = {
        "run_id": run_id,
        "cycle": "SALES",
        "as_of": date(2026, 8, 21),
        "snapshot_id": None,
        "runtime_status": "RUNTIME_NOT_READY",
        "request_payload": {},
        "response_payload": {},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    with patch("app.finance.router.get_finance_run", return_value=run):
        response = TestClient(app).get(f"/finance/runs/{run_id}")
    assert response.status_code == 200

    with patch("app.finance.router.get_finance_run", side_effect=LookupError):
        response = TestClient(app).get("/finance/runs/00000000-0000-0000-0000-000000000002")
    assert response.status_code == 404


def test_finance_runs_api_rejects_invalid_cycle():
    response = TestClient(app).get("/finance/runs", params={"cycle": "INVALID"})

    assert response.status_code == 422
