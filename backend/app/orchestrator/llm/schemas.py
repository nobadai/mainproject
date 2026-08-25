"""Orchestrator Local LLM contracts and response extension fields.

★ 오케스트레이터의 LLM 지점은 T3-5 선정 하나뿐이다 (설계서 §5.3).
  LLM 은 **순위만 정하고 사람이 읽을 문장을 쓴다.** 수량·금액은 T3-2 클리핑이 이미 확정했다.

  출력 스키마(`SelectionInterpretation`)에 수량·금액 필드가 **존재하지 않으므로**
  생성이 불가능하다. §1.2-3 을 프롬프트가 아니라 타입으로 보장하는 방법이다.

  상태 필드 5종(`llm_status` ~ `llm_fallback_used`)은 Finance / Logistics 와 동일하다.
  `summary` 도 공통으로 유지해 Frontend 의 공통 AI 카드가 수정 없이 동작한다.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]

# 클리핑 강도를 숫자 대신 3구간 라벨로 준다 — Context 에서 숫자를 지우기 위한 것이다.
ClipMagnitude = Literal["FULL", "MINOR_CLIP", "MAJOR_CLIP"]


class SelectionInterpretation(BaseModel):
    """T3-5 선정 결과. 수량·금액 필드가 없다 — 이것이 안전장치의 전부다."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    ranked_scenario_ids: list[str]
    rationale_per_id: dict[str, str]
    conflict_note: str | None


class CandidateContext(BaseModel):
    """후보 1건. 숫자를 담지 않는다 — 클리핑 강도는 라벨로, 제약은 코드로."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    clip_magnitude: ClipMagnitude
    binding_constraints: list[str]


class SanitizedLLMContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal["ORCHESTRATOR"] = "ORCHESTRATOR"
    cycle: Literal["PROCUREMENT", "SALES"]
    signals: list[str]
    facts: list[str]
    candidates: list[CandidateContext]


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: SelectionInterpretation
    llm_status: LLMStatus
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool


def default_interpretation() -> SelectionInterpretation:
    return SelectionInterpretation(
        summary="결정론적 오케스트레이터 결과를 유지합니다.",
        ranked_scenario_ids=[],
        rationale_per_id={},
        conflict_note=None,
    )


class LLMResponseFields(BaseModel):
    interpretation: SelectionInterpretation = Field(default_factory=default_interpretation)
    llm_status: LLMStatus = "DISABLED"
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_attempts: int = Field(default=0, ge=0)
    llm_fallback_used: bool = False
