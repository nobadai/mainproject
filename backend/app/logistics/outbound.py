"""outbound.py — 예약 · FEFO 후보 · 할당 · 실출고 (3-C1).

```text
Sales 확정 출고량
   → reserve_stock          품목 총량을 가용재고에서 잡아 둔다 (Lot 미지정)
   → recommend_fefo_candidates   ★ 추천만 한다. 고르지 않는다
   → allocate_stock         사람이 고른 lot_id + 수량을 확정한다
   → ship_allocated_stock   그때 처음 원장 OUT 이 나간다
   → release_reservation    잡아 둔 것을 되돌린다 (원장 Move 없음)
```

🔴 **물류는 무엇을 팔지 정하지 않는다.** 누구에게 · 얼마에 · 팔지 말지는 Sales 가
   정하고, 물류는 **확정된 출고량을 받아** 재고 쪽 사실만 만든다.

🔴 **예약은 잔량을 줄이지 않는다.** `inventory_lots.remaining_qty_kg` 를 바꾸는 것은
   **실출고의 원장 OUT 뿐**이다.

  ```text
  on_hand    = Lot.remaining_qty_kg              물리적으로 창고에 있는 양
  available  = remaining − 아직 안 나간 할당분    다른 판매가 이미 잡아 둔 몫을 뺀 것
  ```

  ⚠️ 그래서 `on_hand ≠ available` 이다. 예약 단계에서 잔량을 줄이면 **창고에 있는
     물건이 장부에서 사라지고**, 실출고 때 또 줄여 이중 차감이 된다.

★ **스키마 실측 (2026-09-05 · 저장소 DDL 과 실 DB 카탈로그 일치).**

  ```text
  inventory_reservations  PK reservation_id · sale_id(FK→sales, nullable) · item_id
                          required_qty_kg > 0 · 0 <= reserved_qty_kg <= required_qty_kg
                          status  RESERVED · PARTIALLY_ALLOCATED · ALLOCATED
                                  · RELEASED · CANCELLED
  inventory_allocations   PK allocation_id · FK reservation_id · lot_id · pallet_id(nullable)
                          allocated_qty_kg > 0
                          allocation_basis  FEFO_TOOL_CONFIRMED · HUMAN_OVERRIDE
                          status  ALLOCATED · PICKED · SHIPPED · CANCELLED
                          decided_by · decided_at  둘 다 NOT NULL → **호출자가 준다**
  ```

  🔴 **Shipment 표가 없다.** 저장소·실 DB 어디에도 outbound/shipment entity 가 없어,
     실출고는 **할당 상태 `SHIPPED` + 원장 OUT** 으로 표현한다. 새 표를 짓지 않는다.

  🔴 **`inventory_reservations` 는 `sale_id` 를 들고 `inventory_moves` 는
     `sale_item_id` 를 든다** (둘 다 FK, 다른 표). 물류가 둘을 서로 유도하지 않는다 —
     **호출자가 각각 준다.**

★ **어휘를 새로 만들지 않았다.** 출고 사유는 기존 원장에 이미 있는
  `SALE_FULFILLMENT` 다 (실측: OUT 75행이 이 값을 쓴다).

🔴 **잠금 순서 계약.** 출고 전용 전역 키 하나를 더한다.

  ```text
  (20260905, 1)  재고 원장 쓰기      ledger.py
  (20260905, 2)  도착 Receipt 쓰기   receipts.py
  (20260905, 3)  출고 예약·할당 쓰기  이 파일          ← 실측 확인 후 빈 키를 골랐다
  ```

  ```text
  ① 출고 전역 (20260905, 3)   ← 가장 먼저
  ② 가용량 **재계산**          잠금 밖에서 본 값을 믿지 않는다
  ③ 예약 / 할당 쓰기
  ④ 원장 전역 (20260905, 1)   record_inventory_move 안에서
  ⑤ Lot 행 FOR UPDATE          〃
  ⑥ 커밋은 호출자가 한 번
  ```

  ⚠️ **입고 경로와 자원이 겹치지 않는다** — 저쪽은 `(…,2) → fixture 행 → (…,1)`,
     이쪽은 `(…,3) → (…,1)` 이라 두 전순서가 `(…,1)` 에서만 만나고 순환이 없다.

  🔴 **가용량은 반드시 잠금 안에서 다시 센다.** 안 그러면 둘이 같은 100kg 을 보고
     각자 80 을 잡아 160 이 나간다.

⚠️ **`confirmed_outbound` 와 이중 차감하지 않는다.** 실 DB 의 그 칸은 지금 **모든
   행에서 비어 있다**(실측) — 예약을 표현하는 코드가 아무 데도 없다. 그래서 가용량의
   차감 근거는 **이 표(할당)뿐**이고, 나중에 그 칸을 쓰게 되면 그때 한 축으로 합쳐야
   한다 (지금 둘을 다 빼면 없는 예약을 두 번 빼게 된다).

⚠️ **운송 Route · Pallet · Location 은 이 판이 아니다.** `pallet_id` 는 NULL 로 둔다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.ledger import record_inventory_move
from app.logistics.turnover import freshness_days_of, is_disposal_candidate

__all__ = [
    "AllocationRequest",
    "AllocationResult",
    "FefoCandidate",
    "InvalidOutboundRequest",
    "OutboundError",
    "OutboundIntegrityError",
    "ReservationConflict",
    "ReservationResult",
    "ShipmentResult",
    "allocate_stock",
    "allocation_id_for",
    "item_free_stock_qty",
    "lock_outbound_writes",
    "move_id_for_allocation",
    "recommend_fefo_candidates",
    "release_reservation",
    "reserve_stock",
    "ship_allocated_stock",
]


#: `ck_inventory_reservations_status` 어휘 그대로다.
ReservationStatus = Literal["RESERVED", "PARTIALLY_ALLOCATED", "ALLOCATED", "RELEASED", "CANCELLED"]
#: `ck_inventory_allocations_status` 어휘 그대로다.
AllocationStatus = Literal["ALLOCATED", "PICKED", "SHIPPED", "CANCELLED"]
#: `ck_inventory_allocations_basis` 어휘 그대로다.
AllocationBasis = Literal["FEFO_TOOL_CONFIRMED", "HUMAN_OVERRIDE"]

_RESERVATION_STATUSES: frozenset[str] = frozenset(get_args(ReservationStatus))
_ALLOCATION_BASES: frozenset[str] = frozenset(get_args(AllocationBasis))

#: 🔴 **아직 재고를 잡고 있는** 예약 상태. `RELEASED` · `CANCELLED` 는 놓아준 것이다.
_HOLDING_RESERVATION: frozenset[str] = frozenset({"RESERVED", "PARTIALLY_ALLOCATED", "ALLOCATED"})

#: 🔴 **아직 창고에서 안 나간** 할당 상태. 가용량에서 빼야 하는 것이 이것이다.
#:
#: ★ `SHIPPED` 는 빼지 않는다 — 그 몫은 이미 원장 OUT 이 `remaining_qty_kg` 에서
#:   덜어냈으므로, 여기서 또 빼면 **같은 수량을 두 번 차감**하게 된다.
_HOLDING_ALLOCATION: frozenset[str] = frozenset({"ALLOCATED", "PICKED"})

#: 🔴 예약이 **이미 Lot 에 배정한** 몫. 예약의 *"아직 안 배정된 잔여"* 를 셀 때 뺀다.
#:
#: ★ `SHIPPED` 도 포함한다 — 나간 몫은 그 예약이 더 이상 새로 잡아 둘 필요가 없다.
#:   빼지 않으면 출고 뒤에도 예약이 원래 총량을 계속 잡고 있는 것으로 보여
#:   **같은 수량이 잔량 감소와 예약 양쪽에서 두 번 깎인다.**
_ASSIGNED_ALLOCATION: frozenset[str] = frozenset({"ALLOCATED", "PICKED", "SHIPPED"})

#: 🔴 기존 원장에 이미 있는 어휘다 (실측: OUT 75행). 새 사유를 만들지 않는다.
_OUT_REASON_CODE = "SALE_FULFILLMENT"

#: 🔴 출고 쓰기 전역 잠금. `(…,1)` 원장 · `(…,2)` 도착과 겹치지 않는 빈 키다 (실측).
_OUTBOUND_LOCK_CLASSID = 20260905
_OUTBOUND_LOCK_OBJID = 3

_AMBIGUITY_PROBE_LIMIT = 2


class OutboundError(RuntimeError):
    """이 모듈이 내는 실패의 조상."""


class InvalidOutboundRequest(OutboundError, ValueError):
    """요청이 DB 계약이나 가용재고를 어긴다. **DML 전에 막는다.**"""


class ReservationConflict(OutboundError, ValueError):
    """같은 `reservation_id` 에 **다른 사실**의 예약이 이미 있다.

    🔴 수량을 조용히 덮어쓰지 않는다 — 덮으면 앞 요청이 소리 없이 사라진다.
    """


class OutboundIntegrityError(OutboundError, ValueError):
    """예약·할당·원장이 서로를 배반한다. **조용히 고치지 않는다.**"""


@dataclass(frozen=True)
class FefoCandidate:
    """FEFO 추천 한 줄. **추천일 뿐 고른 것이 아니다.**"""

    lot_id: str
    available_qty_kg: Decimal
    #: `repository` 와 **같은 식**으로 센다 — 새 유통기한 공식을 만들지 않는다.
    remaining_freshness_days: int | None
    received_at: date
    #: DB raw 등급 그대로. 정규화하지 않는다.
    grade: str | None


@dataclass(frozen=True)
class ReservationResult:
    applied: bool
    reservation_id: str
    status: ReservationStatus
    required_qty_kg: Decimal


@dataclass(frozen=True)
class AllocationRequest:
    """**사람이 고른** Lot 과 수량. 코드가 정하지 않는다."""

    lot_id: str
    quantity_kg: Decimal


@dataclass(frozen=True)
class AllocationResult:
    applied: bool
    allocation_ids: tuple[str, ...]
    reservation_status: ReservationStatus
    allocated_qty_kg: Decimal


@dataclass(frozen=True)
class ShipmentResult:
    applied: bool
    #: 이번에 실제로 내보낸 할당들.
    shipped_allocation_ids: tuple[str, ...]
    move_ids: tuple[str, ...]
    shipped_qty_kg: Decimal


# ── 순수 도우미 ─────────────────────────────────────────────────────────


def allocation_id_for(*, reservation_id: str, lot_id: str) -> str:
    """할당 PK. **순수 계산이고 결정론이다.**

    ```text
    ALC-{reservation_id}-{lot_id}
    ```

    ★ 한 예약이 여러 Lot 에 걸칠 수 있고(스키마가 그렇게 설계됐다) **Lot 마다 한 줄**
      이므로, 두 값이 함께여야 정체성이 된다.

    🔴 난수 · 시계 · 시퀀스를 쓰지 않는다 — 재실행이 같은 할당을 다른 행으로 만들면
       가용량이 두 번 깎인다.
    """
    _require_text(reservation_id, 칸="reservation_id")
    _require_text(lot_id, 칸="lot_id")
    return f"ALC-{reservation_id}-{lot_id}"


def move_id_for_allocation(*, allocation_id: str) -> str:
    """출고 Move 의 멱등 키. **할당에 뿌리를 둔다.**

    ★ `record_inventory_move` 가 `move_id` 로 이미 멱등하다 — 그 장치를 그대로 쓴다.
    """
    _require_text(allocation_id, 칸="allocation_id")
    return f"MOVE-OUT-{allocation_id}"


def _require_text(값: Any, *, 칸: str) -> str:
    if not isinstance(값, str) or not 값.strip():
        raise InvalidOutboundRequest(f"{칸} 가 비었다: {값!r}")
    return 값


def _quantity(값: Any, *, 칸: str) -> Decimal:
    """수량을 `Decimal` 로 좁힌다. **float 도 비유한값도 받지 않는다.**

    ★ `ledger._quantity` · `inspections._quantity` 와 같은 규율이다.
    """
    if isinstance(값, bool) or not isinstance(값, Decimal):
        raise InvalidOutboundRequest(
            f"{칸} 은 Decimal 이어야 한다 (받은 것: {값!r} · {type(값).__name__})."
        )
    if not 값.is_finite():
        raise InvalidOutboundRequest(f"{칸} 이 유한한 수가 아니다: {값!r}")
    if 값 <= 0:
        raise InvalidOutboundRequest(f"{칸} 은 0보다 커야 한다 (받은 것: {값})")
    return 값


def lock_outbound_writes(cursor: Any) -> None:
    """출고 쓰기를 **하나의 전역 잠금으로 직렬화한다.**

    🔴 **이 잠금이 가용량 경합을 닫는다.**

    ```text
    잠금 없이   T1 available 100 · T2 available 100 → 각자 80 예약 → 160 이 나간다
    잠금 있으면 T2 는 T1 이 끝난 뒤 **다시 세고** 20 만 남은 것을 본다
    ```

    ★ **건별 잠금을 쓰지 않는다** — `ledger._lock_ledger_writes` 가 적어 둔 교착이
      그대로 재현된다. 한 트랜잭션이 여러 Lot 을 다루면 요청 잠금 **집합**이 달라져
      전순서를 매길 수 없다.

    ★ transaction-level 이라 호출자의 커밋/롤백과 함께 풀린다. unlock 을 부르지 않는다.
    """
    cursor.execute(
        sql.SQL("SELECT pg_advisory_xact_lock(%s, %s)"),
        (_OUTBOUND_LOCK_CLASSID, _OUTBOUND_LOCK_OBJID),
    )


def _cell(row: Any, index: int, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _rows(
    conn: Any, query: Any, params: tuple | Mapping[str, Any], 이름: tuple[str, ...]
) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        found = cursor.fetchall()
    return [{n: _cell(r, i, n) for i, n in enumerate(이름)} for r in found]


# ── 가용재고 ────────────────────────────────────────────────────────────

_LOT_AVAILABILITY_COLUMNS = (
    "lot_id",
    "remaining_qty_kg",
    "received_at",
    "grade",
    "operational_limit_days",
    "medium_grade_factor",
    "held_qty_kg",
)


def _available_lots(
    conn: Any, schema: sql.Identifier, *, sim_run_id: str, item_id: str, as_of: date
) -> list[dict[str, Any]]:
    """이 품목의 **가용** Lot 들. 반드시 잠금 안에서 부른다.

    ```text
    available = remaining_qty_kg − (아직 안 나간 할당 합)
    ```

    🔴 **`SHIPPED` 할당은 빼지 않는다.** 그 몫은 원장 OUT 이 이미 `remaining_qty_kg`
       에서 덜어냈다 — 여기서 또 빼면 같은 수량을 두 번 차감한다.

    ★ **`status='ACTIVE'` 와 `remaining > 0` 로 거른다** — `repository` 가 가용 재고를
      보는 눈과 같다 (비-ACTIVE 는 물리 점유만 하고 가용에서 빠진다).

    🔴 **신선도가 소진된 Lot(`remaining_freshness_days <= 0`)도 뺀다.** 그 Lot 은
       `tools.build_inventory_by_item` 이 이미 판매 가용에서 빼고 있고,
       `turnover.is_disposal_candidate` 가 폐기대기로 표시하는 바로 그 재고다.
       여기서 안 빼면 **판매 못 하는 재고를 예약·할당이 다시 잡는다.**

       ⚠️ **재고를 없애는 것이 아니다.** `remaining_qty_kg` 도 Lot 상태도 그대로이고,
          창고 점유도 그대로다 — 빠지는 것은 *"팔 수 있는 양"* 하나뿐이다.
          실제 감소는 `disposal.confirm_disposal` 만 한다.

    ★ **신선도 식을 새로 만들지 않는다.** `turnover.freshness_days_of` 를 그대로 쓴다 —
      그쪽이 `repository` 와 같은 계산을 이미 하고 있어 세 곳이 갈릴 자리가 없다.

    ⚠️ 신선도 계산에 쓰는 두 값(`operational_limit_days` · `medium_grade_factor`)도
       같은 조인에서 가져온다 — `repository` 와 **같은 출처**여야 두 곳이 안 갈린다.
    """
    query = sql.SQL(
        """
        SELECT l.lot_id, l.remaining_qty_kg, l.received_at, l.grade,
               p.operational_limit_days, p.medium_grade_factor,
               COALESCE((
                   SELECT SUM(a.allocated_qty_kg)
                   FROM {schema}.inventory_allocations a
                   JOIN {schema}.inventory_reservations r
                     ON r.reservation_id = a.reservation_id
                   WHERE a.lot_id = l.lot_id
                     AND a.status = ANY(%s)
                     AND r.status = ANY(%s)
               ), 0) AS held_qty_kg
        FROM {schema}.inventory_lots l
        JOIN {schema}.item_storage_policies p ON p.item_id = l.item_id
        WHERE l.sim_run_id = %s
          AND l.item_id = %s
          AND l.status = 'ACTIVE'
          AND l.remaining_qty_kg > 0
        ORDER BY l.lot_id
        """
    ).format(schema=schema)
    행들 = _rows(
        conn,
        query,
        (
            sorted(_HOLDING_ALLOCATION),
            sorted(_HOLDING_RESERVATION),
            sim_run_id,
            item_id,
        ),
        _LOT_AVAILABILITY_COLUMNS,
    )
    # 🔴 판매 가용에서 이미 빠진 Lot 은 예약·FEFO·할당 어디에도 오르지 않는다.
    #    ★ `0 != null` — 신선도를 **모르는** Lot 은 빼지 않는다 (확인된 만료가 아니다).
    return [
        행
        for 행 in 행들
        if not is_disposal_candidate(remaining_freshness_days=freshness_days_of(행, as_of=as_of))
    ]


def _available_qty(행: Mapping[str, Any]) -> Decimal:
    return Decimal(행["remaining_qty_kg"]) - Decimal(행["held_qty_kg"])


def item_free_stock_qty(
    conn: Any, schema: sql.Identifier, *, sim_run_id: str, item_id: str, as_of: date
) -> Decimal:
    """이 품목에서 **아무도 잡지 않은, 팔 수 있는 총량.** 반드시 잠금 안에서 부른다.

    ★ **읽는 곳은 예약(`reserve_stock`) 하나다** — *"새 Reservation 이 확보할 수 있는
      판매 가능한 미확보 품목 총량"* 이라는 뜻이고, 그 밖의 축에는 답이 되지 않는다.

    🔴 **Lot 가용량의 합과 다르다.** Lot 가용량은 *"이 Lot 에서 아직 어떤 할당에도
       안 묶인 물리량"* 이고, 이 값은 *"아직 아무도 잡지 않은 총량"* 이다.
       **아직 Lot 을 안 고른 예약**은 특정 Lot 에 안 붙어 있어 Lot 가용량에서
       안 빠진다 — 그것만 보면 같은 재고를 두 번 예약하게 된다.

    ```text
    Lot remaining 100 · 예약 A 80 (할당 0)
    Lot 가용량 합   = 100      ← 예약 B 80 이 통과해 버린다 🔴
    이 함수         = 20
    ```

    ```text
    free = Σ(_available_lots 의 가용량)   판매 가능 Lot 만 · 살아있는 할당은 이미 빠졌다
         − unallocated_reservations       잡아 뒀지만 Lot 을 안 고른 몫
    ```

    🔴 **`_available_lots` 를 거쳐 센다.** 그래야 비-ACTIVE·신선도 소진 Lot 이
       **재고와 할당 양쪽에서 함께** 빠진다 — 한쪽만 빼면 과다·과소 차감이 된다.

    ⚠️ **이중 차감을 피하는 규칙.**

    ```text
    unallocated = max(reserved_qty − 이미 배정한 몫, 0)
                  이미 배정한 몫 = ALLOCATED · PICKED · SHIPPED

    SHIPPED 를 빼는 이유   그 몫은 원장 OUT 이 remaining 에서 이미 덜어냈다
    ALLOCATED/PICKED 는    _available_lots 가 이미 뺐으므로 여기서 다시 세지 않는다
    ```

    ⚠️ **`RELEASED` · `CANCELLED` 예약은 세지 않는다** — 놓아준 몫이라 돌아와야 한다.

    :raises OutboundIntegrityError: 계산이 음수일 때. **0 으로 보정하지 않는다** —
        음수는 이미 잡힌 몫이 실재 재고를 넘었다는 뜻이라 데이터 문제다.
    """
    가용합 = sum(
        (
            _available_qty(행)
            for 행 in _available_lots(
                conn, schema, sim_run_id=sim_run_id, item_id=item_id, as_of=as_of
            )
        ),
        start=Decimal(0),
    )
    query = sql.SQL(
        """
        SELECT COALESCE(SUM(GREATEST(r.reserved_qty_kg - COALESCE((
                   SELECT SUM(a.allocated_qty_kg)
                   FROM {schema}.inventory_allocations a
                   WHERE a.reservation_id = r.reservation_id
                     AND a.status = ANY(%(assigned)s)
               ), 0), 0)), 0) AS unallocated_reservations
        FROM {schema}.inventory_reservations r
        WHERE r.sim_run_id = %(sim)s AND r.item_id = %(item)s
          AND r.status = ANY(%(holding)s)
        """
    ).format(schema=schema)
    found = _rows(
        conn,
        query,
        {
            "sim": sim_run_id,
            "item": item_id,
            "assigned": sorted(_ASSIGNED_ALLOCATION),
            "holding": sorted(_HOLDING_RESERVATION),
        },
        ("unallocated_reservations",),
    )
    미할당 = Decimal(found[0]["unallocated_reservations"])
    free = 가용합 - 미할당
    if free < 0:
        raise OutboundIntegrityError(
            f"예약 가능량이 음수다 (item_id={item_id!r}): 판매가능 {가용합}"
            f" · 미할당 예약 {미할당}."
            " 0 으로 보정하지 않는다 — 잡힌 몫이 실재 재고를 넘었다는 뜻이다."
        )
    return free


# ── 예약 ────────────────────────────────────────────────────────────────

_RESERVATION_COLUMNS = (
    "reservation_id",
    "sim_run_id",
    "item_id",
    "sale_id",
    "required_qty_kg",
    "reserved_qty_kg",
    "status",
    "due_date",
)


def _reservation(conn: Any, schema: sql.Identifier, *, reservation_id: str) -> dict | None:
    found = _rows(
        conn,
        sql.SQL(
            """
            SELECT reservation_id, sim_run_id, item_id, sale_id,
                   required_qty_kg, reserved_qty_kg, status, due_date
            FROM {}.inventory_reservations
            WHERE reservation_id = %s
            """
        ).format(schema),
        (reservation_id,),
        _RESERVATION_COLUMNS,
    )
    return found[0] if found else None


def reserve_stock(
    conn: Any,
    *,
    reservation_id: str,
    sim_run_id: str,
    item_id: str,
    required_qty_kg: Decimal,
    as_of: date,
    sale_id: str | None = None,
    due_date: date | None = None,
) -> ReservationResult:
    """Sales 가 확정한 출고량을 가용재고에서 **잡아 둔다.** Lot 은 아직 안 고른다.

    🔴 **`remaining_qty_kg` 를 건드리지 않는다.** 예약은 *"이 몫은 남이 못 쓴다"* 는
       사실이고, 물건은 아직 창고에 있다. 잔량을 줄이는 것은 실출고의 원장 OUT 뿐이다.

    ⚠️ **잠금 안에서 가용량을 다시 센다.** 잠금 밖에서 본 값은 이미 낡았을 수 있다.

    ★ **`reservation_id` 는 호출자가 준다.** 스키마가 한 `sale_id` 에 여러 예약을
      허용하므로(유일 제약이 `reservation_id` 뿐이다) 물류가 `RSV-{sale_id}` 같은
      규칙을 강요하지 않는다 — 그 규칙은 판매 쪽 정체성이다.

    ```text
    같은 id + 같은 사실  applied=False
    같은 id + 다른 사실  ReservationConflict   ★ 수량을 조용히 덮지 않는다
    가용 부족            InvalidOutboundRequest (DML 전)
    ```

    :param as_of: 가용량 기준일. **신선도가 날짜에 달려 있어** 필요하다 — 폐기대기
        Lot(`remaining_freshness_days <= 0`)은 이 날짜로 걸러진다.
    """
    _require_text(reservation_id, 칸="reservation_id")
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(item_id, 칸="item_id")
    required = _quantity(required_qty_kg, 칸="required_qty_kg")
    schema = sql.Identifier(get_db_schema())

    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    기존 = _reservation(conn, schema, reservation_id=reservation_id)
    if 기존 is not None:
        다른것 = {
            칸: (기존[칸], 값)
            for 칸, 값 in (
                ("sim_run_id", sim_run_id),
                ("item_id", item_id),
                ("sale_id", sale_id),
                ("required_qty_kg", required),
                ("due_date", due_date),
            )
            if 기존[칸] != 값
        }
        if 다른것:
            raise ReservationConflict(
                f"같은 reservation_id 에 다른 사실의 예약이 있다"
                f" ({reservation_id!r}): {다른것!r}. 덮지도 버리지도 않는다."
            )
        return ReservationResult(
            applied=False,
            reservation_id=reservation_id,
            status=기존["status"],
            required_qty_kg=기존["required_qty_kg"],
        )

    # ★ 잠금 안에서 다시 센다.
    # 🔴 **Lot 가용량의 합이 아니라 품목 예약 가능량이다.** 아직 Lot 을 안 고른
    #    남의 예약은 어떤 Lot 에도 안 붙어 있어 Lot 가용량에서 안 빠진다 —
    #    그것만 보면 같은 재고를 두 번 예약하게 된다 (`item_free_stock_qty`).
    예약가능 = item_free_stock_qty(
        conn, schema, sim_run_id=sim_run_id, item_id=item_id, as_of=as_of
    )
    if 예약가능 < required:
        raise InvalidOutboundRequest(
            f"가용재고가 모자라 예약할 수 없다 (item_id={item_id!r}):"
            f" 필요 {required} · 예약 가능 {예약가능}."
            " 없는 재고를 잡아 두지 않는다 — 잡아 두면 다른 판매가 그만큼 못 쓴다."
        )

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.inventory_reservations (
                    reservation_id, sim_run_id, item_id, sale_id,
                    required_qty_kg, reserved_qty_kg, due_date, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema),
            (
                reservation_id,
                sim_run_id,
                item_id,
                sale_id,
                required,
                # ★ 품목 총량을 확보한 것이다 — Lot 지정은 Allocation 쪽이다.
                required,
                due_date,
                "RESERVED",
            ),
        )
    return ReservationResult(
        applied=True, reservation_id=reservation_id, status="RESERVED", required_qty_kg=required
    )


def release_reservation(
    conn: Any, *, reservation_id: str, status: ReservationStatus = "RELEASED"
) -> ReservationResult:
    """잡아 둔 몫을 **놓아준다.** 원장 Move 가 없다.

    🔴 **이미 나간 수량을 되돌리지 않는다.** `SHIPPED` 할당이 하나라도 있으면 멈춘다 —
       환입은 이 판의 범위가 아니고, `ADJUST_IN` 을 쓰지도 않는다.

    ★ 아직 안 나간 할당은 함께 `CANCELLED` 로 내린다. 그래야 그 Lot 의 가용량이
      실제로 돌아온다 (`_HOLDING_ALLOCATION` 에서 빠진다).
    """
    _require_text(reservation_id, 칸="reservation_id")
    if status not in {"RELEASED", "CANCELLED"}:
        raise InvalidOutboundRequest(
            f"놓아주는 상태가 아니다: {status!r}. 허용: RELEASED · CANCELLED."
        )
    schema = sql.Identifier(get_db_schema())

    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    기존 = _reservation(conn, schema, reservation_id=reservation_id)
    if 기존 is None:
        raise OutboundIntegrityError(f"놓아줄 예약이 없다: {reservation_id!r}")
    if 기존["status"] == status:
        return ReservationResult(
            applied=False,
            reservation_id=reservation_id,
            status=status,
            required_qty_kg=기존["required_qty_kg"],
        )

    나간것 = [
        행
        for 행 in _allocations(conn, schema, reservation_id=reservation_id)
        if 행["status"] == "SHIPPED"
    ]
    if 나간것:
        raise OutboundIntegrityError(
            f"이미 출고된 할당이 있어 예약을 놓아줄 수 없다 ({reservation_id!r}):"
            f" {[행['allocation_id'] for 행 in 나간것]!r}."
            " 나간 재고를 예약 취소로 되돌리지 않는다 — 환입은 이 판의 범위가 아니다."
        )

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.inventory_allocations
                SET status = 'CANCELLED'
                WHERE reservation_id = %s AND status = ANY(%s)
                """
            ).format(schema),
            (reservation_id, sorted(_HOLDING_ALLOCATION)),
        )
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.inventory_reservations
                SET status = %s, reserved_qty_kg = 0, updated_at = now()
                WHERE reservation_id = %s
                """
            ).format(schema),
            (status, reservation_id),
        )
    return ReservationResult(
        applied=True,
        reservation_id=reservation_id,
        status=status,
        required_qty_kg=기존["required_qty_kg"],
    )


# ── FEFO 후보 ───────────────────────────────────────────────────────────


def recommend_fefo_candidates(
    conn: Any, *, sim_run_id: str, item_id: str, as_of: date
) -> tuple[FefoCandidate, ...]:
    """FEFO 순서로 **후보만** 돌려준다.

    🔴 **고르지 않는다.** 이 함수는 Lot 을 잡지도, 할당을 만들지도, 아무것도 쓰지도
       않는다. 자동 Allocation 은 후속 과제이고
       (`inventory_allocations.allocation_basis` 주석이 그렇게 적고 있다),
       사람이 `allocate_stock` 에 `lot_id` 와 수량을 명시해야 확정된다.

    ```text
    정렬  remaining_freshness_days ASC → received_at ASC → lot_id ASC
    제외  available <= 0
    ```

    🔴 **여기의 `available_qty_kg` 는 "추가로 예약할 수 있는 양"이 아니다.**
       뜻은 *"이 Lot 에서 아직 다른 할당에 묶이지 않은 물리적 후보량"* 이다.

    ```text
    Lot remaining 100 · 예약 A 80 (아직 Lot 미지정)
    → 이 후보의 available_qty_kg = 100    ★ A 가 어느 Lot 도 안 골랐으니 맞다
    → 그러나 새로 예약할 수 있는 양은 20  (`item_free_stock_qty`)
    ```

       ⚠️ 이 값을 예약 가능량으로 쓰면 **같은 재고를 두 번 예약한다.** 이 수치는
          *"이미 확보된 예약분을 어느 Lot 에서 뺄까"* 를 고를 때 보는 것이다.

    ⚠️ 신선도를 모르는 Lot(보관 정책에 `operational_limit_days` 가 없음)은 **맨 뒤**로
       보낸다 — 모르는 것을 *"가장 급하다"* 로도 *"가장 여유롭다"* 로도 읽지 않는다.
    """
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(item_id, 칸="item_id")
    schema = sql.Identifier(get_db_schema())

    후보: list[FefoCandidate] = []
    for 행 in _available_lots(conn, schema, sim_run_id=sim_run_id, item_id=item_id, as_of=as_of):
        가용 = _available_qty(행)
        if 가용 <= 0:
            continue
        후보.append(
            FefoCandidate(
                lot_id=행["lot_id"],
                available_qty_kg=가용,
                remaining_freshness_days=freshness_days_of(행, as_of=as_of),
                received_at=행["received_at"],
                grade=행["grade"],
            )
        )
    후보.sort(
        key=lambda c: (
            c.remaining_freshness_days is None,
            c.remaining_freshness_days if c.remaining_freshness_days is not None else 0,
            c.received_at,
            c.lot_id,
        )
    )
    return tuple(후보)


# ── 할당 ────────────────────────────────────────────────────────────────

_ALLOCATION_COLUMNS = ("allocation_id", "reservation_id", "lot_id", "allocated_qty_kg", "status")


def _allocations(conn: Any, schema: sql.Identifier, *, reservation_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        sql.SQL(
            """
            SELECT allocation_id, reservation_id, lot_id, allocated_qty_kg, status
            FROM {}.inventory_allocations
            WHERE reservation_id = %s
            ORDER BY allocation_id
            """
        ).format(schema),
        (reservation_id,),
        _ALLOCATION_COLUMNS,
    )


def allocate_stock(
    conn: Any,
    *,
    reservation_id: str,
    requests: Sequence[AllocationRequest],
    decided_by: str,
    decided_at: datetime,
    allocation_basis: AllocationBasis,
    as_of: date,
) -> AllocationResult:
    """**사람이 고른** Lot 과 수량으로 할당을 확정한다.

    🔴 **여기서 Lot 을 고르지 않는다.** FEFO 는 추천까지이고, 무엇을 얼마나 뺄지는
       호출자가 `requests` 로 명시한다.

    ```text
    검증  각 수량 > 0
          합계 <= 예약 잔여량 (이미 확정된 할당분을 뺀 나머지)
          Lot 별 가용량 >= 이번 할당량   ★ 다른 예약이 잡아 둔 몫은 못 쓴다
    ```

    ⚠️ **원장 OUT 을 부르지 않는다.** 할당은 *"어느 Lot 에서 뺄지 정했다"* 이지 아직
       나간 것이 아니다. 잔량은 실출고 때 움직인다.

    :param decided_by: 누가 정했나. **NOT NULL 이고 호출자가 준다** — 물류가 사람
        이름을 지어내지 않는다.
    :param decided_at: 언제 정했나. 시계를 읽지 않고 호출자가 준다 (tz 필요).
    :param allocation_basis: 이 선택이 FEFO 추천을 따른 것인가 사람이 다르게 정한
        것인가. **기본값이 없다 — 호출자가 반드시 말해야 한다.**

        🔴 **`FEFO_TOOL_CONFIRMED` 를 기본값으로 두면 안 된다.** FEFO 후보를
           불러 봤다는 사실과 그 추천을 **따랐다**는 사실은 다른 것이고, 기본값은
           묻지도 않고 뒤엣것을 장부에 적는다. 사람이 다른 Lot 을 골랐어도
           *"Tool 이 추천한 대로 했다"* 로 남아, 나중에 왜 그 Lot 이었는지 물을 때
           **근거가 거짓으로 서 있다.**
    """
    _require_text(reservation_id, 칸="reservation_id")
    _require_text(decided_by, 칸="decided_by")
    if not isinstance(decided_at, datetime) or decided_at.tzinfo is None:
        raise InvalidOutboundRequest(
            f"decided_at 은 시간대를 단 datetime 이어야 한다: {decided_at!r}"
        )
    if allocation_basis not in _ALLOCATION_BASES:
        raise InvalidOutboundRequest(
            f"할당 근거가 계약 어휘 밖이다: {allocation_basis!r}."
            f" 허용: {sorted(_ALLOCATION_BASES)}."
        )
    if not requests:
        raise InvalidOutboundRequest("할당할 Lot 이 하나도 없다.")

    묶음: dict[str, Decimal] = {}
    for 요청 in requests:
        lot_id = _require_text(요청.lot_id, 칸="lot_id")
        수량 = _quantity(요청.quantity_kg, 칸="quantity_kg")
        if lot_id in 묶음:
            raise InvalidOutboundRequest(
                f"같은 Lot 이 요청에 두 번 있다: {lot_id!r}. 합쳐서 한 번에 준다."
            )
        묶음[lot_id] = 수량

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    예약 = _reservation(conn, schema, reservation_id=reservation_id)
    if 예약 is None:
        raise OutboundIntegrityError(f"할당할 예약이 없다: {reservation_id!r}")
    if 예약["status"] not in _HOLDING_RESERVATION:
        raise OutboundIntegrityError(
            f"놓아준 예약에는 할당할 수 없다 ({reservation_id!r}, status={예약['status']!r})."
        )

    기존 = {
        행["allocation_id"]: 행 for 행 in _allocations(conn, schema, reservation_id=reservation_id)
    }
    살아있는 = [행 for 행 in 기존.values() if 행["status"] in _HOLDING_ALLOCATION | {"SHIPPED"}]
    이미할당 = sum((Decimal(행["allocated_qty_kg"]) for 행 in 살아있는), start=Decimal(0))

    새것: list[tuple[str, str, Decimal]] = []
    applied = False
    for lot_id, 수량 in 묶음.items():
        allocation_id = allocation_id_for(reservation_id=reservation_id, lot_id=lot_id)
        있던것 = 기존.get(allocation_id)
        if 있던것 is not None and 있던것["status"] != "CANCELLED":
            if Decimal(있던것["allocated_qty_kg"]) != 수량:
                raise ReservationConflict(
                    f"같은 할당에 다른 수량이 이미 있다 ({allocation_id!r}):"
                    f" 기존 {있던것['allocated_qty_kg']} 이번 {수량}. 덮지 않는다."
                )
            continue  # ★ 멱등 재실행이다.
        새것.append((allocation_id, lot_id, 수량))

    if 새것:
        더할것 = sum((수량 for _, _, 수량 in 새것), start=Decimal(0))
        남은예약 = Decimal(예약["required_qty_kg"]) - 이미할당
        if 더할것 > 남은예약:
            raise InvalidOutboundRequest(
                f"예약 잔여량을 넘는 할당이다 ({reservation_id!r}):"
                f" 이번 {더할것} · 남은 {남은예약} (요구 {예약['required_qty_kg']}"
                f" · 이미 {이미할당})."
            )

        가용 = {
            행["lot_id"]: _available_qty(행)
            for 행 in _available_lots(
                conn, schema, sim_run_id=예약["sim_run_id"], item_id=예약["item_id"], as_of=as_of
            )
        }
        for allocation_id, lot_id, 수량 in 새것:
            if lot_id not in 가용:
                raise InvalidOutboundRequest(
                    f"이 품목의 가용 Lot 이 아니다: {lot_id!r}"
                    f" (item_id={예약['item_id']!r}). 다른 품목이거나 비-ACTIVE 이거나"
                    " 잔량이 0 이다."
                )
            if 수량 > 가용[lot_id]:
                raise InvalidOutboundRequest(
                    f"Lot 가용량을 넘는 할당이다 ({lot_id!r}): 이번 {수량} · 가용"
                    f" {가용[lot_id]}. 다른 예약이 잡아 둔 몫은 쓸 수 없다."
                )

        # ★ **`ON CONFLICT` 를 쓰지 않는다.** 잠금 안에서 기존 행을 이미 읽었으므로
        #   여기서 가르면 된다 — DB 충돌 처리를 정상 흐름으로 쓰면 무엇이 새 행이고
        #   무엇이 되살린 행인지 코드에서 안 보인다 (`ledger.py` 와 같은 규율).
        with conn.cursor() as cursor:
            for allocation_id, lot_id, 수량 in 새것:
                되살릴것 = 기존.get(allocation_id)
                if 되살릴것 is None:
                    cursor.execute(
                        sql.SQL(
                            """
                            INSERT INTO {}.inventory_allocations (
                                allocation_id, reservation_id, lot_id, allocated_qty_kg,
                                allocation_basis, decided_by, decided_at, status
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'ALLOCATED')
                            """
                        ).format(schema),
                        (
                            allocation_id,
                            reservation_id,
                            lot_id,
                            수량,
                            allocation_basis,
                            decided_by,
                            decided_at,
                        ),
                    )
                else:
                    # ★ 취소됐던 할당을 같은 정체성으로 다시 세운다.
                    cursor.execute(
                        sql.SQL(
                            """
                            UPDATE {}.inventory_allocations
                            SET allocated_qty_kg = %s, allocation_basis = %s,
                                decided_by = %s, decided_at = %s, status = 'ALLOCATED'
                            WHERE allocation_id = %s AND status = 'CANCELLED'
                            """
                        ).format(schema),
                        (수량, allocation_basis, decided_by, decided_at, allocation_id),
                    )
            이미할당 += 더할것
        applied = True

    상태 = _reservation_status_for(예약, allocated=이미할당)
    if 상태 != 예약["status"]:
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {}.inventory_reservations
                    SET status = %s, updated_at = now()
                    WHERE reservation_id = %s
                    """
                ).format(schema),
                (상태, reservation_id),
            )

    전체 = tuple(allocation_id_for(reservation_id=reservation_id, lot_id=lot_id) for lot_id in 묶음)
    return AllocationResult(
        applied=applied,
        allocation_ids=전체,
        reservation_status=상태,
        allocated_qty_kg=이미할당,
    )


