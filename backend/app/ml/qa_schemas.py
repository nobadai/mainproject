"""예측 질의응답 — 요청·응답 계약.

★ 돌려주는 것은 **둘**이다.

    markdown   채팅에 **그대로** 붙일 글. 마스터가 다시 요약하지 않는다
    meta       봉투에 실을 출처와 상태. 마크다운을 파싱하지 말 것

🔴 `status` 가 `OK` 가 아니어도 `markdown` 은 **항상 찬다.** 못 한 것이 한 것처럼
   보이지 않게 하려는 것이다 — 빈 답을 돌려주면 부르는 쪽이 «안 물어봤다» 와
   «못 답했다» 를 구분할 수 없다.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.ml.schemas import ITEMS, TargetKind

#: 답할 수 있는 범위. **마스터 라우팅 목록과 같은 값이어야 한다.**
QA_ITEMS = ITEMS
QA_KINDS: tuple[str, ...] = ("AUC", "WHSL", "RTL")

#: 오늘(리드 0) ~ 18일 뒤. 전달표는 D+1~D+18 이고 당일은 원본 창고에서 읽는다.
QA_MIN_OFFSET = 0
QA_MAX_OFFSET = 18

QaStatus = Literal[
    "OK",                  # 전부 답했다
    "PARTIAL",             # 일부 날짜만 답했다 — 하나가 없다고 전체를 버리지 않는다
    "NEED_CLARIFY",        # 되물어야 한다
    "OUT_OF_SCOPE",        # 우리 소관이 아니다
    "NO_DATA",             # 범위 안인데 그 행이 없다
    "SOURCE_UNAVAILABLE",  # 창고를 못 읽었다 — 예시값으로 떨어지지 않는다
    "LLM_UNAVAILABLE",     # 질문을 해석하지 못했다
]


class QaRequest(BaseModel):
    """질문 하나.

    ★ **두 가지 방법으로 부를 수 있다.**

        question 만 준다            → LLM 이 해석한다 (supervise)
        item·kind·dates 를 준다     → 해석을 건너뛰고 바로 조회한다

    둘째 길을 열어 둔 이유는, LLM 없이도 **값과 서식을 사람이 직접 확인**할 수
    있어야 하기 때문이다. 계약이 바뀌어도 버려지지 않는 부분이 여기다.
    """

    question: str | None = Field(default=None, description="사용자가 채팅에 친 그대로")
    item: str | None = Field(default=None, description="배추 · 무 · 양파")
    kind: TargetKind | None = Field(
        default=None, description="AUC 경락가 · WHSL 중도매가 · RTL 소매가"
    )
    dates: list[date] | None = Field(default=None, description="대상일 목록. 없으면 내일 하루")
    as_of: date | None = Field(default=None, description="기준일. 없으면 전달표의 최신 기준일")


class QaMeta(BaseModel):
    """출처와 상태. **마스터는 이 값을 봉투에 싣는다.**"""

    status: QaStatus
    item: str | None = None
    kind: str | None = None
    base_dt: date | None = None
    targets: list[date] = Field(default_factory=list)
    missing: list[date] = Field(default_factory=list, description="범위 안인데 행이 없던 날")
    out_of_range: list[date] = Field(default_factory=list, description="예측 범위 밖이던 날")
    model_version: str | None = None
    generated_at: str | None = None
    source: str | None = Field(
        default=None, description="ml_price_forecasts | prediction_log | 둘 다"
    )
    is_filled: list[bool] = Field(default_factory=list)
    band_method: str | None = None
    use_recommended: bool | None = None


class QaAnswer(BaseModel):
    """돌려주는 것 — 마크다운 한 덩어리 + 기계용 값."""

    markdown: str
    meta: QaMeta
