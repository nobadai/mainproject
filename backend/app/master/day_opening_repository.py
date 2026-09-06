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

__all__ = ["DayOpeningRecord", "record_day_opening", "read_day_opening"]

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
    payload = [part.model_dump(mode="json") if hasattr(part, "model_dump") else part for part in parts]
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
    except Exception:  # noqa: BLE001 - 정본을 못 열어도 개장은 이미 끝났다.
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
    except Exception:  # noqa: BLE001 - 이력이 없는 것보다 하루를 못 여는 것이 나쁘다.
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
    except Exception:  # noqa: BLE001
        logger.exception("개장 정본 커넥션 실패 - 근사로 답한다")
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, (as_of, sim_run_id))
            row = cursor.fetchone()
    except Exception:  # noqa: BLE001
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
