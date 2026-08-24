"""재고·물류 Agent A/B 결정론적 실행 흐름."""

from app.logistics.repository import get_current_inventory_logistics_snapshot
from app.logistics.rules import evaluate_procurement_rules, evaluate_sales_rules
from app.logistics.schemas import (
    InboundConstraints,
    InventoryLogisticsSnapshot,
    LogisticsBand,
    LogisticsEvidence,
    LogisticsProcurementResponse,
    LogisticsSalesRequest,
    LogisticsSalesResponse,
    PurchaseAgentOutput,
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

    return LogisticsProcurementResponse(
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
    return LogisticsSalesResponse(
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


def _evidences(snapshot: InventoryLogisticsSnapshot | None) -> list[LogisticsEvidence]:
    if snapshot is None:
        return []
    return [
        LogisticsEvidence(ref_id=ref_id, claim="Inventory/Logistics Snapshot source")
        for ref_id in snapshot.evidence_refs
    ]
