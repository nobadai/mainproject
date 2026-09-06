"""최종 Sales Proposal Core.

다른 Domain의 수치·판정을 계산하지 않고, 전달된 사실로만 시나리오와 검증 의존성을
표현한다. 레거시 allocation 흐름은 이 모듈과 독립적으로 유지한다.
"""

from decimal import Decimal

from pydantic import ValidationError

from app.sales.llm.runtime import interpret_candidates
from app.sales.ranking import rank_scenarios, recommended_scenario_id, remove_dominated_scenarios
from app.sales.schemas import (
    AllocationLeg,
    ProposalSelfCheck,
    PurchaseAdditionalSupplyResult,
    SalesCandidate,
    SalesDecisionTrace,
    SalesDomainReply,
    SalesFinanceReplySubset,
    SalesProposalInput,
    SalesProposalReply,
    SalesRecommendation,
    SalesScenario,
    ScenarioSupply,
)

_TYPES = (
    ("A", "CONSERVATIVE", "RISK_DEFENSE"),
    ("B", "BALANCED", "BALANCE"),
    ("C", "AGGRESSIVE", "SALES_OPPORTUNITY"),
)


def validate_context(request: SalesProposalInput) -> list[str]:
    """Sales가 원안을 만들 수 있는 최소 사실과 항목 일치만 결정론적으로 검사한다."""
    issues: list[str] = []
    contract = request.contract_context
    if request.business_mode == "CONTRACT_FULFILLMENT":
        if contract is None:
            issues.append("CONTRACT_CONTEXT_REQUIRED")
        elif contract.contract_quantity_kg is None:
            issues.append("CONTRACT_QUANTITY_REQUIRED")
    if request.business_mode == "CONTRACT_PROPOSAL_RENEWAL" and contract is None:
        issues.append("PREVIOUS_CONTRACT_CONTEXT_REQUIRED")
    if request.is_refeed and request.feedback_attempt <= 0:
        issues.append("REFEED_ATTEMPT_REQUIRED")
    if (
        request.feedback
        and request.is_refeed
        and request.feedback.attempt != request.feedback_attempt
    ):
        issues.append("REFEED_ATTEMPT_INCONSISTENT")
    if (
        request.business_mode != "CONTRACT_FULFILLMENT"
        and request.user_request.requested_quantity_kg is None
        and not (request.business_mode == "CONTRACT_PROPOSAL_RENEWAL" and contract)
    ):
        issues.append("PROPOSAL_QUANTITY_REQUIRED")
    expected_item = request.user_request.item
    for actual, code in (
        (contract.item if contract else None, "CONTRACT_ITEM_MISMATCH"),
        (request.ml_context.item if request.ml_context else None, "ML_ITEM_MISMATCH"),
        (
            request.logistics_context.query_scope.item
            if request.logistics_context and request.logistics_context.query_scope
            else None,
            "LOGISTICS_ITEM_MISMATCH",
        ),
    ):
        if actual is not None and actual != expected_item:
            issues.append(code)
    if request.feedback:
        known_refs = {reply.reply_ref for reply in request.feedback.domain_replies}
        expected_ids = {f"SALES-001-{suffix}" for suffix, _, _ in _TYPES}
        for feedback in request.feedback.scenario_feedback:
            if feedback.scenario_id not in expected_ids:
                issues.append("SCENARIO_FEEDBACK_UNKNOWN_SCENARIO")
            if any(reply_ref not in known_refs for reply_ref in feedback.reply_refs):
                issues.append("SCENARIO_FEEDBACK_UNKNOWN_REPLY_REF")
    return list(dict.fromkeys(issues))


