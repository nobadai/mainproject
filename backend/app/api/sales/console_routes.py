"""Sales operations-console reads.  The runtime axis is never inferred."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.sales.dashboard import get_sales_dashboard
from app.sales.schemas import SalesDashboardResponse

router = APIRouter(prefix="/console/sales", tags=["console:sales"])


@router.get("/summary", response_model=SalesDashboardResponse)
def summary(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> SalesDashboardResponse:
    """Stored sales, receivable and item facts for exactly one simulation run."""
    return get_sales_dashboard(sim_run_id=sim_run_id, as_of=as_of)
