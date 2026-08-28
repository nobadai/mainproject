from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.logistics.scenario_engine import (
    run_logistics_procurement_scenario,
    run_logistics_sales_scenario,
    validate_purchase_scenarios,
)
from app.logistics.schemas import (
    InTransitItem,
    InventoryLotSnapshot,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScheduledQuantity,
)
from app.logistics.service import (
    run_logistics_procurement_with_snapshot,
    run_logistics_sales_with_snapshot,
)
from app.logistics.tools import calculate_cap_by_date

ARRIVAL = date(2026, 8, 23)


def _baechu_lot(qty_kg: int) -> InventoryLotSnapshot:
    """확정 출고는 자기 품목 재고에서만 공간을 열므로 Lot과 출고 품목을 맞춘다."""
    return InventoryLotSnapshot(
        lot_id="LOT-BAECHU",
        item="배추",
        available_qty_kg=Decimal(qty_kg),
        remaining_freshness_days=8,
        status="ACTIVE",
    )


def _request(logistics_purchase_payload, qty_kg: int) -> PurchaseAgentOutput:
    # 사중 일치(총량=split=sourcing, 금액=Σ qty×단가)를 지키며 요청 수량만 바꾼다.
    scenario = logistics_purchase_payload["scenarios"][0]
    scenario["total_qty_kg"] = qty_kg
    scenario["split_plan"][0]["qty_kg"] = qty_kg
    scenario["sourcing_plan"] = [
        {"market": "가락", "grade": "상", "qty_kg": qty_kg, "grade_unit_price": 1650}
    ]
    scenario["total_amount_krw"] = qty_kg * 1650
    return PurchaseAgentOutput.model_validate(logistics_purchase_payload)


def test_logistics_procurement_scenario_is_deterministic(
    complete_logistics_snapshot,
    logistics_purchase_payload,
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    first = run_logistics_procurement_scenario(request, complete_logistics_snapshot)
    second = run_logistics_procurement_scenario(request, complete_logistics_snapshot)

    assert first == second
    assert first["expected_arrival_dates"] == [ARRIVAL]
    assert first["cap_by_date"] == {ARRIVAL: Decimal(7000)}
    assert first["inventory_by_item"] is not None
    assert [(row.item, row.available_qty_kg) for row in first["inventory_by_item"]] == [
        ("배추", Decimal(1000))
    ]
    assert [result.verdict for result in first["scenario_results"]] == ["ok"]


def test_scenario_within_capacity_is_ok(complete_logistics_snapshot, logistics_purchase_payload):
    """TC-12: cap 2000 / request 1500 → ok."""
    snapshot = complete_logistics_snapshot.model_copy(update={"used_capacity_kg": Decimal(6000)})
    request = _request(logistics_purchase_payload, 1500)

    results = validate_purchase_scenarios(request, snapshot)

    assert len(results) == 1
    assert results[0].verdict == "ok"
    assert results[0].reason_codes == []
    assert results[0].adjustments == []


def test_scenario_over_capacity_suggests_quantity(
    complete_logistics_snapshot, logistics_purchase_payload
):
    """TC-13: cap 2000 / request 3000 → conditional + quantity."""
    snapshot = complete_logistics_snapshot.model_copy(update={"used_capacity_kg": Decimal(6000)})
    request = _request(logistics_purchase_payload, 3000)

    results = validate_purchase_scenarios(request, snapshot)

    assert results[0].verdict == "conditional"
    assert results[0].reason_codes == ["CAPACITY_EXCEEDED"]
    adjustment = results[0].adjustments[0]
    assert adjustment.axis == "quantity"
    assert adjustment.suggested_qty_kg == Decimal(2000)
    assert adjustment.suggested_arrival_date is None


def test_split_inbound_accumulates_within_scenario_only(
    complete_logistics_snapshot, logistics_purchase_payload
):
    """같은 Scenario의 앞선 split 입고량은 이후 split 가용 Capacity에서 누적 차감된다.

    used 6000 / guaranteed 8000 → base cap 2000.
    split1 1500(도착 8/23) 통과 후 8/24 가용은 500뿐이므로 split2 1500은 초과다 —
    두 split을 base cap과 독립 비교해 둘 다 통과시키면 안 된다.
    서로 다른 Scenario는 대안 관계라 누적하지 않는다.
    """
    snapshot = complete_logistics_snapshot.model_copy(update={"used_capacity_kg": Decimal(6000)})
    base = logistics_purchase_payload["scenarios"][0]
    split_scenario = {
        **base,
        "label": "기본",
        "total_qty_kg": 3000,
        "total_amount_krw": 3000 * 1650,
        "split_plan": [
            {"seq": 1, "date": "2026-08-21", "qty_kg": 1500},
            {"seq": 2, "date": "2026-08-22", "qty_kg": 1500},
        ],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 3000, "grade_unit_price": 1650}
        ],
    }
    sibling_scenario = {
        **base,
        "label": "보수",
        "total_qty_kg": 2000,
        "total_amount_krw": 2000 * 1650,
        "split_plan": [{"seq": 1, "date": "2026-08-21", "qty_kg": 2000}],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 2000, "grade_unit_price": 1650}
        ],
    }
    logistics_purchase_payload["scenarios"] = [split_scenario, sibling_scenario]
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    results = validate_purchase_scenarios(request, snapshot)

    split_result = next(result for result in results if result.label == "기본")
    assert split_result.verdict == "conditional"
    assert split_result.reason_codes == ["CAPACITY_EXCEEDED"]
    adjustment = split_result.adjustments[0]
    assert adjustment.axis == "quantity"
    assert adjustment.split_date == date(2026, 8, 22)
    # 8/24 가용 = base 2000 − split1 누적 1500 = 500. 독립 비교였다면 조정 없이 ok였다.
    assert adjustment.suggested_qty_kg == Decimal(500)

    # 다른 Scenario에는 앞 Scenario의 입고가 누적되지 않는다 — 2000 그대로 가능.
    sibling_result = next(result for result in results if result.label == "보수")
    assert sibling_result.verdict == "ok"
    assert sibling_result.adjustments == []


