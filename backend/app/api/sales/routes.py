"""판매 탭 주소. 소유: 판매 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.sales.query import build
from app.api.sales.schema import SalesTab

router = APIRouter(prefix="/sales", tags=["api:sales"])


@router.get("", response_model=SalesTab, summary="판매 탭 (조회 전용)")
def sales_tab(as_of: Annotated[date, Query(description="기준일")]) -> SalesTab:
    return build(as_of)
