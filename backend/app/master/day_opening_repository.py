"""
day_opening_repository.py — 개장 정본(`master_day_openings`) 적재·조회.

🔴 **파트 트랜잭션 밖에서 쓴다.**

  실패도 기록해야 시도 횟수를 셀 수 있는데, 파트 트랜잭션 안에 넣으면 **롤백될 때
  실패했다는 사실까지 사라진다.** `persistence.record` 가 응답 이력을 따로 남기는 것과
  같은 자리다.

🔴 **적재 실패가 개장을 죽이지 않는다.**

  이력이 없는 것보다 하루를 못 여는 것이 나쁘다. `run_repository.try_save_run` 이
  *"이력이 없는 것보다 결과를 못 주는 것이 나쁘다"* 라 적은 것과 같은 판단이고,
  **조용히 넘어가지는 않는다** — 로그에 남긴다.

★ **정본 키는 `(as_of, sim_run_id)` 다** (재무·물류 2026-09-06 합의).

  ```text
  Master 공통 정본     (as_of, sim_run_id)
  Finance 실제 상태     (sim_run_id, as_of, financing_mode)
  Logistics 실제 상태   (sim_run_id, as_of, usage_scope)
  ```

  ⚠️ `financing_mode` · `usage_scope` 는 **파트 고유 축**이라 마스터가 안 가진다.
    가지기 시작하면 파트가 늘 때마다 정본 키가 바뀐다.

🔴 **두 칸을 센다. 계약이 하나만 적은 것이 얕았다.**

  ```text
  attempt_count   이 날에 몇 번 불렀나 (성공·실패 다)
  failure_count   **연속** 실패 — 성공하면 0 으로 돌아간다
  ```

  ★ `next_action` 이 쓰는 것은 `failure_count` 다. 계약이 *"실패 1회째는 재시도,
    2회 이상은 사람"* 이라 했는데, **어제 성공하고 오늘 처음 실패한 것을 "2번째" 로
    세면 재시도 한 번 없이 사람을 부른다.**
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from app.finance.db import get_connection, get_db_schema

__all__ = ["DayOpeningRecord", "read_day_opening", "record_day_opening"]

logger = logging.getLogger(__name__)

TABLE = "master_day_openings"

#: 조회 컬럼 순서. `read_day_opening` 의 SELECT 와 **같아야 한다.**
_COLUMNS = ("as_of", "sim_run_id", "result", "attempt_count", "failure_count", "reason")

#: 성공 어휘. 이 둘이면 연속 실패가 0 으로 돌아간다.
_SUCCESS = frozenset({"OPENED", "ALREADY_OPENED"})


class DayOpeningRecord:
    """개장 정본 한 행. **읽기 전용 값이다.**"""

    __slots__ = ("as_of", "attempt_count", "failure_count", "reason", "result", "sim_run_id")

    def __init__(
        self,
        *,
        as_of: date,
        sim_run_id: str,
        result: str,
        attempt_count: int,
        failure_count: int,
        reason: str | None,
    ) -> None:
        self.as_of = as_of
        self.sim_run_id = sim_run_id
        self.result = result
        self.attempt_count = attempt_count
        self.failure_count = failure_count
        self.reason = reason


def _table() -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(TABLE))


def record_day_opening(
    *,
    as_of: date,
    sim_run_id: str,
    result: str,
    reason: str = "",
    parts: Sequence[Any] = (),
    connect: Callable[[], Any] | None = None,
) -> bool:
    """개장 1회를 정본에 적는다. **예외를 올리지 않는다.**

    ```text
    처음이면        attempt_count=1 · failure_count = 성공이면 0 아니면 1
    다시 부르면      attempt_count += 1
                    성공이면 failure_count = 0
                    실패면   failure_count += 1
    ```

    🔴 **성공이 연속 실패를 0 으로 되돌린다.** 그래야 *"어제 성공하고 오늘 처음 실패"*
       가 첫 실패로 세어지고, 재시도를 한 번은 권하게 된다.

    ⚠️ **`parts` 원문을 그대로 담는다.** `PART_FAILED` 의 내부 상세는 여기에만 남고
      화면에 안 간다 (계약 §6). 마스터가 그것을 해석하지 않는다.

    :returns: 적었으면 참. **못 적어도 거짓을 돌려줄 뿐 개장을 죽이지 않는다.**
    """
    succeeded = result in _SUCCESS
    payload = [
        part.model_dump(mode="json") if hasattr(part, "model_dump") else part for part in parts
    ]
    query = sql.SQL(
        """
        INSERT INTO {} (as_of, sim_run_id, result, attempt_count, failure_count, reason, parts_json)
        VALUES (%s, %s, %s, 1, %s, %s, %s)
        ON CONFLICT (as_of, sim_run_id) DO UPDATE SET
            result = EXCLUDED.result,
            attempt_count = {}.attempt_count + 1,
            -- 🔴 성공이면 0, 실패면 직전 값 + 1. 여기가 next_action 의 근거다.
            failure_count = CASE WHEN %s THEN 0 ELSE {}.failure_count + 1 END,
            reason = EXCLUDED.reason,
            parts_json = EXCLUDED.parts_json,
            last_attempt_at = now()
        """
    ).format(_table(), _table(), _table())

    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception:
        logger.exception("개장 정본 커넥션 실패 - 개장 결과는 그대로 나간다")
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                query,
                (
                    as_of,
                    sim_run_id,
                    result,
                    0 if succeeded else 1,
                    reason or None,
                    Jsonb(payload),
                    succeeded,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("개장 정본 적재 실패 - 개장 결과는 그대로 나간다")
        return False
    finally:
        conn.close()
    return True


def read_day_opening(
    *, as_of: date, sim_run_id: str, connect: Callable[[], Any] | None = None
) -> DayOpeningRecord | None:
    """그 날의 개장 정본. **없으면 `None` 이고 그것은 *"한 번도 안 불렀다"* 다.**

    ⚠️ **못 읽은 것도 `None` 이다.** 관문이 이 값을 못 읽었다고 판단을 멈추면 안 되고,
      그때는 근사를 쓰되 **근사라는 것을 사유에 적는다** (`day_gate`).
    """
    query = sql.SQL(
        "SELECT as_of, sim_run_id, result, attempt_count, failure_count, reason"
        " FROM {} WHERE as_of = %s AND sim_run_id = %s"
    ).format(_table())

    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception:
        logger.exception("개장 정본 커넥션 실패 - 근사로 답한다")
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, (as_of, sim_run_id))
            row = cursor.fetchone()
    except Exception:
        logger.exception("개장 정본 조회 실패 - 근사로 답한다")
        return None
    finally:
        conn.close()
    if row is None:
        return None
    # ★ `dict_row` 면 Mapping, 아니면 순서 튜플이다. 조회 컬럼 순서와 짝이다.
    if isinstance(row, Mapping):
        values = [row[name] for name in _COLUMNS]
    else:
        values = list(row)
    as_of_v, sim_v, result_v, attempt_v, failure_v, reason_v = values
    return DayOpeningRecord(
        as_of=as_of_v,
        sim_run_id=sim_v,
        result=result_v,
        attempt_count=int(attempt_v),
        failure_count=int(failure_v),
        reason=reason_v,
    )


def opened_days_after(
    *, after: date, sim_run_id: str, connect: Callable[[], Any] | None = None
) -> tuple[date, ...] | None:
    """`after` **보다 뒤에** 이미 열린 날들. 오래된 것부터.

    🔴 **왜 이 함수가 필요한가** (물류 물음 2026-09-07 · 실측 2026-09-07).

      승인 전이는 `target_state_date`(= 승인일 + 1) **한 행에만** 쓴다. 그런데 그
      다음 날들이 **이미 열려 있으면** 그 행들은 승인 이전의 전날에서 물려받은
      것이라 **새 도착분을 모른다.**

      ```text
      2026-01-14   in_transit 2건   ← 승인이 여기 들어갔다
      2026-01-15   in_transit 1건   ← **도착일인데 새 것이 없다**
      ```

      ★ 정방향 운영에서는 안 생긴다 — 내일은 아직 없으니까. 다만 *"내일을 미리 열어
        두고 오늘 승인"* 은 실제로 있을 수 있는 순서이고, 실측 장부가 그 상태였다.

    🔴 **마스터가 물류 표를 읽지 않는다** (정의서 §3.2.5). 어느 날이 열렸는지는
       `master_day_openings` 가 아는 **마스터 사실**이라 여기서 답할 수 있다.

       ⚠️ 그래서 **정본에 없는 날은 안 보인다.** 이 표가 생기기 전에 열린 날은
         마스터도 모르고, 그것은 근사가 아니라 **모르는 것**이다 — 지어내지 않는다.

    ★ **성공한 개장만 센다.** `NOT_OPENED` · `REJECTED_GAP` 인 날은 파트 행이 안 섰
      으므로 물려줄 것도 없다.

    🔴 **못 읽으면 `None` 이다. 빈 튜플이 아니다** (물류 지적 2026-09-07).

      ```text
      ()      앞질러 열린 날이 **없다**        정방향이다
      None    **못 읽었다**                    있었는지조차 모른다
      ```

      ⚠️ 둘을 `()` 하나로 접으면 낡은 미래 행이 남아 있는데도 호출자가 *"따라잡을
        것이 없었다"* 로 읽는다. **없는 것과 못 읽은 것은 다르다.**

    ★ 못 읽는 것이 승인을 멈추지는 않는다 — `record_day_opening` 이 절대 raise 하지
      않는 것과 같은 규율이다. 다만 그 사실이 `TransitionOut.carried_forward_status`
      로 나간다.
    """
    query = sql.SQL(
        "SELECT as_of FROM {} WHERE sim_run_id = %s AND as_of > %s"
        " AND result IN ('OPENED', 'ALREADY_OPENED') ORDER BY as_of"
    ).format(_table())

    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception:
        logger.exception("개장 정본 조회 실패 - 앞질러 열린 날을 모른 채 간다")
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(query, (sim_run_id, after))
            rows = cur.fetchall()
    except Exception:
        logger.exception("개장 정본 조회 실패 - 앞질러 열린 날을 모른 채 간다")
        return None
    finally:
        conn.close()
    return tuple(row["as_of"] if isinstance(row, Mapping) else row[0] for row in rows)
