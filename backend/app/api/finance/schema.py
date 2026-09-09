"""재무 탭이 받는 모양.

소유: **재무 파트.**

★ 이 탭은 **조회 전용**입니다. 에이전트를 돌리지 않고 저장된 값만 봅니다.
  화면을 열 때마다 에이전트가 돌면 느리고, 볼 때마다 답이 달라집니다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import Card, Chart, Note, Source, Stat, Table


class StateOption(BaseModel):
    """저장된 재무 기준 상태 하나."""

    key: str = Field(description="base · loan 처럼 주소에 실리는 값")
    label: str = Field(description="화면에 보일 이름")
    explain: str = Field(description="이 상태가 무엇인지 한 문단")


class FlowCell(BaseModel):
    """돈이 어디로 나가고 들어왔나 — 한 칸."""

    label: str = Field(description="원장 용어 말고 사람 말로")
    value: str = Field(description="금액. 자릿점까지 넣어서")
    tone: str = Field(default="neutral", description="neutral · good · warn · bad")


class FinanceTab(BaseModel):
    states: list[StateOption] = Field(description="실제로 저장된 재무 상태")
    selected: str = Field(description="대표로 요약한 상태")
    requested_as_of: str = Field(description="사용자가 요청한 기준일")
    state_as_of: str | None = Field(description="실제로 조회된 재무 상태 기준일")
    latest_closing_as_of: str | None = Field(description="최근 일마감 기준일")
    stats: list[Stat] = Field(description="현금 · 최소 운영자금 · 받을 돈 · 부채")
    state_cards: list[Card] = Field(description="저장된 재무 상태 비교 카드")
    explain: Note = Field(description="고른 상태가 지금 어떤 뜻인가")
    read_only: Note = Field(description="이 화면이 조회 전용이라는 안내")
    cash_chart: Chart = Field(description="최근 일별 현금")
    flows: list[FlowCell] = Field(description="기준일까지 누적 자금 흐름")
    balances: list[Stat] = Field(description="받을 돈 · 줄 돈")
    balances_note: Note = Field(description="미지급이 없으면 경고 대신 «정산 완료» 로")
    closings: Table = Field(description="최근 일별 마감")
    source: Source = Field(description="예시값인지 실제 값인지")
