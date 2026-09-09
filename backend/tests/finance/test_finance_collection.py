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

source_SIM_RUN_ID = "SIM-COLLECTION-SOURCE"
source_MODE = "LOAN_BASELINE"
source_AS_OF = date(2026, 1, 10)
source_RECEIVABLE_ID = "AR-COLLECTION-SOURCE-1"
source_ORIGINAL = Decimal(1000)


def source_state(*, cash: Decimal = Decimal(5000), receivables: Decimal = source_ORIGINAL) -> dict[str, Any]:
    return {
        "finance_state_id": daily_finance_state_id(
            sim_run_id=source_SIM_RUN_ID,
            financing_mode=source_MODE,
            state_date=source_AS_OF,
        ),
        "sim_run_id": source_SIM_RUN_ID,
        "state_date": source_AS_OF,
        "state_type": "DAY",
        "financing_mode": source_MODE,
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


def source_receivable(*, sim_run_id: str = source_SIM_RUN_ID) -> dict[str, Any]:
    return {
        "receivable_id": source_RECEIVABLE_ID,
        "sim_run_id": sim_run_id,
        "sale_id": "SALE-COLLECTION-SOURCE-1",
        "issued_date": source_AS_OF,
        "due_date": source_AS_OF,
        "original_amount_krw": source_ORIGINAL,
        "received_amount_krw": Decimal(0),
        "outstanding_amount_krw": source_ORIGINAL,
        "status": "OPEN",
    }


def source_event(
    day: date,
    target: object,
    *,
    sim_run_id: str = source_SIM_RUN_ID,
    mode: str = source_MODE,
) -> CollectionEvent:
    return CollectionEvent(
        sim_run_id=sim_run_id,
        financing_mode=mode,
        collection_date=day,
        receivable_id=source_RECEIVABLE_ID,
        target_received_total_krw=target,
    )


class source_Cursor:
    def __init__(self, conn: source_Connection) -> None:
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


class source_Connection:
    def __init__(self, *, states=None, receivable=None) -> None:
        rows = [source_state()] if states is None else states
        self.states = {str(row["finance_state_id"]): deepcopy(row) for row in rows}
        receivables = [receivable or source_receivable()]
        self.receivables = {str(row["receivable_id"]): deepcopy(row) for row in receivables}
        self.executed: list[tuple[str, object]] = []
        self.receivable_updates = 0
        self.finance_state_updates = 0
        self.transaction_calls: list[str] = []

    def cursor(self):
        return source_Cursor(self)

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
def source_schema():
    with patch("app.finance.collection.get_db_schema", return_value="test_schema"):
        yield


def source_source(events) -> FinanceCollectionSource:
    return FinanceCollectionSource(
        sim_run_id=source_SIM_RUN_ID,
        financing_mode=source_MODE,
        source=DeterministicCollectionFixtureSource(events=events),
    )


def test_empty_source_returns_nothing_due_without_mutation():
    conn = source_Connection()

    out = source_source(()).collect(conn, as_of=source_AS_OF)

    assert isinstance(out, CollectionPartOut)
    assert out.status == "NOTHING_DUE"
    assert out.collected == []
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0
    assert conn.transaction_calls == []


def test_event_on_the_day_is_collected_through_finance_transition():
    conn = source_Connection()

    out = source_source((source_event(source_AS_OF, Decimal(250)),)).collect(conn, as_of=source_AS_OF)

    state = next(iter(conn.states.values()))
    receivable = conn.receivables[source_RECEIVABLE_ID]
    assert out.status == "COLLECTED"
    assert out.collected == [source_RECEIVABLE_ID]
    assert state["current_cash_krw"] == Decimal(5250)
    assert state["receivables_krw"] == Decimal(750)
    assert receivable["received_amount_krw"] == Decimal(250)
    assert receivable["outstanding_amount_krw"] == Decimal(750)


def test_same_cumulative_target_is_idempotent():
    conn = source_Connection()
    source = source_source((source_event(source_AS_OF, Decimal(250)),))

    first = source.collect(conn, as_of=source_AS_OF)
    updates_after_first = (conn.receivable_updates, conn.finance_state_updates)
    second = source.collect(conn, as_of=source_AS_OF)

    state = next(iter(conn.states.values()))
    receivable = conn.receivables[source_RECEIVABLE_ID]
    assert first.status == "COLLECTED"
    assert second.status == "NOTHING_DUE"
    assert (conn.receivable_updates, conn.finance_state_updates) == updates_after_first
    assert state["current_cash_krw"] == Decimal(5250)
    assert state["receivables_krw"] == Decimal(750)
    assert receivable["received_amount_krw"] == Decimal(250)


def test_other_dates_and_axes_are_not_invented_or_executed():
    conn = source_Connection()
    events = (
        source_event(date(2026, 1, 9), Decimal(111)),
        source_event(source_AS_OF, Decimal(222), sim_run_id="SIM-OTHER"),
        source_event(source_AS_OF, Decimal(333), mode="BASE_NO_LOAN"),
    )

    out = source_source(events).collect(conn, as_of=source_AS_OF)

    assert out.status == "NOTHING_DUE"
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0


def test_corrupt_source_axis_mismatch_blocks_without_mutation():
    class _CorruptSource:
        def events_for_date(self, **_kwargs):
            return (source_event(source_AS_OF, Decimal(250), sim_run_id="SIM-OTHER"),)

    conn = source_Connection()
    source = FinanceCollectionSource(
        sim_run_id=source_SIM_RUN_ID,
        financing_mode=source_MODE,
        source=_CorruptSource(),
    )

    out = source.collect(conn, as_of=source_AS_OF)

    assert out.status == "BLOCKED"
    assert "실행 기준" in (out.reason or "")
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0


def test_invalid_event_fails_closed_without_mutation():
    conn = source_Connection()

    out = source_source((source_event(source_AS_OF, Decimal(1001)),)).collect(conn, as_of=source_AS_OF)

    assert out.status == "BLOCKED"
    assert conn.receivables[source_RECEIVABLE_ID]["status"] == "OPEN"
    assert conn.receivable_updates == 0
    assert conn.finance_state_updates == 0


from datetime import date
from decimal import Decimal

from app.finance.collection import CollectionEvent
from app.finance.collection import DeterministicCollectionFixtureSource


def fixture_event(
    day: date,
    target: object,
    *,
    sim_run_id: str = "SIM-1",
    financing_mode: str = "LOAN_BASELINE",
    receivable_id: str = "AR-1",
) -> CollectionEvent:
    return CollectionEvent(
        sim_run_id=sim_run_id,
        financing_mode=financing_mode,
        collection_date=day,
        receivable_id=receivable_id,
        target_received_total_krw=target,
    )


def test_returns_only_explicit_events_for_the_requested_date():
    event = fixture_event(date(2026, 1, 15), Decimal(4_000_000))
    source = DeterministicCollectionFixtureSource.from_events([event], source_ref="TEST")

    assert source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    ) == (event,)


