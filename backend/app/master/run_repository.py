"""run_repository.py - 마스터 실행이력 표 접근. **마스터 소유다.**

★ 왜 `app/orchestrator/run_repository.py` 를 안 쓰는가 (2026-09-02)
  그 모듈은 오케 · Critic · 마스터가 한 표(`orchestrator_agent_runs`)를 쓰던 시절의
  것이고, `agent` 축으로 셋을 갈랐다. 어휘의 소유가 없어서 마스터가 조회(`STATUS`)를
  이력에 남기려 해도 CHECK 를 못 고쳤다 - 남의 행의 뜻까지 건드리기 때문이다.

  마스터 표를 따로 두면서 이 모듈이 그 표를 소유한다. Critic 은 옛 모듈을 그대로
  쓴다 - 남의 코드를 건드리지 않는다.

★ 계산과 적재를 섞지 않는다.
  `flow.py` 는 DB 를 모르고 `service.py` 는 경계 변환만 한다. 여기서만 SQL 을 쓴다.

★ 적재 실패가 응답을 막지 않는다.
  이력이 없는 것보다 결과를 못 주는 것이 나쁘다 - `try_save_run` 이 삼킨다.
  다만 **읽기는 삼키지 않는다.** 없는 실행을 빈 값으로 돌려주면 화면이
  "실행이 없다" 와 "DB 가 죽었다" 를 구별하지 못한다.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, TypedDict
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from app.finance.db import execute_returning_one, fetch_all, fetch_one, get_db_schema

logger = logging.getLogger(__name__)

#: 마스터가 도는 사이클. **`A` · `B` 는 오케 어휘라 없다.**
#:
#: ★ `STATUS` 가 새로 들어왔다. 옛 표에는 없어서 조회를 이력에 안 적고 있었고,
#:   그래서 예산을 쓰는 호출이 이력에서 보이지 않았다. M-16 이 막으려는 것이
#:   정확히 "안 보이는 호출" 이다.
RunCycle = str  # PROCUREMENT | SALES | STATUS | DAY - CHECK 는 DB 가 강제한다

_TABLE = "master_agent_runs"

#: 장부 관문이 막아 **판단을 한 번도 안 돌린 날**이 다는 종료 코드 (`#465`).
#:
#: ★ **주인이 여기다.** `persistence.record_ledger_gap` 이 이 값을 적고
#:   `count_runs_by_day` 가 이 값으로 그 행을 되찾는다. 두 벌로 적어 두면 한쪽만
#:   바뀌는 날 성적표의 `gate_blocked` 가 조용히 늘 거짓이 된다.
LEDGER_GAP_END_CODE = "E4_NOT_STARTED"

_COLUMNS = (
    "run_id",
    "request_id",
    "as_of",
    "cycle",
    "run_seq",
    "item",
    "end_code",
    "runtime_status",
    "coverage_ran",
    "coverage_total",
    "elapsed_ms",
    "plan",
    "request_payload",
    "response_payload",
    "sim_run_id",
    "created_at",
)


class MasterAgentRun(TypedDict):
    run_id: UUID
    request_id: str | None
    as_of: date
    cycle: str
    run_seq: int
    item: str | None
    end_code: str | None
    runtime_status: str
    coverage_ran: int | None
    coverage_total: int | None
    elapsed_ms: int | None
    plan: list[dict[str, object]] | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    sim_run_id: str | None
    created_at: datetime


class DayRunCount(TypedDict):
    """**행이 있는 날** 하나의 집계. 행이 없는 날은 여기 없다.

    🔴 **표는 없는 것을 말할 수 없다.** 안 돈 날은 이 목록에서 그냥 빠져 있고, 그
       빈 자리가 *"안 도는 날이라 없다"* 인지 *"실행일인데 없다"* 인지는 여기서
       답하지 않는다 — 범위를 아는 `app/master/walk_report.py` 가 답한다.

    ★ **행 수와 뜻을 따로 낸다.** `runs` 는 몇 행인가이고 `end_codes` 는 그 행들이
      어떻게 끝났나다. 둘을 섞으면 *"행이 4건이니 개장일이다"* 같은 오독이 나온다 —
      그 4행이 전부 *"실행일이 아니다"* 를 사유로 달고 있어도 행 수는 4다.
    """

    as_of: date
    #: 그날 행 수.
    runs: int
    #: 그날 나온 종료코드와 건수. `end_code` 가 NULL 인 행은 세지 않는다.
    end_codes: dict[str, int]
    #: 그날 나온 품목. 품목 칸이 빈 행(관문·조회)은 빼고 모은다.
    items: tuple[str, ...]
    #: 장부 관문 행(`#465`)이 있었나 — 품목이 없고 `E4_NOT_STARTED` 인 행.
    gate_blocked: bool


def _null_if_blank(value: str | None) -> str | None:
    """빈 문자열을 `None` 으로 접는다. **이 모듈이 그 주인이다.**

    🔴 **왜 필요한가.** `ExecutionContext.sim_run_id` 의 기본값이 `""` 이고 그것은
      *"아직 안 실렸다"* 는 뜻이다 (`envelope.py` 의 ①②③). 그대로 넣으면 표 안에
      **모르는 것이 두 모양**으로 앉는다 — 옛 행은 NULL, 안 실린 새 행은 `''`.
      그러면 *"축이 없는 실행"* 을 세는 질문에 `IS NULL` 만으로는 답이 안 나오고,
      `sim_run_id = ''` 를 잊은 조회가 조용히 틀린 수를 낸다.

    ★ **접는 자리를 여기 하나로 둔다.** `record_*` 넷이 저마다 접으면 하나를
      빠뜨렸을 때 그 사이클만 `''` 를 적고, 아무 오류도 안 난다.
    """
    if value is None:
        return None
    return value or None


def _select() -> sql.Composed:
    return sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS),
        sql.Identifier(get_db_schema()),
        sql.Identifier(_TABLE),
    )


# ── 쓰기 ────────────────────────────────────────────────────────────────────


def save_run(
    *,
    cycle: str,
    as_of: date,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
    request_id: str | None = None,
    run_seq: int = 1,
    item: str | None = None,
    end_code: str | None = None,
    runtime_status: str = "READY",
    coverage_ran: int | None = None,
    coverage_total: int | None = None,
    elapsed_ms: int | None = None,
    plan: list[dict[str, object]] | None = None,
    sim_run_id: str | None = None,
) -> MasterAgentRun:
    """실행 1건을 적재한다.

    ★ `agent` 인자가 없다. 이 표는 마스터 전용이라 늘 같은 값이었고, 상수를
      컬럼으로 두면 "언젠가 다른 값이 들어올 수 있다" 로 읽힌다.

    ★ `item` · `end_code` 는 payload 안에도 있지만 컬럼으로도 받는다.
      "배추가 며칠째 E2 인가" 를 JSONB 를 파지 않고 보기 위해서다. 값을 여기서
      꺼내지 않고 **부르는 쪽이 준다** - 이 모듈이 payload 모양을 알면 응답
      스키마가 바뀔 때마다 적재가 흔들린다.

    ★ `sim_run_id` 는 **어느 장부 위에서 돌았나**다 (2026-09-08 · `Refs #150`).
      출처는 `ExecutionContext.sim_run_id` 하나이고, 값은 부르는 쪽이 실어 준다 -
      전역이나 환경변수에서 집으면 봉투에 실린 값과 표에 적힌 값이 갈린다.

      🔴 빈 문자열은 NULL 로 접는다 (`_null_if_blank`). 기본값 `""` 는 *"아직 안
      실렸다"* 이지 값이 아니다.
    """
    query = sql.SQL(
        """
        INSERT INTO {}.{} (
            run_id, request_id, as_of, cycle, run_seq,
            item, end_code, runtime_status,
            coverage_ran, coverage_total, elapsed_ms,
            plan, request_payload, response_payload,
            sim_run_id
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s
        )
        RETURNING {}
        """
    ).format(
        sql.Identifier(get_db_schema()),
        sql.Identifier(_TABLE),
        sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS),
    )
    row = execute_returning_one(
        query,
        (
            uuid4(),
            request_id,
            as_of,
            cycle,
            run_seq,
            item,
            end_code,
            runtime_status,
            coverage_ran,
            coverage_total,
            elapsed_ms,
            None if plan is None else Jsonb(plan),
            Jsonb(request_payload),
            Jsonb(response_payload),
            _null_if_blank(sim_run_id),
        ),
    )
    return row  # type: ignore[return-value]


def history_enabled() -> bool:
    """실행이력을 남길지.

    ★ **pytest 안에서는 남기지 않는다.** 표가 팀 공용 DB 에 있어, 테스트를 돌릴
      때마다 2ms 짜리 가짜 실행이 쌓여 진짜 이력을 덮는다 (옛 표에서 실측:
      12행 중 10행이 테스트 산물이었다). `RUN_HISTORY_ENABLED=false` 로 수동으로도
      끌 수 있다.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return os.getenv("RUN_HISTORY_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def try_save_run(**kwargs: Any) -> UUID | None:
    """적재하고 `run_id` 를 돌려준다. **실패하면 `None` 이고 예외를 올리지 않는다.**

    ★ 이력이 없는 것보다 결과를 못 주는 것이 나쁘다. 다만 조용히 넘어가지는
      않는다 - 로그에 남긴다. 실패하면 그 실행은 결정이 가리킬 수 없고,
      `master_decisions.run_id` 가 NULL 을 허용하는 이유가 그것이다.
    """
    if not history_enabled():
        return None
    try:
        return save_run(**kwargs)["run_id"]
    except Exception:
        logger.exception("마스터 실행이력 적재 실패 - 응답은 그대로 나간다")
        return None


# ── 읽기 ────────────────────────────────────────────────────────────────────


def get_run(run_id: UUID) -> MasterAgentRun:
    """UUID 로 실행 1건. 없으면 `LookupError`.

    ★ 없는 것을 빈 값으로 돌려주지 않는다. 화면이 "그런 실행이 없다" 와
      "가져오지 못했다" 를 구별할 수 있어야 한다.
    """
    query = _select() + sql.SQL(" WHERE run_id = %s")
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"실행이 없다: {run_id}")
    return row  # type: ignore[return-value]


