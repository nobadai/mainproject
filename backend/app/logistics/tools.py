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


#: 품목을 식별할 수 없는 물리 점유·입고·출고를 담는 버킷 키.
_UNATTRIBUTED: str | None = None


def _replay_aggregate_occupancy(
    *,
    start_occupancy: Decimal,
    inbound: list[ScheduledQuantity],
    outbound: list[ScheduledQuantity],
    target_date: date,
) -> Decimal:
    """품목 축 없이 총 kg만 날짜순으로 재생한다 (H1 미래 점유 전용).

    출고가 열어주는 공간은 **그 시점에 실제 창고에 있는 물량까지**다. 확정 출고가
    보유량보다 많은 것은 계산이 무너져야 하는 오류가 아니라 추가 매입이 필요할 수
    있는 정상 업무 상태이고, 모자란 물량은 음수 점유량이 아니라 별개의 업무 사실이다
    (상세설계 §4 물리 점유량 정의).

    H1 승인 매입 Schedule에는 품목 축이 없어(`ArrivalScheduleItem`) 품목별 재생을
    할 수 없으므로 이 경로만 총량 계산을 유지한다. PRE의 `cap_by_date`는
    `_replay_occupancy_by_item()`을 쓴다.

    날짜 규칙은 기존 정책 그대로다 — 입고는 `<= target_date`로 당일부터 점유하고,
    출고는 `< target_date`로 당일 공간을 열지 않고 D+1부터 해제한다 (상세설계 §9).
    """
    inbound_by_date: dict[date, Decimal] = {}
    for row in inbound:
        if row.date <= target_date:
            inbound_by_date[row.date] = inbound_by_date.get(row.date, Decimal(0)) + row.quantity_kg
    outbound_by_date: dict[date, Decimal] = {}
    for row in outbound:
        if row.date < target_date:
            released = outbound_by_date.get(row.date, Decimal(0))
            outbound_by_date[row.date] = released + row.quantity_kg

    occupancy = start_occupancy
    for day in sorted({*inbound_by_date, *outbound_by_date}):
        occupancy += inbound_by_date.get(day, Decimal(0))
        # 같은 날 입고분까지 포함한 실재 물량이 그날 출고가 해제할 수 있는 상한이다.
        # 못 내보낸 물량을 이후 입고에 떠넘기지 않는다 — 애초에 없던 재고다.
        occupancy -= min(outbound_by_date.get(day, Decimal(0)), occupancy)
    return occupancy


def _initial_occupancy_by_item(
    snapshot: InventoryLogisticsSnapshot,
) -> dict[str | None, Decimal]:
    """현재 물리 점유를 품목별로 나눈다. 총량 정본은 `used_capacity_kg`다.

    Lot으로 식별되는 만큼만 품목에 귀속시키고, `used_capacity_kg`에 못 미치는
    차이는 품목 미귀속 점유로 남긴다 — 특정 품목의 출고가 그 몫을 대신 소진했다고
    보지 않는다. `sum(on_hand_by_lot)`으로 총량 정본을 대체하지 않는다.
    """
    buckets: dict[str | None, Decimal] = {}
    for lot in snapshot.on_hand_by_lot:
        buckets[lot.item] = buckets.get(lot.item, Decimal(0)) + lot.available_qty_kg
    identified = sum(buckets.values(), start=Decimal(0))
    buckets[_UNATTRIBUTED] = max(Decimal(0), snapshot.used_capacity_kg - identified)
    return buckets


def _replay_occupancy_by_item(
    snapshot: InventoryLogisticsSnapshot,
    target_date: date,
) -> Decimal:
    """품목별 물리 점유를 날짜순으로 재생해 target_date 시점의 창고 점유량을 만든다.

    품목이 명확한 확정 출고는 **그 품목 재고에서만** 공간을 연다 — 다른 품목이나
    미귀속 점유를 대신 소진했다고 계산하지 않는다. 창고 Capacity는 총 kg만 맞으면
    되는 숫자가 아니기 때문이다.

    품목을 알 수 없는 출고(`item=None`)만 기존처럼 남은 전체 물량 범위에서 총량으로
    차감한다. 임의 품목 배분은 하지 않는다 — Partial Output 정책상 이 행이 있어도
    총량 Capacity는 계속 제공해야 한다.

    Lot 단위 배정은 하지 않는다 (FIFO/FEFO 없음). 날짜 규칙은 기존 정책 그대로다.
    """
    assert snapshot.confirmed_inbound_schedule is not None
    assert snapshot.confirmed_outbound_schedule is not None

    inbound_on: dict[date, list[ScheduledQuantity]] = {}
    for row in snapshot.confirmed_inbound_schedule:
        if row.date <= target_date:
            inbound_on.setdefault(row.date, []).append(row)
    outbound_on: dict[date, list[ScheduledQuantity]] = {}
    for row in snapshot.confirmed_outbound_schedule:
        if row.date < target_date:
            outbound_on.setdefault(row.date, []).append(row)

    buckets = _initial_occupancy_by_item(snapshot)
    #: 품목 불명 출고가 총량에서 걷어낸 누계. 어느 품목에도 귀속시키지 않는다.
    unattributed_release = Decimal(0)
    for day in sorted({*inbound_on, *outbound_on}):
        for row in inbound_on.get(day, []):
            buckets[row.item] = buckets.get(row.item, Decimal(0)) + row.quantity_kg
        # 품목 지정 출고를 먼저 처리한다 — 자기 품목 재고까지만 열 수 있다.
        for row in outbound_on.get(day, []):
            if row.item is None:
                continue
            held = buckets.get(row.item, Decimal(0))
            buckets[row.item] = held - min(row.quantity_kg, held)
        for row in outbound_on.get(day, []):
            if row.item is not None:
                continue
            gross = sum(buckets.values(), start=Decimal(0))
            remaining = max(Decimal(0), gross - unattributed_release)
            unattributed_release += min(row.quantity_kg, remaining)

    gross = sum(buckets.values(), start=Decimal(0))
    return gross - min(unattributed_release, gross)


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

    result: dict[date, Decimal] = {}
    for arrival_date in arrival_dates:
        projected_occupancy = _replay_occupancy_by_item(snapshot, arrival_date)
        # 품목별 재생이 각 버킷을 0 아래로 내리지 않으므로 정상 입력에서는 걸리지
        # 않는다. 불변식이 깨진 Snapshot을 잡는 최후 방어로 남긴다.
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
        # 출고는 D+1부터 해제하고, 해제량은 그 시점에 실제 존재하는 물량을 넘지
        # 않는다. 품목 축은 쓰지 않는다 — 승인 매입 Schedule에 item이 없어서다.
        value = _replay_aggregate_occupancy(
            start_occupancy=snapshot.used_capacity_kg,
            inbound=inbound_schedule,
            outbound=snapshot.confirmed_outbound_schedule,
            target_date=target_date,
        )
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
            # 등급은 Snapshot이 이미 정규화해 둔 값을 그대로 나른다 — 여기서
            # 다시 계산하거나 None을 임의 등급으로 채우지 않는다.
            grade=lot.grade,
            status=lot.status,
        )
        for lot in snapshot.on_hand_by_lot
    ]
