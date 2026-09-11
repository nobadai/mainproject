"""확정 판매 물량의 FIFO 재고 취득원가 (#1 `inventory_cost_basis`).

이 파일이 지키는 것은 하나다 — **원가는 창고 장부에서만 온다.**

```text
어느 Lot 을 헐었나   received_at ASC → lot_id ASC   (allocation_method = FIFO)
얼마에 들어왔나      inventory_lots.unit_cost_krw_per_kg  (cost_method = ACTUAL)
다 못 덮으면         기준을 세우지 않는다 (None)  → 재무 RUNTIME_NOT_READY
```

🔴 두 축을 섞지 않는다. *"FIFO 로 골랐으니 원가도 FIFO 다"* 라는 말은 없다.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.logistics.schemas import InventoryLotSnapshot, OutboundCommitment
from app.logistics.tools import build_inventory_by_item, fifo_inventory_cost_basis


def _lot(
    lot_id: str,
    *,
    item: str = "배추",
    qty: int | str,
    received: date | None,
    cost: int | str | None,
    freshness: int | None = 8,
    status: str = "ACTIVE",
) -> InventoryLotSnapshot:
    return InventoryLotSnapshot(
        lot_id=lot_id,
        item=item,
        available_qty_kg=Decimal(qty),
        received_at=received,
        unit_cost_krw_per_kg=None if cost is None else Decimal(cost),
        remaining_freshness_days=freshness,
        status=status,
    )


@pytest.fixture
def two_lot_snapshot(complete_logistics_snapshot):
    """실측 회귀 고정물 — 58kg = 29×886 + 29×682 = 45,472 KRW.

    ★ Lot ID 를 계산에 박지 않는다. 순서를 정하는 것은 `received_at` 이고, ID 는
      같은 날 입고가 겹칠 때의 안정적인 2차 키일 뿐이다.
    """
    return complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-B", qty=29, received=date(2026, 8, 20), cost=682),
                _lot("LOT-A", qty=29, received=date(2026, 8, 18), cost=886),
            ],
            "used_capacity_kg": Decimal(58),
        }
    )


# ---------------------------------------------------------------------------
# FIFO 배부
# ---------------------------------------------------------------------------


def test_two_lots_are_consumed_oldest_first(two_lot_snapshot):
    basis = fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.amount_krw == Decimal(45_472)
    assert basis.quantity_kg == Decimal(58)
    # 오래된 Lot 이 먼저다 — 목록의 순서가 아니라 입고일이 정한다.
    assert basis.source_refs == ("LOT-A", "LOT-B")


def test_a_partial_draw_touches_only_the_oldest_lot(two_lot_snapshot):
    basis = fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.amount_krw == Decimal(8_860)
    assert basis.source_refs == ("LOT-A",)


def test_the_same_received_date_falls_back_to_a_stable_lot_order(complete_logistics_snapshot):
    """같은 날 입고는 `lot_id ASC` 로 갈린다 — 목록 순서에 흔들리지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-Z", qty=10, received=date(2026, 8, 18), cost=500),
                _lot("LOT-A", qty=10, received=date(2026, 8, 18), cost=100),
            ]
        }
    )

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(15))

    assert basis is not None
    assert basis.source_refs == ("LOT-A", "LOT-Z")
    assert basis.amount_krw == Decimal(10 * 100 + 5 * 500)


def test_other_items_are_never_drawn(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-무", item="무", qty=100, received=date(2026, 8, 1), cost=100),
                _lot("LOT-배추", qty=10, received=date(2026, 8, 18), cost=900),
            ]
        }
    )

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.source_refs == ("LOT-배추",)
    assert basis.amount_krw == Decimal(9_000)


def test_every_number_stays_decimal(two_lot_snapshot):
    basis = fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert isinstance(basis.amount_krw, Decimal)
    assert isinstance(basis.quantity_kg, Decimal)


# ---------------------------------------------------------------------------
# 부족·미확인 — 메우지 않는다
# ---------------------------------------------------------------------------


def test_insufficient_stock_produces_no_basis_at_all(two_lot_snapshot):
    """🔴 모자란 몫을 0원으로 메우지 않는다. 기준 자체가 서지 않는다."""
    assert fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(59)) is None


def test_a_lot_without_a_unit_cost_stops_the_basis(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=10, received=date(2026, 8, 18), cost=886),
                _lot("LOT-B", qty=10, received=date(2026, 8, 19), cost=None),
            ]
        }
    )

    # 앞 Lot 만으로 덮이면 뒤 Lot 의 미확인 단가는 이 판매와 무관하다.
    covered = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))
    assert covered is not None
    assert covered.amount_krw == Decimal(8_860)

    # 단가를 모르는 Lot 을 헐어야 하면 이 판매의 원가는 알 수 없다.
    assert fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(11)) is None


