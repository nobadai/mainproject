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
    "PAYROLL": "급여",
    "LOGISTICS_SERVICE": "물류 용역비",
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
    #: 원장이 적어 둔 생명주기 상태. `ACCRUED` · `PAID` · `CANCELLED`.
    status: str = "PAID"
    #: 지급하기로 한 날. 이 칸이 생기기 전 행은 `None` 이다.
    due_date: date | None = None
    #: 🔴 **실제로 지급한 날. 모르면 `None` 이고, 발생일로 메우지 않는다.**
    #:
    #:   마감은 지급일을 모르는 기존 `PAID` 행에 한해 `expense_date` 를 지급 기준일로
    #:   읽지만(읽기 전용 호환), **화면은 그러면 안 된다.** 추측한 날짜를 «지급일» 이라고
    #:   적으면 사용자는 그날 돈이 나간 것으로 읽고, 통장과 맞춰 보다 원인을 못 찾는다.
    paid_date: date | None = None
    #: 지급일을 아는가. `PAID` 인데 거짓이면 «지급일 미상» 인 기존 데이터다.
    paid_date_known: bool = False
    #: 납품에 붙은 비용이면 그 납품 번호. 마감에서 물류비 칸으로 가는 근거다.
    related_delivery_id: str | None = None


class ConsoleExpenseCategoryTotal(BaseModel):
    raw_category: str
    display_category: str
    expense_count: int
    total_amount_krw: Decimal


class ConsoleExpenseSummary(BaseModel):
    total_expenses_krw: Decimal = Decimal(0)
    #: 아직 안 나간 돈. `ACCRUED` 합계다.
    accrued_krw: Decimal = Decimal(0)
    #: 실제로 나간 돈. `PAID` 합계다.
    paid_krw: Decimal = Decimal(0)
    #: 나가지 않기로 한 돈. `CANCELLED` 합계이고 **현금과 무관하다.**
    cancelled_krw: Decimal = Decimal(0)
    accrued_count: int = 0
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
    status: str | None = None,
) -> list[dict[str, object]]:
    """Expense rows for one run.  `as_of` is the ceiling the run has reached.

    ★ **발생일로 자른다.** `as_of` 는 그 실행이 도달한 날이고, 그날까지 «생긴» 비용을
      보여 준다 — 지급 예정일이 그 뒤인 `ACCRUED` 도 포함이다. 앞으로 나갈 돈을 화면에서
      빼면 이 칸을 만든 이유가 없어진다.
    """
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
    if status is not None:
        conditions.append(sql.SQL("status = %s"))
        params.append(status)
    query = (
        sql.SQL(
            """
            SELECT expense_id, expense_date, expense_category, amount_krw,
                   evidence_id, note, status, due_date, paid_date,
                   related_delivery_id
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
    status: str | None = None,
) -> ConsoleExpensesResponse:
    """Read expenses for exactly one simulation run.

    Totals are built from the rows that were returned, so the number on screen is
    the sum of the lines under it.  A filtered view therefore totals the filtered
    lines — the alternative is a header that no visible row explains.

    🔴 **상태별 합계를 따로 센다.** 예전에는 «누적 비용» 한 칸뿐이라 아직 안 나간 돈과
       이미 나간 돈이 같은 숫자에 들어 있었다. 취소한 비용까지 그 안에 있었다 — 나가지
       않기로 한 돈이 비용 총액에 섞여 있으면 그 숫자로는 아무 판단도 못 한다.
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
        status=status,
    ):
        stored = raw["amount_krw"]
        if stored is None:
            raise ValueError("expenses.amount_krw must not be null")
        amount = Decimal(str(stored))
        raw_category = str(raw["expense_category"])
        label = display_category(raw_category)
        row_status = "PAID" if raw.get("status") is None else str(raw["status"])
        paid_date = raw.get("paid_date")
        rows.append(
            ConsoleExpenseRow(
                expense_id=str(raw["expense_id"]),
                expense_date=raw["expense_date"],
                raw_category=raw_category,
                display_category=label,
                amount_krw=amount,
                source_ref=None if raw["evidence_id"] is None else str(raw["evidence_id"]),
                note=None if raw["note"] is None else str(raw["note"]),
                status=row_status,
                due_date=raw.get("due_date"),  # type: ignore[arg-type]
                paid_date=paid_date,  # type: ignore[arg-type]
                #  ★ 지급했다고 적혀 있는데 날짜가 없으면 «모른다» 다. 발생일을 대신
                #    넣지 않는다 — 화면은 사실만 말한다.
                paid_date_known=row_status == "PAID" and paid_date is not None,
                related_delivery_id=(
                    None
                    if raw.get("related_delivery_id") is None
                    else str(raw["related_delivery_id"])
                ),
            )
        )
        summary.total_expenses_krw += amount
        if row_status == "ACCRUED":
            summary.accrued_krw += amount
            summary.accrued_count += 1
        elif row_status == "PAID":
            summary.paid_krw += amount
        elif row_status == "CANCELLED":
            summary.cancelled_krw += amount
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
