"""재무 탭이 받는 모양.

소유: **재무 파트.**

★ 이 탭은 **조회 전용**입니다. 에이전트를 돌리지 않고 저장된 값만 봅니다.
  화면을 열 때마다 에이전트가 돌면 느리고, 볼 때마다 답이 달라집니다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import Chart, Note, Source, Stat, Table


class StateOption(BaseModel):
    """저장된 재무 기준 상태 하나.

    ★ **이건 "대출 승인 결과" 가 아닙니다.** DB 에 저장된 기준 상태입니다.
      데모에도 그렇게 적혀 있습니다 — 헷갈리면 안 됩니다.
    """

    key: str
    label: str
    explain: str = Field(description="이 상태가 무엇인지 한 문단")


class FlowCell(BaseModel):
    """돈이 어디로 나가고 들어왔나 — 한 칸."""

    label: str = Field(description="원장 용어 말고 사람 말로")
    value: str
    term: str = Field(description="원래 회계 용어. 작게 같이 보인다")
    tone: str = "neutral"


class FinanceTab(BaseModel):
    states: list[StateOption]
    selected: str
    stats: list[Stat]
    explain: Note = Field(description="고른 상태가 지금 어떤 뜻인가")
    read_only: Note = Field(description="이 화면이 조회 전용이라는 안내")
    cash_chart: Chart = Field(description="한 달 일별 현금")
    flows: list[FlowCell]
    balances: list[Stat] = Field(description="받을 돈 · 줄 돈")
    balances_note: Note
    closings: Table = Field(description="최근 일별 마감")
    tables_read: list[str] = Field(description="어느 표를 읽었나. 화면 아래에 적는다")
    source: Source
