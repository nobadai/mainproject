"""Critic Local LLM contracts and response extension fields.

★ Critic 의 LLM 지점은 L5 논리 일관성 판정 하나뿐이다 (설계서 §6.4).
  Judge 는 **설명문이 데이터와 모순되는지만** 본다. 수량을 바꾸라고 제안하지 않는다 —
  L5 로 숫자를 바꾸면 LLM 이 숫자를 만든 것이 되어 §1.2-3 위반이다.

  FAIL 이어도 결정을 죽이지 못한다. Critic 판정은 CONCERN 까지만 올라간다.

  상태 필드 5종(`llm_status` ~ `llm_fallback_used`)은 Finance / Logistics / Orchestrator 와
  동일하다. `summary` 도 공통으로 유지한다.

  ⚠️ `CriticVerdictOut.skipped` 와 `llm_status="SKIPPED_TEMPLATE"` 는 의미가 다르다.
     · skipped            — 미검사 항목 목록. coverage 하락으로 드러난다 (설계서 §8)
     · SKIPPED_TEMPLATE   — LLM 호출이 불필요해 기본값을 썼다
     L5 judge 미가동이면 **둘 다** 발생한다.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]
JudgeVerdict = Literal["PASS", "FAIL"]


class JudgeInterpretation(BaseModel):
    """L5 판정 결과. 수량·금액 필드가 없다 — 판정만 하고 숫자를 만들지 못한다."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    verdict: JudgeVerdict
    note: str


class SanitizedLLMContext(BaseModel):
    """판정 대상. 검사 재료는 코드·문장뿐이고 수량은 넘기지 않는다."""

    model_config = ConfigDict(extra="forbid")

    domain: Literal["CRITIC"] = "CRITIC"
    cycle: Literal["A", "B"]
    signals: list[str]
    facts: list[str]
    binding_constraints: list[str]
    rationale: str


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: JudgeInterpretation
    llm_status: LLMStatus
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool


def default_interpretation() -> JudgeInterpretation:
    return JudgeInterpretation(
        summary="결정론적 Critic 검증 결과를 유지합니다.",
        verdict="PASS",
        note="L5 논리 일관성 검증이 수행되지 않았습니다.",
    )


class LLMResponseFields(BaseModel):
    interpretation: JudgeInterpretation = Field(default_factory=default_interpretation)
    llm_status: LLMStatus = "DISABLED"
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_attempts: int = Field(default=0, ge=0)
    llm_fallback_used: bool = False
