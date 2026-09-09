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
    text: str = Field(description="근거 한 줄. 숫자를 그대로 적는다")
    ref: str | None = Field(default=None, description="근거 꼬리표")
    carried: bool = Field(default=False, description="어제 판단에서 이어받은 것인가")


class Plan(BaseModel):
    """매입안 하나.

    🔴 **상한이 둘이다. 09-08 에 갈렸다** (`#398` · `dev@a615aa6`). 지금은 같은
    값이지만 `09-17` 에 밴드가 바뀌면 갈라진다.

    .. code-block:: text

        max_price       재무 STRESS 로 나간다 — 남이 등식을 검사한다
                        finance/capabilities/scenario.py  amount_max_krw 등식
                        master/verifier.py                검사 이름 L-PAYSCHED-MAX
        cut_unit_price  우리 컷 (self_check.check_max_price)

    ⚠️ 화면이 「이보다 비싸면 안 산다」 자리에 ``max_price`` 를 보이면 `09-17` 뒤로
    조용히 틀린 값이 뜬다. 그 자리는 ``cut_unit_price`` 다.
    """

    key: str = Field(description="보수 · 기본 · 공격")
    coverage: str = Field(description="며칠치인가")
    knob: str = Field(description="무엇으로 조절한 안인가")
    qty_kg: float = Field(description="사는 양 (kg)")
    amount_krw: int = Field(description="예상 금액 (원)")
    unit_price: int = Field(description="등급 단가 (원/kg)")
    grade: str = Field(description="배정된 등급")
    max_price: int = Field(description="재무 스트레스 기준 (amount_max_krw = qty × 이것)")
    cut_unit_price: int | None = Field(
        default=None,
        description="🔴 컷 기준 — 이보다 비싸면 안 산다. 없으면 그 실행에 칸이 없던 것이다",
    )
    legs: Table = Field(description="회차 — 언제 사서 언제 오나")
    payments: Table = Field(description="언제 얼마 내나")
    reasons: list[Reason] = Field(description="이 안을 왜 냈나. 여섯 갈래를 다 채운다")
    risks: list[str] = Field(description="걸리는 것. 비어 있으면 안 적는다")
    pending: bool = Field(description="아직 사람이 안 고른 안인가")
    approved: bool = Field(default=False, description="이미 승인된 안인가")


class PurchaseTab(BaseModel):
    stats: list[Stat] = Field(description="오늘 제안 · 승인 대기 · 확정 매입액 · 입고 예정")
    plans: list[Plan] = Field(description="오늘 낸 안들. 비면 화면이 «안이 없다» 고 적는다")
    plans_note: Note = Field(description="안이 왜 이 개수인가")
    committed: Table = Field(description="사람이 고른 뒤에 생기는 확정 매입")
    committed_note: Note = Field(description="승인 전에는 표가 빈다는 안내")
    source: Source = Field(description="예시값인지 실제 값인지")
