"""재무·자금 Agent API 라우터."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.finance import messages
from app.finance.adapter import finance_port
from app.finance.execution import get_finance_execution, get_finance_run, list_finance_runs
from app.finance.legacy.deterministic_service import run_finance_sales
from app.finance.schemas import (
    FinalVerdict,
    FinanceAgentRunResponse,
    FinanceCycle,
    FinanceSalesRequest,
    FinanceSalesResponse,
    RuntimeStatus,
)
from app.master.envelope import AgentReply, AgentRequest

router = APIRouter(prefix="/finance", tags=["finance"])


@router.post("/agent", summary="Finance v2.2 Tool-Using Agent")
def run_finance_agent(request: AgentRequest) -> AgentReply:
    """Master와 동일한 Finance Port를 통해 Agent를 실행한다."""
    reply, _metadata = finance_port(request)
    return reply


@router.get("/agent/runs/{run_id}", summary="Finance v2.2 execution metadata")
def get_finance_execution_by_id(run_id: UUID) -> dict[str, object]:
    try:
        return get_finance_execution(run_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=messages.RUN_NOT_FOUND) from error


@router.post(
    "/sales",
    response_model=FinanceSalesResponse,
    summary="Finance B 판매 현금 회수 가이드 조회",
)
def review_finance_sales(request: FinanceSalesRequest) -> FinanceSalesResponse:
    """승인 매입 지급 의무를 반영한 판매 회수 가이드를 반환한다."""
    return run_finance_sales(request)


@router.get(
    "/runs",
    response_model=list[FinanceAgentRunResponse],
    summary="Finance Agent 실행이력 목록 조회",
)
def get_finance_runs(
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[FinanceAgentRunResponse]:
    """cycle, as_of, runtime_status 필터로 최근 실행이력을 반환한다."""
    return list_finance_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )


@router.get(
    "/runs/{run_id}",
    response_model=FinanceAgentRunResponse,
    summary="Finance Agent 실행이력 단건 조회",
)
def get_finance_run_by_id(run_id: UUID) -> FinanceAgentRunResponse:
    """run_id에 해당하는 실행이력을 반환한다."""
    try:
        return get_finance_run(run_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=messages.RUN_NOT_FOUND,
        ) from error
