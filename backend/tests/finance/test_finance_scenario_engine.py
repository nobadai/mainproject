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
    finance_context,
    purchase_payload,
):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    first = run_finance_procurement_scenario(request, finance_context)
    second = run_finance_procurement_scenario(request, finance_context)

    assert first == second
    assert first["max_feasible_amount_krw"] == Decimal(6111353)
    assert first["amount_comparisons"][0]["recalculated_amount_krw"] == Decimal(7125000)
    assert first["has_cost_mismatch"] is False


def test_finance_external_snapshot_bypasses_repository_and_round_trips_t0_id(
    finance_snapshot,
    finance_policy,
    purchase_payload,
):
    snapshot = finance_snapshot.model_copy(update={"snapshot_id": "T0-20251231-001"})
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with patch(
        "app.finance.service.get_current_finance_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_finance_procurement_with_snapshot(request, snapshot, policy=finance_policy)

    assert response.snapshot_id == "T0-20251231-001"
    assert response.snapshot_id != snapshot.finance_state_id
    assert response.band.max_feasible_amount_krw == Decimal(6111353)


def test_finance_sales_scenario_and_external_snapshot_are_deterministic(
    finance_context,
    sales_payload,
):
    snapshot = finance_context.snapshot.model_copy(update={"snapshot_id": "T0-20251231-001"})
    context = finance_context.model_copy(update={"snapshot": snapshot})
    request = FinanceSalesRequest.model_validate(sales_payload)
    before = snapshot.model_dump()

    first = run_finance_sales_scenario(request, context)
    second = run_finance_sales_scenario(request, context)
    with patch(
        "app.finance.service.get_current_finance_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_finance_sales_with_snapshot(request, snapshot, policy=context.policy)

    assert first == second
    assert first["post_h1_projection"] is not None
    assert response.snapshot_id == "T0-20251231-001"
    assert response.approval_id == request.approved_purchase.approval_id
    assert snapshot.model_dump() == before
