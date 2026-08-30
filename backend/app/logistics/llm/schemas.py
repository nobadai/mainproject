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
    facts: list[str]
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
