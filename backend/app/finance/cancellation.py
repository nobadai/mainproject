"""Finance-owned cancellation of unpaid purchase obligations.

Cancellation is a ledger event, not a negative approval and not a payment. The caller owns
the cross-domain transaction and supplies the connection, purchase IDs, cancellation date,
and target Finance-state date. Finance locks and validates the complete Payable set before it
changes anything, then reverses only the amount actually changed from ``OPEN`` to
``CANCELLED``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.finance.db import FinanceDataNotReady, get_db_schema
from app.finance.state_identity import daily_finance_state_id

__all__ = [
    "FinanceCancellationAdapter",
    "FinanceCancellationConflict",
    "FinanceCancellationResult",
    "cancel_finance_payables",
]

CANCELLATION_STATE_TYPE = "H1_CANCELLATION"


class FinanceCancellationConflict(RuntimeError):
    """The requested reversal conflicts with persisted Finance ledger facts."""

    def __init__(self, reason: str, *, purchase_ids: Sequence[str] = ()) -> None:
        self.reason = reason
        self.purchase_ids = tuple(purchase_ids)
        super().__init__(reason)


@dataclass(frozen=True)
class FinanceCancellationResult:
    """Structured facts returned by one cancellation attempt."""

    requested_count: int
    newly_cancelled_count: int
    newly_cancelled_amount_krw: Decimal
    finance_state_updated: bool
    finance_state_id: str | None


@dataclass(frozen=True)
class _PayableFact:
    purchase_id: str
    sim_run_id: str
    original_amount_krw: Decimal
    paid_amount_krw: Decimal
    cancelled_amount_krw: Decimal
    outstanding_amount_krw: Decimal
    status: str


@dataclass(frozen=True)
class _StateFact:
    finance_state_id: str
    financing_mode: str
    unsettled_purchase_payables_krw: Decimal


def cancel_finance_payables(
    conn: Any,
    *,
    purchase_ids: Sequence[str],
    as_of: date,
    target_state_date: date,
) -> FinanceCancellationResult:
    """Cancel unpaid Payables and reverse their daily-state obligation exactly once.

    The operation deliberately has no ``build`` phase: eligibility and the reversible amount
    are persisted Payable facts and must be read under row locks. It opens no connection and
    never commits, rolls back, or closes the supplied connection.
    """
    requested = _normalize_purchase_ids(purchase_ids)
    if target_state_date <= as_of:
        raise ValueError("target_state_date must be after the cancellation as_of")

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(_locked_payables_query(schema), {"purchase_ids": list(requested)})
        payable_rows = cursor.fetchall()
        payables = tuple(_payable_fact(row) for row in payable_rows)
        _validate_complete_target_set(requested, payables)

        eligible = tuple(row for row in payables if row.status == "OPEN")
        if not eligible:
            return FinanceCancellationResult(
                requested_count=len(requested),
                newly_cancelled_count=0,
                newly_cancelled_amount_krw=Decimal(0),
                finance_state_updated=False,
                finance_state_id=None,
            )

        sim_run_id = payables[0].sim_run_id
        target_state = _one_state(
            cursor,
            schema=schema,
            sim_run_id=sim_run_id,
            state_date=target_state_date,
            lock="UPDATE",
        )
        source_state = None
        if target_state is None:
            source_state = _one_state(
                cursor,
                schema=schema,
                sim_run_id=sim_run_id,
                state_date=as_of,
                lock="UPDATE",
            )
            if source_state is None:
                raise FinanceDataNotReady("historical_finance_position")

        expected_amount = sum((row.outstanding_amount_krw for row in eligible), start=Decimal(0))
        base_state = target_state or source_state
        assert base_state is not None
        if base_state.unsettled_purchase_payables_krw < expected_amount:
            raise FinanceCancellationConflict("finance_unsettled_underflow")

        eligible_ids = tuple(row.purchase_id for row in eligible)
        cursor.execute(
            _cancel_payables_query(schema),
            {
                "purchase_ids": list(eligible_ids),
                "cancelled_date": as_of,
            },
        )
        changed_rows = cursor.fetchall()
        changed = tuple(
            (
                str(_value(row, "purchase_id", 0)),
                Decimal(str(_value(row, "cancelled_amount_krw", 1))),
            )
            for row in changed_rows
        )
        if {purchase_id for purchase_id, _ in changed} != set(eligible_ids):
            # Rows were locked and validated above. A mismatch is a ledger race or contract
            # violation, never a partial success; the caller must roll the transaction back.
            raise FinanceCancellationConflict("payable_cancellation_race")

        newly_cancelled_amount = sum((amount for _, amount in changed), start=Decimal(0))
        if newly_cancelled_amount != expected_amount:
            raise FinanceCancellationConflict("payable_cancellation_amount_mismatch")

        if target_state is not None:
            finance_state_id = _subtract_existing_state(
                cursor,
                schema=schema,
                state=target_state,
                cancelled_amount=newly_cancelled_amount,
            )
        else:
            assert source_state is not None
            finance_state_id = _carry_and_subtract_state(
                cursor,
                schema=schema,
                source=source_state,
                sim_run_id=sim_run_id,
                as_of=as_of,
                target_state_date=target_state_date,
                cancelled_amount=newly_cancelled_amount,
            )

    return FinanceCancellationResult(
        requested_count=len(requested),
        newly_cancelled_count=len(changed),
        newly_cancelled_amount_krw=newly_cancelled_amount,
        finance_state_updated=True,
        finance_state_id=finance_state_id,
    )


def _normalize_purchase_ids(purchase_ids: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for purchase_id in purchase_ids:
        if not isinstance(purchase_id, str) or not purchase_id.strip():
            raise ValueError("purchase_ids must contain non-blank strings")
        if purchase_id not in seen:
            normalized.append(purchase_id)
            seen.add(purchase_id)
    if not normalized:
        raise ValueError("purchase_ids must not be empty")
    return tuple(normalized)


def _payable_fact(row: Any) -> _PayableFact:
    return _PayableFact(
        purchase_id=str(_value(row, "purchase_id", 0)),
        sim_run_id=str(_value(row, "sim_run_id", 1)),
        original_amount_krw=Decimal(str(_value(row, "original_amount_krw", 2))),
        paid_amount_krw=Decimal(str(_value(row, "paid_amount_krw", 3))),
        cancelled_amount_krw=Decimal(str(_value(row, "cancelled_amount_krw", 4))),
        outstanding_amount_krw=Decimal(str(_value(row, "outstanding_amount_krw", 5))),
        status=str(_value(row, "status", 6)),
    )


def _validate_complete_target_set(
    requested: tuple[str, ...], payables: tuple[_PayableFact, ...]
) -> None:
    found = {row.purchase_id for row in payables}
    missing = tuple(purchase_id for purchase_id in requested if purchase_id not in found)
    if missing:
        raise FinanceCancellationConflict("payable_not_found", purchase_ids=missing)
    if len(found) != len(payables):
        raise FinanceCancellationConflict("payable_target_ambiguous")
    if len({row.sim_run_id for row in payables}) != 1:
        raise FinanceCancellationConflict("payable_runtime_axis_ambiguous")

    blocked: list[str] = []
    for row in payables:
        if row.status == "OPEN":
            if row.paid_amount_krw != 0 or row.cancelled_amount_krw != 0:
                blocked.append(row.purchase_id)
            continue
        if row.status == "CANCELLED":
            if (
                row.paid_amount_krw != 0
                or row.outstanding_amount_krw != 0
                or row.cancelled_amount_krw != row.original_amount_krw
            ):
                blocked.append(row.purchase_id)
            continue
        blocked.append(row.purchase_id)
    if blocked:
        raise FinanceCancellationConflict("payable_not_cancellable", purchase_ids=tuple(blocked))
    statuses = {row.status for row in payables}
    if statuses == {"OPEN", "CANCELLED"}:
        # This operation changes the complete locked set atomically, so its legitimate retry
        # states are all OPEN (not applied) or all CANCELLED (already applied). Without a
        # stable Master cancellation-event ID, a mixed set cannot be proven to be this
        # operation's partial retry and must not be silently completed.
        raise FinanceCancellationConflict("payable_cancellation_state_mixed")


def _one_state(
    cursor: Any,
    *,
    schema: sql.Identifier,
    sim_run_id: str,
    state_date: date,
    lock: str,
) -> _StateFact | None:
    cursor.execute(
        _exact_state_query(schema, lock=lock),
        {"sim_run_id": sim_run_id, "state_date": state_date},
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise FinanceCancellationConflict("finance_runtime_axis_ambiguous")
    if not rows:
        return None
    row = rows[0]
    return _StateFact(
        finance_state_id=str(_value(row, "finance_state_id", 0)),
        financing_mode=str(_value(row, "financing_mode", 1)),
        unsettled_purchase_payables_krw=Decimal(
            str(_value(row, "unsettled_purchase_payables_krw", 2))
        ),
    )


def _subtract_existing_state(
    cursor: Any,
    *,
    schema: sql.Identifier,
    state: _StateFact,
    cancelled_amount: Decimal,
) -> str:
    cursor.execute(
        sql.SQL(
            """
            UPDATE {}.finance_states
            SET unsettled_purchase_payables_krw =
                unsettled_purchase_payables_krw - %(cancelled_amount)s
            WHERE finance_state_id = %(finance_state_id)s
              AND unsettled_purchase_payables_krw >= %(cancelled_amount)s
            RETURNING finance_state_id
            """
        ).format(schema),
        {
            "finance_state_id": state.finance_state_id,
            "cancelled_amount": cancelled_amount,
        },
    )
    return _one_returned_state_id(cursor)


def _carry_and_subtract_state(
    cursor: Any,
    *,
    schema: sql.Identifier,
    source: _StateFact,
    sim_run_id: str,
    as_of: date,
    target_state_date: date,
    cancelled_amount: Decimal,
) -> str:
    target_id = daily_finance_state_id(
        sim_run_id=sim_run_id,
        financing_mode=source.financing_mode,
        state_date=target_state_date,
    )
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {schema}.finance_states AS current_state (
                finance_state_id, sim_run_id, state_date, state_type, financing_mode,
                current_cash_krw, minimum_operating_cash_krw, committed_outflows_krw,
                unsettled_purchase_payables_krw, receivables_krw,
                inventory_book_value_krw, operational_inventory_value_krw,
                current_debt_krw, recommended_loan_amount_krw, note
            )
            SELECT
                %(finance_state_id)s, source.sim_run_id, %(target_state_date)s,
                %(state_type)s, source.financing_mode,
                source.current_cash_krw, source.minimum_operating_cash_krw,
                source.committed_outflows_krw,
                source.unsettled_purchase_payables_krw - %(cancelled_amount)s,
                source.receivables_krw, source.inventory_book_value_krw,
                source.operational_inventory_value_krw, source.current_debt_krw,
                source.recommended_loan_amount_krw, %(note)s
            FROM {schema}.finance_states source
            WHERE source.finance_state_id = %(source_finance_state_id)s
              AND source.state_date = %(as_of)s
              AND source.unsettled_purchase_payables_krw >= %(cancelled_amount)s
            ON CONFLICT (sim_run_id, financing_mode, state_date) DO UPDATE SET
                unsettled_purchase_payables_krw =
                    current_state.unsettled_purchase_payables_krw - %(cancelled_amount)s
            WHERE current_state.unsettled_purchase_payables_krw >= %(cancelled_amount)s
            RETURNING finance_state_id
            """
        ).format(schema=schema),
        {
            "finance_state_id": target_id,
            "target_state_date": target_state_date,
            "state_type": CANCELLATION_STATE_TYPE,
            "cancelled_amount": cancelled_amount,
            "note": f"{as_of} 미지급 매입채무 취소 반영",
            "source_finance_state_id": source.finance_state_id,
            "as_of": as_of,
        },
    )
    return _one_returned_state_id(cursor)


