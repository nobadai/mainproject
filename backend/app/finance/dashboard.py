"""Finance 화면용 DB 조회와 dashboard 응답 조립."""

from datetime import date
from decimal import Decimal

from psycopg import sql

from app.finance.db import fetch_all, fetch_one, get_db_schema
from app.finance.schemas import (
    FinanceCashflowResponse,
    FinanceCashflowSummary,
    FinanceClosingItem,
    FinanceDashboardMeta,
    FinanceDashboardResponse,
    FinanceExpenseSummary,
    FinancePayableItem,
    FinancePayableSummary,
    FinanceReceivableItem,
    FinanceReceivableSummary,
    FinanceStateView,
)


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


_ZERO = Decimal(0)


def get_finance_dashboard(
    *, sim_run_id: str, as_of: date, recent_limit: int = 10
) -> FinanceDashboardResponse:
    meta = _dashboard_meta(sim_run_id=sim_run_id, as_of=as_of)
    state_rows = load_finance_states(sim_run_id=sim_run_id, as_of=as_of)
    receivable_summary = _receivable_summary(
        load_receivable_summary(sim_run_id=sim_run_id, as_of=as_of)
    )
    payable_summary = _payable_summary(
        load_payable_summary(sim_run_id=sim_run_id, as_of=as_of)
    )
    return FinanceDashboardResponse(
        meta=meta,
        states=_states(state_rows),
        cashflow_summary=_cashflow_summary(
            load_cashflow_summary(sim_run_id=sim_run_id, as_of=as_of)
        ),
        ledger_summary={"receivables": receivable_summary, "payables": payable_summary},
        receivables=[
            FinanceReceivableItem.model_validate(row)
            for row in load_receivables(sim_run_id=sim_run_id, as_of=as_of)
        ],
        payables=[
            FinancePayableItem.model_validate(row)
            for row in load_payables(sim_run_id=sim_run_id, as_of=as_of)
        ],
        expenses=[
            FinanceExpenseSummary.model_validate(row)
            for row in load_expense_summary(sim_run_id=sim_run_id, as_of=as_of)
        ],
        recent_closings=_closings(
            load_recent_closings(
                sim_run_id=sim_run_id, as_of=as_of, limit=recent_limit
            ),
            states=state_rows,
        ),
    )


def get_finance_cashflow(
    *, sim_run_id: str, as_of: date, days: int = 30
) -> FinanceCashflowResponse:
    states = load_finance_states(sim_run_id=sim_run_id, as_of=as_of)
    return FinanceCashflowResponse(
        meta=_dashboard_meta(sim_run_id=sim_run_id, as_of=as_of),
        cashflow=_closings(
            load_cashflow(sim_run_id=sim_run_id, as_of=as_of, days=days),
            states=states,
        ),
    )


def _dashboard_meta(*, sim_run_id: str, as_of: date) -> FinanceDashboardMeta:
    row = load_finance_dashboard_meta(sim_run_id=sim_run_id, as_of=as_of)
    return FinanceDashboardMeta(
        sim_run_id=sim_run_id,
        as_of=as_of,
        data_type=None if row is None else str(row["data_type"]),
    )


def _states(rows: list[dict[str, object]]) -> list[FinanceStateView]:
    result = []
    for row in rows:
        cash = _decimal(row["current_cash_krw"])
        minimum = _decimal(row["minimum_operating_cash_krw"])
        result.append(
            FinanceStateView(
                **row,
                operating_cash_buffer_krw=cash - minimum,
            )
        )
    return result


def _cashflow_summary(row: dict[str, object] | None) -> FinanceCashflowSummary:
    row = row or {}
    return FinanceCashflowSummary(
        purchase_cash_out_krw=_decimal(row.get("purchase_cash_out_krw")),
        logistics_cash_out_krw=_decimal(row.get("logistics_cash_out_krw")),
        payroll_interest_cash_out_krw=_decimal(row.get("payroll_interest_cash_out_krw")),
        sales_recognized_krw=_decimal(row.get("sales_recognized_krw")),
        collection_cash_in_krw=_decimal(row.get("collection_cash_in_krw")),
        base_net_cash_krw=_decimal(row.get("base_net_cash_krw")),
        loan_execution_krw=_decimal(row.get("loan_execution_krw")),
    )


def _receivable_summary(row: dict[str, object] | None) -> FinanceReceivableSummary:
    row = row or {}
    return FinanceReceivableSummary(
        count=int(row.get("count") or 0),
        collected_count=int(row.get("collected_count") or 0),
        partial_count=int(row.get("partial_count") or 0),
        open_count=int(row.get("open_count") or 0),
        original_amount_krw=_decimal(row.get("original_amount_krw")),
        received_amount_krw=_decimal(row.get("received_amount_krw")),
        outstanding_amount_krw=_decimal(row.get("outstanding_amount_krw")),
        overdue_amount_krw=_decimal(row.get("overdue_amount_krw")),
    )


def _payable_summary(row: dict[str, object] | None) -> FinancePayableSummary:
    row = row or {}
    return FinancePayableSummary(
        count=int(row.get("count") or 0),
        original_amount_krw=_decimal(row.get("original_amount_krw")),
        paid_amount_krw=_decimal(row.get("paid_amount_krw")),
        outstanding_amount_krw=_decimal(row.get("outstanding_amount_krw")),
        overdue_amount_krw=_decimal(row.get("overdue_amount_krw")),
    )


def _closings(
    rows: list[dict[str, object]], *, states: list[dict[str, object]]
) -> list[FinanceClosingItem]:
    minimum = _minimum_cash(states)
    return [
        FinanceClosingItem(
            **_closing_payload(row),
            minimum_operating_cash_krw=minimum,
            base_operating_buffer_krw=None
            if minimum is None
            else _decimal(row["base_cash_balance_krw"]) - minimum,
            loan_operating_buffer_krw=None
            if minimum is None
            else _decimal(row["loan_cash_balance_krw"]) - minimum,
        )
        for row in rows
    ]


def _closing_payload(row: dict[str, object]) -> dict[str, object]:
    return {
        key: row[key]
        for key in (
            "close_date",
            "day_no",
            "purchase_cash_out_krw",
            "logistics_cash_out_krw",
            "payroll_interest_cash_out_krw",
            "sales_recognized_krw",
            "collection_cash_in_krw",
            "base_net_cash_krw",
            "base_cash_balance_krw",
            "loan_execution_krw",
            "loan_cash_balance_krw",
            "receivables_balance_krw",
            "inventory_qty_kg",
            "accounting_inventory_cost_krw",
        )
    }


def _minimum_cash(rows: list[dict[str, object]]) -> Decimal | None:
    for row in rows:
        if row.get("financing_mode") == "BASE_NO_LOAN":
            return _decimal(row.get("minimum_operating_cash_krw"))
    if rows:
        return _decimal(rows[0].get("minimum_operating_cash_krw"))
    return None


def _decimal(value: object) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
