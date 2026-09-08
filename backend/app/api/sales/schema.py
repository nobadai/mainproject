"""판매 탭이 받는 모양.

소유: **판매 파트.**

★ 재무 탭과 마찬가지로 **조회 전용**입니다. 시나리오를 돌리지 않고 저장된
  판매 · 수금 결과만 봅니다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import Card, Note, Source, Stat


class SalesTab(BaseModel):
    stats: list[Stat] = Field(description="총 판매금액 · 판매량 · 공헌이익 · 아직 받을 돈")
    read_only: Note = Field(description="이 화면이 조회 전용이라는 안내")
    cards: list[Card] = Field(description="카드 목록. 더해도 화면은 안 고친다")
    source: Source = Field(description="예시값인지 실제 값인지")
