from datetime import date
from decimal import Decimal

import pytest

from app.logistics.rules import (
    derive_logistics_verdict,
    evaluate_procurement_rules,
    evaluate_sales_rules,
)
from app.logistics.schemas import (
    InTransitItem,
    InventoryLotSnapshot,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScheduledQuantity,
)
from app.logistics.tools import (
    build_inventory_by_item,
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_expected_arrival_dates,
    calculate_future_occupancy_by_date,
    find_in_transit_schedule_gap,
    has_unattributed_confirmed_outbound,
    is_inbound_schedule_complete,
    overlay_approved_purchase,
)

AS_OF = date(2026, 8, 21)
ARRIVAL = date(2026, 8, 23)


def _matched_in_transit_snapshot(complete_logistics_snapshot, **transit_overrides):
    transit = {
        "inbound_id": "INB-001",
        "item": "배추",
        "quantity_kg": Decimal(500),
        "expected_arrival_date": date(2026, 8, 30),
    }
    transit.update(transit_overrides)
    confirmed = ScheduledQuantity(
        inbound_id="INB-001",
        item="배추",
        quantity_kg=Decimal(500),
        date=date(2026, 8, 30),
    )
    return complete_logistics_snapshot.model_copy(
        update={
            "in_transit": [InTransitItem(**transit)],
            "confirmed_inbound_schedule": [confirmed],
        }
    )


def test_expected_arrival_date_uses_canonical_kg_contract(logistics_purchase_payload):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    assert request.scenarios[0].total_qty_kg == 4500
    assert calculate_expected_arrival_dates(request, 2) == [ARRIVAL]


def test_cap_by_date_uses_guaranteed_capacity_only(complete_logistics_snapshot):
    """1차 Hard는 guaranteed 8,000 하나다 — daily(3,000)/transport(2,500)가 깎으면 실패."""
    result = calculate_cap_by_date(complete_logistics_snapshot, [ARRIVAL])

    assert result == {ARRIVAL: Decimal(7000)}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("guaranteed_capacity_kg", Decimal(1500), Decimal(500)),
        ("daily_inbound_capacity_kg", Decimal(1200), Decimal(7000)),
        ("inbound_transport_capacity_kg", Decimal(900), Decimal(7000)),
        ("burst_capacity_kg", Decimal(9600), Decimal(7000)),
    ],
)
def test_only_guaranteed_capacity_moves_the_cap(
    complete_logistics_snapshot, field, value, expected
):
    snapshot = complete_logistics_snapshot.model_copy(update={field: value})

    assert calculate_cap_by_date(snapshot, [ARRIVAL]) == {ARRIVAL: expected}


def test_cap_follows_confirmed_only_projection(complete_logistics_snapshot):
    """TC-06: guaranteed 8000 / used 6000 / daily 1000 / transport 1000 → cap 2000."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "used_capacity_kg": Decimal(6000),
            "daily_inbound_capacity_kg": Decimal(1000),
            "inbound_transport_capacity_kg": Decimal(1000),
        }
    )

    assert calculate_cap_by_date(snapshot, [ARRIVAL]) == {ARRIVAL: Decimal(2000)}


def test_burst_capacity_is_not_a_hard_limit(complete_logistics_snapshot):
    """TC-07: burst 9,600이 있어도 8,000 초과를 조용히 허용하면 안 된다."""
    cap = calculate_cap_by_date(complete_logistics_snapshot, [ARRIVAL])[ARRIVAL]

    assert cap == complete_logistics_snapshot.guaranteed_capacity_kg - Decimal(1000)
    assert cap != complete_logistics_snapshot.burst_capacity_kg - Decimal(1000)


def test_same_day_outbound_releases_space_from_next_day(complete_logistics_snapshot):
    """TC-08: D일 출고는 D일 입고 공간을 열어주지 않는다 — D+1부터 해제."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "confirmed_outbound_schedule": [
                ScheduledQuantity(date=ARRIVAL, quantity_kg=Decimal(1000), item="배추")
            ]
        }
    )

    result = calculate_cap_by_date(snapshot, [ARRIVAL, date(2026, 8, 24)])

    assert result[ARRIVAL] == Decimal(7000)
    assert result[date(2026, 8, 24)] == Decimal(8000)


def test_confirmed_inbound_occupies_from_arrival_day(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "confirmed_inbound_schedule": [
                ScheduledQuantity(date=ARRIVAL, quantity_kg=Decimal(2000), item="배추")
            ]
        }
    )

    result = calculate_cap_by_date(snapshot, [date(2026, 8, 22), ARRIVAL])

    assert result[date(2026, 8, 22)] == Decimal(7000)
    assert result[ARRIVAL] == Decimal(5000)


