"""재고·물류 화면 조회 Service — 도메인 함수를 **조립만** 한다.

🔴 **자리는 `app/logistics` 이고, 부르는 곳은 `app/api/logistics/query.py` 하나다**
   (2026-09-15 · 물류 문서 28). 화면 HTTP 경계는 `app/api/logistics` 이고 이 파일은
   그 화면이 읽는 조회를 도메인 쪽에서 조립한다.

```text
app/api/logistics    화면 전용 — routes(HTTP) · query(탭 조립) · schema(LogisticsTab)
app/logistics        물류 도메인 · Agent · 그리고 이 조회 조립
```

   ⚠️ **의존은 한 방향이다.** `app/api` 가 여기를 부르는 것은 정상이고,
      `app/logistics` 가 `app/api` 를 부르는 것은 **없다.**

🔴 **이 파일은 업무 계산을 새로 만들지 않는다.** 판매가능량 · 신선도 · 회전 · FEFO ·
   Capacity 는 전부 도메인 모듈이 정본이고, 여기가 하는 일은 두 가지뿐이다.

```text
① 행 목록을 내는 SELECT      기존에 "단건 조회"만 있던 자리 (목록 함수가 없었다)
② 그 결과를 화면 계약으로 조립  기존 함수 결과를 옮겨 담기만 한다
```

   ⚠️ 그래서 아래 SQL 어디에도 **판매가능량 공식이 없다.** 그 값은
      `tools.build_inventory_by_item` 하나가 만들고 이 파일은 받아 적는다.
      같은 계산을 SQL 로 한 벌 더 만들면 두 답이 갈리고, 갈린 날 어느 쪽이
      맞는지 아무도 말해 주지 않는다.

🔴 **예약·할당 축은 `historical_repository.reservation_state_at` 하나가 정본이다** (#760).
   예약 3칸(reserved · allocated · unallocated)도 판매가능량도 그 `as_of` 결과에서
   나온다 — 이 파일은 현재 status 를 다시 세지 않는다(종전 `_reservation_totals_by_item`
   현재-축 집계는 제거). 상태 어휘를 문자열로 다시 적으면 한쪽만 고쳐지는 날이 온다.

🔴 **네 조회가 같은 시간축(`sim_run_id`, `as_of`)에 선다.** 화면이 고른 날짜의
   사실은 `historical_repository` 가 원장·사건에서 되살리고, 이 파일은
   **Current Cache 칸을 과거 값으로 읽지 않는다.**

```text
되살린다 (HISTORICAL_AS_OF)   Lot 잔량 · Lot 상태 · 신선도 · 회전 · used_capacity_kg
                              Receipt 상태 · 검수 · 재고반영 · Pallet 자리
                              예약 3칸(reserved · allocated · unallocated) · 판매가능량 (#760)
지금 행 그대로 (CURRENT_ROW) Zone 정원(되살릴 정본 없음)
```

   ⚠️ 예약·할당·판매가능량은 이제 `reservation_state_at` · `lot_state_at` 의 `as_of`
      결과로 되살린다 (#760 · 종전엔 «되살릴 정본 없음» 이었으나 WP-3 이 예약 시간축
      정본을 세웠다). Zone 정원만 되살릴 정본 컬럼이 없어(자리 정원 이력 없음) 지금
      값을 쓰고, 응답의 `*_time_basis` 로 그 사실을 말한다.

🔴 **커넥션은 화면 한 판에 하나이고, 이 파일은 열지 않는다** (2026-09-15).
   `build_result` 가 하나를 열어 `conn=` 으로 넘기고, 여기 함수 넷과 `load_console_runtime`
   은 그것을 빌려 쓴다. 종전에는 콘솔 호출마다 · FEFO 예약마다 · repository 읽기마다
   커넥션을 새로 열어 **한 판에 23개 · 388 ms**(원격 DB · 연결당 14~22 ms)였다.
   커넥션 재사용이 줄이는 것은 **연결 비용과 중복 조회**다.
   `repository` 의 읽기 함수들도 같은 `conn` 을 받는다 (`get_current_logistics_read(conn=)`).

   ⚠️ **커넥션 하나가 «모든 SELECT 가 같은 시점» 을 보장하지는 않는다.** `get_connection`
      은 격리수준을 안 정해 PostgreSQL 기본값 `READ COMMITTED` 로 돈다 — 같은 트랜잭션
      안이라도 SELECT 는 문장마다 새 스냅샷을 잡아, 사이에 다른 커밋이 있으면 두 조회가
      다른 값을 볼 수 있다. 화면이 «같은 as_of» 로 서는 근거는 커넥션이 아니라 **각 SQL 의
      `as_of` cutoff**(`historical_repository`)다. 조회 원자성이 필요하면 `REPEATABLE READ`
      가 있어야 하고, 그것은 이 작업의 범위가 아니다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, cast

from psycopg import sql

from app.logistics import arrival, historical_repository, outbound
from app.logistics.db import get_db_schema
from app.logistics.historical_repository import (
    HistoricalAllocation,
    HistoricalLot,
    HistoricalReservation,
)
from app.logistics.inbound_schedules import receivable_at, receivable_from
from app.logistics.outbound import (
    AllocationStatus,
    ReservationStatus,
)
from app.logistics.repository import (
    LogisticsRead,
    get_active_logistics_policy,
    get_current_logistics_read,
)
from app.logistics.schemas import (
    ConsoleAllocation,
    ConsoleArrivalSummary,
    ConsoleCapacity,
    ConsoleFefoCandidate,
    ConsoleInboundReceipt,
    ConsoleInboundResponse,
    ConsoleInTransitItem,
    ConsoleInventoryItem,
    ConsoleInventoryLot,
    ConsoleInventoryResponse,
    ConsoleOutboundResponse,
    ConsoleReservation,
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    OutboundCommitment,
)
from app.logistics.tools import build_inventory_by_item

#: 🔴 **화면(`app/api/logistics/query.py`)이 부르는 넷뿐이다** (2026-09-15).
#:
#:    종전에는 여기에 창고 조회 · 재고이동 조회 · 배치 후보 · 운송 견적과 쓰기 넷
#:    (`place_lot` · `allocate_reservation` · `ship_reservation` ·
#:    `release_reservation_console`)이 더 있었다. 그것들을 부르는 자리는
#:    `app/logistics/router.py` 하나였고, 그 라우터를 **화면도 마스터도 안 불렀다** —
#:    화면은 `/api/logistics`, 마스터는 `adapter.logistics_port` 를 파이썬으로 쓴다.
#:
#:    ⚠️ **쓰기 넷은 여기서 만든 것이 아니라 도메인 함수를 감싼 것이었다.** 정본은
#:       `outbound.allocate_stock` · `ship_allocated_stock` · `release_reservation` ·
#:       `warehouse.place_lot` 이고 그대로 있다 — 없앤 것은 감싼 껍질뿐이다.
__all__ = [
    "get_fefo_candidates_by_item",
    "get_inbound_console",
    "get_inventory_console",
    "get_outbound_console",
    "load_console_runtime",
]


# ── 공통 ────────────────────────────────────────────────────────────────


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


def _reservation_totals_from_history(
    reservations: Sequence[HistoricalReservation],
) -> dict[str, dict[str, Any]]:
    """품목별 **그날 재고를 잡고 있던 양** — Historical 예약 reader 결과를 집계한다 (#760).

    ★ 의미는 종전 `_reservation_totals_by_item`(현재-status 축)과 **같다** — «그 시점에
      실제로 재고를 잡고 있는 양»(holding). 다른 것은 시간축뿐이라, 이제 `as_of` 로
      되살린 `reservation_state_at` 결과를 쓴다.

    ```text
    allocated_qty_kg             Σ 그날 ALLOCATED 할당           SHIPPED·RELEASED 제외
    unallocated_reserved_qty_kg  Σ 그날 미할당 잔여              놓아준 날부터 0
    reserved_qty_kg              위 둘의 합                      ★ 정의가 곧 항등식이다
    active_reservation_count     위 합이 0 보다 큰 예약 수
    ```

    🔴 **`HistoricalReservation.reserved_qty_kg`(원래 확보량)를 그대로 합하지 않는다.**
       그건 «잡고 있는 양» 이 아니라 «확보했던 양» 이라 의미가 다르다 —
       `ConsoleReservation.reserved_qty_kg`(행값)와 이 품목 축 값을 섞지 않는 것과
       같은 규율이다.

    ⚠️ **cross-day top-up 한계(LOG-HIST-001)는 이 함수가 고치지 않는다.** `reserved`
       행값이 다른 날 채워졌으면 그날 `unallocated` 복원은 그만큼 부정확할 수 있다 —
       실측 0건이고, 이 이슈(#760)의 범위 밖이다.
    """
    totals: dict[str, dict[str, Any]] = {}
    for resv in reservations:
        holding = resv.allocated_qty_kg + resv.unallocated_qty_kg
        bucket = totals.setdefault(
            resv.item_id,
            {
                "reserved_qty_kg": Decimal(0),
                "allocated_qty_kg": Decimal(0),
                "unallocated_reserved_qty_kg": Decimal(0),
                "active_reservation_count": 0,
            },
        )
        bucket["allocated_qty_kg"] += resv.allocated_qty_kg
        bucket["unallocated_reserved_qty_kg"] += resv.unallocated_qty_kg
        bucket["reserved_qty_kg"] += holding
        if holding > Decimal(0):
            bucket["active_reservation_count"] += 1
    return totals


def _historical_commitments(
    reservations: Sequence[HistoricalReservation],
) -> list[OutboundCommitment]:
    """그날 출고가 이미 잡아 둔 몫 — Historical 예약에서 조립한다 (#760).

    `repository.get_outbound_commitments`(현재축)와 **같은 의미**다. 시간축만 `as_of` 다.

    ```text
    lot_id 있음   그날 살아있는 할당(state == ALLOCATED)      SHIPPED·RELEASED 제외
    lot_id 없음   그날 미할당 예약 잔여(unallocated_qty_kg)   놓아준 날부터 0
    ```

    🔴 **SHIPPED 는 넣지 않는다.** 나간 몫은 원장 OUT 이 `remaining_qty_kg` 에서 이미
       덜어냈다 — 다시 빼면 이중 차감이다 (`OutboundCommitment` 규율).

    ★ **품목 키는 `item_name` 이다** — Lot 축(`_historical_availability_snapshot`)이
      같은 키를 써야 `build_inventory_by_item` 의 품목 차감이 맞는다.
    """
    out: list[OutboundCommitment] = []
    for resv in reservations:
        item = resv.item_name or resv.item_id
        for alloc in resv.allocations:
            if alloc.state == "ALLOCATED" and alloc.allocated_qty_kg > Decimal(0):
                out.append(
                    OutboundCommitment(
                        item=item, lot_id=alloc.lot_id, quantity_kg=alloc.allocated_qty_kg
                    )
                )
        if resv.unallocated_qty_kg > Decimal(0):
            out.append(
                OutboundCommitment(item=item, lot_id=None, quantity_kg=resv.unallocated_qty_kg)
            )
    return out


def _historical_availability_snapshot(
    base: InventoryLogisticsSnapshot,
    *,
    lots: Sequence[HistoricalLot],
    reservations: Sequence[HistoricalReservation],
    used_capacity_kg: Decimal,
) -> InventoryLogisticsSnapshot:
    """판매가능량 정본(`tools.build_inventory_by_item`)에 먹일 **그날** 스냅샷 (#760).

    ★ 점유(Lot)축과 예약·할당(commitments)축만 `as_of` 값으로 바꾼다 — 나머지(정책·
      용량 한도)는 `agent.tools._as_of_snapshot` 과 같은 규율로 base 를 그대로 둔다.
      `build_inventory_by_item` 은 이 두 축만 읽는다.

    🔴 **판매가능량 공식을 여기서 다시 쓰지 않는다.** 축만 그날 값으로 세워 정본
       함수에 넘긴다 — 두 답이 갈리지 않게.
    """
    return base.model_copy(
        update={
            "on_hand_by_lot": [
                InventoryLotSnapshot(
                    lot_id=lot.lot_id,
                    item=lot.item_name or lot.item_id,
                    grade=lot.grade,
                    available_qty_kg=lot.remaining_qty_kg,
                    received_at=lot.received_at,
                    unit_cost_krw_per_kg=lot.unit_cost_krw_per_kg,
                    remaining_freshness_days=lot.turnover.remaining_freshness_days,
                    effective_freshness_limit_days=lot.turnover.effective_freshness_limit_days,
                    # 🔴 유도된 상태다 — `inventory_lots.status` 컬럼이 아니다.
                    status=lot.state,
                    storage_zone=lot.storage_zone,
                )
                for lot in lots
                if lot.remaining_qty_kg > Decimal(0)
            ],
            "outbound_commitments": _historical_commitments(reservations),
            "used_capacity_kg": used_capacity_kg,
        }
    )


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


def load_console_runtime(*, conn: Any, sim_run_id: str, as_of: date) -> LogisticsRead | None:
    """그날의 Agent Runtime 읽기 한 벌. **없으면 `None` 이고 그것도 사실이다.**

    🔴 **한 화면에 한 번만 부른다** (2026-09-15). 종전에는 재고 콘솔이 `LogisticsRead`
       를, 입고 콘솔이 fixture 를 **각자** 읽어 같은 `logistics_runtime_fixture` ·
       `inbound_schedules` 질의가 한 요청에 두 번씩 나갔다 (일정 질의만 5번 · 421 ms).
       이제 `build_result` 가 이 함수를 한 번 부르고 두 콘솔에 `runtime=` 으로 넘긴다 —
       재고 콘솔은 Snapshot(판매가능량 축)을, 입고 콘솔은 `fixture`(운송 중 Header)와
       `inbound_schedule_views`(도착 처리 대상)를 같은 한 벌에서 꺼낸다.

    🔴 **부재(`LookupError`)만 삼킨다.** 활성 fixture 가 둘인 무결성 위반
       (`ValueError`)은 그대로 올려 보낸다 — 깨진 데이터가 *"데이터를 주세요"* 로
       둔갑하면 안 된다 (`repository` 의 같은 규율).

    ★ 이 값이 없어도 **재고 수량은 답한다.** 수량 정본은 원장이고 fixture 가 아니다.
      못 내는 것은 그 스냅샷의 확정 출고 축이 필요한 판매가능량과, 운송 중 목록뿐이다.
    """
    try:
        return get_current_logistics_read(as_of=as_of, sim_run_id=sim_run_id, conn=conn)
    except LookupError:
        return None


def get_inventory_console(
    *,
    conn: Any,
    sim_run_id: str,
    as_of: date,
    runtime: LogisticsRead | None,
    reservations: Sequence[HistoricalReservation] | None = None,
    item_id: str | None = None,
) -> ConsoleInventoryResponse:
    """품목 카드 · Lot 목록 · 창고 kg Capacity 한 판. **기준일은 `as_of` 다.**

    ```text
    현재고 · Lot · Capacity 사용량   historical_repository.lot_state_at        ← as_of 원장
    신선도 · 회전                     turnover (같은 함수 · 잔량만 그 시점 값)
    Capacity 한도                     agent_policy_config (지금 활성 · capacity_basis 표기)
    예약 3칸 · 건수                    reservation_state_at → 품목 집계          ← as_of (#760)
    판매가능량                        tools.build_inventory_by_item(그날 스냅샷)  ← as_of (#760)
    ```

    🔴 **`inventory_lots.remaining_qty_kg` 를 과거 잔량으로 쓰지 않는다.** 종전에는
       그 컬럼(`> 0`)으로 Lot 을 골라서, 실측 84 Lot 이 전부 잔량 0 이 된 뒤
       **모든 과거 날짜가 0 kg · 0 Lot** 으로 나왔다 (원장 복원값은 각각 294.4 ·
       806.4 · 806.4 · 6,452.4 kg).

    🔴 **만료 Lot 은 `on_hand_qty_kg` 와 `used_capacity_kg` 에 남고 판매가능량에서만
       빠진다.** 판매불가는 창고에서 사라진 것이 아니다.

    ★ **예약 3칸·판매가능량도 `as_of` 축이다 (#760 · LOG-HIST-002).** 예약·할당은
      `reservation_state_at` 이 그날 값으로 되살리고(품목 집계 =
      `_reservation_totals_from_history`), 판매가능량은 그 예약과 `lot_state_at` 으로
      **그날 스냅샷**을 세워 정본 `tools.build_inventory_by_item` 에 그대로 먹인다 —
      한 화면이 한 시간축에 선다.

    ★ **보관정책이 없는 품목의 Lot 도 싣는다.** 종전 스냅샷 경로는
      `item_storage_policies` 를 `INNER JOIN` 해서 그런 Lot 을 통째로 떨어뜨렸다 —
      정책이 없다는 이유로 실물 재고를 조회에서 지우지 않는다.

    :param runtime: `load_console_runtime` 이 그 `(sim_run_id, as_of)` 로 낸 한 벌.
        `None` 은 그날 Runtime Snapshot 이 없다는 사실이다 — 여기서 다시 읽지 않는다.
    :param reservations: `build_result` 가 한 판에 한 번 읽어 넘긴 그날 예약 목록.
        `None` 이면 여기서 직접 읽는다(단독 호출·테스트) — 화면 경로는 출고 콘솔과
        **같은 한 벌**을 공유해 중복 조회를 피한다(#719 · #760).
    """
    read = runtime
    policy = read.policy if read is not None else get_active_logistics_policy()

    historical_lots = _historical_lots(conn, sim_run_id=sim_run_id, as_of=as_of)
    names = _item_names(conn)
    resv = (
        reservations
        if reservations is not None
        else historical_repository.reservation_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    )
    reservation_totals = _reservation_totals_from_history(resv)
    mvp_items = _mvp_item_ids(conn)

    # ★ 창고 점유는 **그날 실재한 모든 Lot** 의 합이다 — 화면 필터보다 앞선다.
    used_capacity = sum((lot.remaining_qty_kg for lot in historical_lots), start=Decimal(0))

    # 판매가능량 — 정본 `build_inventory_by_item` 에 «그날» 스냅샷을 먹인다 (#760).
    #   축(Lot · 예약·할당)만 그날 값으로 세우고 계산은 정본이 한다.
    if read is None:
        inventory_by_item = None
        unresolved_reason: str | None = "RUNTIME_SNAPSHOT_UNAVAILABLE"
    else:
        avail_snapshot = _historical_availability_snapshot(
            read.snapshot,
            lots=historical_lots,
            reservations=resv,
            used_capacity_kg=used_capacity,
        )
        inventory_by_item = build_inventory_by_item(avail_snapshot)
        unresolved_reason = (
            None if inventory_by_item is not None
            else _available_unresolved_reason(avail_snapshot)
        )
    available_by_name = (
        None
        if inventory_by_item is None
        else {row.item: row.available_qty_kg for row in inventory_by_item}
    )

    # 🔴 «살아 있는 Lot» 만 목록에 싣는다 — 종전 화면과 같은 모집단이다.
    #    잔량 0 이 된 Lot 까지 늘어놓으면 84 줄이 되고 그날의 재고가 안 보인다.
    lots = [
        _console_lot(lot, names)
        for lot in historical_lots
        if lot.remaining_qty_kg > Decimal(0) and (item_id is None or lot.item_id == item_id)
    ]

    # 품목 축: 재고가 있는 품목 ∪ 예약이 있는 품목 ∪ mvp_active 품목.
    # ★ mvp_active 가 아니어도 실물이 있으면 싣는다 — 계약 밖 품목이라고 재고를 숨기지 않는다.
    item_ids = {lot.item_id for lot in lots} | set(reservation_totals) | set(mvp_items)
    if item_id is not None:
        item_ids = item_ids & {item_id}

    items: list[ConsoleInventoryItem] = []
    for current in sorted(item_ids):
        item_lots = [lot for lot in lots if lot.item_id == current]
        expired = [
            lot for lot in item_lots if lot.disposal_candidate and lot.remaining_qty_kg > Decimal(0)
        ]
        totals = reservation_totals.get(current, {})
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


def get_inbound_console(
    *, conn: Any, sim_run_id: str, as_of: date, runtime: LogisticsRead | None
) -> ConsoleInboundResponse:
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

    :param runtime: `load_console_runtime` 이 낸 한 벌. fixture(운송 중 Header)와
        일정 views(도착 처리 대상)를 여기서 꺼내 쓴다 — **다시 읽지 않는다.**
        `None` 은 그날 Runtime Snapshot 이 없다는 사실이다.
    """
    fixture = None if runtime is None else runtime.fixture
    in_transit = None if fixture is None else fixture.in_transit

    # ★ 도착 요약은 **받을 것이 남았나** 를 센다 — 운송 중 목록이 아니다.
    #   fixture 가 없는 날(미확인)에는 그 판정도 세울 수 없어 `None` 을 넘긴다.
    #   views 는 `runtime` 이 이미 읽은 것을 쓴다. 손수 만든 `LogisticsRead`(views
    #   없음)만 종전처럼 표에서 다시 읽는다.
    if fixture is None:
        due_source = None
    elif runtime is not None and runtime.inbound_schedule_views is not None:
        due_source = receivable_from(runtime.inbound_schedule_views)
    else:
        due_source = receivable_at(conn, sim_run_id=sim_run_id, as_of=as_of)
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
    *,
    conn: Any,
    sim_run_id: str,
    as_of: date,
    status: ReservationStatus | None = None,
    reservations: Sequence[HistoricalReservation] | None = None,
) -> ConsoleOutboundResponse:
    """`as_of` 시점의 예약 목록과 그 아래 할당들. **네 조회와 같은 축이다.**

    ```text
    예약 존재    sales.order_date <= as_of    ← 확정일부터다 (예약은 확정 직후 선다)
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

    :param reservations: `build_result` 가 한 판에 한 번 읽어 넘긴 그날 예약 목록.
        `None` 이면 여기서 직접 읽는다 — 화면 경로는 재고 콘솔과 **같은 한 벌**을
        공유해 `reservation_state_at` 을 두 번 조회하지 않는다 (#719 · #760).
    """
    rows = (
        reservations
        if reservations is not None
        else historical_repository.reservation_state_at(
            conn, sim_run_id=sim_run_id, as_of=as_of
        )
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
            for row in rows
            if status is None or row.status == status
        ],
    )


def get_fefo_candidates_by_item(
    *, conn: Any, sim_run_id: str, item_ids: Iterable[str], as_of: date
) -> dict[str, list[ConsoleFefoCandidate]]:
    """품목별 FEFO 후보. 🔴 **추천만 한다 — 고르지도 쓰지도 않는다.**

    ```text
    후보 = outbound.recommend_fefo_candidates(conn, sim_run_id, item_id, as_of)   품목당 1회
    ```

    🔴 **예약마다 묻지 않는다** (2026-09-15). 후보는 예약과 무관한 함수다 — Lot 잔량에서
       살아 있는 할당을 뺀 «물리 후보» 를 `(sim_run_id, item_id, as_of)` 만으로 내고, 정렬도
       `turnover.fefo_sort_key` 하나다 (`outbound.recommend_fefo_candidates` · 잠금 없음 ·
       쓰기 없음). 같은 품목의 예약 N 건에 N 번 물으면 같은 답을 N 번 받는다 — 종전에는
       그 호출마다 커넥션까지 새로 열어 예약 164건에 8.6초가 걸렸다 (마스터 실측).
       품목은 계약상 셋(`contracts.core.ITEMS`)이라 많아야 세 번이다.

    ★ **`sim_run_id` 는 호출자가 지어내지 않는다.** 화면은 `get_outbound_console` 이
      `reservation_state_at` 으로 이미 `r.sim_run_id = %(sim)s` 로 자른 그 축을 넘긴다 —
      종전 `_reservation_axis` 가 예약 행에서 읽어 주던 값과 같은 값이다.

    ★ 순서는 `item_id` 정렬이다 — 호출 순서가 결과를 바꾸지 않게.
    """
    return {
        item_id: [
            ConsoleFefoCandidate(
                lot_id=candidate.lot_id,
                available_qty_kg=candidate.available_qty_kg,
                remaining_freshness_days=candidate.remaining_freshness_days,
                received_at=candidate.received_at,
                grade=candidate.grade,
            )
            for candidate in outbound.recommend_fefo_candidates(
                conn, sim_run_id=sim_run_id, item_id=item_id, as_of=as_of
            )
        ]
        for item_id in sorted(set(item_ids))
    }


