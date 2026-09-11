"""확정 판매 물량의 FEFO 재고 취득원가 (#1 `inventory_cost_basis`).

이 파일이 지키는 것은 둘이다.

```text
① 원가는 창고 장부에서만 온다      inventory_lots.unit_cost_krw_per_kg (cost_method=ACTUAL)
② Lot 고르는 순서는 실제 출고와 같다 turnover.fefo_sort_key            (allocation_method=FEFO)
```

```text
어느 Lot 을 고르나   신선도 UNKNOWN 후행 → remaining_freshness_days ASC
                     → received_at ASC → lot_id ASC
다 못 덮으면         기준을 세우지 않는다 (None)  → 재무 RUNTIME_NOT_READY
```

🔴 두 축을 섞지 않는다. *"FEFO 로 골랐으니 원가도 FEFO 다"* 라는 말은 없다.

⚠️ **여기 담기는 Lot 은 «출고된 Lot» 이 아니다.** PRE_SALES 는 이 판매의 예약도
   할당도 서기 전이라, *"지금 출고한다면 FEFO 가 집을 Lot"* 이다. 그래서 **순서만이라도**
   실제 자동 출고(`outbound.recommend_fefo_candidates`)와 같아야 한다.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.logistics import tools as logistics_tools
from app.logistics.schemas import InventoryLotSnapshot, OutboundCommitment
from app.logistics.tools import build_inventory_by_item, fefo_inventory_cost_basis
from app.logistics.turnover import fefo_sort_key


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
# FEFO 배부 — 신선도가 같으면 입고순이다 (기존 회귀)
# ---------------------------------------------------------------------------


def test_two_lots_are_consumed_oldest_first(two_lot_snapshot):
    """신선도가 같으면 FEFO 의 2차 키(`received_at`)가 결정한다 — 입고순이다."""
    basis = fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.amount_krw == Decimal(45_472)
    assert basis.quantity_kg == Decimal(58)
    # 오래된 Lot 이 먼저다 — 목록의 순서가 아니라 입고일이 정한다.
    assert basis.source_refs == ("LOT-A", "LOT-B")


def test_a_partial_draw_touches_only_the_oldest_lot(two_lot_snapshot):
    basis = fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.amount_krw == Decimal(8_860)
    assert basis.source_refs == ("LOT-A",)


def test_the_same_received_date_falls_back_to_a_stable_lot_order(complete_logistics_snapshot):
    """신선도도 입고일도 같으면 `lot_id ASC` 로 갈린다 — 목록 순서에 안 흔들린다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-Z", qty=10, received=date(2026, 8, 18), cost=500),
                _lot("LOT-A", qty=10, received=date(2026, 8, 18), cost=100),
            ]
        }
    )

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(15))

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

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.source_refs == ("LOT-배추",)
    assert basis.amount_krw == Decimal(9_000)


def test_every_number_stays_decimal(two_lot_snapshot):
    basis = fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert isinstance(basis.amount_krw, Decimal)
    assert isinstance(basis.quantity_kg, Decimal)


# ---------------------------------------------------------------------------
# 부족·미확인 — 메우지 않는다
# ---------------------------------------------------------------------------


def test_insufficient_stock_produces_no_basis_at_all(two_lot_snapshot):
    """🔴 모자란 몫을 0원으로 메우지 않는다. 기준 자체가 서지 않는다."""
    assert fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(59)) is None


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
    covered = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))
    assert covered is not None
    assert covered.amount_krw == Decimal(8_860)

    # 단가를 모르는 Lot 을 헐어야 하면 이 판매의 원가는 알 수 없다.
    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(11)) is None


def test_a_lot_without_a_received_date_breaks_the_order(complete_logistics_snapshot):
    """순서를 모르는 Lot 을 아무 데나 끼우지 않는다 — 배부 자체를 하지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=100, received=None, cost=886),
            ]
        }
    )

    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10)) is None


def test_unread_commitments_fail_closed(complete_logistics_snapshot):
    """`outbound_commitments = None` 은 미조회다 — 0건으로 놓지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [_lot("LOT-A", qty=100, received=date(2026, 8, 18), cost=886)],
            "outbound_commitments": None,
        }
    )

    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10)) is None
    assert build_inventory_by_item(snapshot) is None


def test_zero_quantity_has_no_cost_basis(two_lot_snapshot):
    """0kg 판매의 «원가 0원» 은 사실이 아니라 셈이 없는 상태다."""
    assert fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(0)) is None


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

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

    assert basis is not None
    assert basis.source_refs == ("LOT-OK",)
    # 격리 Lot 을 헐어야 하는 양은 덮이지 않는다.
    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(11)) is None


def test_expired_lots_are_not_drawn(complete_logistics_snapshot):
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-EXPIRED", qty=100, received=date(2026, 8, 1), cost=100, freshness=0),
                _lot("LOT-OK", qty=10, received=date(2026, 8, 18), cost=900),
            ]
        }
    )

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

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

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10))

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

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))

    assert basis is not None
    # 이미 남에게 잡힌 Lot 은 헐 수 없다 — 다음 Lot 이 전부를 덮는다.
    assert basis.source_refs == ("LOT-B",)
    assert basis.amount_krw == Decimal(29 * 682)


