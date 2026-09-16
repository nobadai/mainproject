"""사용자 기록 자금 입·출금 — Finance State를 덮어쓰지 않는 불변 원장."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from psycopg import sql

from app.finance.db import FinanceDataNotReady, get_db_schema

CashDirection = Literal["INFLOW", "OUTFLOW"]
CashCategory = Literal["OWNER_INJECTION", "OWNER_WITHDRAWAL", "OTHER"]


class CashAdjustmentConflict(ValueError):
    """자금 조정이 현재 Finance State 불변식을 위반했다."""


@dataclass(frozen=True)
class CashAdjustmentResult:
    cash_adjustment_id: str
    current_cash_krw: Decimal


def record_cash_adjustment(
    conn,
    *,
    sim_run_id: str,
    financing_mode: str,
    adjustment_date: date,
    direction: CashDirection,
    category: CashCategory,
    amount_krw: Decimal,
    source_ref: str,
    note: str | None,
    recorded_by: str,
) -> CashAdjustmentResult:
    """한 기준일 state에 실제 자금 입·출금을 원자적으로 반영한다."""
    if amount_krw <= 0:
        raise CashAdjustmentConflict("자금 조정 금액은 0원보다 커야 합니다.")
    delta = amount_krw if direction == "INFLOW" else -amount_krw
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT finance_state_id, current_cash_krw
                FROM {}.finance_states
                WHERE sim_run_id = %s AND financing_mode = %s AND state_date = %s
                FOR UPDATE
                """
            ).format(schema),
            [sim_run_id, financing_mode, adjustment_date],
        )
        rows = cursor.fetchall()
        if not rows:
            raise FinanceDataNotReady("historical_finance_position")
        if len(rows) != 1:
            raise FinanceDataNotReady("finance_state_ambiguous")
        state = rows[0]
        next_cash = Decimal(str(state["current_cash_krw"])) + delta
        if next_cash < 0:
            raise CashAdjustmentConflict("출금 후 현금이 0원보다 작아질 수 없습니다.")
        adjustment_id = f"CASH-ADJ-{uuid4()}"
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.finance_cash_adjustments (
                    cash_adjustment_id, sim_run_id, financing_mode, adjustment_date,
                    direction, category, amount_krw, source_ref, note, recorded_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema),
            [
                adjustment_id,
                sim_run_id,
                financing_mode,
                adjustment_date,
                direction,
                category,
                amount_krw,
                source_ref,
                note,
                recorded_by,
            ],
        )
        cursor.execute(
            sql.SQL(
                "UPDATE {}.finance_states SET current_cash_krw = %s WHERE finance_state_id = %s"
            ).format(schema),
            [next_cash, state["finance_state_id"]],
        )
        if cursor.rowcount != 1:
            raise CashAdjustmentConflict("재무 상태를 갱신하지 못했습니다.")
    return CashAdjustmentResult(adjustment_id, next_cash)
