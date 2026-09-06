"""warehouse.py — Pallet · Location · Zone Capacity (3-E1).

```text
Lot  → place_lot_on_pallet   Pallet 한 장을 만들어 자리에 앉힌다   CREATED
     → move_pallet           자리를 옮긴다 (수량은 안 건드린다)     RELOCATED · HOLD_MOVED
     → empty_pallet          다 나간 Pallet 을 치워 자리를 돌려준다  EMPTIED
     → get_lot_position      이 Lot 이 지금 어디에 몇 장 있나
     → get_zone_capacity     Zone 에 자리가 몇 개 남았나
```

★ **사실 하나에 함수 하나다.** `pallet_events` 어휘를 아무 함수나 쓰지 못하게 나눴다 —
  `move_pallet` 에 `CREATED` 를 주면 거절한다. 한 함수가 남의 사실을 대신 적으면
  이력만 보고는 무슨 일이 있었는지 알 수 없게 된다.

🔴 **재고 수량의 정본은 여기가 아니다.**

```text
수량 정본   inventory_lots.remaining_qty_kg + inventory_moves    ← ledger.py
물리 배치   pallets · storage_locations · pallet_events          ← 이 파일
```

  ⚠️ 그래서 이 파일은 **원장 Move 를 만들지 않고 `remaining_qty_kg` 도 바꾸지 않는다.**
     Pallet 을 만들거나 옮기는 것은 *"같은 물건이 어디 있나"* 이지 *"물건이 늘거나
     줄었나"* 가 아니다. `pallet_events` 주석이 그렇게 못박고 있다 —
     *"수량변동 없는 Pallet 위치이동 이력. 수량이 바뀌는 것은 inventory_moves 쪽이다."*

🔴 **Zone Capacity 의 단위는 kg 이 아니라 Pallet Position 이다.**

```text
storage_locations  한 행 = Pallet 한 자리   (DDL 주석: "한 행 = Pallet 한 자리다")
pallets            uq_pallets_location UNIQUE (current_location_id)
                   → 한 자리에 Pallet 은 하나
Zone 정원   = 그 Zone 의 is_active 자리 수
Zone 점유   = 그 자리에 앉은 ACTIVE·HOLD Pallet 수
```

  ★ **kg 을 자리 수로 바꾸는 정본은 `item_packaging_specs.default_kg_per_pallet` 이다**
    (DDL 주석: *"kg → Pallet Position 환산의 정본이다"*). 코드에 *"1 Pallet = 500kg"*
    같은 숫자를 만들지 않는다.

  ⚠️ **`pallets` 에 수량 칸이 없다.** DDL 주석: *"Pallet 별 현재 수량은 저장하지 않고
     Move Line 에서 계산한다."* 그래서 *"이 Pallet 에 60kg"* 을 적을 자리가 없고,
     Lot 의 과다 배치는 **자리 수**로 막는다 — 스키마가 세는 단위 그대로다.

🔴 **두 Zone 축을 섞지 않는다.** 실측(2026-09-05)이 둘이 다른 축임을 보인다.

```text
inventory_lots.storage_zone  ← item_storage_policies.storage_zone
   COLD_HUMID_0_3 · COLD_DRY_0 · COLD_HUMID_0_4 · FROZEN_DRY_-3 · COLD_DRY_0_1   (5품목)

warehouse_zones.zone_id      ← item_zone_assignments.zone_id
   HIGH_HUMIDITY_COLD · ONION_COOL_DRY · HOLD_QUARANTINE
   · OUTBOUND_STAGING · RECEIVING_INSPECTION                                     (3품목)
```

  ⚠️ 둘을 잇는 표가 **없다.** 이름이 비슷해 보여도(`COLD_*` ↔ `*_COLD`) 5:5 로 맞지도
     않고 품목 커버리지도 다르다. 그래서 **문자열로 `zone_id` 를 추측하지 않는다** —
     허용 Zone 은 `item_zone_assignments` 가 명시할 때만 안다.

  ⚠️ **정책 없는 품목을 아무 Zone 에나 넣지 않는다.** `ITEM-GEONGOCHU` ·
     `ITEM-PIMANUL` 은 배정이 아예 없다(실측). 그런 품목은 `ZonePolicyUnresolved`
     로 멈춘다 — 기본 Zone 을 지어내면 온습도가 틀린 자리에 물건이 들어간다.

🔴 **잠금 순서 계약.** 배치 전용 전역 키 하나를 더한다.

```text
(20260905, 1)  재고 원장 쓰기      ledger.py
(20260905, 2)  도착 Receipt 쓰기   receipts.py
(20260905, 3)  출고 예약·할당 쓰기  outbound.py
(20260905, 4)  Pallet 배치 쓰기    이 파일        ← 실측 확인 후 빈 키를 골랐다
```

```text
① 배치 전역 (20260905, 4)   ← 가장 먼저
② 자리·정원 **재계산**        잠금 밖에서 본 값을 믿지 않는다
③ pallets / pallet_events 쓰기
④ 커밋은 호출자가 한 번
```

  ★ **원장 키 `(…,1)` 을 잡지 않는다.** 이 파일은 Move 를 안 만들어서 잡을 일이 없고,
    그래서 다른 경로와 교착할 자원이 아예 없다.

🔴 **재고가 0 이 돼도 자리는 저절로 안 비운다.**

```text
OUT · DISPOSE   →  remaining_qty_kg 를 0 으로 만든다      (원장이 하는 일)
empty_pallet    →  자리를 돌려준다                        (사람이 부르는 일)
```

  ⚠️ `confirm_disposal` 도 `ship_allocated_stock` 도 `empty_pallet` 을 부르지 않는다.
     한 Lot 이 여러 장에 나뉘어 있으면 **어느 장을 실제로 치웠는지** 원장이 알지 못한다.

⚠️ **기존 입고·출고를 강제하지 않는다.** 입고는 지금도 Pallet 없이
   `Receipt → Inspection → Lot → Ledger IN → PUTAWAY_DONE` 로 끝나고, 할당의
   `pallet_id` 는 여전히 NULL 로 둔다. 배치는 **뒤따르는 별도 단계**다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "EmptyResult",
    "InvalidPlacementRequest",
    "LotPosition",
    "PalletNotEmptyable",
    "PlacementConflict",
    "PlacementResult",
    "SpecItemMismatch",
    "WarehouseError",
    "WarehouseIntegrityError",
    "ZoneCapacity",
    "ZonePolicyUnresolved",
    "empty_pallet",
    "get_lot_position",
    "get_zone_capacity",
    "lock_warehouse_writes",
    "move_pallet",
    "place_lot_on_pallet",
    "required_pallet_count",
]


#: `ck_pallets_status` 어휘 그대로다.
PalletStatus = Literal["ACTIVE", "HOLD", "EMPTIED", "DISPOSED"]
#: `ck_pallet_events_type` 어휘 그대로다.
PalletEventType = Literal["CREATED", "PUTAWAY", "RELOCATED", "HOLD_MOVED", "EMPTIED"]

_PALLET_EVENT_TYPES: frozenset[str] = frozenset(get_args(PalletEventType))

#: 🔴 **`move_pallet` 이 받는 어휘는 DB 어휘보다 좁다.** 이벤트 vocabulary 와 함수
#: responsibility 는 다른 것이다.
#:
#: ```text
#: CREATED   place_lot_on_pallet 전용   — 자리 배정과 함께 나온다
#: EMPTIED   empty_pallet 전용          — 자리를 비우는 것은 이동이 아니다
#: PUTAWAY   ❌ 이번 판에 경로가 없다     — 배치가 CREATED 로 한 번에 끝나서
#:                                       PUTAWAY 가 따로 뜻할 일이 아직 없다
#: ```
#:
#: ⚠️ 없는 뜻을 억지로 만들지 않는다. 검수 Zone → 보관 Zone 이동이 필요하면 그것은
#:    `RELOCATED` 다.
MoveEventType = Literal["RELOCATED", "HOLD_MOVED"]

_MOVE_EVENT_TYPES: frozenset[str] = frozenset(get_args(MoveEventType))

#: 🔴 **자리를 차지하는** Pallet 상태. `ck_pallets_location_matches_status` 가
#: 이 둘에만 `current_location_id` 를 허용한다 — 정원 계산과 같은 집합이어야 한다.
_OCCUPYING_PALLET: frozenset[str] = frozenset({"ACTIVE", "HOLD"})

#: 🔴 배치 쓰기 전역 잠금. `(…,1)` 원장 · `(…,2)` 도착 · `(…,3)` 출고와 겹치지 않는다.
_WAREHOUSE_LOCK_CLASSID = 20260905
_WAREHOUSE_LOCK_OBJID = 4

_AMBIGUITY_PROBE_LIMIT = 2


class WarehouseError(RuntimeError):
    """이 모듈이 내는 실패의 조상."""


class InvalidPlacementRequest(WarehouseError, ValueError):
    """요청이 DB 계약이나 Zone·Capacity 규칙을 어긴다. **DML 전에 막는다.**"""


class ZonePolicyUnresolved(WarehouseError, ValueError):
    """이 품목의 허용 Zone 을 **모른다.**

    🔴 모른다는 것과 *"아무 데나 된다"* 는 다르다. 기본 Zone 을 지어내지 않는다.
    """


class PlacementConflict(WarehouseError, ValueError):
    """같은 `pallet_id` 에 **다른 사실**의 배치가 이미 있다. 조용히 덮지 않는다."""


class SpecItemMismatch(WarehouseError, ValueError):
    """건네받은 포장규격이 **다른 품목의 것**이다.

    🔴 FK 는 규격의 *존재*만 보장한다. `item_packaging_specs.item_id` 가 Lot 의 품목과
       같은지는 아무도 안 본다 — 그래서 배추 Lot 에 양파 규격이 붙을 수 있다.
    """


class PalletNotEmptyable(WarehouseError, ValueError):
    """이 Pallet 을 비워도 된다는 **수량 근거가 없다.**

    🔴 남은 물량을 추측해서 비우지 않는다. 비운 자리는 다른 Pallet 이 즉시 차지한다.
    """


class WarehouseIntegrityError(WarehouseError, ValueError):
    """배치 데이터가 스스로를 배반한다. **조용히 고치지 않는다.**"""


@dataclass(frozen=True)
class PlacementResult:
    applied: bool
    pallet_id: str
    location_id: str
    zone_id: str
    status: PalletStatus


@dataclass(frozen=True)
class EmptyResult:
    """Pallet 을 비운 결과. **수량은 여기 없다.**"""

    applied: bool
    pallet_id: str
    #: 비우기 **전에** 앉아 있던 자리. 비운 뒤에는 `NULL` 이라 결과로만 남는다.
    freed_location_id: str | None
    zone_id: str | None
    status: PalletStatus


@dataclass(frozen=True)
class LotPosition:
    """이 Lot 이 지금 앉아 있는 자리 한 줄."""

    pallet_id: str
    location_id: str
    zone_id: str
    status: PalletStatus


@dataclass(frozen=True)
class ZoneCapacity:
    """Zone 의 자리 사정. **단위는 Pallet Position 이다.**"""

    zone_id: str
    zone_kind: str
    #: `is_active` 인 자리 수. 정원이다.
    total_positions: int
    #: 그 자리에 앉은 `ACTIVE`·`HOLD` Pallet 수.
    occupied_positions: int
    free_positions: int


# ── 순수 도우미 ─────────────────────────────────────────────────────────


def required_pallet_count(*, quantity_kg: Decimal, kg_per_pallet: Decimal) -> int:
    """이 수량을 담는 데 필요한 **자리 수**. 순수 계산이다.

    ```text
    ceil(quantity_kg / kg_per_pallet)
    ```

    ★ 올림이다 — 350kg 짜리 Pallet 에 400kg 을 담으려면 두 자리가 필요하다.
      반올림하면 마지막 자투리가 갈 곳을 잃는다.
    """
    수량 = _quantity(quantity_kg, 칸="quantity_kg")
    단위 = _quantity(kg_per_pallet, 칸="kg_per_pallet")
    return math.ceil(수량 / 단위)


def _require_text(값: Any, *, 칸: str) -> str:
    if not isinstance(값, str) or not 값.strip():
        raise InvalidPlacementRequest(f"{칸} 가 비었다: {값!r}")
    return 값


def _quantity(값: Any, *, 칸: str) -> Decimal:
    """수량을 `Decimal` 로 좁힌다. **float 도 비유한값도 받지 않는다.**

    ★ `ledger._quantity` · `outbound._quantity` 와 같은 규율이다.
    """
    if isinstance(값, bool) or not isinstance(값, Decimal):
        raise InvalidPlacementRequest(
            f"{칸} 은 Decimal 이어야 한다 (받은 것: {값!r} · {type(값).__name__})."
        )
    if not 값.is_finite():
        raise InvalidPlacementRequest(f"{칸} 이 유한한 수가 아니다: {값!r}")
    if 값 <= 0:
        raise InvalidPlacementRequest(f"{칸} 은 0보다 커야 한다 (받은 것: {값})")
    return 값


def lock_warehouse_writes(cursor: Any) -> None:
    """Pallet 배치 쓰기를 **하나의 전역 잠금으로 직렬화한다.**

    🔴 **이 잠금이 자리 경합을 닫는다.**

    ```text
    잠금 없이   T1·T2 가 같은 빈 자리를 보고 각자 Pallet 을 앉힌다
                → uq_pallets_location 이 뒤늦게 터진다 (UniqueViolation 을 흐름으로 쓰지 않는다)
    잠금 있으면 T2 는 T1 이 끝난 뒤 **다시 세고** 그 자리가 찬 것을 본다
    ```

    ★ transaction-level 이라 호출자의 커밋/롤백과 함께 풀린다. unlock 을 부르지 않는다.
    """
    cursor.execute(
        sql.SQL("SELECT pg_advisory_xact_lock(%s, %s)"),
        (_WAREHOUSE_LOCK_CLASSID, _WAREHOUSE_LOCK_OBJID),
    )


def _cell(row: Any, index: int, name: str) -> Any:
    return row[name] if isinstance(row, dict) else row[index]


def _scalar(cursor: Any, name: str) -> Any:
    """`count(*)` 처럼 **반드시 한 줄 한 칸**인 집계를 읽는다.

    ★ 집계는 0행이 나올 수 없어서 `_one` 의 0/1/2+ 규율을 적용할 자리가 아니다.
    """
    행 = cursor.fetchall()[0]
    return _cell(행, 0, name)


def _one(rows: Any, *, 무엇: str) -> Any:
    """0/1/2+ 를 **셋 다 다르게** 다룬다. `fetchone()` 을 쓰지 않는다.

    🔴 첫 행을 집어오면 둘 이상인 것을 영영 모른다.
    """
    목록 = list(rows)
    if not 목록:
        return None
    if len(목록) > 1:
        raise WarehouseIntegrityError(
            f"{무엇} 이 둘 이상이다 ({len(목록)}건). 하나를 고르지 않는다."
        )
    return 목록[0]


# ── 읽기 ────────────────────────────────────────────────────────────────


def _load_location(cursor: Any, schema: sql.Identifier, *, location_id: str) -> Any:
    """자리 한 줄 + 그 자리가 속한 Zone. **없으면 `None` 을 돌려준다.**"""
    cursor.execute(
        sql.SQL(
            """
            SELECT sl.location_id, sl.zone_id, sl.is_active, sl.location_kind,
                   wz.zone_kind, wz.is_active AS zone_active
            FROM {}.storage_locations AS sl
            JOIN {}.warehouse_zones AS wz ON wz.zone_id = sl.zone_id
            WHERE sl.location_id = %s
            LIMIT %s
            """
        ).format(schema, schema),
        (location_id, _AMBIGUITY_PROBE_LIMIT),
    )
    return _one(cursor.fetchall(), 무엇=f"자리 {location_id!r}")


def _load_pallet(cursor: Any, schema: sql.Identifier, *, pallet_id: str) -> Any:
    cursor.execute(
        sql.SQL(
            """
            SELECT pallet_id, lot_id, packaging_spec_id, current_location_id, status
            FROM {}.pallets
            WHERE pallet_id = %s
            LIMIT %s
            """
        ).format(schema),
        (pallet_id, _AMBIGUITY_PROBE_LIMIT),
    )
    return _one(cursor.fetchall(), 무엇=f"Pallet {pallet_id!r}")


def _load_lot(cursor: Any, schema: sql.Identifier, *, sim_run_id: str, lot_id: str) -> Any:
    cursor.execute(
        sql.SQL(
            """
            SELECT lot_id, item_id, remaining_qty_kg, status
            FROM {}.inventory_lots
            WHERE lot_id = %s AND sim_run_id = %s
            LIMIT %s
            """
        ).format(schema),
        (lot_id, sim_run_id, _AMBIGUITY_PROBE_LIMIT),
    )
    return _one(cursor.fetchall(), 무엇=f"Lot {lot_id!r}")


def _kg_per_pallet(cursor: Any, schema: sql.Identifier, *, item_id: str) -> Decimal | None:
    """이 품목의 kg → 자리 환산 단위. **정본은 `item_packaging_specs` 하나다.**

    ```text
    None      환산 정본이 없다 (UNRESOLVED)   → 자리 수 상한을 못 센다
    Decimal   is_default 규격의 값
    ```

    ⚠️ 기본 규격이 없으면 **다른 규격을 아무거나 집지 않는다** — 부분 UNIQUE
       `uq_item_packaging_specs_default` 가 기본을 하나로 못박아 둔 이유가 그것이다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT default_kg_per_pallet
            FROM {}.item_packaging_specs
            WHERE item_id = %s AND is_default
            LIMIT %s
            """
        ).format(schema),
        (item_id, _AMBIGUITY_PROBE_LIMIT),
    )
    행 = _one(cursor.fetchall(), 무엇=f"품목 {item_id!r} 의 기본 포장규격")
    return None if 행 is None else Decimal(str(_cell(행, 0, "default_kg_per_pallet")))


def _zone_allowed(
    cursor: Any, schema: sql.Identifier, *, item_id: str, zone_id: str
) -> bool | None:
    """이 품목을 이 Zone 에 둘 수 있나. **세 상태를 구분한다.**

    ```text
    None    이 품목의 Zone 정책이 아예 없다   → UNRESOLVED. 추측하지 않는다
    False   정책이 있고 이 Zone 은 금지다
    True    정책이 있고 이 Zone 은 허용이다
    ```

    🔴 *정책 없음* 과 *금지* 를 같은 값으로 뭉개면, 정책을 안 만든 품목이
       *"모든 Zone 금지"* 로 보이거나 그 반대가 된다.
    """
    cursor.execute(
        sql.SQL("SELECT count(*) FROM {}.item_zone_assignments WHERE item_id = %s").format(schema),
        (item_id,),
    )
    정책수 = _scalar(cursor, "count")
    if not 정책수:
        return None
    cursor.execute(
        sql.SQL(
            """
            SELECT allowed FROM {}.item_zone_assignments
            WHERE item_id = %s AND zone_id = %s
            LIMIT %s
            """
        ).format(schema),
        (item_id, zone_id, _AMBIGUITY_PROBE_LIMIT),
    )
    행 = _one(cursor.fetchall(), 무엇=f"품목 {item_id!r} · Zone {zone_id!r} 배정")
    # ★ 정책은 있는데 이 Zone 줄이 없다 = 열거되지 않은 Zone = 금지다.
    return False if 행 is None else bool(_cell(행, 0, "allowed"))


def _occupying_pallet_count(
    cursor: Any, schema: sql.Identifier, *, lot_id: str, 제외: str | None = None
) -> int:
    """이 Lot 이 지금 차지한 자리 수. `제외` 는 재실행 중인 자기 Pallet 이다."""
    cursor.execute(
        sql.SQL(
            """
            SELECT count(*) FROM {}.pallets
            WHERE lot_id = %s AND status = ANY(%s)
              AND (%s::text IS NULL OR pallet_id <> %s)
            """
        ).format(schema),
        (lot_id, sorted(_OCCUPYING_PALLET), 제외, 제외),
    )
    return int(_scalar(cursor, "count"))


def get_zone_capacity(conn: Any, *, zone_id: str) -> ZoneCapacity:
    """Zone 의 자리 사정. **kg 이 아니라 Pallet Position 으로 센다.**

    ```text
    total    = is_active 자리 수
    occupied = 그 자리에 앉은 ACTIVE·HOLD Pallet 수
    ```

    ⚠️ **판매 가용재고와 다른 축이다.** 폐기대기 Lot 도, 예약·할당된 Lot 도 실제로
       창고에 있으면 자리를 차지한다. 자리가 비는 것은 원장 OUT·DISPOSE 뒤에 사람이
       Pallet 을 비웠을 때(`EMPTIED`)다.

    ⚠️ **비활성 자리는 정원에서 뺀다.** 하지만 거기 앉은 Pallet 은 점유로 센다 —
       자리를 닫았다고 물건이 사라지지는 않는다. 점유가 정원을 넘으면 조용히 0 으로
       깎지 않고 `WarehouseIntegrityError` 로 멈춘다.
    """
    schema = sql.Identifier(get_db_schema())
    _require_text(zone_id, 칸="zone_id")
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT zone_id, zone_kind FROM {}.warehouse_zones WHERE zone_id = %s LIMIT %s"
            ).format(schema),
            (zone_id, _AMBIGUITY_PROBE_LIMIT),
        )
        zone = _one(cursor.fetchall(), 무엇=f"Zone {zone_id!r}")
        if zone is None:
            raise InvalidPlacementRequest(f"없는 Zone 이다: {zone_id!r}")
        cursor.execute(
            sql.SQL(
                "SELECT count(*) FROM {}.storage_locations WHERE zone_id = %s AND is_active"
            ).format(schema),
            (zone_id,),
        )
        정원 = int(_scalar(cursor, "count"))
        cursor.execute(
            sql.SQL(
                """
                SELECT count(*)
                FROM {}.pallets AS p
                JOIN {}.storage_locations AS sl ON sl.location_id = p.current_location_id
                WHERE sl.zone_id = %s AND p.status = ANY(%s)
                """
            ).format(schema, schema),
            (zone_id, sorted(_OCCUPYING_PALLET)),
        )
        점유 = int(_scalar(cursor, "count"))
    if 점유 > 정원:
        raise WarehouseIntegrityError(
            f"Zone 점유가 정원을 넘는다 (zone_id={zone_id!r}): 정원 {정원} · 점유 {점유}."
            " 자리를 닫았는데 Pallet 이 남아 있는지 확인한다."
        )
    return ZoneCapacity(
        zone_id=str(_cell(zone, 0, "zone_id")),
        zone_kind=str(_cell(zone, 1, "zone_kind")),
        total_positions=정원,
        occupied_positions=점유,
        free_positions=정원 - 점유,
    )


def get_lot_position(conn: Any, *, sim_run_id: str, lot_id: str) -> tuple[LotPosition, ...]:
    """이 Lot 이 지금 어디에 있나. **한 Lot 이 여러 자리에 나뉠 수 있다.**

    ★ 스키마가 `1 Lot : N Pallet` 을 허용하고 `1 Pallet : 1 Lot` 만 막는다
      (`pallets.lot_id` 는 단일 값이다). 그래서 부분 Pallet 은 되고, 한 Pallet 에
      두 Lot 을 섞는 것은 안 된다. 관계를 임의로 넓히지 않는다.

    ★ 자리를 안 차지하는 `EMPTIED`·`DISPOSED` Pallet 은 빼고 돌려준다 — *"지금 어디"*
      를 묻는 질문이라 비운 Pallet 은 답이 아니다.
    """
    schema = sql.Identifier(get_db_schema())
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(lot_id, 칸="lot_id")
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT p.pallet_id, p.current_location_id, sl.zone_id, p.status
                FROM {}.pallets AS p
                JOIN {}.inventory_lots AS il ON il.lot_id = p.lot_id
                JOIN {}.storage_locations AS sl ON sl.location_id = p.current_location_id
                WHERE p.lot_id = %s AND il.sim_run_id = %s AND p.status = ANY(%s)
                ORDER BY p.pallet_id
                """
            ).format(schema, schema, schema),
            (lot_id, sim_run_id, sorted(_OCCUPYING_PALLET)),
        )
        행들 = list(cursor.fetchall())
    return tuple(
        LotPosition(
            pallet_id=str(_cell(행, 0, "pallet_id")),
            location_id=str(_cell(행, 1, "current_location_id")),
            zone_id=str(_cell(행, 2, "zone_id")),
            status=str(_cell(행, 3, "status")),  # type: ignore[arg-type]
        )
        for 행 in 행들
    )


