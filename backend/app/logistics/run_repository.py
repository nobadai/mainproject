"""Logistics Agent 실행이력 전용 PostgreSQL Repository."""

from datetime import date, datetime
from typing import TypedDict, cast
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from app.logistics.db import execute_returning_one, fetch_all, fetch_one, get_db_schema
from app.logistics.schemas import FinalVerdict, LogisticsCycle, RuntimeStatus


class LogisticsAgentRun(TypedDict):
    run_id: UUID
    cycle: LogisticsCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime


_SELECT_COLUMNS = sql.SQL(
    """
    SELECT
        run_id,
        cycle,
        as_of,
        snapshot_id,
        runtime_status,
        verdict,
        request_payload,
        response_payload,
        created_at
    FROM {}.logistics_agent_runs
    """
)


def save_logistics_agent_run(
    *,
    cycle: LogisticsCycle,
    as_of: date,
    snapshot_id: str | None,
    runtime_status: RuntimeStatus,
    verdict: FinalVerdict | None,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> LogisticsAgentRun:
    """완성된 Logistics Agent Request와 Response를 실행이력으로 저장한다."""
    if response_payload.get("verdict") != verdict:
        raise ValueError("Logistics run verdict metadata must match response_payload.verdict")
    query = sql.SQL(
        """
        INSERT INTO {}.logistics_agent_runs (
            run_id,
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            verdict,
            request_payload,
            response_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING
            run_id,
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            verdict,
            request_payload,
            response_payload,
            created_at
        """
    ).format(sql.Identifier(get_db_schema()))
    row = execute_returning_one(
        query,
        (
            uuid4(),
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            verdict,
            Jsonb(request_payload),
            Jsonb(response_payload),
        ),
    )
    return cast(LogisticsAgentRun, row)


def get_logistics_agent_run(run_id: UUID) -> LogisticsAgentRun:
    """run_id로 Logistics Agent 실행이력 한 건을 조회한다."""
    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema())) + sql.SQL(" WHERE run_id = %s")
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"Logistics Agent run was not found: {run_id}")
    return cast(LogisticsAgentRun, row)


def list_logistics_agent_runs(
    *,
    cycle: LogisticsCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: int = 100,
) -> list[LogisticsAgentRun]:
    """선택한 필터로 최신 Logistics Agent 실행이력을 조회한다."""
    conditions: list[sql.Composable] = []
    params: list[object] = []
    if cycle is not None:
        conditions.append(sql.SQL("cycle = %s"))
        params.append(cycle)
    if as_of is not None:
        conditions.append(sql.SQL("as_of = %s"))
        params.append(as_of)
    if runtime_status is not None:
        conditions.append(sql.SQL("runtime_status = %s"))
        params.append(runtime_status)
    if verdict is not None:
        conditions.append(sql.SQL("verdict = %s"))
        params.append(verdict)

    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema()))
    if conditions:
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
    query += sql.SQL(" ORDER BY created_at DESC, run_id DESC LIMIT %s")
    params.append(limit)
    return cast(list[LogisticsAgentRun], fetch_all(query, params))
