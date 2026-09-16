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
    graph.add_node("plan_strategy", _plan_strategy)
    graph.add_node("generate_candidates", _generate_candidates)
    graph.add_node("plan_validations", _plan_validations)
    graph.add_node("apply_feedback", _apply_feedback)
    graph.add_node("evaluate_candidates", _evaluate_candidates)
    graph.add_node("rank_candidates", _rank_candidates)
    graph.add_node("self_check", _self_check)
    graph.add_node("interpret_recommendation", _interpret_recommendation)
    graph.add_node("final_recommendation", _final_recommendation)

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "classify_situation")
    graph.add_conditional_edges(
        "classify_situation",
        _route_after_situation,
        {"incomplete": "self_check", "generate": "plan_strategy"},
    )
    # ★ 입력이 모자란 길에서는 전략을 세우지 않는다 — 만들 안이 없는데 모델을 부르면
    #   그 호출은 아무것도 바꾸지 못하고 비용만 쓴다.
    graph.add_edge("plan_strategy", "generate_candidates")
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
        {"rerank": "rank_candidates", "final": "interpret_recommendation"},
    )
    graph.add_edge("interpret_recommendation", "final_recommendation")
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


def _plan_strategy(state: SalesAgentState) -> SalesAgentState:
    """**후보를 만들기 전에 세 전략의 자세를 정한다** (2026-09-16).

    🔴 **모델이 불리는 두 번째 자리이고, 앞자리다.** 뒤쪽 `interpret_recommendation`
      은 이미 정해진 추천을 말로 옮기는 자리라 전략에 참여하지 않는다 — 그래서
      판매안이 *"모델이 만든 전략"* 인 적이 없었다.

    🔴 **여기서도 숫자는 안 나온다.** 모델은 자세(닫힌 어휘)만 고르고, 그 자세가
      실제 단가·수량이 되는 것은 `_generate_scenarios` 의 결정론 계산이다.

    ★ **계획은 한 실행에 한 번 선다.** 노드를 따로 세운 이유가 이것이다 — 후보
      생성 안에서 매번 만들면 모델을 여러 번 부르고, 회차마다 다른 자세가 나오면
      같은 실행 안에서 세 안의 기준이 갈린다.
    """
    from app.sales.proposal import _all_feedback_replies
    from app.sales.strategy import plan_strategies

    request = state["request"]
    plan, signals = plan_strategies(request, _all_feedback_replies(request))
    return {
        **state,
        "strategy_plan": plan,
        "agent_trace": [
            *state.get("agent_trace", []),
            {
                "stage": "plan_strategy",
                "strategy_source": plan.source,
                "llm_status": plan.llm_status,
                "depletion_pressure": signals.depletion_pressure,
                "postures": [
                    {"strategy": p.strategy, "price_posture": p.price_posture}
                    for p in plan.profiles
                ],
                "clamped": plan.clamped_reason_codes,
            },
        ],
    }


def _generate_candidates(state: SalesAgentState) -> SalesAgentState:
    from app.sales.proposal import _generate_scenarios

    candidates = _generate_scenarios(state["request"], state.get("strategy_plan"))
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
    """**결정한 것을 스스로 되짚는다.** 여기를 지나면 추천이 확정된다.

    설명하는 node 가 뒤에 따로 서 있으므로 그 앞에서 추천이 굳어 있어야 한다.
    그래서 최종 조립이 아니라 이 자리에서 `_resolve_recommendation_id` 를 부른다.
    다시 순위를 매기러 돌아가는 길에서는 부르지 않는다 — 곧 순위가 다시 정할 값이다.
    """
    from app.sales.proposal import self_check_scenarios

    scenarios = state.get("validated_candidates", [])
    check = _agent_self_check(state, self_check_scenarios(scenarios))
    if check.passed or state.get("feedback_attempt", 0) >= 1:
        settled = {
            **state,
            "self_check": check,
            "agent_trace": [
                *state.get("agent_trace", []),
                {"stage": "self_check", "passed": check.passed, "issues": check.issue_codes},
            ],
        }
        return {**settled, "recommendation_id": _resolve_recommendation_id(settled)}
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
    cleared = {
        **state,
        "recommendation_id": None,
        "self_check": check,
        "self_check_reranked": True,
        "agent_trace": [
            *state.get("agent_trace", []),
            {"stage": "self_check", "passed": False, "action": "CLEAR_RECOMMENDATION"},
        ],
    }
    return {**cleared, "recommendation_id": _resolve_recommendation_id(cleared)}


def _resolve_recommendation_id(state: SalesAgentState) -> str | None:
    """**누가 추천인지 확정한다.** 숫자와 규칙만 쓴다 — 모델은 오지 않는다.

    순위를 거쳐 온 길은 이미 첫 자리를 들고 있다. 순위를 건너뛴 길(입력 미비 ·
    검증 대기)은 여기서 처음 추천을 정한다. 그리고 거절된 안이 추천에 앉아 있으면
    추천을 **비운다** — 막힌 안을 권할 수는 없다.
    """
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
    return recommendation_id


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


