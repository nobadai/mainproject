"""재고·물류 Agent의 Runtime 및 Hard Constraint 규칙."""

from datetime import date
from decimal import Decimal
from typing import TypedDict

from app.logistics.schemas import (
    ConstraintResult,
    FinalVerdict,
    InventoryLogisticsSnapshot,
    RuntimeStatus,
)
from app.logistics.tools import is_inbound_schedule_complete


class LogisticsRuleResult(TypedDict):
    runtime_status: RuntimeStatus
    hard_constraints: list[ConstraintResult]
    soft_warnings: list[str]
    calculation_ready: bool


def derive_logistics_verdict(result: LogisticsRuleResult) -> FinalVerdict:
    """Runtime readiness와 개별 Hard Check 상태를 분리해 최종 판정을 집계한다."""
    if result["runtime_status"] != "READY":
        return "REVIEW_REQUIRED"
    statuses = {constraint.status for constraint in result["hard_constraints"]}
    if "FAIL" in statuses:
        return "FAIL"
    if "UNRESOLVED" in statuses:
        return "REVIEW_REQUIRED"
    return "PASS"


def evaluate_procurement_rules(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsRuleResult:
    """Logistics A의 cap_by_date 계산 가능 여부를 fail-closed로 판단한다."""
    boundary = _snapshot_boundary(as_of=as_of, snapshot=snapshot)
    if boundary is not None:
        return boundary
    assert snapshot is not None

    constraints = [
        _known_constraint("LOG-H01", snapshot.guaranteed_capacity_kg, "N2_UNRESOLVED"),
        _known_constraint(
            "LOG-H02",
            snapshot.guaranteed_capacity_by_zone_kg,
            "ZONE_CAPACITY_UNRESOLVED",
        ),
        _known_constraint(
            "LOG-H03",
            snapshot.daily_inbound_capacity_kg,
            "DAILY_INBOUND_CAPACITY_UNRESOLVED",
        ),
        _known_constraint(
            "LOG-H04",
            snapshot.inbound_transport_capacity_kg,
            "INBOUND_TRANSPORT_CAPACITY_UNRESOLVED",
        ),
        _known_constraint("LOG-H05", snapshot.inbound_lead_days, "N4_UNRESOLVED"),
    ]
    if not is_inbound_schedule_complete(snapshot):
        constraints.append(
            ConstraintResult(
                code="IN_TRANSIT_SCHEDULE_UNRESOLVED",
                status="UNRESOLVED",
                skip_reason=_inbound_schedule_skip_reason(snapshot),
            )
        )
    soft_warnings = _snapshot_warnings(snapshot)
    core_values = (
        snapshot.guaranteed_capacity_kg,
        snapshot.daily_inbound_capacity_kg,
        snapshot.inbound_transport_capacity_kg,
        snapshot.inbound_lead_days,
        snapshot.confirmed_inbound_schedule,
        snapshot.confirmed_outbound_schedule,
    )
    calculation_ready = all(value is not None for value in core_values) and (
        is_inbound_schedule_complete(snapshot)
    )
    return {
        "runtime_status": "READY" if calculation_ready else "RUNTIME_NOT_READY",
        "hard_constraints": constraints,
        "soft_warnings": soft_warnings,
        "calculation_ready": calculation_ready,
    }


def evaluate_sales_rules(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
    future_occupancy_by_date: dict[date, Decimal] | None,
) -> LogisticsRuleResult:
    """Logistics B의 outbound 및 H1 미래 점유 계산 가능 여부를 판단한다."""
    boundary = _snapshot_boundary(as_of=as_of, snapshot=snapshot)
    if boundary is not None:
        return boundary
    assert snapshot is not None

    warehouse_constraint = _known_constraint(
        "LOG-H01", snapshot.guaranteed_capacity_kg, "N2_UNRESOLVED"
    )
    if snapshot.guaranteed_capacity_kg is not None and future_occupancy_by_date is not None:
        warehouse_constraint = ConstraintResult(
            code="LOG-H01",
            status="PASS"
            if all(
                value <= snapshot.guaranteed_capacity_kg
                for value in future_occupancy_by_date.values()
            )
            else "FAIL",
        )
    outbound_constraint = _known_constraint(
        "N17",
        snapshot.shared_daily_outbound_capacity_kg,
        "N17_UNRESOLVED",
    )
    lots_complete = all(lot.remaining_freshness_days is not None for lot in snapshot.on_hand_by_lot)
    lot_constraint = ConstraintResult(
        code="N17-LOT",
        status="PASS" if lots_complete else "UNRESOLVED",
        skip_reason=None if lots_complete else "N17_LOT_FRESHNESS_UNRESOLVED",
    )
    inbound_completeness_constraint = None
    if not is_inbound_schedule_complete(snapshot):
        inbound_completeness_constraint = ConstraintResult(
            code="IN_TRANSIT_SCHEDULE_UNRESOLVED",
            status="UNRESOLVED",
            skip_reason=_inbound_schedule_skip_reason(snapshot),
        )
    soft_warnings = _snapshot_warnings(snapshot)
    if future_occupancy_by_date is None:
        soft_warnings.append("H1_FUTURE_OCCUPANCY_UNRESOLVED")
    calculation_ready = (
        snapshot.shared_daily_outbound_capacity_kg is not None
        and snapshot.guaranteed_capacity_kg is not None
        and future_occupancy_by_date is not None
        and lots_complete
        and is_inbound_schedule_complete(snapshot)
    )
    constraints = [warehouse_constraint, outbound_constraint, lot_constraint]
    if inbound_completeness_constraint is not None:
        constraints.append(inbound_completeness_constraint)
    return {
        "runtime_status": "READY" if calculation_ready else "RUNTIME_NOT_READY",
        "hard_constraints": constraints,
        "soft_warnings": soft_warnings,
        "calculation_ready": calculation_ready,
    }


def _snapshot_boundary(
    *,
    as_of: date,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsRuleResult | None:
    if snapshot is None:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "hard_constraints": [
                ConstraintResult(
                    code="REQUIRED_LOGISTICS_SNAPSHOT_MISSING",
                    status="FAIL",
                )
            ],
            "soft_warnings": [],
            "calculation_ready": False,
        }
    if as_of != snapshot.as_of:
        return {
            "runtime_status": "RUNTIME_NOT_READY",
            "hard_constraints": [ConstraintResult(code="AS_OF_MISMATCH", status="FAIL")],
            "soft_warnings": _snapshot_warnings(snapshot),
            "calculation_ready": False,
        }
    return None


def _known_constraint(
    code: str,
    value: object | None,
    skip_reason: str,
) -> ConstraintResult:
    return ConstraintResult(
        code=code,
        status="PASS" if value is not None else "UNRESOLVED",
        skip_reason=None if value is not None else skip_reason,
    )


def _inbound_schedule_skip_reason(snapshot: InventoryLogisticsSnapshot) -> str:
    if snapshot.in_transit is None:
        return "IN_TRANSIT_UNRESOLVED"
    if snapshot.confirmed_inbound_schedule is None:
        return "CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED"
    return "IN_TRANSIT_SCHEDULE_DEDUPLICATION_UNRESOLVED"


def _snapshot_warnings(snapshot: InventoryLogisticsSnapshot) -> list[str]:
    warnings: list[str] = []
    if snapshot.snapshot_id is None:
        warnings.append("SNAPSHOT_ID_UNRESOLVED")
    if any("provisional=true" in ref for ref in snapshot.evidence_refs):
        warnings.append("PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT")
    if snapshot.in_transit is None:
        warnings.append("IN_TRANSIT_UNRESOLVED")
    if snapshot.confirmed_inbound_schedule is None:
        warnings.append("CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED")
    if snapshot.confirmed_outbound_schedule is None:
        warnings.append("CONFIRMED_OUTBOUND_SCHEDULE_UNRESOLVED")
    return warnings
