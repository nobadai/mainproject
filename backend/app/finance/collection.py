"""기존 receivables/finance_states를 사용하는 누적 수금 전이.

연결과 commit/rollback은 호출자가 소유한다. 이 모듈은 새 event ledger를 만들지 않고,
누적 target과 현재 누적액의 차이만 같은 트랜잭션 안에서 반영한다.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from psycopg import Connection, sql

from app.finance.db import row_value
from app.finance.db import FinanceDataNotReady, get_db_schema

FixtureEvidenceGrade = Literal["SIM_FIXED"]


class FinanceCollectionConflict(ValueError):
    """누적 수금 target 또는 기존 원장 상태가 전이 불변식을 어겼다."""


@dataclass(frozen=True)
class CollectionTransitionPlan:
    receivable_id: str
    finance_state_id: str
    target_received_total_krw: Decimal
    delta_received_krw: Decimal
    next_outstanding_amount_krw: Decimal
    next_status: str
    next_current_cash_krw: Decimal
    next_receivables_krw: Decimal


@dataclass(frozen=True)
class CollectionEvent:
    """호출자가 명시한 매출채권별 누적 수금 사실."""

    sim_run_id: str
    financing_mode: str
    collection_date: date
    receivable_id: str
    target_received_total_krw: object


@dataclass(frozen=True)
class DeterministicCollectionFixtureSource:
    """시뮬레이션 fixture가 명시한 수금 event source."""

    events: tuple[CollectionEvent, ...] = ()
    evidence_grade: FixtureEvidenceGrade = "SIM_FIXED"
    source_ref: str = "finance_collection_fixture"

    @classmethod
    def from_events(
        cls,
        events: Iterable[CollectionEvent],
        *,
        source_ref: str = "finance_collection_fixture",
    ) -> "DeterministicCollectionFixtureSource":
        return cls(events=tuple(events), source_ref=source_ref)

    def events_for_date(
        self,
        *,
        sim_run_id: str,
        financing_mode: str,
        as_of: date,
    ) -> tuple[CollectionEvent, ...]:
        """실행 축과 날짜가 정확히 일치하는 명시 event만 반환한다."""
        return tuple(
            event
            for event in self.events
            if event.sim_run_id == sim_run_id
            and event.financing_mode == financing_mode
            and event.collection_date == as_of
        )


def build_collection_transition(
    receivable: Mapping[str, object],
    finance_state: Mapping[str, object],
    *,
    target_received_total_krw: object,
) -> CollectionTransitionPlan:
    """누적 target을 delta 전이로 계산한다. DB를 변경하지 않는다."""
    target = _money(target_received_total_krw, "target_received_total_krw")
    original = _money(receivable.get("original_amount_krw"), "original_amount_krw")
    current = _money(receivable.get("received_amount_krw"), "received_amount_krw")
    outstanding = _money(receivable.get("outstanding_amount_krw"), "outstanding_amount_krw")
    if original - current != outstanding:
        raise FinanceCollectionConflict("receivable amount identity is inconsistent")
    current_status = str(receivable.get("status"))
    if current_status not in {"OPEN", "PARTIAL", "COLLECTED"}:
        raise FinanceCollectionConflict("receivable status cannot accept collection")
    if current_status == "COLLECTED" and current != original:
        raise FinanceCollectionConflict("collected receivable is not fully received")
    if target < current:
        raise FinanceCollectionConflict("cumulative collection cannot regress")
    if target > original:
        raise FinanceCollectionConflict("cumulative collection cannot exceed original amount")

    delta = target - current
    next_outstanding = original - target
    current_cash = _money(finance_state.get("current_cash_krw"), "current_cash_krw")
    receivables = _money(finance_state.get("receivables_krw"), "receivables_krw")
    if receivables < delta:
        raise FinanceCollectionConflict("finance state receivables cannot cover collection delta")
    if receivable.get("sim_run_id") != finance_state.get("sim_run_id"):
        raise FinanceCollectionConflict("receivable and finance state axes do not match")
    status = "COLLECTED" if target == original else "PARTIAL" if target > 0 else "OPEN"
    return CollectionTransitionPlan(
        receivable_id=str(receivable["receivable_id"]),
        finance_state_id=str(finance_state["finance_state_id"]),
        target_received_total_krw=target,
        delta_received_krw=delta,
        next_outstanding_amount_krw=next_outstanding,
        next_status=status,
        next_current_cash_krw=current_cash + delta,
        next_receivables_krw=receivables - delta,
    )


def apply_cumulative_collection(
    conn: Connection[dict[str, object]],
    *,
    receivable_id: str,
    finance_state_id: str,
    target_received_total_krw: object,
) -> CollectionTransitionPlan:
    """잠근 기존 두 행에 누적 수금 delta를 원자적으로 반영한다.

    같은 target을 재적용하면 delta가 0이라 UPDATE도 현금 증가도 없다.
    commit/rollback은 하지 않는다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT * FROM {}.finance_states WHERE finance_state_id = %s FOR UPDATE"
            ).format(schema),
            [finance_state_id],
        )
        finance_state = cursor.fetchone()
        if finance_state is None:
            raise LookupError(f"Finance state was not found: {finance_state_id}")
        cursor.execute(
            sql.SQL("SELECT * FROM {}.receivables WHERE receivable_id = %s FOR UPDATE").format(
                schema
            ),
            [receivable_id],
        )
        receivable = cursor.fetchone()
        if receivable is None:
            raise LookupError(f"Receivable was not found: {receivable_id}")
        plan = build_collection_transition(
            receivable,
            finance_state,
            target_received_total_krw=target_received_total_krw,
        )
        if plan.delta_received_krw == 0:
            return plan
        cursor.execute(
            sql.SQL(
                """UPDATE {}.receivables
                   SET received_amount_krw = %s, outstanding_amount_krw = %s, status = %s
                   WHERE receivable_id = %s"""
            ).format(schema),
            [
                plan.target_received_total_krw,
                plan.next_outstanding_amount_krw,
                plan.next_status,
                plan.receivable_id,
            ],
        )
        if cursor.rowcount != 1:
            raise FinanceCollectionConflict("receivable update did not affect exactly one row")
        cursor.execute(
            sql.SQL(
                """UPDATE {}.finance_states
                   SET current_cash_krw = %s, receivables_krw = %s
                   WHERE finance_state_id = %s"""
            ).format(schema),
            [
                plan.next_current_cash_krw,
                plan.next_receivables_krw,
                plan.finance_state_id,
            ],
        )
        if cursor.rowcount != 1:
            raise FinanceCollectionConflict("finance state update did not affect exactly one row")
        return plan


