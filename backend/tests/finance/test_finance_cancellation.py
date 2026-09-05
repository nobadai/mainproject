"""Payable cancellation is an audited, retry-safe Finance reversal."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.cancellation import (
    FinanceCancellationAdapter,
    FinanceCancellationConflict,
    cancel_finance_payables,
)
from app.finance.db import FinanceDataNotReady, _fetch_open_payable_events

AS_OF = date(2026, 1, 5)
TARGET = date(2026, 1, 6)


def _payable(
    purchase_id: str,
    amount: str,
    *,
    status: str = "OPEN",
    paid: str = "0",
    cancelled: str = "0",
    outstanding: str | None = None,
) -> dict[str, object]:
    return {
        "purchase_id": purchase_id,
        "sim_run_id": "SIM-1",
        "original_amount_krw": Decimal(amount),
        "paid_amount_krw": Decimal(paid),
        "cancelled_amount_krw": Decimal(cancelled),
        "outstanding_amount_krw": Decimal(amount if outstanding is None else outstanding),
        "status": status,
        "due_date": AS_OF,
        "cancelled_date": None,
        "settled_date": None,
    }


def _state(
    state_id: str,
    state_date: date,
    *,
    unsettled: str = "500",
) -> dict[str, object]:
    return {
        "finance_state_id": state_id,
        "sim_run_id": "SIM-1",
        "state_date": state_date,
        "state_type": "DAY",
        "financing_mode": "NONE",
        "current_cash_krw": Decimal(1000),
        "minimum_operating_cash_krw": Decimal(100),
        "committed_outflows_krw": Decimal(20),
        "unsettled_purchase_payables_krw": Decimal(unsettled),
        "receivables_krw": Decimal(300),
        "inventory_book_value_krw": Decimal(400),
        "operational_inventory_value_krw": Decimal(350),
        "current_debt_krw": Decimal(50),
        "recommended_loan_amount_krw": Decimal(10),
        "note": "before",
    }


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rows: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        params = params or {}
        self.conn.executed.append((text, deepcopy(params)))

        if "SELECT" in text and ".payables" in text and "FOR UPDATE" in text:
            requested = set(params["purchase_ids"])
            self.rows = [
                deepcopy(row)
                for purchase_id, row in sorted(self.conn.payables.items())
                if purchase_id in requested
            ]
            return

        if "SELECT finance_state_id" in text and ".finance_states" in text:
            self.rows = [
                {
                    "finance_state_id": row["finance_state_id"],
                    "financing_mode": row["financing_mode"],
                    "unsettled_purchase_payables_krw": row["unsettled_purchase_payables_krw"],
                }
                for row in self.conn.states.values()
                if row["sim_run_id"] == params["sim_run_id"]
                and row["state_date"] == params["state_date"]
            ]
            return

        if "UPDATE" in text and ".payables" in text:
            assert "cancelled_amount_krw = outstanding_amount_krw" in text
            assert "outstanding_amount_krw = 0" in text
            assert "status = 'CANCELLED'" in text
            assert "AND status = 'OPEN'" in text
            assert "AND paid_amount_krw = 0" in text
            assert "RETURNING purchase_id, cancelled_amount_krw" in text
            self.rows = []
            for purchase_id in params["purchase_ids"]:
                row = self.conn.payables[purchase_id]
                if row["status"] != "OPEN" or row["paid_amount_krw"] != 0:
                    continue
                amount = row["outstanding_amount_krw"]
                row["cancelled_amount_krw"] = amount
                row["outstanding_amount_krw"] = Decimal(0)
                row["status"] = "CANCELLED"
                row["cancelled_date"] = params["cancelled_date"]
                self.rows.append(
                    {
                        "purchase_id": purchase_id,
                        "cancelled_amount_krw": amount,
                    }
                )
            return

        if "INSERT INTO" in text and ".finance_states" in text:
            assert text.count("unsettled_purchase_payables_krw - %(cancelled_amount)s") == 2
            assert "ON CONFLICT (sim_run_id, financing_mode, state_date)" in text
            source = self.conn.states[params["source_finance_state_id"]]
            amount = params["cancelled_amount"]
            matching = next(
                (
                    row
                    for row in self.conn.states.values()
                    if row["sim_run_id"] == source["sim_run_id"]
                    and row["financing_mode"] == source["financing_mode"]
                    and row["state_date"] == params["target_state_date"]
                ),
                None,
            )
            base = matching or source
            if base["unsettled_purchase_payables_krw"] < amount:
                self.rows = []
                return
            if matching is None:
                matching = deepcopy(source)
                matching.update(
                    finance_state_id=params["finance_state_id"],
                    state_date=params["target_state_date"],
                    state_type=params["state_type"],
                    note=params["note"],
                )
                self.conn.states[matching["finance_state_id"]] = matching
            matching["unsettled_purchase_payables_krw"] -= amount
            self.rows = [{"finance_state_id": matching["finance_state_id"]}]
            return

        if "UPDATE" in text and ".finance_states" in text:
            assert "unsettled_purchase_payables_krw - %(cancelled_amount)s" in text
            row = self.conn.states[params["finance_state_id"]]
            amount = params["cancelled_amount"]
            if row["unsettled_purchase_payables_krw"] < amount:
                self.rows = []
                return
            row["unsettled_purchase_payables_krw"] -= amount
            self.rows = [{"finance_state_id": row["finance_state_id"]}]
            return

        raise AssertionError(f"unexpected SQL: {text}")

    def fetchall(self):
        return deepcopy(self.rows)


class _Conn:
    def __init__(self, *, payables, states) -> None:
        self.payables = {row["purchase_id"]: deepcopy(row) for row in payables}
        self.states = {row["finance_state_id"]: deepcopy(row) for row in states}
        self.executed: list[tuple[str, object]] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commit_calls += 1
        raise AssertionError("Finance cancellation must not commit")

    def rollback(self):
        self.rollback_calls += 1
        raise AssertionError("Finance cancellation must not roll back")

    def close(self):
        self.close_calls += 1
        raise AssertionError("Finance cancellation must not close the supplied connection")


def _existing_target_conn(*payables, unsettled="500") -> _Conn:
    return _Conn(
        payables=payables,
        states=[
            _state("FIN-SOURCE", AS_OF, unsettled=unsettled),
            _state("FIN-TARGET", TARGET, unsettled=unsettled),
        ],
    )


def test_open_payable_is_cancelled_without_becoming_paid_or_deleted():
    conn = _existing_target_conn(_payable("PUR-A", "300"))

    result = FinanceCancellationAdapter().cancel(
        conn,
        purchase_ids=["PUR-A"],
        as_of=AS_OF,
        target_state_date=TARGET,
    )

    row = conn.payables["PUR-A"]
    assert row["status"] == "CANCELLED"
    assert row["original_amount_krw"] == Decimal(300)
    assert row["paid_amount_krw"] == 0
    assert row["cancelled_amount_krw"] == Decimal(300)
    assert row["outstanding_amount_krw"] == 0
    assert row["cancelled_date"] == AS_OF
    assert row["settled_date"] is None
    assert result.newly_cancelled_amount_krw == Decimal(300)
    assert not any("DELETE" in query.upper() for query, _ in conn.executed)


def test_existing_target_state_reverses_only_newly_cancelled_amount_and_keeps_cash():
    conn = _existing_target_conn(_payable("PUR-A", "300"))
    before = deepcopy(conn.states["FIN-TARGET"])

    result = cancel_finance_payables(
        conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET
    )

    after = conn.states["FIN-TARGET"]
    assert result.finance_state_id == "FIN-TARGET"
    assert after["unsettled_purchase_payables_krw"] == Decimal(200)
    for field in (
        "current_cash_krw",
        "minimum_operating_cash_krw",
        "committed_outflows_krw",
        "receivables_krw",
        "inventory_book_value_krw",
        "operational_inventory_value_krw",
        "current_debt_krw",
        "recommended_loan_amount_krw",
    ):
        assert after[field] == before[field]


def test_retry_is_a_noop_for_payable_and_state():
    conn = _existing_target_conn(_payable("PUR-A", "300"))
    first = cancel_finance_payables(
        conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET
    )
    after_first = deepcopy(conn.states["FIN-TARGET"])

    second = cancel_finance_payables(
        conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET
    )

    assert first.newly_cancelled_count == 1
    assert second.newly_cancelled_count == 0
    assert second.newly_cancelled_amount_krw == 0
    assert second.finance_state_updated is False
    assert conn.states["FIN-TARGET"] == after_first


def test_two_legs_cancel_independently_and_leave_other_approval_open():
    conn = _existing_target_conn(
        _payable("PUR-A-S1", "100"),
        _payable("PUR-A-S2", "200"),
        _payable("PUR-B-S1", "50"),
        unsettled="350",
    )

    result = cancel_finance_payables(
        conn,
        purchase_ids=["PUR-A-S1", "PUR-A-S2"],
        as_of=AS_OF,
        target_state_date=TARGET,
    )

    assert result.newly_cancelled_count == 2
    assert result.newly_cancelled_amount_krw == Decimal(300)
    assert conn.payables["PUR-A-S1"]["status"] == "CANCELLED"
    assert conn.payables["PUR-A-S2"]["status"] == "CANCELLED"
    assert conn.payables["PUR-A-S1"]["due_date"] == conn.payables["PUR-A-S2"]["due_date"]
    assert conn.payables["PUR-B-S1"]["status"] == "OPEN"
    assert conn.states["FIN-TARGET"]["unsettled_purchase_payables_krw"] == Decimal(50)


def test_mixed_open_and_cancelled_set_fails_without_stable_cancellation_identity():
    cancelled = _payable("PUR-A-S1", "100", status="CANCELLED", cancelled="100", outstanding="0")
    cancelled["cancelled_date"] = AS_OF
    conn = _existing_target_conn(cancelled, _payable("PUR-A-S2", "200"), unsettled="200")

    with pytest.raises(FinanceCancellationConflict) as raised:
        cancel_finance_payables(
            conn,
            purchase_ids=["PUR-A-S1", "PUR-A-S2"],
            as_of=AS_OF,
            target_state_date=TARGET,
        )

    assert raised.value.reason == "payable_cancellation_state_mixed"
    assert conn.payables["PUR-A-S2"]["status"] == "OPEN"
    assert conn.states["FIN-TARGET"]["unsettled_purchase_payables_krw"] == 200


@pytest.mark.parametrize(
    ("status", "paid", "outstanding"),
    [("PARTIAL", "40", "60"), ("SETTLED", "100", "0"), ("WRITEOFF", "0", "100")],
)
def test_paid_or_written_off_payable_fails_closed(status, paid, outstanding):
    payable = _payable("PUR-A", "100", status=status, paid=paid, outstanding=outstanding)
    conn = _existing_target_conn(payable)

    with pytest.raises(FinanceCancellationConflict) as raised:
        cancel_finance_payables(conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET)

    assert raised.value.reason == "payable_not_cancellable"
    assert conn.payables["PUR-A"] == payable
    assert conn.states["FIN-TARGET"]["unsettled_purchase_payables_krw"] == 500


def test_missing_target_fails_before_any_partial_cancellation():
    payable = _payable("PUR-A", "100")
    conn = _existing_target_conn(payable)

    with pytest.raises(FinanceCancellationConflict) as raised:
        cancel_finance_payables(
            conn,
            purchase_ids=["PUR-A", "PUR-MISSING"],
            as_of=AS_OF,
            target_state_date=TARGET,
        )

    assert raised.value.reason == "payable_not_found"
    assert raised.value.purchase_ids == ("PUR-MISSING",)
    assert conn.payables["PUR-A"] == payable


def test_missing_target_state_carries_exact_source_and_subtracts():
    conn = _Conn(
        payables=[_payable("PUR-A", "300")],
        states=[_state("FIN-SOURCE", AS_OF, unsettled="500")],
    )

    result = cancel_finance_payables(
        conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET
    )

    target_id = "FIN-DAY-SIM-1-NONE-20260106"
    assert result.finance_state_id == target_id
    assert conn.states[target_id]["unsettled_purchase_payables_krw"] == Decimal(200)
    assert conn.states[target_id]["current_cash_krw"] == Decimal(1000)
    assert conn.states[target_id]["state_type"] == "H1_CANCELLATION"
    assert conn.states["FIN-SOURCE"]["unsettled_purchase_payables_krw"] == Decimal(500)


def test_missing_exact_source_state_fails_before_payable_change():
    payable = _payable("PUR-A", "100")
    conn = _Conn(payables=[payable], states=[])

    with pytest.raises(FinanceDataNotReady) as raised:
        cancel_finance_payables(conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET)

    assert raised.value.key == "historical_finance_position"
    assert conn.payables["PUR-A"] == payable


def test_negative_unsettled_fails_before_payable_change():
    payable = _payable("PUR-A", "300")
    conn = _existing_target_conn(payable, unsettled="200")

    with pytest.raises(FinanceCancellationConflict) as raised:
        cancel_finance_payables(conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=TARGET)

    assert raised.value.reason == "finance_unsettled_underflow"
    assert conn.payables["PUR-A"] == payable
    assert conn.states["FIN-TARGET"]["unsettled_purchase_payables_krw"] == 200


def test_weekend_target_is_accepted_and_caller_connection_is_not_managed():
    saturday = date(2026, 1, 10)
    conn = _Conn(
        payables=[_payable("PUR-A", "100")],
        states=[_state("FIN-SOURCE", AS_OF, unsettled="100")],
    )

    result = cancel_finance_payables(
        conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=saturday
    )

    assert result.finance_state_id == "FIN-DAY-SIM-1-NONE-20260110"
    assert conn.commit_calls == conn.rollback_calls == conn.close_calls == 0


def test_cancelled_payables_are_excluded_from_projection_by_status_contract():
    captured = {}

    def fake_fetch(query, params):
        captured["query"] = str(query)
        captured["params"] = params
        return []

    with patch("app.finance.db.fetch_all", side_effect=fake_fetch):
        rows, events = _fetch_open_payable_events(
            sim_run_id="SIM-1", as_of=AS_OF, horizon_end=TARGET
        )

    assert rows == events == []
    assert "status = 'OPEN'" in captured["query"]
    assert "CANCELLED" not in captured["query"]


def test_invalid_target_date_and_empty_purchase_ids_fail_before_sql():
    conn = _existing_target_conn(_payable("PUR-A", "100"))

    with pytest.raises(ValueError, match="must not be empty"):
        cancel_finance_payables(conn, purchase_ids=[], as_of=AS_OF, target_state_date=TARGET)
    with pytest.raises(ValueError, match="must be after"):
        cancel_finance_payables(conn, purchase_ids=["PUR-A"], as_of=AS_OF, target_state_date=AS_OF)

    assert conn.executed == []
