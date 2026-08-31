"""Logistics Local LLM contracts and response extension fields."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]

#: LLM 호출이 최종적으로 실패한 원인 분류. 최종 상태만 기록한다 — 재시도 후 성공하면
#: None 이다(중간 실패는 로그 몫). API Key 원문 같은 세부 정보는 어디에도 싣지 않는다.
LLMErrorKind = Literal[
    "TIMEOUT",
    "NETWORK_ERROR",
    "SERVER_ERROR",
    "AUTH_ERROR",
    "QUOTA_EXCEEDED",
    "BAD_REQUEST",
    "INVALID_RESPONSE",
    "VALIDATION_FAILED",
]


class ContextFact(BaseModel):
    """판정에 실제 사용된 수치의 확정 표기 (LLM 정책 결정서 v1.3 §5).

    LLM에 계산을 시키지 않는다는 원칙은 그대로다 — LLM이 할 수 있는 것은
    `display_value` 표기의 인용뿐이다. 1차에서 `raw_value`·`unit`·`date`·`source`
    필드는 추가하지 않는다. 관계 수치(판정값과 임계)는 "91.7% (임계 90%)" 처럼
    한 fact로 묶어 라벨-값 뒤바뀜을 구조로 차단한다.
    `display_value`는 단일 formatter(interpretation.py)만 만든다 — 인용 검사가
    exact 대조라 표기가 두 곳에서 만들어지면 검증이 흔들린다.
    """

    model_config = ConfigDict(extra="forbid")

    #: 무숫자 명명 — 숫자 포함 코드는 검증기의 숫자 검사와 충돌한 전례가 있다.
    fact_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    display_value: str = Field(min_length=1)


class AgentInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    risks: list[str]
    suggested_adjustment: str | None


class SanitizedLLMContext(BaseModel):
    """LLM에 전달하는 유일한 입력. 외부 Provider 전송 경계이기도 하다.

    원본 숫자(재고 kg·날짜·비율·금액)·lot_id·거래처는 싣지 않는다 — LLM에 계산을
    시키지 않는다는 역할 제한의 구조적 보장이자, 외부 API 전송 시 원본 업무 데이터가
    나가지 않게 하는 경계다.

    `signals`와 `missing_data`는 저장 위치가 아니라 **코드의 의미**로 분류한다:
    업무 상태/위험 코드 → signals, 정보·정책 미확정 코드 → missing_data(무숫자 번역명).
    """

    model_config = ConfigDict(extra="forbid")

    domain: Literal["LOGISTICS"] = "LOGISTICS"
    signals: list[str]
    #: 판정에 실제 사용된 수치의 구조화 표기 (v1.3 — 결론 문장 폐기).
    #: 상한: signal당 최대 3개 · Context 전체 최대 8개. 초과 시 조용한 절단 금지 —
    #: LLM을 호출하지 않고 무숫자 Template을 유지한다 (조립기가 강제).
    facts: list[ContextFact]
    allowed_adjustments: list[str]
    #: Rule/Scenario Engine이 이미 결정한 우선 조정 방향. LLM이 고르지 않는다 —
    #: 값이 있으면 그 방향만 설명하고, None이면 추천하지 않는다(검증기가 강제).
    preferred_adjustment: str | None = None
    #: 시스템이 확정한 미확정 정보의 무숫자 번역명. LLM이 새로 만들지 않고
    #: 여기 있는 이름만 설명할 수 있다. 빈 리스트는 "미확정 없음"이다.
    missing_data: list[str] = Field(default_factory=list)


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: AgentInterpretation
    llm_status: LLMStatus
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool
    #: 최종 실패 원인. SUCCESS(재시도 후 성공 포함)면 None.
    llm_error_kind: LLMErrorKind | None = None
    #: LLM 호출에 사용된 fact 목록. 기준은 수신이 아니라 **호출 확정**이다 —
    #: provider.generate(context)의 입력으로 쓰였으면 기록한다. Key 없음(AUTH_ERROR)
    #: 처럼 전송 전에 실패한 FALLBACK도 기록된다. "Gemini가 실제 수신한 값"을
    #: 뜻하지 않는다. SUCCESS·FALLBACK → 기록 / SKIPPED_TEMPLATE·DISABLED → 빈 목록.
    llm_context_facts: list[ContextFact] = Field(default_factory=list)


def default_interpretation() -> AgentInterpretation:
    return AgentInterpretation(
        summary="결정론적 재고물류 결과를 유지합니다.",
        risks=[],
        suggested_adjustment=None,
    )


class LLMResponseFields(BaseModel):
    interpretation: AgentInterpretation = Field(default_factory=default_interpretation)
    llm_status: LLMStatus = "DISABLED"
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_attempts: int = Field(default=0, ge=0)
    llm_fallback_used: bool = False
    llm_error_kind: LLMErrorKind | None = None
    #: LLM 호출에 사용된 Sanitized ContextFact 목록 — 독립 Response로 노출되고
    #: response_payload 실행이력에 자동 기록된다 (저장 스키마 무변경).
    llm_context_facts: list[ContextFact] = Field(default_factory=list)