def _interpret_recommendation(state: SalesAgentState) -> SalesAgentState:
    """**정해진 추천을 말로 옮긴다.** 그래프에서 모델이 불리는 유일한 자리다.

    이 node 는 아무것도 고르지 않는다. 추천은 `rank_candidates` 가 정하고
    `self_check` 가 확정한 뒤 여기 도착한다. 수량·단가·금액·날짜·결제조건은
    읽지도 넘기지도 않는다 — 모델에는 라벨만 간다.

    ★ 검증이 끝나지 않은 안(`UNRESOLVED`)과 막힌 안(`INFEASIBLE`)은
      `_interpret_scenarios` 가 애초에 후보에서 뺀다. 그래서 첫 실행에서는 설명할
      것이 없어 `SKIPPED_TEMPLATE` 로 끝나고 모델을 부르지 않는다. **node 를 따로
      세웠다는 이유로 판정 전 안을 설명하게 두면, 보지 않은 안이 제안처럼 읽힌다.**
    """
    from app.sales.proposal import _interpret_scenarios

    scenarios = state.get("validated_candidates", state.get("candidates", []))
    recommendation_id = state.get("recommendation_id")
    recommendation = _interpret_scenarios(scenarios, recommendation_id)
    return {
        **state,
        "recommendation": recommendation,
        "agent_trace": [
            *state.get("agent_trace", []),
            {
                "stage": "interpret_recommendation",
                "recommended_scenario_id": recommendation_id,
                "llm_status": recommendation.status,
                "llm_attempts": recommendation.llm_attempts,
            },
        ],
    }


def _final_recommendation(state: SalesAgentState) -> SalesAgentState:
    """**답장을 조립한다.** 새로 정하는 것은 없다 — 앞에서 정해진 것을 옮겨 담는다."""
    from app.sales.proposal import _missing_capabilities, _reply_refs

    request = state["request"]
    scenarios = state.get("validated_candidates", state.get("candidates", []))
    ranked_ids = state.get("ranked_candidate_ids", [])
    recommendation_id = state.get("recommendation_id")
    recommendation = state["recommendation"]
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
            policy_model_refs=(
                [request.ml_context.model_version]
                if scenario.ml_support_used and request.ml_context
                else []
            ),
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
            policy_model_refs=(
                [request.ml_context.model_version]
                if scenario.ml_support_used and request.ml_context
                else []
            ),
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
            policy_model_refs=(
                [request.ml_context.model_version]
                if scenario.ml_support_used and request.ml_context
                else []
            ),
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
        # 🔴 **장애를 숨기지 않는다** (§10). 모델이 실패했는데 성공처럼 보이면 모델이
        #   죽은 날과 산 날이 화면에서 같아진다.
        **_strategy_fields(state),
        # ★ 제약 때문에 숫자가 수렴한 사실은 **버그가 아니라 결과의 일부**다 (§8).
        **_strategy_collapse(scenarios),
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


def _strategy_collapse(scenarios) -> dict[str, object]:
    """자세는 갈렸는데 **숫자가 수렴했는가.** 숫자를 벌리지 않고 원인만 남긴다.

    ```text
    자세가 한 가지뿐이다          → 수렴이 아니다. 애초에 나뉜 적이 없다
    자세는 여럿인데 단가가 한 가지 → 수렴이다. 무엇이 묶었는지를 적는다
    ```

    🔴 **수렴 원인을 지어내지 않는다.** 코드는 결정론 계산이 실제로 쓴 것
      (`price_strategy_codes`)에서만 온다 — `MARGIN_FLOOR` 가 세 안을 다 묶었으면
      그 이름이 거기 있다.

    ★ **단가가 없는 안은 안 센다.** 가격을 못 만든 것과 같은 값에 닿은 것은 다르다.
    """
    by_price: dict[object, list] = {}
    for scenario in scenarios:
        if scenario.unit_price_krw is not None:
            by_price.setdefault(scenario.unit_price_krw, []).append(scenario)

    reasons: set[str] = set()
    collapsed = False
    for group in by_price.values():
        if len(group) < 2 or len({_posture_of(s) for s in group}) < 2:
            # 같은 자세끼리 같은 값인 것은 수렴이 아니다 — 애초에 안 나뉜 것이다.
            continue
        collapsed = True
        # ★ **그 묶임을 다 설명하는 코드만** 원인이다. 한 안에만 있는 코드는
        #   왜 둘이 같은 값에 닿았는지를 말해 주지 못한다.
        reasons |= set.intersection(*(set(s.price_strategy_codes) for s in group))
    if not collapsed:
        return {}
    return {"strategy_collapsed": True, "strategy_collapse_reason_codes": sorted(reasons)}


def _posture_of(scenario) -> str | None:
    profile = scenario.strategy_profile
    return getattr(profile, "price_posture", None) if profile is not None else None


def _strategy_fields(state: SalesAgentState) -> dict[str, object]:
    """전략 출처 세 칸. **계획이 없으면 "꺼져 있었다" 가 아니라 "안 세웠다" 다.**

    ★ 입력이 모자라 전략 노드를 지나지 않은 길에서는 계획 자체가 없다 — 그때는
      `SKIPPED_TEMPLATE` 이다. `DISABLED` 로 적으면 설정을 안 켠 것처럼 읽힌다
      (envelope §LLMStatus 가 가른 바로 그 둘).
    """
    plan = state.get("strategy_plan")
    if plan is None:
        return {"strategy_source": "TEMPLATE_FALLBACK", "strategy_llm_status": "SKIPPED_TEMPLATE"}
    return {
        "strategy_source": plan.source,
        "strategy_llm_status": plan.llm_status,
        "strategy_clamped_reason_codes": list(plan.clamped_reason_codes),
        "strategy_llm_failure_reason": plan.llm_failure_reason,
    }


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
