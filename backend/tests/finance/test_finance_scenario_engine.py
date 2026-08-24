from decimal import Decimal
from unittest.mock import patch

from app.finance.scenario_engine import (
    run_finance_procurement_scenario,
    run_finance_sales_scenario,
)
from app.finance.schemas import FinanceSalesRequest, PurchaseAgentOutput
from app.finance.service import (
    run_finance_procurement_with_snapshot,
    run_finance_sales_with_snapshot,
)


def test_finance_procurement_scenario_is_deterministic(
    finance_snapshot,
    purchase_payload,
):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    first = run_finance_procurement_scenario(request, finance_snapshot)
    second = run_finance_procurement_scenario(request, finance_snapshot)

    assert first == second
    assert first["financial_limit_matches"] is True
    assert first["amount_comparisons"][0]["recalculated_amount_krw"] == Decimal(7125000)
    assert first["has_cost_mismatch"] is True


def test_finance_external_snapshot_bypasses_repository_and_round_trips_t0_id(
    finance_snapshot,
    purchase_payload,
):
    snapshot = finance_snapshot.model_copy(update={"snapshot_id": "T0-20251231-001"})
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with patch(
        "app.finance.service.get_current_finance_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_finance_procurement_with_snapshot(request, snapshot)

    assert response.snapshot_id == "T0-20251231-001"
    assert response.snapshot_id != snapshot.finance_state_id
    assert response.band.max_feasible_amount_krw == Decimal("16091273.770000")


def test_finance_sales_scenario_and_external_snapshot_are_deterministic(
    finance_snapshot,
    sales_payload,
):
    snapshot = finance_snapshot.model_copy(update={"snapshot_id": "T0-20251231-001"})
    request = FinanceSalesRequest.model_validate(sales_payload)
    before = snapshot.model_dump()

    first = run_finance_sales_scenario(request, snapshot)
    second = run_finance_sales_scenario(request, snapshot)
    with patch(
        "app.finance.service.get_current_finance_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_finance_sales_with_snapshot(request, snapshot)

    assert first == second
    assert first["financial_limit_matches"] is True
    assert first["post_approved_purchase_cash_krw"] == Decimal("21674918.770000")
    assert response.snapshot_id == "T0-20251231-001"
    assert response.approval_id == request.approved_purchase.approval_id
    assert snapshot.model_dump() == before
