"""매입 탭 주소. 소유: 매입 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.purchase.query import build
from app.api.purchase.schema import PurchaseTab

router = APIRouter(prefix="/purchase", tags=["api:purchase"])


@router.get("", response_model=PurchaseTab, summary="매입 탭")
def purchase_tab(
    as_of: Annotated[date, Query(description="기준일")],
    sim_run_id: Annotated[
        str | None,
        Query(description="걷기 축(sim_runs.sim_run_id). 안 주면 전부 봅니다"),
    ] = None,
) -> PurchaseTab:
    #  🔴 값을 여기서 짓지 않습니다 — 받아서 그대로 흘립니다. 상수를 박으면 축이
    #     갈리는 날 이 파일을 고쳐야 하고, 그때 다른 탭과 갈립니다.
    return build(as_of, sim_run_id)
