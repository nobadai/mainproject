"""Finance side of Master's structural DayOpening contract."""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.day_open import DAY_OPEN_STATE_TYPE, FinanceDayOpening
from app.finance.db import FinanceDataNotReady

CARRY_FROM = date(2026, 1, 5)
AS_OF = date(2026, 1, 6)
SIM_RUN_ID = "SIM-BURNIN-202512"
MODE = "LOAN_BASELINE"

SOURCE = {
    "finance_state_id": "FIN-PROOF-20260105-LOAN",
    "sim_run_id": SIM_RUN_ID,
    "state_date": CARRY_FROM,
    "state_type": "TRANSITION_PROOF_T0",
    "financing_mode": MODE,
    "current_cash_krw": Decimal(32000000),
    "minimum_operating_cash_krw": Decimal(15902640),
    "committed_outflows_krw": Decimal(150000),
    "unsettled_purchase_payables_krw": Decimal(4500000),
    "receivables_krw": Decimal("73051531.25"),
    "inventory_book_value_krw": Decimal(3100000),
    "operational_inventory_value_krw": Decimal(2900000),
    "current_debt_krw": Decimal("45272104.184486"),
    "recommended_loan_amount_krw": Decimal(12000000),
    "note": "proof fixture",
}


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None)
        self.conn.executed.append((text, params))
        self.rows = []
        self.rowcount = 0
        if "SELECT DISTINCT sim_run_id" in text:
            self.rows = list(self.conn.axes)
        elif text.lstrip().startswith("SELECT finance_state_id"):
            self.rows = [
                (row["finance_state_id"],)
                for row in self.conn.states
                if row["sim_run_id"] == params["sim_run_id"]
                and row["financing_mode"] == params["financing_mode"]
                and row["state_date"] == params["state_date"]
            ][:2]
        elif "INSERT INTO" in text:
            source = next(
                (
                    row
                    for row in self.conn.states
                    if row["sim_run_id"] == params["sim_run_id"]
                    and row["financing_mode"] == params["financing_mode"]
                    and row["state_date"] == params["carry_from"]
                ),
                None,
            )
            if source is not None and not any(
                row["finance_state_id"] == params["finance_state_id"] for row in self.conn.states
            ):
                carried = deepcopy(source)
                carried.update(
                    finance_state_id=params["finance_state_id"],
                    state_date=params["as_of"],
                    state_type=params["state_type"],
                    note=params["note"],
                )
                self.conn.states.append(carried)
                self.rowcount = 1

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, states=(), axes=((SIM_RUN_ID, MODE),)):
        self.states = [deepcopy(row) for row in states]
        self.axes = list(axes)
        self.executed = []
        self.calls = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


@pytest.fixture(autouse=True)
def _schema():
    with patch("app.finance.day_open.get_db_schema", return_value="haetdeul"):
        yield


def test_is_open_requires_the_exact_requested_date():
    conn = _Conn([SOURCE])
    opening = FinanceDayOpening()

    assert opening.is_open(conn, as_of=CARRY_FROM) is True
    assert opening.is_open(conn, as_of=AS_OF) is False


def test_open_day_requires_the_exact_carry_from_state():
    conn = _Conn([dict(SOURCE, state_date=CARRY_FROM.replace(day=4))])

    with pytest.raises(FinanceDataNotReady) as raised:
        FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    assert raised.value.key == "historical_finance_position"
    assert len(conn.states) == 1


def test_first_open_carries_state_and_second_open_is_idempotent():
    conn = _Conn([SOURCE])
    opening = FinanceDayOpening()

    opening.open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)
    opening.open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    opened = [row for row in conn.states if row["state_date"] == AS_OF]
    assert len(opened) == 1
    row = opened[0]
    assert row["finance_state_id"] == f"FIN-DAY-{SIM_RUN_ID}-{MODE}-20260106"
    assert row["state_type"] == DAY_OPEN_STATE_TYPE
    for field in (
        "sim_run_id",
        "financing_mode",
        "current_cash_krw",
        "minimum_operating_cash_krw",
        "committed_outflows_krw",
        "unsettled_purchase_payables_krw",
        "receivables_krw",
        "inventory_book_value_krw",
        "operational_inventory_value_krw",
        "current_debt_krw",
        "recommended_loan_amount_krw",
    ):
        assert row[field] == SOURCE[field]
    assert opening.is_open(conn, as_of=AS_OF) is True
    assert conn.calls == []


def test_transition_created_target_means_open_day_does_not_create_another_state():
    transition_state = dict(
        SOURCE,
        # 과거 E2E가 만든 approval 기반 ID여도 exact axis/date가 이미 열렸다는 의미다.
        finance_state_id="FIN-H1-REQ-EXISTING-1",
        state_date=AS_OF,
        state_type="H1_COMMITMENT",
        unsettled_purchase_payables_krw=Decimal(5_000_000),
    )
    conn = _Conn([SOURCE, transition_state])

    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    assert len([row for row in conn.states if row["state_date"] == AS_OF]) == 1
    assert not any("INSERT INTO" in query for query, _params in conn.executed)


def test_weekend_calendar_date_can_be_opened():
    friday = date(2026, 1, 2)
    saturday = date(2026, 1, 3)
    conn = _Conn([dict(SOURCE, state_date=friday)])

    FinanceDayOpening().open_day(conn, as_of=saturday, carry_from=friday)

    assert any(row["state_date"] == saturday for row in conn.states)


def test_insert_omits_generated_financial_limit_and_uses_exact_source_date():
    conn = _Conn([SOURCE])

    FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    query, params = next(item for item in conn.executed if "INSERT INTO" in item[0])
    insert_columns = query.split(")", 1)[0]
    assert "financial_limit_krw" not in insert_columns
    assert "base.state_date = %(carry_from)s" in query
    assert params["carry_from"] == CARRY_FROM


def test_open_day_uses_supplied_connection_and_opens_none_of_its_own():
    conn = _Conn([SOURCE])

    with patch("app.finance.db.get_connection") as opened:
        FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    opened.assert_not_called()
    assert conn.calls == []


def test_ambiguous_runtime_axis_fails_closed():
    conn = _Conn([SOURCE], axes=((SIM_RUN_ID, MODE), ("SIM-OTHER", "BASE_NO_LOAN")))

    with pytest.raises(FinanceDataNotReady) as raised:
        FinanceDayOpening().is_open(conn, as_of=CARRY_FROM)

    assert raised.value.key == "finance_runtime_axis_ambiguous"


def test_duplicate_exact_source_states_fail_closed():
    duplicate = dict(SOURCE, finance_state_id="FIN-DUPLICATE")
    conn = _Conn([SOURCE, duplicate])

    with pytest.raises(FinanceDataNotReady) as raised:
        FinanceDayOpening().open_day(conn, as_of=AS_OF, carry_from=CARRY_FROM)

    assert raised.value.key == "finance_state_ambiguous"
    assert len(conn.states) == 2


def test_implementation_matches_master_structural_protocol_shape():
    import app.finance.day_open as module
    from app.master.day_open import DayOpening

    implementation = FinanceDayOpening()
    for method in ("is_open", "open_day"):
        expected = inspect.signature(getattr(DayOpening, method)).parameters
        actual = inspect.signature(getattr(implementation, method)).parameters
        assert set(expected) - {"self"} == set(actual)
    assert "from app.master" not in inspect.getsource(module)