def test_item_level_reservations_are_eaten_oldest_first(complete_logistics_snapshot):
    """Lot 을 안 고른 예약도 FEFO 앞 Lot 부터 선점된 것으로 본다.

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

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))
    assert basis is not None
    assert basis.source_refs == ("LOT-B",)
    assert basis.amount_krw == Decimal(29 * 682)
    # 품목 합계가 29kg 인데 30kg 의 원가는 설 수 없다 — 두 셈이 같은 상한을 본다.
    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(30)) is None


# ---------------------------------------------------------------------------
# 계약 모양
# ---------------------------------------------------------------------------


def test_the_basis_declares_both_axes_separately(two_lot_snapshot):
    basis = fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.allocation_method == "FEFO"
    assert basis.cost_method == "ACTUAL"
    assert basis.included_components == ("inventory_acquisition_cost",)
    assert basis.evidence_grade == "SIM_FIXED"
    assert basis.item == "배추"


def test_the_single_source_ref_is_only_the_first_of_the_lineage(two_lot_snapshot):
    """`source_ref` 는 하위 호환용 대표 하나다. 배부 근거 전체는 `source_refs` 다."""
    basis = fefo_inventory_cost_basis(two_lot_snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.source_ref == "LOT-A"
    assert len(basis.source_refs) == 2


# ---------------------------------------------------------------------------
# 🔴 FEFO 순서 — 실제 자동 출고와 갈리지 않는다 (FEFO 전환 회귀)
#
#   `received_at` 순서와 `remaining_freshness_days` 순서는 같은 품목 안에서 유효
#   보관한계가 갈릴 때 달라진다 (`중` 등급 계수 · 보관정책 미등록). 아래가 그 자리다.
# ---------------------------------------------------------------------------


def test_fefo_and_fifo_agree_when_freshness_follows_receipt(complete_logistics_snapshot):
    """Case 1 — 먼저 들어온 것이 먼저 만료되면 두 순서가 같다. **기존 동작 그대로다.**"""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=29, received=date(2026, 8, 18), cost=886, freshness=5),
                _lot("LOT-B", qty=29, received=date(2026, 8, 20), cost=682, freshness=20),
            ]
        }
    )

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(58))

    assert basis is not None
    assert basis.source_refs == ("LOT-A", "LOT-B")
    assert basis.amount_krw == Decimal(29 * 886 + 29 * 682)


def test_the_sooner_expiring_lot_wins_even_if_it_arrived_later(complete_logistics_snapshot):
    """Case 2 — **여기가 입고순과 갈린다.** 나중에 들어왔어도 먼저 만료되면 먼저 쓴다.

    🔴 입고순이면 `LOT-OLD` 가 먼저다. 실제 자동 출고는 `LOT-NEW` 를 먼저 내보내므로
       (`outbound.recommend_fefo_candidates`), 원가도 그것을 따라야 한다.
    """
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-OLD", qty=29, received=date(2026, 8, 18), cost=1000, freshness=20),
                _lot("LOT-NEW", qty=29, received=date(2026, 8, 20), cost=500, freshness=5),
            ]
        }
    )

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))

    assert basis is not None
    assert basis.source_refs == ("LOT-NEW",)
    assert basis.amount_krw == Decimal(29 * 500)
    # 입고순이면 1000원짜리가 잡혔을 자리다 — 두 값이 다른 것이 이 테스트의 요점이다.
    assert basis.amount_krw != Decimal(29 * 1000)


def test_an_unknown_freshness_lot_goes_last_like_real_shipping(complete_logistics_snapshot):
    """Case 3 — 신선도를 **모르는** Lot 은 맨 뒤다. 실제 출고 규칙과 같다 (`0 != null`).

    ★ 가용에서 빼지는 않는다 — 빼는 것은 «만료 확인»(`<= 0`)뿐이다.
    """
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-UNKNOWN", qty=29, received=date(2026, 8, 18), cost=900, freshness=None),
                _lot("LOT-B", qty=29, received=date(2026, 8, 20), cost=700, freshness=5),
            ]
        }
    )

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))
    assert basis is not None
    assert basis.source_refs == ("LOT-B",)

    # 뒤로 갔을 뿐 빠진 것이 아니다 — 더 달라면 그 Lot 이 이어서 덮는다.
    전량 = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(58))
    assert 전량 is not None
    assert 전량.source_refs == ("LOT-B", "LOT-UNKNOWN")


def test_source_refs_keep_the_order_the_basis_was_drawn_in(complete_logistics_snapshot):
    """Case 4 — 여러 Lot 에 걸치면 **FEFO 배부 순서 그대로** 모두 남는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-A", qty=50, received=date(2026, 8, 10), cost=100, freshness=10),
                _lot("LOT-B", qty=20, received=date(2026, 8, 20), cost=200, freshness=3),
                _lot("LOT-C", qty=30, received=date(2026, 8, 15), cost=300, freshness=30),
            ]
        }
    )

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(100))

    assert basis is not None
    # 신선도 3 → 10 → 30 순이다. 입고순(A·C·B)도 목록순(A·B·C)도 아니다.
    assert basis.source_refs == ("LOT-B", "LOT-A", "LOT-C")
    assert basis.amount_krw == Decimal(20 * 200 + 50 * 100 + 30 * 300)


