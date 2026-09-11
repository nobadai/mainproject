from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.finance.aging import classify_receivable_aging
from app.finance.console_receivables import get_console_receivables

AS_OF = date(2026, 9, 11)


@pytest.mark.parametrize(
    "days,bucket",
    [(0, "CURRENT"), (1, "1_7"), (7, "1_7"), (8, "8_30"), (30, "8_30"), (31, "30_PLUS")],
)
def test_aging_boundaries(days, bucket):
    got, overdue = classify_receivable_aging(
        outstanding_amount_krw=Decimal(1), due_date=AS_OF - timedelta(days=days), as_of=AS_OF
    )
    assert got == bucket
    assert overdue == (0 if days == 0 else days)


def test_paid_is_not_missing_and_none_is_rejected():
    assert classify_receivable_aging(
        outstanding_amount_krw=Decimal(0), due_date=AS_OF, as_of=AS_OF
    ) == ("PAID", None)
    with pytest.raises(ValueError):
        classify_receivable_aging(outstanding_amount_krw=None, due_date=AS_OF, as_of=AS_OF)


def test_console_receivables_never_mix_runs(monkeypatch):
    def rows(_query, params):
        return [
            {
                "receivable_id": "AR-A" if params[0] == "SIM-CONSOLE-A" else "AR-B",
                "sale_id": "S",
                "partner_id": "P",
                "partner_name": "P",
                "original_amount_krw": Decimal(10),
                "received_amount_krw": Decimal(0),
                "outstanding_amount_krw": Decimal("10" if params[0] == "SIM-CONSOLE-A" else "20"),
                "due_date": AS_OF,
                "status": "OPEN",
            }
        ]

    monkeypatch.setattr("app.finance.console_receivables.fetch_all", rows)
    monkeypatch.setattr("app.finance.console_receivables.get_db_schema", lambda: "haetdeul")
    a = get_console_receivables(sim_run_id="SIM-CONSOLE-A", as_of=AS_OF)
    b = get_console_receivables(sim_run_id="SIM-CONSOLE-B", as_of=AS_OF)
    assert a.rows[0].receivable_id == "AR-A" and a.summary.total_outstanding_krw == Decimal(10)
    assert b.rows[0].receivable_id == "AR-B" and b.summary.total_outstanding_krw == Decimal(20)
