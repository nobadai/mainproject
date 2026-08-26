# ─────────────────────────────────────────────────────────────────────────────
# STATUS: ACTIVE · 공용 (2026-08-26) — persistence.py 와 같은 계열. 위치 재검토 대상.
# ─────────────────────────────────────────────────────────────────────────────
"""오케스트레이터 · Critic 실행이력 Repository.

★ 코어의 DB 미접근 원칙(§5.1)은 그대로다. 여기는 **계산이 끝난 뒤** 요청·응답을 적는
  감사 기록이며, 저장한 값을 계산 입력으로 되읽지 않는다.

★ 적재 실패가 API 를 죽이지 않는다. 이력이 없는 것보다 결과를 못 주는 것이 나쁘다 —
  저장은 `try_save_run` 을 통해 best-effort 로 부른다.

Finance / Logistics 의 `run_repository` 와 같은 모양이되, `agent` 축이 하나 더 있다.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Literal, TypedDict, cast
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from app.finance.db import execute_returning_one, fetch_all, fetch_one, get_db_schema

logger = logging.getLogger(__name__)

Agent = Literal["orchestrator", "critic"]
RunCycle = Literal["PROCUREMENT", "SALES", "DAY", "A", "B"]

_TABLE = "orchestrator_agent_runs"
_COLUMNS = (
    "run_id",
    "agent",
    "cycle",
    "as_of",
    "run_seq",
    "snapshot_id",
    "runtime_status",
    "critic_status",
    "coverage_ran",
    "coverage_total",
    "llm_status",
    "llm_model",
    "llm_attempts",
    "llm_fallback_used",
    "elapsed_ms",
    "request_payload",
    "response_payload",
    "created_at",
)


class OrchestratorAgentRun(TypedDict):
    run_id: UUID
    agent: Agent
    cycle: RunCycle
    as_of: date
    run_seq: int
    snapshot_id: str | None
    runtime_status: str
    critic_status: str | None
    coverage_ran: int | None
    coverage_total: int | None
    llm_status: str | None
    llm_model: str | None
    llm_attempts: int | None
    llm_fallback_used: bool | None
    elapsed_ms: int | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime


def _select() -> sql.Composed:
    return sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS),
        sql.Identifier(get_db_schema()),
        sql.Identifier(_TABLE),
    )


def save_run(
    *,
    agent: Agent,
    cycle: RunCycle,
    as_of: date,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
    run_seq: int = 1,
    snapshot_id: str | None = None,
    runtime_status: str = "READY",
    critic_status: str | None = None,
    coverage_ran: int | None = None,
    coverage_total: int | None = None,
    llm_status: str | None = None,
    llm_model: str | None = None,
    llm_attempts: int | None = None,
    llm_fallback_used: bool | None = None,
    elapsed_ms: int | None = None,
) -> OrchestratorAgentRun:
    """실행 1건을 적재한다."""
    query = sql.SQL(
        """
        INSERT INTO {}.{} (
            run_id, agent, cycle, as_of, run_seq, snapshot_id, runtime_status,
            critic_status, coverage_ran, coverage_total,
            llm_status, llm_model, llm_attempts, llm_fallback_used, elapsed_ms,
            request_payload, response_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            agent,
            cycle,
            as_of,
            run_seq,
            snapshot_id,
            runtime_status,
            critic_status,
            coverage_ran,
            coverage_total,
            llm_status,
            llm_model,
            llm_attempts,
            llm_fallback_used,
            elapsed_ms,
            Jsonb(request_payload),
            Jsonb(response_payload),
        ),
    )
    return cast(OrchestratorAgentRun, row)


def history_enabled() -> bool:
    """실행이력을 남길지.

    ★ pytest 안에서는 남기지 않는다. 표가 팀 공용 DB 에 있어, 테스트를 돌릴 때마다
      2ms 짜리 가짜 실행이 쌓여 진짜 이력을 덮는다(실측: 12행 중 10행이 테스트 산물이었다).
      `RUN_HISTORY_ENABLED=false` 로 수동으로도 끌 수 있다.
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
    """적재를 시도하되 실패해도 예외를 올리지 않는다.

    ★ DB 가 없거나 표가 아직 없어도 API 는 계산 결과를 돌려줘야 한다.
      이력이 없는 것보다 결과를 못 주는 것이 나쁘다.
    """
    if not history_enabled():
        return None
    try:
        return save_run(**kwargs)["run_id"]
    except Exception:
        logger.warning("실행이력 적재 실패 — 계산 결과는 정상 반환합니다", exc_info=True)
        return None


def get_run(run_id: UUID) -> OrchestratorAgentRun:
    query = _select() + sql.SQL(" WHERE run_id = %s")
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"실행이력을 찾을 수 없습니다: {run_id}")
    return cast(OrchestratorAgentRun, row)


def list_runs(
    *,
    agent: Agent | None = None,
    as_of: date | None = None,
    limit: int = 50,
) -> list[OrchestratorAgentRun]:
    """최신순 목록. 화면에서 그날 무슨 일이 있었는지 훑는 용도다."""
    clauses: list[sql.Composable] = []
    params: list[object] = []
    if agent is not None:
        clauses.append(sql.SQL("agent = %s"))
        params.append(agent)
    if as_of is not None:
        clauses.append(sql.SQL("as_of = %s"))
        params.append(as_of)

    query = _select()
    if clauses:
        query = query + sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
    query = query + sql.SQL(" ORDER BY created_at DESC LIMIT %s")
    params.append(max(1, min(limit, 200)))
    return cast(list[OrchestratorAgentRun], fetch_all(query, tuple(params)))
