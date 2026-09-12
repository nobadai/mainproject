"""`logistics_exceptions` 한 표만 읽고 쓴다. **업무 판단이 여기 없다.**

```text
읽기   live_exceptions          살아 있는(OPEN·PROPOSED) 행
       previous_exception_id_for 같은 축의 가장 최근 닫힌 행 — 재발을 잇는 고리
쓰기   open_exception           INSERT (status=OPEN)
       touch_exception          UPDATE evidence · severity · last_detected · observed
       resolve_exception        UPDATE status=RESOLVED
```

🔴 **커밋도 롤백도 안 한다.** 트랜잭션의 주인은 부르는 쪽이다
   (`master/inspection.py` — `auto_maintenance` ↔ `master/maintenance.py` 와 같은 나눔).

🔴 **상태를 여기서 정하지 않는다.** 무엇을 열고 무엇을 닫을지는 `detect.py` 가 정하고,
   이 파일은 그 결정을 표에 옮기기만 한다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from psycopg import sql

from app.logistics.agent.schemas import (
    LIVE_STATUSES,
    ExceptionEvidence,
    ExceptionRow,
)
from app.logistics.db import get_db_schema

__all__ = [
    "EmptyEvidence",
    "exception_id_for",
    "live_exceptions",
    "open_exception",
    "previous_exception_id_for",
    "resolve_exception",
    "touch_exception",
]

_COLUMNS = (
    "exception_id",
    "sim_run_id",
    "code",
    "subject_type",
    "subject_id",
    "severity",
    "status",
    "opened_as_of",
    "last_detected_as_of",
    "observed_as_of",
    "resolved_as_of",
    "resolved_by",
    "risk_accepted_as_of",
    "evidence_json",
    "detector_version",
    "previous_exception_id",
    "note",
)


class EmptyEvidence(ValueError):
    """근거 0 건으로 Exception 을 만들려 했다. 🔴 **DB CHECK 보다 먼저 막는다.**

    ★ 제약이 이미 막지만 여기서 한 번 더 막는 이유는 **어느 탐지기가** 근거를 안
      냈는지를 사유에 적기 위해서다 — DB 오류 문자열에는 그것이 없다.
    """


def _schema() -> sql.Identifier:
    return sql.Identifier(get_db_schema())


def _rows(conn: Any, query: sql.Composed, params: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _row(raw: dict[str, Any]) -> ExceptionRow:
    근거 = raw["evidence_json"] or []
    if isinstance(근거, str):  # jsonb 를 문자열로 돌려주는 드라이버 설정 대비
        근거 = json.loads(근거)
    return ExceptionRow(
        exception_id=raw["exception_id"],
        sim_run_id=raw["sim_run_id"],
        code=raw["code"],
        subject_type=raw["subject_type"],
        subject_id=raw["subject_id"],
        severity=raw["severity"],
        status=raw["status"],
        opened_as_of=raw["opened_as_of"],
        last_detected_as_of=raw["last_detected_as_of"],
        observed_as_of=raw["observed_as_of"],
        evidence=tuple(ExceptionEvidence.from_json(one) for one in 근거),
        detector_version=raw["detector_version"],
        resolved_as_of=raw["resolved_as_of"],
        resolved_by=raw["resolved_by"],
        risk_accepted_as_of=raw["risk_accepted_as_of"],
        previous_exception_id=raw["previous_exception_id"],
        note=raw["note"],
    )


def live_exceptions(conn: Any, *, sim_run_id: str) -> tuple[ExceptionRow, ...]:
    """지금 살아 있는 Exception 전부. **축은 실행 하나다.**

    🔴 **`as_of` 로 자르지 않는다.** Exception 은 열린 뒤 닫힐 때까지 계속 살아 있는
       상태이지 그날의 사건이 아니다 — 어제 열린 행을 오늘 못 보면 같은 문제로 새 행을
       또 만든다.
    """
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT {columns}
            FROM {schema}.logistics_exceptions
            WHERE sim_run_id = %(sim)s
              AND status = ANY(%(live)s)
            ORDER BY exception_id
            """
        ).format(
            columns=sql.SQL(", ").join(sql.Identifier(name) for name in _COLUMNS),
            schema=_schema(),
        ),
        {"sim": sim_run_id, "live": list(LIVE_STATUSES)},
    )
    return tuple(_row(raw) for raw in rows)


