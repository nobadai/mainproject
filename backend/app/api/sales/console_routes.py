"""Sales operations-console reads.  The runtime axis is never inferred."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.contracts.aging import AgingBucket
from app.sales.console_collections import ConsoleCollectionsResponse, get_console_collections
from app.sales.console_items import ConsoleItemsResponse, get_console_items
from app.sales.console_lifecycle import ConsoleSaleLifecycle, get_console_sale_lifecycle
from app.sales.console_partners import (
    ConsolePartnerDetailResponse,
    ConsolePartnersResponse,
    get_console_partner_detail,
    get_console_partners,
)
from app.sales.console_proposals import (
    ConsoleSalesProposalsResponse,
    get_console_sales_proposals,
)
from app.sales.console_runs import ConsoleSalesRunsResponse, get_console_sales_runs
from app.sales.console_trend import (
    MAX_TREND_DAYS,
    SalesTrendResponse,
    get_console_sales_trend,
)
from app.sales.dashboard import get_sales_dashboard
from app.sales.schemas import SalesDashboardResponse

router = APIRouter(prefix="/console/sales", tags=["console:sales"])


@router.get("/items", response_model=ConsoleItemsResponse)
def items() -> ConsoleItemsResponse:
    """공용 품목 원장에서 현재 활성인 판매 입력 선택지를 반환한다."""
    return get_console_items()


@router.get("/summary", response_model=SalesDashboardResponse)
def summary(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> SalesDashboardResponse:
    """Stored sales, receivable and item facts for exactly one simulation run."""
    return get_sales_dashboard(sim_run_id=sim_run_id, as_of=as_of)


@router.get("/proposals", response_model=ConsoleSalesProposalsResponse)
def proposals(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> ConsoleSalesProposalsResponse:
    """The sales proposals this run produced on exactly this day.  Reads only."""
    return get_console_sales_proposals(sim_run_id=sim_run_id, as_of=as_of)


@router.get("/trend", response_model=SalesTrendResponse)
def trend(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    days: Annotated[int, Query(ge=1, le=MAX_TREND_DAYS)] = MAX_TREND_DAYS,
    from_date: date | None = None,
    to_date: date | None = None,
) -> SalesTrendResponse:
    """Stored sales folded by date for exactly one requested range."""
    try:
        return get_console_sales_trend(
            sim_run_id=sim_run_id,
            as_of=as_of,
            days=days,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="시작일은 종료일보다 늦을 수 없습니다.",
        ) from error


@router.get("/partners", response_model=ConsolePartnersResponse)
def partners(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    query: str | None = None,
    status: str | None = None,
    partner_type: str | None = None,
) -> ConsolePartnersResponse:
    """Partner masters with this run's own sales and receivable aggregates."""
    return get_console_partners(
        sim_run_id=sim_run_id,
        as_of=as_of,
        query=query,
        status=status,
        partner_type=partner_type,
    )


@router.get("/partners/{partner_id}", response_model=ConsolePartnerDetailResponse)
def partner_detail(
    partner_id: str,
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> ConsolePartnerDetailResponse:
    """One partner seen through one run.  Credit stays Finance's to answer."""
    detail = get_console_partner_detail(sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="거래처를 찾지 못했습니다.")
    return detail


@router.get("/collections", response_model=ConsoleCollectionsResponse)
def collections(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    partner_id: str | None = None,
    aging_bucket: AgingBucket | None = None,
    status: str | None = None,
) -> ConsoleCollectionsResponse:
    """Receivables of one run, aged by the Finance rule.  No second aging vocabulary."""
    return get_console_collections(
        sim_run_id=sim_run_id,
        as_of=as_of,
        partner_id=partner_id,
        aging_bucket=aging_bucket,
        status=status,
    )


@router.get("/runs", response_model=ConsoleSalesRunsResponse)
def runs(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date | None = None,
    partner_id: str | None = None,
    item: str | None = None,
    runtime_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConsoleSalesRunsResponse:
    """Sales execution history for one simulation run.  Reads only — never re-runs."""
    return get_console_sales_runs(
        sim_run_id=sim_run_id,
        as_of=as_of,
        partner_id=partner_id,
        item=item,
        runtime_status=runtime_status,
        limit=limit,
    )


@router.get("/{sale_id}/lifecycle", response_model=ConsoleSaleLifecycle)
def sale_lifecycle(
    sale_id: str,
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> ConsoleSaleLifecycle:
    """판매 한 건의 흐름. 🔴 **저장된 연결키로만 잇는다** — 날짜·품목 추정 금지."""
    lifecycle = get_console_sale_lifecycle(sim_run_id=sim_run_id, sale_id=sale_id, as_of=as_of)
    if lifecycle is None:
        raise HTTPException(status_code=404, detail="판매를 찾지 못했습니다.")
    return lifecycle
