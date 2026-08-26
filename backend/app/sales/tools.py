"""영업 Agent의 결정론적 계산 도구.

이번 범위의 순수 계산 세 함수만 둔다. 확정주문·재고·입고예정만 쓰는 산술이며
정책값에 의존하지 않는다. 값이 없는 경우 0으로 대체하지 않는다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.sales.schemas import OnHandLot, SalesSnapshotA, SalesSnapshotB


def _usable_on_hand_kg(on_hand: list[OnHandLot], as_of: date, target: date) -> Decimal:
    """target 날짜에 아직 신선한 on_hand 로트 수량 합.

    로트는 as_of + freshness_days_left 까지 쓸 수 있다. 그 뒤 날짜에는 못 쓴다.
    """
    return sum(
        (
            lot.qty_kg
            for lot in on_hand
            if as_of + timedelta(days=lot.freshness_days_left) >= target
        ),
        start=Decimal(0),
    )


def build_floor_vector(snapshot: SalesSnapshotA) -> dict[date, Decimal]:
    """납기 날짜별 매입 부족 하한을 계산한다.

        floor[d] = max(0, d까지 확정주문 − d에 가용한 on_hand − d까지 도착한 in_transit)

    확정주문·on_hand·in_transit 만으로 계산한다. 매입 후보는 읽지 않는다.
    확정주문이 없으면 빈 벡터를 반환한다.
    """
    as_of = snapshot.as_of
    on_hand = snapshot.inventory.on_hand
    in_transit = snapshot.inventory.in_transit
    due_dates = sorted({order.delivery_date for order in snapshot.confirmed_orders})

    floor_vector: dict[date, Decimal] = {}
    for due_date in due_dates:
        cumulative_confirmed = sum(
            (o.qty_kg for o in snapshot.confirmed_orders if o.delivery_date <= due_date),
            start=Decimal(0),
        )
        usable_on_hand = _usable_on_hand_kg(on_hand, as_of, due_date)
        arrived_in_transit = sum(
            (lot.qty_kg for lot in in_transit if lot.expected_arrival_date <= due_date),
            start=Decimal(0),
        )
        shortfall = cumulative_confirmed - usable_on_hand - arrived_in_transit
        floor_vector[due_date] = shortfall if shortfall > 0 else Decimal(0)
    return floor_vector


def resolve_today_floor(
    floor_vector: dict[date, Decimal],
    inbound_lead_days: int | None,
    as_of: date,
) -> Decimal | None:
    """입고 리드타임 안에 있는 납기의 구속 하한을 반환한다.

    오늘 사지 않으면 못 지키는 납기(as_of + inbound_lead_days 이내)의 하한 중
    가장 큰 값이 오늘의 구속 하한이다.
    inbound_lead_days(N4)가 None이면 계산할 수 없어 None을 반환한다.
    None을 0으로 대체하지 않는다 — 미결과 '하한 0'은 다른 상태다.
    """
    if inbound_lead_days is None:
        return None
    window_end = as_of + timedelta(days=inbound_lead_days)
    in_window = [kg for due_date, kg in floor_vector.items() if due_date <= window_end]
    if not in_window:
        return Decimal(0)
    return max(in_window)


def strategic_inventory_by_date(snapshot: SalesSnapshotB) -> dict[date, Decimal]:
    """날짜별 전략 판매 가능 재고를 계산한다.

    오늘 배분 원금 = on_hand − 확정 주문 예약분(reserved_for_confirmed_kg).
    in_transit 은 도착일 이후 날짜의 전망에만 더하고, 오늘 판매 후보 물량으로는
    잡지 않는다. 신선도가 지난 로트는 해당 날짜의 가용에서 빠진다.
    """
    as_of = snapshot.as_of
    on_hand = snapshot.inventory.on_hand
    in_transit = snapshot.inventory.in_transit

    projection_dates = sorted(
        {as_of} | {lot.expected_arrival_date for lot in in_transit}
    )

    result: dict[date, Decimal] = {}
    for target in projection_dates:
        strategic_on_hand = sum(
            (
                max(Decimal(0), lot.qty_kg - lot.reserved_for_confirmed_kg)
                for lot in on_hand
                if as_of + timedelta(days=lot.freshness_days_left) >= target
            ),
            start=Decimal(0),
        )
        arrived_in_transit = sum(
            (lot.qty_kg for lot in in_transit if lot.expected_arrival_date <= target),
            start=Decimal(0),
        )
        result[target] = strategic_on_hand + arrived_in_transit
    return result
