"""Finance-owned daily closing facts.

The Master closing registry supplies the transaction boundary.  This module owns
the Finance facts that are written to ``daily_closings`` and deliberately does
not import Master models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.finance.db import (
    FinanceDataNotReady,
    decimal_value,
    get_connection,
    get_db_schema,
    load_inventory_snapshot_as_of,
)
from app.finance.tools import effective_cash_date

__all__ = ["FinanceDayClosing", "FinanceDayClosingResult", "close_day"]

_ZERO = Decimal(0)
_BASE_MODE = "BASE_NO_LOAN"
_LOAN_MODE = "LOAN_BASELINE"

#: `payroll_interest_cash_out_krw` 에 들어가는 **원장의 실제 비용 분류**.
#:
#: 🔴 `LOAN_INTEREST` 가 빠져 있었다. 원장이 쓰는 이름은 `INTEREST` 가 아니라
#:    `LOAN_INTEREST` 이고(실측 `expenses.expense_category`), 그래서 이자 지급이 있는
#:    날은 마감이 통째로 `daily_closing_expense_category` 로 막혔다 — 2025-12-31 이
#:    실제로 그 날이다. 이미 적힌 그날 마감값(13,035,596.88 = 급여 + 대출이자)이
#:    **두 비용을 함께 세는 것이 정본 계약임을 증명한다.**
#:
#: ★ `INTEREST` 도 남긴다. 원장 이름이 바뀐 것이 아니라 **모르는 이름을 하나 더 아는
#:   것**이고, 아는 이름을 지우면 예전 데이터가 다시 막힌다.
#:
#: ★ 목록 밖은 여전히 `FinanceDataNotReady` 다. 모르는 분류를 조용히 어느 칸에
#:   넣으면 그 순간 마감이 **틀린 값을 확정한다** — 막히는 편이 낫다.
_PAYROLL_INTEREST_CATEGORIES: frozenset[str] = frozenset(
    {"PAYROLL", "INTEREST", "LOAN_INTEREST"}
)


@dataclass(frozen=True)
class FinanceDayClosingResult:
    """Structural result consumed by Master's closing port without importing it."""

    part: str
    status: Literal["CLOSED", "NOTHING_DUE", "BLOCKED"]
    reason: str = ""
    closed: list[str] | None = None
    created: int = 0

    def __post_init__(self) -> None:
        if self.closed is None:
            object.__setattr__(self, "closed", [])


@dataclass(frozen=True)
class _FinanceState:
    financing_mode: str
    current_cash_krw: Decimal
    receivables_krw: Decimal
    current_debt_krw: Decimal


@dataclass(frozen=True)
class _ClosingFacts:
    day_no: int
    purchase_cash_out_krw: Decimal
    logistics_cash_out_krw: Decimal
    payroll_interest_cash_out_krw: Decimal
    sales_recognized_krw: Decimal
    collection_cash_in_krw: Decimal
    base_cash_balance_krw: Decimal
    loan_execution_krw: Decimal
    loan_cash_balance_krw: Decimal
    receivables_balance_krw: Decimal
    inventory_qty_kg: Decimal
    accounting_inventory_cost_krw: Decimal

    @property
    def base_net_cash_krw(self) -> Decimal:
        return (
            self.collection_cash_in_krw
            - self.purchase_cash_out_krw
            - self.logistics_cash_out_krw
            - self.payroll_interest_cash_out_krw
        )


class FinanceDayClosing:
    """Write one Finance daily-closing row with a caller-owned connection."""

    def close(self, conn: Any, *, as_of: date, sim_run_id: str) -> FinanceDayClosingResult:
        if not isinstance(sim_run_id, str) or not sim_run_id.strip():
            raise ValueError("sim_run_id must be a non-blank string")

        facts = _load_closing_facts(conn, as_of=as_of, sim_run_id=sim_run_id)
        created = _upsert_daily_closing(conn, as_of=as_of, sim_run_id=sim_run_id, facts=facts)
        return FinanceDayClosingResult(
            part="finance",
            status="CLOSED",
            closed=[f"{sim_run_id}:{as_of.isoformat()}"],
            created=created,
        )


def close_day(
    *, as_of: date, sim_run_id: str, conn: Any | None = None
) -> FinanceDayClosingResult:
    """Close a Finance day using the supplied transaction when one exists."""

    if conn is not None:
        return FinanceDayClosing().close(conn, as_of=as_of, sim_run_id=sim_run_id)
    with get_connection() as owned_connection:
        return FinanceDayClosing().close(
            owned_connection, as_of=as_of, sim_run_id=sim_run_id
        )


