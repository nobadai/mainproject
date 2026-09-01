from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from psycopg import OperationalError

from app.finance.repository import (
    get_current_finance_runtime_context,
    get_current_finance_snapshot,
    get_current_finance_state,
)
from app.finance.schemas import FinanceSalesRequest, PurchaseAgentOutput
from app.finance.service import run_finance_procurement, run_finance_sales

#: `patch()` 대상 모듈 경로 — 소유 모듈을 직접 가리킨다.
_LEGACY_SERVICE = "app.finance.legacy.deterministic_service"

#: `patch()` 대상 모듈 경로 — 소유 모듈을 직접 가리킨다.
_STATE_REPO = "app.finance.infrastructure.finance_state_repository"


def test_procurement_service_returns_one_band_without_cost_warning(
    finance_context, purchase_payload
):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with (
        patch(
            f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", return_value=finance_context
        ),
        patch(f"{_LEGACY_SERVICE}.save_finance_agent_run") as save_run,
    ):
        response = run_finance_procurement(request)

    assert response.runtime_status == "READY"
    assert response.policy_version == "v1.3-PROVISIONAL"
    assert response.band.max_feasible_amount_krw == Decimal(6111353)
    assert response.base_projected_cash_min == Decimal("19052633.770000")
    assert response.base_cash_priority == "MEDIUM"
    assert response.band.scope == "ALL_ITEMS_TOTAL"
    assert response.soft_warnings == []
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    assert response.verdict == "PASS"
    saved = save_run.call_args.kwargs
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["runtime_status"] == "READY"
    assert saved["verdict"] == "PASS"
    assert saved["response_payload"]["verdict"] == "PASS"
    assert saved["snapshot_id"] is None
    assert saved["request_payload"]["meta"]["as_of"] == "2025-12-31"
    assert saved["request_payload"]["scenarios"][0]["total_amount_krw"] == 7125000
    stored_limit = saved["response_payload"]["band"]["max_feasible_amount_krw"]
    assert stored_limit == "6111353"
    assert saved["response_payload"]["llm_status"] == "SKIPPED_TEMPLATE"


