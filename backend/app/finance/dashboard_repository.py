"""Finance Dashboard read-only repository."""

from datetime import date

from psycopg import sql

from app.finance.db import fetch_all, fetch_one, get_db_schema


def load_finance_dashboard_meta(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    query = sql.SQL(
        """
        SELECT sim_run_id, %s::date AS as_of, run_type AS data_type
        FROM {}.sim_runs
        WHERE sim_run_id = %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_one(query, [as_of, sim_run_id])


def load_finance_states(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT
            finance_state_id,
            state_date,
            state_type,
            financing_mode,
            current_cash_krw,
            minimum_operating_cash_krw,
            committed_outflows_krw,
            unsettled_purchase_payables_krw,
            receivables_krw,
            inventory_book_value_krw,
            operational_inventory_value_krw,
            current_debt_krw,
            financial_limit_krw,
            recommended_loan_amount_krw,
            note
        FROM {}.finance_states
        WHERE sim_run_id = %s
          AND state_date = %s
        ORDER BY financing_mode
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of])


def load_cashflow_summary(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    query = sql.SQL(
        """
        SELECT
            COALESCE(SUM(purchase_cash_out_krw), 0) AS purchase_cash_out_krw,
            COALESCE(SUM(logistics_cash_out_krw), 0) AS logistics_cash_out_krw,
            COALESCE(SUM(payroll_interest_cash_out_krw), 0)
                AS payroll_interest_cash_out_krw,
            COALESCE(SUM(sales_recognized_krw), 0) AS sales_recognized_krw,
            COALESCE(SUM(collection_cash_in_krw), 0) AS collection_cash_in_krw,
            COALESCE(SUM(base_net_cash_krw), 0) AS base_net_cash_krw,
            COALESCE(SUM(loan_execution_krw), 0) AS loan_execution_krw
        FROM {}.daily_closings
        WHERE sim_run_id = %s
          AND close_date <= %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_one(query, [sim_run_id, as_of])


def load_receivable_summary(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    query = sql.SQL(
        """
        SELECT
            COUNT(*)::int AS count,
            COUNT(*) FILTER (WHERE status = 'COLLECTED')::int AS collected_count,
            COUNT(*) FILTER (WHERE status = 'PARTIAL')::int AS partial_count,
            COUNT(*) FILTER (WHERE status = 'OPEN')::int AS open_count,
            COALESCE(SUM(original_amount_krw), 0) AS original_amount_krw,
            COALESCE(SUM(received_amount_krw), 0) AS received_amount_krw,
            COALESCE(SUM(outstanding_amount_krw), 0) AS outstanding_amount_krw,
            COALESCE(SUM(outstanding_amount_krw) FILTER (
                WHERE due_date < %s AND outstanding_amount_krw > 0
            ), 0) AS overdue_amount_krw
        FROM {}.receivables
        WHERE sim_run_id = %s
          AND issued_date <= %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_one(query, [as_of, sim_run_id, as_of])


def load_payable_summary(*, sim_run_id: str, as_of: date) -> dict[str, object] | None:
    query = sql.SQL(
        """
        SELECT
            COUNT(*)::int AS count,
            COALESCE(SUM(original_amount_krw), 0) AS original_amount_krw,
            COALESCE(SUM(paid_amount_krw), 0) AS paid_amount_krw,
            COALESCE(SUM(outstanding_amount_krw), 0) AS outstanding_amount_krw,
            COALESCE(SUM(outstanding_amount_krw) FILTER (
                WHERE due_date < %s AND outstanding_amount_krw > 0
            ), 0) AS overdue_amount_krw
        FROM {}.payables
        WHERE sim_run_id = %s
          AND issued_date <= %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_one(query, [as_of, sim_run_id, as_of])


def load_receivables(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT receivable_id, sale_id, issued_date, due_date, original_amount_krw,
               received_amount_krw, outstanding_amount_krw, status
        FROM {}.receivables
        WHERE sim_run_id = %s
          AND issued_date <= %s
        ORDER BY due_date ASC, receivable_id ASC
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of])


def load_payables(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT payable_id, purchase_id, issued_date, due_date, original_amount_krw,
               paid_amount_krw, outstanding_amount_krw, status
        FROM {}.payables
        WHERE sim_run_id = %s
          AND issued_date <= %s
        ORDER BY due_date ASC, payable_id ASC
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of])


def load_expense_summary(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT
            expense_category,
            status,
            COUNT(*)::int AS expense_count,
            COALESCE(SUM(amount_krw), 0) AS total_amount_krw,
            COALESCE(SUM(amount_krw) FILTER (WHERE is_fixed), 0) AS fixed_amount_krw,
            COALESCE(SUM(amount_krw) FILTER (WHERE NOT is_fixed), 0)
                AS variable_amount_krw
        FROM {}.expenses
        WHERE sim_run_id = %s
          AND expense_date <= %s
        GROUP BY expense_category, status
        ORDER BY expense_category, status
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of])


def load_recent_closings(
    *, sim_run_id: str, as_of: date, limit: int
) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT *
        FROM {}.daily_closings
        WHERE sim_run_id = %s
          AND close_date <= %s
        ORDER BY close_date DESC, day_no DESC
        LIMIT %s
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of, limit])


def load_cashflow(*, sim_run_id: str, as_of: date, days: int) -> list[dict[str, object]]:
    query = sql.SQL(
        """
        SELECT *
        FROM (
            SELECT *
            FROM {}.daily_closings
            WHERE sim_run_id = %s
              AND close_date <= %s
            ORDER BY close_date DESC, day_no DESC
            LIMIT %s
        ) AS recent
        ORDER BY close_date ASC, day_no ASC
        """
    ).format(sql.Identifier(get_db_schema()))
    return fetch_all(query, [sim_run_id, as_of, days])