def _load_closing_facts(conn: Any, *, as_of: date, sim_run_id: str) -> _ClosingFacts:
    period_start = _load_period_start(conn, sim_run_id=sim_run_id, as_of=as_of)
    states = _load_exact_states(conn, sim_run_id=sim_run_id, as_of=as_of)
    base = states.get(_BASE_MODE)
    if base is None:
        raise FinanceDataNotReady("base_finance_state")

    prior_base = _load_prior_state(
        conn, sim_run_id=sim_run_id, financing_mode=_BASE_MODE, as_of=as_of
    )
    loan = states.get(_LOAN_MODE)
    prior_loan = (
        _load_prior_state(conn, sim_run_id=sim_run_id, financing_mode=_LOAN_MODE, as_of=as_of)
        if loan is not None
        else None
    )
    issued_receivables = _sum_receivables_issued(conn, sim_run_id=sim_run_id, as_of=as_of)
    collection_cash_in = _collection_delta(
        prior_receivables=prior_base.receivables_krw if prior_base is not None else _ZERO,
        issued_receivables=issued_receivables,
        current_receivables=base.receivables_krw,
    )
    receivables_balance = _sum_receivables_outstanding(
        conn, sim_run_id=sim_run_id, as_of=as_of
    )
    if receivables_balance != base.receivables_krw:
        raise FinanceDataNotReady("receivables_balance_mismatch")

    inventory = load_inventory_snapshot_as_of(conn, sim_run_id=sim_run_id, as_of=as_of)
    purchase_cash_out = _purchase_cash_out(conn, sim_run_id=sim_run_id, as_of=as_of)
    logistics_cash_out, payroll_interest_cash_out = _expense_cash_out(
        conn, sim_run_id=sim_run_id, as_of=as_of
    )
    return _ClosingFacts(
        day_no=(as_of - period_start).days + 1,
        purchase_cash_out_krw=purchase_cash_out,
        logistics_cash_out_krw=logistics_cash_out,
        payroll_interest_cash_out_krw=payroll_interest_cash_out,
        sales_recognized_krw=_sales_recognized(conn, sim_run_id=sim_run_id, as_of=as_of),
        collection_cash_in_krw=collection_cash_in,
        base_cash_balance_krw=base.current_cash_krw,
        loan_execution_krw=(
            _loan_execution(loan, prior_loan) if loan is not None else _ZERO
        ),
        loan_cash_balance_krw=loan.current_cash_krw if loan is not None else base.current_cash_krw,
        receivables_balance_krw=receivables_balance,
        inventory_qty_kg=inventory.quantity_kg,
        accounting_inventory_cost_krw=inventory.inventory_book_value_krw,
    )


def _load_period_start(conn: Any, *, sim_run_id: str, as_of: date) -> date:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT period_start, period_end
                FROM {}.sim_runs
                WHERE sim_run_id = %s
                """
            ).format(schema),
            [sim_run_id],
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise FinanceDataNotReady("sim_run")
    row = rows[0]
    period_start, period_end = _row_value(row, "period_start", 0), _row_value(row, "period_end", 1)
    if not isinstance(period_start, date) or not isinstance(period_end, date):
        raise FinanceDataNotReady("sim_run")
    if not period_start <= as_of <= period_end:
        raise FinanceDataNotReady("sim_run_date_out_of_range")
    return period_start


def _load_exact_states(conn: Any, *, sim_run_id: str, as_of: date) -> dict[str, _FinanceState]:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT financing_mode, current_cash_krw, receivables_krw, current_debt_krw
                FROM {}.finance_states
                WHERE sim_run_id = %s
                  AND state_date = %s
                """
            ).format(schema),
            [sim_run_id, as_of],
        )
        rows = cursor.fetchall()
    states: dict[str, _FinanceState] = {}
    for row in rows:
        mode = _row_value(row, "financing_mode", 0)
        if not isinstance(mode, str) or mode in states:
            raise FinanceDataNotReady("finance_state_ambiguous")
        states[mode] = _FinanceState(
            financing_mode=mode,
            current_cash_krw=_daily_closing_amount(_row_value(row, "current_cash_krw", 1)),
            receivables_krw=_daily_closing_amount(_row_value(row, "receivables_krw", 2)),
            current_debt_krw=_daily_closing_amount(_row_value(row, "current_debt_krw", 3)),
        )
    return states


