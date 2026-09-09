from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from app.finance.collection import CollectionEvent
from app.finance.collection import DeterministicCollectionFixtureSource
from app.finance.collection_adapter import FinanceCollectionSource
from app.finance.state_identity import daily_finance_state_id
from app.master.collection import CollectionPartOut

SIM_RUN_ID = "SIM-COLLECTION-SOURCE"
MODE = "LOAN_BASELINE"
AS_OF = date(2026, 1, 10)
RECEIVABLE_ID = "AR-COLLECTION-SOURCE-1"
ORIGINAL = Decimal(1000)


def _state(*, cash: Decimal = Decimal(5000), receivables: Decimal = ORIGINAL) -> dict[str, Any]:
    return {
        "finance_state_id": daily_finance_state_id(
            sim_run_id=SIM_RUN_ID,
            financing_mode=MODE,
            state_date=AS_OF,
        ),
        "sim_run_id": SIM_RUN_ID,
        "state_date": AS_OF,
        "state_type": "DAY",
        "financing_mode": MODE,
        "current_cash_krw": cash,
        "minimum_operating_cash_krw": Decimal(0),
        "committed_outflows_krw": Decimal(0),
        "unsettled_purchase_payables_krw": Decimal(0),
        "receivables_krw": receivables,
        "inventory_book_value_krw": Decimal(0),
        "operational_inventory_value_krw": Decimal(0),
        "current_debt_krw": Decimal(0),
        "recommended_loan_amount_krw": Decimal(0),
        "financial_limit_krw": Decimal(0),
        "note": "source test fixture",
    }


def _receivable(*, sim_run_id: str = SIM_RUN_ID) -> dict[str, Any]:
    return {
        "receivable_id": RECEIVABLE_ID,
        "sim_run_id": sim_run_id,
        "sale_id": "SALE-COLLECTION-SOURCE-1",
        "issued_date": AS_OF,
        "due_date": AS_OF,
        "original_amount_krw": ORIGINAL,
        "received_amount_krw": Decimal(0),
        "outstanding_amount_krw": ORIGINAL,
        "status": "OPEN",
    }


