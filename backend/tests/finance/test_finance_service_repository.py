from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from psycopg import OperationalError

from app.finance.repository import get_current_finance_state
from app.finance.schemas import FinanceSalesRequest, PurchaseAgentOutput
from app.finance.service import run_finance_procurement, run_finance_sales


def test_procurement_service_returns_one_band_and_cost_warning(finance_state, purchase_payload):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with patch("app.finance.service.get_current_finance_state", return_value=finance_state):
        response = run_finance_procurement(request)

    assert response.runtime_status == "READY"
    assert response.policy_version == "v1.3-PROVISIONAL"
    assert response.band.max_feasible_amount_krw == Decimal("16091273.770000")
    assert response.band.scope == "ALL_ITEMS_TOTAL"
    assert response.soft_warnings == ["COST_MISMATCH"]
    assert "verdict" not in response.model_dump()


def test_procurement_service_stops_on_financial_limit_mismatch(finance_state, purchase_payload):
    finance_state["financial_limit_krw"] = Decimal(1)
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with (
        patch("app.finance.service.get_current_finance_state", return_value=finance_state),
        pytest.raises(ValueError, match="FINANCIAL_LIMIT_MISMATCH"),
    ):
        run_finance_procurement(request)


def test_procurement_service_maps_only_lookup_error_to_not_ready(purchase_payload):
    request = PurchaseAgentOutput.model_validate(purchase_payload)
    with patch("app.finance.service.get_current_finance_state", side_effect=LookupError):
        response = run_finance_procurement(request)
    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.hard_constraints == ["REQUIRED_FINANCE_STATE_MISSING"]

    with (
        patch(
            "app.finance.service.get_current_finance_state",
            side_effect=OperationalError("database unavailable"),
        ),
        pytest.raises(OperationalError),
    ):
        run_finance_procurement(request)


def test_sales_service_applies_approved_purchase_overlay(finance_state, sales_payload):
    sales_payload["approved_purchase"]["total_amount_krw"] = 18000000
    request = FinanceSalesRequest.model_validate(sales_payload)

    with patch("app.finance.service.get_current_finance_state", return_value=finance_state):
        response = run_finance_sales(request)

    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.sales_cash_priority is None
    assert "FINANCIAL_LIMIT_EXCEEDED" in response.hard_constraints
    assert response.collection_preferences[0].liquidity_rank == 1


def test_repository_preserves_decimal_row(finance_state):
    with patch("app.finance.repository.fetch_one", return_value=finance_state):
        state = get_current_finance_state()

    assert state["finance_state_id"] == "FIN-DAY30-LOAN"
    assert state["state_date"] == date(2025, 12, 31)
    assert isinstance(state["financial_limit_krw"], Decimal)