def test_split_occupancy_ignores_mid_window_confirmed_outbound(
    complete_logistics_snapshot, logistics_purchase_payload
):
    """중간 확정 출고가 앞선 split을 소진했을 수 있어도 split 누적은 그대로 유지된다.

    재고 100 / split1 50(도착 8/23) / 확정 출고 150(8/24) / split2(도착 8/25).
    물리적으로는 8/24 출고가 100 + 50을 전부 실어내 8/25 점유가 0이므로 8,000까지
    받을 수 있다. 엔진은 확정 Fact만으로 base cap을 만든 뒤 proposal 입고를 따로
    누적하므로 7,950으로 **보수적으로** 본다.

    미승인 proposal 물량이 확정 납품을 충족한다고 볼지는 Lot 배정 문제라 1차 범위
    밖이다. 과대평가가 아니라 과소평가 방향이어서 매입을 잘못 통과시키지 않는다.
    """
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_baechu_lot(100)],
            "used_capacity_kg": Decimal(100),
            "confirmed_outbound_schedule": [
                ScheduledQuantity(date=date(2026, 8, 24), quantity_kg=Decimal(150), item="배추")
            ],
        }
    )
    base = logistics_purchase_payload["scenarios"][0]
    logistics_purchase_payload["scenarios"] = [
        {
            **base,
            "label": "기본",
            "total_qty_kg": 8050,
            "total_amount_krw": 8050 * 1650,
            "split_plan": [
                {"seq": 1, "date": "2026-08-21", "qty_kg": 50},
                {"seq": 2, "date": "2026-08-23", "qty_kg": 8000},
            ],
            "sourcing_plan": [
                {"market": "가락", "grade": "상", "qty_kg": 8050, "grade_unit_price": 1650}
            ],
        }
    ]
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    # 8/24 출고 150은 실재 100만 해제하므로 8/25 base cap은 8,000이다.
    split2_arrival = date(2026, 8, 25)
    assert calculate_cap_by_date(snapshot, [split2_arrival]) == {split2_arrival: Decimal(8000)}

    results = validate_purchase_scenarios(request, snapshot)

    assert results[0].verdict == "conditional"
    adjustment = results[0].adjustments[0]
    assert adjustment.axis == "quantity"
    assert adjustment.split_date == date(2026, 8, 23)
    # base 8,000 − split1 누적 50. 물리 상태만 보면 8,000이 가능하다.
    assert adjustment.suggested_qty_kg == Decimal(7950)


