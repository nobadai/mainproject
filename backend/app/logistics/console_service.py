"""재고·물류 운영 콘솔 Service — 기존 도메인 함수를 **조립만** 한다.

🔴 **이 파일은 업무 계산을 새로 만들지 않는다.** 판매가능량 · 신선도 · 회전 · FEFO ·
   Capacity 는 전부 기존 모듈이 정본이고, 여기가 하는 일은 두 가지뿐이다.

```text
① 행 목록을 내는 SELECT      기존에 "단건 조회"만 있던 자리 (목록 함수가 없었다)
② 그 결과를 화면 계약으로 조립  기존 함수 결과를 옮겨 담기만 한다
```

   ⚠️ 그래서 아래 SQL 어디에도 **판매가능량 공식이 없다.** 그 값은
      `tools.build_inventory_by_item` 하나가 만들고 이 파일은 받아 적는다.
      같은 계산을 SQL 로 한 벌 더 만들면 두 답이 갈리고, 갈린 날 어느 쪽이
      맞는지 아무도 말해 주지 않는다.

🔴 **상태 어휘를 새로 적지 않는다.** `_HOLDING_RESERVATION` · `_HOLDING_ALLOCATION` ·
   `_ASSIGNED_ALLOCATION` 은 `outbound.py` 에서 가져다 쓴다. 여기에 문자열로 다시
   적으면 한쪽만 고쳐지는 날이 온다.

🔴 **네 조회가 같은 시간축(`sim_run_id`, `as_of`)에 선다.** 화면이 고른 날짜의
   사실은 `historical_repository` 가 원장·사건에서 되살리고, 이 파일은
   **Current Cache 칸을 과거 값으로 읽지 않는다.**

```text
되살린다 (HISTORICAL_AS_OF)   Lot 잔량 · Lot 상태 · 신선도 · 회전 · used_capacity_kg
                              Receipt 상태 · 검수 · 재고반영 · Pallet 자리
지금 행 그대로 (CURRENT_ROW) 판매가능량(Runtime 축) · Zone 정원(되살릴 정본 없음)
```

   ⚠️ 뒤엣것들은 **되살릴 정본 컬럼이 아직 없다** (`inventory_reservations` 에
      시뮬레이션 날짜 컬럼 없음 · `released_as_of` 는 WP-3 · 자리 정원 이력 없음).
      없는 것을 지어내지 않고 응답의 `*_time_basis` 로 그 사실을 말한다.

★ **커넥션은 한 호출에 하나다.** 화면 한 판이 여러 커넥션에 걸치면 그 사이 원장이
  바뀌어 *"같은 as_of 인데 칸마다 다른 시점"* 이 성립한다. 다만
  `repository.get_current_logistics_read` 는 자기 커넥션을 여는 기존 구현이라
  그 부분만 예외다 — 이 파일이 그 규약을 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from psycopg import sql

from app.logistics import arrival, historical_repository, outbound, transport, warehouse
from app.logistics.console_schemas import (
    ConsoleAllocateResponse,
    ConsoleAllocation,
    ConsoleAllocationRequestItem,
    ConsoleArrivalSummary,
    ConsoleCapacity,
    ConsoleFefoCandidate,
    ConsoleFefoResponse,
    ConsoleFreeLocation,
    ConsoleInboundReceipt,
    ConsoleInboundResponse,
    ConsoleInTransitItem,
    ConsoleInventoryItem,
    ConsoleInventoryLot,
    ConsoleInventoryMove,
    ConsoleInventoryMovesResponse,
    ConsoleInventoryResponse,
    ConsoleLotLocation,
    ConsoleOutboundResponse,
    ConsolePlacementOptionsResponse,
    ConsolePlacementResponse,
    ConsolePlacementZone,
    ConsoleReleaseResponse,
    ConsoleReservation,
    ConsoleShipResponse,
    ConsoleTransportQuoteResponse,
    ConsoleWarehouseResponse,
    ConsoleZone,
)
from app.logistics.db import get_connection, get_db_schema
from app.logistics.historical_repository import HistoricalAllocation, HistoricalLot
from app.logistics.inbound_schedules import receivable_at
from app.logistics.outbound import (
    _ASSIGNED_ALLOCATION,
    _HOLDING_ALLOCATION,
    _HOLDING_RESERVATION,
    AllocationStatus,
    HumanAllocationBasis,
    ReservationStatus,
)
from app.logistics.repository import (
    LogisticsRead,
    get_active_logistics_policy,
    get_active_logistics_runtime_fixture,
    get_current_logistics_read,
)
from app.logistics.schemas import InventoryLogisticsSnapshot, LogisticsRuntimeFixture
from app.logistics.tools import build_inventory_by_item
from app.logistics.warehouse import _OCCUPYING_PALLET

__all__ = [
    "allocate_reservation",
    "get_inbound_console",
    "get_inventory_console",
    "get_inventory_moves_console",
    "get_outbound_console",
    "get_placement_options_console",
    "get_reservation_fefo_console",
    "get_transport_quote_console",
    "get_warehouse_console",
    "place_lot",
    "release_reservation_console",
    "ship_reservation",
]


# ── 커넥션 ──────────────────────────────────────────────────────────────


@contextmanager
def _read_connection() -> Iterator[Any]:
    """읽기 전용 커넥션 하나. **commit 하지 않는다.**"""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_connection() -> Iterator[Any]:
    """쓰기 커넥션 하나 — 성공하면 **한 번** commit, 실패하면 rollback.

    🔴 **도메인 함수 안의 잠금 · 검증 · 멱등 · 무결성 검사를 여기서 복제하지 않는다.**
       그것들은 이미 `outbound` · `warehouse` · `ledger` 안에 있고, 밖에서 한 벌 더
       두면 두 판정이 갈린다. 이 자리가 하는 일은 트랜잭션 경계 하나뿐이다.
    """
    conn = get_connection()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def _rows(conn: Any, query: sql.Composed, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _schema() -> sql.Identifier:
    return sql.Identifier(get_db_schema())


def _decimal(value: Any) -> Decimal:
    """숫자 칸을 Decimal 로. `None` 은 0 이 아니라 호출부가 따로 다룬다."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ── 공통 조회 ───────────────────────────────────────────────────────────