def _event(
    day: date,
    target: object,
    *,
    sim_run_id: str = SIM_RUN_ID,
    mode: str = MODE,
) -> CollectionEvent:
    return CollectionEvent(
        sim_run_id=sim_run_id,
        financing_mode=mode,
        collection_date=day,
        receivable_id=RECEIVABLE_ID,
        target_received_total_krw=target,
    )


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
        self.conn.executed.append((text, deepcopy(params)))
        self.rows = []
        self.rowcount = 0

        if text.startswith("SELECT finance_state_id"):
            matches = [
                row
                for row in self.conn.states.values()
                if row["sim_run_id"] == params["sim_run_id"]
                and row["financing_mode"] == params["financing_mode"]
                and row["state_date"] == params["collection_date"]
            ]
            self.rows = [(row["finance_state_id"],) for row in matches]
            return

        if "SELECT * FROM" in text and ".finance_states" in text:
            row = self.conn.states.get(params[0])
            self.rows = [] if row is None else [deepcopy(row)]
            return

        if "SELECT * FROM" in text and ".receivables" in text:
            row = self.conn.receivables.get(params[0])
            self.rows = [] if row is None else [deepcopy(row)]
            return

        if "UPDATE" in text and ".receivables" in text:
            target, outstanding, status, receivable_id = params
            row = self.conn.receivables.get(receivable_id)
            if row is not None:
                row.update(
                    received_amount_krw=target,
                    outstanding_amount_krw=outstanding,
                    status=status,
                )
                self.rowcount = 1
                self.conn.receivable_updates += 1
            return

        if "UPDATE" in text and ".finance_states" in text:
            cash, receivables, finance_state_id = params
            row = self.conn.states.get(finance_state_id)
            if row is not None:
                row.update(current_cash_krw=cash, receivables_krw=receivables)
                self.rowcount = 1
                self.conn.finance_state_updates += 1
            return

        raise AssertionError(f"unexpected SQL: {text}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, *, states=None, receivable=None) -> None:
        rows = [_state()] if states is None else states
        self.states = {str(row["finance_state_id"]): deepcopy(row) for row in rows}
        receivables = [receivable or _receivable()]
        self.receivables = {str(row["receivable_id"]): deepcopy(row) for row in receivables}
        self.executed: list[tuple[str, object]] = []
        self.receivable_updates = 0
        self.finance_state_updates = 0
        self.transaction_calls: list[str] = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.transaction_calls.append("commit")
        raise AssertionError("Finance collection source must not commit")

    def rollback(self):
        self.transaction_calls.append("rollback")
        raise AssertionError("Finance collection source must not roll back")

    def close(self):
        self.transaction_calls.append("close")
        raise AssertionError("Finance collection source must not close")


@pytest.fixture(autouse=True)
def _schema():
    with patch("app.finance.collection.get_db_schema", return_value="test_schema"):
        yield


def _source(events) -> FinanceCollectionSource:
    return FinanceCollectionSource(
        sim_run_id=SIM_RUN_ID,
        financing_mode=MODE,
        source=DeterministicCollectionFixtureSource(events=events),
    )


def test_empty_source_returns_nothing_due_without_mutation():
    conn = _Connection()

    out = _source(()).collect(conn, as_of=AS_OF)

    assert isinstance(out, CollectionPartOut)
    assert out.status == "NOTHING_DUE"
    assert out.collected == []
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0
    assert conn.transaction_calls == []


def test_event_on_the_day_is_collected_through_finance_transition():
    conn = _Connection()

    out = _source((_event(AS_OF, Decimal(250)),)).collect(conn, as_of=AS_OF)

    state = next(iter(conn.states.values()))
    receivable = conn.receivables[RECEIVABLE_ID]
    assert out.status == "COLLECTED"
    assert out.collected == [RECEIVABLE_ID]
    assert state["current_cash_krw"] == Decimal(5250)
    assert state["receivables_krw"] == Decimal(750)
    assert receivable["received_amount_krw"] == Decimal(250)
    assert receivable["outstanding_amount_krw"] == Decimal(750)


def test_same_cumulative_target_is_idempotent():
    conn = _Connection()
    source = _source((_event(AS_OF, Decimal(250)),))

    first = source.collect(conn, as_of=AS_OF)
    updates_after_first = (conn.receivable_updates, conn.finance_state_updates)
    second = source.collect(conn, as_of=AS_OF)

    state = next(iter(conn.states.values()))
    receivable = conn.receivables[RECEIVABLE_ID]
    assert first.status == "COLLECTED"
    assert second.status == "NOTHING_DUE"
    assert (conn.receivable_updates, conn.finance_state_updates) == updates_after_first
    assert state["current_cash_krw"] == Decimal(5250)
    assert state["receivables_krw"] == Decimal(750)
    assert receivable["received_amount_krw"] == Decimal(250)


def test_other_dates_and_axes_are_not_invented_or_executed():
    conn = _Connection()
    events = (
        _event(date(2026, 1, 9), Decimal(111)),
        _event(AS_OF, Decimal(222), sim_run_id="SIM-OTHER"),
        _event(AS_OF, Decimal(333), mode="BASE_NO_LOAN"),
    )

    out = _source(events).collect(conn, as_of=AS_OF)

    assert out.status == "NOTHING_DUE"
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0


def test_corrupt_source_axis_mismatch_blocks_without_mutation():
    class _CorruptSource:
        def events_for_date(self, **_kwargs):
            return (_event(AS_OF, Decimal(250), sim_run_id="SIM-OTHER"),)

    conn = _Connection()
    source = FinanceCollectionSource(
        sim_run_id=SIM_RUN_ID,
        financing_mode=MODE,
        source=_CorruptSource(),
    )

    out = source.collect(conn, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert "실행 기준" in (out.reason or "")
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0


def test_invalid_event_fails_closed_without_mutation():
    conn = _Connection()

    out = _source((_event(AS_OF, Decimal(1001)),)).collect(conn, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert conn.receivables[RECEIVABLE_ID]["status"] == "OPEN"
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0
