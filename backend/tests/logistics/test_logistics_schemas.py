from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.logistics.schemas import (
    InTransitItem,
    LogisticsProcurementResponse,
    LogisticsSalesRequest,
    PurchaseAgentOutput,
    ScenarioAdjustment,
    ScheduledQuantity,
)


def _procurement_response(**overrides) -> LogisticsProcurementResponse:
    base = {
        "as_of": "2026-08-21",
        "snapshot_id": None,
        "runtime_status": "READY",
        "verdict": "PASS",
        "band": {"cap_by_date": {}},
        "inbound_constraints": {
            "inbound_lead_days": 2,
            "daily_inbound_capacity_kg": None,
            "inbound_transport_capacity_kg": None,
        },
        "hard_constraints": [],
        "soft_warnings": [],
        "evidences": [],
    }
    return LogisticsProcurementResponse(**{**base, **overrides})


def test_logistics_procurement_accepts_purchase_v04(logistics_purchase_payload):
    request = PurchaseAgentOutput.model_validate(logistics_purchase_payload)

    assert request.scenarios[0].total_qty_kg == 4500
    assert request.scenarios[0].split_plan[0].qty_kg == 4500


def test_logistics_procurement_rejects_legacy_ton_fields(logistics_purchase_payload):
    scenario = logistics_purchase_payload["scenarios"][0]
    scenario["total_quantity_ton"] = scenario.pop("total_qty_kg")

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(logistics_purchase_payload)


def test_logistics_procurement_rejects_duplicate_kg_names(logistics_purchase_payload):
    scenario = logistics_purchase_payload["scenarios"][0]
    scenario["total_quantity_kg"] = scenario.pop("total_qty_kg")
    scenario["split_plan"][0]["quantity_kg"] = scenario["split_plan"][0].pop("qty_kg")

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(logistics_purchase_payload)


def test_logistics_sales_contract_keeps_approved_quantity_decimal(logistics_sales_payload):
    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    assert request.approved_purchase.total_qty_kg == Decimal(4500)


def test_logistics_sales_rejects_sales_agent_candidates(logistics_sales_payload):
    logistics_sales_payload["candidates"] = [{"channel": "KIMCHI_FACTORY"}]

    with pytest.raises(ValidationError):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


def test_logistics_sales_rejects_arrival_total_mismatch(logistics_sales_payload):
    logistics_sales_payload["approved_purchase"]["total_qty_kg"] = 4000

    with pytest.raises(ValidationError):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


def test_logistics_sales_rejects_arrival_before_as_of(logistics_sales_payload):
    logistics_sales_payload["approved_purchase"]["expected_arrival_date"] = "2026-08-20"
    logistics_sales_payload["approved_purchase"]["arrival_schedule"][0]["date"] = "2026-08-20"

    with pytest.raises(ValidationError, match="on or after as_of"):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


@pytest.mark.parametrize("arrival_date", ["2026-08-21", "2026-08-23"])
def test_logistics_sales_accepts_arrival_on_or_after_as_of(logistics_sales_payload, arrival_date):
    logistics_sales_payload["approved_purchase"]["expected_arrival_date"] = arrival_date
    logistics_sales_payload["approved_purchase"]["arrival_schedule"][0]["date"] = arrival_date

    request = LogisticsSalesRequest.model_validate(logistics_sales_payload)

    assert request.approved_purchase.expected_arrival_date.isoformat() == arrival_date


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("approved_purchase", "total_qty_kg"), True),
        (("approved_purchase", "arrival_schedule", 0, "quantity_kg"), True),
    ],
)
def test_logistics_sales_rejects_boolean_numbers(logistics_sales_payload, path, value):
    target = logistics_sales_payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        LogisticsSalesRequest.model_validate(logistics_sales_payload)


