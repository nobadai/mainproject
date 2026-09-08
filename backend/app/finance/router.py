"""재무·자금 Agent API 라우터."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.finance import messages
from app.finance.adapter import finance_port
from app.finance.dashboard_service import get_finance_cashflow, get_finance_dashboard
from app.finance.execution import get_finance_execution, get_finance_run, list_finance_runs
from app.finance.schemas import (
    FinalVerdict,
    FinanceAgentRunResponse,
    FinanceCashflowResponse,
    FinanceCycle,
    FinanceDashboardResponse,
    RuntimeStatus,
)
from app.master.envelope import AgentReply, AgentRequest

router = APIRouter(prefix="/finance", tags=["finance"])


@router.get(
    "/dashboard",
    response_model=FinanceDashboardResponse,
    summary="재무 Dashboard 조회",
)
def read_finance_dashboard(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> FinanceDashboardResponse:
    """저장된 재무·현금·채권·채무 원장 사실만 집계해 반환한다."""
    return get_finance_dashboard(sim_run_id=sim_run_id, as_of=as_of, recent_limit=limit)


@router.get(
    "/dashboard/cashflow",
    response_model=FinanceCashflowResponse,
    summary="재무 Dashboard 일별 현금흐름 조회",
)
def read_finance_dashboard_cashflow(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    days: Annotated[int, Query(ge=1, le=30)] = 30,
) -> FinanceCashflowResponse:
    """최근 최대 30일의 저장된 일마감 현금흐름을 날짜 오름차순으로 반환한다."""
    return get_finance_cashflow(sim_run_id=sim_run_id, as_of=as_of, days=days)


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