# ── 쓰기 ────────────────────────────────────────────────────────────────


def _check_zone(
    cursor: Any, schema: sql.Identifier, *, item_id: str, zone_id: str, location_id: str
) -> None:
    """이 자리의 Zone 이 이 품목에 허용되는지 본다. **모르면 멈춘다.**"""
    허용 = _zone_allowed(cursor, schema, item_id=item_id, zone_id=zone_id)
    if 허용 is None:
        raise ZonePolicyUnresolved(
            f"이 품목의 허용 Zone 을 모른다 (item_id={item_id!r}):"
            f" {'item_zone_assignments'} 에 줄이 없다."
            " 🔴 기본 Zone 을 지어내지 않는다 — 온습도가 틀린 자리에 물건이 들어간다."
            " inventory_lots.storage_zone 문자열로 zone_id 를 유추하지도 않는다"
            " (둘을 잇는 권위 있는 표가 없다)."
        )
    if not 허용:
        raise InvalidPlacementRequest(
            f"이 품목에 금지된 Zone 이다 (item_id={item_id!r} · zone_id={zone_id!r}"
            f" · location_id={location_id!r})."
            " 예외 배치가 필요하면 zone_override_approvals 로 승인을 남긴다."
        )


def _check_packaging_spec(
    cursor: Any, schema: sql.Identifier, *, packaging_spec_id: str, item_id: str, lot_id: str
) -> None:
    """건네받은 포장규격이 **이 Lot 의 품목 것**인지 본다. DML 전에 막는다.

    🔴 **FK 는 존재만 보장한다.** `pallets.packaging_spec_id → item_packaging_specs` 는
       그 규격이 있느냐만 볼 뿐, 그것이 **누구의 규격인지**는 안 본다. 그래서 배추 Lot 에
       양파 규격(450kg/PLT)이 붙을 수 있고, 그러면 자리 수 환산이 조용히 틀린다.

    ⚠️ **규격을 임의로 바꾸거나 기본값으로 대체하지 않는다.** 호출자가 잘못 준 것을
       말없이 고치면 다음에 같은 실수가 또 온다.

    ★ `packaging_spec_id=None` 은 스키마가 허용하는 상태라 그대로 둔다 — 규격을 아직
      안 정한 것과 틀린 규격을 준 것은 다르다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT packaging_spec_id, item_id FROM {}.item_packaging_specs
            WHERE packaging_spec_id = %s
            LIMIT %s
            """
        ).format(schema),
        (packaging_spec_id, _AMBIGUITY_PROBE_LIMIT),
    )
    규격 = _one(cursor.fetchall(), 무엇=f"포장규격 {packaging_spec_id!r}")
    if 규격 is None:
        raise SpecItemMismatch(
            f"없는 포장규격이다: {packaging_spec_id!r}. FK 가 뒤늦게 터지기 전에 막는다."
        )
    규격품목 = str(_cell(규격, 1, "item_id"))
    if 규격품목 != item_id:
        raise SpecItemMismatch(
            f"다른 품목의 포장규격이다 (packaging_spec_id={packaging_spec_id!r}):"
            f" 규격은 {규격품목!r} 것인데 Lot({lot_id!r}) 은 {item_id!r} 이다."
            " 🔴 규격을 임의로 바꾸거나 기본값으로 대체하지 않는다."
        )


