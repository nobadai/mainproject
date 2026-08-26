"""재무·자금 Agent API 라우터."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.finance.agent_v22 import FinanceAgentController
from app.finance.repository import PostgresFinanceAsOfDataPort
from app.finance.run_repository import get_finance_v22_run
from app.finance.schemas import (
    FinalVerdict,
    FinanceAgentRunResponse,
    FinanceCycle,
    FinanceProcurementResponse,
    FinanceReviewRequest,
    FinanceSalesRequest,
    FinanceSalesResponse,
    PurchaseAgentOutput,
    RuntimeStatus,
)
from app.finance.service import (
    FinanceCoreResult,
    get_finance_run,
    list_finance_runs,
    run_finance_core,
    run_finance_procurement,
    run_finance_sales,
)
from app.master.envelope import AgentReply, AgentRequest

router = APIRouter(prefix="/finance", tags=["finance"])


@router.post("/agent", summary="Finance v2.2 Tool-Using Agent")
def run_finance_agent(request: AgentRequest) -> AgentReply:
    """Primary v2.2 entrypoint; it has no Snapshot/T0 boundary."""
    reply, _metadata = FinanceAgentController(PostgresFinanceAsOfDataPort()).run(request)
    return reply


@router.get("/agent/runs/{run_id}", summary="Finance v2.2 execution metadata")
def get_finance_agent_v22_run(run_id: UUID) -> dict[str, object]:
    try:
        return get_finance_v22_run(run_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Finance v2.2 run was not found") from error


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
            detail="Finance Agent run was not found",
        ) from error
