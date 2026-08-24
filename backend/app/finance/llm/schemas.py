"""Finance Local LLM contracts and response extension fields."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]


class AgentInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    risks: list[str]
    suggested_adjustment: str | None


class SanitizedLLMContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal["FINANCE"] = "FINANCE"
    signals: list[str]
    facts: list[str]
    allowed_adjustments: list[str]


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: AgentInterpretation
    llm_status: LLMStatus
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool


def default_interpretation() -> AgentInterpretation:
    return AgentInterpretation(
        summary="결정론적 재무 결과를 유지합니다.",
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
