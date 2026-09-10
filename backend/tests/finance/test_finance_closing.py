from datetime import date
from decimal import Decimal

import pytest

from app.finance import closing
from app.finance.db import InventorySnapshot

SIM_RUN_ID = "SIM-WALK-202601"
AS_OF = date(2026, 1, 5)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = str(query)
        self.conn.executed.append((text, params))
        self.rows = []
        self.row = None
        self.rowcount = 0
        if "sim_runs" in text:
            self.rows = [(date(2026, 1, 1), date(2026, 1, 31))]
        elif ".finance_states" in text and "state_date =" in text:
            self.rows = [
                {
                    "financing_mode": "BASE_NO_LOAN",
                    "current_cash_krw": Decimal(9_000),
                    "receivables_krw": Decimal(700),
                    "current_debt_krw": Decimal(0),
                },
                {
                    "financing_mode": "LOAN_BASELINE",
                    "current_cash_krw": Decimal(12_000),
                    "receivables_krw": Decimal(700),
                    "current_debt_krw": Decimal(3_000),
                },
            ]
        elif ".finance_states" in text and "state_date <" in text:
            mode = params[1]
            self.rows = [
                {
                    "financing_mode": mode,
                    "current_cash_krw": Decimal(8_000 if mode == "BASE_NO_LOAN" else 10_000),
                    "receivables_krw": Decimal(1_200),
                    "current_debt_krw": Decimal(0 if mode == "BASE_NO_LOAN" else 2_000),
                }
            ]
        elif "SUM(original_amount_krw)" in text:
            self.row = {"amount": Decimal(500)}
        elif "SUM(outstanding_amount_krw)" in text:
            self.row = {"amount": Decimal(700)}
        elif ".payables" in text:
            self.rows = self.conn.payables
        elif ".expenses" in text:
            self.rows = [
                ("PAYROLL", None, Decimal(100)),
                ("LOGISTICS", "DELIVERY-1", Decimal(30)),
            ]
        elif "SUM(total_amount_krw)" in text:
            self.row = {"amount": Decimal(1_000)}
        elif "INSERT INTO" in text and "daily_closings" in text:
            key = (params["sim_run_id"], params["close_date"])
            if key not in self.conn.closings:
                self.conn.closings[key] = dict(params, closed=True)
                self.rowcount = 1
        elif "UPDATE" in text and "daily_closings" in text:
            key = (params["sim_run_id"], params["close_date"])
            self.conn.closings[key].update(params, closed=True)
            self.rowcount = 1
        else:
            raise AssertionError(text)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, payables):
        self.payables = payables
        self.closings = {}
        self.executed = []
        self.cursor_value = _Cursor(self)

    def cursor(self):
        return self.cursor_value


@pytest.fixture(autouse=True)
def _schema_and_inventory(monkeypatch):
    monkeypatch.setattr(closing, "get_db_schema", lambda: "test_schema")
    calls = []

    def inventory_snapshot(*args, **kwargs):
        calls.append((args, kwargs))
        return InventorySnapshot(
            quantity_kg=Decimal(42),
            inventory_book_value_krw=Decimal(840),
            operational_inventory_value_krw=Decimal(840),
        )

    monkeypatch.setattr(
        closing,
        "load_inventory_snapshot_as_of",
        inventory_snapshot,
    )
    return calls


def _close(conn):
    return closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)


def test_close_day_writes_one_closed_2026_january_row_and_is_idempotent(_schema_and_inventory):
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    first = _close(conn)
    second = _close(conn)

    assert first.status == second.status == "CLOSED"
    assert (first.created, second.created) == (1, 0)
    assert list(conn.closings) == [(SIM_RUN_ID, AS_OF)]
    row = conn.closings[(SIM_RUN_ID, AS_OF)]
    assert row["day_no"] == 5
    assert row["closed"] is True
    assert row["purchase_cash_out_krw"] == Decimal(200)
    assert row["logistics_cash_out_krw"] == Decimal(30)
    assert row["payroll_interest_cash_out_krw"] == Decimal(100)
    assert row["sales_recognized_krw"] == Decimal(1_000)
    assert row["collection_cash_in_krw"] == Decimal(1_000)
    assert row["base_net_cash_krw"] == Decimal(670)
    assert row["base_cash_balance_krw"] == Decimal(9_000)
    assert row["loan_execution_krw"] == Decimal(1_000)
    assert row["loan_cash_balance_krw"] == Decimal(12_000)
    assert row["receivables_balance_krw"] == Decimal(700)
    assert row["inventory_qty_kg"] == Decimal(42)
    assert row["accounting_inventory_cost_krw"] == Decimal(840)
    assert _schema_and_inventory[0][1] == {"sim_run_id": SIM_RUN_ID, "as_of": AS_OF}


def test_persisted_payable_due_date_is_the_purchase_cash_authority():
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    _close(conn)

    assert conn.closings[(SIM_RUN_ID, AS_OF)]["purchase_cash_out_krw"] == Decimal(200)
    assert not any(".purchases" in text for text, _ in conn.executed)


def test_weekend_due_date_is_not_rewritten_and_moves_cash_out_to_monday():
    sunday = date(2026, 1, 4)
    conn = _Connection(payables=[(sunday, Decimal(200))])

    _close(conn)

    assert conn.closings[(SIM_RUN_ID, AS_OF)]["purchase_cash_out_krw"] == Decimal(200)
    assert conn.payables[0][0] == sunday


def test_sim_run_id_is_bound_to_repository_query_not_interpreted_from_its_text():
    sim_run_id = "not-a-date-or-policy-axis"
    conn = _Connection(payables=[])

    closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=sim_run_id)

    sim_run_query = next(text for text, _ in conn.executed if "sim_runs" in text)
    assert "period_start" in sim_run_query
    assert (sim_run_id, AS_OF) in conn.closings