def _check_free_position(cursor: Any, schema: sql.Identifier, *, location_id: str) -> None:
    """그 자리가 비어 있나. **`uq_pallets_location` 이 터지기 전에 막는다.**

    🔴 UniqueViolation 을 정상 흐름으로 쓰지 않는다 — 잡아서 흐름을 만들면 어떤 제약이
       터졌는지 구분하지 못하고 트랜잭션도 이미 더러워진다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT pallet_id FROM {}.pallets
            WHERE current_location_id = %s
            LIMIT %s
            """
        ).format(schema),
        (location_id, _AMBIGUITY_PROBE_LIMIT),
    )
    앉은것 = _one(cursor.fetchall(), 무엇=f"자리 {location_id!r} 의 Pallet")
    if 앉은것 is not None:
        raise InvalidPlacementRequest(
            f"이미 찬 자리다 (location_id={location_id!r}):"
            f" {_cell(앉은것, 0, 'pallet_id')!r} 가 앉아 있다."
            " 한 자리에 Pallet 은 하나다 (uq_pallets_location)."
        )


def _usable_location(cursor: Any, schema: sql.Identifier, *, location_id: str) -> Any:
    자리 = _load_location(cursor, schema, location_id=location_id)
    if 자리 is None:
        raise InvalidPlacementRequest(
            f"없는 자리다: {location_id!r}."
            " 🔴 이름으로 자리를 유추하지 않는다 — 호출자가 location_id 를 준다."
        )
    if not _cell(자리, 2, "is_active"):
        raise InvalidPlacementRequest(f"닫힌 자리다 (location_id={location_id!r}).")
    if not _cell(자리, 5, "zone_active"):
        raise InvalidPlacementRequest(
            f"닫힌 Zone 의 자리다 (location_id={location_id!r}"
            f" · zone_id={_cell(자리, 1, 'zone_id')!r})."
        )
    return 자리


