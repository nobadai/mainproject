"""매입 탭이 받는 모양.

소유: **매입 파트.** 모양을 바꾸려면 화면 담당(ML)과 같이 봅니다 — 칸 이름을
바꾸면 화면이 조용히 빈 칸이 됩니다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import Note, Source, Stat, Table


class Reason(BaseModel):
    """이 안을 왜 냈나 — 한 줄.

    ★ `ref` 를 반드시 채웁니다. 근거에 꼬리표가 없으면 나중에 되짚을 수
      없습니다. 매입 파트가 이미 `FC-…` · `MQ-…` 형태로 쓰고 있습니다.
    """

    source: str = Field(description="예측 · 시세 · 주문 · 재고 · 현금 · 창고")
    text: str
    ref: str | None = Field(default=None, description="근거 꼬리표")
    carried: bool = Field(default=False, description="어제 판단에서 이어받은 것인가")


class Plan(BaseModel):
    """매입안 하나."""

    key: str = Field(description="보수 · 기본 · 공격")
    coverage: str = Field(description="며칠치인가")
    knob: str = Field(description="무엇으로 조절한 안인가")
    qty_kg: float
    amount_krw: int
    unit_price: int
    grade: str
    max_price: int = Field(description="이보다 비싸면 안 산다")
    legs: Table = Field(description="회차 — 언제 사서 언제 오나")
    payments: Table = Field(description="언제 얼마 내나")
    reasons: list[Reason]
    risks: list[str] = Field(description="걸리는 것. 비어 있으면 안 적는다")
    pending: bool = Field(description="아직 사람이 안 고른 안인가")
    approved: bool = False


class PurchaseTab(BaseModel):
    stats: list[Stat]
    plans: list[Plan]
    plans_note: Note = Field(description="안이 왜 이 개수인가")
    committed: Table = Field(description="사람이 고른 뒤에 생기는 확정 매입")
    committed_note: Note
    source: Source