def test_a_lot_without_a_received_date_breaks_the_order(complete_logistics_snapshot):
    """순서를 모르는 Lot 을 아무 데나 끼우지 않는다 — 배부 자체를 하지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=100, received=None, cost=886),
            ]
        }
    )

    assert fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10)) is None


def test_unread_commitments_fail_closed(complete_logistics_snapshot):
    """`outbound_commitments = None` 은 미조회다 — 0건으로 놓지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot("LOT-A", qty=100, received=date(2026, 8, 18), cost=886)],
            "outbound_commitments": None,
        }
    )

    assert fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10)) is None
    assert build_inventory_by_item(snapshot) is None


def test_zero_quantity_has_no_cost_basis(two_lot_snapshot):
    """0kg 판매의 «원가 0원» 은 사실이 아니라 셈이 없는 상태다."""
    assert fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(0)) is None


# ---------------------------------------------------------------------------
# 판매가능 판정의 주인은 한 곳 — 같은 규칙을 소비한다
# ---------------------------------------------------------------------------


def test_non_active_lots_are_not_drawn(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot(
                    "LOT-HOLD", qty=100, received=date(2026, 8, 1), cost=100, status="QUARANTINED"
                ),
                _lot("LOT-OK", qty=10, received=date(2026, 8, 18), cost=900),
            ]
        }
    )

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.source_refs == ("LOT-OK",)
    # 격리 Lot 을 헐어야 하는 양은 덮이지 않는다.
    assert fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(11)) is None


def test_expired_lots_are_not_drawn(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-EXPIRED", qty=100, received=date(2026, 8, 1), cost=100, freshness=0),
                _lot("LOT-OK", qty=10, received=date(2026, 8, 18), cost=900),
            ]
        }
    )

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.source_refs == ("LOT-OK",)


def test_an_unknown_freshness_lot_is_still_sellable(complete_logistics_snapshot):
    """`None` 은 «만료 확인» 이 아니다 — 가용에서 숨기지 않는다 (0 != null)."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=10, received=date(2026, 8, 18), cost=886, freshness=None)
            ]
        }
    )

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.source_refs == ("LOT-A",)


def test_lot_allocations_are_removed_before_the_draw(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=29, received=date(2026, 8, 18), cost=886),
                _lot("LOT-B", qty=29, received=date(2026, 8, 20), cost=682),
            ],
            "outbound_commitments": [
                OutboundCommitment(item="배추", lot_id="LOT-A", quantity_kg=Decimal(29))
            ],
        }
    )

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))

    assert basis is not None
    # 이미 남에게 잡힌 Lot 은 헐 수 없다 — 다음 Lot 이 전부를 덮는다.
    assert basis.source_refs == ("LOT-B",)
    assert basis.amount_krw == Decimal(29 * 682)


def test_item_level_reservations_are_eaten_oldest_first(complete_logistics_snapshot):
    """Lot 을 안 고른 예약도 FIFO 로 선점된 것으로 본다.

    🔴 그래야 여기서 배부 가능한 총량이 `build_inventory_by_item` 의 품목 합계와
       **정확히 같다.** 두 셈이 갈리면 «팔 수 있다고 답한 양» 의 원가를 못 낸다.
    """
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=29, received=date(2026, 8, 18), cost=886),
                _lot("LOT-B", qty=29, received=date(2026, 8, 20), cost=682),
            ],
            "outbound_commitments": [
                OutboundCommitment(item="배추", lot_id=None, quantity_kg=Decimal(29))
            ],
        }
    )

    inventory = build_inventory_by_item(snapshot)
    assert inventory is not None
    assert [(row.item, row.available_qty_kg) for row in inventory] == [("배추", Decimal(29))]

    basis = fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))
    assert basis is not None
    assert basis.source_refs == ("LOT-B",)
    assert basis.amount_krw == Decimal(29 * 682)
    # 품목 합계가 29kg 인데 30kg 의 원가는 설 수 없다 — 두 셈이 같은 상한을 본다.
    assert fifo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(30)) is None


# ---------------------------------------------------------------------------
# 계약 모양
# ---------------------------------------------------------------------------


def test_the_basis_declares_both_axes_separately(two_lot_snapshot):
    basis = fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.allocation_method == "FIFO"
    assert basis.cost_method == "ACTUAL"
    assert basis.included_components == ("inventory_acquisition_cost",)
    assert basis.evidence_grade == "SIM_FIXED"
    assert basis.item == "배추"


def test_the_single_source_ref_is_only_the_first_of_the_lineage(two_lot_snapshot):
    """`source_ref` 는 하위 호환용 대표 하나다. 계보는 `source_refs` 다."""
    basis = fifo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.source_ref == "LOT-A"
    assert len(basis.source_refs) == 2
