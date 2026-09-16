from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance.cash_adjustments import CashAdjustmentConflict, record_cash_adjustment


class _Cursor:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn
        self.rows: list[dict[str, object]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params):
        text = " ".join(
            (query.as_string(None) if hasattr(query, "as_string") else str(query)).split()
        )
        self.rows = []
        self.rowcount = 0
        if "SELECT finance_state_id, current_cash_krw" in text:
            self.rows = [dict(self.conn.state)]
        elif "INSERT INTO" in text and "finance_cash_adjustments" in text:
            self.conn.entries.append(tuple(params))
            self.rowcount = 1
        elif "UPDATE" in text and "finance_states" in text:
            self.conn.state["current_cash_krw"] = params[0]
            self.rowcount = 1
        else:
            raise AssertionError(text)

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, cash: Decimal) -> None:
        self.state = {"finance_state_id": "FIN-1", "current_cash_krw": cash}
        self.entries: list[tuple[object, ...]] = []

    def cursor(self):
        return _Cursor(self)


def _record(conn: _Connection, *, direction: str = "INFLOW", amount: Decimal = Decimal(300)):
    return record_cash_adjustment(
        conn,
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        adjustment_date=date(2026, 9, 16),
        direction=direction,
        category="OWNER_INJECTION" if direction == "INFLOW" else "OWNER_WITHDRAWAL",
        amount_krw=amount,
        source_ref="BANK-001",
        note="운영 자금",
        recorded_by="tester",
    )


def test_user_cash_inflow_is_ledgered_and_updates_the_exact_state(monkeypatch):
    monkeypatch.setattr("app.finance.cash_adjustments.get_db_schema", lambda: "haetdeul")
    conn = _Connection(Decimal(1000))

    result = _record(conn)

    assert result.current_cash_krw == Decimal(1300)
    assert conn.state["current_cash_krw"] == Decimal(1300)
    assert len(conn.entries) == 1
    assert conn.entries[0][4] == "INFLOW"


def test_user_cash_outflow_cannot_make_the_state_negative(monkeypatch):
    monkeypatch.setattr("app.finance.cash_adjustments.get_db_schema", lambda: "haetdeul")
    conn = _Connection(Decimal(100))

    with pytest.raises(CashAdjustmentConflict, match="0원"):
        _record(conn, direction="OUTFLOW", amount=Decimal(101))

    assert conn.entries == []
    assert conn.state["current_cash_krw"] == Decimal(100)