def _record_pallet_event(
    cursor: Any,
    schema: sql.Identifier,
    *,
    pallet_id: str,
    event_type: str,
    from_location_id: str | None,
    to_location_id: str | None,
    occurred_at: datetime,
    recorded_by: str,
    note: str | None,
) -> None:
    """위치이동 이력 한 줄. **수량은 여기에 없다.**"""
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {}.pallet_events (
                pallet_id, event_type, from_location_id, to_location_id,
                occurred_at, recorded_by, note
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
        ).format(schema),
        (pallet_id, event_type, from_location_id, to_location_id, occurred_at, recorded_by, note),
    )


def place_lot_on_pallet(
    conn: Any,
    *,
    pallet_id: str,
    sim_run_id: str,
    lot_id: str,
    location_id: str,
    occurred_at: datetime,
    recorded_by: str,
    packaging_spec_id: str | None = None,
    note: str | None = None,
) -> PlacementResult:
    """Lot 을 Pallet 한 장에 올려 자리에 앉힌다. **재고는 1g 도 움직이지 않는다.**

    ```text
    ① 입력 검증                       DB 를 안 만진다
    ② 배치 전역 잠금                   자리 경합을 닫는다
    ③ 같은 Pallet 이 이미 있나          있으면 사실 대조 후 applied=False
    ④ Lot · 자리 · Zone 확인            모르는 Zone 정책이면 멈춘다
    ⑤ 자리 수 한도                     ceil(remaining / kg_per_pallet)
    ⑥ pallets INSERT + pallet_events CREATED
    ```

    🔴 **`INSERT ... pallets` 는 자리를 반드시 함께 받는다.** 스키마의
       `ck_pallets_location_matches_status` 가 *"살아 있는 Pallet 은 자리가 있다"* 를
       강제한다 — 그래서 *"Pallet 만 만들고 나중에 앉히기"* 라는 중간 상태가 없다.
       두 단계로 나누면 DB 가 거절한다.

    ⚠️ **원장을 만들지 않는다.** `inventory_moves` · `inventory_move_lines` ·
       `remaining_qty_kg` 어느 것도 건드리지 않는다.

    ```text
    Lot remaining 100kg  →  Pallet A · Pallet B 에 나눠 앉힘
                        →  Lot remaining 여전히 100kg
    ```

    ★ **부분 Pallet 은 허용이다.** 한 Lot 이 여러 Pallet 에 나뉠 수 있다. 반대로 한
      Pallet 에 두 Lot 을 섞는 것은 `pallets.lot_id` 가 단일 값이라 스키마가 막는다.

    :param pallet_id: 배치의 정체성. **호출자가 준다** — 한 Lot 이 여러 장으로 나뉘어
        `lot_id` 로는 가를 수 없다.
    :param recorded_by: `pallet_events.recorded_by` 가 NOT NULL 이다. 물류가 지어내지 않는다.
    :raises ZonePolicyUnresolved: 이 품목의 허용 Zone 을 모를 때.
    :raises InvalidPlacementRequest: 자리·Zone·자리 수 한도를 어길 때.
    :raises PlacementConflict: 같은 `pallet_id` 에 다른 사실이 있을 때.
    """
    schema = sql.Identifier(get_db_schema())
    # ── ① 검증 ────────────────────────────────────────────────────────
    _require_text(pallet_id, 칸="pallet_id")
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(lot_id, 칸="lot_id")
    _require_text(location_id, 칸="location_id")
    _require_text(recorded_by, 칸="recorded_by")
    if not isinstance(occurred_at, datetime):
        raise InvalidPlacementRequest(f"occurred_at 은 datetime 이어야 한다: {occurred_at!r}")

    with conn.cursor() as cursor:
        # ── ② 잠금 ────────────────────────────────────────────────────
        lock_warehouse_writes(cursor)

        # ── ③ 재실행인가 ──────────────────────────────────────────────
        기존 = _load_pallet(cursor, schema, pallet_id=pallet_id)
        if 기존 is not None:
            _assert_same_placement(
                기존,
                pallet_id=pallet_id,
                lot_id=lot_id,
                location_id=location_id,
                packaging_spec_id=packaging_spec_id,
            )
            자리 = _usable_location(cursor, schema, location_id=location_id)
            return PlacementResult(
                applied=False,
                pallet_id=pallet_id,
                location_id=location_id,
                zone_id=str(_cell(자리, 1, "zone_id")),
                status=str(_cell(기존, 4, "status")),  # type: ignore[arg-type]
            )

        # ── ④ Lot · 자리 · Zone ───────────────────────────────────────
        lot = _load_lot(cursor, schema, sim_run_id=sim_run_id, lot_id=lot_id)
        if lot is None:
            raise InvalidPlacementRequest(
                f"없는 Lot 이다 (lot_id={lot_id!r} · sim_run_id={sim_run_id!r})."
            )
        item_id = str(_cell(lot, 1, "item_id"))
        remaining = Decimal(str(_cell(lot, 2, "remaining_qty_kg")))
        자리 = _usable_location(cursor, schema, location_id=location_id)
        zone_id = str(_cell(자리, 1, "zone_id"))
        _check_zone(cursor, schema, item_id=item_id, zone_id=zone_id, location_id=location_id)
        if packaging_spec_id is not None:
            _check_packaging_spec(
                cursor,
                schema,
                packaging_spec_id=packaging_spec_id,
                item_id=item_id,
                lot_id=lot_id,
            )
        _check_free_position(cursor, schema, location_id=location_id)

        # ── ⑤ 자리 수 한도 ────────────────────────────────────────────
        _check_pallet_budget(
            cursor, schema, lot_id=lot_id, item_id=item_id, remaining=remaining, 제외=None
        )

        # ── ⑥ 쓰기 ────────────────────────────────────────────────────
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.pallets (
                    pallet_id, lot_id, packaging_spec_id, current_location_id, status, note
                ) VALUES (%s, %s, %s, %s, 'ACTIVE', %s)
                """
            ).format(schema),
            (pallet_id, lot_id, packaging_spec_id, location_id, note),
        )
        _record_pallet_event(
            cursor,
            schema,
            pallet_id=pallet_id,
            event_type="CREATED",
            from_location_id=None,
            to_location_id=location_id,
            occurred_at=occurred_at,
            recorded_by=recorded_by,
            note=note,
        )
    return PlacementResult(
        applied=True,
        pallet_id=pallet_id,
        location_id=location_id,
        zone_id=zone_id,
        status="ACTIVE",
    )


def _assert_same_placement(
    기존: Any,
    *,
    pallet_id: str,
    lot_id: str,
    location_id: str,
    packaging_spec_id: str | None,
) -> None:
    """재실행이 **같은 사실**인지 본다. 다르면 덮지 않고 멈춘다."""
    이름 = ("lot_id", "current_location_id", "packaging_spec_id")
    있는값 = (
        _cell(기존, 1, "lot_id"),
        _cell(기존, 3, "current_location_id"),
        _cell(기존, 2, "packaging_spec_id"),
    )
    온값 = (lot_id, location_id, packaging_spec_id)
    다름 = [(칸, 있, 온) for 칸, 있, 온 in zip(이름, 있는값, 온값, strict=True) if 있 != 온]
    if 다름:
        상세 = " · ".join(f"{칸}: 기존 {있!r} ≠ 요청 {온!r}" for 칸, 있, 온 in 다름)
        raise PlacementConflict(
            f"같은 pallet_id 에 다른 배치가 이미 있다 (pallet_id={pallet_id!r}): {상세}."
            " 🔴 조용히 덮어쓰지 않는다 — 자리를 옮기려면 move_pallet 을 쓴다."
        )


def _check_pallet_budget(
    cursor: Any,
    schema: sql.Identifier,
    *,
    lot_id: str,
    item_id: str,
    remaining: Decimal,
    제외: str | None,
) -> None:
    """이 Lot 이 자리를 몇 개까지 차지할 수 있나.

    ```text
    한도 = ceil(remaining_qty_kg / default_kg_per_pallet)
    ```

    🔴 **kg 이 아니라 자리 수로 잰다.** `pallets` 에 수량 칸이 없어서(DDL 주석:
       *"Pallet 별 현재 수량은 저장하지 않고 Move Line 에서 계산한다"*) *"이 Pallet 에
       60kg"* 을 적을 데가 없다. 그래서 과다 배치는 스키마가 세는 단위 —
       **Pallet Position** — 으로 막는다.

    🔴 **빈 Lot 은 포장규격과 무관하게 먼저 막는다.** 잔량 0 은 *"창고에 없는 물건"*
       이라 몇 kg 씩 쌓는지와 상관없이 앉힐 자리가 없다. 이 검사를 환산 정본 뒤에 두면
       **규격이 없는 품목만 빈 Lot 을 자리에 다시 올릴 수 있게 된다** — 없는 재고가
       Capacity 를 먹는다.

    ⚠️ **환산 정본이 없으면 한도만 안 센다.** `item_packaging_specs` 에 기본 규격이
       없는 품목은 *"1 Pallet = 몇 kg"* 을 아무도 정하지 않았다는 뜻이다. 숫자를
       지어내는 대신 자리 수 상한을 건너뛴다 — 없는 근거로 배치를 막지도, 지어낸
       근거로 통과시키지도 않는다. 위의 잔량 검사는 그래도 지나야 한다.
    """
    if remaining <= 0:
        raise InvalidPlacementRequest(
            f"잔량이 없는 Lot 은 자리에 앉히지 않는다 (lot_id={lot_id!r}): remaining {remaining}."
            " 다 나간 Lot 의 자리는 empty_pallet 으로 비운다."
        )
    단위 = _kg_per_pallet(cursor, schema, item_id=item_id)
    if 단위 is None:
        return
    한도 = required_pallet_count(quantity_kg=remaining, kg_per_pallet=단위)
    이미 = _occupying_pallet_count(cursor, schema, lot_id=lot_id, 제외=제외)
    if 이미 + 1 > 한도:
        raise InvalidPlacementRequest(
            f"이 Lot 이 쓸 수 있는 자리 수를 넘는다 (lot_id={lot_id!r}):"
            f" 잔량 {remaining}kg ÷ {단위}kg/PLT → 한도 {한도}자리 · 이미 {이미}자리."
            " 🔴 자리를 더 쓰려면 잔량이 늘어야 한다 — 배치가 재고를 만들지 않는다."
        )


def move_pallet(
    conn: Any,
    *,
    pallet_id: str,
    to_location_id: str,
    occurred_at: datetime,
    recorded_by: str,
    event_type: MoveEventType = "RELOCATED",
    note: str | None = None,
) -> PlacementResult:
    """Pallet 을 다른 자리로 옮긴다. **수량도 원장도 건드리지 않는다.**

    ```text
    ① 입력 검증
    ② 배치 전역 잠금
    ③ 이미 그 자리면 applied=False    ← 멱등
    ④ 목적지 자리 · Zone 확인
    ⑤ pallets.current_location_id UPDATE + pallet_events
    ```

    ⚠️ **Zone 을 넘는 이동도 여기서 한다.** 목적지 Zone 이 이 품목에 허용인지 다시
       본다 — 출발지에서 허용이었다고 목적지에서도 허용인 것은 아니다.

    ⚠️ **자리를 비우는(`EMPTIED`) 경로는 여기 없다.** 그것은 잔량이 0 이 된 뒤의
       일이라 원장 쪽 사실과 맞물린다. 이번 판에서는 옮기기만 한다.

    :raises InvalidPlacementRequest: 없는 Pallet·자리, 찬 자리, 금지 Zone 일 때.
    :raises ZonePolicyUnresolved: 목적지 Zone 정책을 모를 때.
    """
    schema = sql.Identifier(get_db_schema())
    # ── ① 검증 ────────────────────────────────────────────────────────
    _require_text(pallet_id, 칸="pallet_id")
    _require_text(to_location_id, 칸="to_location_id")
    _require_text(recorded_by, 칸="recorded_by")
    if event_type not in _MOVE_EVENT_TYPES:
        raise InvalidPlacementRequest(
            f"이 함수가 낼 수 있는 event_type 이 아니다: {event_type!r}."
            f" move_pallet 은 {sorted(_MOVE_EVENT_TYPES)} 만 낸다."
            " 🔴 CREATED 는 place_lot_on_pallet 이, EMPTIED 는 empty_pallet 이 낸다 —"
            " 한 함수가 남의 사실을 대신 적지 않는다."
        )
    if not isinstance(occurred_at, datetime):
        raise InvalidPlacementRequest(f"occurred_at 은 datetime 이어야 한다: {occurred_at!r}")

    with conn.cursor() as cursor:
        # ── ② 잠금 ────────────────────────────────────────────────────
        lock_warehouse_writes(cursor)

        pallet = _load_pallet(cursor, schema, pallet_id=pallet_id)
        if pallet is None:
            raise InvalidPlacementRequest(f"없는 Pallet 이다: {pallet_id!r}.")
        상태 = str(_cell(pallet, 4, "status"))
        if 상태 not in _OCCUPYING_PALLET:
            raise InvalidPlacementRequest(
                f"자리에 없는 Pallet 은 옮기지 않는다 (pallet_id={pallet_id!r} · status={상태!r})."
            )
        현재 = _cell(pallet, 3, "current_location_id")

        # ── ③ 이미 그 자리인가 ────────────────────────────────────────
        자리 = _usable_location(cursor, schema, location_id=to_location_id)
        zone_id = str(_cell(자리, 1, "zone_id"))
        if 현재 == to_location_id:
            return PlacementResult(
                applied=False,
                pallet_id=pallet_id,
                location_id=to_location_id,
                zone_id=zone_id,
                status=상태,  # type: ignore[arg-type]
            )

        # ── ④ 목적지 검사 ─────────────────────────────────────────────
        lot_id = str(_cell(pallet, 1, "lot_id"))
        cursor.execute(
            sql.SQL("SELECT item_id FROM {}.inventory_lots WHERE lot_id = %s LIMIT %s").format(
                schema
            ),
            (lot_id, _AMBIGUITY_PROBE_LIMIT),
        )
        lot = _one(cursor.fetchall(), 무엇=f"Lot {lot_id!r}")
        if lot is None:
            raise WarehouseIntegrityError(
                f"Pallet 이 없는 Lot 을 가리킨다 (pallet_id={pallet_id!r} · lot_id={lot_id!r})."
            )
        _check_zone(
            cursor,
            schema,
            item_id=str(_cell(lot, 0, "item_id")),
            zone_id=zone_id,
            location_id=to_location_id,
        )
        _check_free_position(cursor, schema, location_id=to_location_id)

        # ── ⑤ 쓰기 ────────────────────────────────────────────────────
        cursor.execute(
            sql.SQL("UPDATE {}.pallets SET current_location_id = %s WHERE pallet_id = %s").format(
                schema
            ),
            (to_location_id, pallet_id),
        )
        _record_pallet_event(
            cursor,
            schema,
            pallet_id=pallet_id,
            event_type=event_type,
            from_location_id=현재,
            to_location_id=to_location_id,
            occurred_at=occurred_at,
            recorded_by=recorded_by,
            note=note,
        )
    return PlacementResult(
        applied=True,
        pallet_id=pallet_id,
        location_id=to_location_id,
        zone_id=zone_id,
        status=상태,  # type: ignore[arg-type]
    )


def empty_pallet(
    conn: Any,
    *,
    pallet_id: str,
    occurred_at: datetime,
    recorded_by: str,
    note: str | None = None,
) -> EmptyResult:
    """다 나간 Pallet 을 비워 **자리를 돌려준다.** 수량은 1g 도 건드리지 않는다.

    ```text
    ① 입력 검증
    ② 배치 전역 잠금
    ③ 이미 비었나                applied=False (모순이면 IntegrityError)
    ④ 비워도 되는 수량 근거       Lot remaining_qty_kg == 0
    ⑤ pallets EMPTIED + location NULL + emptied_at
       pallet_events EMPTIED (from=옛 자리 · to=NULL)
    ```

    🔴 **자동으로 불리지 않는다.** `confirm_disposal` 도 `ship_allocated_stock` 도 이
       함수를 부르지 않는다. *"재고가 0 이 됐다"* 와 *"사람이 Pallet 을 치웠다"* 는
       다른 사실이고, 한 Lot 이 여러 Pallet 에 나뉘어 있으면 **어느 장을 치웠는지**
       원장이 알지 못한다.

    ```text
    재고 0        ≠  자동으로 자리 비움
    empty_pallet  →  물리 자리 비움
    ```

    🔴 **수량 근거는 Lot 전체 잔량이다 — 추측하지 않는다.**

    ```text
    실측 (2026-09-05 · 실 DB)
      inventory_move_lines            0행
      pallet_id 가 채워진 Move Line   0행
      살아 있는 Pallet 3장의 Lot 잔량  286.92 · 61.76 · 5.72 kg
    ```

      ⚠️ 스키마 주석은 Pallet 별 수량을 *"Move Line 에서 계산한다"* 고 하지만, **어떤
         코드도 Line 을 쓴 적이 없다.** 그래서 Line 으로 재면 저 3장이 전부 *"0kg"* 으로
         보여 **물건이 실려 있는 Pallet 을 비워도 된다고 답한다.** 그 축은 아직 정본이
         아니다.

      ★ 그래서 이번 판은 증명 가능한 쪽만 쓴다 — `remaining_qty_kg == 0` 이면 그 Lot 의
        어떤 Pallet 에도 남은 것이 없다는 것이 **확실하다.** 일부라도 남아 있으면
        어느 장에 남았는지 알 수 없으므로 `PalletNotEmptyable` 로 멈춘다.

      ⚠️ Move Line 을 실제로 쓰기 시작하면 이 규칙을 Line 축으로 좁힐 수 있다. 그때까지
         **덜 비우는 쪽**으로 틀린다 — 자리는 늦게 돌아와도 되지만, 있는 물건의 자리를
         남에게 내주면 안 된다.

    ⚠️ **원장을 만들지 않는다.** 수량 변화는 이미 `OUT`·`DISPOSE` 가 끝낸 뒤다.

    :param recorded_by: `pallet_events.recorded_by` 가 NOT NULL 이다. 물류가 지어내지 않는다.
    :raises InvalidPlacementRequest: 없는 Pallet 이거나 이미 폐기된 Pallet 일 때.
    :raises PalletNotEmptyable: Lot 에 재고가 남아 있을 때.
    :raises WarehouseIntegrityError: `EMPTIED` 인데 자리가 남아 있는 등 모순일 때.
    """
    schema = sql.Identifier(get_db_schema())
    # ── ① 검증 ────────────────────────────────────────────────────────
    _require_text(pallet_id, 칸="pallet_id")
    _require_text(recorded_by, 칸="recorded_by")
    if not isinstance(occurred_at, datetime):
        raise InvalidPlacementRequest(f"occurred_at 은 datetime 이어야 한다: {occurred_at!r}")

    with conn.cursor() as cursor:
        # ── ② 잠금 ────────────────────────────────────────────────────
        lock_warehouse_writes(cursor)

        pallet = _load_pallet(cursor, schema, pallet_id=pallet_id)
        if pallet is None:
            raise InvalidPlacementRequest(f"없는 Pallet 이다: {pallet_id!r}.")
        상태 = str(_cell(pallet, 4, "status"))
        현재 = _cell(pallet, 3, "current_location_id")

        # ── ③ 이미 비었나 ─────────────────────────────────────────────
        if 상태 == "EMPTIED":
            if 현재 is not None:
                raise WarehouseIntegrityError(
                    f"EMPTIED 인데 자리가 남아 있다 (pallet_id={pallet_id!r}"
                    f" · current_location_id={현재!r})."
                    " 조용히 고치지 않는다 — 그 자리를 누가 쓰는지 사람이 봐야 한다."
                )
            return EmptyResult(
                applied=False,
                pallet_id=pallet_id,
                freed_location_id=None,
                zone_id=None,
                status="EMPTIED",
            )
        if 상태 not in _OCCUPYING_PALLET:
            raise InvalidPlacementRequest(
                f"비울 수 있는 상태가 아니다 (pallet_id={pallet_id!r} · status={상태!r})."
                f" ACTIVE · HOLD 만 비운다."
            )
        if 현재 is None:
            raise WarehouseIntegrityError(
                f"살아 있는 Pallet 에 자리가 없다 (pallet_id={pallet_id!r} · status={상태!r})."
            )

        # ── ④ 수량 근거 ───────────────────────────────────────────────
        lot_id = str(_cell(pallet, 1, "lot_id"))
        cursor.execute(
            sql.SQL(
                "SELECT lot_id, remaining_qty_kg FROM {}.inventory_lots WHERE lot_id = %s LIMIT %s"
            ).format(schema),
            (lot_id, _AMBIGUITY_PROBE_LIMIT),
        )
        lot = _one(cursor.fetchall(), 무엇=f"Lot {lot_id!r}")
        if lot is None:
            raise WarehouseIntegrityError(
                f"Pallet 이 없는 Lot 을 가리킨다 (pallet_id={pallet_id!r} · lot_id={lot_id!r})."
            )
        remaining = Decimal(str(_cell(lot, 1, "remaining_qty_kg")))
        if remaining != 0:
            raise PalletNotEmptyable(
                f"Lot 에 재고가 남아 있어 비울 수 없다 (pallet_id={pallet_id!r}"
                f" · lot_id={lot_id!r}): remaining {remaining}kg."
                " 🔴 Pallet 별 남은 물량을 추측하지 않는다 — 한 Lot 이 여러 장에 나뉘어"
                " 있으면 어느 장이 비었는지 지금 스키마로는 증명할 수 없다."
                " 먼저 출고(OUT)나 폐기(DISPOSE)로 잔량을 0 으로 만든다."
            )

        # ── ⑤ 쓰기 ────────────────────────────────────────────────────
        zone_id = _zone_of(cursor, schema, location_id=str(현재))
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.pallets
                SET status = 'EMPTIED', current_location_id = NULL, emptied_at = %s
                WHERE pallet_id = %s
                """
            ).format(schema),
            (occurred_at, pallet_id),
        )
        _record_pallet_event(
            cursor,
            schema,
            pallet_id=pallet_id,
            event_type="EMPTIED",
            from_location_id=현재,
            to_location_id=None,
            occurred_at=occurred_at,
            recorded_by=recorded_by,
            note=note,
        )
    return EmptyResult(
        applied=True,
        pallet_id=pallet_id,
        freed_location_id=str(현재),
        zone_id=zone_id,
        status="EMPTIED",
    )


def _zone_of(cursor: Any, schema: sql.Identifier, *, location_id: str) -> str | None:
    cursor.execute(
        sql.SQL("SELECT zone_id FROM {}.storage_locations WHERE location_id = %s LIMIT %s").format(
            schema
        ),
        (location_id, _AMBIGUITY_PROBE_LIMIT),
    )
    행 = _one(cursor.fetchall(), 무엇=f"자리 {location_id!r}")
    return None if 행 is None else str(_cell(행, 0, "zone_id"))
