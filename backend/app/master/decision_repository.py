"""`master_decisions` 적재·조회.

★ `run_repository.try_save_run` 과 달리 **실패를 삼키지 않는다.**
  실행 이력은 없어도 결과를 줄 수 있지만, 결정은 안 남으면 승인이 없었던 것과 같다.
  적재가 실패하면 사용자에게 실패를 알려야 한다.

★ UPDATE·DELETE 가 없다. 번복은 `decision_seq` 를 올린 새 행이다.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from psycopg import sql

from app.finance.db import execute_returning_one, fetch_all, fetch_one, get_db_schema
from app.master.decision import Decision, DecisionOut, mark_current

_TABLE = "master_decisions"
_COLUMNS = (
    "decision_id",
    "request_id",
    "decision_seq",
    "decision",
    "scenario_label",
    "condition_text",
    "decided_by",
    "follow_up_request_id",
    "end_code_at_decision",
    "note",
    "created_at",
)


def _columns() -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS)


def _row_to_out(row: dict[str, Any]) -> DecisionOut:
    return DecisionOut(
        decision_id=cast(UUID, row["decision_id"]),
        request_id=row["request_id"],
        decision_seq=row["decision_seq"],
        decision=cast(Decision, row["decision"]),
        scenario_label=row.get("scenario_label"),
        condition_text=row.get("condition_text"),
        decided_by=row["decided_by"],
        follow_up_request_id=row.get("follow_up_request_id"),
        end_code_at_decision=row["end_code_at_decision"],
        note=row.get("note"),
        created_at=row["created_at"],
    )


def list_decisions(request_id: str) -> list[DecisionOut]:
    """한 요청에 붙은 결정 전부. 오래된 것부터.

    최신 하나만 `is_current=True` 로 표시된다 — 이력은 지우지 않고 접는다.
    """
    query = sql.SQL("SELECT {} FROM {}.{} WHERE request_id = %s ORDER BY decision_seq ASC").format(
        _columns(),
        sql.Identifier(get_db_schema()),
        sql.Identifier(_TABLE),
    )
    rows = fetch_all(query, (request_id,))
    return mark_current([_row_to_out(dict(row)) for row in rows])


def save_decision(
    *,
    request_id: str,
    decision_seq: int,
    decision: Decision,
    decided_by: str,
    end_code_at_decision: str,
    scenario_label: str | None = None,
    condition_text: str | None = None,
    follow_up_request_id: str | None = None,
    note: str | None = None,
) -> DecisionOut:
    """결정 1건을 적재한다.

    ★ `UNIQUE (request_id, decision_seq)` 가 동시 결정을 막는다. 두 사람이 같은 회차로
      동시에 밀면 뒤엣것이 `UniqueViolation` 으로 떨어진다 — **조용히 덮어쓰지 않는다.**
      호출자가 회차를 다시 읽어 재시도할지 정한다.
    """
    query = sql.SQL(
        """
        INSERT INTO {}.{} (
            decision_id, request_id, decision_seq, decision, scenario_label,
            condition_text, decided_by, follow_up_request_id, end_code_at_decision, note
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {}
        """
    ).format(
        sql.Identifier(get_db_schema()),
        sql.Identifier(_TABLE),
        _columns(),
    )
    row = execute_returning_one(
        query,
        (
            uuid4(),
            request_id,
            decision_seq,
            decision,
            scenario_label,
            condition_text,
            decided_by,
            follow_up_request_id,
            end_code_at_decision,
            note,
        ),
    )
    out = _row_to_out(dict(row))
    return out.model_copy(update={"is_current": True})


def link_follow_up(*, decision_id: UUID, follow_up_request_id: str) -> bool:
    """조건부 재요청이 실제로 실행됐을 때 후속 키를 잇는다.

    ⚠️ **이 한 곳만 UPDATE 한다.** 결정 내용(무엇을 골랐나)은 안 건드리고 NULL 이던
      후속 링크만 채운다. `IS NULL` 조건이 **한 번만** 채워지게 한다 — 이미 이어진
      결정에 다른 실행을 덧붙이면 체인이 갈라진다.

    :return: 실제로 이었으면 True. 이미 이어져 있었으면 False — **예외가 아니다.**
      호출자가 "이 결정은 이미 후속이 있다"를 판단할 몫이다.
    """
    query = sql.SQL(
        """
        UPDATE {}.{}
           SET follow_up_request_id = %s
         WHERE decision_id = %s
           AND follow_up_request_id IS NULL
        RETURNING decision_id
        """
    ).format(
        sql.Identifier(get_db_schema()),
        sql.Identifier(_TABLE),
    )
    return fetch_one(query, (follow_up_request_id, decision_id)) is not None
