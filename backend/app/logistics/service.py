"""재고·물류 Agent A/B 결정론적 실행 흐름."""

from datetime import date
from uuid import UUID

from app.logistics.interpretation import enrich_logistics_response, translate_missing_data
from app.logistics.llm.runtime import InterpretationService
from app.logistics.repository import get_current_inventory_logistics_snapshot
from app.logistics.rules import (
    FRESHNESS_QUALITY_RISK,
    SALES_PRIORITY_ADJUSTMENT,
    BusinessSignalResult,
    LogisticsRuleResult,
    derive_logistics_verdict,
    evaluate_procurement_business_signals,
    evaluate_procurement_rules,
    evaluate_sales_business_signals,
    evaluate_sales_rules,
    merge_business_warnings,
)
from app.logistics.run_repository import (
    get_logistics_agent_run,
    list_logistics_agent_runs,
    save_logistics_agent_run,
)
from app.logistics.scenario_engine import (
    derive_preferred_adjustment,
    run_logistics_procurement_scenario,
    run_logistics_sales_scenario,
)
from app.logistics.schemas import (
    FinalVerdict,
    InboundConstraints,
    InventoryLogisticsSnapshot,
    LogisticsAgentRunResponse,
    LogisticsBand,
    LogisticsCycle,
    LogisticsEvidence,
    LogisticsProcurementResponse,
    LogisticsSalesRequest,
    LogisticsSalesResponse,
    PurchaseAgentOutput,
    RuntimeStatus,
)


def _get_snapshot_or_none(*, as_of: date) -> InventoryLogisticsSnapshot | None:
    try:
        return get_current_inventory_logistics_snapshot(as_of=as_of)
    except LookupError:
        return None


