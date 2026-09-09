from __future__ import annotations

import os
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("DB_SCHEMA", "haetdeul")

from app.finance.receivables import (
    ReceivablePersistenceConflict,
    build_receivable_write_plan,
    confirm_receivable,
    receivable_id_for,
)
from app.finance.sales_validation import ReceivableCreateInput
from app.finance.state_identity import daily_finance_state_id

SALE_ID = "SALE-RUN-1-SCN-1"
SIM_RUN_ID = "SIM-1"
MODE = "LOAN_BASELINE"
SALE_DATE = date(2026, 9, 10)
DUEDATE = date(2026, 10, 10)


def _sale_row(**overrides) -> dict[str, object]:
    row = {
        "sale_id": SALE_ID,
        "sim_run_id": SIM_RUN_ID,
        "customer_partner_id": "PARTNER-1",
        "order_date": date(2026, 9, 9),
        "sale_date": SALE_DATE,
        "collection_due_date": DUEDATE,
        "total_quantity_kg": Decimal(8500),
        "total_amount_krw": Decimal(19550000),
        "contribution_profit_krw": Decimal(3850000),
        "collection_status": "OPEN",
        "source_order_id": None,
        "note": "approved",
        "order_status": "CONFIRMED",
    }
    row.update(overrides)
    return row


def _state(
    *, mode: str = MODE, state_date: date = SALE_DATE, receivables: str = "10000000"
) -> dict[str, object]:
    return {
        "finance_state_id": daily_finance_state_id(
            sim_run_id=SIM_RUN_ID, financing_mode=mode, state_date=state_date
        ),
        "sim_run_id": SIM_RUN_ID,
        "state_date": state_date,
        "state_type": "DAY",
        "financing_mode": mode,
        "current_cash_krw": Decimal(20000000),
        "minimum_operating_cash_krw": Decimal(5000000),
        "committed_outflows_krw": Decimal(100000),
        "unsettled_purchase_payables_krw": Decimal(2000000),
        "receivables_krw": Decimal(receivables),
        "inventory_book_value_krw": Decimal(3000000),
        "operational_inventory_value_krw": Decimal(2500000),
        "current_debt_krw": Decimal(1000000),
        "recommended_loan_amount_krw": Decimal(0),
        "financial_limit_krw": Decimal(15000000),
        "note": "TEST",
    }


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

        if "SELECT sale_id, sim_run_id" in text and ".sales" in text:
            row = self.conn.sales.get(params[0])
            self.rows = [] if row is None else [deepcopy(row)]
            return

        if "SELECT finance_state_id" in text and ".finance_states" in text:
            if len(params) == 1:
                row = self.conn.states_by_id.get(params[0])
                self.rows = [] if row is None else [deepcopy(row)]
                return
            matches = [
                row
                for row in self.conn.states_by_id.values()
                if row["sim_run_id"] == params[0]
                and row["financing_mode"] == params[1]
                and row["state_date"] == params[2]
            ]
            self.rows = [deepcopy(row) for row in matches]
            return

        if "INSERT INTO" in text and ".receivables" in text:
            receivable_id = params[0]
            row = {
                "receivable_id": params[0],
                "sim_run_id": params[1],
                "sale_id": params[2],
                "issued_date": params[3],
                "due_date": params[4],
                "original_amount_krw": params[5],
                "received_amount_krw": params[6],
                "outstanding_amount_krw": params[7],
                "status": params[8],
            }
            existing = self.conn.receivables.get(receivable_id)
            if existing is None:
                self.conn.receivables[receivable_id] = deepcopy(row)
                self.rowcount = 1
            return

        if "UPDATE" in text and ".finance_states" in text:
            delta, finance_state_id = params
            row = self.conn.states_by_id.get(finance_state_id)
            if row is not None:
                row["receivables_krw"] = row["receivables_krw"] + delta
                self.rowcount = 1
            return

        if "SELECT * FROM" in text and ".receivables" in text:
            row = next(
                (r for r in self.conn.receivables.values() if r["sale_id"] == params[0]),
                None,
            )
            self.rows = [] if row is None else [deepcopy(row)]
            return

        raise AssertionError(f"unexpected SQL: {text}")

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self) -> None:
        primary = _state()
        secondary = _state(mode="BASE_NO_LOAN")
        self.sales = {SALE_ID: _sale_row()}
        self.states_by_id = {
            primary["finance_state_id"]: primary,
            secondary["finance_state_id"]: secondary,
        }
        self.receivables: dict[str, dict[str, object]] = {}
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


def _request(**overrides) -> ReceivableCreateInput:
    data = {
        "sale_id": SALE_ID,
        "sim_run_id": SIM_RUN_ID,
        "financing_mode": MODE,
        "sale_date": SALE_DATE,
        "customer_partner_id": "PARTNER-1",
        "due_date": DUEDATE,
        "original_amount_krw": Decimal(19550000),
    }
    data.update(overrides)
    return ReceivableCreateInput.model_validate(data)


def test_build_receivable_write_plan_is_exact_and_deterministic():
    plan = build_receivable_write_plan(_request(), sale_row=_sale_row())
    assert plan.receivable_id == receivable_id_for(SALE_ID)
    assert plan.finance_state_id == daily_finance_state_id(
        sim_run_id=SIM_RUN_ID, financing_mode=MODE, state_date=SALE_DATE
    )
    assert plan.original_amount_krw == Decimal(19550000)
    assert plan.due_date == DUEDATE


def test_confirm_receivable_persists_receivable_and_updates_exact_state():
    conn = _Connection()
    result = confirm_receivable(conn, _request())

    assert result.receivables_written == 1
    assert result.finance_state_updates == 1
    assert conn.receivables[result.receivable_id]["status"] == "OPEN"
    assert conn.states_by_id[result.finance_state_id]["receivables_krw"] == Decimal(
        29550000
    )
    assert conn.transaction_calls == []


def test_confirm_receivable_retry_is_noop():
    conn = _Connection()
    first = confirm_receivable(conn, _request())
    second = confirm_receivable(conn, _request())

    assert first.receivables_written == 1
    assert first.finance_state_updates == 1
    assert second.receivables_written == 0
    assert second.finance_state_updates == 0


def test_confirm_receivable_updates_only_requested_financing_mode():
    conn = _Connection()
    confirm_receivable(conn, _request())

    other_state = _state(mode="BASE_NO_LOAN")
    assert conn.states_by_id[other_state["finance_state_id"]]["receivables_krw"] == Decimal(
        10000000
    )


def test_confirm_receivable_conflicting_sale_fields_fail_closed():
    conn = _Connection()
    wrong = _request(original_amount_krw=Decimal(19560000))
    with pytest.raises(ReceivablePersistenceConflict):
        confirm_receivable(conn, wrong)