def test_other_dates_return_empty_without_due_date_inference():
    source = DeterministicCollectionFixtureSource.from_events(
        [fixture_event(date(2026, 1, 15), Decimal(4_000_000))],
        source_ref="TEST",
    )

    assert source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 14),
    ) == ()


def test_sim_run_id_isolated():
    source = DeterministicCollectionFixtureSource.from_events(
        [fixture_event(date(2026, 1, 15), Decimal(4_000_000), sim_run_id="SIM-1")],
        source_ref="TEST",
    )

    assert source.events_for_date(
        sim_run_id="SIM-2",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    ) == ()


def test_financing_mode_isolated():
    loan = fixture_event(date(2026, 1, 15), Decimal(4_000_000), financing_mode="LOAN_BASELINE")
    base = fixture_event(date(2026, 1, 15), Decimal(1_000_000), financing_mode="BASE_NO_LOAN")
    source = DeterministicCollectionFixtureSource.from_events([loan, base], source_ref="TEST")

    assert source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    ) == (loan,)


def test_cumulative_target_is_preserved_exactly():
    event = fixture_event(date(2026, 1, 15), Decimal("4000000.123456"))
    source = DeterministicCollectionFixtureSource.from_events([event], source_ref="TEST")

    got = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )

    assert got[0].target_received_total_krw == Decimal("4000000.123456")


