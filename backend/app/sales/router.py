"""영업 Agent API Router."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError

from app.sales.partner_profile import (
    FOREIGN_FIELDS,
    PartnerProfile,
    PartnerProfileUpdate,
    get_partner_profile,
    update_partner_profile,
)
from app.sales.proposal import run_proposal
from app.sales.runs import get_sales_run, list_sales_runs
from app.sales.schemas import (
    RuntimeStatus,
    SalesAgentRunResponse,
    SalesCycle,
    SalesProposalInput,
    SalesProposalReply,
)

router = APIRouter(prefix="/sales", tags=["sales"])


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


@router.get(
    "/partners/{partner_id}/profile",
    response_model=PartnerProfile,
    summary="거래처 기본정보 조회",
)
def read_partner_profile(partner_id: str) -> PartnerProfile:
    """거래처 원장 행 그대로. 🔴 여신 한도는 여기 없다 — 재무 정본이다."""
    profile = get_partner_profile(partner_id=partner_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="거래처를 찾지 못했습니다."
        )
    return profile


@router.patch(
    "/partners/{partner_id}/profile",
    response_model=PartnerProfile,
    summary="거래처 기본정보 수정",
)
def edit_partner_profile(
    partner_id: str, body: dict[str, object]
) -> PartnerProfile:
    """준 칸만 고치고 **저장된 결과**를 돌려준다.

    🔴 **남의 도메인 값은 조용히 무시하지 않고 거절한다.** 무시하면 사용자는 고쳐진
       줄 알고 화면을 닫는다 — 여신 한도가 특히 그렇다.
    """
    foreign = sorted(name for name in body if name in FOREIGN_FIELDS)
    if foreign:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=" ".join(FOREIGN_FIELDS[name] for name in foreign),
        )
    try:
        update = PartnerProfileUpdate.model_validate(body)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    profile = update_partner_profile(partner_id=partner_id, update=update)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="거래처를 찾지 못했습니다."
        )
    return profile