def get_run_by_request_id(request_id: str, *, cycle: str | None = None) -> MasterAgentRun:
    """업무 키로 **가장 최근** 실행 1건. 없으면 `LookupError`.

    ★ 같은 업무 키로 여러 번 돌면 행이 여럿이다 (append-only). "그 요청 어떻게
      됐냐" 에는 마지막 결과가 답이라 최신을 돌려준다. 전체가 필요하면
      `list_runs(request_id=...)` 를 쓴다.

    🔴 **`cycle` 을 주는 쪽이 왜 중요한가** (2026-09-02, 조회 적재 배선).

      조회와 매입이 **같은 업무 키를 쓴다.** 둘 다 `make_request_id(as_of)` 로
      `REQ-20251231-0001` 을 만들고, 순번 관리는 호출자 몫이라 화면이 안 주면
      같은 값이 된다.

      조회를 이력에 적기 시작하면 그 행이 최신이 되는 날이 생긴다. 그러면

      ```text
      결정 경로     승인할 실행을 찾다가 조회를 집는다 - 조회는 승인 대상이 아니다
      이력 화면     매입 실행을 보여줘야 할 자리에 조회가 뜬다
      ```

      **기본값을 두지 않는다.** 조용히 걸러 주면 새 호출자가 무엇을 보는지
      모른 채 쓰게 된다 - 부르는 쪽이 자기가 무엇을 찾는지 밝힌다.
    """
    clauses = [sql.SQL("request_id = %s")]
    params: list[Any] = [request_id]
    if cycle is not None:
        clauses.append(sql.SQL("cycle = %s"))
        params.append(cycle)

    query = (
        _select()
        + sql.SQL(" WHERE ")
        + sql.SQL(" AND ").join(clauses)
        + sql.SQL(" ORDER BY created_at DESC, run_seq DESC LIMIT 1")
    )
    row = fetch_one(query, tuple(params))
    if row is None:
        scope = "" if cycle is None else f" ({cycle})"
        raise LookupError(f"업무 키로 찾은 실행이 없다{scope}: {request_id}")
    return row  # type: ignore[return-value]


