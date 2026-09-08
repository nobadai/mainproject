"""영업 Agent API Router."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.sales.dashboard_service import get_sales_dashboard
from app.sales.proposal import run_proposal
from app.sales.schemas import (
    RuntimeStatus,
    SalesAgentRunResponse,
    SalesCycle,
    SalesDashboardResponse,
    SalesProposalInput,
    SalesProposalReply,
)
from app.sales.service import get_sales_run, list_sales_runs

router = APIRouter(prefix="/sales", tags=["sales"])


@router.get(
    "/dashboard",
    response_model=SalesDashboardResponse,
    summary="영업 Dashboard 조회",
)
def read_sales_dashboard(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> SalesDashboardResponse:
    """저장된 판매·수금 원장 사실만 집계해 반환한다."""
    return get_sales_dashboard(sim_run_id=sim_run_id, as_of=as_of, recent_limit=limit)


@router.post(
    "/proposal",
    response_model=SalesProposalReply,
    summary="영업 판매 시나리오 제안",
)
def review_sales_proposal(request: SalesProposalInput) -> SalesProposalReply:
    """Master 연동 전 Sales 전용 시나리오 생성·해석 경로다."""
    return run_proposal(request)


@router.get(
    "/runs",
    response_model=list[SalesAgentRunResponse],
    summary="영업 Agent 실행이력 목록 조회",
)
def get_sales_runs(
    cycle: SalesCycle | None = None,
    as_of: date | None = None,
    snapshot_id: str | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[SalesAgentRunResponse]:
    """cycle, as_of, snapshot_id, runtime_status 필터로 최근 실행이력을 반환한다."""
    return list_sales_runs(
        cycle=cycle,
        as_of=as_of,
        snapshot_id=snapshot_id,
        runtime_status=runtime_status,
        limit=limit,
    )


@router.get(
    "/runs/{run_id}",
    response_model=SalesAgentRunResponse,
    summary="영업 Agent 실행이력 단건 조회",
)
def get_sales_run_by_id(run_id: UUID) -> SalesAgentRunResponse:
    """run_id에 해당하는 실행이력을 반환한다."""
    try:
        return get_sales_run(run_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales Agent run was not found",
        ) from error
