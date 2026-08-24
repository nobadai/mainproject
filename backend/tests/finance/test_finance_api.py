from decimal import Decimal
from unittest.mock import patch

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
    assert response.json()["band"]["scope"] == "ALL_ITEMS_TOTAL"
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


def test_finance_openapi_keeps_legacy_endpoint_deprecated():
    schema = TestClient(app).get("/openapi.json").json()

    assert "/finance/procurement" in schema["paths"]
    assert "/finance/sales" in schema["paths"]
    assert schema["paths"]["/finance/core-review"]["post"]["deprecated"] is True
