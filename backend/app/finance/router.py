"""재무·자금 Agent API 라우터."""

from fastapi import APIRouter

from app.finance.schemas import FinanceReviewRequest
from app.finance.service import FinanceCoreResult, run_finance_core

router = APIRouter(prefix="/finance", tags=["finance"])


@router.post(
    "/core-review",
    response_model=FinanceCoreResult,
    summary="Finance P0 deterministic core 검증",
)
def review_finance_core(request: FinanceReviewRequest) -> FinanceCoreResult:
    """LLM 이전의 Repository → Tools → Rules 흐름을 검증한다."""
    return run_finance_core(request)
