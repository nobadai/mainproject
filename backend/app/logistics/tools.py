"""재고·물류 Agent의 결정론적 계산 도구."""

from datetime import date, timedelta
from decimal import Decimal

from app.logistics.schemas import (
    InventoryLogisticsSnapshot,
    LogisticsApprovedPurchaseCommitment,
    LotConstraint,
    PurchaseAgentOutput,
    ScheduledQuantity,
)

KG_PER_TON = Decimal(1000)


def ton_to_kg(quantity_ton: Decimal) -> Decimal:
    """톤 단위 수량을 Decimal kg로 변환한다."""
    return quantity_ton * KG_PER_TON


def calculate_expected_arrival_dates(
    purchase: PurchaseAgentOutput,
    inbound_lead_days: int,
) -> list[date]:
    """각 매입일에 입고 Lead Time을 더한 고유 도착일을 반환한다."""
    return sorted(
        {
            item.date + timedelta(days=inbound_lead_days)
            for scenario in purchase.scenarios
            for item in scenario.split_plan
        }
    )


def calculate_cap_by_date(
    snapshot: InventoryLogisticsSnapshot,
    arrival_dates: list[date],
) -> dict[date, Decimal]:
    """확정된 창고, 일일 입고, 운송 Capacity로 날짜별 신규 입고 Band를 계산한다."""
    required_values = (
        snapshot.guaranteed_capacity_kg,
        snapshot.daily_inbound_capacity_kg,
        snapshot.inbound_transport_capacity_kg,
        snapshot.confirmed_inbound_schedule,
        snapshot.confirmed_outbound_schedule,
    )
    if any(value is None for value in required_values):
        raise ValueError("LOGISTICS_CAPACITY_INPUT_MISSING")
    assert snapshot.guaranteed_capacity_kg is not None
    assert snapshot.daily_inbound_capacity_kg is not None
    assert snapshot.inbound_transport_capacity_kg is not None
    assert snapshot.confirmed_inbound_schedule is not None
    assert snapshot.confirmed_outbound_schedule is not None

    result: dict[date, Decimal] = {}
    for arrival_date in arrival_dates:
        confirmed_inbound = sum(
            (
                item.quantity_kg
                for item in snapshot.confirmed_inbound_schedule
                if item.date <= arrival_date
            ),
            start=Decimal(0),
        )
        confirmed_outbound = sum(
            (
                item.quantity_kg
                for item in snapshot.confirmed_outbound_schedule
                if item.date <= arrival_date
            ),
            start=Decimal(0),
        )
        projected_occupancy = snapshot.used_capacity_kg + confirmed_inbound - confirmed_outbound
        if projected_occupancy < Decimal(0):
            raise ValueError("NEGATIVE_PROJECTED_OCCUPANCY")
        free_capacity = snapshot.guaranteed_capacity_kg - projected_occupancy
        result[arrival_date] = max(
            Decimal(0),
            min(
                free_capacity,
                snapshot.daily_inbound_capacity_kg,
                snapshot.inbound_transport_capacity_kg,
            ),
        )
    return result


def overlay_approved_purchase(
    snapshot: InventoryLogisticsSnapshot,
    approved_purchase: LogisticsApprovedPurchaseCommitment,
) -> list[ScheduledQuantity] | None:
    """H1 승인 매입을 on_hand가 아닌 미래 입고 Schedule에 Overlay한다."""
    if snapshot.confirmed_inbound_schedule is None:
        return None
    approved_schedule = [
        ScheduledQuantity(date=item.date, quantity_kg=item.quantity_kg)
        for item in approved_purchase.arrival_schedule
    ]
    return [*snapshot.confirmed_inbound_schedule, *approved_schedule]


def calculate_future_occupancy_by_date(
    snapshot: InventoryLogisticsSnapshot,
    inbound_schedule: list[ScheduledQuantity],
) -> dict[date, Decimal] | None:
    """H1 미래 입고와 확정 출고를 반영한 날짜별 창고 점유량을 계산한다."""
    if snapshot.confirmed_outbound_schedule is None:
        return None
    dates = sorted({item.date for item in inbound_schedule})
    occupancy: dict[date, Decimal] = {}
    for target_date in dates:
        inbound = sum(
            (item.quantity_kg for item in inbound_schedule if item.date <= target_date),
            start=Decimal(0),
        )
        outbound = sum(
            (
                item.quantity_kg
                for item in snapshot.confirmed_outbound_schedule
                if item.date <= target_date
            ),
            start=Decimal(0),
        )
        value = snapshot.used_capacity_kg + inbound - outbound
        if value < Decimal(0):
            raise ValueError("NEGATIVE_PROJECTED_OCCUPANCY")
        occupancy[target_date] = value
    return occupancy


def build_lot_constraints(snapshot: InventoryLogisticsSnapshot) -> list[LotConstraint]:
    """현재 on_hand Lot만 S3 대조용 최소 Constraint로 변환한다."""
    return [
        LotConstraint(
            lot_id=lot.lot_id,
            item=lot.item,
            available_qty_kg=lot.available_qty_kg,
            remaining_freshness_days=lot.remaining_freshness_days,
            status=lot.status,
        )
        for lot in snapshot.on_hand_by_lot
    ]
