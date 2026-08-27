"""오케스트레이터 Agent API 라우터.

세 엔드포인트 모두 순수 계산이다 — 원본 DB 를 읽지 않고 요청 본문만으로 응답한다 (§5.1).
계약 위반(밴드 기여 방향·품목 침범 등)은 422 로 돌려준다.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.orchestrator.contracts_core import ContractViolation
from app.orchestrator.persistence import record
from app.orchestrator.run_repository import get_run, list_runs
from app.orchestrator.schemas import (
    DayRequest,
    DayResponse,
    ProcurementRequest,
    ProcurementResponse,
    SalesRequest,
    SalesResponse,
)
from app.orchestrator.service import run_day, run_procurement, run_sales

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


def _guard(func, request, *, agent="orchestrator", cycle="PROCUREMENT"):
    try:
        # 계산 → 실행이력 적재. 적재 실패는 응답을 막지 않는다.
        return record(func, request, agent=agent, cycle=cycle)
    except (ContractViolation, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@router.post(
    "/procurement",
    response_model=ProcurementResponse,
    summary="T3 매입 밴드 결합·클리핑",
)
def orchestrate_procurement(request: ProcurementRequest) -> ProcurementResponse:
    """부서 회신을 결합해 그날의 매입 제약 밴드를 만들고, 매입 후보를 클리핑·순위한다."""
    return _guard(run_procurement, request, cycle="PROCUREMENT")


@router.post(
    "/sales",
    response_model=SalesResponse,
    summary="S3 판매 공용 출고 결합·클리핑",
)
def orchestrate_sales(request: SalesRequest) -> SalesResponse:
    """재고·재무 회신으로 공용 출고 밴드를 만들고, 판매 배분 후보를 클리핑·순위한다."""
    return _guard(run_sales, request, cycle="SALES")


@router.post(
    "/day",
    response_model=DayResponse,
    summary="하루 전체 — 매입(T3) → 판매(S3) 코어",
)
def orchestrate_day(request: DayRequest) -> DayResponse:
    """매입 코어 뒤에 판매 코어를 순차로 돌려 하루 결과를 반환한다."""
    return _guard(run_day, request, cycle="DAY")


@router.get("/runs", summary="실행이력 목록 (최신순)")
def list_orchestrator_runs(
    as_of: date | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """그날 무슨 일이 있었는지 훑는 용도. 계산 입력으로 되읽지 않는다."""
    return [dict(r) for r in list_runs(agent="orchestrator", as_of=as_of, limit=limit)]


@router.get("/runs/{run_id}", summary="실행이력 1건")
def get_orchestrator_run(run_id: UUID) -> dict:
    try:
        return dict(get_run(run_id))
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
