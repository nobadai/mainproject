"""Sales 확정분을 Finance receivables 원장으로 멱등 저장하는 계약."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.finance.db import FinanceDataNotReady, decimal_value, get_db_schema, row_value
from app.finance.sales_validation import ReceivableCreateInput


class ReceivablePersistenceConflict(RuntimeError):
    """같은 sale_id 축으로 다른 사실이 들어왔다."""


@dataclass(frozen=True)
class ReceivableWritePlan:
    receivable_id: str
    sale_id: str
    sim_run_id: str
    financing_mode: str
    finance_state_id: str
    issued_date: date
    due_date: date
    original_amount_krw: Decimal
    received_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    status: str


@dataclass(frozen=True)
class ReceivableWriteResult:
    receivable_id: str
    finance_state_id: str
    receivables_written: int
    finance_state_updates: int


def confirm_receivable(conn: Any, request: ReceivableCreateInput) -> ReceivableWriteResult:
    """확정된 Sale 을 읽어 receivable 과 Finance State 를 멱등 저장한다."""

    sale_row = load_sale_row(conn, request.sale_id)
    finance_state_id = load_sale_date_finance_state_id(
        conn,
        sim_run_id=request.sim_run_id,
        financing_mode=request.financing_mode,
        state_date=request.sale_date,
    )
    plan = build_receivable_write_plan(
        request,
        sale_row=sale_row,
        finance_state_id=finance_state_id,
    )
    return persist_receivable(conn, plan)


def load_sale_row(conn: Any, sale_id: str) -> dict[str, Any]:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT
                    sale_id, sim_run_id, customer_partner_id, order_date, sale_date,
                    collection_due_date, total_quantity_kg, total_amount_krw,
                    contribution_profit_krw, collection_status, source_order_id, note,
                    order_status
                FROM {}.sales
                WHERE sale_id = %s
                LIMIT 2
                """
            ).format(schema),
            [sale_id],
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise ReceivablePersistenceConflict(f"sale was not found: {sale_id}")
    return dict(rows[0])


def build_receivable_write_plan(
    request: ReceivableCreateInput, *, sale_row: Mapping[str, Any], finance_state_id: str
) -> ReceivableWritePlan:
    """Sales header 1건을 Finance receivable 1건으로 옮기는 계획을 만든다."""

    sale_id = str(sale_row["sale_id"])
    if sale_id != request.sale_id:
        raise ReceivablePersistenceConflict("sale row does not match the requested sale_id")
    if str(sale_row["sim_run_id"]) != request.sim_run_id:
        raise ReceivablePersistenceConflict("sale row sim_run_id does not match the request")
    if row_value(sale_row, "customer_partner_id") != request.customer_partner_id:
        raise ReceivablePersistenceConflict(
            "sale row customer_partner_id does not match the request"
        )
    if row_value(sale_row, "sale_date") != request.sale_date:
        raise ReceivablePersistenceConflict("sale row sale_date does not match the request")
    if row_value(sale_row, "collection_due_date") != request.due_date:
        raise ReceivablePersistenceConflict("sale row due_date does not match the request")
    if decimal_value(row_value(sale_row, "total_amount_krw")) != request.original_amount_krw:
        raise ReceivablePersistenceConflict("sale row original amount does not match the request")

    due_date = request.due_date
    issued_date = request.sale_date
    if due_date is None or issued_date is None:
        raise ReceivablePersistenceConflict("sale row is missing receivable dates")
    return ReceivableWritePlan(
        receivable_id=receivable_id_for(sale_id),
        sale_id=sale_id,
        sim_run_id=request.sim_run_id,
        financing_mode=request.financing_mode,
        finance_state_id=finance_state_id,
        issued_date=issued_date,
        due_date=due_date,
        original_amount_krw=request.original_amount_krw,
        received_amount_krw=Decimal(0),
        outstanding_amount_krw=request.original_amount_krw,
        status="OPEN",
    )


