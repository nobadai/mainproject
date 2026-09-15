"""매입 탭 주소. 소유: 매입 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.purchase.query import build
from app.api.purchase.schema import PurchaseTab
from app.api.shown_run import SHOWN_SIM_RUN_ID

router = APIRouter(prefix="/purchase", tags=["api:purchase"])


@router.get("", response_model=PurchaseTab, summary="매입 탭")
def purchase_tab(
    as_of: Annotated[date, Query(description="기준일")],
    sim_run_id: Annotated[
        str | None,
        Query(
            description=(
                "걷기 축(sim_runs.sim_run_id). 안 주면 화면이 보는 실행"
                "(app/api/shown_run.py SHOWN_SIM_RUN_ID)을 봅니다"
            )
        ),
    ] = None,
) -> PurchaseTab:
    #  🔴 값을 여기서 짓지 않습니다 — 주면 준 값을 그대로 흘립니다.
    #  ★ 안 주면 다른 네 탭과 같은 실행을 봅니다. 그 값의 주인은 `app/api/shown_run.py`
    #    하나이고, 여기서는 가리키기만 합니다. `build()` 의 기본값(None · 전부)은
    #    그대로 둡니다 — 채우는 자리는 이 라우터뿐입니다.
    return build(as_of, SHOWN_SIM_RUN_ID if sim_run_id is None else sim_run_id)
