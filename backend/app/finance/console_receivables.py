"""Finance operations-console receivable read model; strictly run-scoped."""
# ruff: noqa: E501

from datetime import date
from decimal import Decimal

from psycopg import sql
from pydantic import BaseModel

from app.finance.aging import AgingBucket, classify_receivable_aging
from app.finance.db import fetch_all, get_db_schema


class ConsoleReceivableRow(BaseModel):
    receivable_id: str
    sale_id: str
    partner_id: str | None
    partner_name: str | None
    original_amount_krw: Decimal
    received_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    due_date: date
    days_overdue: int | None
    aging_bucket: AgingBucket
    status: str


class ConsoleReceivableSummary(BaseModel):
    current_krw: Decimal = Decimal(0)
    days_1_7_krw: Decimal = Decimal(0)
    days_8_30_krw: Decimal = Decimal(0)
    days_30_plus_krw: Decimal = Decimal(0)
    total_outstanding_krw: Decimal = Decimal(0)


class ConsoleReceivablesResponse(BaseModel):
    sim_run_id: str
    as_of: date
    summary: ConsoleReceivableSummary
    rows: list[ConsoleReceivableRow]


def get_console_receivables(
    *,
    sim_run_id: str,
    as_of: date,
    aging_bucket: AgingBucket | None = None,
    partner_id: str | None = None,
) -> ConsoleReceivablesResponse:
    """Read only receivables belonging to the caller's explicit simulation run."""
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT r.receivable_id, r.sale_id, s.customer_partner_id AS partner_id,
               p.partner_name, r.original_amount_krw, r.received_amount_krw,
               r.outstanding_amount_krw, r.due_date, r.status
        FROM {}.receivables r
        LEFT JOIN {}.sales s ON s.sale_id = r.sale_id AND s.sim_run_id = r.sim_run_id
        LEFT JOIN {}.partners p ON p.partner_id = s.customer_partner_id
        WHERE r.sim_run_id = %s AND r.issued_date <= %s
        ORDER BY r.due_date ASC, r.receivable_id ASC
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema), sql.Identifier(schema))
    rows: list[ConsoleReceivableRow] = []
    summary = ConsoleReceivableSummary()
    for raw in fetch_all(query, [sim_run_id, as_of]):
        outstanding = raw["outstanding_amount_krw"]
        if outstanding is None:
            raise ValueError("receivables.outstanding_amount_krw must not be null")
        amount = Decimal(str(outstanding))
        bucket, overdue = classify_receivable_aging(
            outstanding_amount_krw=amount, due_date=raw["due_date"], as_of=as_of
        )
        if (aging_bucket is None or bucket == aging_bucket) and (
            partner_id is None or raw["partner_id"] == partner_id
        ):
            rows.append(
                ConsoleReceivableRow(
                    receivable_id=str(raw["receivable_id"]),
                    sale_id=str(raw["sale_id"]),
                    partner_id=None if raw["partner_id"] is None else str(raw["partner_id"]),
                    partner_name=None if raw["partner_name"] is None else str(raw["partner_name"]),
                    original_amount_krw=Decimal(str(raw["original_amount_krw"])),
                    received_amount_krw=Decimal(str(raw["received_amount_krw"])),
                    outstanding_amount_krw=amount,
                    due_date=raw["due_date"],
                    days_overdue=overdue,
                    aging_bucket=bucket,
                    status=str(raw["status"]),
                )
            )
        if bucket != "PAID":
            summary.total_outstanding_krw += amount
            if bucket == "CURRENT":
                summary.current_krw += amount
            elif bucket == "1_7":
                summary.days_1_7_krw += amount
            elif bucket == "8_30":
                summary.days_8_30_krw += amount
            elif bucket == "30_PLUS":
                summary.days_30_plus_krw += amount
    return ConsoleReceivablesResponse(
        sim_run_id=sim_run_id, as_of=as_of, summary=summary, rows=rows
    )
