"""Sales Dashboard read-only repository."""

from datetime import date

from psycopg import sql

from app.sales.db import fetch_all, fetch_one, get_db_schema


def load_sales_dashboard_meta(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    query = sql.SQL(
        """
        SELECT sim_run_id, %s::date AS as_of, run_type AS data_type
        FROM {}.sim_runs
        WHERE sim_run_id = %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_one(query, [as_of, sim_run_id])


def load_sales_summary(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            COUNT(*)::int AS sales_count,
            COUNT(DISTINCT s.customer_partner_id)::int AS customer_count,
            COALESCE(SUM(s.total_quantity_kg), 0) AS total_sales_quantity_kg,
            COALESCE(SUM(s.total_amount_krw), 0) AS total_sales_amount_krw,
            COALESCE(SUM(s.contribution_profit_krw), 0) AS contribution_profit_krw,
            COALESCE(SUM(r.received_amount_krw), 0) AS received_amount_krw,
            COALESCE(SUM(r.outstanding_amount_krw), 0) AS outstanding_receivables_krw
        FROM {}.sales AS s
        LEFT JOIN {}.receivables AS r
          ON r.sale_id = s.sale_id
         AND r.sim_run_id = s.sim_run_id
         AND r.issued_date <= %s
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema))
    return fetch_one(query, [as_of, sim_run_id, as_of])


def load_collection_summary(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT
            s.collection_status,
            COUNT(*)::int AS count,
            COALESCE(SUM(s.total_amount_krw), 0) AS sales_amount_krw
        FROM {}.sales AS s
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        GROUP BY s.collection_status
        ORDER BY s.collection_status
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of])


def load_item_summaries(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            si.item_id,
            i.item_name,
            COUNT(*)::int AS line_count,
            COALESCE(SUM(si.quantity_kg), 0) AS total_quantity_kg,
            COALESCE(SUM(si.line_amount_krw), 0) AS sales_amount_krw,
            COALESCE(SUM(si.contribution_profit_krw), 0) AS contribution_profit_krw,
            COALESCE(SUM(si.line_amount_krw) / NULLIF(SUM(si.quantity_kg), 0), 0)
                AS avg_unit_price_krw_per_kg
        FROM {}.sale_items AS si
        JOIN {}.sales AS s
          ON s.sale_id = si.sale_id
        JOIN {}.items AS i
          ON i.item_id = si.item_id
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        GROUP BY si.item_id, i.item_name
        ORDER BY sales_amount_krw DESC, si.item_id
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema), sql.Identifier(schema))
    return fetch_all(query, [sim_run_id, as_of])


def load_recent_sales(*, sim_run_id: str, as_of: date, limit: int) -> list[dict[str, object]]:
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            s.sale_id,
            s.sale_date,
            s.customer_partner_id,
            p.partner_name,
            s.total_quantity_kg,
            s.total_amount_krw,
            s.contribution_profit_krw,
            s.collection_due_date,
            s.collection_status,
            s.order_status
        FROM {}.sales AS s
        LEFT JOIN {}.partners AS p
          ON p.partner_id = s.customer_partner_id
        WHERE s.sim_run_id = %s
          AND s.sale_date <= %s
        ORDER BY s.sale_date DESC, s.sale_id DESC
        LIMIT %s
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema))
    return fetch_all(query, [sim_run_id, as_of, limit])


def load_sales_receivables(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    schema = get_db_schema()
    query = sql.SQL(
        """
        SELECT
            r.receivable_id,
            r.sale_id,
            s.sale_date,
            s.customer_partner_id,
            p.partner_name,
            r.issued_date,
            r.due_date,
            r.original_amount_krw,
            r.received_amount_krw,
            r.outstanding_amount_krw,
            r.status
        FROM {}.receivables AS r
        JOIN {}.sales AS s
          ON s.sale_id = r.sale_id
         AND s.sim_run_id = r.sim_run_id
        LEFT JOIN {}.partners AS p
          ON p.partner_id = s.customer_partner_id
        WHERE r.sim_run_id = %s
          AND r.issued_date <= %s
          AND s.sale_date <= %s
        ORDER BY r.due_date ASC, r.receivable_id ASC
        """
    ).format(sql.Identifier(schema), sql.Identifier(schema), sql.Identifier(schema))
    return fetch_all(query, [sim_run_id, as_of, as_of])
