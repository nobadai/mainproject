"""Finance operations-console run-history read model; strictly run-scoped.

🔴 **The run axis is not stored on the Finance run row.**  `finance_agent_runs_v22`
keeps `request_id` but no `sim_run_id`, so this reader asks the one table that does
record that binding — Master's `master_agent_runs`, which wrote the request in the
first place.  It is read, never written, and nothing is copied out of it beyond the
axis it owns.

The alternative would be parsing a run id out of the request-id string.  That reads
like it works and is a guess: a request id is a business key, not a schema, and the
day its shape changes the console starts attributing runs to the wrong simulation
without any error to notice.

A Finance run whose request never reached Master therefore belongs to no run here
and is not returned.  That is the honest answer — there is no stored fact that puts
it in one.
"""

from datetime import date, datetime
from typing import Any

from psycopg import sql
from pydantic import BaseModel

from app.finance.db import fetch_all, get_db_schema

#: Deterministic order: newest first, then a stable tie-break so two runs written in
#: the same transaction never swap places between two identical requests.
_ORDER = sql.SQL(" ORDER BY f.created_at DESC, f.run_id DESC")


class ConsoleFinanceRun(BaseModel):
    run_id: str
    request_id: str
    sim_run_id: str
    as_of: date
    mode: str
    runtime_status: str
    business_status: str
    #: `PASS` / `REVIEW_REQUIRED` / `FAIL`, or null when the stored reply carried no
    #: verdict.  Null is "this run did not conclude", never a verdict of its own.
    verdict: str | None
    llm_status: str
    #: The deterministic block the agent actually stored.  Not recomputed here.
    deterministic_result: dict[str, Any] | None
    evidence: list[str] | None
    #: Finance stores no free-text interpretation per run yet; the column is carried
    #: as null rather than filled with the reasoning of some other layer.
    interpretation: str | None
    created_at: datetime


class ConsoleFinanceRunsResponse(BaseModel):
    sim_run_id: str
    rows: list[ConsoleFinanceRun]


def _row(raw: dict[str, object], *, sim_run_id: str) -> ConsoleFinanceRun:
    response = raw["response_payload"] if isinstance(raw["response_payload"], dict) else {}
    verdict = response.get("finance_verdict")
    summary = response.get("financial_summary")
    evidence = response.get("evidence_refs")
    return ConsoleFinanceRun(
        run_id=str(raw["run_id"]),
        request_id=str(raw["request_id"]),
        sim_run_id=sim_run_id,
        as_of=raw["as_of"],
        mode=str(raw["mode"]),
        runtime_status=str(raw["runtime_status"]),
        business_status=str(raw["business_status"]),
        verdict=None if verdict is None else str(verdict),
        llm_status=str(raw["llm_status"]),
        deterministic_result=summary if isinstance(summary, dict) else None,
        evidence=[str(item) for item in evidence] if isinstance(evidence, list) else None,
        interpretation=None,
        created_at=raw["created_at"],
    )


def _select(
    *,
    sim_run_id: str,
    as_of: date | None,
    from_date: date | None,
    to_date: date | None,
    runtime_status: str | None,
    verdict: str | None,
    limit: int,
) -> list[dict[str, object]]:
    schema = get_db_schema()
    conditions: list[sql.Composable] = [
        sql.SQL(
            """EXISTS (SELECT 1 FROM {}.master_agent_runs m
                       WHERE m.request_id = f.request_id AND m.sim_run_id = %s)"""
        ).format(sql.Identifier(schema))
    ]
    params: list[object] = [sim_run_id]
    if as_of is not None:
        conditions.append(sql.SQL("f.as_of = %s"))
        params.append(as_of)
    if from_date is not None:
        conditions.append(sql.SQL("f.as_of >= %s"))
        params.append(from_date)
    if to_date is not None:
        conditions.append(sql.SQL("f.as_of <= %s"))
        params.append(to_date)
    if runtime_status is not None:
        conditions.append(sql.SQL("f.runtime_status = %s"))
        params.append(runtime_status)
    if verdict is not None:
        conditions.append(sql.SQL("f.response_payload->>'finance_verdict' = %s"))
        params.append(verdict)
    query = (
        sql.SQL(
            """
            SELECT f.run_id, f.request_id, f.as_of, f.mode, f.runtime_status,
                   f.business_status, f.llm_status, f.response_payload, f.created_at
            FROM {}.finance_agent_runs_v22 f
            WHERE
            """
        ).format(sql.Identifier(schema))
        + sql.SQL(" AND ").join(conditions)
        + _ORDER
        + sql.SQL(" LIMIT %s")
    )
    params.append(limit)
    return fetch_all(query, params)


def get_console_finance_runs(
    *,
    sim_run_id: str,
    as_of: date | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    runtime_status: str | None = None,
    verdict: str | None = None,
    limit: int = 100,
) -> ConsoleFinanceRunsResponse:
    """Stored Finance runs belonging to exactly one simulation run."""
    rows = _select(
        sim_run_id=sim_run_id,
        as_of=as_of,
        from_date=from_date,
        to_date=to_date,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )
    return ConsoleFinanceRunsResponse(
        sim_run_id=sim_run_id, rows=[_row(raw, sim_run_id=sim_run_id) for raw in rows]
    )


def get_console_finance_latest_run(
    *, sim_run_id: str, as_of: date | None = None
) -> ConsoleFinanceRun | None:
    """The newest stored Finance run **inside this simulation run**, or null.

    🔴 This reads history.  It never calls the agent — a GET that re-runs Finance
    would make opening a screen change the ledger.

    🔴 There is no global fallback.  If this run has no Finance run yet, the answer
    is "none", not somebody else's newest run.
    """
    rows = _select(
        sim_run_id=sim_run_id,
        as_of=as_of,
        from_date=None,
        to_date=None,
        runtime_status=None,
        verdict=None,
        limit=1,
    )
    return None if not rows else _row(rows[0], sim_run_id=sim_run_id)
