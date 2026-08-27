"""⑤ 등급 조합 판단의 LLM 계약 (상세설계 §4-⑤ E3-2 확정).

**출력 스키마에 비율·수량·단가 필드가 없다 — 이것이 안전장치의 전부다.**
orchestrator의 ``SelectionInterpretation``이 쓴 방법 그대로다("수량·금액 필드가
**존재하지 않으므로** 생성이 불가능하다. §1.2-3을 프롬프트가 아니라 타입으로 보장하는
방법이다"). 프롬프트로 "숫자를 만들지 마"라고 부탁하는 것과 달리, 타입은 어긴 출력을
파싱 단계에서 되돌려보낸다.

LLM이 돌려주는 건 **후보 id 하나와 사유 문장**뿐이고, 비율은 규칙이 만든 후보의 것을
그대로 쓴다 (CLAUDE.md 규칙 6 · 정의서 §1.2-3).

``SanitizedLLMContext``에도 숫자를 싣지 않는다 — 스프레드·신선도·납품량을 전부 라벨로
바꿔 넣는다. orchestrator의 ``classify_clip``이 클리핑 강도를 3구간 라벨로 바꾼 것과
같은 이유다: 컨텍스트에 숫자가 없으면 LLM이 베껴 쓸 숫자도 없다.

상태 필드 5종(``llm_status``~``llm_fallback_used``)은 finance/logistics/orchestrator/
critic과 **동일하다** — 팀 공용 AI 카드가 수정 없이 동작한다.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]

#: 스프레드 라벨. 숫자(0.212)를 그대로 주면 LLM이 그 숫자를 사유에 베껴 쓰고,
#: 그 순간 "LLM이 만든 숫자"가 출력에 실린다. 판정은 규칙이 이미 끝냈으므로 결론만 준다.
SpreadLabel = Literal["SPREAD_NORMAL", "SPREAD_WIDE"]
#: 잔여신선도가 근접 납품을 감당하는가. 일수 대신 3구간.
FreshnessLabel = Literal["SHELF_AMPLE", "SHELF_TIGHT", "SHELF_UNKNOWN"]


class MixCandidate(BaseModel):
    """규칙이 만든 후보 1건. **비율도 수량도 담지 않는다** — id와 라벨뿐이다.

    실제 비율은 노드가 같은 id로 들고 있다. LLM에 비율을 보여주면 사유에 숫자가 새고,
    검증기의 숫자 금지 규칙에 걸려 매번 재시도가 돈다.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    #: 사람이 읽는 짧은 설명. 숫자를 넣지 않는다 ("중품 상한만큼" O, "중품 60%" X).
    summary: str = Field(min_length=1)


class GradeMixInterpretation(BaseModel):
    """LLM의 응답. **고른 후보 id와 사유가 전부다.**

    ⚠️ **``min_length`` 제약을 걸지 않는다.** 이 모델의 ``model_json_schema()``가 그대로
    구조화 출력 API에 실리는데, Anthropic 구조화 출력과 OpenAI strict 모드 **둘 다**
    문자열 길이 제약(``minLength``/``maxLength``)을 지원하지 않는다. 스키마에 넣으면
    한쪽에서 400이 나거나 조용히 무시된다.

    빈 문자열 검사는 ``runtime.validate_interpretation``이 한다 — 검증을 프로바이더 밖
    공통 층에 두는 원칙 그대로다. 타입이 못 막는 건 검증기가 막는다.
    """

    model_config = ConfigDict(extra="forbid")

    chosen_candidate_id: str
    reason: str


class SanitizedLLMContext(BaseModel):
    """LLM에 주는 판단 재료. **숫자가 하나도 없다.**"""

    model_config = ConfigDict(extra="forbid")

    domain: Literal["PURCHASE"] = "PURCHASE"
    item: str = Field(min_length=1)
    spread: SpreadLabel
    freshness: FreshnessLabel
    #: 규칙이 낸 신호 코드. 예: "MID_GRADE_SCORE_POSITIVE", "NEAR_TERM_DEMAND_LIMITED".
    signals: list[str]
    #: 사람이 읽는 사실 문장. 숫자를 넣지 않는다.
    facts: list[str]
    candidates: list[MixCandidate]


class InterpretationResult(BaseModel):
    """노드로 돌아가는 결과. 상태 5종은 팀 공통이다."""

    model_config = ConfigDict(extra="forbid")

    interpretation: GradeMixInterpretation
    llm_status: LLMStatus
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool
