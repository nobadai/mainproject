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

    🔴 **상한이 둘이다. 09-08 에 갈렸다** (`#398` · `dev@a615aa6`). 지금은 같은 값이고,
    **컷 산식을 바꾸는 날** 갈라진다.

    .. code-block:: text

        max_price       재무 STRESS 로 나간다 — 남이 등식을 검사한다
                        finance/capabilities/scenario.py  amount_max_krw 등식
                        master/verifier.py                검사 이름 L-PAYSCHED-MAX
        cut_unit_price  우리 컷 (self_check.check_max_price)

    🔴 ~~`09-17` 에 밴드가 바뀌면 갈라진다~~ — **낡았다** (ML 회신 2026-09-10). 밴드
    교체는 `09-03` 에 끝났고 `09-17` 은 «그림자 기록 2주가 차는 날» 이다. 계기는 날짜가
    아니라 **우리가 산식을 바꾸는 것**이다.

    ⚠️ 화면이 「이보다 비싸면 안 산다」 자리에 ``max_price`` 를 보이면 그날부터 조용히
    틀린 값이 뜬다. 그 자리는 ``cut_unit_price`` 다.
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
    #  🔴 **`None` 은 «못 읽었다» 가 아니라 «걷기 밖» 이다.** 축이 붙기 전에 만든
    #     실행이거나 손으로 돌린 것이고, 그 사실이 화면에 보여야 한다 (마스터 청구
    #     2026-09-10). 걷기와 손 실행이 **같아 보이면** 보는 사람이 둘을 한 세상으로
    #     읽는다 — 마스터가 실제로 그 오독을 했다.
    sim_run_id: str | None = Field(
        default=None,
        description="어느 걷기의 실행인가. None 이면 걷기 밖(손 실행·축이 생기기 전)이다",
    )


class PurchaseTab(BaseModel):
    stats: list[Stat] = Field(description="오늘 제안 · 승인 대기 · 확정 매입액 · 입고 예정")
    plans: list[Plan] = Field(description="오늘 낸 안들. 비면 화면이 «안이 없다» 고 적는다")
    plans_note: Note = Field(description="안이 왜 이 개수인가")
    #  🔴 **«사람이» 라고 쓰지 않는다** (2026-09-10 · 마스터 통보 「백필 승인은 사람 승인이
    #     아닙니다」). 걷기 구간을 `decided_by="AUTO-BACKFILL"` 로 채우기로 정해졌다.
    #     그날부터 이 표에는 **사람이 누른 것과 자동으로 채운 것이 같이 실린다** —
    #     «사람이 고른 뒤에» 는 그때 화면에서 거짓말이 된다.
    #
    #  ⚠️ 지금 고치는 이유는 «미리 맞춰 두려고» 가 아니다. **둘 다 참인 문장이 있어서**다 —
    #     승인을 거친다는 것은 지금도 참이고 백필 뒤에도 참이다. 주체를 단정한 쪽만 깨진다.
    #
    #  🟡 «누가 승인했나» 를 이 표에 칸으로 더하는 것은 **다음 판**이다. 원장에 그 값이
    #     없고(`purchases` 에 승인자 칸 없음), 마스터가 `purchases.decision_id`(FK)로
    #     `master_decisions` 를 가리키게 하기로 정했다. 칸이 선 뒤에 조인해 읽는다.
    committed: Table = Field(description="승인을 거친 뒤에 생기는 확정 매입")
    committed_note: Note = Field(description="승인 전에는 표가 빈다는 안내")
    source: Source = Field(description="예시값인지 실제 값인지")
