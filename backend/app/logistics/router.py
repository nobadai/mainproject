"""재고·물류 Agent API Router."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.logistics.schemas import (
    FinalVerdict,
    LogisticsAgentRunResponse,
    LogisticsCycle,
    LogisticsProcurementResponse,
    LogisticsSalesRequest,
    LogisticsSalesResponse,
    PurchaseAgentOutput,
    RuntimeStatus,
)
from app.logistics.service import (
    get_logistics_run,
    list_logistics_runs,
    run_logistics_procurement,
    run_logistics_sales,
)

router = APIRouter(prefix="/logistics", tags=["logistics"])


@router.post(
    "/procurement",
    response_model=LogisticsProcurementResponse,
    summary="Logistics A 날짜별 입고 가능 Band 조회",
)
def review_logistics_procurement(request: PurchaseAgentOutput) -> LogisticsProcurementResponse:
    """Purchase v0.4와 실제 Snapshot으로 Logistics A Reply를 반환한다."""
    return run_logistics_procurement(request)


@router.post(
    "/sales",
    response_model=LogisticsSalesResponse,
    summary="Logistics B 출고 Capacity 및 Lot Constraint 조회",
)
def review_logistics_sales(request: LogisticsSalesRequest) -> LogisticsSalesResponse:
    """H1 승인 매입을 미래 입고로 Overlay한 Logistics B Reply를 반환한다."""
    return run_logistics_sales(request)


@router.get(
    "/runs",
    response_model=list[LogisticsAgentRunResponse],
    summary="Logistics Agent 실행이력 목록 조회",
)
def get_logistics_runs(
    cycle: LogisticsCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[LogisticsAgentRunResponse]:
    """cycle, as_of, runtime_status 필터로 최근 실행이력을 반환한다."""
    return list_logistics_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )


@router.get(
    "/runs/{run_id}",
    response_model=LogisticsAgentRunResponse,
    summary="Logistics Agent 실행이력 단건 조회",
)
def get_logistics_run_by_id(run_id: UUID) -> LogisticsAgentRunResponse:
    """run_id에 해당하는 실행이력을 반환한다."""
    try:
        return get_logistics_run(run_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Logistics Agent run was not found",
        ) from error
