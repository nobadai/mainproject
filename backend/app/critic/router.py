"""Critic Agent API 라우터.

Critic 은 오케 산출물을 검증만 한다 (숫자 불변). 요청 본문만으로 T3/S3 를 재현해 검증한다.
계약 위반(밴드 기여 방향 등)은 422 로 돌려준다.
"""

from fastapi import APIRouter, HTTPException, status

from app.critic.schemas import (
    CriticProcurementRequest,
    CriticSalesRequest,
    CriticVerdictOut,
)
from app.critic.service import run_critic_procurement, run_critic_sales
from app.orchestrator.contracts_core import ContractViolation

router = APIRouter(prefix="/critic", tags=["critic"])


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
    response_model=CriticVerdictOut,
    summary="Critic A - 매입 T3 결과 6레이어 검증",
)
def critic_procurement(request: CriticProcurementRequest) -> CriticVerdictOut:
    """부서 회신·매입 후보로 T3 를 재현하고, 대상 후보를 L0~L4 로 검증한다."""
    return _guard(run_critic_procurement, request)


@router.post(
    "/sales",
    response_model=CriticVerdictOut,
    summary="Critic B - 판매 검증 (사이클 무관 계층만, L4-B 미구현)",
)
def critic_sales(request: CriticSalesRequest) -> CriticVerdictOut:
    """회신 스냅샷 바인딩과 S3 전속 권한 침범을 검사한다. L4-B 는 coverage 에서 skipped."""
    return _guard(run_critic_sales, request)
