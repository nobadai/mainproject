"""Finance Dashboard response assembly."""

from datetime import date
from decimal import Decimal

from app.finance import dashboard_repository as repository
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

_ZERO = Decimal(0)


def get_finance_dashboard(
    *, sim_run_id: str, as_of: date, recent_limit: int = 10
) -> FinanceDashboardResponse:
    meta = _dashboard_meta(sim_run_id=sim_run_id, as_of=as_of)
    state_rows = repository.load_finance_states(sim_run_id=sim_run_id, as_of=as_of)
    receivable_summary = _receivable_summary(
        repository.load_receivable_summary(sim_run_id=sim_run_id, as_of=as_of)
    )
    payable_summary = _payable_summary(
        repository.load_payable_summary(sim_run_id=sim_run_id, as_of=as_of)
    )
    return FinanceDashboardResponse(
        meta=meta,
        states=_states(state_rows),
        cashflow_summary=_cashflow_summary(
            repository.load_cashflow_summary(sim_run_id=sim_run_id, as_of=as_of)
        ),
        ledger_summary={"receivables": receivable_summary, "payables": payable_summary},
        receivables=[
            FinanceReceivableItem.model_validate(row)
            for row in repository.load_receivables(sim_run_id=sim_run_id, as_of=as_of)
        ],
        payables=[
            FinancePayableItem.model_validate(row)
            for row in repository.load_payables(sim_run_id=sim_run_id, as_of=as_of)
        ],
        expenses=[
            FinanceExpenseSummary.model_validate(row)
            for row in repository.load_expense_summary(sim_run_id=sim_run_id, as_of=as_of)
        ],
        recent_closings=_closings(
            repository.load_recent_closings(
                sim_run_id=sim_run_id, as_of=as_of, limit=recent_limit
            ),
            states=state_rows,
        ),
    )


def get_finance_cashflow(
    *, sim_run_id: str, as_of: date, days: int = 30
) -> FinanceCashflowResponse:
    states = repository.load_finance_states(sim_run_id=sim_run_id, as_of=as_of)
    return FinanceCashflowResponse(
        meta=_dashboard_meta(sim_run_id=sim_run_id, as_of=as_of),
        cashflow=_closings(
            repository.load_cashflow(sim_run_id=sim_run_id, as_of=as_of, days=days),
            states=states,
        ),
    )


def _dashboard_meta(*, sim_run_id: str, as_of: date) -> FinanceDashboardMeta:
    row = repository.load_finance_dashboard_meta(sim_run_id=sim_run_id, as_of=as_of)
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
