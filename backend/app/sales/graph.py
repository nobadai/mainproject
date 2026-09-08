"""LangGraph 기반 Sales proposal agent."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.sales.ranking import rank_scenarios, recommended_scenario_id, remove_dominated_scenarios
from app.sales.schemas import SalesDecisionTrace, SalesProposalInput, SalesProposalReply
from app.sales.state import SalesAgentState


def run_sales_agent(request: SalesProposalInput) -> SalesProposalReply:
    state = _graph().invoke({"request": request})
    return state["reply"]


def _graph():
    graph = StateGraph(SalesAgentState)
    graph.add_node("prepare_context", _prepare_context)
    graph.add_node("classify_situation", _classify_situation)
    graph.add_node("generate_candidates", _generate_candidates)
    graph.add_node("plan_validations", _plan_validations)
    graph.add_node("apply_feedback", _apply_feedback)
    graph.add_node("evaluate_candidates", _evaluate_candidates)
    graph.add_node("rank_candidates", _rank_candidates)
    graph.add_node("self_check", _self_check)
    graph.add_node("final_recommendation", _final_recommendation)

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "classify_situation")
    graph.add_conditional_edges(
        "classify_situation",
        _route_after_situation,
        {"incomplete": "self_check", "generate": "generate_candidates"},
    )
    graph.add_edge("generate_candidates", "plan_validations")
    graph.add_conditional_edges(
        "plan_validations",
        _route_after_validation_plan,
        {"feedback": "apply_feedback", "evaluate": "evaluate_candidates"},
    )
    graph.add_edge("apply_feedback", "evaluate_candidates")
    graph.add_edge("evaluate_candidates", "rank_candidates")
    graph.add_edge("rank_candidates", "self_check")
    graph.add_conditional_edges(
        "self_check",
        _route_after_self_check,
        {"rerank": "rank_candidates", "final": "final_recommendation"},
    )
    graph.add_edge("final_recommendation", END)
    return graph.compile()


def _prepare_context(state: SalesAgentState) -> SalesAgentState:
    request = state["request"]
    feedback = request.feedback
    return {
        **state,
        "as_of": request.execution_identity.as_of if request.execution_identity else None,
        "item": request.user_request.item,
        "business_mode": request.business_mode,
        "feedback_attempt": request.feedback_attempt,
        "external_feedback": {
            "reply_count": len(feedback.domain_replies) if feedback else 0,
            "scenario_feedback_count": len(feedback.scenario_feedback) if feedback else 0,
        },
        "sales_context": {
            "has_contract": request.contract_context is not None,
            "has_logistics": request.logistics_context is not None,
            "has_finance": request.finance_context is not None,
            "is_refeed": request.is_refeed,
        },
    }


def _classify_situation(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import validate_context

    missing = validate_context(state["request"])
    return {
        **state,
        "missing_data": missing,
        "status": "INPUT_INCOMPLETE" if missing else "READY_TO_GENERATE",
    }


def _route_after_situation(state: SalesAgentState) -> str:
    return "incomplete" if state.get("missing_data") else "generate"


def _generate_candidates(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import _generate_scenarios

    candidates = _generate_scenarios(state["request"])
    return {**state, "candidates": candidates, "status": "CANDIDATES_GENERATED"}


def _plan_validations(state: SalesAgentState) -> SalesAgentState:
    required = []
    for candidate in state.get("candidates", []):
        for validation in candidate.required_validations:
            key = f"{candidate.scenario_id}:{validation}"
            if key not in required:
                required.append(key)
    return {**state, "required_validations": required}


def _route_after_validation_plan(state: SalesAgentState) -> str:
    feedback = state.get("external_feedback", {})
    return "feedback" if feedback.get("reply_count", 0) else "evaluate"


def _apply_feedback(state: SalesAgentState) -> SalesAgentState:
    candidates = state.get("candidates", [])
    rejected = [candidate for candidate in candidates if candidate.status == "INFEASIBLE"]
    return {
        **state,
        "candidates": candidates,
        "rejected_candidates": rejected,
        "uncertainties": _unique(
            uncertainty
            for candidate in candidates
            for uncertainty in candidate.uncertainties
        ),
        "status": "FEEDBACK_APPLIED",
    }


def _evaluate_candidates(state: SalesAgentState) -> SalesAgentState:
    candidates, excluded = remove_dominated_scenarios(state.get("candidates", []))
    rejected = [
        candidate
        for candidate in state.get("candidates", [])
        if candidate.status == "INFEASIBLE" or candidate.scenario_id in excluded
    ]
    return {
        **state,
        "validated_candidates": candidates,
        "rejected_candidates": rejected,
        "excluded_reasons": excluded,
    }


def _rank_candidates(state: SalesAgentState) -> SalesAgentState:
    ranked = rank_scenarios(state.get("validated_candidates", []))
    return {**state, "ranked_candidate_ids": [candidate.scenario_id for candidate in ranked]}


def _self_check(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import self_check_scenarios

    scenarios = state.get("validated_candidates", [])
    check = self_check_scenarios(scenarios)
    if check.passed or state.get("feedback_attempt", 0) >= 1:
        return {**state, "self_check": check}
    filtered = [scenario for scenario in scenarios if scenario.status != "INFEASIBLE"]
    if len(filtered) != len(scenarios):
        return {
            **state,
            "validated_candidates": filtered,
            "self_check": check,
            "self_check_reranked": True,
        }
    return {**state, "self_check": check, "self_check_reranked": True}


def _route_after_self_check(state: SalesAgentState) -> str:
    check = state.get("self_check")
    if (
        check
        and not check.passed
        and state.get("feedback_attempt", 0) < 1
        and not state.get("self_check_reranked")
    ):
        return "rerank"
    return "final"


def _final_recommendation(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import _interpret_scenarios, _missing_capabilities, _reply_refs

    request = state["request"]
    scenarios = state.get("validated_candidates", [])
    ranked_ids = state.get("ranked_candidate_ids", [])
    recommendation_id = recommended_scenario_id(scenarios)
    recommendation = _interpret_scenarios(scenarios, recommendation_id)
    exclusions = state.get("excluded_reasons", {})
    trace = [
        SalesDecisionTrace(
            candidate_id=scenario.scenario_id,
            status=scenario.status,
            rank=ranked_ids.index(scenario.scenario_id) + 1
            if scenario.scenario_id in ranked_ids
            else None,
            recommended=scenario.scenario_id == recommendation_id,
            finance_verdict=scenario.finance_verdict,
            profitability_krw=scenario.contribution_margin_krw,
            scenario_projected_cash_min=scenario.scenario_projected_cash_min,
            depends_on_projected_inflow=scenario.depends_on_projected_inflow,
            inventory_risk_severity=scenario.authoritative_inventory_risk_severity,
            sell_priority=scenario.sell_priority,
            remaining_freshness_days=scenario.remaining_freshness_days,
            dependencies=scenario.execution_dependencies,
            ml_support_used=scenario.ml_support_used,
            changed_axes=scenario.sales_decision_axes,
            exclusion_reasons=exclusions.get(scenario.scenario_id, []),
            unresolved_fields=scenario.uncertainties,
            reply_refs=_reply_refs(scenario.domain_replies),
            policy_model_refs=[request.ml_context.model_version] if request.ml_context else [],
        )
        for scenario in scenarios
    ]
    trace.extend(
        SalesDecisionTrace(
            candidate_id=scenario.scenario_id,
            status=scenario.status,
            finance_verdict=scenario.finance_verdict,
            profitability_krw=scenario.contribution_margin_krw,
            scenario_projected_cash_min=scenario.scenario_projected_cash_min,
            depends_on_projected_inflow=scenario.depends_on_projected_inflow,
            inventory_risk_severity=scenario.authoritative_inventory_risk_severity,
            sell_priority=scenario.sell_priority,
            remaining_freshness_days=scenario.remaining_freshness_days,
            dependencies=scenario.execution_dependencies,
            ml_support_used=scenario.ml_support_used,
            changed_axes=scenario.sales_decision_axes,
            exclusion_reasons=exclusions[scenario.scenario_id],
            unresolved_fields=scenario.uncertainties,
            reply_refs=_reply_refs(scenario.domain_replies),
            policy_model_refs=[request.ml_context.model_version] if request.ml_context else [],
        )
        for scenario in state.get("candidates", [])
        if scenario.scenario_id in exclusions
    )
    collapse_reasons = _unique(
        scenario.variant_collapsed_reason
        for scenario in scenarios
        if scenario.variant_collapsed_reason
    )
    reply = SalesProposalReply(
        status="INPUT_INCOMPLETE" if state.get("missing_data") else "SCENARIOS_GENERATED",
        business_mode=request.business_mode,
        is_refeed=request.is_refeed,
        feedback_attempt=request.feedback_attempt,
        scenarios=scenarios,
        variant_collapsed=bool(collapse_reasons),
        variant_collapsed_reason=_collapse_reason(collapse_reasons),
        missing_data=state.get("missing_data", []),
        missing_capabilities=_missing_capabilities(request),
        recommended_scenario_id=recommendation_id,
        llm=recommendation,
        recommendation=recommendation,
        self_check=state["self_check"],
        decision_trace=trace,
    )
    return {**state, "decision_trace": trace, "recommendation": recommendation, "reply": reply}


def _collapse_reason(reasons: list[str]) -> str | None:
    if len(reasons) == 1:
        return reasons[0]
    if reasons:
        return "SCENARIO_VARIANTS_PARTIALLY_COLLAPSED"
    return None


def _unique(values) -> list:
    return list(dict.fromkeys(value for value in values if value))