def _load_prior_state(
    conn: Any, *, sim_run_id: str, financing_mode: str, as_of: date
) -> _FinanceState | None:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT state_date, financing_mode, current_cash_krw, receivables_krw,
                       current_debt_krw
                FROM {}.finance_states
                WHERE sim_run_id = %s
                  AND financing_mode = %s
                  AND state_date < %s
                ORDER BY state_date DESC
                LIMIT 2
                """
            ).format(schema),
            [sim_run_id, financing_mode, as_of],
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    # 🔴 **행이 둘이라는 것은 모호하다는 뜻이 아니다.** 예전에는 `len(rows) > 1` 만
    #    보고 세웠는데, 그 조건은 *"이 축에 이전 상태가 둘 이상 있다"* 이고 그것은
    #    **일별 상태가 쌓인 정상 실행의 모습**이다. 실측 축(`LOAN_BASELINE`, 252행)에서
    #    셋째 날부터 모든 마감이 `finance_state_ambiguous` 로 막혔다.
    #
    # ★ 모호한 것은 **가장 늦은 날짜가 둘일 때**뿐이다 — 그때만 어느 행이 직전 상태인지
    #   고를 수 없다. `load_finance_state_row` 가 이미 같은 규율을 적어 두었다.
    latest_date = _row_value(rows[0], "state_date", 0)
    if len(rows) > 1 and _row_value(rows[1], "state_date", 0) == latest_date:
        raise FinanceDataNotReady("finance_state_ambiguous")
    row = rows[0]
    return _FinanceState(
        financing_mode=str(_row_value(row, "financing_mode", 1)),
        current_cash_krw=_daily_closing_amount(_row_value(row, "current_cash_krw", 2)),
        receivables_krw=_daily_closing_amount(_row_value(row, "receivables_krw", 3)),
        current_debt_krw=_daily_closing_amount(_row_value(row, "current_debt_krw", 4)),
    )


def _sum_receivables_issued(conn: Any, *, sim_run_id: str, as_of: date) -> Decimal:
    return _sum_query(
        conn,
        """
        SELECT COALESCE(SUM(original_amount_krw), 0) AS amount
        FROM {schema}.receivables
        WHERE sim_run_id = %s AND issued_date = %s
        """,
        [sim_run_id, as_of],
    )


def _sum_receivables_outstanding(conn: Any, *, sim_run_id: str, as_of: date) -> Decimal:
    return _sum_query(
        conn,
        """
        SELECT COALESCE(SUM(outstanding_amount_krw), 0) AS amount
        FROM {schema}.receivables
        WHERE sim_run_id = %s AND issued_date <= %s
        """,
        [sim_run_id, as_of],
    )


def _purchase_cash_out(conn: Any, *, sim_run_id: str, as_of: date) -> Decimal:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT due_date, outstanding_amount_krw
                FROM {}.payables
                WHERE sim_run_id = %s
                  AND issued_date <= %s
                  AND due_date <= %s
                  AND status IN ('OPEN', 'PARTIAL')
                """
            ).format(schema),
            [sim_run_id, as_of, as_of],
        )
        rows = cursor.fetchall()
    total = _ZERO
    for row in rows:
        due_date = _row_value(row, "due_date", 0)
        if not isinstance(due_date, date):
            raise FinanceDataNotReady("payable_due_date")
        if effective_cash_date(due_date) == as_of:
            total += _daily_closing_amount(_row_value(row, "outstanding_amount_krw", 1))
    return total


