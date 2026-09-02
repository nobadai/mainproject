"""영업 Agent 계산 실행과 실행이력 저장·조회 흐름.

동결된 요청 입력만으로 계산하고, 실행 중 원본 DB를 다시 조회하지 않는다.
DB는 계산 결과의 실행이력을 저장·조회하는 용도로만 쓴다.

입력·출력은 팀 공통 I/O 계약(캐논)의 구조에 맞춘다. 다만 미구현 결과를 정상 업무 결과처럼
채우지 않는다.
- 계산 가능한 값은 실제값으로 낸다(floor_vector, band, 확정 의무량).
- 값이 미정이면 null로 둔다(0으로 대체하지 않는다).
- 목록 결과가 아직 없으면 빈 배열로 둔다(가짜 후보·경고를 만들지 않는다).
- 검사가 미구현이면 passed=null과 명시적 skip_reason(*_NOT_IMPLEMENTED)으로 표시한다.
- runtime_status는 캐논 출력 본문에 없어 응답에는 넣지 않고 실행이력 DB 컬럼 용도로만 내부 계산한다.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from app.sales.llm.runtime import interpret_candidates
from app.sales.run_repository import (
    get_sales_agent_run,
    list_sales_agent_runs,
    save_sales_agent_run,
)
from app.sales.schemas import (
    AllocationLeg,
    BaseSalesProposal,
    ExternalValidationResult,
    FloorVectorEntry,
    HardConstraintResult,
    MissingCapability,
    Rationale,
    RuntimeStatus,
    SalesAgentRunResponse,
    SalesAllocationInput,
    SalesAllocationReply,
    SalesBand,
    SalesCandidate,
    SalesCycle,
    SalesFloorInput,
    SalesFloorReply,
    SalesProposalMeta,
    SalesSelfCheck,
    SuggestedAdjustment,
)
from app.sales.tools import build_floor_vector, resolve_today_floor


def run_floor_reply(request: SalesFloorInput) -> SalesFloorReply:
    """사이클 A 매입 하한을 계산하고 캐논 T2 회신으로 조립해 실행이력을 저장한다."""
    snapshot = request.sales_snapshot
    floor_vector = build_floor_vector(snapshot)
    today_floor = resolve_today_floor(floor_vector, snapshot.inbound_lead_days, snapshot.as_of)
    binding_delivery_date = _resolve_binding_date(
        floor_vector, snapshot.inbound_lead_days, snapshot.as_of, today_floor
    )
    runtime_status: RuntimeStatus = (
        "READY" if snapshot.inbound_lead_days is not None else "RUNTIME_NOT_READY"
    )

    reply = SalesFloorReply(
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        policy_version=snapshot.policy_version,
        item=snapshot.item,
        # 소프트 경고·제약 판정(SAL-09~10)이 미구현이라 자기 회신 상태를 아직 평가하지 못한다.
        verdict=None,
        band=SalesBand(today_floor_kg=today_floor, binding_delivery_date=binding_delivery_date),
        floor_vector=[
            FloorVectorEntry(date=due_date, kg=kg) for due_date, kg in floor_vector.items()
        ],
        # 하드 제약 3종은 미구현이라 가짜 통과 없이 미구현으로 표시한다.
        hard_constraints=_not_implemented_hard_constraints(),
        # 소프트 경고 판정이 미구현이라 빈 목록으로 둔다.
        soft_warnings=[],
        suggested_adjustment=SuggestedAdjustment(
            axis="quantity",
            action="floor",
            # 미결(None)이면 0으로 대체하지 않는다. 0과 null은 다른 상태다.
            min_qty_kg=today_floor,
        ),
    )
    save_sales_agent_run(
        cycle="PROCUREMENT",
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        runtime_status=runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=reply.model_dump(mode="json"),
    )
    return reply


def run_allocation(request: SalesAllocationInput) -> SalesAllocationReply:
    """사이클 B 판매 배분 제안을 캐논 S1 SalesProposal로 조립해 실행이력을 저장한다.

    근거가 있는 기회 사실만으로 최대 세 개의 초안 후보를 조립한다. Inventory, Finance,
    Purchase의 사실은 재계산하지 않고 담당 Agent의 권위 있는 refeed 제약을 반영한다.
    """
    snapshot = request.sales_snapshot
    approval_id = request.approved_purchase.approval_id if request.approved_purchase else None

    # confirmed_orders는 비-nullable이라 항상 리스트다. []는 "확정 주문 없음"이라는 사실이다.
    confirmed_obligation = sum((o.qty_kg for o in snapshot.confirmed_orders), start=Decimal(0))

    situation = _classify_situation(request)
    base_proposals = _build_base_proposals(request)
    candidates = _build_candidates(request, base_proposals)
    missing_capabilities = _missing_capabilities(candidates, request.refeed_results)
    no_feasible_reason = None
    no_feasible_message = None
    if not candidates:
        no_feasible_reason = (
            "PRICE_EVIDENCE_MISSING"
            if snapshot.sales_opportunities
            else "CANDIDATE_EVIDENCE_MISSING"
        )
        no_feasible_message = (
            "현재 정보만으로 판매가격을 확정하기 어렵습니다. "
            "계약가, 기존 거래가격 또는 시장가격 정보가 필요합니다."
            if snapshot.sales_opportunities
            else "판매안을 만들 수 있는 계약 또는 판매 기회 정보가 부족합니다."
        )
    self_check = _self_check(candidates)
    recommendation = interpret_candidates(candidates)

    reply = SalesAllocationReply(
        meta=SalesProposalMeta(
            as_of=snapshot.as_of,
            item=snapshot.item,
            snapshot_id=snapshot.snapshot_id,
            approval_id=approval_id,
            policy_version=snapshot.policy_version,
            agent_version="v1.4",
        ),
        candidates=candidates,
        confirmed_obligation_kg=confirmed_obligation,
        # Sales는 로컬 inventory snapshot으로 권위 있는 가용량을 산출하지 않는다.
        coverable_kg=None,
        no_feasible_reason=no_feasible_reason,
        no_feasible_message=no_feasible_message,
        missing_capabilities=missing_capabilities,
        business_mode=snapshot.business_mode,
        situation=situation,
        self_check=self_check,
        base_proposals=base_proposals,
        recommendation=recommendation,
    )
    save_sales_agent_run(
        cycle="SALES",
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        runtime_status="READY" if candidates else "RUNTIME_NOT_READY",
        request_payload=request.model_dump(mode="json"),
        response_payload=reply.model_dump(mode="json"),
    )
    return reply


def _classify_situation(request: SalesAllocationInput) -> str:
    """입력 사실만으로 판매 상황을 분류하며 미정 Spot 판매를 자동 선택하지 않는다."""
    snapshot = request.sales_snapshot
    if snapshot.business_mode is not None:
        return snapshot.business_mode
    if snapshot.confirmed_orders:
        return "CONTRACT_FULFILLMENT"
    if snapshot.sales_opportunities:
        return "CONTRACT_PROPOSAL"
    return "UNRESOLVED"


def _build_base_proposals(request: SalesAllocationInput) -> list[BaseSalesProposal]:
    """근거가 완비된 기회를 외부 제약 적용 전 원안으로 고정한다."""
    opportunities = request.sales_snapshot.sales_opportunities or []
    proposals: list[BaseSalesProposal] = []
    seen: set[tuple[object, ...]] = set()
    for opportunity in opportunities:
        # 실행 가능한 배분에는 수량·납기가 필요하고, 가격 없는 리드는 제안처럼 취급하지 않는다.
        if (
            opportunity.qty_kg is None
            or opportunity.delivery_date is None
            or opportunity.unit_price is None
        ):
            continue
        signature = (
            opportunity.channel,
            opportunity.qty_kg,
            opportunity.unit_price,
            opportunity.delivery_date,
            opportunity.payment_days,
            opportunity.contract_term_days,
        )
        if signature in seen:
            continue
        seen.add(signature)
        proposals.append(
            BaseSalesProposal(
                proposal_id=opportunity.opportunity_id,
                source_opportunity_id=opportunity.opportunity_id,
                allocation=AllocationLeg(
                    channel=opportunity.channel,
                    qty_kg=opportunity.qty_kg,
                    unit_price=opportunity.unit_price,
                ),
                delivery_date=opportunity.delivery_date,
                payment_days=opportunity.payment_days,
                contract_term_days=opportunity.contract_term_days,
                evidence_ref=opportunity.evidence_ref,
            )
        )
        if len(proposals) == 3:
            break
    return proposals


def _build_candidates(
    request: SalesAllocationInput, base_proposals: list[BaseSalesProposal]
) -> list[SalesCandidate]:
    """원안마다 확정 공급안과 근거가 있는 조건부 조달안을 최대 세 개로 파생한다."""
    candidates: list[SalesCandidate] = []
    for base in base_proposals:
        candidate = SalesCandidate(
            candidate_id=f"{base.proposal_id}:CONFIRMED",
            base_proposal_id=base.proposal_id,
            strategy_label="확정 공급 우선",
            allocation=[base.allocation],
            outbound_by_date=[{"date": base.delivery_date, "kg": base.allocation.qty_kg}],
            rationale=[
                {
                    "source": "SALES_OPPORTUNITY",
                    "claim": "Master가 전달한 상업 조건",
                    "ref_id": base.evidence_ref,
                }
            ],
            adjustment_axis="NONE",
            payment_days=base.payment_days,
            contract_term_days=base.contract_term_days,
            uncertainties=[] if base.evidence_ref else ["EVIDENCE_REF_MISSING"],
        )
        candidates.append(_apply_refeed(candidate, request.refeed_results))
        purchase = _conditional_purchase_result(base.proposal_id, request.refeed_results)
        if purchase is not None and len(candidates) < 3:
            conditional = SalesCandidate.model_validate(candidate.model_dump())
            conditional.candidate_id = f"{base.proposal_id}:CONDITIONAL_PURCHASE"
            conditional.strategy_label = "추가 확보 조건부"
            conditional.conditional = True
            # 조건부안은 원안 수량을 유지하므로 확정 공급안의 수량 조정축을 물려받지 않는다.
            conditional.adjustment_axis = "NONE"
            conditional.rationale.append(
                Rationale(
                    source="PURCHASE",
                    claim="추가 확보 결과를 전제로 한 판매안",
                    ref_id=purchase.ref_id,
                )
            )
            conditional.uncertainties.append("PURCHASE_SUPPLY_CONDITIONAL")
            conditional.messages.append(
                "추가 매입 물량은 아직 확정되지 않아 조건부로 반영했습니다."
            )
            if purchase.additional_qty_kg is not None:
                conditional.allocation[0].qty_kg = base.allocation.qty_kg
                conditional.outbound_by_date[0].kg = base.allocation.qty_kg
            if purchase.available_date is not None:
                conditional.outbound_by_date[0].date = purchase.available_date
                conditional_data = conditional.model_dump()
                _add_adjustment_axis(conditional_data, "DELIVERY")
                conditional = SalesCandidate.model_validate(conditional_data)
            candidates.append(_apply_refeed(conditional, request.refeed_results))
        if len(candidates) == 3:
            break
    return _deduplicate_candidates(candidates)


def _conditional_purchase_result(proposal_id: str, results: list[ExternalValidationResult]):
    """추가 수량과 확보일이 함께 있는 조건부 Purchase 결과만 별도 판매안 근거로 사용한다."""
    for result in results:
        if (
            result.candidate_id in {proposal_id, f"{proposal_id}:CONFIRMED"}
            and result.source == "PURCHASE"
            and result.conditional
            and result.additional_qty_kg is not None
            and result.available_date is not None
        ):
            return result
    return None


def _deduplicate_candidates(candidates: list[SalesCandidate]) -> list[SalesCandidate]:
    """동일 조건의 시나리오는 하나만 유지한다."""
    unique: list[SalesCandidate] = []
    seen: set[tuple[object, ...]] = set()
    for candidate in candidates:
        leg, outbound = candidate.allocation[0], candidate.outbound_by_date[0]
        signature = (
            leg.qty_kg,
            leg.unit_price,
            outbound.date,
            candidate.payment_days,
            candidate.conditional,
        )
        if signature not in seen:
            unique.append(candidate)
            seen.add(signature)
    return unique[:3]


def _apply_refeed(
    candidate: SalesCandidate, results: list[ExternalValidationResult]
) -> SalesCandidate:
    """전달받은 한도로 후보를 재구성하며 타 도메인 계산을 하지 않는다."""
    matching = [
        result
        for result in results
        if result.candidate_id in {candidate.candidate_id, candidate.base_proposal_id}
    ]
    data = candidate.model_dump()
    refs = [result.ref_id for result in matching if result.ref_id]
    data["external_validation_refs"] = refs
    for result in matching:
        if result.verdict == "FAIL":
            data["risks"].append(f"{result.source}_FAIL")
            data["messages"].append(_validation_failure_message(result.source))
            data["uncertainties"].extend(result.unresolved_fields)
            continue
        if result.source == "LOGISTICS" and not candidate.conditional:
            current_qty = data["allocation"][0]["qty_kg"]
            if result.max_qty_kg is not None and result.max_qty_kg < current_qty:
                data["allocation"][0]["qty_kg"] = result.max_qty_kg
                data["outbound_by_date"][0]["kg"] = result.max_qty_kg
                _add_adjustment_axis(data, "QUANTITY")
                data["messages"].append(
                    "요청 수량 전체를 공급하기 어려워 공급 가능한 수량 기준으로 조정했습니다."
                )
            if result.earliest_delivery_date is not None:
                data["outbound_by_date"][0]["date"] = result.earliest_delivery_date
                _add_adjustment_axis(data, "DELIVERY")
                data["messages"].append("납품 가능 일정을 기준으로 납기일을 조정했습니다.")
        elif result.source == "FINANCE" and result.max_payment_days is not None:
            data["payment_days"] = result.max_payment_days
            _add_adjustment_axis(data, "PAYMENT_TERMS")
            data["messages"].append(
                "요청한 결제조건이 현재 재무 기준을 초과하여 허용 가능한 기간으로 조정했습니다."
            )
        elif result.source == "PURCHASE" and result.conditional and (
            candidate.conditional
            or result.additional_qty_kg is None
            or result.available_date is None
        ):
            data["conditional"] = True
            data["uncertainties"].append("PURCHASE_SUPPLY_CONDITIONAL")
            data["messages"].append("추가 매입 물량은 아직 확정되지 않아 조건부로 반영했습니다.")
        data["uncertainties"].extend(result.unresolved_fields)
    # 같은 미해결 필드가 재전달되어도 안정적으로 한 번만 유지한다.
    data["risks"] = list(dict.fromkeys(data["risks"]))
    data["uncertainties"] = list(dict.fromkeys(data["uncertainties"]))
    data["messages"] = list(dict.fromkeys(data["messages"]))
    return SalesCandidate.model_validate(data)


def _validation_failure_message(source: str) -> str:
    """외부 검증 실패를 코드 대신 사용자가 이해할 수 있는 문장으로 함께 제공한다."""
    return {
        "FINANCE": "현재 재무 기준을 충족하지 못해 결제조건 또는 거래 조건 조정이 필요합니다.",
        "LOGISTICS": "현재 물류 조건으로는 판매안을 확정하기 어렵습니다.",
        "PURCHASE": "추가 매입 가능 여부를 다시 확인해야 합니다.",
    }[source]


def _self_check(candidates: list[SalesCandidate]) -> SalesSelfCheck:
    """Sales가 조립한 값의 근거·중복·조정축만 검사하고 타 도메인 판단은 건드리지 않는다."""
    issue_codes: list[str] = []
    messages: list[str] = []
    if len(candidates) > 3:
        issue_codes.append("CANDIDATE_LIMIT_EXCEEDED")
    seen: set[tuple[object, ...]] = set()
    for candidate in candidates:
        leg = candidate.allocation[0] if candidate.allocation else None
        outbound = candidate.outbound_by_date[0] if candidate.outbound_by_date else None
        if leg is None or leg.unit_price is None or outbound is None:
            issue_codes.append("UNSUPPORTED_COMMERCIAL_VALUE")
        has_adjustment = any("조정했습니다" in message for message in candidate.messages)
        if (candidate.adjustment_axis == "NONE" and has_adjustment) or (
            candidate.adjustment_axis != "NONE" and not has_adjustment
        ):
            issue_codes.append("ADJUSTMENT_AXIS_INCONSISTENT")
        signature = (
            leg.channel if leg else None,
            leg.qty_kg if leg else None,
            leg.unit_price if leg else None,
            outbound.date if outbound else None,
            candidate.payment_days,
            candidate.contract_term_days,
        )
        if signature in seen:
            issue_codes.append("DUPLICATE_CANDIDATE")
        seen.add(signature)
    issue_codes = list(dict.fromkeys(issue_codes))
    if issue_codes:
        messages.append("판매안을 다시 확인해야 하는 항목이 있습니다. 근거와 조건을 검토해 주세요.")
    return SalesSelfCheck(passed=not issue_codes, issue_codes=issue_codes, messages=messages)


def _add_adjustment_axis(data: dict[str, object], axis: str) -> None:
    """한 가지 또는 여러 전달 변경사항과 adjustment axis를 일치시킨다."""
    current = data["adjustment_axis"]
    data["adjustment_axis"] = axis if current == "NONE" else "MIX" if current != axis else current


def _missing_capabilities(
    candidates: list[SalesCandidate], results: list[ExternalValidationResult]
) -> list[MissingCapability]:
    capabilities: list[MissingCapability] = []
    for candidate in candidates:
        capabilities.extend(
            [
                MissingCapability(
                    candidate_id=candidate.candidate_id,
                    capability="FINANCE_SALES_VALIDATION",
                    reason="결제 조건·신용·매출채권·현금 상태는 Finance 검증이 필요합니다.",
                ),
                MissingCapability(
                    candidate_id=candidate.candidate_id,
                    capability="LOGISTICS_SUPPLY_FEASIBILITY",
                    reason="수량·신선도·납기 가능성은 Logistics 검증이 필요합니다.",
                ),
            ]
        )
        logistics_limit = any(
            result.candidate_id in {candidate.candidate_id, candidate.base_proposal_id}
            and result.source == "LOGISTICS"
            and result.max_qty_kg is not None
            for result in results
        )
        purchase_result = any(
            result.candidate_id in {candidate.candidate_id, candidate.base_proposal_id}
            and result.source == "PURCHASE"
            for result in results
        )
        if logistics_limit and not purchase_result:
            capabilities.append(
                MissingCapability(
                    candidate_id=candidate.candidate_id,
                    capability="ADDITIONAL_PROCUREMENT_FEASIBILITY",
                    reason="요청 수량을 유지하려면 추가 확보 가능 여부를 확인해야 합니다.",
                )
            )
    return capabilities


def get_sales_run(run_id: UUID) -> SalesAgentRunResponse:
    """실행이력 한 건을 조회한다."""
    return SalesAgentRunResponse.model_validate(get_sales_agent_run(run_id))


def list_sales_runs(
    *,
    cycle: SalesCycle | None = None,
    as_of: date | None = None,
    snapshot_id: str | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: int = 100,
) -> list[SalesAgentRunResponse]:
    """실행이력 목록을 조회한다."""
    rows = list_sales_agent_runs(
        cycle=cycle,
        as_of=as_of,
        snapshot_id=snapshot_id,
        runtime_status=runtime_status,
        limit=limit,
    )
    return [SalesAgentRunResponse.model_validate(row) for row in rows]


def _resolve_binding_date(floor_vector, inbound_lead_days, as_of, today_floor) -> date | None:
    """오늘 구속 하한을 만든 납기일. 하한이 0이거나 미결이면 None."""
    if inbound_lead_days is None or today_floor is None or today_floor <= 0:
        return None
    window_end = as_of + timedelta(days=inbound_lead_days)
    binding = [
        due_date
        for due_date, kg in floor_vector.items()
        if due_date <= window_end and kg == today_floor
    ]
    return min(binding) if binding else None


def _not_implemented_hard_constraints() -> list[HardConstraintResult]:
    """캐논 T2 하드 제약 3종. 판정 로직(SAL-09)이 미구현이라 모두 미구현으로 표시한다.

    입력이 null이라 건너뛴 상태가 아니라 검사 자체가 아직 없는 상태이므로,
    passed=null과 *_NOT_IMPLEMENTED skip_reason으로 두 상태를 구분한다.
    """
    return [
        HardConstraintResult(
            code=code, basis="BASE", passed=None, skip_reason=f"{code}_CHECK_NOT_IMPLEMENTED"
        )
        for code in ("CONFIRMED_DEMAND_TOTAL", "DELIVERY_DEADLINE", "DAILY_OUTBOUND_CAPACITY")
    ]
