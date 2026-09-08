"""매입 탭 주소. 소유: 매입 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.purchase.query import build
from app.api.purchase.schema import PurchaseTab

router = APIRouter(prefix="/purchase", tags=["api:purchase"])


@router.get("", response_model=PurchaseTab, summary="매입 탭")
def purchase_tab(as_of: Annotated[date, Query(description="기준일")]) -> PurchaseTab:
    return build(as_of)
