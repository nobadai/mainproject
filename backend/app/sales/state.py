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
    as_of: object
    item: str
    business_mode: str
    sales_context: dict[str, object]
    candidates: list[SalesScenario]
    validated_candidates: list[SalesScenario]
    rejected_candidates: list[SalesScenario]
    required_validations: list[str]
    external_feedback: dict[str, object]
    missing_data: list[str]
    uncertainties: list[str]
    feedback_attempt: int
    decision_trace: list[SalesDecisionTrace]
    recommendation: SalesRecommendation
    self_check: ProposalSelfCheck
    self_check_reranked: bool
    ranked_candidate_ids: list[str]
    excluded_reasons: dict[str, list[str]]
    status: SalesCandidateStatus | str
    reply: SalesProposalReply
