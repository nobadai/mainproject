"""가격 예측 탭 주소. 소유: ML 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.forecast.query import ITEMS, build
from app.api.forecast.schema import ForecastTab

router = APIRouter(prefix="/forecast", tags=["api:forecast"])


@router.get("", response_model=ForecastTab, summary="가격 예측 탭")
def forecast_tab(
    as_of: Annotated[date, Query(description="기준일")],
    item: Annotated[str, Query(description="품목")] = "배추",
) -> ForecastTab:
    if item not in ITEMS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"지원하지 않는 품목입니다: {item}. 가능: {', '.join(ITEMS)}",
        )
    return build(as_of, item)
