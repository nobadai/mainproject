"""재고 · 물류 탭이 받는 모양.

소유: **물류 파트.**

★ 이 탭은 안에서 넷으로 또 나뉩니다 (재고·예약 / 입고 / 창고 / 출고).
  카드가 열다섯 장이라 **목록으로 받습니다** — 카드를 하나 더해도 화면은
  안 고칩니다.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.api.primitives import Note, Pane, Source


class LogisticsTab(BaseModel):
    panes: list[Pane]
    selected: str
    principle: Note
    source: Source
