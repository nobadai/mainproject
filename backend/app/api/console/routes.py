"""운영 콘솔의 부서 공통 조회 — 지금은 실행 목록 하나다."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.console.runs import ConsoleRunsResponse, get_console_runs

router = APIRouter(prefix="/console", tags=["console"])


@router.get("/runs", response_model=ConsoleRunsResponse)
def runs(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> ConsoleRunsResponse:
    """고를 수 있는 실행 목록. 🔴 읽기 전용 — 실행을 만들거나 고치지 않는다."""
    return get_console_runs(limit=limit)