def _item_names(conn: Any) -> dict[str, str]:
    rows = _rows(conn, sql.SQL("SELECT item_id, item_name FROM {}.items").format(_schema()), [])
    return {row["item_id"]: row["item_name"] for row in rows}


def _mvp_item_ids(conn: Any) -> list[str]:
    """화면이 기본으로 보여 줄 품목. 재고가 0kg 이어도 칸은 서야 한다."""
    rows = _rows(
        conn,
        sql.SQL("SELECT item_id FROM {}.items WHERE mvp_active ORDER BY item_id").format(_schema()),
        [],
    )
    return [row["item_id"] for row in rows]


def _reservation_totals_by_item(conn: Any, *, sim_run_id: str) -> dict[str, dict[str, Any]]:
    """품목별 **지금 재고를 잡고 있는 양**. 🔴 예약 원래 확보량의 합이 아니다.

    ```text
    allocated_qty_kg             ALLOCATED · PICKED                아직 안 나간 몫
    unallocated_reserved_qty_kg  reserved − (ALLOCATED·PICKED·SHIPPED)   Lot 미지정 잔여
    reserved_qty_kg              위 둘의 합                        ★ 정의가 곧 항등식이다
    ```

    🔴 **`SUM(r.reserved_qty_kg)` 를 쓰지 않는다.** `ship_allocated_stock` 은 할당을
       `SHIPPED` 로 내리고 원장 OUT 을 내지만 **예약 행의 `reserved_qty_kg` 도
       `status` 도 건드리지 않는다.** 그래서 원래 확보량을 그대로 합하면
       *"500kg 전량 출고가 끝났는데 화면에는 아직 500kg 을 잡고 있다"* 가 된다.

    ```text
    예약 500 → 할당 500 → 전량 SHIPPED
      종전  reserved 500 · allocated 0 · unallocated 0   🔴 셋이 안 맞는다
      지금  reserved   0 · allocated 0 · unallocated 0
    ```

       ⚠️ **출고 쪽 상태 정책을 고쳐서 맞추지 않는다.** `ship_allocated_stock` 의
          예약 상태 처리는 그대로 두고, 여기서 *"지금 잡고 있는 양"* 을 바로 센다.
          이 함수는 읽기이고 저쪽은 장부다.

    ★ `active_reservation_count` 도 같은 눈이다 — 상태가 `ALLOCATED` 로 남아 있어도
      **잡고 있는 양이 0 이면 세지 않는다.** 상태 어휘가 아니라 수량이 기준이다.

    ⚠️ **`ConsoleReservation.reserved_qty_kg` 와 뜻이 다르다.** 저쪽은 예약 행에
       적힌 DB 값 그대로(그 예약이 확보했다고 적은 양)이고, 여기는 품목 축의
       *"지금 잡고 있는 양"* 이다. 한 화면에 나란히 두지 않는다.
    """
    schema = _schema()
    # ★ 두 파생식을 SELECT 에서 세 번 쓰므로 이름을 붙여 한 곳에서만 적는다.
    잡은할당 = sql.SQL("COALESCE(h.qty, 0)")
    미할당잔여 = sql.SQL("GREATEST(r.reserved_qty_kg - COALESCE(a.qty, 0), 0)")
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT r.item_id,
                   count(*) FILTER (WHERE {잡은할당} + {미할당잔여} > 0)::int
                       AS active_reservation_count,
                   COALESCE(SUM({잡은할당} + {미할당잔여}), 0) AS reserved_qty_kg,
                   COALESCE(SUM({잡은할당}), 0) AS allocated_qty_kg,
                   COALESCE(SUM({미할당잔여}), 0) AS unallocated_reserved_qty_kg
            FROM {schema}.inventory_reservations r
            LEFT JOIN (
                SELECT reservation_id, SUM(allocated_qty_kg) AS qty
                FROM {schema}.inventory_allocations
                WHERE status = ANY(%(holding_alloc)s)
                GROUP BY reservation_id
            ) h ON h.reservation_id = r.reservation_id
            LEFT JOIN (
                SELECT reservation_id, SUM(allocated_qty_kg) AS qty
                FROM {schema}.inventory_allocations
                WHERE status = ANY(%(assigned_alloc)s)
                GROUP BY reservation_id
            ) a ON a.reservation_id = r.reservation_id
            WHERE r.sim_run_id = %(sim)s
              AND r.status = ANY(%(holding_resv)s)
            GROUP BY r.item_id
            """
        ).format(schema=schema, 잡은할당=잡은할당, 미할당잔여=미할당잔여),
        {
            "sim": sim_run_id,
            "holding_alloc": sorted(_HOLDING_ALLOCATION),
            "assigned_alloc": sorted(_ASSIGNED_ALLOCATION),
            "holding_resv": sorted(_HOLDING_RESERVATION),
        },
    )
    return {row["item_id"]: row for row in rows}


# ── GET /logistics/inventory ────────────────────────────────────────────


def _available_unresolved_reason(snapshot: InventoryLogisticsSnapshot) -> str | None:
    """`build_inventory_by_item` 이 `None` 을 내는 경로와 **같은 순서로** 본다.

    순서가 계약이다 — 저쪽 guard 와 어긋나면 이유가 사실과 달라진다.

    🔴 **확정 출고 축 두 이유가 없어졌다 (WP-3).** 판매가능량이 그 축을 더 이상 안
       빼므로(`tools.build_inventory_by_item` 의 «차감 축은 한 벌») 그것을 못 읽었다는
       이유로 이 값을 못 낸다고 답할 수 없다.
    """
    if snapshot.outbound_commitments is None:
        return "OUTBOUND_COMMITMENTS_UNRESOLVED"
    return None


def _historical_lots(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[HistoricalLot, ...]:
    """그날 존재한 Lot 전부. **`historical_repository` 하나가 정본이다.**"""
    return historical_repository.lot_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)


def _console_lot(lot: HistoricalLot, names: dict[str, str]) -> ConsoleInventoryLot:
    """되살린 Lot 하나를 화면 계약으로 옮겨 담는다. **계산하지 않는다.**"""
    return ConsoleInventoryLot(
        lot_id=lot.lot_id,
        item_id=lot.item_id,
        item_name=lot.item_name or names.get(lot.item_id),
        grade=lot.grade,
        remaining_qty_kg=lot.remaining_qty_kg,
        received_at=lot.received_at,
        # 🔴 유도된 상태다 — `inventory_lots.status` 컬럼이 아니다.
        status=lot.state,
        storage_zone=lot.storage_zone,
        remaining_freshness_days=lot.turnover.remaining_freshness_days,
        remaining_turnover_days=lot.turnover.remaining_turnover_days,
        turnover_status=lot.turnover.turnover_status,
        sell_priority=lot.turnover.sell_priority,
        disposal_candidate=lot.turnover.disposal_candidate,
    )


def _runtime_read_or_none(*, sim_run_id: str, as_of: date) -> LogisticsRead | None:
    """그날의 Agent Runtime Snapshot. **없으면 `None` 이고 그것도 사실이다.**

    🔴 **부재(`LookupError`)만 삼킨다.** 활성 fixture 가 둘인 무결성 위반
       (`ValueError`)은 그대로 올려 보낸다 — 깨진 데이터가 *"데이터를 주세요"* 로
       둔갑하면 안 된다 (`repository` 의 같은 규율).

    ★ 이 값이 없어도 **재고 수량은 답한다.** 수량 정본은 원장이고 fixture 가 아니다.
      못 내는 것은 그 스냅샷의 확정 출고 축이 필요한 판매가능량뿐이다.
    """
    try:
        return get_current_logistics_read(as_of=as_of, sim_run_id=sim_run_id)
    except LookupError:
        return None


def _runtime_fixture_or_none(*, sim_run_id: str, as_of: date) -> LogisticsRuntimeFixture | None:
    """`_runtime_read_or_none` 과 같은 규율의 fixture 단독 조회."""
    try:
        return get_active_logistics_runtime_fixture(as_of=as_of, sim_run_id=sim_run_id)
    except LookupError:
        return None


def get_inventory_console(
    *, sim_run_id: str, as_of: date, item_id: str | None = None
) -> ConsoleInventoryResponse:
    """품목 카드 · Lot 목록 · 창고 kg Capacity 한 판. **기준일은 `as_of` 다.**

    ```text
    현재고 · Lot · Capacity 사용량   historical_repository.lot_state_at   ← as_of 원장
    신선도 · 회전                     turnover (같은 함수 · 잔량만 그 시점 값)
    Capacity 한도                     agent_policy_config (지금 활성 · capacity_basis 표기)
    판매가능량                        tools.build_inventory_by_item        ← 예약 축은 현재 행
    ```

    🔴 **`inventory_lots.remaining_qty_kg` 를 과거 잔량으로 쓰지 않는다.** 종전에는
       그 컬럼(`> 0`)으로 Lot 을 골라서, 실측 84 Lot 이 전부 잔량 0 이 된 뒤
       **모든 과거 날짜가 0 kg · 0 Lot** 으로 나왔다 (원장 복원값은 각각 294.4 ·
       806.4 · 806.4 · 6,452.4 kg).

    🔴 **만료 Lot 은 `on_hand_qty_kg` 와 `used_capacity_kg` 에 남고 판매가능량에서만
       빠진다.** 판매불가는 창고에서 사라진 것이 아니다.

    ⚠️ **`available_qty_kg` 는 아직 다른 시간축이다** (`available_qty_time_basis`).
       그 값은 **지금** 예약·할당을 뺀 Runtime 축이다. WP-3 이 예약 축의 시간
       정본을 세웠으니 되살릴 수는 있지만, 어느 화면 값을 과거로 옮길지는 별도
       결정이라 여기서 바꾸지 않았다 (예약 목록은 `get_outbound_console` 이
       `as_of` 로 낸다).

    ★ **보관정책이 없는 품목의 Lot 도 싣는다.** 종전 스냅샷 경로는
      `item_storage_policies` 를 `INNER JOIN` 해서 그런 Lot 을 통째로 떨어뜨렸다 —
      정책이 없다는 이유로 실물 재고를 조회에서 지우지 않는다.
    """
    read = _runtime_read_or_none(sim_run_id=sim_run_id, as_of=as_of)
    policy = read.policy if read is not None else get_active_logistics_policy()

    inventory_by_item = None if read is None else build_inventory_by_item(read.snapshot)
    if read is None:
        unresolved_reason: str | None = "RUNTIME_SNAPSHOT_UNAVAILABLE"
    elif inventory_by_item is None:
        unresolved_reason = _available_unresolved_reason(read.snapshot)
    else:
        unresolved_reason = None
    available_by_name = (
        None
        if inventory_by_item is None
        else {row.item: row.available_qty_kg for row in inventory_by_item}
    )

    with _read_connection() as conn:
        historical_lots = _historical_lots(conn, sim_run_id=sim_run_id, as_of=as_of)
        names = _item_names(conn)
        reservations = _reservation_totals_by_item(conn, sim_run_id=sim_run_id)
        mvp_items = _mvp_item_ids(conn)

    # ★ 창고 점유는 **그날 실재한 모든 Lot** 의 합이다 — 화면 필터보다 앞선다.
    used_capacity = sum((lot.remaining_qty_kg for lot in historical_lots), start=Decimal(0))

    # 🔴 «살아 있는 Lot» 만 목록에 싣는다 — 종전 화면과 같은 모집단이다.
    #    잔량 0 이 된 Lot 까지 늘어놓으면 84 줄이 되고 그날의 재고가 안 보인다.
    lots = [
        _console_lot(lot, names)
        for lot in historical_lots
        if lot.remaining_qty_kg > Decimal(0) and (item_id is None or lot.item_id == item_id)
    ]

    # 품목 축: 재고가 있는 품목 ∪ 예약이 있는 품목 ∪ mvp_active 품목.
    # ★ mvp_active 가 아니어도 실물이 있으면 싣는다 — 계약 밖 품목이라고 재고를 숨기지 않는다.
    item_ids = {lot.item_id for lot in lots} | set(reservations) | set(mvp_items)
    if item_id is not None:
        item_ids = item_ids & {item_id}

    items: list[ConsoleInventoryItem] = []
    for current in sorted(item_ids):
        item_lots = [lot for lot in lots if lot.item_id == current]
        expired = [
            lot for lot in item_lots if lot.disposal_candidate and lot.remaining_qty_kg > Decimal(0)
        ]
        totals = reservations.get(current, {})
        name = names.get(current, current)
        items.append(
            ConsoleInventoryItem(
                item_id=current,
                item_name=name,
                on_hand_qty_kg=sum((lot.remaining_qty_kg for lot in item_lots), start=Decimal(0)),
                # 못 읽은 축이 있으면 None 그대로 나간다. 0 으로 메우지 않는다.
                available_qty_kg=(
                    None if available_by_name is None else available_by_name.get(name, Decimal(0))
                ),
                reserved_qty_kg=_decimal(totals.get("reserved_qty_kg", 0)),
                allocated_qty_kg=_decimal(totals.get("allocated_qty_kg", 0)),
                unallocated_reserved_qty_kg=_decimal(totals.get("unallocated_reserved_qty_kg", 0)),
                active_reservation_count=int(totals.get("active_reservation_count", 0)),
                sell_priority_lot_count=sum(1 for lot in item_lots if lot.sell_priority),
                expired_lot_count=len(expired),
                expired_qty_kg=sum((lot.remaining_qty_kg for lot in expired), start=Decimal(0)),
                disposal_candidate_lot_count=len(expired),
            )
        )

    capacity = historical_repository.capacity_at(
        used_capacity_kg=used_capacity,
        guaranteed_capacity_kg=policy.guaranteed_capacity_kg,
        burst_capacity_kg=policy.burst_capacity_kg,
    )
    return ConsoleInventoryResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        items=items,
        lots=lots,
        capacity=ConsoleCapacity(
            used_capacity_kg=capacity.used_capacity_kg,
            guaranteed_capacity_kg=capacity.guaranteed_capacity_kg,
            burst_capacity_kg=capacity.burst_capacity_kg,
            capacity_basis=capacity.capacity_basis,  # type: ignore[arg-type]
        ),
        available_qty_unresolved_reason=unresolved_reason,  # type: ignore[arg-type]
    )


# ── GET /logistics/inventory/moves ──────────────────────────────────────


def get_inventory_moves_console(
    *,
    sim_run_id: str,
    lot_id: str | None = None,
    item_id: str | None = None,
    moved_from: date | None = None,
    moved_to: date | None = None,
    limit: int = 100,
) -> ConsoleInventoryMovesResponse:
    """원장 이력. **읽기만 한다 — 이 경로는 Move 를 만들지 않는다.**"""
    schema = _schema()
    with _read_connection() as conn:
        rows = _rows(
            conn,
            sql.SQL(
                """
                SELECT m.move_id, m.lot_id, l.item_id, i.item_name,
                       m.move_type, m.quantity_kg, m.moved_at,
                       m.reason_code, m.note, m.sale_item_id
                FROM {schema}.inventory_moves m
                LEFT JOIN {schema}.inventory_lots l ON l.lot_id = m.lot_id
                LEFT JOIN {schema}.items i ON i.item_id = l.item_id
                WHERE m.sim_run_id = %(sim)s
                  AND (%(lot_id)s::text IS NULL OR m.lot_id = %(lot_id)s)
                  AND (%(item_id)s::text IS NULL OR l.item_id = %(item_id)s)
                  AND (%(moved_from)s::date IS NULL OR m.moved_at >= %(moved_from)s)
                  AND (%(moved_to)s::date IS NULL OR m.moved_at <= %(moved_to)s)
                ORDER BY m.moved_at DESC, m.move_id DESC
                LIMIT %(limit)s
                """
            ).format(schema=schema),
            {
                "sim": sim_run_id,
                "lot_id": lot_id,
                "item_id": item_id,
                "moved_from": moved_from,
                "moved_to": moved_to,
                "limit": limit,
            },
        )
    return ConsoleInventoryMovesResponse(
        sim_run_id=sim_run_id,
        moves=[ConsoleInventoryMove(**row) for row in rows],
    )


# ── GET /logistics/inbound ──────────────────────────────────────────────


def _inbound_receipts(conn: Any, *, sim_run_id: str, as_of: date) -> list[ConsoleInboundReceipt]:
    """그날까지 도착한 Receipt. 🔴 **상태를 사건에서 유도한다.**

    ```text
    ARRIVED       arrived_at <= as_of
    INSPECTED     검수 inspected_at < (as_of+1) 00:00 KST
    PUTAWAY_DONE  그 Receipt 의 Lot 과 원장 IN 이 as_of 까지 있다
    ```

    🔴 **`receipt_status` 컬럼을 읽지 않는다.** 실측 4건이 전부 `PUTAWAY_DONE` 이라
       그대로 실으면 도착만 한 날에도 «입고 완료» 로 보인다.

    ⚠️ **NULL 수량을 0 으로 바꾸지 않는다** (DDL 주석).
    """
    return [
        ConsoleInboundReceipt(
            inbound_id=receipt.inbound_id,
            receipt_id=receipt.receipt_id,
            item_id=receipt.item_id,
            item_name=receipt.item_name,
            arrived_at=receipt.arrived_at,
            ordered_qty_kg=receipt.ordered_qty_kg,
            accepted_qty_kg=receipt.accepted_qty_kg,
            hold_qty_kg=receipt.hold_qty_kg,
            rejected_qty_kg=receipt.rejected_qty_kg,
            receipt_status=receipt.state,
            fact_source=receipt.fact_source,
            inspection_id=receipt.inspection_id,
            inspection_verdict=receipt.inspection_verdict,
            inspected_qty_kg=receipt.inspected_qty_kg,
            lot_id=receipt.lot_id,
            in_move_id=receipt.in_move_id,
            # Lot 과 원장 IN 이 **둘 다** 있어야 재고가 섰다고 본다.
            stock_applied=receipt.stock_applied,
        )
        for receipt in historical_repository.receipt_state_at(
            conn, sim_run_id=sim_run_id, as_of=as_of
        )
    ]


def get_inbound_console(*, sim_run_id: str, as_of: date) -> ConsoleInboundResponse:
    """운송 중 일정 · Receipt · 도착 자격 요약.

    ★ **운송 중 목록의 정본은 `inbound_schedules` 다 (W3-2).** fixture 는
      `in_transit_status`(`None` 미확인 / `[]` 0건 확인을 가르는 값)만 준다 —
      그 칸은 스냅샷 계약에 없어서 여기서 직접 읽는다.

    🔴 **두 목록의 종료조건이 다르다. 같은 목록을 두 번 쓰지 않는다.**

    ```text
    in_transit        Receipt 가 생기면 빠진다        "아직 창고에 안 온 것"
    arrival_summary   Lot + 원장 IN 이 서면 빠진다     "아직 받을 것이 남았나"
    ```

       ⚠️ 종전에는 둘 다 `fixture.in_transit` 하나를 봤다. 그대로 두면 **검수에서
          막힌 건(Receipt 만 있고 Lot 없음)이 도착 요약에서 사라져** 화면이
          *"오늘 받을 것이 없다"* 고 말한다 — 실제로는 이어받아야 할 건이다.

    🔴 **fixture 가 없는 날도 답한다.** 종전에는 `LookupError` 가 그대로 올라가
       화면 전체가 예시값으로 떨어졌다. 운송 중을 모르는 것과 Receipt 를 모르는
       것은 다른 사실이므로, 앞은 `UNRESOLVED` 로 적고 뒤는 그대로 되살린다.
    """
    fixture = _runtime_fixture_or_none(sim_run_id=sim_run_id, as_of=as_of)
    in_transit = None if fixture is None else fixture.in_transit

    with _read_connection() as conn:
        # ★ 도착 요약은 **받을 것이 남았나** 를 센다 — 운송 중 목록이 아니다.
        #   fixture 가 없는 날(미확인)에는 그 판정도 세울 수 없어 `None` 을 넘긴다.
        due_source = (
            None
            if fixture is None
            else receivable_at(conn, sim_run_id=sim_run_id, as_of=as_of)
        )
        selection = arrival.select_due_inbound(due_source, as_of=as_of)
        receipts = _inbound_receipts(conn, sim_run_id=sim_run_id, as_of=as_of)

    return ConsoleInboundResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        # fixture 가 없으면 «그날 운송 중 목록을 확인하지 못했다» 다 — 0건 확인이 아니다.
        in_transit_status=("UNRESOLVED" if fixture is None else fixture.in_transit_status),
        in_transit=(
            None
            if in_transit is None
            else [
                ConsoleInTransitItem(
                    inbound_id=item.inbound_id,
                    purchase_id=item.purchase_id,
                    item=item.item,
                    quantity_kg=item.quantity_kg,
                    expected_arrival_date=item.expected_arrival_date,
                )
                for item in in_transit
            ]
        ),
        receipts=receipts,
        arrival_summary=ConsoleArrivalSummary(
            source_status=selection.source_status,
            due_count=len(selection.due),
            blocked_count=len(selection.blocked),
            not_due_count=len(selection.not_due),
            unresolved_count=len(selection.unresolved),
            overdue_count=selection.overdue_count,
        ),
    )


# ── GET /logistics/warehouse ────────────────────────────────────────────


def _zones(conn: Any) -> list[ConsoleZone]:
    """Zone 자리 사정. 🔴 **정원·점유·여유는 `warehouse.get_zone_capacity` 가 낸다.**

    ```text
    이 함수의 SQL    zone_id · zone_code · zone_name · zone_kind · purpose   이름표뿐
    get_zone_capacity  total · occupied · free                                셈은 저쪽
    ```

    🔴 **같은 셈을 여기 한 벌 더 두지 않는다.** 종전에는 정원·점유를 직접 세면서
       *"점유 > 정원이어도 `free_positions` 를 음수로 내보낸다"* 는 Console 전용
       정책까지 들고 있었다. 그 순간 창고 무결성 판정이 두 벌이 되고, 도메인이
       **오류로 막는 상태를 화면만 정상 응답으로 통과**시킨다.

    ⚠️ **깨진 Zone 이 있으면 이 조회도 멈춘다** (`WarehouseIntegrityError` → 409).
       화면 한 판을 못 보는 대신, 도메인과 같은 사실을 본다. 그것이 계약이다.

    ★ Zone 수가 적어(실측 5) Zone 마다 한 번 부르는 구조로 충분하다. 성능을 이유로
      Capacity 판정을 새로 만들지 않는다.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT z.zone_id, z.zone_code, z.zone_name, z.zone_kind, z.purpose
            FROM {schema}.warehouse_zones z
            WHERE z.is_active
            ORDER BY z.zone_kind, z.zone_id
            """
        ).format(schema=schema),
        [],
    )
    zones: list[ConsoleZone] = []
    for row in rows:
        capacity = warehouse.get_zone_capacity(conn, zone_id=row["zone_id"])
        zones.append(
            ConsoleZone(
                zone_id=row["zone_id"],
                zone_code=row["zone_code"],
                zone_name=row["zone_name"],
                # ★ zone_kind 도 저쪽이 읽은 값을 쓴다 — 한 Zone 을 두 번 읽고
                #   서로 다른 값을 싣는 일이 없게 한다.
                zone_kind=capacity.zone_kind,
                purpose=row["purpose"],
                total_positions=capacity.total_positions,
                occupied_positions=capacity.occupied_positions,
                free_positions=capacity.free_positions,
            )
        )
    return zones


def _lot_locations(conn: Any, *, sim_run_id: str, as_of: date) -> list[ConsoleLotLocation]:
    """그날 Lot 이 어디 있었나. 🔴 **`pallet_events` 재생 결과다.**

    ```text
    싣는다   그날 원장 잔량 > 0                     그날의 실재 재고
             잔량 0 인데 Pallet 이 자리를 차지함     자리는 아직 안 돌아왔다
    안 싣는다 잔량 0 이고 자리도 안 잡고 있음
    ```

    🔴 **`pallets.current_location_id` 를 과거 위치로 쓰지 않는다.** 실측 3장은
       전부 2026-09-12 에 `EMPTIED` 되어 지금 자리가 없다 — 그 값을 2026-01 화면에
       실으면 그때도 자리가 없었던 것으로 보인다.

    🔴 **잔량 0 이라고 자리가 도는 것이 아니다.** 원장 `OUT` · `DISPOSE` 는 수량만
       줄이고 Pallet 을 비우지 않는다 — 자리 반환은 `warehouse.empty_pallet` 이
       따로 해야 하는 별개 사실이다.

    ⚠️ **`UNRECORDED` 와 `UNPLACED` 는 다르다.** 앞은 그날까지 그 Lot 의 Pallet
       사건이 하나도 없다는 뜻(모른다)이고, 뒤는 사건은 있는데 자리를 안 잡고
       있었다는 뜻(확인했고 없다)이다. 없는 자리를 지어내지 않는다.
    """
    lots = historical_repository.lot_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    positions = historical_repository.pallet_position_at(
        conn, sim_run_id=sim_run_id, as_of=as_of
    )
    by_lot: dict[str, list[Any]] = {}
    for position in positions:
        by_lot.setdefault(position.lot_id, []).append(position)

    rows: list[ConsoleLotLocation] = []
    for lot in sorted(lots, key=lambda row: row.lot_id):
        recorded = sorted(by_lot.get(lot.lot_id, []), key=lambda row: row.pallet_id)
        occupying = [position for position in recorded if position.occupies_position]
        if occupying:
            rows.extend(
                ConsoleLotLocation(
                    lot_id=lot.lot_id,
                    item_id=lot.item_id,
                    item_name=lot.item_name,
                    remaining_qty_kg=lot.remaining_qty_kg,
                    pallet_id=position.pallet_id,
                    # 🔴 Pallet 상태는 사건으로 유도되지 않는다 (계약 주석 참조).
                    pallet_status=None,
                    zone_id=position.zone_id,
                    location_id=position.location_id,
                    placement="PLACED",
                )
                for position in occupying
            )
            continue
        if lot.remaining_qty_kg <= Decimal(0):
            continue
        rows.append(
            ConsoleLotLocation(
                lot_id=lot.lot_id,
                item_id=lot.item_id,
                item_name=lot.item_name,
                remaining_qty_kg=lot.remaining_qty_kg,
                pallet_id=None,
                pallet_status=None,
                zone_id=None,
                location_id=None,
                placement=("UNPLACED" if recorded else "UNRECORDED"),
            )
        )
    return rows


def get_warehouse_console(*, sim_run_id: str, as_of: date) -> ConsoleWarehouseResponse:
    """Zone 자리 사정과 Lot 물리 위치. 🔴 **단위는 Pallet Position 이다 (kg 아님).**

    ⚠️ **두 목록의 시간축이 다르다** — 응답이 그것을 말한다.

    ```text
    lot_locations  HISTORICAL_AS_OF   pallet_events 재생
    zones          CURRENT_ROW        지금 창고의 자리 정원 · 점유
    ```

       자리 정원(`storage_locations` · `warehouse_zones`)에 유효일이 없어 그날의
       정원을 알 수 없다. **정원 이력 표를 새로 만들지 않는다** (`07 §15`) —
       그 셈의 주인은 `warehouse.get_zone_capacity` 하나이며 여기서 복제하지 않는다.
    """
    with _read_connection() as conn:
        return ConsoleWarehouseResponse(
            sim_run_id=sim_run_id,
            as_of=as_of,
            zones=_zones(conn),
            lot_locations=_lot_locations(conn, sim_run_id=sim_run_id, as_of=as_of),
        )


def get_placement_options_console(
    *, sim_run_id: str, lot_id: str
) -> ConsolePlacementOptionsResponse:
    """사람이 자리를 고를 때 보는 것. 🔴 **추천도 자동선택도 하지 않는다.**

    ⚠️ 이 품목의 Zone 정책이 **아예 없으면** `UNRESOLVED` 로 답하고 목록을 비운다 —
       *"전부 금지"* 가 아니라 *"먼저 정책을 정해야 한다"* 는 뜻이다
       (`warehouse._zone_allowed` 가 두 상태를 가르는 것과 같은 규율이고,
       실제로 그 상태에서 `place_lot_on_pallet` 은 `ZonePolicyUnresolved` 로 멈춘다).
    """
    schema = _schema()
    with _read_connection() as conn:
        found = _rows(
            conn,
            sql.SQL(
                """
                SELECT lot_id, item_id FROM {}.inventory_lots
                WHERE lot_id = %s AND sim_run_id = %s
                """
            ).format(schema),
            [lot_id, sim_run_id],
        )
        if not found:
            raise LookupError(f"없는 Lot 이다 (sim_run_id={sim_run_id!r}, lot_id={lot_id!r})")
        item_id = found[0]["item_id"]

        zone_rows = _rows(
            conn,
            sql.SQL(
                """
                SELECT za.zone_id, z.zone_name, za.allowed, za.is_default
                FROM {schema}.item_zone_assignments za
                JOIN {schema}.warehouse_zones z ON z.zone_id = za.zone_id
                WHERE za.item_id = %s
                ORDER BY za.is_default DESC, za.allowed DESC, za.zone_id
                """
            ).format(schema=schema),
            [item_id],
        )
        zones = [ConsolePlacementZone(**row) for row in zone_rows]
        allowed_zones = [zone.zone_id for zone in zones if zone.allowed]

        free_locations: list[ConsoleFreeLocation] = []
        if allowed_zones:
            location_rows = _rows(
                conn,
                sql.SQL(
                    """
                    SELECT sl.zone_id, sl.location_id, sl.location_kind,
                           sl.lane_code, sl.rack_code, sl.bay_code,
                           sl.level_no, sl.position_no
                    FROM {schema}.storage_locations sl
                    LEFT JOIN {schema}.pallets p
                           ON p.current_location_id = sl.location_id
                          AND p.status = ANY(%(occupying)s)
                    WHERE sl.is_active
                      AND p.pallet_id IS NULL
                      AND sl.zone_id = ANY(%(zones)s)
                    ORDER BY sl.zone_id, sl.location_id
                    """
                ).format(schema=schema),
                {"occupying": sorted(_OCCUPYING_PALLET), "zones": allowed_zones},
            )
            free_locations = [ConsoleFreeLocation(**row) for row in location_rows]

    return ConsolePlacementOptionsResponse(
        sim_run_id=sim_run_id,
        lot_id=lot_id,
        item_id=item_id,
        zone_policy_status="CONFIRMED" if zone_rows else "UNRESOLVED",
        zones=zones,
        free_locations=free_locations,
    )


# ── GET /logistics/outbound ─────────────────────────────────────────────


#: 유도된 할당 상태를 화면 어휘로 옮긴다. 🔴 **새 어휘를 만들지 않는다** —
#: `AllocationStatus` 는 DB `ck_inventory_allocations_status` 그대로이고, 놓아준
#: 예약의 할당은 DB 에서도 실제로 `CANCELLED` 로 내려간다
#: (`outbound.release_reservation`).
_ALLOCATION_STATE_TO_CONSOLE: dict[str, AllocationStatus] = {
    "ALLOCATED": "ALLOCATED",
    "SHIPPED": "SHIPPED",
    "RELEASED": "CANCELLED",
}


def _console_allocation(allocation: HistoricalAllocation) -> ConsoleAllocation:
    """`as_of` 시점 할당 하나를 화면 계약으로. **상태는 유도값이다.**"""
    return ConsoleAllocation(
        allocation_id=allocation.allocation_id,
        lot_id=allocation.lot_id,
        pallet_id=allocation.pallet_id,
        allocated_qty_kg=allocation.allocated_qty_kg,
        allocation_basis=allocation.allocation_basis,
        decided_by=allocation.decided_by,
        decided_at=allocation.decided_at,
        status=_ALLOCATION_STATE_TO_CONSOLE[allocation.state],
        note=allocation.note,
    )


def get_outbound_console(
    *, sim_run_id: str, as_of: date, status: ReservationStatus | None = None
) -> ConsoleOutboundResponse:
    """`as_of` 시점의 예약 목록과 그 아래 할당들. **네 조회와 같은 축이다.**

    ```text
    예약 존재    sales.sale_date <= as_of
    예약 소멸    released_as_of <= as_of
    할당 존재    decided_at < timestamp_cutoff(as_of)
    출고         MOVE-OUT-{allocation_id} · moved_at <= as_of
    ```

    🔴 **저장된 `status` 두 칸을 안 읽는다** — `historical_repository.reservation_state_at`
       하나가 정본이고 이 파일은 받아 적는다. 종전에는 `inventory_reservations` ·
       `inventory_allocations` 의 지금 행을 그대로 내고 `reservation_time_basis` 로
       *"과거가 아니다"* 라고만 말했다 (WP-3 이전에는 자를 정본이 없었다).

    ★ **`status` 필터도 유도된 상태에 건다.** 지금 DB 값으로 거르면 **그날 살아 있던
      예약이 오늘 놓아줬다는 이유로 과거 화면에서 사라진다.** DB 에 거는 `WHERE` 를
      쓰지 않고 유도 뒤에 파이썬에서 거른다 — 유도식의 주인이 하나여야 하기 때문이다.

    ★ `status` 를 안 주면 **거르지 않는다** — 놓아준 예약(RELEASED · CANCELLED)을
      기본으로 숨기는 정책을 여기서 새로 만들지 않는다. 화면이 골라 쓴다.

    ★ 0건이면 `reservations: []` 가 정상이다. 더미를 만들지 않는다.
    """
    with _read_connection() as conn:
        reservations = historical_repository.reservation_state_at(
            conn, sim_run_id=sim_run_id, as_of=as_of
        )

    return ConsoleOutboundResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        reservations=[
            ConsoleReservation(
                reservation_id=row.reservation_id,
                item_id=row.item_id,
                item_name=row.item_name,
                sale_id=row.sale_id,
                required_qty_kg=row.required_qty_kg,
                reserved_qty_kg=row.reserved_qty_kg,
                allocated_qty_kg=row.allocated_qty_kg,
                unallocated_qty_kg=row.unallocated_qty_kg,
                due_date=row.due_date,
                status=cast(ReservationStatus, row.status),
                allocations=[_console_allocation(a) for a in row.allocations],
            )
            for row in reservations
            if status is None or row.status == status
        ],
    )


def _reservation_axis(conn: Any, *, reservation_id: str) -> dict[str, Any]:
    """예약 하나의 실행 축과 잔여량. 🔴 **호출자가 sim_run_id 를 지어내지 않게 한다.**"""
    schema = _schema()
    found = _rows(
        conn,
        sql.SQL(
            """
            SELECT r.reservation_id, r.sim_run_id, r.item_id, r.reserved_qty_kg,
                   GREATEST(r.reserved_qty_kg - COALESCE(a.qty, 0), 0)
                       AS remaining_reservation_qty_kg
            FROM {schema}.inventory_reservations r
            LEFT JOIN (
                SELECT reservation_id, SUM(allocated_qty_kg) AS qty
                FROM {schema}.inventory_allocations
                WHERE status = ANY(%(assigned_alloc)s)
                GROUP BY reservation_id
            ) a ON a.reservation_id = r.reservation_id
            WHERE r.reservation_id = %(reservation_id)s
            """
        ).format(schema=schema),
        {"reservation_id": reservation_id, "assigned_alloc": sorted(_ASSIGNED_ALLOCATION)},
    )
    if not found:
        raise LookupError(f"없는 예약이다: {reservation_id!r}")
    return found[0]


def get_reservation_fefo_console(*, reservation_id: str, as_of: date) -> ConsoleFefoResponse:
    """이 예약에 쓸 FEFO 후보. 🔴 **추천만 한다 — 고르지도 쓰지도 않는다.**

    실행 축(`sim_run_id`)과 품목은 예약 행에서 읽는다. 호출자가 넘기게 하면
    남의 실행 Lot 을 이 예약에 붙일 수 있다.
    """
    with _read_connection() as conn:
        axis = _reservation_axis(conn, reservation_id=reservation_id)
        candidates = outbound.recommend_fefo_candidates(
            conn, sim_run_id=axis["sim_run_id"], item_id=axis["item_id"], as_of=as_of
        )

    return ConsoleFefoResponse(
        reservation_id=reservation_id,
        sim_run_id=axis["sim_run_id"],
        item_id=axis["item_id"],
        remaining_reservation_qty_kg=_decimal(axis["remaining_reservation_qty_kg"]),
        candidates=[
            ConsoleFefoCandidate(
                lot_id=candidate.lot_id,
                available_qty_kg=candidate.available_qty_kg,
                remaining_freshness_days=candidate.remaining_freshness_days,
                received_at=candidate.received_at,
                grade=candidate.grade,
            )
            for candidate in candidates
        ],
    )


# ── GET /logistics/transport/quote ──────────────────────────────────────


def get_transport_quote_console(
    *,
    shipment_qty_kg: Decimal,
    logistics_contract_id: str | None = None,
    body_type: str | None = None,
) -> ConsoleTransportQuoteResponse:
    """운송 견적. 🔴 **재고도 원장도 건드리지 않는다 — 읽기와 계산뿐이다.**"""
    with _read_connection() as conn:
        plan = transport.plan_fixed_route_transport(
            conn,
            shipment_qty_kg=shipment_qty_kg,
            logistics_contract_id=logistics_contract_id,
            body_type=body_type,
        )
    return ConsoleTransportQuoteResponse(
        logistics_contract_id=plan.logistics_contract_id,
        distance_km=plan.distance_km,
        vehicle_class=plan.vehicle_class,
        body_type=plan.body_type,
        vehicle_operational_payload_kg=plan.vehicle_operational_payload_kg,
        shipment_qty_kg=plan.shipment_qty_kg,
        trip_count=plan.trip_count,
        fixed_fee_per_trip_krw=plan.fixed_fee_per_trip_krw,
        estimated_cost_krw=plan.estimated_cost_krw,
        # 정본이 스키마에 없다. 항상 None 이고 여기서 지어내지 않는다.
        standard_minutes=plan.standard_minutes,
        contract_baseline_cost_krw=plan.contract_baseline_cost_krw,
        contract_vehicle_class=plan.contract_vehicle_class,
    )


# ── Command ─────────────────────────────────────────────────────────────


def place_lot(
    *,
    pallet_id: str,
    sim_run_id: str,
    lot_id: str,
    location_id: str,
    occurred_at: datetime,
    recorded_by: str,
    packaging_spec_id: str | None = None,
    note: str | None = None,
) -> ConsolePlacementResponse:
    """Pallet 배치. Zone · 자리 수 검사는 **도메인 함수 안에 있다.**"""
    with _write_connection() as conn:
        result = warehouse.place_lot_on_pallet(
            conn,
            pallet_id=pallet_id,
            sim_run_id=sim_run_id,
            lot_id=lot_id,
            location_id=location_id,
            occurred_at=occurred_at,
            recorded_by=recorded_by,
            packaging_spec_id=packaging_spec_id,
            note=note,
        )
    return ConsolePlacementResponse(
        applied=result.applied,
        pallet_id=result.pallet_id,
        location_id=result.location_id,
        zone_id=result.zone_id,
        status=result.status,
    )


def allocate_reservation(
    *,
    reservation_id: str,
    requests: Sequence[ConsoleAllocationRequestItem],
    decided_by: str,
    decided_at: datetime,
    allocation_basis: HumanAllocationBasis,
    as_of: date,
) -> ConsoleAllocateResponse:
    """사람이 고른 Lot 으로 할당을 확정한다. 🔴 **원장 OUT 은 나가지 않는다.**

    🔴 **`HumanAllocationBasis` 만 받는다.** 이 문은 사람의 것이고
       `FEFO_AUTO_SELECTED` 는 자동 경로가 스스로 적는 값이다 — 사람이 그 값을 넣으면
       하지 않은 일이 장부에 선다.

       ⚠️ **`outbound.allocate_stock` 의 타입은 안 좁힌다.** 그 코어는 사람 경로와
          자동 경로가 함께 쓰는 자리라 셋을 다 받아야 한다. 좁히는 것은 **이 입구**다.
    """
    with _write_connection() as conn:
        result = outbound.allocate_stock(
            conn,
            reservation_id=reservation_id,
            requests=[
                outbound.AllocationRequest(lot_id=item.lot_id, quantity_kg=item.quantity_kg)
                for item in requests
            ],
            decided_by=decided_by,
            decided_at=decided_at,
            allocation_basis=allocation_basis,
            as_of=as_of,
        )
    return ConsoleAllocateResponse(
        applied=result.applied,
        reservation_id=reservation_id,
        allocation_ids=list(result.allocation_ids),
        reservation_status=result.reservation_status,
        allocated_qty_kg=result.allocated_qty_kg,
    )


def ship_reservation(
    *, reservation_id: str, shipped_at: date, sale_item_id: str | None = None
) -> ConsoleShipResponse:
    """실출고. 🔴 **여기서만 원장 OUT 이 나가고 잔량이 준다.**

    ⚠️ `remaining_qty_kg` UPDATE 를 이 경로가 따로 쓰지 않는다 — 잔량을 바꾸는 것은
       `ledger` 하나이고 `ship_allocated_stock` 이 그것을 부른다.
    """
    with _write_connection() as conn:
        result = outbound.ship_allocated_stock(
            conn,
            reservation_id=reservation_id,
            shipped_at=shipped_at,
            sale_item_id=sale_item_id,
        )
    return ConsoleShipResponse(
        applied=result.applied,
        reservation_id=reservation_id,
        shipped_allocation_ids=list(result.shipped_allocation_ids),
        move_ids=list(result.move_ids),
        shipped_qty_kg=result.shipped_qty_kg,
    )


def release_reservation_console(
    *, reservation_id: str, released_as_of: date, status: ReservationStatus
) -> ConsoleReleaseResponse:
    """예약을 **그날부터** 놓아준다. 이미 `SHIPPED` 인 할당이 있으면 도메인이 막는다.

    🔴 **`released_as_of` 를 여기서 만들지 않는다** (WP-3 M3). 사람이 콘솔에서 어느
       날짜의 사실로 놓아주는지 말해야 한다 — 서버 시계로 채우면 그 값이 시뮬레이션
       날짜 행세를 하고 Historical 이 그것을 그대로 믿는다.
    """
    with _write_connection() as conn:
        result = outbound.release_reservation(
            conn, reservation_id=reservation_id, released_as_of=released_as_of, status=status
        )
    return ConsoleReleaseResponse(
        applied=result.applied,
        reservation_id=result.reservation_id,
        status=result.status,
        required_qty_kg=result.required_qty_kg,
    )
