"""Finance State 조회 Repository."""

from datetime import date
from decimal import Decimal
from typing import TypedDict, cast

from psycopg import sql

from app.finance.db import fetch_one, get_db_schema
from app.finance.schemas import FinanceSnapshot


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
