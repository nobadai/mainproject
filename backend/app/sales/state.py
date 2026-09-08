"""Sales LangGraph state."""

from __future__ import annotations

from typing import TypedDict

from app.sales.schemas import (
    ProposalSelfCheck,
    SalesCandidateStatus,
    SalesDecisionTrace,
    SalesProposalInput,
    SalesProposalReply,
    SalesRecommendation,
    SalesScenario,
)


class SalesAgentState(TypedDict, total=False):
    request: SalesProposalInput
    business_mode: str
    sales_context: dict[str, object]
    candidates: list[SalesScenario]
    validated_candidates: list[SalesScenario]
    rejected_candidates: list[SalesScenario]
    required_validations: list[dict[str, str]]
    external_feedback: dict[str, object]
    missing_data: list[str]
    feedback_attempt: int
    decision_trace: list[SalesDecisionTrace]
    agent_trace: list[dict[str, object]]
    recommendation: SalesRecommendation
    recommendation_id: str | None
    self_check: ProposalSelfCheck
    self_check_reranked: bool
    ranked_candidate_ids: list[str]
    excluded_reasons: dict[str, list[str]]
    status: SalesCandidateStatus | str
    terminal_reason: str | None
    reply: SalesProposalReply
