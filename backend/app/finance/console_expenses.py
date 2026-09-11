"""Finance operations-console expense read model; strictly run-scoped."""

from datetime import date
from decimal import Decimal

from psycopg import sql
from pydantic import BaseModel

from app.finance.db import fetch_all, get_db_schema

#: Screen wording for the categories the ledger actually stores.  The stored value
#: is the record; this only names it in Korean.
#:
#: A category missing from this table is **not** an error and is never re-bucketed
#: into "기타" — the row keeps its stored name in both fields, so a category added
#: to the ledger shows up as itself instead of quietly joining someone else's total.
_DISPLAY_NAMES: dict[str, str] = {
    "LABOR": "인건비",
    "RENT": "임차료",
    "UTILITY": "수도광열비",
    "LOGISTICS": "물류비",
    "TRANSPORT": "운송비",
    "COMMISSION": "수수료",
    "INTEREST": "이자비용",
    "LOAN_INTEREST": "이자비용",
    "PACKAGING": "포장비",
    "DISPOSAL": "폐기비용",
    "OTHER": "기타",
}


class ConsoleExpenseRow(BaseModel):
    expense_id: str
    expense_date: date
    #: What the ledger stores.  Grouping and filtering use this, never the label.
    raw_category: str
    #: What the screen shows.  Falls back to the stored value when unnamed.
    display_category: str
    amount_krw: Decimal
    #: Null means the stored row carries no evidence reference.
    source_ref: str | None
    note: str | None


class ConsoleExpenseCategoryTotal(BaseModel):
    raw_category: str
    display_category: str
    expense_count: int
    total_amount_krw: Decimal


class ConsoleExpenseSummary(BaseModel):
    total_expenses_krw: Decimal = Decimal(0)
    category_totals: list[ConsoleExpenseCategoryTotal] = []


class ConsoleExpensesResponse(BaseModel):
    sim_run_id: str
    as_of: date
    summary: ConsoleExpenseSummary
    rows: list[ConsoleExpenseRow]


def display_category(raw_category: str) -> str:
    """Name a stored category for the screen without reclassifying it."""
    return _DISPLAY_NAMES.get(raw_category, raw_category)


def load_console_expenses(
    *,
    sim_run_id: str,
    as_of: date,
    from_date: date | None = None,
    to_date: date | None = None,
    category: str | None = None,
) -> list[dict[str, object]]:
    """Expense rows for one run.  `as_of` is the ceiling the run has reached."""
    schema = get_db_schema()
    conditions: list[sql.Composable] = [
        sql.SQL("sim_run_id = %s"),
        sql.SQL("expense_date <= %s"),
    ]
    params: list[object] = [sim_run_id, as_of]
    if from_date is not None:
        conditions.append(sql.SQL("expense_date >= %s"))
        params.append(from_date)
    if to_date is not None:
        conditions.append(sql.SQL("expense_date <= %s"))
        params.append(to_date)
    if category is not None:
        conditions.append(sql.SQL("expense_category = %s"))
        params.append(category)
    query = (
        sql.SQL(
            """
            SELECT expense_id, expense_date, expense_category, amount_krw,
                   evidence_id, note
            FROM {}.expenses
            WHERE
            """
        ).format(sql.Identifier(schema))
        + sql.SQL(" AND ").join(conditions)
        + sql.SQL(" ORDER BY expense_date DESC, expense_id ASC")
    )
    return fetch_all(query, params)


def get_console_expenses(
    *,
    sim_run_id: str,
    as_of: date,
    from_date: date | None = None,
    to_date: date | None = None,
    category: str | None = None,
) -> ConsoleExpensesResponse:
    """Read expenses for exactly one simulation run.

    Totals are built from the rows that were returned, so the number on screen is
    the sum of the lines under it.  A filtered view therefore totals the filtered
    lines — the alternative is a header that no visible row explains.
    """
    rows: list[ConsoleExpenseRow] = []
    totals: dict[str, ConsoleExpenseCategoryTotal] = {}
    summary = ConsoleExpenseSummary(category_totals=[])
    for raw in load_console_expenses(
        sim_run_id=sim_run_id,
        as_of=as_of,
        from_date=from_date,
        to_date=to_date,
        category=category,
    ):
        stored = raw["amount_krw"]
        if stored is None:
            raise ValueError("expenses.amount_krw must not be null")
        amount = Decimal(str(stored))
        raw_category = str(raw["expense_category"])
        label = display_category(raw_category)
        rows.append(
            ConsoleExpenseRow(
                expense_id=str(raw["expense_id"]),
                expense_date=raw["expense_date"],
                raw_category=raw_category,
                display_category=label,
                amount_krw=amount,
                source_ref=None if raw["evidence_id"] is None else str(raw["evidence_id"]),
                note=None if raw["note"] is None else str(raw["note"]),
            )
        )
        summary.total_expenses_krw += amount
        bucket = totals.get(raw_category)
        if bucket is None:
            totals[raw_category] = ConsoleExpenseCategoryTotal(
                raw_category=raw_category,
                display_category=label,
                expense_count=1,
                total_amount_krw=amount,
            )
        else:
            bucket.expense_count += 1
            bucket.total_amount_krw += amount
    summary.category_totals = [totals[name] for name in sorted(totals)]
    return ConsoleExpensesResponse(
        sim_run_id=sim_run_id, as_of=as_of, summary=summary, rows=rows
    )
