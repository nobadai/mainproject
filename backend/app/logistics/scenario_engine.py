"""고정 Inventory/Logistics Snapshot에서 A/B 계산과 Rule 호출을 조립한다."""

from datetime import date
from decimal import Decimal
from typing import TypedDict

from app.logistics.rules import (
    LogisticsRuleResult,
    evaluate_procurement_rules,
    evaluate_sales_rules,
)
from app.logistics.schemas import (
    InventoryLogisticsSnapshot,
    LogisticsSalesRequest,
    LotConstraint,
    PurchaseAgentOutput,
    ScheduledQuantity,
)
from app.logistics.tools import (
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_expected_arrival_dates,
    calculate_future_occupancy_by_date,
    overlay_approved_purchase,
)


class LogisticsProcurementScenarioResult(TypedDict):
    expected_arrival_dates: list[date]
    cap_by_date: dict[date, Decimal]
    rule_result: LogisticsRuleResult


class LogisticsSalesScenarioResult(TypedDict):
    inbound_schedule: list[ScheduledQuantity] | None
    future_occupancy_by_date: dict[date, Decimal] | None
    daily_outbound_capacity_kg: Decimal | None
    lot_constraints: list[LotConstraint]
    rule_result: LogisticsRuleResult


def run_logistics_procurement_scenario(
    request: PurchaseAgentOutput,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsProcurementScenarioResult:
    """Logistics A 계산을 기존 Tool로 수행하고 Runtime Rule을 호출한다."""
    rule_result = evaluate_procurement_rules(as_of=request.meta.as_of, snapshot=snapshot)
    expected_arrival_dates: list[date] = []
    cap_by_date: dict[date, Decimal] = {}
    if rule_result["calculation_ready"]:
        assert snapshot is not None
        assert snapshot.inbound_lead_days is not None
        expected_arrival_dates = calculate_expected_arrival_dates(
            request,
            snapshot.inbound_lead_days,
        )
        cap_by_date = calculate_cap_by_date(snapshot, expected_arrival_dates)
    return {
        "expected_arrival_dates": expected_arrival_dates,
        "cap_by_date": cap_by_date,
        "rule_result": rule_result,
    }


def run_logistics_sales_scenario(
    request: LogisticsSalesRequest,
    snapshot: InventoryLogisticsSnapshot | None,
) -> LogisticsSalesScenarioResult:
    """H1 미래 입고와 lot/outbound 계산 후 Logistics B Rule을 호출한다."""
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
    rule_result = evaluate_sales_rules(
        as_of=request.as_of,
        snapshot=snapshot,
        future_occupancy_by_date=future_occupancy,
    )
    return {
        "inbound_schedule": inbound_schedule,
        "future_occupancy_by_date": future_occupancy,
        "daily_outbound_capacity_kg": daily_outbound_capacity,
        "lot_constraints": lot_constraints,
        "rule_result": rule_result,
    }
