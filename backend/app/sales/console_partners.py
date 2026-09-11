"""Sales operations-console partner read models; aggregates are run-scoped.

★ A partner master row is run-independent — the same customer exists in every
  simulation.  What that customer *did* is not: sales, receivables and the latest
  sale date all belong to one run, and this module keeps that split explicit so a
  screen never shows run A's turnover next to run B's partner list.
"""

from datetime import date
from decimal import Decimal

from psycopg import sql
from pydantic import BaseModel

from app.finance.aging import AgingBucket, classify_receivable_aging
from app.sales.db import fetch_all, get_db_schema

_ZERO = Decimal(0)


class ConsolePartnerRow(BaseModel):
    partner_id: str
    partner_name: str | None
    partner_type: str | None
    #: The stored `partners.active` flag, named for the screen.  Not a sales status.
    status: str
    total_sales_krw: Decimal
    total_sales_count: int
    receivable_balance_krw: Decimal
    overdue_balance_krw: Decimal
    #: Null means this partner has no sale in this run — not "no partner".
    latest_sale_date: date | None


class ConsolePartnersResponse(BaseModel):
    sim_run_id: str
    as_of: date
    rows: list[ConsolePartnerRow]


class ConsolePartnerBasic(BaseModel):
    partner_id: str
    partner_name: str | None
    partner_type: str | None
    client_type: str | None
    factory_region: str | None
    sales_collection_days: int | None
    pricing_contract_type: str | None
    status: str


class ConsolePartnerSummary(BaseModel):
    total_sales_krw: Decimal = _ZERO
    sales_count: int = 0
    contribution_profit_krw: Decimal = _ZERO
    #: Null when there is no turnover to divide by.  Zero would claim a measured
    #: margin of 0%, which is a different statement from "nothing to measure".
    contribution_margin_rate: Decimal | None = None
    receivable_balance_krw: Decimal = _ZERO
    overdue_balance_krw: Decimal = _ZERO
    latest_sale_date: date | None = None


class ConsolePartnerSaleRow(BaseModel):
    sale_id: str
    sale_date: date
    item: str | None
    quantity_kg: Decimal
    unit_price_krw: Decimal | None
    sales_amount_krw: Decimal
    contribution_profit_krw: Decimal | None


class ConsolePartnerItemRow(BaseModel):
    item: str
    quantity_kg: Decimal
    sales_amount_krw: Decimal
    contribution_profit_krw: Decimal


class ConsolePartnerReceivableRow(BaseModel):
    receivable_id: str
    sale_id: str
    due_date: date
    original_amount_krw: Decimal
    received_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    days_overdue: int | None
    aging_bucket: AgingBucket
    status: str


class ConsolePartnerDetailResponse(BaseModel):
    sim_run_id: str
    as_of: date
    basic: ConsolePartnerBasic
    summary: ConsolePartnerSummary
    recent_sales: list[ConsolePartnerSaleRow]
    item_summary: list[ConsolePartnerItemRow]
    receivables: list[ConsolePartnerReceivableRow]
    #: 🔴 Credit is Finance's number.  Sales does not compute it from receivables —
    #: a second formula here would quietly become a second credit policy.
    credit: None = None
    credit_status: str = "UNSUPPORTED"


def _decimal(value: object) -> Decimal:
    return _ZERO if value is None else Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def load_partner_rows(
    *,
    sim_run_id: str,
    as_of: date,
    query: str | None = None,
    status: str | None = None,
    partner_type: str | None = None,
) -> list[dict[str, object]]:
    """Partner masters joined to the aggregates of one run.

    The aggregate sub-selects carry `sim_run_id` themselves; the partner row does
    not, because the master table has no run axis to filter on.
    """
    schema = get_db_schema()
    conditions: list[sql.Composable] = []
    # Placeholder order below, read top-to-bottom through the statement:
    #   sales sub-select      sim_run_id, as_of
    #   receivable sub-select as_of (the overdue FILTER), sim_run_id, as_of
    params: list[object] = [sim_run_id, as_of, as_of, sim_run_id, as_of]
    if query is not None:
        conditions.append(sql.SQL("(p.partner_id ILIKE %s OR p.partner_name ILIKE %s)"))
        params.extend([f"%{query}%", f"%{query}%"])
    if status is not None:
        conditions.append(sql.SQL("(CASE WHEN p.active THEN 'ACTIVE' ELSE 'INACTIVE' END) = %s"))
        params.append(status)
    if partner_type is not None:
        conditions.append(sql.SQL("p.partner_type = %s"))
        params.append(partner_type)
    statement = sql.SQL(
        """
        SELECT p.partner_id, p.partner_name, p.partner_type, p.active,
               COALESCE(s.total_sales_krw, 0) AS total_sales_krw,
               COALESCE(s.total_sales_count, 0) AS total_sales_count,
               s.latest_sale_date,
               COALESCE(r.receivable_balance_krw, 0) AS receivable_balance_krw,
               COALESCE(r.overdue_balance_krw, 0) AS overdue_balance_krw
        FROM {schema}.partners p
        LEFT JOIN (
            SELECT customer_partner_id,
                   SUM(total_amount_krw) AS total_sales_krw,
                   COUNT(*)::int AS total_sales_count,
                   MAX(sale_date) AS latest_sale_date
            FROM {schema}.sales
            WHERE sim_run_id = %s AND sale_date <= %s
            GROUP BY customer_partner_id
        ) s ON s.customer_partner_id = p.partner_id
        LEFT JOIN (
            SELECT sa.customer_partner_id,
                   SUM(rc.outstanding_amount_krw) AS receivable_balance_krw,
                   SUM(rc.outstanding_amount_krw)
                       FILTER (WHERE rc.due_date < %s) AS overdue_balance_krw
            FROM {schema}.receivables rc
            JOIN {schema}.sales sa
              ON sa.sale_id = rc.sale_id AND sa.sim_run_id = rc.sim_run_id
            WHERE rc.sim_run_id = %s AND rc.issued_date <= %s
            GROUP BY sa.customer_partner_id
        ) r ON r.customer_partner_id = p.partner_id
        """
    ).format(schema=sql.Identifier(schema))
    if conditions:
        statement += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
    statement += sql.SQL(" ORDER BY p.partner_id ASC")
    return fetch_all(statement, params)