def test_repeated_lookup_is_deterministic():
    events = (
        fixture_event(date(2026, 1, 15), Decimal(4_000_000), receivable_id="AR-1"),
        fixture_event(date(2026, 1, 15), Decimal(2_000_000), receivable_id="AR-2"),
    )
    source = DeterministicCollectionFixtureSource(events=events, source_ref="TEST")

    first = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )
    second = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )

    assert first == events
    assert second == events


def test_provider_reuses_existing_collection_event_type():
    event = fixture_event(date(2026, 1, 15), Decimal(4_000_000))
    source = DeterministicCollectionFixtureSource.from_events([event], source_ref="TEST")

    got = source.events_for_date(
        sim_run_id="SIM-1",
        financing_mode="LOAN_BASELINE",
        as_of=date(2026, 1, 15),
    )

    assert isinstance(got[0], CollectionEvent)


def test_source_marks_sim_fixed_fixture_evidence():
    source = DeterministicCollectionFixtureSource.from_events([], source_ref="TEST")

    assert source.evidence_grade == "SIM_FIXED"
    assert source.source_ref == "TEST"


"""Explicit Collection fixture to Finance ledger execution regressions."""

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
from app.finance.db import FinanceDataNotReady, InventorySnapshot, PostgresFinanceAsOfDataPort
from app.finance.state_identity import daily_finance_state_id

execution_SIM_RUN_ID = "SIM-COLLECTION-30D"
execution_MODE = "LOAN_BASELINE"
execution_START = date(2026, 1, 7)
execution_END = date(2026, 2, 5)
execution_RECEIVABLE_ID = "AR-COLLECTION-1"
execution_ORIGINAL = Decimal(10_000_000)


@pytest.fixture(autouse=True)
def execution_inventory_snapshot():
    with patch(
        "app.finance.day_open.load_inventory_snapshot_as_of",
        return_value=InventorySnapshot(
            Decimal(1), Decimal(3_000_000), Decimal(2_500_000)
        ),
    ):
        yield


def execution_state(
    state_date: date,
    *,
    mode: str = execution_MODE,
    state_id: str | None = None,
    cash: Decimal = Decimal(20_000_000),
    receivables: Decimal = execution_ORIGINAL,
) -> dict[str, object]:
    return {
        "finance_state_id": state_id
        or daily_finance_state_id(
            sim_run_id=execution_SIM_RUN_ID,
            financing_mode=mode,
            state_date=state_date,
        ),
        "sim_run_id": execution_SIM_RUN_ID,
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


def execution_receivable(*, sim_run_id: str = execution_SIM_RUN_ID) -> dict[str, object]:
    return {
        "receivable_id": execution_RECEIVABLE_ID,
        "sim_run_id": sim_run_id,
        "sale_id": "SALE-COLLECTION-1",
        "issued_date": execution_START,
        "due_date": date(2026, 1, 8),
        "original_amount_krw": execution_ORIGINAL,
        "received_amount_krw": Decimal(0),
        "outstanding_amount_krw": execution_ORIGINAL,
        "status": "OPEN",
    }


class execution_Cursor:
    def __init__(self, conn: execution_Connection) -> None:
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
            self.rows = [(execution_SIM_RUN_ID, execution_MODE)]
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
                    inventory_book_value_krw=params["inventory_book_value_krw"],
                    operational_inventory_value_krw=params[
                        "operational_inventory_value_krw"
                    ],
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


class execution_Connection:
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
        return execution_Cursor(self)

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
def execution_schema():
    with (
        patch("app.finance.collection.get_db_schema", return_value="test_schema"),
        patch("app.finance.day_open.get_db_schema", return_value="test_schema"),
    ):
        yield


def execution_connection(*, states=None, receivable=None) -> execution_Connection:
    source_date = execution_START - timedelta(days=1)
    return execution_Connection(
        states=[execution_state(source_date)] if states is None else states,
        receivables=[receivable or execution_receivable()],
    )


def execution_open(conn: execution_Connection, state_date: date, carry_from: date) -> None:
    FinanceDayOpening().open_day(conn, as_of=state_date, carry_from=carry_from)


def execution_select_state(conn: execution_Connection, finance_state_id: str) -> dict[str, object]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM test_schema.finance_states WHERE finance_state_id = %s",
            [finance_state_id],
        )
        row = cursor.fetchone()
    assert isinstance(row, dict)
    return row


