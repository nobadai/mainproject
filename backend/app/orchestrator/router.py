"""오케스트레이터 Agent API 라우터.

세 엔드포인트 모두 순수 계산이다 — 원본 DB 를 읽지 않고 요청 본문만으로 응답한다 (§5.1).
계약 위반(밴드 기여 방향·품목 침범 등)은 422 로 돌려준다.
"""

from fastapi import APIRouter, HTTPException, status

from app.orchestrator.contracts_core import ContractViolation
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


def _guard(func, request):
    try:
        return func(request)
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
    return _guard(run_procurement, request)


@router.post(
    "/sales",
    response_model=SalesResponse,
    summary="S3 판매 공용 출고 결합·클리핑",
)
def orchestrate_sales(request: SalesRequest) -> SalesResponse:
    """재고·재무 회신으로 공용 출고 밴드를 만들고, 판매 배분 후보를 클리핑·순위한다."""
    return _guard(run_sales, request)


@router.post(
    "/day",
    response_model=DayResponse,
    summary="하루 전체 — 매입(T3) → 판매(S3) 코어",
)
def orchestrate_day(request: DayRequest) -> DayResponse:
    """매입 코어 뒤에 판매 코어를 순차로 돌려 하루 결과를 반환한다."""
    return _guard(run_day, request)