def test_unresolved_capacity_is_not_zero_or_unlimited(unresolved_logistics_snapshot):
    snapshot = unresolved_logistics_snapshot.model_copy(
        update={
            "in_transit": [],
            "confirmed_inbound_schedule": [],
            "confirmed_outbound_schedule": [],
        }
    )
    with pytest.raises(ValueError, match="LOGISTICS_CAPACITY_INPUT_MISSING"):
        calculate_cap_by_date(snapshot, [ARRIVAL])


# ---------------------------------------------------------------------------
# B-1 — in_transit 3상태와 inbound_id 정합성
# ---------------------------------------------------------------------------


def test_in_transit_none_blocks_procurement_and_sales(complete_logistics_snapshot):
    """None(미확인)은 [](0건 확인)이 아니다 — RUNTIME_NOT_READY."""
    snapshot = complete_logistics_snapshot.model_copy(update={"in_transit": None})

    assert find_in_transit_schedule_gap(snapshot) == "IN_TRANSIT_UNRESOLVED"
    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    sales = evaluate_sales_rules(
        as_of=AS_OF,
        snapshot=snapshot,
        future_occupancy_by_date={ARRIVAL: Decimal(5500)},
    )

    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"
    assert sales["runtime_status"] == "RUNTIME_NOT_READY"
    for result in (procurement, sales):
        constraint = next(
            item
            for item in result["hard_constraints"]
            if item.code == "IN_TRANSIT_SCHEDULE_UNRESOLVED"
        )
        assert constraint.skip_reason == "IN_TRANSIT_UNRESOLVED"


def test_empty_in_transit_is_known_zero(complete_logistics_snapshot):
    assert is_inbound_schedule_complete(complete_logistics_snapshot) is True

    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=complete_logistics_snapshot)
    inbound = overlay_approved_purchase(
        complete_logistics_snapshot,
        LogisticsSalesRequest.model_validate(
            {
                "cycle": "SALES",
                "as_of": "2026-08-21",
                "approved_purchase": {
                    "approval_id": "H1-KNOWN-ZERO",
                    "total_qty_kg": 500,
                    "expected_arrival_date": "2026-08-23",
                    "arrival_schedule": [{"date": "2026-08-23", "quantity_kg": 500}],
                },
            }
        ).approved_purchase,
    )

    assert procurement["runtime_status"] == "READY"
    assert inbound is not None
    assert sum((item.quantity_kg for item in inbound), start=Decimal(0)) == Decimal(500)


def test_matched_inbound_id_is_ready_without_double_counting(complete_logistics_snapshot):
    """TC-09: 같은 입고가 양쪽에 보여도 confirmed schedule만 한 번 반영한다."""
    snapshot = _matched_in_transit_snapshot(complete_logistics_snapshot)

    assert find_in_transit_schedule_gap(snapshot) is None
    assert is_inbound_schedule_complete(snapshot) is True
    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    assert procurement["runtime_status"] == "READY"

    cap = calculate_cap_by_date(snapshot, [date(2026, 8, 30)])
    # used 1000 + confirmed 500 → 6500. in_transit을 중복 가산하면 6000이 된다.
    assert cap[date(2026, 8, 30)] == Decimal(6500)


def test_in_transit_without_inbound_id_fails_closed(
    complete_logistics_snapshot, logistics_sales_payload
):
    snapshot = _matched_in_transit_snapshot(complete_logistics_snapshot, inbound_id=None)
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    assert find_in_transit_schedule_gap(snapshot) == "IN_TRANSIT_INBOUND_ID_MISSING"
    assert is_inbound_schedule_complete(snapshot) is False
    assert overlay_approved_purchase(snapshot, request.approved_purchase) is None
    assert calculate_future_occupancy_by_date(snapshot, []) is None
    with pytest.raises(ValueError, match="IN_TRANSIT_SCHEDULE_UNRESOLVED"):
        calculate_cap_by_date(snapshot, [ARRIVAL])

    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"
    constraint = next(
        item
        for item in procurement["hard_constraints"]
        if item.code == "IN_TRANSIT_SCHEDULE_UNRESOLVED"
    )
    assert constraint.skip_reason == "IN_TRANSIT_INBOUND_ID_MISSING"


def test_in_transit_id_missing_from_confirmed_schedule_fails_closed(
    complete_logistics_snapshot,
):
    snapshot = _matched_in_transit_snapshot(complete_logistics_snapshot, inbound_id="INB-002")

    assert find_in_transit_schedule_gap(snapshot) == "IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE"
    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"


@pytest.mark.parametrize(
    "mismatch",
    [
        {"item": "무"},
        {"quantity_kg": Decimal(400)},
        {"expected_arrival_date": date(2026, 8, 31)},
    ],
)
def test_same_inbound_id_field_mismatch_fails_closed(complete_logistics_snapshot, mismatch):
    snapshot = _matched_in_transit_snapshot(complete_logistics_snapshot, **mismatch)

    assert find_in_transit_schedule_gap(snapshot) == "IN_TRANSIT_CONFIRMED_SCHEDULE_MISMATCH"
    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"