def test_uncomputable_inventory_by_item_is_omitted_not_empty():
    """None(계산 불가)은 키 생략, [](0건 확인)은 그대로 나간다 — 0 != null."""
    omitted = _procurement_response(inventory_by_item=None).model_dump(mode="json")
    kept = _procurement_response(inventory_by_item=[]).model_dump(mode="json")

    assert "inventory_by_item" not in omitted
    assert kept["inventory_by_item"] == []


def test_inventory_by_item_round_trips():
    response = _procurement_response(inventory_by_item=[{"item": "배추", "available_qty_kg": 3000}])

    assert response.inventory_by_item is not None
    assert response.inventory_by_item[0].available_qty_kg == Decimal(3000)


def test_scenario_adjustment_allows_only_quantity_and_timing():
    """물류 Adjustment 축은 quantity/timing뿐 — amount/channel_mix 금지."""
    ScenarioAdjustment(axis="quantity", split_date=date(2026, 8, 21))
    ScenarioAdjustment(axis="timing", split_date=date(2026, 8, 21))
    for forbidden in ("amount", "channel_mix"):
        with pytest.raises(ValidationError):
            ScenarioAdjustment(axis=forbidden, split_date=date(2026, 8, 21))


# ── InTransitItem.purchase_id (읽기 계약만) ─────────────────────────────
#
# 🟡 **받을 자리는 뚫려 있지만 아직 안 켜졌다.** `purchase_id` 를 만드는 곳은
#    마스터이고, 그 값이 물류로 넘어오려면 마스터 전이 규약이 바뀌어야 한다 —
#    **후속 협의 안건**이다. 여기서 잠그는 것은 **모델의 읽기 계약**이고,
#    생산자 쪽 동작은 `test_logistics_transition.py` 가 잰다.


def test_기존_fixture_행은_purchase_id_없이도_읽힌다():
    """🔴 전역 필수로 올리면 이미 적혀 있는 행들이 통째로 파싱에 실패한다.

    그러면 물류가 그날 `RUNTIME_NOT_READY` 로 돌아선다 — 값 하나 때문에 판단 전체가
    멈추는 자리다. `extra="forbid"` 라 **없는 키는 금지지만 기본값 있는 새 필드는
    안전하다.**
    """
    옛_행 = {
        "inbound_id": "INB-OLD-1",
        "item": "배추",
        "quantity_kg": "100",
        "expected_arrival_date": "2026-01-07",
    }

    되읽은 = InTransitItem.model_validate(옛_행)

    assert 되읽은.purchase_id is None
    assert 되읽은.inbound_id == "INB-OLD-1"
    assert 되읽은.quantity_kg == Decimal(100)


def test_purchase_id_는_inbound_id_와_다른_정체성이다():
    """★ 둘을 같은 칸으로 접지 않는다 — B-1 대조의 열쇠는 여전히 `inbound_id` 다."""
    행 = InTransitItem(
        inbound_id="INB-H1-REQ-1-1-1",
        purchase_id="PUR-REQ-1-D1-S1",
        item="배추",
        quantity_kg=Decimal(300),
        expected_arrival_date=date(2026, 1, 2),
    )

    assert 행.inbound_id != 행.purchase_id
    assert 행.model_dump(mode="json")["purchase_id"] == "PUR-REQ-1-D1-S1"


def test_확정_입고_일정에는_purchase_id_칸이_없다():
    """🔴 **일정·수량 사실에는 출처를 얹지 않는다.**

    `ScheduledQuantity` 는 outbound 등 다른 일정에도 재사용된다. 어느 매입에서
    왔는지는 **운송 중인 물건의 속성**이지 일정의 속성이 아니다.
    """
    assert "purchase_id" not in ScheduledQuantity.model_fields

    with pytest.raises(ValidationError):
        ScheduledQuantity(
            date=date(2026, 1, 2),
            quantity_kg=Decimal(300),
            item="배추",
            inbound_id="INB-H1-REQ-1-1-1",
            purchase_id="PUR-REQ-1-D1-S1",
        )