def load_sale_date_finance_state_id(
    conn: Any, *, sim_run_id: str, financing_mode: str, state_date: date
) -> str:
    """기존 Finance state는 ID 조립이 아니라 실행 축으로 찾는다.

    ``daily_finance_state_id``는 새 일별 상태를 만들 때 쓰는 결정론 ID 규칙이다.
    이미 존재하는 상태 조회의 정본 키는 ``(sim_run_id, financing_mode, state_date)``이며,
    조회된 실제 ``finance_state_id``를 receivable lineage에 연결한다.
    """

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT finance_state_id
                FROM {}.finance_states
                WHERE sim_run_id = %s
                  AND financing_mode = %s
                  AND state_date = %s
                FOR UPDATE
                """
            ).format(schema),
            [sim_run_id, financing_mode, state_date],
        )
        rows = cursor.fetchall()
    if not rows:
        raise FinanceDataNotReady("finance_state_for_receivable")
    if len(rows) != 1:
        raise FinanceDataNotReady("finance_state_ambiguous")
    return str(row_value(rows[0], "finance_state_id", 0))


def persist_receivable(conn: Any, plan: ReceivableWritePlan) -> ReceivableWriteResult:
    """caller-owned connection으로 receivable과 Finance State AR을 멱등 저장한다."""

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT
                    finance_state_id, receivables_krw
                FROM {}.finance_states
                WHERE finance_state_id = %s
                LIMIT 2
                FOR UPDATE
                """
            ).format(schema),
            [plan.finance_state_id],
        )
        finance_rows = cursor.fetchall()
        if len(finance_rows) != 1:
            raise ReceivablePersistenceConflict(
                f"finance state was not found: {plan.finance_state_id}"
            )

        insert_count = _insert_receivable(cursor, schema, plan)
        update_count = 0
        if insert_count:
            update_count = _apply_finance_state_delta(
                cursor, schema, plan.finance_state_id, plan.original_amount_krw
            )
            if update_count != 1:
                raise ReceivablePersistenceConflict(
                    "finance state receivables could not be updated"
                )
        else:
            _assert_same_receivable(cursor, schema, plan)
    return ReceivableWriteResult(
        receivable_id=plan.receivable_id,
        finance_state_id=plan.finance_state_id,
        receivables_written=insert_count,
        finance_state_updates=update_count,
    )


def receivable_id_for(sale_id: str) -> str:
    return f"AR-{sale_id}"


def _insert_receivable(cursor: Any, schema: sql.Identifier, plan: ReceivableWritePlan) -> int:
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {}.receivables (
                receivable_id, sim_run_id, sale_id, issued_date, due_date,
                original_amount_krw, received_amount_krw, outstanding_amount_krw, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sale_id) DO NOTHING
            """
        ).format(schema),
        [
            plan.receivable_id,
            plan.sim_run_id,
            plan.sale_id,
            plan.issued_date,
            plan.due_date,
            plan.original_amount_krw,
            plan.received_amount_krw,
            plan.outstanding_amount_krw,
            plan.status,
        ],
    )
    return int(cursor.rowcount)


def _apply_finance_state_delta(
    cursor: Any, schema: sql.Identifier, finance_state_id: str, delta: Decimal
) -> int:
    cursor.execute(
        sql.SQL(
            """
            UPDATE {}.finance_states
            SET receivables_krw = receivables_krw + %s
            WHERE finance_state_id = %s
            """
        ).format(schema),
        [delta, finance_state_id],
    )
    return int(cursor.rowcount)


def _assert_same_receivable(cursor: Any, schema: sql.Identifier, plan: ReceivableWritePlan) -> None:
    cursor.execute(
        sql.SQL(
            """
            SELECT *
            FROM {}.receivables
            WHERE sale_id = %s
            LIMIT 2
            """
        ).format(schema),
        [plan.sale_id],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ReceivablePersistenceConflict(
            f"receivable was not found after insert conflict: {plan.sale_id}"
        )
    row = rows[0]
    expected = {
        "receivable_id": plan.receivable_id,
        "sim_run_id": plan.sim_run_id,
        "sale_id": plan.sale_id,
        "issued_date": plan.issued_date,
        "due_date": plan.due_date,
        "original_amount_krw": plan.original_amount_krw,
        "received_amount_krw": plan.received_amount_krw,
        "outstanding_amount_krw": plan.outstanding_amount_krw,
        "status": plan.status,
    }
    for key, value in expected.items():
        if row_value(row, key) != value:
            raise ReceivablePersistenceConflict(
                f"conflicting receivable row for {plan.sale_id}: {key}"
            )
