"""One Finance runtime axis has exactly one state per calendar date."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from app.finance.db import FinanceDataNotReady, _fetch_scheduled_rows, load_finance_state_row
from app.finance.state_identity import daily_finance_state_id

_DB = "app.finance.db"


def test_daily_state_id_contains_the_full_axis_and_date():
    assert (
        daily_finance_state_id(
            sim_run_id="SIM-1",
            financing_mode="LOAN_BASELINE",
            state_date=date(2026, 1, 6),
        )
        == "FIN-DAY-SIM-1-LOAN_BASELINE-20260106"
    )


def test_loader_still_fails_closed_if_same_axis_date_is_ambiguous(finance_state):
    first = dict(finance_state, finance_state_id="FIN-A", state_date=date(2026, 1, 6))
    second = dict(finance_state, finance_state_id="FIN-B", state_date=date(2026, 1, 6))
    with (
        patch(
            f"{_DB}.get_finance_runtime_axis",
            return_value={"sim_run_id": "SIM-BURNIN-202512", "financing_mode": "LOAN_BASELINE"},
        ),
        patch(f"{_DB}.fetch_all", return_value=[first, second]),
        pytest.raises(FinanceDataNotReady) as raised,
    ):
        load_finance_state_row(date(2026, 1, 6))

    assert raised.value.key == "finance_state_ambiguous"


def test_daily_unique_migration_preflights_and_enforces_the_full_axis():
    migration = (
        Path(__file__).parents[3] / "database" / "finance" / "finance_state_daily_unique.sql"
    ).read_text(encoding="utf-8")

    assert "HAVING COUNT(*) > 1" in migration
    assert "RAISE EXCEPTION" in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_finance_states_axis_date" in migration
    assert "(sim_run_id, financing_mode, state_date)" in migration
    assert all(word not in migration.upper() for word in ("DELETE ", "TRUNCATE ", "DROP "))


@pytest.mark.parametrize("as_of", [date(2026, 1, 5), date(2026, 1, 6)])
def test_overdue_receivables_remain_excluded_from_future_projection(as_of):
    """지난 due_date는 두 날 모두 미래 event에서 빠질 뿐 원장을 변경하지 않는다."""
    with patch(f"{_DB}.fetch_all", return_value=[]) as fetched:
        rows = _fetch_scheduled_rows(
            table="receivables",
            columns=("receivable_id", "due_date", "outstanding_amount_krw"),
            sim_run_id="SIM-1",
            as_of=as_of,
            horizon_end=date(2026, 2, 5),
            status_column="status",
            active_status="OPEN",
        )

    assert rows == []
    query, params = fetched.call_args.args
    assert '"due_date" > %s' in query.as_string(None)
    assert params[1] == as_of
