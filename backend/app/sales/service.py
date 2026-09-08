"""Sales run history service."""

from datetime import date
from uuid import UUID

from app.sales.run_repository import get_sales_agent_run, list_sales_agent_runs
from app.sales.schemas import RuntimeStatus, SalesAgentRunResponse, SalesCycle


def get_sales_run(run_id: UUID) -> SalesAgentRunResponse:
    return SalesAgentRunResponse.model_validate(get_sales_agent_run(run_id))


def list_sales_runs(
    *,
    cycle: SalesCycle | None = None,
    as_of: date | None = None,
    snapshot_id: str | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: int = 100,
) -> list[SalesAgentRunResponse]:
    rows = list_sales_agent_runs(
        cycle=cycle,
        as_of=as_of,
        snapshot_id=snapshot_id,
        runtime_status=runtime_status,
        limit=limit,
    )
    return [SalesAgentRunResponse.model_validate(row) for row in rows]
