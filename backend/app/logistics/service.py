"""재고·물류 Agent A/B 결정론적 실행 흐름."""

from datetime import date
from uuid import UUID

from app.logistics.repository import get_current_inventory_logistics_snapshot
from app.logistics.rules import evaluate_procurement_rules, evaluate_sales_rules
from app.logistics.run_repository import (
    get_logistics_agent_run,
    list_logistics_agent_runs,
    save_logistics_agent_run,
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
from app.logistics.tools import (
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_expected_arrival_dates,
    calculate_future_occupancy_by_date,
    overlay_approved_purchase,
)


def _get_snapshot_or_none() -> InventoryLogisticsSnapshot | None:
    try:
        return get_current_inventory_logistics_snapshot()
    except LookupError:
        return None


def run_logistics_procurement(request: PurchaseAgentOutput) -> LogisticsProcurementResponse:
    """현재 Snapshot과 Purchase 날짜 축으로 Logistics A Band를 만든다."""
    snapshot = _get_snapshot_or_none()
    rule_result = evaluate_procurement_rules(as_of=request.meta.as_of, snapshot=snapshot)
    cap_by_date = {}
    if rule_result["calculation_ready"]:
        assert snapshot is not None
        assert snapshot.inbound_lead_days is not None
        arrival_dates = calculate_expected_arrival_dates(request, snapshot.inbound_lead_days)
        cap_by_date = calculate_cap_by_date(snapshot, arrival_dates)

    response = LogisticsProcurementResponse(
        as_of=request.meta.as_of,
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        runtime_status=rule_result["runtime_status"],
        band=LogisticsBand(cap_by_date=cap_by_date),
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
    save_logistics_agent_run(
        cycle="PROCUREMENT",
        as_of=request.meta.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


def run_logistics_sales(request: LogisticsSalesRequest) -> LogisticsSalesResponse:
    """H1 승인 매입을 미래 입고로 Overlay해 Logistics B Reply를 만든다."""
    snapshot = _get_snapshot_or_none()
    future_occupancy = None
    lot_constraints = []
    if snapshot is not None:
        lot_constraints = build_lot_constraints(snapshot)
        inbound_schedule = overlay_approved_purchase(snapshot, request.approved_purchase)
        if inbound_schedule is not None:
            future_occupancy = calculate_future_occupancy_by_date(snapshot, inbound_schedule)
    rule_result = evaluate_sales_rules(
        as_of=request.as_of,
        snapshot=snapshot,
        future_occupancy_by_date=future_occupancy,
    )
    response = LogisticsSalesResponse(
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        approval_id=request.approved_purchase.approval_id,
        runtime_status=rule_result["runtime_status"],
        daily_outbound_capacity_kg=(
            snapshot.shared_daily_outbound_capacity_kg if snapshot is not None else None
        ),
        lot_constraints=lot_constraints,
        hard_constraints=rule_result["hard_constraints"],
        soft_warnings=rule_result["soft_warnings"],
    )
    save_logistics_agent_run(
        cycle="SALES",
        as_of=request.as_of,
        snapshot_id=response.snapshot_id,
        runtime_status=response.runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return response


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
