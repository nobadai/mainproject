"""Finance operations-console payable read model; strictly run-scoped."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.finance.dashboard import load_payables

#: Payable rows the console treats as still owed.  A settled row is a fact, not a
#: gap, so it stays readable while it stops counting toward what is outstanding.
_SETTLED_STATUS = "SETTLED"


class ConsolePayableRow(BaseModel):
    payable_id: str
    #: The purchase this debt came from.  Null means the stored row has no source
    #: reference — the console reports that absence instead of inventing one.
    purchase_id: str | None
    issued_date: date
    due_date: date
    original_amount_krw: Decimal
    paid_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    #: Negative means the due date has already passed.  Zero means it is due today,
    #: which is not the same fact, so neither is folded into the other.
    days_until_due: int
    status: str


class ConsolePayableSummary(BaseModel):
    total_outstanding_krw: Decimal = Decimal(0)
    due_today_krw: Decimal = Decimal(0)
    due_next_7d_krw: Decimal = Decimal(0)
    overdue_krw: Decimal = Decimal(0)


class ConsolePayablesResponse(BaseModel):
    sim_run_id: str
    as_of: date
    summary: ConsolePayableSummary
    rows: list[ConsolePayableRow]


def get_console_payables(
    *,
    sim_run_id: str,
    as_of: date,
    status: str | None = None,
    due_within_days: int | None = None,
) -> ConsolePayablesResponse:
    """Read payables for exactly one simulation run.

    The loader is Finance's existing one (`dashboard.load_payables`); the console
    adds no second definition of what a payable is.  Filters narrow the rows the
    caller sees, and never the summary — a summary that moved with the filter
    would answer "what is owed" differently depending on what the screen asked.
    """
    summary = ConsolePayableSummary()
    rows: list[ConsolePayableRow] = []
    for raw in load_payables(sim_run_id=sim_run_id, as_of=as_of):
        outstanding = raw["outstanding_amount_krw"]
        if outstanding is None:
            raise ValueError("payables.outstanding_amount_krw must not be null")
        amount = Decimal(str(outstanding))
        due_date = raw["due_date"]
        days_until_due = (due_date - as_of).days
        row_status = str(raw["status"])
        if row_status != _SETTLED_STATUS and amount > 0:
            summary.total_outstanding_krw += amount
            if days_until_due < 0:
                summary.overdue_krw += amount
            elif days_until_due == 0:
                summary.due_today_krw += amount
            if 0 <= days_until_due <= 7:
                summary.due_next_7d_krw += amount
        if status is not None and row_status != status:
            continue
        if due_within_days is not None and days_until_due > due_within_days:
            continue
        rows.append(
            ConsolePayableRow(
                payable_id=str(raw["payable_id"]),
                purchase_id=None if raw["purchase_id"] is None else str(raw["purchase_id"]),
                issued_date=raw["issued_date"],
                due_date=due_date,
                original_amount_krw=Decimal(str(raw["original_amount_krw"])),
                paid_amount_krw=Decimal(str(raw["paid_amount_krw"])),
                outstanding_amount_krw=amount,
                days_until_due=days_until_due,
                status=row_status,
            )
        )
    return ConsolePayablesResponse(
        sim_run_id=sim_run_id, as_of=as_of, summary=summary, rows=rows
    )
