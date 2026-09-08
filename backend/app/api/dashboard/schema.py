"""대시보드가 받는 모양.

소유: **마스터.** 다만 값은 만들지 않습니다 — 다섯 부서 값을 **날짜 축에
놓기만** 합니다. 데모 사이드바에 적힌 그대로입니다.

    마스터는 숫자를 만들지 않는다.
    부서 값을 날짜 축에 놓고, 없으면 공란으로 둔다.

★ 그래서 이 탭의 `query.py` 에는 계산이 없습니다. 다른 탭의 `build()` 를
  불러서 골라 담기만 합니다. **숫자가 두 군데서 달라지는 일을 막습니다.**
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.forecast.schema import ItemCard
from app.api.primitives import Badge, CalendarAxis, Chart, Note, Source, Stat, Table


class DashboardTab(BaseModel):
    axis: CalendarAxis = Field(description="여섯 탭이 함께 쓰는 날짜축. 그래프 계열 길이의 기준")
    badges: list[Badge] = Field(description="상단 알약 — 오늘 상태")
    stats: list[Stat] = Field(description="맨 위 요약 다섯 칸. **다섯 파트에서 하나씩 가져온다**")
    forecast_cards: list[ItemCard] = Field(description="가격 예측 탭과 **같은 값**")
    purchase: Table = Field(description="승인을 기다리는 안")
    purchase_note: Note = Field(description="상한가가 안마다 다르다는 안내")
    cash_chart: Chart = Field(description="재무의 dashboard_cash() 가 만든 것. 여기서 만들지 말 것")
    stock_chart: Chart = Field(
        description="물류의 dashboard_stock() 가 만든 것. 여기서 만들지 말 것"
    )
    sources: list[Source] = Field(description="탭마다 채워졌나 — 하나라도 예시면 화면이 알린다")
