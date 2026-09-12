"""운영 콘솔의 실행 목록 — **읽기 전용**이다.

★ **실행(`sim_runs`)의 주인은 마스터다.** 이 모듈은 그 표를 **읽기만** 한다. 실행을
  만들거나 고치거나 상태를 바꾸는 길은 여기 없다 — 그것은 `app/master/sim_run.py` 와
  `sim_run_runner` 가 하는 일이고, 화면이 그 자리를 대신하면 «화면에서 실행을 열었다»
  같은 경로가 생긴다.

🔴 **없는 칸을 만들지 않는다.** `sim_runs` 가 들고 있는 값만 낸다. 정책 버전처럼 그
   표에 없는 것은 **칸 자체를 두지 않는다** — `null` 로라도 이름을 내면 받는 쪽은
   *"언젠가 올 값"* 으로 읽고 그 자리를 비워 둔다.

🔴 **이름에서 뜻을 읽지 않는다.** `SIM-WALK-…-BASE` 의 꼬리표는 사람이 고를 때 쓰는
   표식이고, 그 실행이 무엇인지는 행이 답한다 (마스터 통보 2026-09-10). 여기서는
   `status` · `run_type` 같은 **저장된 값**만 옮긴다.
"""

from datetime import date, datetime

from psycopg import sql
from pydantic import BaseModel

from app.finance.db import fetch_all, get_db_schema


class ConsoleRun(BaseModel):
    sim_run_id: str
    #: 실행 종류 (예: WALK).  `sim_runs.run_type` 그대로다.
    run_type: str | None
    #: 이 실행이 서 있는 기준일.  화면의 조회 기준일 기본값이 되는 값이다.
    as_of: date | None
    period_start: date | None
    period_end: date | None
    status: str | None
    financing_mode: str | None
    company_persona_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    #: 이 실행에 **마지막으로 기록이 쌓인 시각**.  마스터 실행 이력의 최신 행이며,
    #: 기록이 하나도 없으면 `null` 이다 — 0 이나 실행 생성 시각으로 메우지 않는다.
    latest_activity_at: datetime | None
    note: str | None


class ConsoleRunsResponse(BaseModel):
    rows: list[ConsoleRun]


def load_console_runs(*, limit: int) -> list[dict[str, object]]:
    """실행 행과, 각 실행에 마지막으로 기록이 쌓인 시각.

    ★ 정렬은 **최근 활동 → 기준일 → 이름** 순이다. 활동이 없는 실행이 목록에서
      사라지지 않도록 `NULLS LAST` 로 뒤에 세운다 — 아직 안 걸은 실행도 고를 수
      있어야 한다.
    """
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT r.sim_run_id, r.run_type, r.as_of, r.period_start, r.period_end,
               r.status, r.financing_mode, r.company_persona_id,
               r.started_at, r.finished_at, r.note,
               a.latest_activity_at
        FROM {schema}.sim_runs r
        LEFT JOIN LATERAL (
            SELECT max(created_at) AS latest_activity_at
            FROM {schema}.master_agent_runs
            WHERE sim_run_id = r.sim_run_id
        ) a ON TRUE
        ORDER BY a.latest_activity_at DESC NULLS LAST,
                 r.as_of DESC NULLS LAST,
                 r.sim_run_id ASC
        LIMIT %s
        """
    ).format(schema=sql.Identifier(schema))
    return fetch_all(statement, [limit])


def get_console_runs(*, limit: int = 100) -> ConsoleRunsResponse:
    """고를 수 있는 실행 목록.  판단하지 않고 저장된 행을 옮기기만 한다."""
    return ConsoleRunsResponse(
        rows=[
            ConsoleRun(
                sim_run_id=str(raw["sim_run_id"]),
                run_type=None if raw["run_type"] is None else str(raw["run_type"]),
                as_of=raw["as_of"],
                period_start=raw["period_start"],
                period_end=raw["period_end"],
                status=None if raw["status"] is None else str(raw["status"]),
                financing_mode=(
                    None if raw["financing_mode"] is None else str(raw["financing_mode"])
                ),
                company_persona_id=(
                    None if raw["company_persona_id"] is None else str(raw["company_persona_id"])
                ),
                started_at=raw["started_at"],
                finished_at=raw["finished_at"],
                latest_activity_at=raw["latest_activity_at"],
                note=None if raw["note"] is None else str(raw["note"]),
            )
            for raw in load_console_runs(limit=limit)
        ]
    )