def execution_select_receivable(conn: execution_Connection) -> dict[str, object]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM test_schema.receivables WHERE receivable_id = %s",
            [execution_RECEIVABLE_ID],
        )
        row = cursor.fetchone()
    assert isinstance(row, dict)
    return row


def execution_event(collection_date: date, target: object, *, mode: str = execution_MODE) -> CollectionEvent:
    return CollectionEvent(
        sim_run_id=execution_SIM_RUN_ID,
        financing_mode=mode,
        collection_date=collection_date,
        receivable_id=execution_RECEIVABLE_ID,
        target_received_total_krw=target,
    )


def test_30_day_walk_changes_ledgers_only_on_explicit_event_days(monkeypatch):
    conn = execution_connection()
    fixtures = (
        execution_event(date(2026, 1, 15), Decimal(4_000_000)),
        execution_event(date(2026, 1, 25), Decimal(7_000_000)),
        execution_event(date(2026, 2, 5), Decimal(10_000_000)),
    )
    fixture_by_date = {event.collection_date: event for event in fixtures}
    snapshots: dict[date, tuple[Decimal, Decimal, Decimal, str]] = {}
    previous = execution_START - timedelta(days=1)

    current = execution_START
    while current <= execution_END:
        execution_open(conn, current, previous)
        event = fixture_by_date.get(current)
        if event is not None:
            apply_explicit_collection(conn, event)
        state_id = daily_finance_state_id(
            sim_run_id=execution_SIM_RUN_ID,
            financing_mode=execution_MODE,
            state_date=current,
        )
        state = execution_select_state(conn, state_id)
        receivable = execution_select_receivable(conn)
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
        if state_date not in fixture_by_date and state_date > execution_START:
            assert snapshot[:2] == snapshots[state_date - timedelta(days=1)][:2]

    final_state = conn.states[
        daily_finance_state_id(
            sim_run_id=execution_SIM_RUN_ID,
            financing_mode=execution_MODE,
            state_date=execution_END,
        )
    ]
    monkeypatch.setattr(
        finance_db,
        "get_finance_runtime_axis",
        lambda: {"sim_run_id": execution_SIM_RUN_ID, "financing_mode": execution_MODE},
    )
    monkeypatch.setattr(finance_db, "fetch_all", lambda *_args, **_kwargs: [final_state])
    runtime_position = PostgresFinanceAsOfDataPort().load_finance_position(execution_END)
    assert runtime_position["current_cash_krw"] == Decimal(30_000_000)
    assert runtime_position["receivables_krw"] == 0


def test_same_cumulative_target_is_a_noop_and_caller_owns_transaction():
    event_date = execution_START
    conn = execution_connection()
    execution_open(conn, event_date, event_date - timedelta(days=1))

    first = apply_explicit_collection(conn, execution_event(event_date, Decimal(4_000_000)))
    updates_after_first = (conn.receivable_updates, conn.finance_state_updates)
    second = apply_explicit_collection(conn, execution_event(event_date, Decimal(4_000_000)))

    assert first.delta_received_krw == Decimal(4_000_000)
    assert second.delta_received_krw == 0
    assert (conn.receivable_updates, conn.finance_state_updates) == updates_after_first
    assert conn.transaction_calls == []