def test_confirmed_inbound_none_fails_closed(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(update={"confirmed_inbound_schedule": None})

    assert find_in_transit_schedule_gap(snapshot) == "CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED"
    procurement = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    assert procurement["runtime_status"] == "RUNTIME_NOT_READY"


# ---------------------------------------------------------------------------
# inventory_by_item — 물리 점유와 가용재고 분리
# ---------------------------------------------------------------------------


def _lot(lot_id: str, item: str, qty, freshness: int | None, status: str) -> InventoryLotSnapshot:
    return InventoryLotSnapshot(
        lot_id=lot_id,
        item=item,
        available_qty_kg=Decimal(qty),
        remaining_freshness_days=freshness,
        status=status,
    )


def _split_lots():
    """TC-01: 정상 가용 600 / 검수·격리·만료 400 → used 1000, 가용 600."""
    return [
        _lot("LOT-OK", "배추", 600, 5, "ACTIVE"),
        _lot("LOT-HOLD", "배추", 300, 5, "QUARANTINED"),
        _lot("LOT-EXPIRED", "배추", 100, 0, "ACTIVE"),
    ]


def test_physical_occupancy_and_available_inventory_are_separate(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={"on_hand_by_lot": _split_lots(), "used_capacity_kg": Decimal(1000)}
    )

    inventory = build_inventory_by_item(snapshot)

    assert inventory is not None
    assert [(row.item, row.available_qty_kg) for row in inventory] == [("배추", Decimal(600))]
    assert snapshot.used_capacity_kg == Decimal(1000)
    total_physical = sum(
        (lot.available_qty_kg for lot in snapshot.on_hand_by_lot), start=Decimal(0)
    )
    assert total_physical == Decimal(1000)


def test_expired_lot_leaves_available_but_keeps_occupying_space(complete_logistics_snapshot):
    """TC-02: freshness=0은 가용 제외, 물리 점유 유지."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot("LOT-EXPIRED", "배추", 400, 0, "ACTIVE")],
            "used_capacity_kg": Decimal(400),
        }
    )

    inventory = build_inventory_by_item(snapshot)

    assert inventory == []
    assert snapshot.used_capacity_kg == Decimal(400)
    assert calculate_cap_by_date(snapshot, [ARRIVAL]) == {ARRIVAL: Decimal(7600)}


def test_confirmed_outbound_reservation_reduces_available_inventory(
    complete_logistics_snapshot,
):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "confirmed_outbound_schedule": [
                ScheduledQuantity(date=date(2026, 8, 22), quantity_kg=Decimal(200), item="배추")
            ]
        }
    )

    inventory = build_inventory_by_item(snapshot)

    assert inventory is not None
    assert [(row.item, row.available_qty_kg) for row in inventory] == [("배추", Decimal(800))]


def test_empty_confirmed_outbound_is_normal(complete_logistics_snapshot):
    """TC-04: confirmed_outbound = []는 0건 확인 — 정상 READY."""
    inventory = build_inventory_by_item(complete_logistics_snapshot)

    assert inventory is not None
    assert has_unattributed_confirmed_outbound(complete_logistics_snapshot) is False
    result = evaluate_procurement_rules(as_of=AS_OF, snapshot=complete_logistics_snapshot)
    assert result["runtime_status"] == "READY"


def test_outbound_row_without_item_omits_inventory_but_keeps_pre_ready(
    complete_logistics_snapshot,
):
    """TC-05: 품목 임의 추정 금지 — inventory_by_item만 생략, PRE는 READY 유지."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "confirmed_outbound_schedule": [
                ScheduledQuantity(date=date(2026, 8, 22), quantity_kg=Decimal(200), item=None)
            ]
        }
    )

    assert has_unattributed_confirmed_outbound(snapshot) is True
    assert build_inventory_by_item(snapshot) is None
    assert build_inventory_by_item(snapshot) != []

    result = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)
    assert result["runtime_status"] == "READY"
    assert result["calculation_ready"] is True
    constraint = next(
        item
        for item in result["hard_constraints"]
        if item.code == "CONFIRMED_OUTBOUND_ITEM_UNRESOLVED"
    )
    assert constraint.status == "UNRESOLVED"
    assert constraint.skip_reason == "CONFIRMED_OUTBOUND_ITEM_UNRESOLVED"
    # 수량 기준 총량 Capacity는 item 없이도 정확히 계산 가능하다 —
    # 8/22 출고 200은 D+1(8/23)부터 해제되어 cap = 8000 - (1000 - 200) = 7200.
    assert calculate_cap_by_date(snapshot, [ARRIVAL]) == {ARRIVAL: Decimal(7200)}


