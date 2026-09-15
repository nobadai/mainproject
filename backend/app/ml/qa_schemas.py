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

from pydantic import BaseModel, Field, field_validator

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

    #   ★ Swagger «Try it out» 이 채워 주는 기본 본문이다.
    #     예시를 안 두면 화면이 item 에 "string" 을 넣어 주고, 그러면 우리가
    #     그 값을 품목으로 읽어 «우리 소관이 아닙니다» 를 답한다. 실제로 그랬다.
    model_config = {
        "json_schema_extra": {
            "examples": [
                {"question": "내일 배추 경락가 얼마야?"},
                {"item": "무", "kind": "RTL", "dates": ["2026-09-15"]},
            ]
        }
    }

    question: str | None = Field(default=None, description="사용자가 채팅에 친 그대로")
    item: str | None = Field(default=None, description="배추 · 무 · 양파")
    #   ★ 여럿을 직접 줄 수도 있다 (2026-09-15). `item` 은 하나짜리 옛 이름으로 남긴다 —
    #     이미 그 이름으로 부르는 곳이 있어 지우면 조용히 안 먹는다.
    items: list[str] | None = Field(default=None, description="품목 여럿. 없으면 item 을 본다")
    kind: TargetKind | None = Field(
        default=None, description="AUC 경락가 · WHSL 중도매가 · RTL 소매가"
    )
    kinds: list[TargetKind] | None = Field(
        default=None, description="가격 종류 여럿. 없으면 kind 를 본다"
    )
    dates: list[date] | None = Field(default=None, description="대상일 목록. 없으면 내일 하루")
    as_of: date | None = Field(default=None, description="기준일. 없으면 전달표의 최신 기준일")


class QaMeta(BaseModel):
    """출처와 상태. **마스터는 이 값을 봉투에 싣는다.**"""

    status: QaStatus
    #   하나짜리 옛 이름 — 조합이 하나면 그 값, 여럿이면 **첫 번째**를 가리킨다.
    item: str | None = None
    kind: str | None = None
    #   ★ 실제로 답한 조합 전부 (2026-09-15). 「배추 경락가랑 도매가」면 둘이 들어온다.
    items: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
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
    #   모델 대신 어제 가격을 그대로 낸 행인가. 답 문장에서는 뺐고 여기로만 간다.
    is_gated: list[bool] = Field(default_factory=list)
    band_method: str | None = None
    use_recommended: bool | None = None

    #   ★ **답 문장에서 뺀 값들** (2026-09-15 · 화면을 깨끗이 하라는 지시).
    #     빼는 것이 아니라 **옮기는** 것이다 — 없애면 다음 셋을 아무도 못 본다.
    #
    #       current_price   출발점. 실제 거래가가 아니라 모델이 출발한 값
    #       accuracy_pct    그 조합의 평균 오차율 · accuracy_note 는 그 조건
    #       spec_desc       무엇을 잰 값인가 (시장·등급·규격) — 매입 파트 요청 항목
    #   🔴 **자료형이 창고마다 다르다** (2026-09-15 실측으로 배웠다).
    #     전달표 행은 정수인데 원본 창고의 당일 행은 `Decimal('1001.090')` 이다.
    #     `int` 로 좁혀 뒀더니 당일 값을 물을 때마다 500 이 났다 — 검사는 도구를
    #     갈아 끼워 정수만 넣어서 안 걸렸다. **반올림해 받는다.**
    current_price: int | None = None

    @field_validator("current_price", mode="before")
    @classmethod
    def _round_price(cls, value):
        return None if value is None else round(float(value))
    accuracy_pct: str | None = None
    accuracy_note: str | None = None
    market_name: str | None = None
    grade_name: str | None = None
    spec_desc: str | None = None


class QaAnswer(BaseModel):
    """돌려주는 것 — 마크다운 한 덩어리 + 기계용 값."""

    markdown: str
    meta: QaMeta

    #: 🔴 **마스터 회신에 붙일 근거의 재료다** (`app/ml/adapter.py`). 읽은 예측 행을
    #: 그대로 담는다 — 어댑터가 `Evidence.value` 와 `ref_ids` 를 여기서 만든다.
    #:
    #: ★ **API 응답에는 안 싣는다.** `/ml/qa` 는 마크다운을 보는 입구라 행을 통째로
    #:   내보내면 답보다 부속이 커진다. `exclude=True` 로 직렬화에서 뺀다.
    #:
    #: 비어 있으면 «예측을 안 읽었다» 는 뜻이고, 어댑터는 그때 `observed_at` 을
    #: 비운다 — 안 읽고 잰 척하지 않기 위해서다.
    rows_for_evidence: list[dict] = Field(default_factory=list, exclude=True)
