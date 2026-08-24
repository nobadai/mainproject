"""재무·자금 Agent API 라우터."""

from fastapi import APIRouter

from app.finance.schemas import (
    FinanceProcurementResponse,
    FinanceReviewRequest,
    FinanceSalesRequest,
    FinanceSalesResponse,
    PurchaseAgentOutput,
)
from app.finance.service import (
    FinanceCoreResult,
    run_finance_core,
    run_finance_procurement,
    run_finance_sales,
)

router = APIRouter(prefix="/finance", tags=["finance"])


@router.post(
    "/core-review",
    response_model=FinanceCoreResult,
    summary="Finance P0 deterministic core 검증",
    deprecated=True,
)
def review_finance_core(request: FinanceReviewRequest) -> FinanceCoreResult:
    """LLM 이전의 Repository → Tools → Rules 흐름을 검증한다."""
    return run_finance_core(request)


@router.post(
    "/procurement",
    response_model=FinanceProcurementResponse,
    summary="Finance A 전사 매입 가능 금액 Band 조회",
)
def review_finance_procurement(request: PurchaseAgentOutput) -> FinanceProcurementResponse:
    """Purchase Agent v0.4 Context와 현재 Finance State로 공통 Band를 반환한다."""
    return run_finance_procurement(request)


@router.post(
    "/sales",
    response_model=FinanceSalesResponse,
    summary="Finance B 판매 현금 회수 가이드 조회",
)
def review_finance_sales(request: FinanceSalesRequest) -> FinanceSalesResponse:
    """승인 매입 지급 의무를 반영한 판매 회수 가이드를 반환한다."""
    return run_finance_sales(request)
