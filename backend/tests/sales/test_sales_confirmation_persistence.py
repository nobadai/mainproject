from __future__ import annotations

import os
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("DB_SCHEMA", "haetdeul")

from app.sales.persistence import (
    SalesPersistenceConflict,
    build_sale_confirmation_plan,
    confirm_sale,
    mark_sale_delivered,
    sale_id_for,
)
from app.sales.schemas import SalesConfirmationInput, SalesScenario


def _scenario(**overrides) -> SalesScenario:
    data = {
        "scenario_id": "SCN-1",
        "scenario_type": "CONSERVATIVE",
        "objective": "RISK_DEFENSE",
        "business_mode": "CONTRACT_FULFILLMENT",
        "item": "배추",
        "partner_id": "PARTNER-1",
        "quantity_kg": Decimal(8500),
        "unit_price_krw": Decimal(2300),
        "sales_amount_krw": Decimal(19550000),
        "delivery_date": date(2026, 9, 10),
        "payment_days": 30,
        "payment_terms_type": "SINGLE",
        "contract_term_days": 90,
        "source_ref": "CONTRACT-1",
        "supply": {"confirmed_quantity_kg": "8500"},
        "sales_decision_axes": ["CONTRACT"],
        "required_validations": [],
        "evidence_refs": [],
        "rationale": [],
        "risks": [],
        "uncertainties": [],
        "conditional_purchase": False,
        "variant_collapsed": False,
        "variant_collapsed_reason": None,
        "domain_replies": [],
        "status": "EXECUTABLE",
        "execution_dependencies": [],
        "unmet_quantity_kg": Decimal(0),
        "finance_verdict": "PASS",
        "contribution_margin_krw": Decimal(3850000),
        "contribution_margin_rate": Decimal("0.197"),
        "sell_priority": "HIGH",
    }
    data.update(overrides)
    return SalesScenario.model_validate(data)


def _request(**overrides) -> SalesConfirmationInput:
    data = {
        "execution_identity": {
            "request_id": "REQ-1",
            "run_id": "RUN-1",
            "as_of": "2026-09-07",
            "policy_version": "v1",
            "feedback_attempt": 0,
        },
        "selected_scenario_id": "SCN-1",
        "selected_scenario": {
            "scenario_id": "SCN-1",
            "scenario_type": "CONSERVATIVE",
            "objective": "RISK_DEFENSE",
            "business_mode": "CONTRACT_FULFILLMENT",
            "item": "배추",
            "partner_id": "PARTNER-1",
            "quantity_kg": "8500",
            "unit_price_krw": "2300",
            "sales_amount_krw": "19550000",
            "delivery_date": "2026-09-10",
            "payment_days": 30,
            "payment_terms_type": "SINGLE",
            "contract_term_days": 90,
            "source_ref": "CONTRACT-1",
            "supply": {"confirmed_quantity_kg": "8500"},
            "sales_decision_axes": ["CONTRACT"],
            "required_validations": [],
            "evidence_refs": [],
            "rationale": [],
            "risks": [],
            "uncertainties": [],
            "conditional_purchase": False,
            "variant_collapsed": False,
            "variant_collapsed_reason": None,
            "domain_replies": [],
            "status": "EXECUTABLE",
            "execution_dependencies": [],
            "unmet_quantity_kg": "0",
            "finance_verdict": "PASS",
            "contribution_margin_krw": "3850000",
            "contribution_margin_rate": "0.197",
            "sell_priority": "HIGH",
        },
        "sim_run_id": "SIM-1",
        "sale_date": "2026-09-10",
        "order_date": "2026-09-09",
        "source_order_id": "ORD-1",
        "note": "approved",
        "line": {
            "item_name": "배추",
            "quantity_kg": "8500",
            "unit_price_krw_per_kg": "2300",
            "grade": None,
            "contribution_profit_krw": "3850000",
            "contribution_margin_rate": "0.197",
        },
    }
    data.update(overrides)
    return SalesConfirmationInput.model_validate(data)


