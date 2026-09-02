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


def run_proposal(request: SalesProposalInput) -> SalesProposalReply:
    """입력 사실로 세 유형의 Sales 시나리오를 만들고 안전하게 추천한다."""
    scenarios = _generate_scenarios(request)
    missing = _missing_capabilities(request)
    check = self_check_scenarios(scenarios)
    recommendation = _interpret_scenarios(scenarios)
    return SalesProposalReply(
        business_mode=request.business_mode,
        scenarios=scenarios,
        missing_capabilities=missing,
        recommended_scenario_id=recommendation.recommended_candidate_id,
        recommendation=recommendation,
        self_check=check,
    )


def _generate_scenarios(request: SalesProposalInput) -> list[SalesScenario]:
    quantity, price, delivery, payment, term, refs = _baseline(request)
    if quantity is None:
        return []
    confirmed = _confirmed_supply(request)
    result: list[SalesScenario] = []
    for suffix, scenario_type, objective in _TYPES:
        scenario_quantity = quantity
        axes: list[str] = []
        collapsed = False
        collapse_reason = None
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
        validations = _required_validations(request, supply)
        replies = _replies_for_scenario(request, parent or scenario_id)
        # 조건부 Purchase 회신은 확정 공급안을 오염시키지 않는다.
        if scenario_type != "AGGRESSIVE":
            replies = [reply for reply in replies if reply.source_agent != "purchase"]
        risks, uncertainties, conditional = _feedback_effects(replies)
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
                contract_term_days=term,
                supply=supply,
                sales_decision_axes=axes,
                required_validations=validations,
                evidence_refs=refs + _reply_refs(replies),
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


def _baseline(request: SalesProposalInput):
    contract = request.contract_context
    user = request.user_request
    if request.business_mode == "CONTRACT_FULFILLMENT" and contract:
        quantity = contract.contract_quantity_kg
        price = contract.contract_unit_price_krw
        delivery = contract.contract_delivery_date
        payment = contract.contract_payment_days
        term = contract.contract_term_days
    else:
        quantity = user.requested_quantity_kg
        price = user.preferred_unit_price_krw
        delivery = user.preferred_delivery_date
        payment = user.preferred_payment_days
        term = user.preferred_contract_term_days
        if request.business_mode == "CONTRACT_PROPOSAL_RENEWAL" and contract:
            quantity = quantity if quantity is not None else contract.contract_quantity_kg
            price = price if price is not None else contract.contract_unit_price_krw
            delivery = delivery if delivery is not None else contract.contract_delivery_date
            payment = payment if payment is not None else contract.contract_payment_days
            term = term if term is not None else contract.contract_term_days
    refs = [contract.source_ref] if contract and contract.source_ref else []
    return quantity, price, delivery, payment, term, refs


def _confirmed_supply(request: SalesProposalInput) -> Decimal | None:
    context = request.logistics_context
    return (
        context.query_scope.max_confirmed_sellable_quantity_kg
        if context and context.query_scope
        else None
    )


def _supply(quantity: Decimal, confirmed: Decimal | None) -> ScenarioSupply:
    # 0은 권위 있는 확정 공급량이며 null과 다르다.
    required = None if confirmed is None else max(Decimal(0), quantity - confirmed)
    return ScenarioSupply(
        confirmed_quantity_kg=confirmed,
        required_additional_quantity_kg=required,
        additional_supply_required=required is not None and required > 0,
    )


def _required_validations(request: SalesProposalInput, supply: ScenarioSupply) -> list[str]:
    validations: list[str] = []
    if not _has_reply(request, "FINANCIAL_VALIDATION"):
        validations.append("FINANCIAL_VALIDATION")
    if request.logistics_context is None or request.logistics_context.query_scope is None:
        validations.append("SELLABLE_SUPPLY_CONTEXT")
    delivery = request.logistics_context.delivery_feasibility if request.logistics_context else None
    if delivery is None or delivery.status != "READY":
        validations.append("DELIVERY_FEASIBILITY_CONTEXT")
    if supply.additional_supply_required and not _has_reply(request, "ADDITIONAL_SUPPLY_CONTEXT"):
        validations.append("ADDITIONAL_SUPPLY_CONTEXT")
    return validations


def _missing_capabilities(request: SalesProposalInput) -> list[str]:
    capabilities: list[str] = []
    if request.logistics_context is None or request.logistics_context.query_scope is None:
        capabilities.append("SELLABLE_SUPPLY_CONTEXT")
    delivery = request.logistics_context.delivery_feasibility if request.logistics_context else None
    if delivery is None or delivery.status == "UNRESOLVED":
        capabilities.append("DELIVERY_FEASIBILITY_CONTEXT")
    if not _has_reply(request, "FINANCIAL_VALIDATION") and request.finance_context is None:
        capabilities.append("FINANCIAL_VALIDATION")
    return capabilities


def _has_reply(request: SalesProposalInput, capability: str) -> bool:
    return bool(
        request.feedback
        and any(r.capability == capability for r in request.feedback.domain_replies)
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
    return [
        reply
        for reply in request.feedback.domain_replies
        if reply.scenario_id in {None, original_id}
    ]


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
            conditional = True
            uncertainties.append("PURCHASE_SUPPLY_CONDITIONAL")
    return risks, uncertainties, conditional


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
        if scenario.revision > 0 and (
            not scenario.parent_scenario_id or "-R" not in scenario.scenario_id
        ):
            issues.append("REFEED_LINEAGE_INVALID")
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