def get_console_partners(
    *,
    sim_run_id: str,
    as_of: date,
    query: str | None = None,
    status: str | None = None,
    partner_type: str | None = None,
) -> ConsolePartnersResponse:
    """Partner list with this run's sales and receivable aggregates already joined."""
    rows = [
        ConsolePartnerRow(
            partner_id=str(raw["partner_id"]),
            partner_name=None if raw["partner_name"] is None else str(raw["partner_name"]),
            partner_type=None if raw["partner_type"] is None else str(raw["partner_type"]),
            status="ACTIVE" if raw["active"] else "INACTIVE",
            total_sales_krw=_decimal(raw["total_sales_krw"]),
            total_sales_count=int(raw["total_sales_count"]),
            receivable_balance_krw=_decimal(raw["receivable_balance_krw"]),
            overdue_balance_krw=_decimal(raw["overdue_balance_krw"]),
            latest_sale_date=raw["latest_sale_date"],
        )
        for raw in load_partner_rows(
            sim_run_id=sim_run_id,
            as_of=as_of,
            query=query,
            status=status,
            partner_type=partner_type,
        )
    ]
    return ConsolePartnersResponse(sim_run_id=sim_run_id, as_of=as_of, rows=rows)


def _load_basic(*, partner_id: str) -> dict[str, object] | None:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT partner_id, partner_name, partner_type, client_type, factory_region,
               sales_collection_days, pricing_contract_type, active
        FROM {}.partners WHERE partner_id = %s
        """
    ).format(sql.Identifier(schema))
    rows = fetch_all(statement, [partner_id])
    return None if not rows else rows[0]


def _load_sales(*, sim_run_id: str, as_of: date, partner_id: str, limit: int) -> list[dict]:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT s.sale_id, s.sale_date, s.total_quantity_kg, s.total_amount_krw,
               s.contribution_profit_krw, i.item_id, i.unit_price_krw_per_kg
        FROM {schema}.sales s
        LEFT JOIN LATERAL (
            SELECT item_id, unit_price_krw_per_kg
            FROM {schema}.sale_items
            WHERE sale_id = s.sale_id
            ORDER BY sale_item_id ASC
            LIMIT 1
        ) i ON TRUE
        WHERE s.sim_run_id = %s AND s.customer_partner_id = %s AND s.sale_date <= %s
        ORDER BY s.sale_date DESC, s.sale_id DESC
        LIMIT %s
        """
    ).format(schema=sql.Identifier(schema))
    return fetch_all(statement, [sim_run_id, partner_id, as_of, limit])


def _load_totals(*, sim_run_id: str, as_of: date, partner_id: str) -> dict[str, object]:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT COALESCE(SUM(total_amount_krw), 0) AS total_sales_krw,
               COUNT(*)::int AS sales_count,
               COALESCE(SUM(contribution_profit_krw), 0) AS contribution_profit_krw,
               MAX(sale_date) AS latest_sale_date
        FROM {}.sales
        WHERE sim_run_id = %s AND customer_partner_id = %s AND sale_date <= %s
        """
    ).format(sql.Identifier(schema))
    return fetch_all(statement, [sim_run_id, partner_id, as_of])[0]


def _load_items(*, sim_run_id: str, as_of: date, partner_id: str) -> list[dict]:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT i.item_id,
               COALESCE(SUM(i.quantity_kg), 0) AS quantity_kg,
               COALESCE(SUM(i.line_amount_krw), 0) AS sales_amount_krw,
               COALESCE(SUM(i.contribution_profit_krw), 0) AS contribution_profit_krw
        FROM {schema}.sale_items i
        JOIN {schema}.sales s ON s.sale_id = i.sale_id
        WHERE s.sim_run_id = %s AND s.customer_partner_id = %s AND s.sale_date <= %s
        GROUP BY i.item_id
        ORDER BY i.item_id ASC
        """
    ).format(schema=sql.Identifier(schema))
    return fetch_all(statement, [sim_run_id, partner_id, as_of])


