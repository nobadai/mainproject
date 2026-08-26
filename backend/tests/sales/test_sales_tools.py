"""영업 Agent 순수 계산 함수 단위 테스트.

수치 예시는 영업 에이전트 IO 명세 v0.8의 배추 예시를 따른다.
"""

from datetime import date
from decimal import Decimal

from app.sales.schemas import SalesSnapshotA, SalesSnapshotB
from app.sales.tools import (
    build_floor_vector,
    resolve_today_floor,
    strategic_inventory_by_date,
)


def _floor_input(**overrides) -> SalesSnapshotA:
    payload = {
        "snapshot_id": "T0-20260821-01",
        "as_of": "2026-08-21",
        "item": "배추",
        "policy_version": None,
        "confirmed_orders": [
            {"order_id": "ORD-20260823-001", "delivery_date": "2026-08-23", "qty_kg": 200},
            {"order_id": "ORD-20260825-002", "delivery_date": "2026-08-25", "qty_kg": 1500},
        ],
        "inventory": {
            "on_hand": [
                {"lot_id": "LOT-20260819-001", "qty_kg": 600, "freshness_days_left": 8},
                {"lot_id": "LOT-20260813-004", "qty_kg": 100, "freshness_days_left": 2},
            ],
            "in_transit": [],
        },
    }
    payload.update(overrides)
    return SalesSnapshotA.model_validate(payload)


def test_build_floor_vector_matches_spec_example():
    floor_vector = build_floor_vector(_floor_input())
    # 08-23: 확정 200 < 가용 700 → 하한 0
    assert floor_vector[date(2026, 8, 23)] == Decimal(0)
    # 08-25: 누적 확정 1700 − 가용 600(임박 로트 100kg 만료) = 1100
    assert floor_vector[date(2026, 8, 25)] == Decimal(1100)


def test_build_floor_vector_no_confirmed_orders_is_empty():
    floor_vector = build_floor_vector(_floor_input(confirmed_orders=[]))
    assert floor_vector == {}


def test_build_floor_vector_does_not_go_negative():
    # 가용 재고가 확정 주문보다 많으면 하한은 음수가 아니라 0
    floor_vector = build_floor_vector(
        _floor_input(
            confirmed_orders=[
                {"order_id": "ORD-1", "delivery_date": "2026-08-23", "qty_kg": 50},
            ]
        )
    )
    assert floor_vector[date(2026, 8, 23)] == Decimal(0)


def test_resolve_today_floor_none_when_lead_days_unresolved():
    floor_vector = build_floor_vector(_floor_input())
    assert resolve_today_floor(floor_vector, None, date(2026, 8, 21)) is None


def test_resolve_today_floor_within_lead_window():
    floor_vector = build_floor_vector(_floor_input())
    # 리드타임 2일 → 08-23까지만 구속. 그 하한은 0
    assert resolve_today_floor(floor_vector, 2, date(2026, 8, 21)) == Decimal(0)
    # 리드타임 4일 → 08-25까지 구속. 하한 1100
    assert resolve_today_floor(floor_vector, 4, date(2026, 8, 21)) == Decimal(1100)


def _allocation_input(**overrides) -> SalesSnapshotB:
    payload = {
        "snapshot_id": "T0-20260821-01",
        "as_of": "2026-08-21",
        "item": "배추",
        "policy_version": None,
        "cost_basis": None,
        "confirmed_orders": [],
        "sales_opportunities": None,
        "inventory": {
            "on_hand": [
                {
                    "lot_id": "LOT-20260819-001",
                    "qty_kg": 600,
                    "freshness_days_left": 8,
                    "reserved_for_confirmed_kg": 200,
                },
                {
                    "lot_id": "LOT-20260813-004",
                    "qty_kg": 100,
                    "freshness_days_left": 2,
                    "reserved_for_confirmed_kg": 0,
                },
            ],
            "in_transit": [],
        },
    }
    payload.update(overrides)
    return SalesSnapshotB.model_validate(payload)


def test_strategic_inventory_today_excludes_reserved():
    result = strategic_inventory_by_date(_allocation_input())
    # 보유 700 − 예약 200 = 500
    assert result[date(2026, 8, 21)] == Decimal(500)


def test_strategic_inventory_adds_in_transit_on_arrival_and_drops_expired_lot():
    result = strategic_inventory_by_date(
        _allocation_input(
            inventory={
                "on_hand": [
                    {
                        "lot_id": "LOT-A",
                        "qty_kg": 600,
                        "freshness_days_left": 8,
                        "reserved_for_confirmed_kg": 200,
                    },
                    {
                        "lot_id": "LOT-B",
                        "qty_kg": 100,
                        "freshness_days_left": 2,
                        "reserved_for_confirmed_kg": 0,
                    },
                ],
                "in_transit": [
                    {"lot_id": "LOT-C", "qty_kg": 300, "expected_arrival_date": "2026-08-24"},
                ],
            }
        )
    )
    # 오늘: 400 + 100 = 500 (in_transit 미반영)
    assert result[date(2026, 8, 21)] == Decimal(500)
    # 08-24: 임박 로트 B(08-23 만료) 제외 → 400, in_transit 300 도착 → 700
    assert result[date(2026, 8, 24)] == Decimal(700)
