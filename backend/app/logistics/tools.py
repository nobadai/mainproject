"""재고·물류 Agent의 결정론적 계산 도구."""

from datetime import date, timedelta
from decimal import Decimal

from app.logistics.schemas import (
    InventoryByItem,
    InventoryLogisticsSnapshot,
    LogisticsApprovedPurchaseCommitment,
    LotConstraint,
    PurchaseAgentOutput,
    ScheduledQuantity,
)

#: cap_by_date 조회 창 길이 (`as_of + inbound_lead_days`부터, Policy 확정값 18).
#: Window 밖은 0이 아니라 미조회 영역이다.
CAP_BY_DATE_WINDOW_DAYS = 18

#: 가용재고로 인정하는 Lot 상태. DB에 없는 상태를 새로 만들지 않는다 —
#: ACTIVE가 아닌 상태(검수/격리/사용불가 등)는 물리 점유만 하고 가용에서 빠진다.
_AVAILABLE_LOT_STATUS = "ACTIVE"


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
    """guaranteed capacity 하나를 1차 Hard Constraint로 날짜별 입고 Band를 계산한다.

    burst/daily inbound/transport/shared outbound capacity는 1차 Hard 판정에
    개입하지 않는다 (Policy 결정값 §3).
    """
    if not is_inbound_schedule_complete(snapshot):
        raise ValueError("IN_TRANSIT_SCHEDULE_UNRESOLVED")
    if snapshot.guaranteed_capacity_kg is None or snapshot.confirmed_outbound_schedule is None:
        raise ValueError("LOGISTICS_CAPACITY_INPUT_MISSING")
    assert snapshot.confirmed_inbound_schedule is not None

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
        # 출고는 `<` — 같은 날 출고는 입출고 순서를 알 수 없으므로 당일 입고 공간을
        # 열어주지 않고 D+1부터 해제한다 (상세설계 §9).
        confirmed_outbound_released = sum(
            (
                item.quantity_kg
                for item in snapshot.confirmed_outbound_schedule
                if item.date < arrival_date
            ),
            start=Decimal(0),
        )
        projected_occupancy = (
            snapshot.used_capacity_kg + confirmed_inbound - confirmed_outbound_released
        )
        if projected_occupancy < Decimal(0):
            raise ValueError("NEGATIVE_PROJECTED_OCCUPANCY")
        result[arrival_date] = max(
            Decimal(0), snapshot.guaranteed_capacity_kg - projected_occupancy
        )
    return result


def overlay_approved_purchase(
    snapshot: InventoryLogisticsSnapshot,
    approved_purchase: LogisticsApprovedPurchaseCommitment,
) -> list[ScheduledQuantity] | None:
    """H1 승인 매입을 on_hand가 아닌 미래 입고 Schedule에 Overlay한다."""
    if not is_inbound_schedule_complete(snapshot):
        return None
    assert snapshot.confirmed_inbound_schedule is not None
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
    if not is_inbound_schedule_complete(snapshot) or snapshot.confirmed_outbound_schedule is None:
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


def find_in_transit_schedule_gap(snapshot: InventoryLogisticsSnapshot) -> str | None:
    """B-1: confirmed_inbound_schedule 완전성을 검증하고 실패 원인 코드를 돌려준다.

    in_transit 3상태를 명시적으로 구분한다 — None(미확인)과 [](0건 확인)은 다르다.
    행이 존재하면 inbound_id로 confirmed schedule 포함 여부와 item/quantity/도착일
    일치를 검증한다. 성공해도 Capacity에는 confirmed_inbound_schedule만 반영한다.
    """
    if snapshot.in_transit is None:
        return "IN_TRANSIT_UNRESOLVED"
    if snapshot.confirmed_inbound_schedule is None:
        return "CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED"
    if snapshot.in_transit == []:
        return None

    confirmed_by_id: dict[str, ScheduledQuantity] = {}
    for row in snapshot.confirmed_inbound_schedule:
        if row.inbound_id is not None:
            confirmed_by_id[row.inbound_id] = row
    for transit in snapshot.in_transit:
        if transit.inbound_id is None:
            return "IN_TRANSIT_INBOUND_ID_MISSING"
        confirmed = confirmed_by_id.get(transit.inbound_id)
        if confirmed is None:
            return "IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE"
        if (
            confirmed.item != transit.item
            or confirmed.quantity_kg != transit.quantity_kg
            or confirmed.date != transit.expected_arrival_date
        ):
            return "IN_TRANSIT_CONFIRMED_SCHEDULE_MISMATCH"
    return None


def is_inbound_schedule_complete(snapshot: InventoryLogisticsSnapshot) -> bool:
    """중복 가산 없이 미래 입고를 계산할 수 있는 상태인지 확인한다."""
    return find_in_transit_schedule_gap(snapshot) is None


def has_unattributed_confirmed_outbound(snapshot: InventoryLogisticsSnapshot) -> bool:
    """품목 식별이 없는 확정 출고 행이 있는지 — Partial Output 판별용.

    이 경우 품목을 임의 추정하지 않고 inventory_by_item만 생략한다(PRE는 READY 유지).
    """
    return snapshot.confirmed_outbound_schedule is not None and any(
        row.item is None for row in snapshot.confirmed_outbound_schedule
    )


def build_inventory_by_item(
    snapshot: InventoryLogisticsSnapshot,
) -> list[InventoryByItem] | None:
    """가용재고 정의를 적용한 품목별 자유재고를 집계한다.

    가용 제외: 비-ACTIVE 상태(검수/격리/사용불가), 신선도 만료(<= 0), 확정 출고 예약분.
    예상 판매·계획 출고는 차감하지 않는다. ML Forecast 유무는 재고 사실과 무관하다
    (피마늘 유지). 확정 출고 행에 item이 없으면 임의 배분하지 않고 None을 돌려준다 —
    호출부는 필드를 생략해야 하며 `[]`(0건 확인)로 대체하면 안 된다.
    """
    if snapshot.confirmed_outbound_schedule is None or has_unattributed_confirmed_outbound(
        snapshot
    ):
        return None

    totals: dict[str, Decimal] = {}
    for lot in snapshot.on_hand_by_lot:
        if lot.status != _AVAILABLE_LOT_STATUS:
            continue
        # 신선도 만료 확인(<= 0)만 제외한다. None은 만료가 확인된 상태가 아니므로
        # 가용에서 숨기지 않는다 (0 != null).
        if lot.remaining_freshness_days is not None and lot.remaining_freshness_days <= 0:
            continue
        totals[lot.item] = totals.get(lot.item, Decimal(0)) + lot.available_qty_kg
    for outbound in snapshot.confirmed_outbound_schedule:
        assert outbound.item is not None
        if outbound.item in totals:
            totals[outbound.item] = max(Decimal(0), totals[outbound.item] - outbound.quantity_kg)
    return [
        InventoryByItem(item=item, available_qty_kg=quantity)
        for item, quantity in sorted(totals.items())
    ]


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
