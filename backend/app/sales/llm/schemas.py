"""LLM에 전달하는 숫자 없는 후보 요약 계약."""

from pydantic import BaseModel, ConfigDict, Field


class CandidateInterpretationInput(BaseModel):
    """LLM은 식별자·의미 라벨만 받아 숫자를 바꿀 수 없다."""

    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    strategy_label: str | None = None
    adjustment_axis: str
    conditional: bool
    risk_labels: list[str] = Field(default_factory=list)
    uncertainty_labels: list[str] = Field(default_factory=list)


class LlmInterpretationOutput(BaseModel):
    """숫자 필드가 없는 LLM 해석 결과."""

    model_config = ConfigDict(extra="forbid")
    recommended_candidate_id: str
    summary: str
    recommendation_reason: str
    risk_explanation: str
    user_message: str
