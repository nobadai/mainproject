"""Sales operations-console collection read model; strictly run-scoped.

🔴 **There is no collection table and this module does not invent one.**  What a
collection *is* — money owed against a confirmed sale — is already recorded in
`receivables`.  A second entity here would be a second truth about the same money.

🔴 **Aging comes from Finance.**  `app.finance.aging.classify_receivable_aging` is
the one rule; a Sales-local copy would let the 수금 screen and the 채권 screen put
the same receivable in different buckets, and neither would be wrong on its own.
"""

from datetime import date
from decimal import Decimal

from psycopg import sql
from pydantic import BaseModel

from app.finance.aging import AgingBucket, classify_receivable_aging
from app.sales.db import fetch_all, get_db_schema

_ZERO = Decimal(0)


class ConsoleCollectionRow(BaseModel):
    partner_id: str | None
    partner_name: str | None
    sale_id: str
    receivable_id: str
    original_amount_krw: Decimal
    received_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    due_date: date
    #: Null once nothing is outstanding — a settled receivable is not "0 days late".
    days_overdue: int | None
    aging_bucket: AgingBucket
    status: str


class ConsoleCollectionSummary(BaseModel):
    total_outstanding_krw: Decimal = _ZERO
    overdue_krw: Decimal = _ZERO
    collected_krw: Decimal = _ZERO


class ConsoleCollectionsResponse(BaseModel):
    sim_run_id: str
    as_of: date
    summary: ConsoleCollectionSummary
    rows: list[ConsoleCollectionRow]


def load_collection_rows(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    """Receivables of one run, with the partner the sale was made to."""
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT r.receivable_id, r.sale_id, r.due_date, r.original_amount_krw,
               r.received_amount_krw, r.outstanding_amount_krw, r.status,
               s.customer_partner_id AS partner_id, p.partner_name
        FROM {schema}.receivables r
        JOIN {schema}.sales s ON s.sale_id = r.sale_id AND s.sim_run_id = r.sim_run_id
        LEFT JOIN {schema}.partners p ON p.partner_id = s.customer_partner_id
        WHERE r.sim_run_id = %s AND r.issued_date <= %s
        ORDER BY r.due_date ASC, r.receivable_id ASC
        """
    ).format(schema=sql.Identifier(schema))
    return fetch_all(statement, [sim_run_id, as_of])


def get_console_collections(
    *,
    sim_run_id: str,
    as_of: date,
    partner_id: str | None = None,
    aging_bucket: AgingBucket | None = None,
    status: str | None = None,
) -> ConsoleCollectionsResponse:
    """Collections for exactly one simulation run.

    The summary counts every receivable in the run, not just the filtered rows —
    a filter narrows what is listed, never what is owed.
    """
    summary = ConsoleCollectionSummary()
    rows: list[ConsoleCollectionRow] = []
    for raw in load_collection_rows(sim_run_id=sim_run_id, as_of=as_of):
        outstanding = raw["outstanding_amount_krw"]
        if outstanding is None:
            raise ValueError("receivables.outstanding_amount_krw must not be null")
        amount = Decimal(str(outstanding))
        received = _ZERO if raw["received_amount_krw"] is None else Decimal(
            str(raw["received_amount_krw"])
        )
        bucket, overdue = classify_receivable_aging(
            outstanding_amount_krw=amount, due_date=raw["due_date"], as_of=as_of
        )
        summary.collected_krw += received
        if bucket != "PAID":
            summary.total_outstanding_krw += amount
            if overdue:
                summary.overdue_krw += amount
        row_partner = None if raw["partner_id"] is None else str(raw["partner_id"])
        row_status = str(raw["status"])
        if partner_id is not None and row_partner != partner_id:
            continue
        if aging_bucket is not None and bucket != aging_bucket:
            continue
        if status is not None and row_status != status:
            continue
        rows.append(
            ConsoleCollectionRow(
                partner_id=row_partner,
                partner_name=None if raw["partner_name"] is None else str(raw["partner_name"]),
                sale_id=str(raw["sale_id"]),
                receivable_id=str(raw["receivable_id"]),
                original_amount_krw=Decimal(str(raw["original_amount_krw"])),
                received_amount_krw=received,
                outstanding_amount_krw=amount,
                due_date=raw["due_date"],
                days_overdue=overdue,
                aging_bucket=bucket,
                status=row_status,
            )
        )
    return ConsoleCollectionsResponse(
        sim_run_id=sim_run_id, as_of=as_of, summary=summary, rows=rows
    )