class _Cursor:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn
        self.rows: list[object] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params=None):
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        text = " ".join(rendered.split())
        self.rows = []
        self.rowcount = 0

        if "SELECT item_id FROM" in text and ".items" in text:
            item_name = params[0]
            row = self.conn.items.get(item_name)
            self.rows = [] if row is None else [deepcopy(row)]
            return

        if "INSERT INTO" in text and ".sales" in text:
            sale_id = params[0]
            row = {
                "sale_id": params[0],
                "sim_run_id": params[1],
                "customer_partner_id": params[2],
                "order_date": params[3],
                "sale_date": params[4],
                "collection_due_date": params[5],
                "total_quantity_kg": params[6],
                "total_amount_krw": params[7],
                "contribution_profit_krw": params[8],
                "collection_status": params[9],
                "source_order_id": params[10],
                "note": params[11],
                "order_status": params[12],
            }
            existing = self.conn.sales.get(sale_id)
            if existing is None:
                self.conn.sales[sale_id] = deepcopy(row)
                self.rowcount = 1
            return

        if "INSERT INTO" in text and ".sale_items" in text:
            sale_item_id = params[0]
            row = {
                "sale_item_id": params[0],
                "sale_id": params[1],
                "item_id": params[2],
                "grade": params[3],
                "quantity_kg": params[4],
                "unit_price_krw_per_kg": params[5],
                "line_amount_krw": params[6],
                "contribution_profit_krw": params[7],
                "contribution_margin_rate": params[8],
            }
            existing = self.conn.sale_items.get(sale_item_id)
            if existing is None:
                self.conn.sale_items[sale_item_id] = deepcopy(row)
                self.rowcount = 1
            return

        if "UPDATE" in text and ".sales" in text and "SET order_status = 'DELIVERED'" in text:
            row = self.conn.sales.get(params[0])
            if row is not None and row["order_status"] in {"CONFIRMED", "READY"}:
                row["order_status"] = "DELIVERED"
                self.rowcount = 1
            return

        if "SELECT order_status" in text and ".sales" in text:
            row = self.conn.sales.get(params[0])
            self.rows = [] if row is None else [{"order_status": row["order_status"]}]
            return

        if "SELECT * FROM" in text and ".sales" in text:
            row = self.conn.sales.get(params[0])
            self.rows = [] if row is None else [deepcopy(row)]
            return

        if "SELECT * FROM" in text and ".sale_items" in text:
            row = self.conn.sale_items.get(params[0])
            self.rows = [] if row is None else [deepcopy(row)]
            return

        raise AssertionError(f"unexpected SQL: {text}")

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self) -> None:
        self.items = {"배추": {"item_id": "ITEM-BAECHU", "item_name": "배추"}}
        self.sales: dict[str, dict[str, object]] = {}
        self.sale_items: dict[str, dict[str, object]] = {}
        self.transaction_calls: list[str] = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.transaction_calls.append("commit")
        raise AssertionError("caller-owned transaction must not be committed here")

    def rollback(self):
        self.transaction_calls.append("rollback")
        raise AssertionError("caller-owned transaction must not be rolled back here")

    def close(self):
        self.transaction_calls.append("close")
        raise AssertionError("caller-owned connection must not be closed here")


def test_build_sale_confirmation_plan_is_deterministic():
    request = _request()
    plan = build_sale_confirmation_plan(request)

    assert plan.sale_id == sale_id_for(request.execution_identity, request.selected_scenario)
    assert plan.customer_partner_id == "PARTNER-1"
    assert plan.total_quantity_kg == Decimal(8500)
    assert plan.total_amount_krw == Decimal(19550000)
    assert plan.collection_due_date == date(2026, 10, 10)
    assert plan.order_status == "CONFIRMED"
    assert plan.sale_item.sale_item_id == "SI-SALE-RUN-1-SCN-1-1"


def test_confirm_sale_persists_header_and_item_without_commit():
    conn = _Connection()
    result = confirm_sale(conn, _request())

    assert result.sales_written == 1
    assert result.sale_items_written == 1
    assert result.sale_id == "SALE-RUN-1-SCN-1"
    assert result.item_id == "ITEM-BAECHU"
    assert result.quantity_kg == Decimal(8500)
    assert conn.sales[result.sale_id]["source_order_id"] == "ORD-1"
    assert conn.sales[result.sale_id]["order_status"] == "CONFIRMED"
    assert conn.sale_items[result.sale_item_id]["item_id"] == "ITEM-BAECHU"
    assert conn.transaction_calls == []


def test_confirm_sale_retry_is_noop():
    conn = _Connection()
    first = confirm_sale(conn, _request())
    second = confirm_sale(conn, _request())

    assert first.sales_written == 1
    assert first.sale_items_written == 1
    assert second.sales_written == 0
    assert second.sale_items_written == 0


def test_confirm_sale_conflicting_amount_is_closed():
    conn = _Connection()
    confirm_sale(conn, _request())
    conflict = _request(
        selected_scenario_id="SCN-1",
        line={
            "item_name": "배추",
            "quantity_kg": "8500",
            "unit_price_krw_per_kg": "2300",
            "grade": None,
            "contribution_profit_krw": "3850000",
            "contribution_margin_rate": "0.197",
        },
    )
    conflict = conflict.model_copy(
        update={
            "selected_scenario": _scenario(sales_amount_krw=Decimal(19560000)),
        }
    )
    with pytest.raises(SalesPersistenceConflict):
        confirm_sale(conn, conflict)


def test_confirm_sale_accepts_null_source_order_id_when_present_in_contract():
    conn = _Connection()
    request = _request(source_order_id=None)
    result = confirm_sale(conn, request)

    assert result.sales_written == 1
    assert conn.sales[result.sale_id]["source_order_id"] is None


def test_confirm_sale_does_not_mark_delivered_before_shipping():
    conn = _Connection()
    result = confirm_sale(conn, _request())

    assert conn.sales[result.sale_id]["order_status"] == "CONFIRMED"


def test_mark_sale_delivered_is_the_post_shipping_boundary():
    conn = _Connection()
    result = confirm_sale(conn, _request())

    changed = mark_sale_delivered(conn, sale_id=result.sale_id)
    second = mark_sale_delivered(conn, sale_id=result.sale_id)

    assert changed is True
    assert second is False
    assert conn.sales[result.sale_id]["order_status"] == "DELIVERED"
    assert conn.transaction_calls == []


def test_confirm_sale_retry_after_delivery_is_still_idempotent():
    conn = _Connection()
    result = confirm_sale(conn, _request())
    mark_sale_delivered(conn, sale_id=result.sale_id)

    retry = confirm_sale(conn, _request())

    assert retry.sales_written == 0
    assert retry.sale_items_written == 0