def previous_exception_id_for(
    conn: Any, *, sim_run_id: str, code: str, subject_type: str, subject_id: str
) -> str | None:
    """같은 축에서 **가장 최근에 닫힌** Exception 의 ID. 없으면 `None`.

    ★ 재발을 새 행으로 열되 **이전 행을 가리킨다** (§6.3). 재오픈하지 않는 이유는
      닫힌 날과 다시 열린 날이 한 행에 겹치면 *"며칠째"* 를 셀 수 없어서다.

    ⚠️ `DISMISSED` 도 대상이다 — 사람이 한 번 덮은 문제가 다시 나왔다는 것은 이어서
       보여야 할 사실이다.
    """
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT exception_id
            FROM {schema}.logistics_exceptions
            WHERE sim_run_id = %(sim)s
              AND code = %(code)s
              AND subject_type = %(subject_type)s
              AND subject_id = %(subject_id)s
              AND status NOT IN ('OPEN', 'PROPOSED')
            ORDER BY COALESCE(resolved_as_of, last_detected_as_of) DESC, exception_id DESC
            LIMIT 1
            """
        ).format(schema=_schema()),
        {
            "sim": sim_run_id,
            "code": code,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
    )
    return rows[0]["exception_id"] if rows else None


def exception_id_for(
    conn: Any, *, sim_run_id: str, code: str, subject_id: str, opened_as_of: date
) -> str:
    """`EX-{실행}-{코드}-{대상}-{연월일}`. **업무 키다 — 같은 날 같은 대상은 하나.**

    ⚠️ 그런데 하루 안에 닫고 다시 여는 일이 생기면 같은 이름이 두 번 필요하다.
       production 흐름에서는 안 난다(닫는 판과 다시 잡는 판이 같은 탐지라 서로 배타다)
       — 그래도 **이름이 겹치면 조용히 덮는 대신 뒤에 번호를 붙인다.** PK 충돌로
       하루가 터지는 것보다 낫고, 번호가 붙었다는 것 자체가 그날의 이상 신호다.
    """
    바탕 = f"EX-{sim_run_id}-{code}-{subject_id}-{opened_as_of:%Y%m%d}"
    후보 = 바탕
    번호 = 1
    while _exists(conn, exception_id=후보):
        번호 += 1
        후보 = f"{바탕}-{번호}"
    return 후보


def _exists(conn: Any, *, exception_id: str) -> bool:
    rows = _rows(
        conn,
        sql.SQL(
            "SELECT 1 AS 있음 FROM {schema}.logistics_exceptions WHERE exception_id = %s"
        ).format(schema=_schema()),
        [exception_id],
    )
    return bool(rows)


def open_exception(conn: Any, *, row: ExceptionRow) -> ExceptionRow:
    """새 Exception 한 행. 🔴 **근거가 비면 만들지 않는다.**"""
    if not row.evidence:
        raise EmptyEvidence(
            f"{row.code}/{row.subject_id} 에 근거가 하나도 없다 —"
            " 근거 없는 Exception 은 «판단» 이 아니라 «추측» 이다"
        )
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.logistics_exceptions (
                    exception_id, sim_run_id, code, subject_type, subject_id,
                    severity, status, opened_as_of, last_detected_as_of, observed_as_of,
                    evidence_json, detector_version, previous_exception_id, note
                ) VALUES (
                    %(exception_id)s, %(sim)s, %(code)s, %(subject_type)s, %(subject_id)s,
                    %(severity)s, %(status)s, %(opened)s, %(detected)s, %(observed)s,
                    %(evidence)s::jsonb, %(detector_version)s, %(previous)s, %(note)s
                )
                """
            ).format(schema=_schema()),
            {
                "exception_id": row.exception_id,
                "sim": row.sim_run_id,
                "code": row.code,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "severity": row.severity,
                "status": row.status,
                "opened": row.opened_as_of,
                "detected": row.last_detected_as_of,
                "observed": row.observed_as_of,
                "evidence": _evidence_json(row.evidence),
                "detector_version": row.detector_version,
                "previous": row.previous_exception_id,
                "note": row.note,
            },
        )
    return row


def touch_exception(
    conn: Any,
    *,
    exception_id: str,
    severity: str,
    evidence: Sequence[ExceptionEvidence],
    last_detected_as_of: date,
    observed_as_of: date | None,
) -> None:
    """같은 문제가 **오늘도** 참이다. 🔴 **`status` 와 `opened_as_of` 는 안 건드린다.**

    ★ 그 둘이 불변인 것이 *"며칠째"* 의 근거다. 매일 새 행을 만들면 그 수가 사라지고,
      `status` 를 되돌리면 조사·제안이 붙은 문제가 조용히 처음으로 돌아간다.
    """
    if not evidence:
        raise EmptyEvidence(f"{exception_id} 를 근거 없이 갱신할 수 없다")
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_exceptions
                   SET severity = %(severity)s,
                       evidence_json = %(evidence)s::jsonb,
                       last_detected_as_of = %(detected)s,
                       observed_as_of = %(observed)s,
                       updated_at = now()
                 WHERE exception_id = %(exception_id)s
                """
            ).format(schema=_schema()),
            {
                "severity": severity,
                "evidence": _evidence_json(evidence),
                "detected": last_detected_as_of,
                "observed": observed_as_of,
                "exception_id": exception_id,
            },
        )


def resolve_exception(
    conn: Any,
    *,
    exception_id: str,
    as_of: date,
    resolved_by: str,
    note: str | None = None,
) -> None:
    """조건이 사라졌다. **결정론이 닫는다 — 사람 확인을 기다리지 않는다.**

    :param resolved_by: 무엇이 닫았나 (`REDETECT` · `LOT_EMPTY` · `COMMITTED` ·
        `ESCALATED:FRESHNESS_EXPIRED`). 🔴 **사람 이름을 지어내지 않는다.**
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {schema}.logistics_exceptions
                   SET status = 'RESOLVED',
                       resolved_as_of = %(as_of)s,
                       resolved_by = %(resolved_by)s,
                       note = COALESCE(%(note)s, note),
                       updated_at = now()
                 WHERE exception_id = %(exception_id)s
                   AND status = ANY(%(live)s)
                """
            ).format(schema=_schema()),
            {
                "as_of": as_of,
                "resolved_by": resolved_by,
                "note": note,
                "exception_id": exception_id,
                "live": list(LIVE_STATUSES),
            },
        )


def _evidence_json(evidence: Sequence[ExceptionEvidence]) -> str:
    return json.dumps([one.as_json() for one in evidence], ensure_ascii=False)
