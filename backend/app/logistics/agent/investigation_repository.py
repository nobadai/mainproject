"""`logistics_investigations` 한 표만 읽고 쓴다. **조사를 돌리지 않는다.**

```text
읽기   select_investigation      한 건
       select_investigations     그 문제의 조사 이력 (오래된 것부터)
쓰기   insert_investigation      INSERT — 🔴 끝난 실행의 기록이라 UPDATE 가 없다
이름   new_investigation_id      INV-{uuid}
```

🔴 **커밋도 롤백도 안 한다.** 트랜잭션의 주인은 `investigation_service` 다
   (`proposals.py` ↔ `proposal_service.py` 와 같은 나눔).

🔴 **`run_investigation` 이 여기 없다.** 이 파일은 이미 끝난 결과를 표에 옮기기만
   한다 — 저장하다가 조사를 다시 돌리거나 Tool 을 부르는 길이 없어야 한다.

🔴 **UPDATE 가 없다.** 한 조사는 **끝난 실행**이다. 다시 조사했으면 그것은 새 실행이고
   새 `investigation_id` 다 — 결과를 덮어쓰면 *"그때 무엇으로 끝났나"* 가 사라진다.

⚠️ **`created_at` 을 `InvestigationRow` 에 안 싣는다.** 감사용 벽시계이고 업무 판단에
   쓰면 안 되는 값이라, 애초에 손에 안 잡히게 둔다 (`ProposalRow` 와 같다).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import uuid4

from psycopg import errors as pg_errors
from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "INVESTIGATION_ID_PREFIX",
    "InvestigationRow",
    "insert_investigation",
    "is_unique_violation",
    "new_investigation_id",
    "select_investigation",
    "select_investigations",
]

#: 조사 식별자의 머리. 🔴 제안(`PRP-`)·문제(`EX-`)와 한눈에 갈린다.
INVESTIGATION_ID_PREFIX = "INV-"


def new_investigation_id() -> str:
    """`INV-{uuid}`. 🔴 **DB 를 한 번도 안 본다.**

    ★ `next_proposal_id` 와 **일부러 다른 방식**이다. 저쪽은 *"이 문제의 몇 번째
      대응안인가"* 가 사람에게 의미 있는 번호라 세어서 짓지만, 조사는 그런 서수가
      업무적으로 아무것도 안 뜻한다 — 그러면서 `COUNT(*)+1` · `MAX(n)+1` 은 두 조사가
      동시에 시작하면 **같은 이름**을 낸다. 조사는 사람이 부를 때마다 서므로 그 경합이
      실제로 열려 있다.

    ⚠️ 값을 밖에서 주입하는 길을 여기 안 둔다 — 필요하면 서비스가 받은 값을 그대로
       쓴다 (저장 재시도).
    """
    return f"{INVESTIGATION_ID_PREFIX}{uuid4()}"


def is_unique_violation(error: BaseException) -> bool:
    """이 실패가 **«누가 먼저 같은 자리를 잡았다»** 인가.

    🔴 **이 값이 업무 답을 정하지 않는다.** 참이라는 것은 *"다시 읽어 봐야 한다"* 는
       뜻일 뿐이다 — 같은 저장을 두 번 시도했을 수도, **다른 내용**이 이미 그 ID 로 서
       있을 수도 있고, 그 둘은 전혀 다른 답이다
       (`investigation_service._settle_after_race`).

    ★ 드라이버 예외를 아는 자리를 저장소 안에 가둔다 — 서비스가 `psycopg` 를 직접 알게
      되면 «SQL 은 저장소에만» 이라는 나눔이 조용히 새어 나간다.
    ⚠️ `proposals.is_unique_violation` 에 **같은 한 줄**이 있다. 그 한 줄을 나누자고 저장소
       둘을 서로 임포트시키면 이유 없는 의존 고리가 생기고, 술어 하나를 위해 공통 모듈을
       세우는 것도 과하다 — 둘은 psycopg 를 이미 아는 **같은 층**이다.
    """
    return isinstance(error, pg_errors.UniqueViolation)


# ══════════════════════════════════════════════════════════════════════════
#  행 — 칸 이름이 DB 와 같다
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, kw_only=True)
class InvestigationRow:
    """`logistics_investigations` 한 행. **조사 한 번의 끝난 모습이다.**

    🔴 **상태 칸도 결정 칸도 없다.** 이 행은 «지금 어떤가» 가 아니라 «그때 무엇이
       일어났나» 를 든다 — 그래서 갱신할 것도, 과거로 접을 것도 없다.
    """

    investigation_id: str
    sim_run_id: str
    exception_id: str
    #: 시뮬레이션 영업일. 🔴 조사 Runtime 이 받은 값 그대로다.
    as_of: date
    #: 🔴 그래프가 **어디서** 끝났나. `llm_status` 와 **다른 축**이다 (§41).
    finish_reason: str
    #: AI 가 **실제로** 판단했나.
    llm_status: str
    #: 공급자 전송이 최종 실패한 원인. 안 실패했으면 `None` 이다.
    llm_error_kind: str | None = None
    #: 🔴 조사가 낸 값 그대로. `None` 이면 `None` 이다 — `as_of` 로 안 메운다.
    observed_as_of: date | None = None
    #: 🔴 **`answer` 가 없다** — 무엇을 어떤 순서로 물었나까지다.
    tool_trace: tuple[Mapping[str, Any], ...] = ()
    #: 조사의 최종 판단. 🔴 여기서 숫자를 새로 만들지 않는다.
    result: Mapping[str, Any] = field(default_factory=dict)


_COLUMNS = (
    "investigation_id",
    "sim_run_id",
    "exception_id",
    "as_of",
    "finish_reason",
    "llm_status",
    "llm_error_kind",
    "observed_as_of",
    "tool_trace_json",
    "result_json",
)


def _schema() -> sql.Identifier:
    return sql.Identifier(get_db_schema())


def _rows(conn: Any, query: sql.Composed, params: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _json(value: Any, *, fallback: Any) -> Any:
    """jsonb 를 문자열로 돌려주는 드라이버 설정에도 같은 값을 낸다."""
    if value is None:
        return fallback
    return json.loads(value) if isinstance(value, str) else value


def _row(raw: Mapping[str, Any]) -> InvestigationRow:
    return InvestigationRow(
        investigation_id=raw["investigation_id"],
        sim_run_id=raw["sim_run_id"],
        exception_id=raw["exception_id"],
        as_of=raw["as_of"],
        finish_reason=raw["finish_reason"],
        llm_status=raw["llm_status"],
        llm_error_kind=raw["llm_error_kind"],
        observed_as_of=raw["observed_as_of"],
        tool_trace=tuple(_json(raw["tool_trace_json"], fallback=[])),
        result=_json(raw["result_json"], fallback={}),
    )


def _select(where: sql.SQL, order: sql.SQL) -> sql.Composed:
    return sql.SQL(
        "SELECT {columns} FROM {schema}.logistics_investigations WHERE {where}"
        " ORDER BY {order}"
    ).format(
        columns=sql.SQL(", ").join(sql.Identifier(name) for name in _COLUMNS),
        schema=_schema(),
        where=where,
        order=order,
    )


# ══════════════════════════════════════════════════════════════════════════
#  읽기
# ══════════════════════════════════════════════════════════════════════════


def select_investigation(
    conn: Any, *, sim_run_id: str, investigation_id: str
) -> InvestigationRow | None:
    """조사 한 건. 🔴 **실행 축을 함께 건다** — 남의 실행 기록이 안 보인다."""
    rows = _rows(
        conn,
        _select(
            sql.SQL("sim_run_id = %(sim)s AND investigation_id = %(investigation)s"),
            sql.SQL("investigation_id"),
        ),
        {"sim": sim_run_id, "investigation": investigation_id},
    )
    return _row(rows[0]) if rows else None


def select_investigations(
    conn: Any, *, sim_run_id: str, exception_id: str
) -> list[InvestigationRow]:
    """그 문제를 조사한 **모든** 실행. 오래된 것부터.

    ⚠️ **같은 날 두 행이 정상이다** (§35). 하루에 한 번만 조사한다는 가정을 여기서
       세우지 않는다 — 조사는 사람이 부를 때마다 돌고, 두 번 불렀으면 두 번 돈 것이다.

    ★ 벽시계(`created_at`)를 정렬 2순위로 쓴다 — 같은 영업일 안의 **실행 순서**는
      업무 날짜로는 못 가리고, 순서를 답하는 데만 쓰지 업무 판단에 쓰지 않는다.
    """
    rows = _rows(
        conn,
        _select(
            sql.SQL("sim_run_id = %(sim)s AND exception_id = %(exception)s"),
            sql.SQL("as_of, created_at, investigation_id"),
        ),
        {"sim": sim_run_id, "exception": exception_id},
    )
    return [_row(raw) for raw in rows]


# ══════════════════════════════════════════════════════════════════════════
#  쓰기 — 🔴 커밋하지 않는다. UPDATE 도 없다
# ══════════════════════════════════════════════════════════════════════════


def insert_investigation(conn: Any, *, row: InvestigationRow) -> InvestigationRow:
    """조사 실행 한 행. 🔴 **이 표에 쓰는 자리는 여기 하나다.**

    ⚠️ `ON CONFLICT DO NOTHING` 을 안 쓴다. 같은 `investigation_id` 가 두 번 들어오는
       것은 **저장 재시도**일 수도 **다른 내용의 충돌**일 수도 있는데, 조용히 삼키면
       그 둘이 같아진다 — 부딪히면 터뜨리고, 무슨 일이었는지는 서비스가 **다시 읽어서**
       정한다 (`investigation_service._settle_after_race`).
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.logistics_investigations (
                    investigation_id, sim_run_id, exception_id, as_of,
                    finish_reason, llm_status, llm_error_kind, observed_as_of,
                    tool_trace_json, result_json
                ) VALUES (
                    %(investigation_id)s, %(sim)s, %(exception)s, %(as_of)s,
                    %(finish)s, %(llm)s, %(error)s, %(observed)s,
                    %(trace)s::jsonb, %(result)s::jsonb
                )
                """
            ).format(schema=_schema()),
            {
                "investigation_id": row.investigation_id,
                "sim": row.sim_run_id,
                "exception": row.exception_id,
                "as_of": row.as_of,
                "finish": row.finish_reason,
                "llm": row.llm_status,
                "error": row.llm_error_kind,
                "observed": row.observed_as_of,
                "trace": _dumps(list(row.tool_trace)),
                "result": _dumps(row.result),
            },
        )
    return row


def _dumps(value: Any) -> str:
    """🔴 `ensure_ascii=False` — 한국어가 `\\uXXXX` 로 굳으면 사람이 못 읽는다."""
    if isinstance(value, Mapping):
        value = dict(value)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        value = list(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
