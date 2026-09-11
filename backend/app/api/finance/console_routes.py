"""Finance operations-console reads.  Every query carries its runtime axis."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.finance.dashboard import get_finance_cashflow, get_finance_dashboard
from app.finance.schemas import FinanceCashflowResponse, FinanceDashboardResponse

router = APIRouter(prefix="/console/finance", tags=["console:finance"])


@router.get("/summary", response_model=FinanceDashboardResponse)
def summary(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> FinanceDashboardResponse:
    """Stored finance facts only; no Burn-in default and no projection recomputation."""
    return get_finance_dashboard(sim_run_id=sim_run_id, as_of=as_of)


@router.get("/cashflow", response_model=FinanceCashflowResponse)
def cashflow(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    days: Annotated[int, Query(ge=1, le=30)] = 30,
) -> FinanceCashflowResponse:
    return get_finance_cashflow(sim_run_id=sim_run_id, as_of=as_of, days=days)
