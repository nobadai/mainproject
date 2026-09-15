"""영업 Agent API Router."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError

from app.sales.partner_profile import (
    FOREIGN_FIELDS,
    PartnerAlreadyExists,
    PartnerProfile,
    PartnerProfileCreate,
    PartnerProfileUpdate,
    create_partner_profile,
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


@router.post(
    "/partners",
    response_model=PartnerProfile,
    status_code=status.HTTP_201_CREATED,
    summary="거래처 등록",
)
def add_partner_profile(body: dict[str, object]) -> PartnerProfile:
    """새 거래처를 만들고 **저장된 행**을 돌려준다.

    ★ 실행 축(`sim_run_id`)을 받지 않는다. 거래처는 실행과 무관한 원장 행이라
      «A 실행의 거래처» 라는 개념이 없다 — `update_partner_profile` 과 같은 규율이다.

    🔴 **여신 한도는 여기서 만들지 않는다.** 정본은 재무의 `partner_credit_limits`
       이고, 같은 이름의 칸을 거래처 행에 두면 두 곳이 다른 한도를 말하는 날이 온다.
    """
    foreign = sorted(name for name in body if name in FOREIGN_FIELDS)
    if foreign:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=" ".join(FOREIGN_FIELDS[name] for name in foreign),
        )
    try:
        create = PartnerProfileCreate.model_validate(body)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_readable(error)
        ) from error
    try:
        return create_partner_profile(create=create)
    except PartnerAlreadyExists as error:
        #  🔴 409 다. 400 으로 내면 화면이 «입력이 틀렸다» 로 읽어 칸을 빨갛게 만든다 —
        #     틀린 것은 칸이 아니라 이미 그 코드가 쓰이고 있다는 사실이다.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"이미 등록된 거래처 코드입니다: {create.partner_id}",
        ) from error


#: 칸 이름을 사용자가 읽는 말로 바꾼다. **값은 바꾸지 않는다.**
_FIELD_LABELS = {
    "partner_id": "내부 거래처 코드",
    "partner_name": "거래처명",
    "partner_type": "거래처 유형",
    "sales_collection_days": "결제일수",
}


def _readable(error: ValidationError) -> str:
    """Pydantic 오류를 사용자 문장으로 옮긴다. **원인을 숨기지 않는다.**

    ⚠️ `str(error)` 를 그대로 내면 `1 validation error for PartnerProfileCreate` 같은
      내부 모델 이름이 화면에 뜬다. 어느 칸이 왜 막혔는지는 그대로 나른다.
    """
    lines = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "입력"
        lines.append(f"{_FIELD_LABELS.get(field, field)}: {item['msg']}")
    return " / ".join(lines)


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
