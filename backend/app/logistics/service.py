"""재고·물류 Agent A/B 결정론적 실행 흐름."""

from datetime import date
from uuid import UUID

from app.logistics.interpretation import enrich_logistics_response
from app.logistics.llm.runtime import InterpretationService
from app.logistics.repository import get_current_inventory_logistics_snapshot
from app.logistics.rules import evaluate_procurement_rules, evaluate_sales_rules
from app.logistics.run_repository import (
    get_logistics_agent_run,
    list_logistics_agent_runs,
    save_logistics_agent_run,
)
from app.logistics.scenario_engine import (
    run_logistics_procurement_scenario,
    run_logistics_sales_scenario,
)
from app.logistics.schemas import (
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


def _get_snapshot_or_none() -> InventoryLogisticsSnapshot | None:
    try:
        return get_current_inventory_logistics_snapshot()
    except LookupError:
        return None


def run_logistics_procurement(request: PurchaseAgentOutput) -> LogisticsProcurementResponse:
    """현재 Snapshot과 Purchase 날짜 축으로 Logistics A Band를 만든다."""
    snapshot = _get_snapshot_or_none()
    response = run_logistics_procurement_with_snapshot(request, snapshot)
    save_logistics_agent_run(
        cycle="PROCUREMENT",
        as_of=request.meta.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
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

    response = LogisticsProcurementResponse(
        as_of=request.meta.as_of,
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        runtime_status=rule_result["runtime_status"],
        band=LogisticsBand(
            cap_by_date=(scenario_result["cap_by_date"] if rule_result["calculation_ready"] else {})
        ),
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
        soft_warnings=rule_result["soft_warnings"],
        evidences=_evidences(snapshot),
    )
    return enrich_logistics_response(response, interpretation_service)


def run_logistics_sales(request: LogisticsSalesRequest) -> LogisticsSalesResponse:
    """H1 승인 매입을 미래 입고로 Overlay해 Logistics B Reply를 만든다."""
    snapshot = _get_snapshot_or_none()
    response = run_logistics_sales_with_snapshot(request, snapshot)
    save_logistics_agent_run(
        cycle="SALES",
        as_of=request.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
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
    response = LogisticsSalesResponse(
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        approval_id=request.approved_purchase.approval_id,
        runtime_status=rule_result["runtime_status"],
        daily_outbound_capacity_kg=scenario_result["daily_outbound_capacity_kg"],
        lot_constraints=scenario_result["lot_constraints"],
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=rule_result["soft_warnings"],
    )
    return enrich_logistics_response(response, interpretation_service)


def get_logistics_run(run_id: UUID) -> LogisticsAgentRunResponse:
    """UI 조회용 Logistics Agent 실행이력 한 건을 반환한다."""
    return LogisticsAgentRunResponse.model_validate(get_logistics_agent_run(run_id))


def list_logistics_runs(
    *,
    cycle: LogisticsCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: int = 100,
) -> list[LogisticsAgentRunResponse]:
    """UI 조회용 Logistics Agent 실행이력 목록을 반환한다."""
    rows = list_logistics_agent_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
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
