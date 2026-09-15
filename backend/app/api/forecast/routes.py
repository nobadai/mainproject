"""가격 예측 탭 주소. 소유: ML 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.forecast.query import ITEMS, KINDS, build
from app.api.forecast.schema import ForecastTab

router = APIRouter(prefix="/forecast", tags=["api:forecast"])


@router.get("", response_model=ForecastTab, summary="가격 예측 탭")
def forecast_tab(
    as_of: Annotated[date, Query(description="기준일. base_dt 를 안 주면 이 날 이하의 최신")],
    item: Annotated[str, Query(description="품목")] = "배추",
    kind: Annotated[str, Query(description="auc 경락가 · whsl 중도매가 · rtl 소매가")] = "auc",
    base_dt: Annotated[
        str | None,
        Query(description="예측을 만든 날을 콕 집을 때. 지난 날짜는 시연·되짚기용"),
    ] = None,
) -> ForecastTab:
    if item not in ITEMS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"지원하지 않는 품목입니다: {item}. 선택 가능: {', '.join(ITEMS)}",
        )
    if kind not in KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"없는 가격 종류입니다: {kind}. 선택 가능: {', '.join(KINDS)}",
        )
    return build(as_of, item, kind, base_dt)