@pytest.mark.parametrize(
    "target",
    [Decimal(-1), 1.5, Decimal("NaN"), Decimal(10_000_001)],
)
def test_invalid_collection_target_fails_closed(target):
    conn = execution_connection()
    execution_open(conn, execution_START, execution_START - timedelta(days=1))

    with pytest.raises(FinanceCollectionConflict):
        apply_explicit_collection(conn, execution_event(execution_START, target))

    assert conn.receivables[execution_RECEIVABLE_ID]["status"] == "OPEN"
    assert conn.receivable_updates == conn.finance_state_updates == 0


def test_cumulative_target_regression_fails_closed():
    conn = execution_connection()
    execution_open(conn, execution_START, execution_START - timedelta(days=1))
    apply_explicit_collection(conn, execution_event(execution_START, Decimal(7_000_000)))
    before = deepcopy(conn.receivables[execution_RECEIVABLE_ID])

    with pytest.raises(FinanceCollectionConflict, match="cannot regress"):
        apply_explicit_collection(conn, execution_event(execution_START, Decimal(6_000_000)))

    assert conn.receivables[execution_RECEIVABLE_ID] == before


def test_requested_financing_mode_is_the_only_state_changed():
    event_date = execution_START
    loan = execution_state(event_date, mode=execution_MODE, state_id="FIN-LOAN")
    base = execution_state(
        event_date,
        mode="BASE_NO_LOAN",
        state_id="FIN-BASE",
        cash=Decimal(8_000_000),
    )
    conn = execution_connection(states=[loan, base])
    base_before = deepcopy(conn.states["FIN-BASE"])

    plan = apply_collection_event(
        conn,
        sim_run_id=execution_SIM_RUN_ID,
        financing_mode=execution_MODE,
        collection_date=event_date,
        receivable_id=execution_RECEIVABLE_ID,
        target_received_total_krw=Decimal(4_000_000),
    )

    assert plan.finance_state_id == "FIN-LOAN"
    assert conn.states["FIN-LOAN"]["current_cash_krw"] == Decimal(24_000_000)
    assert conn.states["FIN-BASE"] == base_before


@pytest.mark.parametrize("states", [[], [execution_state(execution_START, mode="BASE_NO_LOAN")]])
def test_missing_exact_collection_state_does_not_carry_or_fallback(states):
    conn = execution_connection(states=states)
    before = deepcopy(conn.states)

    with pytest.raises(FinanceDataNotReady) as raised:
        apply_explicit_collection(conn, execution_event(execution_START, Decimal(4_000_000)))

    assert raised.value.key == "historical_finance_position"
    assert conn.states == before
    assert conn.receivables[execution_RECEIVABLE_ID]["status"] == "OPEN"


def test_duplicate_exact_collection_state_fails_closed():
    conn = execution_connection(
        states=[
            execution_state(execution_START, state_id="FIN-ONE"),
            execution_state(execution_START, state_id="FIN-TWO"),
        ]
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        apply_explicit_collection(conn, execution_event(execution_START, Decimal(4_000_000)))

    assert raised.value.key == "finance_state_ambiguous"
    assert conn.receivable_updates == conn.finance_state_updates == 0


def test_receivable_and_state_sim_runs_must_match():
    conn = execution_connection(states=[execution_state(execution_START)], receivable=execution_receivable(sim_run_id="SIM-OTHER"))

    with pytest.raises(FinanceCollectionConflict, match="axes do not match"):
        apply_explicit_collection(conn, execution_event(execution_START, Decimal(4_000_000)))

    assert conn.receivable_updates == conn.finance_state_updates == 0


def test_finance_receivables_underflow_fails_before_either_update():
    conn = execution_connection(states=[execution_state(execution_START, receivables=Decimal(3_000_000))])

    with pytest.raises(FinanceCollectionConflict, match="cannot cover"):
        apply_explicit_collection(conn, execution_event(execution_START, Decimal(4_000_000)))

    assert conn.receivables[execution_RECEIVABLE_ID]["status"] == "OPEN"
    assert conn.receivable_updates == conn.finance_state_updates == 0