def _expense_cash_out(conn: Any, *, sim_run_id: str, as_of: date) -> tuple[Decimal, Decimal]:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT expense_category, related_delivery_id, amount_krw
                FROM {}.expenses
                WHERE sim_run_id = %s
                  AND expense_date = %s
                  AND status = 'PAID'
                """
            ).format(schema),
            [sim_run_id, as_of],
        )
        rows = cursor.fetchall()
    logistics = _ZERO
    payroll_interest = _ZERO
    for row in rows:
        category = _row_value(row, "expense_category", 0)
        delivery_id = _row_value(row, "related_delivery_id", 1)
        amount = _daily_closing_amount(_row_value(row, "amount_krw", 2))
        if delivery_id is not None:
            logistics += amount
        elif category in _PAYROLL_INTEREST_CATEGORIES:
            payroll_interest += amount
        else:
            raise FinanceDataNotReady("daily_closing_expense_category")
    return logistics, payroll_interest


def _sales_recognized(conn: Any, *, sim_run_id: str, as_of: date) -> Decimal:
    return _sum_query(
        conn,
        """
        SELECT COALESCE(SUM(total_amount_krw), 0) AS amount
        FROM {schema}.sales
        WHERE sim_run_id = %s
          AND sale_date = %s
          AND order_status IN ('CONFIRMED', 'DELIVERED')
        """,
        [sim_run_id, as_of],
    )


def _sum_query(conn: Any, query: str, params: list[object]) -> Decimal:
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL(query).format(schema=sql.Identifier(get_db_schema())), params)
        row = cursor.fetchone()
    if row is None:
        raise FinanceDataNotReady("daily_closing_ledger")
    return _daily_closing_amount(_row_value(row, "amount", 0))


def _collection_delta(
    *, prior_receivables: Decimal, issued_receivables: Decimal, current_receivables: Decimal
) -> Decimal:
    collected = prior_receivables + issued_receivables - current_receivables
    if collected < 0:
        raise FinanceDataNotReady("collection_balance_mismatch")
    return collected


def _loan_execution(current: _FinanceState, prior: _FinanceState | None) -> Decimal:
    prior_debt = prior.current_debt_krw if prior is not None else _ZERO
    return max(current.current_debt_krw - prior_debt, _ZERO)


def _upsert_daily_closing(
    conn: Any, *, as_of: date, sim_run_id: str, facts: _ClosingFacts
) -> int:
    schema = sql.Identifier(get_db_schema())
    params = {
        "sim_run_id": sim_run_id,
        "close_date": as_of,
        "day_no": facts.day_no,
        "purchase_cash_out_krw": facts.purchase_cash_out_krw,
        "logistics_cash_out_krw": facts.logistics_cash_out_krw,
        "payroll_interest_cash_out_krw": facts.payroll_interest_cash_out_krw,
        "sales_recognized_krw": facts.sales_recognized_krw,
        "collection_cash_in_krw": facts.collection_cash_in_krw,
        "base_net_cash_krw": facts.base_net_cash_krw,
        "base_cash_balance_krw": facts.base_cash_balance_krw,
        "loan_execution_krw": facts.loan_execution_krw,
        "loan_cash_balance_krw": facts.loan_cash_balance_krw,
        "receivables_balance_krw": facts.receivables_balance_krw,
        "inventory_qty_kg": facts.inventory_qty_kg,
        "accounting_inventory_cost_krw": facts.accounting_inventory_cost_krw,
    }
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.daily_closings (
                    sim_run_id, close_date, day_no,
                    purchase_cash_out_krw, logistics_cash_out_krw,
                    payroll_interest_cash_out_krw, sales_recognized_krw,
                    collection_cash_in_krw, base_net_cash_krw, base_cash_balance_krw,
                    loan_execution_krw, loan_cash_balance_krw, receivables_balance_krw,
                    inventory_qty_kg, accounting_inventory_cost_krw, closed
                ) VALUES (
                    %(sim_run_id)s, %(close_date)s, %(day_no)s,
                    %(purchase_cash_out_krw)s, %(logistics_cash_out_krw)s,
                    %(payroll_interest_cash_out_krw)s, %(sales_recognized_krw)s,
                    %(collection_cash_in_krw)s, %(base_net_cash_krw)s,
                    %(base_cash_balance_krw)s, %(loan_execution_krw)s,
                    %(loan_cash_balance_krw)s, %(receivables_balance_krw)s,
                    %(inventory_qty_kg)s, %(accounting_inventory_cost_krw)s, TRUE
                ) ON CONFLICT (sim_run_id, close_date) DO NOTHING
                """
            ).format(schema=schema),
            params,
        )
        if cursor.rowcount:
            return 1
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.daily_closings
                SET day_no = %(day_no)s,
                    purchase_cash_out_krw = %(purchase_cash_out_krw)s,
                    logistics_cash_out_krw = %(logistics_cash_out_krw)s,
                    payroll_interest_cash_out_krw = %(payroll_interest_cash_out_krw)s,
                    sales_recognized_krw = %(sales_recognized_krw)s,
                    collection_cash_in_krw = %(collection_cash_in_krw)s,
                    base_net_cash_krw = %(base_net_cash_krw)s,
                    base_cash_balance_krw = %(base_cash_balance_krw)s,
                    loan_execution_krw = %(loan_execution_krw)s,
                    loan_cash_balance_krw = %(loan_cash_balance_krw)s,
                    receivables_balance_krw = %(receivables_balance_krw)s,
                    inventory_qty_kg = %(inventory_qty_kg)s,
                    accounting_inventory_cost_krw = %(accounting_inventory_cost_krw)s,
                    closed = TRUE
                WHERE sim_run_id = %(sim_run_id)s AND close_date = %(close_date)s
                """
            ).format(schema=schema),
            params,
        )
        if cursor.rowcount != 1:
            raise FinanceDataNotReady("daily_closing_write")
    return 0


def _row_value(row: object, name: str, index: int) -> object:
    if isinstance(row, dict):
        return row[name]
    return row[index]  # type: ignore[index]


def _daily_closing_amount(value: object) -> Decimal:
    try:
        money = decimal_value(value)
    except Exception as exc:  # pragma: no cover - exact exception depends on DB adapter.
        raise FinanceDataNotReady("daily_closing_ledger") from exc
    if not money.is_finite() or money < 0:
        raise FinanceDataNotReady("daily_closing_ledger")
    return money