def _load_receivables(*, sim_run_id: str, as_of: date, partner_id: str) -> list[dict]:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT r.receivable_id, r.sale_id, r.due_date, r.original_amount_krw,
               r.received_amount_krw, r.outstanding_amount_krw, r.status
        FROM {schema}.receivables r
        JOIN {schema}.sales s ON s.sale_id = r.sale_id AND s.sim_run_id = r.sim_run_id
        WHERE r.sim_run_id = %s AND s.customer_partner_id = %s AND r.issued_date <= %s
        ORDER BY r.due_date ASC, r.receivable_id ASC
        """
    ).format(schema=sql.Identifier(schema))
    return fetch_all(statement, [sim_run_id, partner_id, as_of])


def get_console_partner_detail(
    *, sim_run_id: str, as_of: date, partner_id: str, recent_limit: int = 20
) -> ConsolePartnerDetailResponse | None:
    """One partner seen through one run.  Returns null when the partner is unknown.

    🔴 Aging comes from Finance's `classify_receivable_aging`.  Sales owning a second
    aging rule would let the two screens disagree about the same receivable.
    """
    basic_raw = _load_basic(partner_id=partner_id)
    if basic_raw is None:
        return None
    totals = _load_totals(sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id)
    total_sales = _decimal(totals["total_sales_krw"])
    profit = _decimal(totals["contribution_profit_krw"])
    summary = ConsolePartnerSummary(
        total_sales_krw=total_sales,
        sales_count=int(totals["sales_count"]),
        contribution_profit_krw=profit,
        contribution_margin_rate=None if total_sales == _ZERO else profit / total_sales,
        latest_sale_date=totals["latest_sale_date"],
    )
    receivables: list[ConsolePartnerReceivableRow] = []
    for raw in _load_receivables(sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id):
        outstanding = raw["outstanding_amount_krw"]
        if outstanding is None:
            raise ValueError("receivables.outstanding_amount_krw must not be null")
        amount = Decimal(str(outstanding))
        bucket, overdue = classify_receivable_aging(
            outstanding_amount_krw=amount, due_date=raw["due_date"], as_of=as_of
        )
        if bucket != "PAID":
            summary.receivable_balance_krw += amount
            if overdue:
                summary.overdue_balance_krw += amount
        receivables.append(
            ConsolePartnerReceivableRow(
                receivable_id=str(raw["receivable_id"]),
                sale_id=str(raw["sale_id"]),
                due_date=raw["due_date"],
                original_amount_krw=_decimal(raw["original_amount_krw"]),
                received_amount_krw=_decimal(raw["received_amount_krw"]),
                outstanding_amount_krw=amount,
                days_overdue=overdue,
                aging_bucket=bucket,
                status=str(raw["status"]),
            )
        )
    recent = [
        ConsolePartnerSaleRow(
            sale_id=str(raw["sale_id"]),
            sale_date=raw["sale_date"],
            item=None if raw["item_id"] is None else str(raw["item_id"]),
            quantity_kg=_decimal(raw["total_quantity_kg"]),
            unit_price_krw=_optional_decimal(raw["unit_price_krw_per_kg"]),
            sales_amount_krw=_decimal(raw["total_amount_krw"]),
            contribution_profit_krw=_optional_decimal(raw["contribution_profit_krw"]),
        )
        for raw in _load_sales(
            sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id, limit=recent_limit
        )
    ]
    items = [
        ConsolePartnerItemRow(
            item=str(raw["item_id"]),
            quantity_kg=_decimal(raw["quantity_kg"]),
            sales_amount_krw=_decimal(raw["sales_amount_krw"]),
            contribution_profit_krw=_decimal(raw["contribution_profit_krw"]),
        )
        for raw in _load_items(sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id)
    ]
    return ConsolePartnerDetailResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        basic=ConsolePartnerBasic(
            partner_id=str(basic_raw["partner_id"]),
            partner_name=None
            if basic_raw["partner_name"] is None
            else str(basic_raw["partner_name"]),
            partner_type=None
            if basic_raw["partner_type"] is None
            else str(basic_raw["partner_type"]),
            client_type=None
            if basic_raw["client_type"] is None
            else str(basic_raw["client_type"]),
            factory_region=None
            if basic_raw["factory_region"] is None
            else str(basic_raw["factory_region"]),
            sales_collection_days=basic_raw["sales_collection_days"],
            pricing_contract_type=None
            if basic_raw["pricing_contract_type"] is None
            else str(basic_raw["pricing_contract_type"]),
            status="ACTIVE" if basic_raw["active"] else "INACTIVE",
        ),
        summary=summary,
        recent_sales=recent,
        item_summary=items,
        receivables=receivables,
    )
