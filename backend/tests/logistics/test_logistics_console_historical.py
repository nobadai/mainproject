"""#760 (LOG-HIST-002) — 재고 화면 예약 3칸·판매가능량이 **그날 축**이다.

이 파일은 DB 를 안 탄다 — `console_service` 가 `historical_repository.reservation_state_at`
결과를 **집계·조립하는** 새 경로(#760)를 손으로 만든 Historical DTO 로 잠근다.

```text
_reservation_totals_from_history   예약별 (allocated + unallocated) 품목 집계 = 그날 holding
_historical_commitments            그날 할당(Lot 축)·미할당 예약(품목 축) → OutboundCommitment
_historical_availability_snapshot  그날 Lot + commitments → 정본 build_inventory_by_item
```

시간축 유도(존재일=order_date · 소멸=released_as_of · 미래 누출 금지)는
`reservation_state_at` 정본이 지고 `test_logistics_agent_tools_db.py` 가 실 DB 로 잠근다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

_KST = timezone(timedelta(hours=9))

from app.logistics.console_service import (
    _historical_availability_snapshot,
    _historical_commitments,
    _reservation_totals_from_history,
)
from app.logistics.historical_repository import (
    HistoricalAllocation,
    HistoricalLot,
    HistoricalReservation,
)
from app.logistics.schemas import InventoryLogisticsSnapshot
from app.logistics.tools import build_inventory_by_item
from app.logistics.turnover import LotTurnover

AS_OF = date(2026, 1, 10)
BAECHU = "ITEM-BAECHU"
MU = "ITEM-MU"


def _alloc(*, lot_id: str, qty: str, state: str = "ALLOCATED") -> HistoricalAllocation:
    return HistoricalAllocation(
        allocation_id=f"ALC-{lot_id}",
        reservation_id="RSV",
        lot_id=lot_id,
        pallet_id=None,
        allocated_qty_kg=Decimal(qty),
        allocation_basis="FEFO_AUTO_SELECTED",
        decided_by="TEST",
        decided_at=datetime(2026, 1, 5, 9, 0, tzinfo=_KST),
        state=state,  # type: ignore[arg-type]
        shipped_at=None if state != "SHIPPED" else date(2026, 1, 6),
        note=None,
    )


def _resv(
    *,
    reservation_id: str = "RSV",
    item_id: str = BAECHU,
    item_name: str = "배추",
    reserved: str,
    allocated: str = "0",
    shipped: str = "0",
    unallocated: str,
    state: str = "HOLDING",
    allocations: tuple[HistoricalAllocation, ...] = (),
) -> HistoricalReservation:
    return HistoricalReservation(
        reservation_id=reservation_id,
        sim_run_id="SIM",
        item_id=item_id,
        item_name=item_name,
        sale_id="SALE",
        sale_date=AS_OF,
        required_qty_kg=Decimal(reserved),
        reserved_qty_kg=Decimal(reserved),
        due_date=AS_OF,
        state=state,  # type: ignore[arg-type]
        status="HOLDING" if state == "HOLDING" else "RELEASED",
        released_as_of=None if state == "HOLDING" else date(2026, 1, 8),
        allocated_qty_kg=Decimal(allocated),
        shipped_qty_kg=Decimal(shipped),
        unallocated_qty_kg=Decimal(unallocated),
        allocations=allocations,
    )


def _turnover(lot_id: str, remaining: Decimal, freshness: int | None) -> LotTurnover:
    return LotTurnover(
        lot_id=lot_id,
        item_id=BAECHU,
        received_at=date(2026, 1, 1),
        remaining_qty_kg=remaining,
        elapsed_days=9,
        remaining_turnover_days=None,
        turnover_status=None,
        sell_priority=False,
        remaining_freshness_days=freshness,
        effective_freshness_limit_days=10,
        sell_priority_remaining_days=None,
        disposal_candidate=False,
    )


def _lot(
    *, lot_id: str, item_name: str = "배추", remaining: str, freshness: int | None = 5,
    state: str = "ACTIVE",
) -> HistoricalLot:
    rem = Decimal(remaining)
    return HistoricalLot(
        lot_id=lot_id,
        item_id=BAECHU,
        item_name=item_name,
        grade="상",
        storage_zone="COLD",
        received_at=date(2026, 1, 1),
        unit_cost_krw_per_kg=Decimal(1200),
        remaining_qty_kg=rem,
        state=state,  # type: ignore[arg-type]
        turnover=_turnover(lot_id, rem, freshness),
    )


def _empty_snapshot() -> InventoryLogisticsSnapshot:
    return InventoryLogisticsSnapshot(
        snapshot_id=None,
        as_of=AS_OF,
        on_hand_by_lot=[],
        item_storage_policies=None,
        in_transit=None,
        confirmed_inbound_schedule=None,
        confirmed_outbound_schedule=None,
        outbound_commitments=None,
        used_capacity_kg=Decimal(0),
        guaranteed_capacity_by_zone_kg=None,
        evidence_refs=[],
    )


# ── _reservation_totals_from_history ─────────────────────────────────────


def test_예약없으면_빈_집계다() -> None:
    """시나리오 1 — 예약이 없는 날은 3칸도 건수도 0(품목 자체가 안 나온다)."""
    assert _reservation_totals_from_history(()) == {}


def test_미할당_예약은_reserved에_잡힌다() -> None:
    """시나리오 3 — 할당 전 예약은 전량 미할당으로 holding."""
    totals = _reservation_totals_from_history((_resv(reserved="400", unallocated="400"),))
    b = totals[BAECHU]
    assert b["reserved_qty_kg"] == Decimal(400)
    assert b["allocated_qty_kg"] == Decimal(0)
    assert b["unallocated_reserved_qty_kg"] == Decimal(400)
    assert b["active_reservation_count"] == 1


def test_일부_할당이면_allocated와_unallocated로_갈린다() -> None:
    """시나리오 4 · 13 — allocated↑ unallocated↓ · reserved == allocated + unallocated."""
    totals = _reservation_totals_from_history(
        (_resv(reserved="400", allocated="250", unallocated="150"),)
    )
    b = totals[BAECHU]
    assert b["allocated_qty_kg"] == Decimal(250)
    assert b["unallocated_reserved_qty_kg"] == Decimal(150)
    assert b["reserved_qty_kg"] == b["allocated_qty_kg"] + b["unallocated_reserved_qty_kg"]


def test_전량_할당이면_unallocated_0() -> None:
    """시나리오 5 — 전량 Lot 에 붙으면 미할당은 0."""
    totals = _reservation_totals_from_history(
        (_resv(reserved="400", allocated="400", unallocated="0"),)
    )
    assert totals[BAECHU]["unallocated_reserved_qty_kg"] == Decimal(0)
    assert totals[BAECHU]["allocated_qty_kg"] == Decimal(400)


def test_전량_출고면_holding_0이라_건수에서도_빠진다() -> None:
    """시나리오 7 — SHIPPED 는 allocated·unallocated 에 없어 holding=0 → 활성 건수 0."""
    shipped = _resv(reserved="400", allocated="0", shipped="400", unallocated="0")
    totals = _reservation_totals_from_history((shipped,))
    assert totals[BAECHU]["reserved_qty_kg"] == Decimal(0)
    assert totals[BAECHU]["active_reservation_count"] == 0


def test_놓아준_예약은_holding_0() -> None:
    """시나리오 9 · 10 — RELEASED 는 allocated·unallocated 가 0 이라 안 잡힌다."""
    released = _resv(reserved="400", allocated="0", unallocated="0", state="RELEASED")
    totals = _reservation_totals_from_history((released,))
    assert totals[BAECHU]["reserved_qty_kg"] == Decimal(0)
    assert totals[BAECHU]["active_reservation_count"] == 0


def test_원래_확보량을_그대로_합하지_않는다() -> None:
    """🔴 reserved_qty_kg(확보했던 양)가 아니라 holding(allocated+unallocated)을 센다."""
    #  확보 500 인데 300 출고됨 → 지금 잡은 건 200 뿐.
    resv = _resv(reserved="500", allocated="0", shipped="300", unallocated="200")
    totals = _reservation_totals_from_history((resv,))
    assert totals[BAECHU]["reserved_qty_kg"] == Decimal(200)  # 500 이 아니다


def test_품목별로_모으고_건수를_센다() -> None:
    """여러 품목·여러 예약이 품목 축으로 합쳐지고 활성 건수는 holding>0 만 센다."""
    totals = _reservation_totals_from_history(
        (
            _resv(reservation_id="R1", item_id=BAECHU, reserved="100", unallocated="100"),
            _resv(reservation_id="R2", item_id=BAECHU, reserved="50", unallocated="50"),
            _resv(reservation_id="R3", item_id=MU, item_name="무", reserved="0", unallocated="0"),
        )
    )
    assert totals[BAECHU]["reserved_qty_kg"] == Decimal(150)
    assert totals[BAECHU]["active_reservation_count"] == 2
    #  holding 0 인 무 예약은 건수 0(품목 키는 남되 활성 아님).
    assert totals[MU]["active_reservation_count"] == 0


# ── _historical_commitments ──────────────────────────────────────────────


def test_commitments_lot축과_품목축으로_갈린다() -> None:
    """살아있는 할당은 lot_id 있는 commitment · 미할당은 lot_id 없는 commitment."""
    resv = _resv(
        reserved="400",
        allocated="250",
        unallocated="150",
        allocations=(_alloc(lot_id="LOT-A", qty="250"),),
    )
    commitments = _historical_commitments((resv,))
    lot_level = [c for c in commitments if c.lot_id is not None]
    item_level = [c for c in commitments if c.lot_id is None]
    assert [(c.lot_id, c.quantity_kg) for c in lot_level] == [("LOT-A", Decimal(250))]
    assert [(c.item, c.quantity_kg) for c in item_level] == [("배추", Decimal(150))]


def test_commitments_는_SHIPPED_할당을_넣지_않는다() -> None:
    """🔴 나간 몫은 원장 OUT 이 이미 뺐다 — 다시 넣으면 이중 차감."""
    resv = _resv(
        reserved="400",
        allocated="0",
        shipped="400",
        unallocated="0",
        allocations=(_alloc(lot_id="LOT-A", qty="400", state="SHIPPED"),),
    )
    assert _historical_commitments((resv,)) == []


def test_commitments_는_놓아준_예약을_넣지_않는다() -> None:
    """RELEASED 예약은 미할당 0 이라 품목 축 commitment 도 없다."""
    released = _resv(reserved="400", allocated="0", unallocated="0", state="RELEASED")
    assert _historical_commitments((released,)) == []


# ── _historical_availability_snapshot → build_inventory_by_item ──────────


def test_판매가능량은_그날_Lot에서_미할당예약을_뺀다() -> None:
    """시나리오 14 — 정본 재사용: ACTIVE·신선 Lot 500 − 미할당 예약 200 = 300 (음수 아님)."""
    snap = _historical_availability_snapshot(
        _empty_snapshot(),
        lots=[_lot(lot_id="LOT-A", remaining="500")],
        reservations=[_resv(reserved="200", unallocated="200")],
        used_capacity_kg=Decimal(500),
    )
    by_item = build_inventory_by_item(snap)
    assert by_item is not None
    avail = {row.item: row.available_qty_kg for row in by_item}
    assert avail["배추"] == Decimal(300)


def test_판매가능량은_붙은_할당을_Lot에서_뺀다() -> None:
    """Lot 500 에 살아있는 할당 200 이 붙으면 남에게 팔 수 있는 건 300."""
    resv = _resv(
        reserved="200",
        allocated="200",
        unallocated="0",
        allocations=(_alloc(lot_id="LOT-A", qty="200"),),
    )
    snap = _historical_availability_snapshot(
        _empty_snapshot(),
        lots=[_lot(lot_id="LOT-A", remaining="500")],
        reservations=[resv],
        used_capacity_kg=Decimal(500),
    )
    by_item = build_inventory_by_item(snap)
    assert by_item is not None
    assert {row.item: row.available_qty_kg for row in by_item}["배추"] == Decimal(300)


def test_판매가능량은_신선도_만료_Lot을_뺀다() -> None:
    """신선도 <= 0 Lot 은 판매가능에서 빠진다(잔량은 남아도)."""
    snap = _historical_availability_snapshot(
        _empty_snapshot(),
        lots=[_lot(lot_id="LOT-FRESH", remaining="300", freshness=5),
              _lot(lot_id="LOT-DEAD", remaining="200", freshness=0)],
        reservations=[],
        used_capacity_kg=Decimal(500),
    )
    by_item = build_inventory_by_item(snap)
    assert by_item is not None
    assert {row.item: row.available_qty_kg for row in by_item}["배추"] == Decimal(300)


def test_판매가능량은_음수로_내려가지_않는다() -> None:
    """시나리오 14 — 미할당 예약이 재고보다 커도 0 에서 멈춘다."""
    snap = _historical_availability_snapshot(
        _empty_snapshot(),
        lots=[_lot(lot_id="LOT-A", remaining="100")],
        reservations=[_resv(reserved="400", unallocated="400")],
        used_capacity_kg=Decimal(100),
    )
    by_item = build_inventory_by_item(snap)
    assert by_item is not None
    assert {row.item: row.available_qty_kg for row in by_item}["배추"] == Decimal(0)