def apply_collection_event(
    conn: Connection[dict[str, object]],
    *,
    sim_run_id: str,
    financing_mode: str,
    collection_date: date,
    receivable_id: str,
    target_received_total_krw: object,
) -> CollectionTransitionPlan:
    """명시된 수금 사실을 이미 열린 해당 Finance 일자에만 반영한다.

    이 경계는 ``due_date`` 로 event를 추론하거나 최신 상태를 임의 선택하지 않는다.
    transaction과 실행 축은 호출자가 소유한다.
    """
    for field, value in (
        ("sim_run_id", sim_run_id),
        ("financing_mode", financing_mode),
        ("receivable_id", receivable_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-blank string")

    finance_state_id = _exact_collection_state_id(
        conn,
        sim_run_id=sim_run_id,
        financing_mode=financing_mode,
        collection_date=collection_date,
    )
    return apply_cumulative_collection(
        conn,
        receivable_id=receivable_id,
        finance_state_id=finance_state_id,
        target_received_total_krw=target_received_total_krw,
    )


def apply_explicit_collection(
    conn: Connection[dict[str, object]], event: CollectionEvent
) -> CollectionTransitionPlan:
    """명시 fixture event를 누적 수금 전이에 연결한다."""
    return apply_collection_event(
        conn,
        sim_run_id=event.sim_run_id,
        financing_mode=event.financing_mode,
        collection_date=event.collection_date,
        receivable_id=event.receivable_id,
        target_received_total_krw=event.target_received_total_krw,
    )


def _exact_collection_state_id(
    conn: Connection[dict[str, object]],
    *,
    sim_run_id: str,
    financing_mode: str,
    collection_date: date,
) -> str:
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT finance_state_id
                FROM {}.finance_states
                WHERE sim_run_id = %(sim_run_id)s
                  AND financing_mode = %(financing_mode)s
                  AND state_date = %(collection_date)s
                FOR UPDATE
                """
            ).format(schema),
            {
                "sim_run_id": sim_run_id,
                "financing_mode": financing_mode,
                "collection_date": collection_date,
            },
        )
        rows = cursor.fetchall()
    if not rows:
        raise FinanceDataNotReady("historical_finance_position")
    if len(rows) != 1:
        raise FinanceDataNotReady("finance_state_ambiguous")
    return str(row_value(rows[0], "finance_state_id", 0))


def _money(value: object, field: str) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        raise FinanceCollectionConflict(f"{field} is not an exact monetary value")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FinanceCollectionConflict(f"{field} is not an exact monetary value") from exc
    if not amount.is_finite() or amount < 0:
        raise FinanceCollectionConflict(f"{field} must be a non-negative finite amount")
    return amount