def _reservation_status_for(예약: Mapping[str, Any], *, allocated: Decimal) -> ReservationStatus:
    """할당 진행도로 예약 상태를 정한다. **어휘는 DB 것 그대로다.**"""
    if allocated <= 0:
        return "RESERVED"
    if allocated >= Decimal(예약["required_qty_kg"]):
        return "ALLOCATED"
    return "PARTIALLY_ALLOCATED"


# ── 실출고 ──────────────────────────────────────────────────────────────


def ship_allocated_stock(
    conn: Any,
    *,
    reservation_id: str,
    shipped_at: date,
    sale_item_id: str | None = None,
) -> ShipmentResult:
    """할당된 몫을 **실제로 내보낸다.** 여기서 처음 원장 OUT 이 나간다.

    ```text
    할당마다  record_inventory_move(OUT, lot_id, allocated_qty_kg, SALE_FULFILLMENT)
              → 그 Lot 의 remaining_qty_kg 가 줄어든다
    그다음    할당 status = SHIPPED
    ```

    🔴 **잔량 UPDATE 를 복제하지 않는다.** `remaining_qty_kg` 를 바꾸는 것은 원장뿐이고
       (`ledger.py` 가 존재하는 이유), 이 함수는 그것을 부르기만 한다.

    ★ **이미 `SHIPPED` 인 할당은 건너뛴다.** 재실행이 같은 Move 를 두 번 만들지 않는다
      (`move_id` 가 결정론이라 원장도 자체 멱등이지만, 여기서 먼저 거른다).

    ⚠️ **Shipment 표가 없다** — 실출고 사실은 `할당 SHIPPED + 원장 OUT` 으로 표현된다.
       새 표를 짓지 않는다 (모듈 docstring 참조).

    :param sale_item_id: `inventory_moves.sale_item_id` 에 그대로 실린다. **Sales 가
        소유한 참조**라 물류가 만들거나 뜯지 않는다. 아직 안 넘어오면 `None` 이다.
    """
    _require_text(reservation_id, 칸="reservation_id")
    schema = sql.Identifier(get_db_schema())

    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    예약 = _reservation(conn, schema, reservation_id=reservation_id)
    if 예약 is None:
        raise OutboundIntegrityError(f"출고할 예약이 없다: {reservation_id!r}")

    내보낼것 = [
        행
        for 행 in _allocations(conn, schema, reservation_id=reservation_id)
        if 행["status"] in _HOLDING_ALLOCATION
    ]
    if not 내보낼것:
        # ★ 이미 다 나갔거나 할당이 없다 — 재실행의 정상 경로다.
        return ShipmentResult(
            applied=False, shipped_allocation_ids=(), move_ids=(), shipped_qty_kg=Decimal(0)
        )

    move_ids: list[str] = []
    보낸것: list[str] = []
    총량 = Decimal(0)
    for 행 in 내보낼것:
        allocation_id = 행["allocation_id"]
        수량 = Decimal(행["allocated_qty_kg"])
        move_id = move_id_for_allocation(allocation_id=allocation_id)
        # 🔴 **원장이 잔량을 줄인다.** 여기서 UPDATE 를 따로 쓰지 않는다.
        record_inventory_move(
            conn,
            move_id=move_id,
            sim_run_id=예약["sim_run_id"],
            lot_id=행["lot_id"],
            move_type="OUT",
            quantity_kg=수량,
            moved_at=shipped_at,
            reason_code=_OUT_REASON_CODE,
            sale_item_id=sale_item_id,
        )
        move_ids.append(move_id)
        보낸것.append(allocation_id)
        총량 += 수량

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.inventory_allocations
                SET status = 'SHIPPED'
                WHERE allocation_id = ANY(%s)
                """
            ).format(schema),
            (보낸것,),
        )
    return ShipmentResult(
        applied=True,
        shipped_allocation_ids=tuple(보낸것),
        move_ids=tuple(move_ids),
        shipped_qty_kg=총량,
    )