def test_scenario_blocked_arrival_suggests_timing(
    complete_logistics_snapshot, logistics_purchase_payload
):
    """TC-14: D+2 불가 / D+3 가능 → conditional + suggested_arrival_date."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_baechu_lot(8000)],
            "used_capacity_kg": Decimal(8000),
            "confirmed_outbound_schedule": [
                ScheduledQuantity(date=ARRIVAL, quantity_kg=Decimal(3000), item="배추")
            ],
        }
    )
    request = _request(logistics_purchase_payload, 3000)

    results = validate_purchase_scenarios(request, snapshot)

    assert results[0].verdict == "conditional"
    assert results[0].reason_codes == ["CAPACITY_EXCEEDED"]
    adjustment = results[0].adjustments[0]
    assert adjustment.axis == "timing"
    assert adjustment.suggested_arrival_date == date(2026, 8, 24)
    assert adjustment.suggested_qty_kg is None


def test_scenario_infeasible_across_window_is_rejected(
    complete_logistics_snapshot, logistics_purchase_payload
):
    """TC-15: 18일 Window 전체 불가 → reject."""
    snapshot = complete_logistics_snapshot.model_copy(update={"used_capacity_kg": Decimal(8000)})
    request = _request(logistics_purchase_payload, 3000)

    results = validate_purchase_scenarios(request, snapshot)

    assert results[0].verdict == "reject"
    assert "NO_FEASIBLE_ARRIVAL_DATE" in results[0].reason_codes
    assert results[0].adjustments == []


def test_scenario_without_snapshot_is_skipped(logistics_purchase_payload):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    results = validate_purchase_scenarios(request, None)

    assert [result.verdict for result in results] == ["skipped"]
    assert results[0].reason_codes == []


def test_scenario_with_unresolved_schedule_is_skipped(
    complete_logistics_snapshot, logistics_purchase_payload
):
    snapshot = complete_logistics_snapshot.model_copy(update={"in_transit": None})
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    results = validate_purchase_scenarios(request, snapshot)

    assert [result.verdict for result in results] == ["skipped"]


def test_scenario_adjustments_never_touch_amount_or_channel(
    complete_logistics_snapshot, logistics_purchase_payload
):
    """TC-13(축 침범): 물류 Adjustment 축은 quantity/timing뿐이다."""
    snapshot = complete_logistics_snapshot.model_copy(update={"used_capacity_kg": Decimal(6000)})
    request = _request(logistics_purchase_payload, 3000)

    results = validate_purchase_scenarios(request, snapshot)

    axes = {adj.axis for result in results for adj in result.adjustments}
    assert axes <= {"quantity", "timing"}


def test_logistics_external_snapshot_bypasses_repository_and_round_trips_id(
    complete_logistics_snapshot,
    logistics_purchase_payload,
):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    with patch(
        "app.logistics.service.get_current_inventory_logistics_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_logistics_procurement_with_snapshot(
            request,
            complete_logistics_snapshot,
        )

    assert response.snapshot_id == "T0-20260821-001"
    assert response.runtime_status == "READY"
    assert response.inventory_by_item is not None
    assert [result.verdict for result in response.scenario_results] == ["ok"]


def test_logistics_sales_engine_keeps_h1_future_and_on_hand_unchanged(
    complete_logistics_snapshot,
    logistics_sales_payload,
):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)
    before = complete_logistics_snapshot.model_dump()

    first = run_logistics_sales_scenario(request, complete_logistics_snapshot)
    second = run_logistics_sales_scenario(request, complete_logistics_snapshot)
    with patch(
        "app.logistics.service.get_current_inventory_logistics_snapshot",
        side_effect=AssertionError("Repository must not be called"),
    ):
        response = run_logistics_sales_with_snapshot(
            request,
            complete_logistics_snapshot,
        )

    assert first == second
    assert first["future_occupancy_by_date"] == {ARRIVAL: Decimal(5500)}
    assert [item.lot_id for item in first["lot_constraints"]] == ["LOT-001"]
    assert response.snapshot_id == "T0-20260821-001"
    assert complete_logistics_snapshot.model_dump() == before


def test_logistics_engine_preserves_inbound_completeness_fail_closed(
    complete_logistics_snapshot,
    logistics_sales_payload,
):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "in_transit": [
                InTransitItem(
                    item="배추",
                    quantity_kg=Decimal(4500),
                    expected_arrival_date=ARRIVAL,
                )
            ]
        }
    )
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    result = run_logistics_sales_scenario(request, snapshot)
    response = run_logistics_sales_with_snapshot(request, snapshot)

    assert result["inbound_schedule"] is None
    assert result["future_occupancy_by_date"] is None
    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert any(item.code == "IN_TRANSIT_SCHEDULE_UNRESOLVED" for item in response.hard_constraints)
