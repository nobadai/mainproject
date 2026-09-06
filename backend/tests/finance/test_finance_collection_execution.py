"""Explicit Collection fixture to Finance ledger execution regressions."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import db as finance_db
from app.finance.collection import (
    CollectionEvent,
    FinanceCollectionConflict,
    apply_collection_event,
    apply_explicit_collection,
)
from app.finance.day_open import FinanceDayOpening
from app.finance.db import FinanceDataNotReady, PostgresFinanceAsOfDataPort
from app.finance.state_identity import daily_finance_state_id

SIM_RUN_ID = "SIM-COLLECTION-30D"
MODE = "LOAN_BASELINE"
START = date(2026, 1, 7)
END = date(2026, 2, 5)
RECEIVABLE_ID = "AR-COLLECTION-1"
ORIGINAL = Decimal(10_000_000)


def _state(
    state_date: date,
    *,
    mode: str = MODE,
    state_id: str | None = None,
    cash: Decimal = Decimal(20_000_000),
    receivables: Decimal = ORIGINAL,
) -> dict[str, object]:
    return {
        "finance_state_id": state_id
        or daily_finance_state_id(
            sim_run_id=SIM_RUN_ID,
            financing_mode=mode,
            state_date=state_date,
        ),
        "sim_run_id": SIM_RUN_ID,
        "state_date": state_date,
        "state_type": "DAY",
        "financing_mode": mode,
        "current_cash_krw": cash,
        "minimum_operating_cash_krw": Decimal(5_000_000),
        "committed_outflows_krw": Decimal(100_000),
        "unsettled_purchase_payables_krw": Decimal(2_000_000),
        "receivables_krw": receivables,
        "inventory_book_value_krw": Decimal(3_000_000),
        "operational_inventory_value_krw": Decimal(2_500_000),
        "current_debt_krw": Decimal(1_000_000),
        "recommended_loan_amount_krw": Decimal(0),
        "financial_limit_krw": Decimal(15_000_000),
        "note": "SIMULATION TEST FIXTURE",
    }


def _receivable(*, sim_run_id: str = SIM_RUN_ID) -> dict[str, object]:
    return {
        "receivable_id": RECEIVABLE_ID,
        "sim_run_id": sim_run_id,
        "sale_id": "SALE-COLLECTION-1",
        "issued_date": START,
        "due_date": date(2026, 1, 8),
        "original_amount_krw": ORIGINAL,
        "received_amount_krw": Decimal(0),
        "outstanding_amount_krw": ORIGINAL,
        "status": "OPEN",
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
        self.conn.executed.append((text, deepcopy(params)))
        self.rows = []
        self.rowcount = 0

        if "SELECT DISTINCT sim_run_id, financing_mode" in text:
            self.rows = [(SIM_RUN_ID, MODE)]
            return

        if text.startswith("SELECT finance_state_id"):
            state_date = params.get("collection_date", params.get("state_date"))
            matches = [
                row
                for row in self.conn.states.values()
                if row["sim_run_id"] == params["sim_run_id"]
                and (
                    "financing_mode = %(financing_mode)s" not in text
                    or row["financing_mode"] == params["financing_mode"]
                )
                and row["state_date"] == state_date
            ]
            self.rows = [(row["finance_state_id"],) for row in matches]
            return

        if "INSERT INTO" in text and ".finance_states" in text:
            source = next(
                (
                    row
                    for row in self.conn.states.values()
                    if row["sim_run_id"] == params["sim_run_id"]
                    and row["financing_mode"] == params["financing_mode"]
                    and row["state_date"] == params["carry_from"]
                ),
                None,
            )
            if source is not None and params["finance_state_id"] not in self.conn.states:
                carried = deepcopy(source)
                carried.update(
                    finance_state_id=params["finance_state_id"],
                    state_date=params["as_of"],
                    state_type=params["state_type"],
                    note=params["note"],
                )
                self.conn.states[str(carried["finance_state_id"])] = carried
                self.rowcount = 1
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
    def __init__(self, *, states, receivables) -> None:
        self.states = {str(row["finance_state_id"]): deepcopy(row) for row in states}
        self.receivables = {
            str(row["receivable_id"]): deepcopy(row) for row in receivables
        }
        self.executed: list[tuple[str, object]] = []
        self.receivable_updates = 0
        self.finance_state_updates = 0
        self.transaction_calls: list[str] = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.transaction_calls.append("commit")
        raise AssertionError("Finance collection must not commit")

    def rollback(self):
        self.transaction_calls.append("rollback")
        raise AssertionError("Finance collection must not roll back")

    def close(self):
        self.transaction_calls.append("close")
        raise AssertionError("Finance collection must not close")


@pytest.fixture(autouse=True)
def _schema():
    with (
        patch("app.finance.collection.get_db_schema", return_value="test_schema"),
        patch("app.finance.day_open.get_db_schema", return_value="test_schema"),
    ):
        yield


def _connection(*, states=None, receivable=None) -> _Connection:
    source_date = START - timedelta(days=1)
    return _Connection(
        states=[_state(source_date)] if states is None else states,
        receivables=[receivable or _receivable()],
    )


def _open(conn: _Connection, state_date: date, carry_from: date) -> None:
    FinanceDayOpening().open_day(conn, as_of=state_date, carry_from=carry_from)


def _select_state(conn: _Connection, finance_state_id: str) -> dict[str, object]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM test_schema.finance_states WHERE finance_state_id = %s",
            [finance_state_id],
        )
        row = cursor.fetchone()
    assert isinstance(row, dict)
    return row


def _select_receivable(conn: _Connection) -> dict[str, object]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM test_schema.receivables WHERE receivable_id = %s",
            [RECEIVABLE_ID],
        )
        row = cursor.fetchone()
    assert isinstance(row, dict)
    return row


def _event(collection_date: date, target: object, *, mode: str = MODE) -> CollectionEvent:
    return CollectionEvent(
        sim_run_id=SIM_RUN_ID,
        financing_mode=mode,
        collection_date=collection_date,
        receivable_id=RECEIVABLE_ID,
        target_received_total_krw=target,
    )


def test_30_day_walk_changes_ledgers_only_on_explicit_event_days(monkeypatch):
    conn = _connection()
    fixtures = (
        _event(date(2026, 1, 15), Decimal(4_000_000)),
        _event(date(2026, 1, 25), Decimal(7_000_000)),
        _event(date(2026, 2, 5), Decimal(10_000_000)),
    )
    fixture_by_date = {event.collection_date: event for event in fixtures}
    snapshots: dict[date, tuple[Decimal, Decimal, Decimal, str]] = {}
    previous = START - timedelta(days=1)

    current = START
    while current <= END:
        _open(conn, current, previous)
        event = fixture_by_date.get(current)
        if event is not None:
            apply_explicit_collection(conn, event)
        state_id = daily_finance_state_id(
            sim_run_id=SIM_RUN_ID,
            financing_mode=MODE,
            state_date=current,
        )
        state = _select_state(conn, state_id)
        receivable = _select_receivable(conn)
        snapshots[current] = (
            Decimal(state["current_cash_krw"]),
            Decimal(state["receivables_krw"]),
            Decimal(receivable["received_amount_krw"]),
            str(receivable["status"]),
        )
        previous = current
        current += timedelta(days=1)

    assert snapshots[date(2026, 1, 14)] == (
        Decimal(20_000_000),
        Decimal(10_000_000),
        Decimal(0),
        "OPEN",
    )
    assert snapshots[date(2026, 1, 15)] == (
        Decimal(24_000_000),
        Decimal(6_000_000),
        Decimal(4_000_000),
        "PARTIAL",
    )
    assert snapshots[date(2026, 1, 25)] == (
        Decimal(27_000_000),
        Decimal(3_000_000),
        Decimal(7_000_000),
        "PARTIAL",
    )
    assert snapshots[date(2026, 2, 5)] == (
        Decimal(30_000_000),
        Decimal(0),
        Decimal(10_000_000),
        "COLLECTED",
    )
    for state_date, snapshot in snapshots.items():
        if state_date not in fixture_by_date and state_date > START:
            assert snapshot[:2] == snapshots[state_date - timedelta(days=1)][:2]

    final_state = conn.states[
        daily_finance_state_id(
            sim_run_id=SIM_RUN_ID,
            financing_mode=MODE,
            state_date=END,
        )
    ]
    monkeypatch.setattr(
        finance_db,
        "get_finance_runtime_axis",
        lambda: {"sim_run_id": SIM_RUN_ID, "financing_mode": MODE},
    )
    monkeypatch.setattr(finance_db, "fetch_all", lambda *_args, **_kwargs: [final_state])
    runtime_position = PostgresFinanceAsOfDataPort().load_finance_position(END)
    assert runtime_position["current_cash_krw"] == Decimal(30_000_000)
    assert runtime_position["receivables_krw"] == 0


def test_same_cumulative_target_is_a_noop_and_caller_owns_transaction():
    event_date = START
    conn = _connection()
    _open(conn, event_date, event_date - timedelta(days=1))

    first = apply_explicit_collection(conn, _event(event_date, Decimal(4_000_000)))
    updates_after_first = (conn.receivable_updates, conn.finance_state_updates)
    second = apply_explicit_collection(conn, _event(event_date, Decimal(4_000_000)))

    assert first.delta_received_krw == Decimal(4_000_000)
    assert second.delta_received_krw == 0
    assert (conn.receivable_updates, conn.finance_state_updates) == updates_after_first
    assert conn.transaction_calls == []


@pytest.mark.parametrize(
    "target",
    [Decimal(-1), 1.5, Decimal("NaN"), Decimal(10_000_001)],
)
def test_invalid_collection_target_fails_closed(target):
    conn = _connection()
    _open(conn, START, START - timedelta(days=1))

    with pytest.raises(FinanceCollectionConflict):
        apply_explicit_collection(conn, _event(START, target))

    assert conn.receivables[RECEIVABLE_ID]["status"] == "OPEN"
    assert conn.receivable_updates == conn.finance_state_updates == 0


def test_cumulative_target_regression_fails_closed():
    conn = _connection()
    _open(conn, START, START - timedelta(days=1))
    apply_explicit_collection(conn, _event(START, Decimal(7_000_000)))
    before = deepcopy(conn.receivables[RECEIVABLE_ID])

    with pytest.raises(FinanceCollectionConflict, match="cannot regress"):
        apply_explicit_collection(conn, _event(START, Decimal(6_000_000)))

    assert conn.receivables[RECEIVABLE_ID] == before


def test_requested_financing_mode_is_the_only_state_changed():
    event_date = START
    loan = _state(event_date, mode=MODE, state_id="FIN-LOAN")
    base = _state(
        event_date,
        mode="BASE_NO_LOAN",
        state_id="FIN-BASE",
        cash=Decimal(8_000_000),
    )
    conn = _connection(states=[loan, base])
    base_before = deepcopy(conn.states["FIN-BASE"])

    plan = apply_collection_event(
        conn,
        sim_run_id=SIM_RUN_ID,
        financing_mode=MODE,
        collection_date=event_date,
        receivable_id=RECEIVABLE_ID,
        target_received_total_krw=Decimal(4_000_000),
    )

    assert plan.finance_state_id == "FIN-LOAN"
    assert conn.states["FIN-LOAN"]["current_cash_krw"] == Decimal(24_000_000)
    assert conn.states["FIN-BASE"] == base_before


@pytest.mark.parametrize("states", [[], [_state(START, mode="BASE_NO_LOAN")]])
def test_missing_exact_collection_state_does_not_carry_or_fallback(states):
    conn = _connection(states=states)
    before = deepcopy(conn.states)

    with pytest.raises(FinanceDataNotReady) as raised:
        apply_explicit_collection(conn, _event(START, Decimal(4_000_000)))

    assert raised.value.key == "historical_finance_position"
    assert conn.states == before
    assert conn.receivables[RECEIVABLE_ID]["status"] == "OPEN"


def test_duplicate_exact_collection_state_fails_closed():
    conn = _connection(
        states=[
            _state(START, state_id="FIN-ONE"),
            _state(START, state_id="FIN-TWO"),
        ]
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        apply_explicit_collection(conn, _event(START, Decimal(4_000_000)))

    assert raised.value.key == "finance_state_ambiguous"
    assert conn.receivable_updates == conn.finance_state_updates == 0


def test_receivable_and_state_sim_runs_must_match():
    conn = _connection(states=[_state(START)], receivable=_receivable(sim_run_id="SIM-OTHER"))

    with pytest.raises(FinanceCollectionConflict, match="axes do not match"):
        apply_explicit_collection(conn, _event(START, Decimal(4_000_000)))

    assert conn.receivable_updates == conn.finance_state_updates == 0


def test_finance_receivables_underflow_fails_before_either_update():
    conn = _connection(states=[_state(START, receivables=Decimal(3_000_000))])

    with pytest.raises(FinanceCollectionConflict, match="cannot cover"):
        apply_explicit_collection(conn, _event(START, Decimal(4_000_000)))

    assert conn.receivables[RECEIVABLE_ID]["status"] == "OPEN"
    assert conn.receivable_updates == conn.finance_state_updates == 0