def list_runs(
    *,
    request_id: str | None = None,
    as_of: date | None = None,
    as_of_before: date | None = None,
    cycle: str | None = None,
    item: str | None = None,
    sim_run_id: str | None = None,
    limit: int = 50,
) -> list[MasterAgentRun]:
    """조건에 맞는 실행 목록. 최신부터.

    ★ 조건을 주지 않으면 전체에서 최신 `limit` 건이다. 필터는 전부 선택이고
      주어진 것만 AND 로 붙는다 - 없는 조건을 기본값으로 채우지 않는다.

    ★ `as_of_before` 는 **그 날 이전**이다 (`<`). 오늘 실행이 어제까지 승인된 것을
      물을 때 쓴다 (#185) - 오늘 것을 같이 세면 자기 자신을 입력으로 먹는다.
      `as_of` 와 함께 주면 둘 다 AND 로 걸린다.

    ★ `sim_run_id` 도 **기본값이 없다** (2026-09-08 · `Refs #150`). 안 주면 안
      좁힌다 - 기존 호출을 안 깨뜨리고, 무엇보다 *"어느 실행인지 기록되지 않은"*
      1,206행을 조용히 감추지 않는다.

      ⚠️ 이 인자로 검증 상태와 장기 상태가 **갈리지는 않는다.** 축이 붙은 행만
        갈리고, 축이 NULL 인 옛 행은 어느 값으로도 안 걸린다 - 그것이 사실이다.

    🔴 **바로 아래 `count_runs_by_day` 는 태도가 반대다.** 거기는 `sim_run_id` 가
       필수이고 없으면 거부한다. 물음이 다르기 때문이다.

       ```text
       list_runs           "무슨 행이 있나"     → 안 좁히는 것이 정직하다
       count_runs_by_day   "이 걷기가 어땠나"   → 어느 걷기인지 없으면 물음이 안 선다
       ```
    """
    clauses: list[sql.Composable] = []
    params: list[Any] = []
    for column, value in (
        ("request_id", request_id),
        ("as_of", as_of),
        ("cycle", cycle),
        ("item", item),
        ("sim_run_id", sim_run_id),
    ):
        if value is not None:
            clauses.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            params.append(value)
    if as_of_before is not None:
        clauses.append(sql.SQL("{} < %s").format(sql.Identifier("as_of")))
        params.append(as_of_before)

    query = _select()
    if clauses:
        query = query + sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
    query = query + sql.SQL(" ORDER BY created_at DESC LIMIT %s")
    params.append(limit)

    return [row for row in fetch_all(query, tuple(params))]  # type: ignore[misc]


