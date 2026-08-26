"""영업 Agent 실행이력 전용 PostgreSQL Repository."""

from datetime import date, datetime
from typing import TypedDict, cast
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from app.sales.db import execute_returning_one, fetch_all, fetch_one, get_db_schema
from app.sales.schemas import RuntimeStatus, SalesCycle


class SalesAgentRun(TypedDict):
    run_id: UUID
    cycle: SalesCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
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
        request_payload,
        response_payload,
        created_at
    FROM {}.sales_agent_runs
    """
)


def save_sales_agent_run(
    *,
    cycle: SalesCycle,
    as_of: date,
    snapshot_id: str | None,
    runtime_status: RuntimeStatus,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> SalesAgentRun:
    """완성된 영업 Agent Request와 Response를 실행이력으로 저장한다."""
    query = sql.SQL(
        """
        INSERT INTO {}.sales_agent_runs (
            run_id,
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            request_payload,
            response_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING
            run_id,
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
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
            Jsonb(request_payload),
            Jsonb(response_payload),
        ),
    )
    return cast(SalesAgentRun, row)


def get_sales_agent_run(run_id: UUID) -> SalesAgentRun:
    """run_id로 영업 Agent 실행이력 한 건을 조회한다."""
    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema())) + sql.SQL(" WHERE run_id = %s")
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"Sales Agent run was not found: {run_id}")
    return cast(SalesAgentRun, row)


def list_sales_agent_runs(
    *,
    cycle: SalesCycle | None = None,
    as_of: date | None = None,
    snapshot_id: str | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: int = 100,
) -> list[SalesAgentRun]:
    """선택한 필터로 최신 영업 Agent 실행이력을 조회한다."""
    conditions: list[sql.Composable] = []
    params: list[object] = []
    if cycle is not None:
        conditions.append(sql.SQL("cycle = %s"))
        params.append(cycle)
    if as_of is not None:
        conditions.append(sql.SQL("as_of = %s"))
        params.append(as_of)
    if snapshot_id is not None:
        conditions.append(sql.SQL("snapshot_id = %s"))
        params.append(snapshot_id)
    if runtime_status is not None:
        conditions.append(sql.SQL("runtime_status = %s"))
        params.append(runtime_status)

    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema()))
    if conditions:
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
    query += sql.SQL(" ORDER BY created_at DESC, run_id DESC LIMIT %s")
    params.append(limit)
    return cast(list[SalesAgentRun], fetch_all(query, params))
