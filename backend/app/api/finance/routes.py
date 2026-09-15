"""재무 탭 주소. 소유: 재무 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.finance.query import STATES, build
from app.api.finance.schema import FinanceTab

router = APIRouter(prefix="/finance", tags=["api:finance"])


@router.get("", response_model=FinanceTab, summary="재무 탭 (조회 전용)")
def finance_tab(
    as_of: Annotated[date, Query(description="기준일")],
    state: Annotated[str, Query(description="저장된 재무 기준 상태")] = "base",
) -> FinanceTab:
    if state not in STATES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"없는 재무 보기입니다: {state}. 가능: {', '.join(STATES)}",
        )
    try:
        return build(as_of, state)
    except LookupError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="이 기준일까지 확인할 수 있는 재무 상태가 없습니다.",
        ) from exc
