"""Finance Agent 실행이력 전용 PostgreSQL Repository."""

from datetime import date, datetime
from typing import TypedDict, cast
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from app.finance.db import execute_returning_one, fetch_all, fetch_one, get_db_schema
from app.finance.schemas import FinalVerdict, FinanceCycle, RuntimeStatus
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata


class FinanceAgentRun(TypedDict):
    run_id: UUID
    cycle: FinanceCycle
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
    FROM {}.finance_agent_runs
    """
)


def save_finance_agent_run(
    *,
    cycle: FinanceCycle,
    as_of: date,
    snapshot_id: str | None,
    runtime_status: RuntimeStatus,
    verdict: FinalVerdict | None,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> FinanceAgentRun:
    """완성된 Finance Agent Request와 Response를 실행이력으로 저장한다."""
    if response_payload.get("verdict") != verdict:
        raise ValueError("Finance run verdict metadata must match response_payload.verdict")
    query = sql.SQL(
        """
        INSERT INTO {}.finance_agent_runs (
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
    return cast(FinanceAgentRun, row)


def get_finance_agent_run(run_id: UUID) -> FinanceAgentRun:
    """run_id로 Finance Agent 실행이력 한 건을 조회한다."""
    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema())) + sql.SQL(" WHERE run_id = %s")
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"Finance Agent run was not found: {run_id}")
    return cast(FinanceAgentRun, row)


def list_finance_agent_runs(
    *,
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: int = 100,
) -> list[FinanceAgentRun]:
    """선택한 필터로 최신 Finance Agent 실행이력을 조회한다."""
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
    return cast(list[FinanceAgentRun], fetch_all(query, params))


def save_finance_v22_run(
    *, request: AgentRequest, reply: AgentReply, metadata: ExecutionMetadata
) -> None:
    """Persist the v2.2 child trace when the migration is installed.

    Persistence failure is intentionally not swallowed: a normal Business
    completion must have a resolvable run_id.
    """
    query = sql.SQL(
        """
        INSERT INTO {}.finance_agent_runs_v22 (
            run_id, request_id, agent, mode, as_of, policy_version, trigger, call_seq,
            runtime_status,
            business_status, request_payload, response_payload,
            used_tools, tool_order, observations, rules_applied, replans,
            llm_status, llm_model, llm_attempts, llm_fallback_used, elapsed_ms
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """
    ).format(sql.Identifier(get_db_schema()))
    execute_returning_one(
        query + sql.SQL(" RETURNING run_id"),
        (
            UUID(reply.run_id),
            request.context.request_id,
            "finance",
            request.mode,
            request.context.as_of,
            request.context.policy_version,
            request.context.trigger,
            request.call_seq,
            reply.runtime_status,
            reply.business_status,
            Jsonb(dict(request.payload)),
            Jsonb(dict(reply.payload)),
            Jsonb(list(metadata.used_tools)),
            Jsonb(list(metadata.tool_order)),
            Jsonb(list(metadata.observations)),
            Jsonb(list(metadata.rules_applied)),
            metadata.replans,
            metadata.llm_status,
            metadata.llm_model,
            metadata.llm_attempts,
            metadata.llm_fallback_used,
            metadata.elapsed_ms,
        ),
    )


def get_finance_v22_run(run_id: UUID) -> dict[str, object]:
    query = sql.SQL("SELECT * FROM {}.finance_agent_runs_v22 WHERE run_id = %s").format(
        sql.Identifier(get_db_schema())
    )
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"Finance v2.2 run was not found: {run_id}")
    return row