def _one_returned_state_id(cursor: Any) -> str:
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise FinanceCancellationConflict("finance_unsettled_underflow")
    return str(_value(rows[0], "finance_state_id", 0))


def _value(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _locked_payables_query(schema: sql.Identifier) -> sql.Composed:
    return sql.SQL(
        """
        SELECT
            purchase_id, sim_run_id, original_amount_krw, paid_amount_krw,
            cancelled_amount_krw, outstanding_amount_krw, status
        FROM {}.payables
        WHERE purchase_id = ANY(%(purchase_ids)s)
        ORDER BY purchase_id
        FOR UPDATE
        """
    ).format(schema)


def _cancel_payables_query(schema: sql.Identifier) -> sql.Composed:
    return sql.SQL(
        """
        UPDATE {}.payables
        SET cancelled_amount_krw = outstanding_amount_krw,
            outstanding_amount_krw = 0,
            status = 'CANCELLED',
            cancelled_date = %(cancelled_date)s
        WHERE purchase_id = ANY(%(purchase_ids)s)
          AND status = 'OPEN'
          AND paid_amount_krw = 0
        RETURNING purchase_id, cancelled_amount_krw
        """
    ).format(schema)


def _exact_state_query(schema: sql.Identifier, *, lock: str) -> sql.Composed:
    if lock != "UPDATE":
        raise ValueError("unsupported Finance state lock")
    return sql.SQL(
        """
        SELECT finance_state_id, financing_mode, unsettled_purchase_payables_krw
        FROM {}.finance_states
        WHERE sim_run_id = %(sim_run_id)s
          AND state_date = %(state_date)s
        ORDER BY financing_mode
        FOR UPDATE
        """
    ).format(schema)


class FinanceCancellationAdapter:
    """Finance-owned surface for a future Master cancellation protocol."""

    def cancel(
        self,
        conn: Any,
        *,
        purchase_ids: Sequence[str],
        as_of: date,
        target_state_date: date,
    ) -> FinanceCancellationResult:
        return cancel_finance_payables(
            conn,
            purchase_ids=purchase_ids,
            as_of=as_of,
            target_state_date=target_state_date,
        )