def test_item_level_reservations_are_eaten_in_fefo_order(complete_logistics_snapshot):
    """Case 5 — 미할당 예약도 **FEFO 앞 Lot 부터** 선점된 것으로 본다.

    🔴 실제 자동 할당(`fefo_allocation.allocate_reserved_stock_fefo`)이 FEFO 앞쪽부터
       집으므로 선점 순서도 그것과 같아야 한다. 입고순으로 먹으면 남는 Lot 이 달라져
       **원가가 통째로 바뀐다.**
    """
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-OLD", qty=29, received=date(2026, 8, 18), cost=1000, freshness=20),
                _lot("LOT-NEW", qty=29, received=date(2026, 8, 20), cost=500, freshness=5),
            ],
            "outbound_commitments": [
                OutboundCommitment(item="배추", lot_id=None, quantity_kg=Decimal(29))
            ],
        }
    )

    # 품목 합계는 순서와 무관하다 — 두 셈이 같은 상한을 본다.
    inventory = build_inventory_by_item(snapshot)
    assert inventory is not None
    assert [(row.item, row.available_qty_kg) for row in inventory] == [("배추", Decimal(29))]

    basis = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(29))

    assert basis is not None
    # 예약이 FEFO 앞쪽(LOT-NEW)을 먹었으므로 남은 것은 LOT-OLD 다.
    assert basis.source_refs == ("LOT-OLD",)
    assert basis.amount_krw == Decimal(29 * 1000)
    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(30)) is None


def test_a_missing_unit_cost_on_the_fefo_first_lot_stops_the_basis(complete_logistics_snapshot):
    """Case 6 — **FEFO 가 실제로 헐어야 하는** Lot 의 단가가 없으면 기준을 안 세운다.

    🔴 입고순이었다면 `LOT-OLD` 만으로 덮여 기준이 섰을 자리다. 순서가 바뀌었으므로
       단가 미확인 Lot 이 배부 대상에 들어오고, 그때는 **멈춘다** (평균·0원 대체 없음).
    """
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-OLD", qty=10, received=date(2026, 8, 18), cost=886, freshness=20),
                _lot("LOT-NEW", qty=10, received=date(2026, 8, 20), cost=None, freshness=5),
            ]
        }
    )

    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(10)) is None


def test_fefo_stock_that_cannot_cover_the_quantity_makes_no_basis(complete_logistics_snapshot):
    """Case 7 — FEFO 가용 Lot 을 다 헐어도 못 덮으면 기준이 서지 않는다."""
    snapshot = complete_logistics_snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                _lot("LOT-OLD", qty=10, received=date(2026, 8, 18), cost=886, freshness=20),
                _lot("LOT-NEW", qty=10, received=date(2026, 8, 20), cost=682, freshness=5),
            ]
        }
    )

    덮인다 = fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(20))
    assert 덮인다 is not None
    assert 덮인다.source_refs == ("LOT-NEW", "LOT-OLD")

    assert fefo_inventory_cost_basis(snapshot, item="배추", quantity_kg=Decimal(21)) is None


# ---------------------------------------------------------------------------
# 🔴 정렬 키의 주인은 하나다
# ---------------------------------------------------------------------------


def test_the_sort_key_is_the_one_real_shipping_uses():
    """원가 배부와 실제 출고가 **같은 함수**를 부른다 — 두 벌이 되면 또 갈린다."""
    물류 = Path(logistics_tools.__file__).parent

    for 이름 in ("tools.py", "outbound.py"):
        코드 = (물류 / 이름).read_text(encoding="utf-8")
        assert "fefo_sort_key" in 코드, f"{이름} 이 공유 정렬 키를 안 쓴다"
        # 정렬 튜플을 그 자리에서 다시 적으면 한쪽만 고쳐지는 날이 온다.
        assert "remaining_freshness_days is None," not in 코드, f"{이름} 이 키를 복제했다"


def test_the_sort_key_orders_unknown_freshness_last():
    """키 자체의 계약 — 네 요소이고 UNKNOWN 이 맨 뒤다."""
    모름 = fefo_sort_key(remaining_freshness_days=None, received_at=date(2026, 1, 1), lot_id="A")
    임박 = fefo_sort_key(remaining_freshness_days=1, received_at=date(2026, 12, 31), lot_id="Z")

    assert 임박 < 모름
    assert 모름 == (True, 0, date(2026, 1, 1), "A")
    assert 임박 == (False, 1, date(2026, 12, 31), "Z")
