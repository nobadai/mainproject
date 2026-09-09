"""LangGraph 기반 Sales proposal agent."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.sales.ranking import rank_scenarios, recommended_scenario_id, remove_dominated_scenarios
from app.sales.schemas import (
    ProposalSelfCheck,
    SalesDecisionTrace,
    SalesProposalInput,
    SalesProposalReply,
)
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
        {
            "feedback": "apply_feedback",
            "evaluate": "evaluate_candidates",
            "self_check": "self_check",
        },
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
    agent_trace = [
        {
            "stage": "prepare_context",
            "business_mode": request.business_mode,
            "feedback_attempt": request.feedback_attempt,
            "has_feedback": bool(feedback and feedback.domain_replies),
        }
    ]
    return {
        **state,
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
        "agent_trace": agent_trace,
    }


def _classify_situation(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import validate_context

    missing = validate_context(state["request"])
    return {
        **state,
        "missing_data": missing,
        "status": "INPUT_INCOMPLETE" if missing else "READY_TO_GENERATE",
        "terminal_reason": "INPUT_INCOMPLETE" if missing else None,
        "agent_trace": [
            *state.get("agent_trace", []),
            {"stage": "classify_situation", "missing_data": missing},
        ],
    }


def _route_after_situation(state: SalesAgentState) -> str:
    return "incomplete" if state.get("missing_data") else "generate"


def _generate_candidates(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import _generate_scenarios

    candidates = _generate_scenarios(state["request"])
    terminal_reason = None if candidates else "NO_SALES_CANDIDATE"
    return {
        **state,
        "candidates": candidates,
        "status": "CANDIDATES_GENERATED" if candidates else "NO_OPPORTUNITY",
        "terminal_reason": terminal_reason,
        "agent_trace": [
            *state.get("agent_trace", []),
            {"stage": "generate_candidates", "candidate_count": len(candidates)},
        ],
    }


def _plan_validations(state: SalesAgentState) -> SalesAgentState:
    required: list[dict[str, str]] = []
    for candidate in state.get("candidates", []):
        for validation in candidate.required_validations:
            entry = {
                "candidate_id": candidate.scenario_id,
                "validation": validation,
                "reason": _validation_reason(validation),
            }
            if entry not in required:
                required.append(entry)
    return {
        **state,
        "required_validations": required,
        "terminal_reason": "VALIDATION_REQUIRED" if required else state.get("terminal_reason"),
        "agent_trace": [
            *state.get("agent_trace", []),
            {"stage": "determine_validations", "required_validations": required},
        ],
    }


def _route_after_validation_plan(state: SalesAgentState) -> str:
    if not state.get("candidates"):
        return "self_check"
    if state.get("required_validations") and not state.get("external_feedback", {}).get(
        "reply_count", 0
    ):
        return "self_check"
    feedback = state.get("external_feedback", {})
    return "feedback" if feedback.get("reply_count", 0) else "evaluate"


def _apply_feedback(state: SalesAgentState) -> SalesAgentState:
    candidates = state.get("candidates", [])
    rejected = [candidate for candidate in candidates if _is_rejected(candidate)]
    return {
        **state,
        "candidates": candidates,
        "rejected_candidates": rejected,
        "status": "FEEDBACK_APPLIED",
        "terminal_reason": None,
        "agent_trace": [
            *state.get("agent_trace", []),
            {
                "stage": "apply_feedback",
                "rejected_candidate_ids": [candidate.scenario_id for candidate in rejected],
                "conditional_candidate_ids": [
                    candidate.scenario_id
                    for candidate in candidates
                    if candidate.status == "CONDITIONAL"
                ],
            },
        ],
    }


def _evaluate_candidates(state: SalesAgentState) -> SalesAgentState:
    candidates, excluded = remove_dominated_scenarios(state.get("candidates", []))
    candidates = [candidate for candidate in candidates if not _is_rejected(candidate)]
    rejected = [
        candidate
        for candidate in state.get("candidates", [])
        if _is_rejected(candidate) or candidate.scenario_id in excluded
    ]
    return {
        **state,
        "validated_candidates": candidates,
        "rejected_candidates": rejected,
        "excluded_reasons": excluded,
        "agent_trace": [
            *state.get("agent_trace", []),
            {
                "stage": "evaluate_candidates",
                "selectable_count": len(
                    [candidate for candidate in candidates if candidate.status == "EXECUTABLE"]
                ),
                "conditional_count": len(
                    [candidate for candidate in candidates if candidate.status == "CONDITIONAL"]
                ),
                "rejected_candidate_ids": [candidate.scenario_id for candidate in rejected],
            },
        ],
    }


def _rank_candidates(state: SalesAgentState) -> SalesAgentState:
    ranked = rank_scenarios(state.get("validated_candidates", []))
    ranked_ids = [candidate.scenario_id for candidate in ranked]
    recommendation_id = ranked_ids[0] if ranked_ids else None
    return {
        **state,
        "ranked_candidate_ids": ranked_ids,
        "recommendation_id": recommendation_id,
        "agent_trace": [
            *state.get("agent_trace", []),
            {"stage": "rank_candidates", "ranked_candidate_ids": ranked_ids},
        ],
    }


def _self_check(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import self_check_scenarios

    scenarios = state.get("validated_candidates", [])
    check = _agent_self_check(state, self_check_scenarios(scenarios))
    if check.passed or state.get("feedback_attempt", 0) >= 1:
        return {
            **state,
            "self_check": check,
            "agent_trace": [
                *state.get("agent_trace", []),
                {"stage": "self_check", "passed": check.passed, "issues": check.issue_codes},
            ],
        }
    filtered = [scenario for scenario in scenarios if not _is_rejected(scenario)]
    if len(filtered) != len(scenarios):
        return {
            **state,
            "validated_candidates": filtered,
            "self_check": check,
            "self_check_reranked": True,
            "agent_trace": [
                *state.get("agent_trace", []),
                {"stage": "self_check", "passed": False, "action": "RERANK_WITHOUT_REJECTED"},
            ],
        }
    return {
        **state,
        "recommendation_id": None,
        "self_check": check,
        "self_check_reranked": True,
        "agent_trace": [
            *state.get("agent_trace", []),
            {"stage": "self_check", "passed": False, "action": "CLEAR_RECOMMENDATION"},
        ],
    }


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
    scenarios = state.get("validated_candidates", state.get("candidates", []))
    ranked_ids = state.get("ranked_candidate_ids", [])
    recommendation_id = state.get("recommendation_id")
    if recommendation_id is None and ranked_ids:
        recommendation_id = ranked_ids[0]
    if recommendation_id is None and not state.get("terminal_reason"):
        recommendation_id = recommended_scenario_id(scenarios)
    if any(
        candidate.scenario_id == recommendation_id
        for candidate in state.get("rejected_candidates", [])
    ):
        recommendation_id = None
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
    traced_ids = {item.candidate_id for item in trace}
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
            exclusion_reasons=["REJECTED_BY_FEEDBACK"],
            unresolved_fields=scenario.uncertainties,
            reply_refs=_reply_refs(scenario.domain_replies),
            policy_model_refs=[request.ml_context.model_version] if request.ml_context else [],
        )
        for scenario in state.get("rejected_candidates", [])
        if scenario.scenario_id not in traced_ids
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
    return {
        **state,
        "decision_trace": trace,
        "recommendation": recommendation,
        "recommendation_id": recommendation_id,
        "agent_trace": [
            *state.get("agent_trace", []),
            {
                "stage": "final_recommendation",
                "terminal_reason": state.get("terminal_reason"),
                "recommended_scenario_id": recommendation_id,
                "self_check_passed": state["self_check"].passed,
            },
        ],
        "reply": reply,
    }


def _agent_self_check(
    state: SalesAgentState, base_check: ProposalSelfCheck
) -> ProposalSelfCheck:
    issues = list(base_check.issue_codes)
    recommendation_id = state.get("recommendation_id")
    rejected_ids = {candidate.scenario_id for candidate in state.get("rejected_candidates", [])}
    candidates = {
        candidate.scenario_id: candidate
        for candidate in state.get("validated_candidates", [])
    }
    if not recommendation_id and not state.get("terminal_reason"):
        issues.append("RECOMMENDATION_MISSING")
    if recommendation_id in rejected_ids:
        issues.append("REJECTED_RECOMMENDATION")
    if recommendation_id and recommendation_id not in candidates:
        issues.append("RECOMMENDATION_NOT_IN_CANDIDATES")
    if recommendation_id and candidates.get(recommendation_id):
        scenario = candidates[recommendation_id]
        if scenario.required_validations and not state.get("external_feedback", {}).get(
            "reply_count", 0
        ):
            issues.append("RECOMMENDATION_VALIDATION_PENDING")
        if scenario.sales_amount_krw is not None and (
            scenario.quantity_kg is None or scenario.unit_price_krw is None
        ):
            issues.append("RECOMMENDATION_AMOUNT_SOURCE_MISSING")
    issues = _unique(issues)
    return base_check.model_copy(
        update={
            "passed": not issues,
            "issue_codes": issues,
            "messages": base_check.messages
            if not issues
            else ["판매안의 추천 후보와 외부 검증 상태를 다시 확인해 주세요."],
        }
    )


def _is_rejected(candidate) -> bool:
    return candidate.status == "INFEASIBLE" or candidate.finance_verdict == "FAIL"


def _validation_reason(validation: str) -> str:
    return {
        "FINANCIAL_VALIDATION": "결제조건·마진·현금 영향은 Finance 검증이 필요합니다.",
        "SELLABLE_SUPPLY_CONTEXT": "판매 가능 수량은 Logistics/Inventory 검증이 필요합니다.",
        "DELIVERY_FEASIBILITY_CONTEXT": "납기 가능 여부는 Logistics 검증이 필요합니다.",
        "ADDITIONAL_SUPPLY_CONTEXT": "부족 물량 확보 가능성은 Purchase 검증이 필요합니다.",
    }.get(validation, "외부 권위 검증이 필요합니다.")


def _collapse_reason(reasons: list[str]) -> str | None:
    if len(reasons) == 1:
        return reasons[0]
    if reasons:
        return "SCENARIO_VARIANTS_PARTIALLY_COLLAPSED"
    return None


def _unique(values) -> list:
    return list(dict.fromkeys(value for value in values if value))