def run_proposal(request: SalesProposalInput) -> SalesProposalReply:
    """입력 사실로 세 유형의 Sales 시나리오를 만들고 안전하게 추천한다."""
    missing_data = validate_context(request)
    generated = [] if missing_data else _generate_scenarios(request)
    scenarios, exclusions = remove_dominated_scenarios(generated)
    missing = _missing_capabilities(request)
    check = self_check_scenarios(scenarios)
    fixed_recommendation = recommended_scenario_id(scenarios)
    recommendation = _interpret_scenarios(scenarios, fixed_recommendation)
    ranked_ids = [scenario.scenario_id for scenario in rank_scenarios(scenarios)]
    trace = [
        SalesDecisionTrace(
            candidate_id=scenario.scenario_id,
            status=scenario.status,
            rank=(
                ranked_ids.index(scenario.scenario_id) + 1
                if scenario.scenario_id in ranked_ids
                else None
            ),
            recommended=scenario.scenario_id == fixed_recommendation,
            finance_verdict=scenario.finance_verdict,
            profitability_krw=scenario.contribution_margin_krw,
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
        for scenario in generated
        if scenario.scenario_id in exclusions
    )
    collapse_reasons = list(
        dict.fromkeys(
            scenario.variant_collapsed_reason
            for scenario in scenarios
            if scenario.variant_collapsed_reason
        )
    )
    return SalesProposalReply(
        status="INPUT_INCOMPLETE" if missing_data else "SCENARIOS_GENERATED",
        business_mode=request.business_mode,
        is_refeed=request.is_refeed,
        feedback_attempt=request.feedback_attempt,
        scenarios=scenarios,
        variant_collapsed=bool(collapse_reasons),
        variant_collapsed_reason=(
            collapse_reasons[0]
            if len(collapse_reasons) == 1
            else "SCENARIO_VARIANTS_PARTIALLY_COLLAPSED"
            if collapse_reasons
            else None
        ),
        missing_data=missing_data,
        missing_capabilities=missing,
        recommended_scenario_id=fixed_recommendation,
        llm=recommendation,
        recommendation=recommendation,
        self_check=check,
        decision_trace=trace,
    )


def _generate_scenarios(request: SalesProposalInput) -> list[SalesScenario]:
    quantity, price, delivery, payment, terms_type, term, source_ref, refs = _baseline(request)
    if quantity is None:
        return []
    result: list[SalesScenario] = []
    for suffix, scenario_type, objective in _TYPES:
        scenario_quantity = quantity
        axes: list[str] = []
        collapsed = False
        collapse_reason = None
        confirmed, supply_uncertainties = resolve_applicable_confirmed_supply(request, delivery)
        if scenario_type == "CONSERVATIVE" and confirmed is not None and confirmed < quantity:
            scenario_quantity = confirmed
            axes.append("QUANTITY")
        elif scenario_type == "BALANCED":
            # 권위 있는 중간 수량 근거가 없으면 임의 수치를 만들지 않는다.
            collapsed = True
            collapse_reason = "AUTHORITATIVE_INTERMEDIATE_OPTION_UNAVAILABLE"
        elif scenario_type == "CONSERVATIVE":
            collapsed = True
            collapse_reason = "CONFIRMED_SUPPLY_LIMIT_NOT_PROVIDED"
        scenario_id, parent, revision = _scenario_lineage(suffix, request)
        replies = _replies_for_scenario(request, parent or scenario_id)
        # 조건부 Purchase 회신은 확정 공급안을 오염시키지 않는다.
        if scenario_type != "AGGRESSIVE":
            replies = [reply for reply in replies if reply.source_agent != "purchase"]
        # 조건부 수량은 회신에서 나오므로 회신을 먼저 고른 뒤 공급을 세운다.
        supply = _supply(scenario_quantity, confirmed, replies)
        unmet_quantity = None
        purchase = _purchase_result(replies)
        if scenario_type == "AGGRESSIVE" and confirmed is not None and purchase is not None:
            procurable = purchase.procurable_quantity_kg
            if procurable is not None:
                supported = confirmed + min(max(quantity - confirmed, Decimal(0)), procurable)
                scenario_quantity = min(quantity, supported)
                unmet_quantity = quantity - scenario_quantity
                supply = _supply(scenario_quantity, confirmed, replies)
                if scenario_quantity != quantity:
                    axes.append("QUANTITY")
        validations = _required_validations(request, supply, parent or scenario_id, delivery)
        risks, uncertainties, conditional = _feedback_effects(replies)
        finance = _finance_reply(replies)
        sell_priority, inventory_severity, remaining_freshness = _logistics_ranking_facts(replies)
        dependencies = _dependencies(request, supply, purchase, delivery, replies)
        if scenario_type == "BALANCED":
            payment, finance, finance_adjusted = _finance_payment_alternative(
                payment, finance, replies
            )
            if finance_adjusted:
                axes.append("PAYMENT_TERMS")
                dependencies.extend(
                    ["USER_PAYMENT_TERM_ACCEPTANCE_REQUIRED", "FINANCE_REVALIDATION_REQUIRED"]
                )
        uncertainties.extend(supply_uncertainties)
        if price is None:
            uncertainties.append("PRICE_CONTEXT_REQUIRED")
        if request.logistics_context and request.logistics_context.delivery_feasibility:
            delivery_status = request.logistics_context.delivery_feasibility.status
            if delivery_status != "READY":
                uncertainties.extend(request.logistics_context.delivery_feasibility.reason_codes)
        ml_support, ml_issue = _ml_support(request, delivery)
        if ml_issue:
            uncertainties.append(ml_issue)
        status = _candidate_status(
            request=request,
            scenario_type=scenario_type,
            validations=validations,
            supply=supply,
            purchase=purchase,
            finance=finance,
            dependencies=dependencies,
            replies=replies,
            unmet_quantity=unmet_quantity,
        )
        result.append(
            SalesScenario(
                scenario_id=scenario_id,
                parent_scenario_id=parent,
                revision=revision,
                scenario_type=scenario_type,
                objective=objective,
                business_mode=request.business_mode,
                item=request.user_request.item,
                partner_id=request.user_request.partner_id
                or (request.contract_context.partner_id if request.contract_context else None),
                quantity_kg=scenario_quantity,
                unit_price_krw=price,
                sales_amount_krw=scenario_quantity * price if price is not None else None,
                delivery_date=delivery,
                payment_days=payment,
                payment_terms_type=terms_type,
                contract_term_days=term,
                source_ref=source_ref,
                supply=supply,
                sales_decision_axes=axes,
                required_validations=validations,
                evidence_refs=_unique_refs(
                    refs + _logistics_refs(request, confirmed) + _reply_refs(replies)
                ),
                rationale=["전달된 계약·사용자 요청·외부 컨텍스트만 사용해 구성했습니다."],
                risks=risks,
                uncertainties=list(dict.fromkeys(uncertainties)),
                conditional_purchase=conditional,
                variant_collapsed=collapsed,
                variant_collapsed_reason=collapse_reason,
                domain_replies=replies,
                status=status,
                execution_dependencies=list(dict.fromkeys(dependencies)),
                unmet_quantity_kg=unmet_quantity,
                finance_verdict=finance.finance_verdict if finance else None,
                contribution_margin_krw=(
                    finance.financial_summary.contribution_margin_krw
                    if finance and finance.financial_summary
                    else None
                ),
                contribution_margin_rate=(
                    finance.financial_summary.contribution_margin_rate
                    if finance and finance.financial_summary
                    else None
                ),
                sell_priority=sell_priority,
                authoritative_inventory_risk_severity=inventory_severity,
                remaining_freshness_days=remaining_freshness,
                ml_support_used=ml_support,
            )
        )
    return result


#: 사용자가 이 중 하나라도 명시하면 **사용자 제안**으로 본다 (갱신 override 판정).
_RENEWAL_OVERRIDE_FIELDS: tuple[str, ...] = (
    "requested_quantity_kg",
    "preferred_unit_price_krw",
    "preferred_delivery_date",
    "preferred_payment_days",
    "preferred_payment_terms_type",
    "preferred_contract_term_days",
)


def _user_overrides_contract(request: SalesProposalInput) -> bool:
    """갱신 제안에서 사용자가 상업조건을 실제로 바꿨는가."""
    return any(
        getattr(request.user_request, field) is not None for field in _RENEWAL_OVERRIDE_FIELDS
    )


def _baseline(request: SalesProposalInput):
    contract = request.contract_context
    user = request.user_request
    if request.business_mode == "CONTRACT_FULFILLMENT" and contract:
        quantity = contract.contract_quantity_kg
        price = contract.contract_unit_price_krw
        delivery = contract.contract_delivery_date
        payment = contract.contract_payment_days
        terms_type = contract.contract_payment_terms_type
        term = contract.contract_term_days
        # 계약 이행은 계약이 상업조건의 직접 출발점이다.
        source_ref = contract.source_ref
    else:
        quantity = user.requested_quantity_kg
        price = user.preferred_unit_price_krw
        delivery = user.preferred_delivery_date
        payment = user.preferred_payment_days
        terms_type = user.preferred_payment_terms_type
        term = user.preferred_contract_term_days
        source_ref = user.source_ref
        if request.business_mode == "CONTRACT_PROPOSAL_RENEWAL" and contract:
            quantity = quantity if quantity is not None else contract.contract_quantity_kg
            price = price if price is not None else contract.contract_unit_price_krw
            delivery = delivery if delivery is not None else contract.contract_delivery_date
            payment = payment if payment is not None else contract.contract_payment_days
            terms_type = (
                terms_type if terms_type is not None else contract.contract_payment_terms_type
            )
            term = term if term is not None else contract.contract_term_days
            # ★ 사용자가 조건을 바꿨으면 **계약 ref 를 그 변경안의 출처로 쓰지 않는다.**
            #   바꾼 사람은 사용자인데 계약을 근거로 달면 누가 정한 조건인지 뒤바뀐다.
            #   사용자 ref 가 없으면 없는 채로 둔다 — 발명하지 않는다.
            if not _user_overrides_contract(request):
                source_ref = contract.source_ref
    refs = [contract.source_ref] if contract and contract.source_ref else []
    return quantity, price, delivery, payment, terms_type, term, source_ref, refs


def resolve_applicable_confirmed_supply(
    request: SalesProposalInput, delivery_date
) -> tuple[Decimal | None, list[str]]:
    """납기일에 맞는 Logistics 권위 수량만 사용하며 scope 최대값은 대체하지 않는다."""
    context = request.logistics_context
    supply = context.sellable_supply if context else None
    if not supply:
        return None, ["SELLABLE_SUPPLY_CONTEXT_REQUIRED"]
    if delivery_date is not None:
        entry = next(
            (entry for entry in supply.supply_capacity_by_date if entry.date == delivery_date), None
        )
        if entry is None:
            return None, ["SUPPLY_DATE_CONTEXT_REQUIRED"]
        return entry.confirmed_sellable_quantity_kg, list(entry.uncertainties)
    if supply.status == "READY":
        current = next(
            (
                entry
                for entry in supply.inventory_by_item
                if entry.item == request.user_request.item
            ),
            None,
        )
        if current is not None:
            return current.available_qty_kg, []
    return None, ["CURRENT_SELLABLE_SUPPLY_UNRESOLVED"]


def _supply(
    quantity: Decimal,
    confirmed: Decimal | None,
    replies: list[SalesDomainReply] | None = None,
) -> ScenarioSupply:
    # 0은 권위 있는 확정 공급량이며 null과 다르다.
    required = None if confirmed is None else max(Decimal(0), quantity - confirmed)
    conditional, dependency_ref = _purchase_conditional_supply(replies or [])
    return ScenarioSupply(
        confirmed_quantity_kg=confirmed,
        required_additional_quantity_kg=required,
        additional_supply_required=required is not None and required > 0,
        # ★ 확정 공급에 더하지 않는다. 조건부는 조건부 자리에만 산다.
        conditional_quantity_kg=conditional,
        dependency_ref=dependency_ref,
    )


#: Purchase 추가공급 회신으로 **읽을 수 있는 유일한** capability.
#:
#: 🔴 출처(source_agent)만 보면 안 된다. Purchase 의 `GENERATE_SCENARIOS` 회신은
#:    `scenarios[i].risks` 를 담은 완전히 다른 모양인데, 출처만 맞다고 추가공급
#:    결과로 읽으면 최상위 `risks` 가 없어 조용히 "위험 0건" 이 되고 수량은 None 이
#:    된다. 물어보지 않은 질문에 답을 받은 셈이 된다.
_ADDITIONAL_SUPPLY_CAPABILITY = "ADDITIONAL_SUPPLY_CONTEXT"


def _is_additional_supply_reply(reply: SalesDomainReply) -> bool:
    """이 회신을 추가공급 결과로 읽어도 되는가 — 출처와 capability 를 **둘 다** 본다."""
    return reply.source_agent == "purchase" and reply.capability == _ADDITIONAL_SUPPLY_CAPABILITY


def _parse_additional_supply(
    reply: SalesDomainReply,
) -> PurchaseAdditionalSupplyResult | None:
    """추가공급 회신 payload 를 약속한 모양으로만 읽는다.

    ★ 약속을 안 지킨 payload 는 **소비하지 않는다.** 키가 없으면 None 을 돌려주고,
      호출부는 그것을 정상 사실로 취급하지 않는다 — `[]`·`0` 으로 메우지 않는다.
    """
    try:
        return PurchaseAdditionalSupplyResult.model_validate(reply.payload)
    except ValidationError:
        return None


def _purchase_conditional_supply(
    replies: list[SalesDomainReply],
) -> tuple[Decimal | None, str | None]:
    """Purchase 가 **실제로 확인해 준** 조건부 확보 가능량만 옮긴다.

    ★ 0 은 사실이다. Purchase 가 "0kg 확보 가능" 이라고 답했으면 0으로 보존한다.
      `READY + skipped + 0kg` 도 정상 조합이다 — 안이 만들어지지 않았지만 확보
      가능량은 0kg 으로 확인됐다는 뜻이라, 그 0 을 None 이나 오류로 바꾸지 않는다.

    ★ 확보 가능량을 **모르는** 경우만 `None` 이다. 회신이 `RUNTIME_NOT_READY` 이거나,
      수량 칸을 명시적 null 로 보냈거나, 약속한 모양이 아니어서 읽을 수 없을 때다.
      0 으로 바꾸면 *답을 못 받은 것*이 *확보 가능량 0* 이라는 사실이 된다.

    ★ 수량과 근거를 같이 나른다. 수량만 남고 어느 회신에서 왔는지 사라지면 나중에
      되짚을 수 없다.
    """
    for reply in replies:
        if not _is_additional_supply_reply(reply):
            continue
        if reply.runtime_status != "READY":
            continue
        parsed = _parse_additional_supply(reply)
        if parsed is None or parsed.procurable_quantity_kg is None:
            continue
        return parsed.procurable_quantity_kg, reply.reply_ref
    return None, None


def _purchase_result(replies: list[SalesDomainReply]) -> PurchaseAdditionalSupplyResult | None:
    for reply in replies:
        if not _is_additional_supply_reply(reply) or reply.runtime_status != "READY":
            continue
        parsed = _parse_additional_supply(reply)
        if parsed is not None:
            return parsed
    return None


def _required_validations(
    request: SalesProposalInput, supply: ScenarioSupply, original_id: str, delivery_date
) -> list[str]:
    validations: list[str] = []
    if not _has_reply(request, "FINANCIAL_VALIDATION", original_id):
        validations.append("FINANCIAL_VALIDATION")
    if supply.confirmed_quantity_kg is None:
        validations.append("SELLABLE_SUPPLY_CONTEXT")
    delivery = request.logistics_context.delivery_feasibility if request.logistics_context else None
    if delivery is None or delivery.status != "READY":
        validations.append("DELIVERY_FEASIBILITY_CONTEXT")
    sourcing_allowed = not (
        request.business_mode == "SPOT_SALES" and not request.user_request.allow_additional_sourcing
    )
    if (
        supply.additional_supply_required
        and sourcing_allowed
        and not _has_reply(request, "ADDITIONAL_SUPPLY_CONTEXT", original_id)
    ):
        validations.append("ADDITIONAL_SUPPLY_CONTEXT")
    return validations


def _missing_capabilities(request: SalesProposalInput) -> list[str]:
    capabilities: list[str] = []
    if request.logistics_context is None or request.logistics_context.sellable_supply is None:
        capabilities.append("SELLABLE_SUPPLY_CONTEXT")
    delivery = request.logistics_context.delivery_feasibility if request.logistics_context else None
    if delivery is None or delivery.status == "UNRESOLVED":
        capabilities.append("DELIVERY_FEASIBILITY_CONTEXT")
    if not _has_reply(request, "FINANCIAL_VALIDATION") and request.finance_context is None:
        capabilities.append("FINANCIAL_VALIDATION")
    return capabilities


def _has_reply(
    request: SalesProposalInput, capability: str, original_id: str | None = None
) -> bool:
    if original_id is None:
        return bool(
            request.feedback
            and any(reply.capability == capability for reply in request.feedback.domain_replies)
        )
    return any(
        reply.capability == capability for reply in _replies_for_scenario(request, original_id)
    )


def _scenario_lineage(suffix: str, request: SalesProposalInput) -> tuple[str, str | None, int]:
    original = f"SALES-001-{suffix}"
    attempt = request.feedback.attempt if request.feedback else request.feedback_attempt
    if request.is_refeed and attempt > 0:
        return f"{original}-R{attempt}", original, attempt
    return original, None, 0


def _replies_for_scenario(request: SalesProposalInput, original_id: str) -> list[SalesDomainReply]:
    if not request.feedback:
        return []
    refs = next(
        (
            feedback.reply_refs
            for feedback in request.feedback.scenario_feedback
            if feedback.scenario_id == original_id
        ),
        [],
    )
    by_ref = {reply.reply_ref: reply for reply in request.feedback.domain_replies}
    return [by_ref[reply_ref] for reply_ref in refs if reply_ref in by_ref]


def _logistics_refs(request: SalesProposalInput, confirmed: Decimal | None) -> list[str]:
    """권위 Logistics 수량을 실제 사용한 경우에만 해당 근거를 연결한다."""
    return (
        list(request.logistics_context.evidence_refs)
        if confirmed is not None and request.logistics_context
        else []
    )


def _unique_refs(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(ref for ref in refs if ref))


def _reply_refs(replies: list[SalesDomainReply]) -> list[str]:
    return [reply.reply_ref for reply in replies]


def _feedback_effects(replies: list[SalesDomainReply]) -> tuple[list[str], list[str], bool]:
    risks: list[str] = []
    uncertainties: list[str] = []
    conditional = False
    for reply in replies:
        if reply.source_agent == "finance" and (reply.business_status or "").lower() in {
            "fail",
            "reject",
        }:
            risks.append("FINANCE_FAIL")
        if _is_additional_supply_reply(reply):
            purchase_risks, depends_on_purchase = _purchase_effects(reply)
            # Purchase가 전달한 위험 문구는 Sales가 새 코드로 재해석하지 않는다.
            risks.extend(purchase_risks)
            if depends_on_purchase:
                conditional = True
                uncertainties.append("PURCHASE_SUPPLY_CONDITIONAL")
    return risks, uncertainties, conditional


def _finance_reply(replies: list[SalesDomainReply]) -> SalesFinanceReplySubset | None:
    for reply in replies:
        if reply.source_agent != "finance" or reply.capability != "FINANCIAL_VALIDATION":
            continue
        try:
            parsed = SalesFinanceReplySubset.model_validate(reply.payload)
        except ValidationError:
            return None
        if parsed.finance_verdict is not None:
            return parsed
        fallback = {
            "ok": "PASS",
            "conditional": "REVIEW_REQUIRED",
            "reject": "FAIL",
            "fail": "FAIL",
        }.get((reply.business_status or "").lower())
        return parsed.model_copy(update={"finance_verdict": fallback})
    return None


def _finance_payment_alternative(payment, finance, replies):
    """Finance가 명시한 상한이 있을 때만 사용자 수락 대상 수정안을 만든다."""
    if finance is None or finance.finance_verdict != "FAIL" or payment is None:
        return payment, finance, False
    reply = next((item for item in replies if item.source_agent == "finance"), None)
    if reply is None:
        return payment, finance, False
    value = reply.payload.get("max_finance_allowed_payment_terms_days")
    if isinstance(value, bool) or not isinstance(value, int) or value >= payment or value < 0:
        return payment, finance, False
    # 원안 FAIL을 수정안 PASS로 바꾸지 않는다. 새 조건은 Finance 재검증 전이다.
    return value, None, True


def _dependencies(request, supply, purchase, delivery_date, replies):
    dependencies: list[str] = []
    if (
        purchase
        and purchase.procurable_quantity_kg is not None
        and purchase.procurable_quantity_kg > 0
    ):
        dependencies.append("PURCHASE_COMMITMENT_REQUIRED")
        if purchase.available_date is not None and purchase.available_date != delivery_date:
            dependencies.append("DELIVERY_REVALIDATION_REQUIRED")
    if _logistics_revalidation_required(replies):
        dependencies.append("DELIVERY_REVALIDATION_REQUIRED")
    finance = _finance_reply(replies)
    if finance and finance.finance_verdict == "REVIEW_REQUIRED":
        dependencies.append("FINANCE_REVALIDATION_REQUIRED")
    return dependencies


def _logistics_revalidation_required(replies: list[SalesDomainReply]) -> bool:
    marker = "LOGISTICS_REVALIDATION_REQUIRED"
    for reply in replies:
        if reply.source_agent != "logistics":
            continue
        if (reply.business_status or "").upper() == marker:
            return True
        reason_codes = reply.payload.get("reason_codes")
        if isinstance(reason_codes, list) and marker in reason_codes:
            return True
        constraints = reply.payload.get("hard_constraints")
        if isinstance(constraints, list):
            for constraint in constraints:
                if isinstance(constraint, dict) and constraint.get("code") == marker:
                    return True
    return False


def _logistics_ranking_facts(
    replies: list[SalesDomainReply],
) -> tuple[str | None, str | None, int | None]:
    """Logistics가 명시한 보조 사실만 읽고 severity나 우선순위를 만들지 않는다."""
    for reply in replies:
        if reply.source_agent != "logistics":
            continue
        priority = reply.payload.get("sell_priority")
        severity = reply.payload.get("inventory_risk_severity")
        freshness = reply.payload.get("remaining_freshness_days")
        return (
            priority if isinstance(priority, str) else None,
            severity if isinstance(severity, str) else None,
            freshness if isinstance(freshness, int) and not isinstance(freshness, bool) else None,
        )
    return None, None, None


def _candidate_status(
    *,
    request,
    scenario_type,
    validations,
    supply,
    purchase,
    finance,
    dependencies,
    replies,
    unmet_quantity,
):
    if _logistics_revalidation_required(replies):
        return "REVIEW_REQUIRED"
    if finance and finance.finance_verdict == "FAIL":
        return (
            "REVIEW_REQUIRED" if request.business_mode == "CONTRACT_FULFILLMENT" else "INFEASIBLE"
        )
    if finance and (
        finance.finance_verdict == "REVIEW_REQUIRED"
        or (
            finance.financial_summary
            and finance.financial_summary.overdue_ar_krw is not None
            and finance.financial_summary.overdue_ar_krw > 0
        )
    ):
        return "REVIEW_REQUIRED"
    if validations:
        return "UNRESOLVED"
    if (
        supply.additional_supply_required
        and purchase is not None
        and purchase.procurable_quantity_kg is None
    ):
        return "UNRESOLVED"
    if (
        request.business_mode == "SPOT_SALES"
        and not request.user_request.allow_additional_sourcing
        and supply.additional_supply_required
    ):
        return "INFEASIBLE"
    if (
        scenario_type == "AGGRESSIVE"
        and supply.additional_supply_required
        and purchase is not None
        and purchase.procurable_quantity_kg == 0
    ):
        return "INFEASIBLE"
    if unmet_quantity is not None and unmet_quantity > 0 and not dependencies:
        return "INFEASIBLE"
    if dependencies:
        return "CONDITIONAL"
    return "EXECUTABLE"


def _ml_support(request: SalesProposalInput, relevant_date) -> tuple[bool, str | None]:
    forecast = request.ml_context
    if forecast is None or forecast.use_recommended is not True:
        return False, None
    if relevant_date is None:
        return False, None
    point = next((point for point in forecast.daily if point.date == relevant_date), None)
    if point is None:
        return False, "ML_HORIZON_EXCEEDED"
    return True, None


def _purchase_effects(reply: SalesDomainReply) -> tuple[list[str], bool]:
    """Purchase의 권위 회신을 조건부 공급 의존성으로만 소비한다.

    ``READY + skipped + 0kg``도 응답된 capability이지만, 확보 가능한 조건부 물량이
    없으므로 Sales Scenario를 조건부로 표시하지 않는다. Purchase 수량은 확정 Logistics
    공급에 합산하거나 Sales 수량을 자동 조정하는 데 사용하지 않는다.

    🔴 약속한 모양이 아닌 payload 는 **위험 0건으로 읽지 않는다.** 예전에는
       `payload.get("risks", [])` 라서 `risks` 칸이 없는 회신이 "위험 없음" 이 됐다.
       확인하지 않은 것과 확인해서 없는 것은 다른 사실이다.
    """
    parsed = _parse_additional_supply(reply)
    if parsed is None:
        # 읽을 수 없는 회신에서 사실을 만들어 내지 않는다.
        return [], False
    depends_on_purchase = (
        reply.runtime_status == "READY"
        and (reply.business_status or "").lower() == "ok"
        and parsed.procurable_quantity_kg is not None
        and parsed.procurable_quantity_kg > 0
    )
    return list(parsed.risks), depends_on_purchase


def _interpret_scenarios(
    scenarios: list[SalesScenario], fixed_recommendation: str | None
) -> SalesRecommendation:
    candidates = [
        SalesCandidate(
            candidate_id=scenario.scenario_id,
            allocation=[
                AllocationLeg(
                    channel="proposal",
                    qty_kg=scenario.quantity_kg if scenario.quantity_kg is not None else Decimal(0),
                    unit_price=scenario.unit_price_krw,
                )
            ],
            adjustment_axis="MIX"
            if len(scenario.sales_decision_axes) > 1
            else (scenario.sales_decision_axes[0] if scenario.sales_decision_axes else "NONE"),
            conditional=scenario.conditional_purchase,
            risks=scenario.risks,
            uncertainties=scenario.uncertainties,
            strategy_label=scenario.scenario_type,
        )
        for scenario in scenarios
        if scenario.status in {"EXECUTABLE", "CONDITIONAL"}
    ]
    return interpret_candidates(candidates, recommended_candidate_id=fixed_recommendation)


def _purchase_reference_issues(scenario: SalesScenario) -> list[str]:
    """이 안에 붙은 Purchase 회신이 **여기 있어도 되는 것인가.**

    🔴 예전에는 `조건부 아님 + Purchase 회신 존재` 를 통째로 누수로 봤다. 그러면
       정상 조합인 `READY + skipped + 0kg`(= 확보 가능량 0kg 확인)까지 오류가 된다.
       회신이 붙어 있다는 사실과 조건부 물량에 의존한다는 사실은 다르다.

    지금 가르는 것은 셋이다.

        A. 추가공급이 아닌 Purchase capability 가 붙음  → capability 결합 오류
        B. 추가공급 검증이 필요 없는 안에 붙음          → scenario 결합 오류
        C. 약속한 모양이 아닌 추가공급 payload          → 읽을 수 없는 회신

    셋 다 "조용히 정상 처리" 하지 않는다.
    """
    issues: list[str] = []
    purchase_replies = [
        reply for reply in scenario.domain_replies if reply.source_agent == "purchase"
    ]
    if not purchase_replies:
        return issues
    for reply in purchase_replies:
        if reply.capability != _ADDITIONAL_SUPPLY_CAPABILITY:
            # A — 물어보지 않은 질문의 답이 붙었다.
            issues.append("PURCHASE_CAPABILITY_MISMATCH")
        elif _parse_additional_supply(reply) is None:
            # C — 추가공급이라고 왔는데 약속한 칸이 없다.
            issues.append("PURCHASE_SUPPLY_PAYLOAD_INVALID")
    if not scenario.supply.additional_supply_required and any(
        reply.capability == _ADDITIONAL_SUPPLY_CAPABILITY for reply in purchase_replies
    ):
        # B — 추가조달이 필요하지 않은 안에 그 검증 결과가 붙었다.
        #
        # ★ 판단 기준은 **이 안이 추가공급을 필요로 하는가**이지 `required_validations`
        #   에 남아 있는가가 아니다. 저 목록은 *아직 answered 되지 않은 요청*이라,
        #   답이 온 순간 사라진다 — 그것을 "묻지 않았다" 로 읽으면 정상 회신이 누수가 된다.
        issues.append("PURCHASE_REFERENCE_LEAK")
    return issues


def _answered_additional_supply(scenario: SalesScenario) -> bool:
    """이 안의 추가공급 질문에 **읽을 수 있는 답이 왔는가.**

    ★ 셋을 모두 만족해야 답으로 친다 — 출처가 매입이고, capability 가 추가공급이고,
      약속한 칸(`procurable_quantity_kg`·`risks`)이 실제로 있어야 한다.

    🔴 여기를 "매입 회신이 하나라도 있으면 답이 왔다" 로 넓히면, capability 가 틀린
       회신이나 칸이 빠진 회신이 검증을 끝낸 것으로 읽힌다. 물어본 것에 대한 답이
       아직 없는데 "확인했다" 가 되는 것이라, 오탐을 고치려다 미검증을 통과시킨다.
    """
    return any(
        reply.source_agent == "purchase"
        and reply.capability == _ADDITIONAL_SUPPLY_CAPABILITY
        and _parse_additional_supply(reply) is not None
        for reply in scenario.domain_replies
    )


def self_check_scenarios(scenarios: list[SalesScenario]) -> ProposalSelfCheck:
    """Sales가 소유한 식별자·금액·의존성 불변식을 검사한다."""
    issues: list[str] = []
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        issues.append("DUPLICATE_SCENARIO_ID")
    for scenario in scenarios:
        if scenario.revision == 0 and scenario.parent_scenario_id is not None:
            issues.append("INITIAL_LINEAGE_INVALID")
        if scenario.revision > 0 and (
            not scenario.parent_scenario_id or "-R" not in scenario.scenario_id
        ):
            issues.append("REFEED_LINEAGE_INVALID")
        if scenario.quantity_kg is not None and scenario.quantity_kg < 0:
            issues.append("NEGATIVE_QUANTITY")
        if scenario.unit_price_krw is not None and scenario.unit_price_krw < 0:
            issues.append("NEGATIVE_PRICE")
        expected_amount = (
            scenario.quantity_kg * scenario.unit_price_krw
            if scenario.quantity_kg is not None and scenario.unit_price_krw is not None
            else None
        )
        if scenario.sales_amount_krw != expected_amount:
            issues.append("SALES_AMOUNT_INCONSISTENT")
        if scenario.variant_collapsed and not scenario.variant_collapsed_reason:
            issues.append("VARIANT_COLLAPSE_REASON_MISSING")
        if scenario.supply.additional_supply_required != (
            scenario.supply.required_additional_quantity_kg is not None
            and scenario.supply.required_additional_quantity_kg > 0
        ):
            issues.append("SUPPLY_DEPENDENCY_INCONSISTENT")
        if (
            scenario.supply.additional_supply_required
            and _ADDITIONAL_SUPPLY_CAPABILITY not in scenario.required_validations
            # 🔴 답이 온 질문을 "안 물어봤다" 로 읽지 않는다. `required_validations` 는
            #    *아직 답이 없는 요청* 목록이라, 회신이 오면 사라지는 것이 정상이다.
            and not _answered_additional_supply(scenario)
            and not (scenario.business_mode == "SPOT_SALES" and scenario.status == "INFEASIBLE")
        ):
            issues.append("ADDITIONAL_SUPPLY_VALIDATION_MISSING")
        if not (scenario.unmet_quantity_kg is not None and scenario.unmet_quantity_kg > 0):
            issues.extend(_purchase_reference_issues(scenario))
    issues = list(dict.fromkeys(issues))
    return ProposalSelfCheck(
        passed=not issues,
        issue_codes=issues,
        messages=[] if not issues else ["판매안의 조건과 외부 검증 참조를 다시 확인해 주세요."],
    )
