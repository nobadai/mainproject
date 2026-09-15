"""`master_purchase_records` 적재·조회 (설계 260915 안 A §4-1).

★ **사람이 적은 실매입 원문**이다. 원장(`purchases` · `payables` · `inbound_schedules`)
  에 무엇이 앉는지는 여기서 모른다 — 그것은 `transition.apply_approval` 이 이 값으로
  덮은 약정을 받아 정한다.

★ UPDATE · DELETE 가 없다. **한 승인에 한 번**이고(PK), 다시 적으면 PK 가 막는다.

🔴 **PK 에 실행 축이 있다** — `(sim_run_id, request_id, decision_seq, leg_seq)`
   (9/15 재무 회신 ③). 그래서 조회도 축으로 좁힌다. 다른 실행의 같은 업무 키는
   별개 기록이다.

🔴 **적재는 부르는 쪽 커넥션으로 하고 커밋하지 않는다.** 기록과 전이가 한 커밋으로
   묶여야 하기 때문이다 (`purchase_record.record_purchase`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from psycopg import sql

from app.finance.db import fetch_all, get_db_schema
from app.master.commitment import RecordedLeg

__all__ = [
    "insert_purchase_record_legs",
    "last_closed_date",
    "list_purchase_record_legs",
    "recorded_decision_keys",
]

_TABLE = "master_purchase_records"


def _table() -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(_TABLE))


def insert_purchase_record_legs(
    conn: Any,
    *,
    sim_run_id: str,
    request_id: str,
    decision_seq: int,
    grade: str,
    recorded_by: str,
    legs: Sequence[RecordedLeg],
) -> None:
    """회차마다 한 행을 적는다. 🔴 **커밋하지 않는다.**

    ★ 이미 적힌 승인이면 PK `(sim_run_id, request_id, decision_seq, leg_seq)` 가
      `UniqueViolation` 으로 막는다 — 조용히 덮어쓰지 않는다.
    """
    query = sql.SQL(
        """
        INSERT INTO {} (
            sim_run_id, request_id, decision_seq, leg_seq,
            quantity_kg, amount_krw, purchase_date, arrival_date,
            grade, recorded_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
    ).format(_table())
    with conn.cursor() as cursor:
        for leg in legs:
            cursor.execute(
                query,
                (
                    sim_run_id,
                    request_id,
                    decision_seq,
                    leg.seq,
                    leg.qty_kg,
                    leg.amount_krw,
                    leg.purchase_date,
                    leg.arrival_date,
                    grade,
                    recorded_by,
                ),
            )


def list_purchase_record_legs(
    *, sim_run_id: str, request_id: str, decision_seq: int
) -> list[dict[str, Any]]:
    """그 실행 축 · 그 승인에 적힌 기록 행 전부. 회차 순. 없으면 **빈 목록**."""
    query = sql.SQL(
        """
        SELECT sim_run_id, request_id, decision_seq, leg_seq,
               quantity_kg, amount_krw, purchase_date, arrival_date,
               grade, recorded_by, recorded_at
        FROM {}
        WHERE sim_run_id = %s AND request_id = %s AND decision_seq = %s
        ORDER BY leg_seq
        """
    ).format(_table())
    return [dict(row) for row in fetch_all(query, (sim_run_id, request_id, decision_seq))]


def recorded_decision_keys(*, sim_run_id: str) -> list[tuple[str, int]]:
    """이 실행 축에서 **기록이 있는 승인** `(request_id, decision_seq)` 전부.

    ★ 재시도가 *"사람 승인인데 기록이 없다"* 를 거르는 데 쓴다
      (`pending_transition.pending_approvals`).
    """
    query = sql.SQL(
        "SELECT DISTINCT request_id, decision_seq FROM {} WHERE sim_run_id = %s"
    ).format(_table())
    return [
        (row["request_id"], int(row["decision_seq"])) for row in fetch_all(query, (sim_run_id,))
    ]


def last_closed_date(*, sim_run_id: str) -> date | None:
    """그 실행 축의 **마지막 재무 일마감일**. 마감이 없으면 `None`.

    ★ **읽기만 한다.** `daily_closings` 의 주인은 재무다 (`finance/closing.py` 가 적는다).
      실매입 매입일이 이미 마감된 날로 들어가지 못하게 막는 데만 쓴다
      (설계 260915 안 A §4-6 ②).
    """
    query = sql.SQL(
        "SELECT max(close_date) AS last_close FROM {}.{} WHERE sim_run_id = %s AND closed"
    ).format(sql.Identifier(get_db_schema()), sql.Identifier("daily_closings"))
    rows = fetch_all(query, (sim_run_id,))
    return rows[0]["last_close"] if rows else None
