"""대시보드 주소. 소유: 마스터."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dashboard.query import build
from app.api.dashboard.schema import DashboardTab

router = APIRouter(prefix="/dashboard", tags=["api:dashboard"])


@router.get("", response_model=DashboardTab, summary="대시보드")
def dashboard_tab(as_of: Annotated[date, Query(description="기준일")]) -> DashboardTab:
    return build(as_of)