def run_logistics_procurement(request: PurchaseAgentOutput) -> LogisticsProcurementResponse:
    """현재 Snapshot과 Purchase 날짜 축으로 Logistics A Band를 만든다."""
    snapshot = _get_snapshot_or_none(as_of=request.meta.as_of)
    response = run_logistics_procurement_with_snapshot(request, snapshot)
    save_logistics_agent_run(
        cycle="PROCUREMENT",
        as_of=request.meta.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        verdict=response.verdict,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def run_logistics_procurement_with_snapshot(
    request: PurchaseAgentOutput,
    snapshot: InventoryLogisticsSnapshot | None,
    interpretation_service: InterpretationService | None = None,
) -> LogisticsProcurementResponse:
    """외부에서 고정한 Inventory/Logistics Snapshot으로 A Cycle을 실행한다."""
    scenario_result = run_logistics_procurement_scenario(request, snapshot)
    rule_result = evaluate_procurement_rules(as_of=request.meta.as_of, snapshot=snapshot)
    # 업무 위험 판정(비교식)은 Rule 소유 — Service 는 계산 결과를 조립만 한다.
    business = evaluate_procurement_business_signals(
        as_of=request.meta.as_of,
        snapshot=snapshot,
        scenario_results=scenario_result["scenario_results"],
    )

    response = LogisticsProcurementResponse(
        as_of=request.meta.as_of,
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        runtime_status=rule_result["runtime_status"],
        verdict=derive_logistics_verdict(rule_result),
        band=LogisticsBand(
            cap_by_date=(scenario_result["cap_by_date"] if rule_result["calculation_ready"] else {})
        ),
        # None이면 직렬화에서 키가 빠진다 — confirmed_outbound.item 누락 같은
        # Partial Output 상태를 `[]`(0건 확인)로 위장하지 않기 위해서다.
        inventory_by_item=scenario_result["inventory_by_item"],
        scenario_results=scenario_result["scenario_results"],
        inbound_constraints=InboundConstraints(
            inbound_lead_days=snapshot.inbound_lead_days if snapshot is not None else None,
            daily_inbound_capacity_kg=(
                snapshot.daily_inbound_capacity_kg if snapshot is not None else None
            ),
            inbound_transport_capacity_kg=(
                snapshot.inbound_transport_capacity_kg if snapshot is not None else None
            ),
        ),
        hard_constraints=rule_result["hard_constraints"],
        # 업무 위험 signal 은 soft_warnings 채널로 나간다 — LLM Context 는 의미
        # 기준(BUSINESS_SIGNALS)으로 이 중 signals 만 골라낸다.
        soft_warnings=merge_business_warnings(rule_result, business),
        missing_data=_missing_data(rule_result, business),
        preferred_adjustment=derive_preferred_adjustment(scenario_result["scenario_results"]),
        evidences=_evidences(snapshot),
    )
    return enrich_logistics_response(
        response, interpretation_service, measurements=business["measurements"]
    )


def run_logistics_sales(request: LogisticsSalesRequest) -> LogisticsSalesResponse:
    """H1 승인 매입을 미래 입고로 Overlay해 Logistics B Reply를 만든다."""
    snapshot = _get_snapshot_or_none(as_of=request.as_of)
    response = run_logistics_sales_with_snapshot(request, snapshot)
    save_logistics_agent_run(
        cycle="SALES",
        as_of=request.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        verdict=response.verdict,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def run_logistics_sales_with_snapshot(
    request: LogisticsSalesRequest,
    snapshot: InventoryLogisticsSnapshot | None,
    interpretation_service: InterpretationService | None = None,
) -> LogisticsSalesResponse:
    """외부에서 고정한 Inventory/Logistics Snapshot으로 B Cycle을 실행한다."""
    scenario_result = run_logistics_sales_scenario(request, snapshot)
    rule_result = evaluate_sales_rules(
        as_of=request.as_of,
        snapshot=snapshot,
        future_occupancy_by_date=scenario_result["future_occupancy_by_date"],
    )
    # 판매 신선도 위험도 비율 Rule 로 판정한다 — Lot status 의존 폐기 (결정서 §3).
    business = evaluate_sales_business_signals(snapshot=snapshot)
    response = LogisticsSalesResponse(
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        approval_id=request.approved_purchase.approval_id,
        runtime_status=rule_result["runtime_status"],
        verdict=derive_logistics_verdict(rule_result),
        daily_outbound_capacity_kg=scenario_result["daily_outbound_capacity_kg"],
        lot_constraints=scenario_result["lot_constraints"],
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=merge_business_warnings(rule_result, business),
        missing_data=_missing_data(rule_result, business),
        # 우선출고 조정은 Rule 이 정한 것이다 — 신선도 위험이 성립할 때만 preferred 로
        # 지정한다. 이게 없으면 preferred 강제 검증과 결합해 판매 추천이 영구 봉쇄된다.
        preferred_adjustment=(
            SALES_PRIORITY_ADJUSTMENT if FRESHNESS_QUALITY_RISK in business["signals"] else None
        ),
    )
    return enrich_logistics_response(
        response, interpretation_service, measurements=business["measurements"]
    )


def _missing_data(
    rule_result: LogisticsRuleResult,
    business: BusinessSignalResult,
) -> list[str]:
    """미확정 계열(비-PASS Constraint · 데이터 경고 · 판정 스킵)의 번역명 목록.

    업무 위험 signal 은 미확정이 아니므로 여기 들어가지 않는다.
    """
    codes = [
        constraint.code
        for constraint in rule_result["hard_constraints"]
        if constraint.status != "PASS"
    ]
    return translate_missing_data([*codes, *rule_result["soft_warnings"], *business["warnings"]])


def get_logistics_run(run_id: UUID) -> LogisticsAgentRunResponse:
    """UI 조회용 Logistics Agent 실행이력 한 건을 반환한다."""
    return LogisticsAgentRunResponse.model_validate(get_logistics_agent_run(run_id))


def list_logistics_runs(
    *,
    cycle: LogisticsCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: int = 100,
) -> list[LogisticsAgentRunResponse]:
    """UI 조회용 Logistics Agent 실행이력 목록을 반환한다."""
    rows = list_logistics_agent_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )
    return [LogisticsAgentRunResponse.model_validate(row) for row in rows]


def _evidences(snapshot: InventoryLogisticsSnapshot | None) -> list[LogisticsEvidence]:
    if snapshot is None:
        return []
    return [
        LogisticsEvidence(ref_id=ref_id, claim="Inventory/Logistics Snapshot source")
        for ref_id in snapshot.evidence_refs
    ]
