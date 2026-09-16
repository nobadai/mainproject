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
from app.sales.strategy import StrategyPlan


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
    #: 세 전략의 자세. **후보를 만들기 전에 선다** (`plan_strategy` 노드).
    strategy_plan: StrategyPlan
    reply: SalesProposalReply
