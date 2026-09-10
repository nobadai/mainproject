"""재고 · 물류 탭 주소. 소유: 물류 파트."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.logistics.query import PANES, build_result
from app.api.logistics.schema import LogisticsTab

router = APIRouter(prefix="/logistics", tags=["api:logistics"])


@router.get("", response_model=LogisticsTab, summary="재고 · 물류 탭")
def logistics_tab(
    as_of: Annotated[date, Query(description="기준일")],
    response: Response,
    pane: Annotated[str, Query(description="안쪽 작은 탭")] = "stock",
) -> LogisticsTab:
    """재고·물류 탭 한 판.

    🔴 **읽기 실패를 `200 OK` 로 내보내지 않는다.** 본문에 `Source.status="ERROR"` 를
       적어도 HTTP 가 200 이면 그 응답은 **성공으로 캐시되고 성공으로 집계되고 성공으로
       재시도되지 않는다.**

    ```text
    OK · NO_DATA          200   읽었다. 값이 있거나, 이 실행이 안 연 날이다
    ERROR · DB 접속 실패   503   지금은 못 읽는다 — 다시 오면 될 수 있다
    ERROR · 그 밖          500   이 요청이 여기서 깨졌다
    ```

    ★ **본문은 오류일 때도 `LogisticsTab` 그대로다.** `HTTPException` 으로 바꾸면
      `{"detail": …}` 만 남아 화면이 «왜» 를 못 그린다 — 코드만 바꾸고 몸통은 준다.
      그래서 `Response.status_code` 를 직접 세운다.

    ⚠️ **없는 pane 은 그와 다르다.** 그것은 요청이 틀린 것이라 400 이고, 몸통을
       줄 이유가 없다.
    """
    if pane not in PANES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"없는 화면입니다: {pane}. 가능: {', '.join(PANES)}",
        )
    result = build_result(as_of, pane)
    response.status_code = result.http_status
    return result.tab
