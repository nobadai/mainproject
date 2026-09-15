"""④ 회차 배분 판단자의 **요청·응답 계약** (E3-9).

🔴 **⑤ 등급 조합의 스키마와 안 섞는다.** 두 역할은 «후보 하나를 고른다» 는 모양만 같고
보는 재료가 다르다 — ⑤ 는 등급 스프레드와 신선도를, 여기는 예측 궤적과 도착일 여유를
본다. 한 모델에 둘을 담으면 쓰지 않는 칸이 생기고, 그 칸이 비었을 때 «안 온 것» 인지
«원래 없는 것» 인지 아무도 모른다.

🔴 **숫자가 하나도 없다.** ⑤ 와 같은 규율이다 — 값을 넘기면 판단자가 그 숫자를 사유에
베껴 쓰고, 그 순간 「LLM 이 만든 숫자」가 출력에 실린다 (규칙 6).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: 예측 궤적. 🔴 기울기 값이 아니라 **판정 결과**다 — 판정은 규칙이 이미 끝냈다.
TrendLabel = Literal["TREND_RISING", "TREND_FLAT"]
#: 도착일 여유. ``CAP_UNKNOWN`` 은 **「넉넉하다」가 아니라 「못 봤다」** 다 (규칙 3).
CapLabel = Literal["CAP_TIGHT", "CAP_AMPLE", "CAP_UNKNOWN"]
#: 회차 수. 숫자 대신 라벨로 넘긴다.
RoundsLabel = Literal["ROUNDS_TWO", "ROUNDS_THREE"]


class SplitCandidate(BaseModel):
    """규칙이 만들어 **안전 검사까지 통과시킨** 배분 후보."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class SplitAllocationContext(BaseModel):
    """판단 재료. **숫자가 하나도 없다.**"""

    model_config = ConfigDict(extra="forbid")

    domain: Literal["PURCHASE_SPLIT"] = "PURCHASE_SPLIT"
    item: str = Field(min_length=1)
    rounds: RoundsLabel
    trend: TrendLabel
    cap: CapLabel
    #: 규칙이 낸 신호 코드. 예: ``"SPLIT_ENTERED_BY_VOLUME"``.
    signals: list[str]
    #: 사람이 읽는 사실 문장. 숫자를 넣지 않는다.
    facts: list[str]
    candidates: list[SplitCandidate]


class SplitAllocationChoice(BaseModel):
    """판단자가 돌려주는 것. **비율도 수량도 날짜도 없다** — 고른 후보와 한 문장뿐이다."""

    model_config = ConfigDict(extra="forbid")

    chosen_candidate_id: str
    reason: str


class SplitAllocationResult(BaseModel):
    """노드로 돌아가는 결과. 상태 5종은 팀 공통이다."""

    model_config = ConfigDict(extra="forbid")

    interpretation: SplitAllocationChoice
    llm_status: Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool
