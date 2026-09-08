"""재고 · 물류 탭 주소. 소유: 물류 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.logistics.query import PANES, build
from app.api.logistics.schema import LogisticsTab

router = APIRouter(prefix="/logistics", tags=["api:logistics"])


@router.get("", response_model=LogisticsTab, summary="재고 · 물류 탭")
def logistics_tab(
    as_of: Annotated[date, Query(description="기준일")],
    pane: Annotated[str, Query(description="안쪽 작은 탭")] = "stock",
) -> LogisticsTab:
    if pane not in PANES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"없는 화면입니다: {pane}. 가능: {', '.join(PANES)}",
        )
    return build(as_of, pane)
