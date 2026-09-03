"""최종 Sales Proposal Core.

다른 Domain의 수치·판정을 계산하지 않고, 전달된 사실로만 시나리오와 검증 의존성을
표현한다. 레거시 allocation 흐름은 이 모듈과 독립적으로 유지한다.
"""

from decimal import Decimal

from app.sales.llm.runtime import interpret_candidates
from app.sales.schemas import (
    AllocationLeg,
    ProposalSelfCheck,
    SalesCandidate,
    SalesDomainReply,
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
    scenarios = [] if missing_data else _generate_scenarios(request)
    missing = _missing_capabilities(request)
    check = self_check_scenarios(scenarios)
    recommendation = _interpret_scenarios(scenarios)
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
        recommended_scenario_id=recommendation.recommended_candidate_id,
        llm=recommendation,
        recommendation=recommendation,
        self_check=check,
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
        supply = _supply(scenario_quantity, confirmed)
        validations = _required_validations(request, supply, parent or scenario_id, delivery)
        replies = _replies_for_scenario(request, parent or scenario_id)
        # 조건부 Purchase 회신은 확정 공급안을 오염시키지 않는다.
        if scenario_type != "AGGRESSIVE":
            replies = [reply for reply in replies if reply.source_agent != "purchase"]
        risks, uncertainties, conditional = _feedback_effects(replies)
        uncertainties.extend(supply_uncertainties)
        if price is None:
            uncertainties.append("PRICE_CONTEXT_REQUIRED")
        if request.logistics_context and request.logistics_context.delivery_feasibility:
            delivery_status = request.logistics_context.delivery_feasibility.status
            if delivery_status != "READY":
                uncertainties.extend(request.logistics_context.delivery_feasibility.reason_codes)
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
        getattr(request.user_request, field) is not None
        for field in _RENEWAL_OVERRIDE_FIELDS
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


def _supply(quantity: Decimal, confirmed: Decimal | None) -> ScenarioSupply:
    # 0은 권위 있는 확정 공급량이며 null과 다르다.
    required = None if confirmed is None else max(Decimal(0), quantity - confirmed)
    return ScenarioSupply(
        confirmed_quantity_kg=confirmed,
        required_additional_quantity_kg=required,
        additional_supply_required=required is not None and required > 0,
    )


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
    if supply.additional_supply_required and not _has_reply(
        request, "ADDITIONAL_SUPPLY_CONTEXT", original_id
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
        if reply.source_agent == "purchase":
            purchase_risks, depends_on_purchase = _purchase_effects(reply)
            # Purchase가 전달한 위험 문구는 Sales가 새 코드로 재해석하지 않는다.
            risks.extend(purchase_risks)
            if depends_on_purchase:
                conditional = True
                uncertainties.append("PURCHASE_SUPPLY_CONDITIONAL")
    return risks, uncertainties, conditional


def _purchase_effects(reply: SalesDomainReply) -> tuple[list[str], bool]:
    """Purchase의 권위 회신을 조건부 공급 의존성으로만 소비한다.

    ``skipped + 0kg``도 응답된 capability이지만, 확보 가능한 조건부 물량이 없으므로
    Sales Scenario를 조건부로 표시하지 않는다. Purchase 수량은 확정 Logistics 공급에
    합산하거나 Sales 수량을 자동 조정하는 데 사용하지 않는다.
    """
    payload = reply.payload
    raw_risks = payload.get("risks", [])
    risks = (
        [risk for risk in raw_risks if isinstance(risk, str)] if isinstance(raw_risks, list) else []
    )
    quantity = payload.get("procurable_quantity_kg")
    has_positive_quantity = (
        isinstance(quantity, (int, float, Decimal))
        and not isinstance(quantity, bool)
        and quantity > 0
    )
    depends_on_purchase = (
        reply.runtime_status == "READY"
        and (reply.business_status or "").lower() == "ok"
        and has_positive_quantity
    )
    return risks, depends_on_purchase


def _interpret_scenarios(scenarios: list[SalesScenario]) -> SalesRecommendation:
    candidates = [
        SalesCandidate(
            candidate_id=scenario.scenario_id,
            allocation=[
                AllocationLeg(
                    channel="proposal",
                    qty_kg=scenario.quantity_kg or Decimal(0),
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
    ]
    return interpret_candidates(candidates)


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
            and "ADDITIONAL_SUPPLY_CONTEXT" not in scenario.required_validations
        ):
            issues.append("ADDITIONAL_SUPPLY_VALIDATION_MISSING")
        if not scenario.conditional_purchase and any(
            reply.source_agent == "purchase" for reply in scenario.domain_replies
        ):
            issues.append("PURCHASE_REFERENCE_LEAK")
    issues = list(dict.fromkeys(issues))
    return ProposalSelfCheck(
        passed=not issues,
        issue_codes=issues,
        messages=[] if not issues else ["판매안의 조건과 외부 검증 참조를 다시 확인해 주세요."],
    )