def test_procurement_service_does_not_compare_new_cap_to_legacy_limit(
    finance_context, purchase_payload
):
    snapshot = finance_context.snapshot.model_copy(update={"financial_limit_krw": Decimal(1)})
    context = finance_context.model_copy(update={"snapshot": snapshot})
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with (
        patch(f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", return_value=context),
        patch(f"{_LEGACY_SERVICE}.save_finance_agent_run"),
    ):
        response = run_finance_procurement(request)
    assert response.band.max_feasible_amount_krw == Decimal(6111353)


def test_procurement_service_maps_only_lookup_error_to_not_ready(purchase_payload):
    request = PurchaseAgentOutput.model_validate(purchase_payload)
    with (
        patch(f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", side_effect=LookupError),
        patch(f"{_LEGACY_SERVICE}.save_finance_agent_run") as save_run,
    ):
        response = run_finance_procurement(request)
    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.verdict is None
    assert response.hard_constraints == ["REQUIRED_FINANCE_STATE_MISSING"]
    assert save_run.call_args.kwargs["runtime_status"] == "RUNTIME_NOT_READY"
    assert save_run.call_args.kwargs["verdict"] is None
    assert save_run.call_args.kwargs["response_payload"]["verdict"] is None

    with (
        patch(
            f"{_LEGACY_SERVICE}.get_current_finance_runtime_context",
            side_effect=OperationalError("database unavailable"),
        ),
        pytest.raises(OperationalError),
    ):
        run_finance_procurement(request)


def test_sales_service_applies_approved_purchase_overlay(finance_context, sales_payload):
    sales_payload["approved_purchase"]["total_amount_krw"] = 18000000
    sales_payload["approved_purchase"]["payment_date"] = "2026-01-07"
    request = FinanceSalesRequest.model_validate(sales_payload)

    with (
        patch(
            f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", return_value=finance_context
        ),
        patch(f"{_LEGACY_SERVICE}.save_finance_agent_run") as save_run,
    ):
        response = run_finance_sales(request)

    assert response.runtime_status == "READY"
    assert response.verdict == "FAIL"
    assert response.sales_cash_priority == "HIGH"
    assert response.llm_status == "SKIPPED_TEMPLATE"
    assert response.llm_attempts == 0
    assert "FIN-H01_MINIMUM_CASH_BALANCE" in response.hard_constraints
    assert response.collection_preferences[0].liquidity_rank == 1
    saved = save_run.call_args.kwargs
    assert saved["cycle"] == "SALES"
    assert saved["runtime_status"] == "READY"
    assert saved["verdict"] == "FAIL"
    assert saved["response_payload"]["verdict"] == "FAIL"
    assert saved["request_payload"]["approved_purchase"]["total_amount_krw"] == "18000000"
    assert saved["response_payload"]["soft_warnings"] == []


def test_repository_preserves_decimal_row(finance_state):
    with patch(f"{_STATE_REPO}.fetch_one", return_value=finance_state):
        state = get_current_finance_state()

    assert state["finance_state_id"] == "FIN-DAY30-LOAN"
    assert state["state_date"] == date(2025, 12, 31)
    assert isinstance(state["financial_limit_krw"], Decimal)

    with patch(f"{_STATE_REPO}.fetch_one", return_value=finance_state):
        snapshot = get_current_finance_snapshot()

    assert snapshot.snapshot_id is None
    assert snapshot.finance_state_id == "FIN-DAY30-LOAN"
    assert isinstance(snapshot.financial_limit_krw, Decimal)


def test_valid_debt_contract_resolves_with_zero_events_in_horizon(
    finance_snapshot, finance_policy, finance_debt_policy
):
    snapshot = finance_snapshot.model_copy(
        update={"current_debt_krw": finance_debt_policy.debt_principal_krw}
    )
    with (
        patch(f"{_STATE_REPO}.get_current_finance_snapshot", return_value=snapshot),
        patch(f"{_STATE_REPO}.get_active_finance_policy", return_value=finance_policy),
        patch(
            f"{_STATE_REPO}.get_active_finance_debt_policy",
            return_value=finance_debt_policy,
        ),
        patch(f"{_STATE_REPO}._fetch_scheduled_rows", return_value=[]),
    ):
        context = get_current_finance_runtime_context()
    assert context.debt_policy == finance_debt_policy
    assert "DEBT_SERVICE" not in context.unresolved_sources
    assert [event for event in context.cash_events if event.event_type == "DEBT_SERVICE"] == []


@pytest.mark.parametrize("debt_error", [LookupError("missing"), ValueError("malformed")])
def test_missing_or_malformed_debt_contract_is_unresolved(
    finance_snapshot, finance_policy, debt_error
):
    snapshot = finance_snapshot.model_copy(update={"current_debt_krw": Decimal(1)})
    with (
        patch(f"{_STATE_REPO}.get_current_finance_snapshot", return_value=snapshot),
        patch(f"{_STATE_REPO}.get_active_finance_policy", return_value=finance_policy),
        patch(f"{_STATE_REPO}.get_active_finance_debt_policy", side_effect=debt_error),
        patch(f"{_STATE_REPO}._fetch_scheduled_rows", return_value=[]),
    ):
        context = get_current_finance_runtime_context()
    assert context.debt_policy is None
    assert context.unresolved_sources == ("DEBT_SERVICE",)


def test_debt_principal_mismatch_is_unresolved(
    finance_snapshot, finance_policy, finance_debt_policy
):
    snapshot = finance_snapshot.model_copy(update={"current_debt_krw": Decimal(1)})
    with (
        patch(f"{_STATE_REPO}.get_current_finance_snapshot", return_value=snapshot),
        patch(f"{_STATE_REPO}.get_active_finance_policy", return_value=finance_policy),
        patch(
            f"{_STATE_REPO}.get_active_finance_debt_policy",
            return_value=finance_debt_policy,
        ),
        patch(f"{_STATE_REPO}._fetch_scheduled_rows", return_value=[]),
    ):
        context = get_current_finance_runtime_context()
    assert context.unresolved_sources == ("DEBT_SERVICE",)
