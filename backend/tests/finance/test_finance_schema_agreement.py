"""Finance daily-state invariant must agree in fresh and migration DDL."""

from __future__ import annotations

import re
from pathlib import Path

import app.finance

_REPO = Path(app.finance.__file__).parent.parent.parent.parent
_FRESH_DDL = _REPO / "database" / "10_domain_schema.sql"
_MIGRATION_DDL = _REPO / "database" / "finance" / "finance_state_daily_unique.sql"
_AXIS = ("sim_run_id", "financing_mode", "state_date")


def _columns(sql: str, pattern: str, *, source: Path) -> tuple[str, ...]:
    found = re.findall(pattern, sql, re.IGNORECASE | re.DOTALL)
    assert len(found) == 1, (
        f"{source.name} must define uq_finance_states_axis_date exactly once; "
        f"found {len(found)} definitions"
    )
    return tuple(column.strip().lower() for column in found[0].split(","))


def test_fresh_and_migration_ddl_enforce_the_same_daily_state_axis():
    fresh_sql = _FRESH_DDL.read_text(encoding="utf-8")
    migration_sql = _MIGRATION_DDL.read_text(encoding="utf-8")

    fresh_axis = _columns(
        fresh_sql,
        r"ADD\s+CONSTRAINT\s+uq_finance_states_axis_date\s+UNIQUE\s*\(([^)]+)\)",
        source=_FRESH_DDL,
    )
    migration_axis = _columns(
        migration_sql,
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+"
        r"uq_finance_states_axis_date\s+ON\s+haetdeul\.finance_states\s*\(([^)]+)\)",
        source=_MIGRATION_DDL,
    )

    assert fresh_axis == _AXIS
    assert migration_axis == _AXIS
    assert fresh_axis == migration_axis


def test_migration_preflights_the_same_axis_without_repairing_data():
    migration_sql = _MIGRATION_DDL.read_text(encoding="utf-8")
    group_axis = _columns(
        migration_sql,
        r"GROUP\s+BY\s+([^\n]+)\s+HAVING\s+COUNT\(\*\)\s*>\s*1",
        source=_MIGRATION_DDL,
    )

    assert group_axis == _AXIS
    assert not re.search(r"\b(?:DELETE|UPDATE|TRUNCATE)\b", migration_sql, re.IGNORECASE)
