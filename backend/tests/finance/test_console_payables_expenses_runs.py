"""Finance console read models: run isolation, windows and category fidelity.

★ These tests stand in for the database, but they do **not** stand in for the
  question the database answers.  Each one checks that the run axis actually
  reaches SQL as a bound parameter — a reader that forgot the `WHERE` would still
  pass a test that only inspects the rows a fake returned.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.console_expenses import display_category, get_console_expenses
from app.finance.console_payables import get_console_payables
from app.finance.console_runs import get_console_finance_latest_run, get_console_finance_runs

AS_OF = date(2026, 1, 9)
RUN_A = "SIM-CONSOLE-A"
RUN_B = "SIM-CONSOLE-B"


class _Capture:
    """Records what the reader asked the database, and answers per run."""

    def __init__(self, rows_by_run: dict[str, list[dict]]):
        self.rows_by_run = rows_by_run
        self.queries: list[tuple[str, list]] = []

    def __call__(self, query, params):
        self.queries.append((str(query), list(params)))
        run = next((value for value in params if value in self.rows_by_run), None)
        return list(self.rows_by_run.get(run, []))


def _payable(payable_id: str, *, due: date, outstanding: str, status: str = "OPEN") -> dict:
    return {
        "payable_id": payable_id,
        "purchase_id": "PUR-1",
        "issued_date": date(2026, 1, 1),
        "due_date": due,
        "original_amount_krw": Decimal(1000),
        "paid_amount_krw": Decimal(0),
        "outstanding_amount_krw": Decimal(outstanding),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Payables
# ---------------------------------------------------------------------------


def test_payables_never_mix_runs(monkeypatch):
    captured: list[list] = []

    def loader(*, sim_run_id, as_of):
        captured.append([sim_run_id, as_of])
        return [_payable(f"PAY-{sim_run_id[-1]}", due=AS_OF, outstanding="10")]

    monkeypatch.setattr("app.finance.console_payables.load_payables", loader)
    a = get_console_payables(sim_run_id=RUN_A, as_of=AS_OF)
    b = get_console_payables(sim_run_id=RUN_B, as_of=AS_OF)

    assert a.rows[0].payable_id == "PAY-A"
    assert b.rows[0].payable_id == "PAY-B"
    # 🔴 The run must reach the loader; a shared default would pass the line above.
    assert captured == [[RUN_A, AS_OF], [RUN_B, AS_OF]]


def test_payable_windows_are_counted_separately(monkeypatch):
    monkeypatch.setattr(
        "app.finance.console_payables.load_payables",
        lambda **_: [
            _payable("OVERDUE", due=date(2026, 1, 5), outstanding="100"),
            _payable("TODAY", due=AS_OF, outstanding="200"),
            _payable("IN-7", due=date(2026, 1, 16), outstanding="300"),
            _payable("LATER", due=date(2026, 2, 9), outstanding="400"),
        ],
    )
    summary = get_console_payables(sim_run_id=RUN_A, as_of=AS_OF).summary

    assert summary.overdue_krw == Decimal(100)
    assert summary.due_today_krw == Decimal(200)
    # Due-today is inside the 7-day window; the window does not exclude its first day.
    assert summary.due_next_7d_krw == Decimal(500)
    assert summary.total_outstanding_krw == Decimal(1000)


def test_settled_payables_stay_readable_but_stop_counting(monkeypatch):
    monkeypatch.setattr(
        "app.finance.console_payables.load_payables",
        lambda **_: [
            _payable("DONE", due=AS_OF, outstanding="0", status="SETTLED"),
            _payable("OPEN", due=AS_OF, outstanding="50"),
        ],
    )
    response = get_console_payables(sim_run_id=RUN_A, as_of=AS_OF)

    assert [row.payable_id for row in response.rows] == ["DONE", "OPEN"]
    assert response.summary.total_outstanding_krw == Decimal(50)


def test_a_filter_narrows_rows_without_moving_the_summary(monkeypatch):
    monkeypatch.setattr(
        "app.finance.console_payables.load_payables",
        lambda **_: [
            _payable("OVERDUE", due=date(2026, 1, 5), outstanding="100"),
            _payable("LATER", due=date(2026, 2, 9), outstanding="400"),
        ],
    )
    response = get_console_payables(sim_run_id=RUN_A, as_of=AS_OF, due_within_days=7)

    assert [row.payable_id for row in response.rows] == ["OVERDUE"]
    # 🔴 What is owed does not change because the screen asked a narrower question.
    assert response.summary.total_outstanding_krw == Decimal(500)


def test_a_null_outstanding_amount_is_refused(monkeypatch):
    monkeypatch.setattr(
        "app.finance.console_payables.load_payables",
        lambda **_: [{**_payable("X", due=AS_OF, outstanding="0"), "outstanding_amount_krw": None}],
    )
    with pytest.raises(ValueError):
        get_console_payables(sim_run_id=RUN_A, as_of=AS_OF)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


def _expense(expense_id: str, *, category: str, amount: str, day: date = AS_OF) -> dict:
    return {
        "expense_id": expense_id,
        "expense_date": day,
        "expense_category": category,
        "amount_krw": Decimal(amount),
        "evidence_id": "EV-1",
        "note": None,
    }


def test_expenses_never_mix_runs(monkeypatch):
    capture = _Capture(
        {
            RUN_A: [_expense("EXP-A", category="LABOR", amount="10")],
            RUN_B: [_expense("EXP-B", category="RENT", amount="20")],
        }
    )
    monkeypatch.setattr("app.finance.console_expenses.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_expenses.get_db_schema", lambda: "haetdeul")

    a = get_console_expenses(sim_run_id=RUN_A, as_of=AS_OF)
    b = get_console_expenses(sim_run_id=RUN_B, as_of=AS_OF)

    assert a.rows[0].expense_id == "EXP-A"
    assert b.rows[0].expense_id == "EXP-B"
    # 🔴 The axis is in the statement, not only in the fake's bookkeeping.
    assert "sim_run_id = %s" in capture.queries[0][0]
    assert capture.queries[0][1][0] == RUN_A


def test_category_totals_group_by_the_stored_name(monkeypatch):
    capture = _Capture(
        {
            RUN_A: [
                _expense("E1", category="LABOR", amount="100"),
                _expense("E2", category="LABOR", amount="50"),
                _expense("E3", category="RENT", amount="70"),
            ]
        }
    )
    monkeypatch.setattr("app.finance.console_expenses.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_expenses.get_db_schema", lambda: "haetdeul")

    summary = get_console_expenses(sim_run_id=RUN_A, as_of=AS_OF).summary

    assert summary.total_expenses_krw == Decimal(220)
    assert [
        (t.raw_category, t.total_amount_krw, t.expense_count) for t in summary.category_totals
    ] == [
        ("LABOR", Decimal(150), 2),
        ("RENT", Decimal(70), 1),
    ]


def test_an_unknown_category_keeps_its_own_name(monkeypatch):
    """🔴 A category the label table does not know is **not** folded into 기타."""
    capture = _Capture({RUN_A: [_expense("E1", category="NEW_THING", amount="5")]})
    monkeypatch.setattr("app.finance.console_expenses.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_expenses.get_db_schema", lambda: "haetdeul")

    row = get_console_expenses(sim_run_id=RUN_A, as_of=AS_OF).rows[0]

    assert row.raw_category == "NEW_THING"
    assert row.display_category == "NEW_THING"
    assert display_category("LABOR") == "인건비"


def test_the_date_filter_reaches_sql(monkeypatch):
    capture = _Capture({RUN_A: []})
    monkeypatch.setattr("app.finance.console_expenses.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_expenses.get_db_schema", lambda: "haetdeul")

    get_console_expenses(
        sim_run_id=RUN_A,
        as_of=AS_OF,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 8),
        category="LABOR",
    )
    statement, params = capture.queries[0]

    assert "expense_date >= %s" in statement
    assert "expense_category = %s" in statement
    assert params == [RUN_A, AS_OF, date(2026, 1, 1), date(2026, 1, 8), "LABOR"]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def _run_row(run_id: str, *, verdict: str | None = "PASS") -> dict:
    return {
        "run_id": run_id,
        "request_id": f"REQ-{run_id}",
        "as_of": AS_OF,
        "mode": "SALES_VALIDATION",
        "runtime_status": "READY",
        "business_status": "ok",
        "llm_status": "DISABLED",
        "response_payload": {
            "finance_verdict": verdict,
            "financial_summary": {"amount_match": True},
            "evidence_refs": ["EV-1"],
        },
        "created_at": date(2026, 1, 9),
    }


def test_finance_runs_are_scoped_to_the_requested_run(monkeypatch):
    capture = _Capture({RUN_A: [_run_row("FIN-A")], RUN_B: [_run_row("FIN-B")]})
    monkeypatch.setattr("app.finance.console_runs.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_runs.get_db_schema", lambda: "haetdeul")

    a = get_console_finance_runs(sim_run_id=RUN_A)
    b = get_console_finance_runs(sim_run_id=RUN_B)

    assert [row.run_id for row in a.rows] == ["FIN-A"]
    assert [row.run_id for row in b.rows] == ["FIN-B"]
    assert a.rows[0].sim_run_id == RUN_A
    # 🔴 The axis is an EXISTS against Master's stored binding, bound as a parameter.
    statement, params = capture.queries[0]
    assert "m.sim_run_id = %s" in statement
    assert params[0] == RUN_A


def test_latest_finance_run_never_falls_back_to_another_run(monkeypatch):
    capture = _Capture({RUN_A: [_run_row("FIN-A")]})
    monkeypatch.setattr("app.finance.console_runs.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_runs.get_db_schema", lambda: "haetdeul")

    assert get_console_finance_latest_run(sim_run_id=RUN_A).run_id == "FIN-A"
    # 🔴 A run with no history answers "none" — not somebody else's newest run.
    assert get_console_finance_latest_run(sim_run_id=RUN_B) is None


def test_latest_finance_run_is_ordered_and_capped(monkeypatch):
    capture = _Capture({RUN_A: [_run_row("FIN-A")]})
    monkeypatch.setattr("app.finance.console_runs.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_runs.get_db_schema", lambda: "haetdeul")

    get_console_finance_latest_run(sim_run_id=RUN_A)
    statement, params = capture.queries[0]

    # 🔴 LIMIT without ORDER BY would return an arbitrary row every call.
    assert "ORDER BY f.created_at DESC, f.run_id DESC" in statement
    assert "LIMIT %s" in statement
    assert params[-1] == 1


def test_a_run_without_a_verdict_reports_null(monkeypatch):
    capture = _Capture({RUN_A: [_run_row("FIN-A", verdict=None)]})
    monkeypatch.setattr("app.finance.console_runs.fetch_all", capture)
    monkeypatch.setattr("app.finance.console_runs.get_db_schema", lambda: "haetdeul")

    row = get_console_finance_runs(sim_run_id=RUN_A).rows[0]

    assert row.verdict is None
    assert row.runtime_status == "READY"
    # Finance stores no per-run prose; the column stays null instead of borrowing one.
    assert row.interpretation is None
    assert row.evidence == ["EV-1"]