def test_item_without_ml_forecast_stays_in_inventory(complete_logistics_snapshot):
    """TC-16: 피마늘은 ML Forecast가 없어도 재고 응답에서 제외하지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                *complete_logistics_snapshot.on_hand_by_lot,
                _lot("LOT-PIMANUL", "피마늘", "8.88", 20, "ACTIVE"),
            ]
        }
    )

    inventory = build_inventory_by_item(snapshot)

    assert inventory is not None
    assert ("피마늘", Decimal("8.88")) in [(row.item, row.available_qty_kg) for row in inventory]
    assert any(lot.item == "피마늘" for lot in build_lot_constraints(snapshot))


# ---------------------------------------------------------------------------
# Runtime 규칙
# ---------------------------------------------------------------------------


def test_procurement_rule_keeps_unresolved_constraints_null(unresolved_logistics_snapshot):
    result = evaluate_procurement_rules(as_of=AS_OF, snapshot=unresolved_logistics_snapshot)

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["calculation_ready"] is False
    assert derive_logistics_verdict(result) is None
    assert all(item.status == "UNRESOLVED" for item in result["hard_constraints"])
    assert "PROVISIONAL_CAPACITY_EXCLUDED_FROM_HARD_LIMIT" in result["soft_warnings"]


def test_procurement_rule_can_be_ready_without_zone_capacity(complete_logistics_snapshot):
    result = evaluate_procurement_rules(as_of=AS_OF, snapshot=complete_logistics_snapshot)

    assert result["runtime_status"] == "READY"
    zone = next(item for item in result["hard_constraints"] if item.code == "LOG-H02")
    assert zone.status == "UNRESOLVED"
    assert derive_logistics_verdict(result) == "REVIEW_REQUIRED"


def test_daily_inbound_and_transport_do_not_gate_runtime(complete_logistics_snapshot):
    """1차 Hard 미사용 값은 없어도 Runtime을 막지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "daily_inbound_capacity_kg": None,
            "inbound_transport_capacity_kg": None,
        }
    )

    result = evaluate_procurement_rules(as_of=AS_OF, snapshot=snapshot)

    assert result["runtime_status"] == "READY"
    assert result["calculation_ready"] is True
    assert calculate_cap_by_date(snapshot, [ARRIVAL]) == {ARRIVAL: Decimal(7000)}


def test_h1_overlay_stays_future_and_does_not_change_on_hand(
    complete_logistics_snapshot, logistics_sales_payload
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    original_lots = build_lot_constraints(complete_logistics_snapshot)

    inbound = overlay_approved_purchase(complete_logistics_snapshot, request.approved_purchase)
    assert inbound is not None
    occupancy = calculate_future_occupancy_by_date(complete_logistics_snapshot, inbound)

    assert occupancy == {ARRIVAL: Decimal(5500)}
    assert build_lot_constraints(complete_logistics_snapshot) == original_lots
    assert len(original_lots) == 1


def test_sales_rule_marks_warehouse_over_capacity(complete_logistics_snapshot):
    result = evaluate_sales_rules(
        as_of=AS_OF,
        snapshot=complete_logistics_snapshot,
        future_occupancy_by_date={ARRIVAL: Decimal(9000)},
    )

    warehouse = next(item for item in result["hard_constraints"] if item.code == "LOG-H01")
    assert warehouse.status == "FAIL"
    assert result["runtime_status"] == "READY"
    assert derive_logistics_verdict(result) == "FAIL"


def test_sales_rule_all_pass_aggregates_to_pass(complete_logistics_snapshot):
    result = evaluate_sales_rules(
        as_of=AS_OF,
        snapshot=complete_logistics_snapshot,
        future_occupancy_by_date={ARRIVAL: Decimal(5500)},
    )

    assert {item.status for item in result["hard_constraints"]} == {"PASS"}
    assert derive_logistics_verdict(result) == "PASS"


def test_sales_rule_requires_n17(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={"shared_daily_outbound_capacity_kg": None}
    )
    result = evaluate_sales_rules(
        as_of=AS_OF,
        snapshot=snapshot,
        future_occupancy_by_date={ARRIVAL: Decimal(5500)},
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    n17 = next(item for item in result["hard_constraints"] if item.code == "N17")
    assert n17.status == "UNRESOLVED"


def test_logistics_rules_fail_closed_on_as_of_mismatch(complete_logistics_snapshot):
    result = evaluate_procurement_rules(
        as_of=date(2026, 8, 22),
        snapshot=complete_logistics_snapshot,
    )

    assert result["runtime_status"] == "RUNTIME_NOT_READY"
    assert result["hard_constraints"][0].code == "AS_OF_MISMATCH"
    assert derive_logistics_verdict(result) is None