def check_walk_scope(*, sim_run_id: str, start: date, end: date) -> str:
    """성적표가 설 수 있는 물음인지 보고, **정규화한 축**을 돌려준다.

    ★ **집계와 성적표가 같은 문장을 쓴다.** 둘 다 이 규칙이 필요하고(하나는 WHERE
      절을 만들고 하나는 날을 만든다), 두 벌로 적으면 한쪽만 고치는 날 진입점이
      400 을 안 내면서 빈 성적표를 내보낸다.

    :raises ValueError: 축이 비었거나 범위가 뒤집혔을 때.
    """
    axis = _null_if_blank(sim_run_id)
    if axis is None:
        raise ValueError(
            "sim_run_id 없이 성적표를 셀 수 없다 — 어느 걷기인지 없으면 물음이 성립하지 않는다"
        )
    if end < start:
        raise ValueError(f"범위가 뒤집혔다: {start.isoformat()} ~ {end.isoformat()}")
    return axis


def count_runs_by_day(
    *,
    sim_run_id: str,
    start: date,
    end: date,
) -> list[DayRunCount]:
    """한 걷기의 **날짜별 집계.** 행이 있는 날만, 오래된 날부터.

    🔴 **`sim_run_id` 가 필수다 — `list_runs` 와 반대다** (`Master 19.0` §3.3).

      성적표는 *"이 걷기가 어땠나"* 를 묻는다. 안 좁히면 사람이 손으로 부른 행과 옛
      실험이 같이 세어지고, **행 수가 걷기의 성적으로 읽힌다.** 실측(2026-09-09)으로
      이 표 1,322행 중 걷기는 116행이고 나머지 1,206행은 축이 안 실린 행이다.

      🔴 빈 값을 조용히 전체로 바꾸지 않는다. `""` 도 `None` 도 거부한다 — 기본값을
        주면 새 호출자가 무엇을 세는지 모른 채 쓰게 된다.

      ⚠️ **축이 NULL 인 행은 어느 걷기에도 안 걸린다.** `sim_run_id = %s` 는 NULL 을
        안 집는다. 그것이 사실이고, 감추는 것이 아니라 못 답하는 것이다.

    ⚠️ **`limit` 이 없다.** `list_runs` 의 기본 50 으로는 200일 걷기를 못 읽는다.
      여기는 집계라 결과가 **날 수만큼**이고 행 수를 따라 늘지 않는다.

    ⚠️ **파이썬에서 세지 않는다.** 600행을 끌어와 세면 *"몇 행을 읽었나"* 와 *"몇
      행이 있나"* 가 갈릴 자리가 생기고, 걷기가 길어질수록 그 자리가 커진다.

    :raises ValueError: `sim_run_id` 가 비었거나 `end` 가 `start` 보다 앞일 때
        (`check_walk_scope`).
    """
    axis = check_walk_scope(sim_run_id=sim_run_id, start=start, end=end)

    query = sql.SQL(
        """
        WITH filtered AS (
            SELECT as_of, end_code, item
            FROM {schema}.{table}
            WHERE sim_run_id = %s AND as_of >= %s AND as_of <= %s
        ),
        per_code AS (
            SELECT as_of, end_code, COUNT(*)::int AS n
            FROM filtered
            WHERE end_code IS NOT NULL
            GROUP BY as_of, end_code
        ),
        per_day AS (
            SELECT
                as_of,
                COUNT(*)::int AS runs,
                COALESCE(
                    ARRAY_AGG(DISTINCT item) FILTER (WHERE item IS NOT NULL),
                    ARRAY[]::text[]
                ) AS items,
                BOOL_OR(item IS NULL AND end_code = %s) AS gate_blocked
            FROM filtered
            GROUP BY as_of
        )
        SELECT
            d.as_of,
            d.runs,
            d.items,
            d.gate_blocked,
            COALESCE(
                (
                    SELECT jsonb_object_agg(p.end_code, p.n)
                    FROM per_code p
                    WHERE p.as_of = d.as_of
                ),
                '{{}}'::jsonb
            ) AS end_codes
        FROM per_day d
        ORDER BY d.as_of
        """
    ).format(
        schema=sql.Identifier(get_db_schema()),
        table=sql.Identifier(_TABLE),
    )
    rows = fetch_all(query, (axis, start, end, LEDGER_GAP_END_CODE))
    return [
        DayRunCount(
            as_of=row["as_of"],
            runs=row["runs"],
            end_codes=dict(row["end_codes"]),
            # ★ 모양만 바꾼다 — 세는 것은 위 SQL 이 이미 다 했다.
            items=tuple(row["items"]),
            gate_blocked=bool(row["gate_blocked"]),
        )
        for row in rows
    ]
