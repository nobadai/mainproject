"""Sales operations-console run-history read model; strictly run-scoped.

★ **The run axis is stored on the row itself.**  Sales writes its execution context
  into `sales_agent_runs.request_payload->'context'`, and `sim_run_id` is one of its
  keys, so this reader filters on the stored value rather than on anything derived
  from a request-id string.

🔴 **Reading history never re-runs the agent.**  A GET that executed Sales would make
  opening a screen write to the ledger.
"""

from datetime import date, datetime
from typing import Any

from psycopg import sql
from pydantic import BaseModel

from app.sales.db import fetch_all, get_db_schema

#: Deterministic order: newest first with a stable tie-break, so two runs stored in
#: the same second keep one fixed order between identical requests.
_ORDER = sql.SQL(" ORDER BY s.created_at DESC, s.run_id DESC")

#: Where Sales stores the axis inside its own request payload.
_AXIS = sql.SQL("s.request_payload->'context'->>'sim_run_id'")


class ConsoleSalesRun(BaseModel):
    run_id: str
    #: Null when the stored context carried no business key.
    request_id: str | None
    sim_run_id: str
    as_of: date
    partner_id: str | None
    partner_name: str | None
    item: str | None
    runtime_status: str
    #: Sales itself stores no verdict; Finance owns that word.  Null, not invented.
    verdict: str | None
    llm_status: str | None
    #: Master's end code for the same request, read from Master's own table.
    master_end_code: str | None
    created_at: datetime


class ConsoleSalesRunsResponse(BaseModel):
    sim_run_id: str
    rows: list[ConsoleSalesRun]


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _payload_item(payload: object, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _row(raw: dict[str, object], *, sim_run_id: str) -> ConsoleSalesRun:
    request = raw["request_payload"]
    response = raw["response_payload"]
    return ConsoleSalesRun(
        run_id=str(raw["run_id"]),
        request_id=_text(_payload_item(request, "context", "request_id")),
        sim_run_id=sim_run_id,
        as_of=raw["as_of"],
        partner_id=_text(_payload_item(request, "payload", "user_request", "partner_id")),
        partner_name=_text(raw["partner_name"]),
        item=_text(_payload_item(request, "payload", "user_request", "item")),
        runtime_status=str(raw["runtime_status"]),
        verdict=None,
        llm_status=_text(_payload_item(response, "payload", "llm", "llm_status")),
        master_end_code=_text(raw["master_end_code"]),
        created_at=raw["created_at"],
    )


def get_console_sales_runs(
    *,
    sim_run_id: str,
    as_of: date | None = None,
    partner_id: str | None = None,
    item: str | None = None,
    runtime_status: str | None = None,
    limit: int = 100,
) -> ConsoleSalesRunsResponse:
    """Stored Sales runs belonging to exactly one simulation run."""
    schema = get_db_schema()
    conditions: list[sql.Composable] = [_AXIS + sql.SQL(" = %s")]
    params: list[object] = [sim_run_id]
    if as_of is not None:
        conditions.append(sql.SQL("s.as_of = %s"))
        params.append(as_of)
    if partner_id is not None:
        conditions.append(
            sql.SQL("s.request_payload->'payload'->'user_request'->>'partner_id' = %s")
        )
        params.append(partner_id)
    if item is not None:
        conditions.append(sql.SQL("s.request_payload->'payload'->'user_request'->>'item' = %s"))
        params.append(item)
    if runtime_status is not None:
        conditions.append(sql.SQL("s.runtime_status = %s"))
        params.append(runtime_status)
    statement = (
        sql.SQL(
            """
            SELECT s.run_id, s.as_of, s.runtime_status, s.request_payload,
                   s.response_payload, s.created_at, p.partner_name,
                   m.end_code AS master_end_code
            FROM {schema}.sales_agent_runs s
            LEFT JOIN {schema}.partners p
              ON p.partner_id = s.request_payload->'payload'->'user_request'->>'partner_id'
            LEFT JOIN LATERAL (
                SELECT end_code
                FROM {schema}.master_agent_runs
                WHERE request_id = s.request_payload->'context'->>'request_id'
                  AND sim_run_id = {axis}
                ORDER BY run_seq DESC, created_at DESC
                LIMIT 1
            ) m ON TRUE
            WHERE
            """
        ).format(schema=sql.Identifier(schema), axis=_AXIS)
        + sql.SQL(" AND ").join(conditions)
        + _ORDER
        + sql.SQL(" LIMIT %s")
    )
    params.append(limit)
    rows = fetch_all(statement, params)
    return ConsoleSalesRunsResponse(
        sim_run_id=sim_run_id, rows=[_row(raw, sim_run_id=sim_run_id) for raw in rows]
    )
