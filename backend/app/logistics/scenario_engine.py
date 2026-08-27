"""고정 Inventory/Logistics Snapshot에서 A/B 계산과 Rule 호출을 조립한다."""

from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict

from app.logistics.schemas import (
    InventoryByItem,
    InventoryLogisticsSnapshot,
    LogisticsReasonCode,
    LogisticsSalesRequest,
    LotConstraint,
    PurchaseAgentOutput,
    ScenarioAdjustment,
    ScenarioValidationResult,
    ScheduledQuantity,
)
from app.logistics.tools import (
    CAP_BY_DATE_WINDOW_DAYS,
    build_inventory_by_item,
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_expected_arrival_dates,
    calculate_future_occupancy_by_date,
    overlay_approved_purchase,
)


class LogisticsProcurementScenarioResult(TypedDict):
    expected_arrival_dates: list[date]
    cap_by_date: dict[date, Decimal]
    inventory_by_item: list[InventoryByItem] | None
    scenario_results: list[ScenarioValidationResult]


class LogisticsSalesScenarioResult(TypedDict):
    inbound_schedule: list[ScheduledQuantity] | None
    future_occupancy_by_date: dict[date, Decimal] | None
    daily_outbound_capacity_kg: Decimal | None
    lot_constraints: list[LotConstraint]


def run_logistics_procurement_scenario(
    request: PurchaseAgentOutput,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsProcurementScenarioResult:
    """Logistics A의 도착일, 입고 Capacity, 가용재고, Scenario 판정을 계산한다."""
    expected_arrival_dates: list[date] = []
    cap_by_date: dict[date, Decimal] = {}
    inventory_by_item: list[InventoryByItem] | None = None
    if snapshot is not None:
        inventory_by_item = build_inventory_by_item(snapshot)
        if snapshot.inbound_lead_days is not None:
            expected_arrival_dates = calculate_expected_arrival_dates(
                request,
                snapshot.inbound_lead_days,
            )
            try:
                cap_by_date = calculate_cap_by_date(snapshot, expected_arrival_dates)
            except ValueError as error:
                if str(error) not in {
                    "IN_TRANSIT_SCHEDULE_UNRESOLVED",
                    "LOGISTICS_CAPACITY_INPUT_MISSING",
                }:
                    raise
    return {
        "expected_arrival_dates": expected_arrival_dates,
        "cap_by_date": cap_by_date,
        "inventory_by_item": inventory_by_item,
        "scenario_results": validate_purchase_scenarios(request, snapshot),
    }


def validate_purchase_scenarios(
    request: PurchaseAgentOutput,
    snapshot: InventoryLogisticsSnapshot | None,
) -> list[ScenarioValidationResult]:
    """각 Scenario의 각 split 요청 수량을 도착일 Capacity와 실제 비교해 판정한다.

    판정: 그대로 가능 → ok / quantity·timing 조정으로 가능 → conditional /
    18일 Window 안에서도 불가 → reject / 필수 물류 Fact 확인 불가 → skipped.
    조정 축은 quantity·timing만 허용한다. Timing 제안은 도착일 기준이다 —
    매입 실행일 역산은 Purchase 책임이다.
    """
    labels = [scenario.label for scenario in request.scenarios]
    if snapshot is None or snapshot.inbound_lead_days is None:
        return [_skipped(label) for label in labels]

    lead = snapshot.inbound_lead_days
    window_start = request.meta.as_of + timedelta(days=lead)
    window_dates = [
        window_start + timedelta(days=offset) for offset in range(CAP_BY_DATE_WINDOW_DAYS)
    ]
    arrival_dates = calculate_expected_arrival_dates(request, lead)
    try:
        caps = calculate_cap_by_date(snapshot, sorted({*window_dates, *arrival_dates}))
    except ValueError:
        # IN_TRANSIT_SCHEDULE_UNRESOLVED · LOGISTICS_CAPACITY_INPUT_MISSING ·
        # NEGATIVE_PROJECTED_OCCUPANCY — 필수 물류 Fact가 확인되지 않은 상태다.
        return [_skipped(label) for label in labels]

    results: list[ScenarioValidationResult] = []
    for scenario in request.scenarios:
        reason_codes: list[LogisticsReasonCode] = []
        adjustments: list[ScenarioAdjustment] = []
        infeasible = False
        for split in scenario.split_plan:
            arrival = split.date + timedelta(days=lead)
            requested = Decimal(split.qty_kg)
            cap = caps[arrival]
            if requested <= cap:
                continue
            _append_unique(reason_codes, "CAPACITY_EXCEEDED")
            if cap > Decimal(0):
                adjustments.append(
                    ScenarioAdjustment(
                        axis="quantity",
                        split_date=split.date,
                        suggested_qty_kg=cap,
                    )
                )
                continue
            feasible_arrival = next((day for day in window_dates if requested <= caps[day]), None)
            if feasible_arrival is not None:
                adjustments.append(
                    ScenarioAdjustment(
                        axis="timing",
                        split_date=split.date,
                        suggested_arrival_date=feasible_arrival,
                    )
                )
            else:
                infeasible = True
                _append_unique(reason_codes, "NO_FEASIBLE_ARRIVAL_DATE")
        if infeasible:
            verdict = "reject"
        elif adjustments:
            verdict = "conditional"
        else:
            verdict = "ok"
        results.append(
            ScenarioValidationResult(
                label=scenario.label,
                verdict=verdict,
                reason_codes=reason_codes,
                adjustments=adjustments,
            )
        )
    return results


def _skipped(label: str) -> ScenarioValidationResult:
    # 데이터 누락은 Business Reason이 아니라 Runtime 상태로 표현한다 —
    # reason_codes를 비워 두고 원인은 ConstraintResult가 담당한다.
    return ScenarioValidationResult(label=label, verdict="skipped", reason_codes=[], adjustments=[])


def _append_unique(codes: list[LogisticsReasonCode], code: LogisticsReasonCode) -> None:
    if code not in codes:
        codes.append(code)


def run_logistics_sales_scenario(
    request: LogisticsSalesRequest,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsSalesScenarioResult:
    """H1 미래 입고와 lot/outbound 중간 결과를 계산한다."""
    inbound_schedule = None
    future_occupancy = None
    daily_outbound_capacity = None
    lot_constraints: list[LotConstraint] = []
    if snapshot is not None:
        daily_outbound_capacity = snapshot.shared_daily_outbound_capacity_kg
        lot_constraints = build_lot_constraints(snapshot)
        inbound_schedule = overlay_approved_purchase(snapshot, request.approved_purchase)
        if inbound_schedule is not None:
            future_occupancy = calculate_future_occupancy_by_date(snapshot, inbound_schedule)
    return {
        "inbound_schedule": inbound_schedule,
        "future_occupancy_by_date": future_occupancy,
        "daily_outbound_capacity_kg": daily_outbound_capacity,
        "lot_constraints": lot_constraints,
    }
