"""Finance State 조회 Repository."""

from datetime import date
from decimal import Decimal
from typing import TypedDict, cast

from psycopg import sql

from app.finance.db import fetch_all, fetch_one, get_db_schema
from app.finance.schemas import FinancePolicy, FinanceSnapshot

FINANCE_POLICY_VERSION = "v1.3-PROVISIONAL"
FINANCE_POLICY_USAGE_SCOPE = "AGENT_MVP_DEMO"
_NUMERIC_POLICY_KEYS = {
    "purchase_payment_days",
    "payroll_date",
    "monthly_labor_cost_krw",
    "minimum_cash_balance_krw",
    "cashflow_projection_days",
    "cash_priority_high_ratio",
    "cash_priority_medium_ratio",
}
_TEXT_POLICY_KEYS = {"cash_priority_reference"}
_REQUIRED_POLICY_KEYS = _NUMERIC_POLICY_KEYS | _TEXT_POLICY_KEYS


class FinanceState(TypedDict):
    finance_state_id: str
    sim_run_id: str
    state_date: date
    state_type: str
    financing_mode: str
    current_cash_krw: Decimal
    minimum_operating_cash_krw: Decimal
    committed_outflows_krw: Decimal
    unsettled_purchase_payables_krw: Decimal
    financial_limit_krw: Decimal


def get_current_finance_state() -> FinanceState:
    """DB View가 지정한 현재 Finance State 한 건을 조회한다."""
    return cast(FinanceState, _get_current_finance_state_row())


def get_current_finance_snapshot() -> FinanceSnapshot:
    """Current View의 Finance State를 T0 ID 미확정 Snapshot으로 변환한다."""
    return FinanceSnapshot(snapshot_id=None, **_get_current_finance_state_row())


def get_active_finance_policy() -> FinancePolicy:
    """현재 Finance MVP 범위의 active policy를 typed contract로 조회한다."""
    query = sql.SQL(
        """
        SELECT
            policy_key,
            value_kind,
            value_numeric,
            value_text,
            value_json,
            source_ref,
            policy_version,
            usage_scope
        FROM {}.agent_policy_config
        WHERE domain = %s
          AND policy_version = %s
          AND usage_scope = %s
          AND is_active = TRUE
        """
    ).format(sql.Identifier(get_db_schema()))
    rows = fetch_all(
        query,
        ["finance", FINANCE_POLICY_VERSION, FINANCE_POLICY_USAGE_SCOPE],
    )
    return _build_finance_policy(rows)


def _build_finance_policy(rows: list[dict[str, object]]) -> FinancePolicy:
    values: dict[str, object] = {}
    source_refs: dict[str, str] = {}

    for row in rows:
        key = row.get("policy_key")
        if key not in _REQUIRED_POLICY_KEYS:
            continue
        if key in values:
            raise ValueError(f"Duplicate Finance policy key: {key}")
        if row.get("policy_version") != FINANCE_POLICY_VERSION:
            raise ValueError(f"Finance policy_version mismatch: {key}")
        if row.get("usage_scope") != FINANCE_POLICY_USAGE_SCOPE:
            raise ValueError(f"Finance policy usage_scope mismatch: {key}")

        kind = row.get("value_kind")
        expected_kind = "NUMERIC" if key in _NUMERIC_POLICY_KEYS else "TEXT"
        if kind != expected_kind:
            raise ValueError(f"Invalid value_kind for Finance policy {key}: {kind}")
        selected_column = "value_numeric" if kind == "NUMERIC" else "value_text"
        unused_columns = {"value_numeric", "value_text", "value_json"} - {selected_column}
        value = row.get(selected_column)
        if value is None or any(row.get(column) is not None for column in unused_columns):
            raise ValueError(f"Inconsistent value columns for Finance policy: {key}")
        if kind == "NUMERIC" and (isinstance(value, bool) or not isinstance(value, Decimal)):
            raise TypeError(f"Invalid Python NUMERIC value for Finance policy: {key}")
        if kind == "TEXT" and not isinstance(value, str):
            raise TypeError(f"Invalid Python TEXT value for Finance policy: {key}")

        source_ref = row.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError(f"Missing source_ref for Finance policy: {key}")
        values[key] = value
        source_refs[key] = source_ref

    missing = _REQUIRED_POLICY_KEYS - values.keys()
    if missing:
        raise LookupError(f"Required Finance policies were not found: {', '.join(sorted(missing))}")

    for key in ("purchase_payment_days", "payroll_date", "cashflow_projection_days"):
        numeric = values[key]
        assert isinstance(numeric, Decimal)
        if numeric != numeric.to_integral_value():
            raise ValueError(f"Finance policy must be an integer: {key}")
        values[key] = int(numeric)

    return FinancePolicy(
        **values,
        policy_version=FINANCE_POLICY_VERSION,
        usage_scope=FINANCE_POLICY_USAGE_SCOPE,
        source_refs=source_refs,
    )


def _get_current_finance_state_row() -> dict[str, object]:
    query = sql.SQL(
        """
        SELECT
            finance_state_id,
            sim_run_id,
            state_date,
            state_type,
            financing_mode,
            current_cash_krw,
            minimum_operating_cash_krw,
            committed_outflows_krw,
            unsettled_purchase_payables_krw,
            financial_limit_krw
        FROM {}.v_current_finance_state
        """
    ).format(sql.Identifier(get_db_schema()))
    row = fetch_one(query)
    if row is None:
        raise LookupError("Current Finance State was not found")
    return row
