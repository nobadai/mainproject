"""inbound_stock.py — 검수 끝난 입고를 **가용재고로 만들고 일정을 걷는다** (3-B4-I).

```text
INSPECTED Receipt
   → accepted 수량만큼 Lot 하나 (remaining 0 으로 세우고)
   → record_inventory_move(IN)      ← remaining 이 여기서 accepted 가 된다
   → Receipt PUTAWAY_DONE
   → in_transit · confirmed_inbound 에서 그 inbound_id 를 함께 제거
```

★ **남의 로직을 복제하지 않는다.** Receipt · 검수 · 원장은 각자의 모듈이 이미 하고
  있고, 이 파일은 **순서와 트랜잭션**만 맡는다 (`master/transition.apply_approval`
  이 재무·물류를 감싸는 것과 같은 결).

🔴 **`accepted_qty_kg` 만 재고가 된다.**

  ```text
  accepted > 0   Lot 1 · IN 1                  ACTIVE · inspection_status=PASS
  accepted = 0   Lot 없음 · IN 없음             REJECT 여도 **정상 완료**다
  hold · reject  Lot 없음 · IN 없음 · 폐기 없음  검수·Receipt 에만 남는다
  ```

  ⚠️ 보류·거부 물량을 자동으로 `DISPOSE` 로 바꾸지 않는다. 그것은 사람이 판단할
     일이고 이 판의 범위 밖이다.

🟢 **보관 Zone 의 권위 출처를 실측으로 찾았다 — 드리프트가 아니었다.**

  ```text
  item_storage_policies.storage_zone   ITEM-BAECHU → COLD_HUMID_0_3   …5품목
  inventory_lots.storage_zone          기존 80행이 **품목마다 정확히 그 값**이다
                                       (16/16 × 5품목, 실측 2026-09-05)
  ```

  ⇒ `inventory_lots.storage_zone` 의 주인은 `item_storage_policies` 다. 품목으로
    찾아 **그 값을 그대로** 쓴다.

  ⚠️ **`item_zone_assignments.zone_id`(`HIGH_HUMIDITY_COLD` 계열)는 다른 축이다.**
     그쪽은 새 WMS 의 **물리 Zone**(`warehouse_zones`)이고 5품목 중 3품목만 덮는다
     (`ITEM-GEONGOCHU` · `ITEM-PIMANUL` 없음). 두 축을 잇는 번역표도 없다
     (`warehouse_zones.zone_code == zone_id` 라 옮길 값이 없다).
     🔴 그래서 여기서 그 둘을 잇는 매핑을 **지어내지 않는다** — 물리 Zone 배정은
        적치(Putaway) 단계의 사실이다.

  🔴 **품목명으로 Zone 을 하드코딩하지 않는다.** `배추 → HIGH_HUMIDITY_COLD` 같은
     줄을 코드에 적으면 그 순간 정책표가 둘이 된다.

🔴 **잠금 순서가 계약이다.** 기존 두 잠금을 그대로 쓰고 새 잠금을 만들지 않는다.

  ```text
  ① 도착 전역 advisory  (20260905, 2)   receipts.lock_arrival_writes
  ② fixture 행 FOR UPDATE               일정 정리 대상 · ③④ 보다 **먼저**
  ③ Lot 조회 / INSERT
  ④ record_inventory_move  → 원장 전역 advisory (20260905, 1) → Lot 행 FOR UPDATE
  ⑤ Receipt UPDATE
  ⑥ 일정 정리 UPDATE (② 의 잠금 아래)
  ⑦ 커밋은 호출자가 한 번
  ```

  ⚠️ **원장 전역을 fixture 행보다 먼저 잡는 경로를 만들면 안 된다** — ② 를 ④ 앞에
     둔 이유가 그것이고, 그 규칙이 이 전순서를 성립시킨다.

⚠️ **Pallet · Location 을 만들지 않는다.** 원장이 `lines=()` 를 허용하고, 실제로
   기존 80 Lot 의 Pallet 배분도 원장에 없다 (`pallets` 주석: *"Lot 잔량이 수량
   정본이다"*). `receiving_location_id` · 팔레트 수도 그대로 NULL 이다.

⚠️ **`CLOSED` 까지 가지 않는다.** `PUTAWAY_DONE` 의 뜻은 *"이 Receipt 에서 가용재고로
   반영할 accepted 수량의 재고화가 끝났다"* 이지 보류·거부까지 정리됐다는 뜻이 아니다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from app.logistics.db import get_db_schema
from app.logistics.inspections import InspectionOutcome, find_inspection
from app.logistics.ledger import record_inventory_move
from app.logistics.purchase_detail import PurchaseDetail
from app.logistics.receipts import ReceiptStatus, lock_arrival_writes
from app.logistics.schemas import InTransitItem, ScheduledQuantity
from app.logistics.transition import USAGE_SCOPE

__all__ = [
    "InboundStockError",
    "InboundStockResult",
    "LotConflict",
    "LotIntegrityError",
    "ScheduleIntegrityError",
    "lot_id_for",
    "materialize_inspected_inbound",
    "move_id_for",
]


#: 이 단계가 손댈 수 있는 Receipt 상태. `ARRIVED` · `INSPECTING` 은 아직 대상이 아니다.
_READY_TO_MATERIALIZE: frozenset[str] = frozenset({"INSPECTED"})

#: 이미 재고화가 끝난 상태. 재실행이면 여기로 온다.
_ALREADY_MATERIALIZED: frozenset[str] = frozenset({"PUTAWAY_DONE", "CLOSED"})

#: 🔴 원장에 이미 있는 어휘다 (실측: `IN` 75행이 이 사유를 쓴다). 새 사유를 만들지 않는다.
_IN_REASON_CODE = "PURCHASE_RECEIPT"

#: 🔴 **둘까지만 읽는다.** 0 · 1 · 2+ 를 가르는 데 그 이상이 필요 없다.
_AMBIGUITY_PROBE_LIMIT = 2

_LOT_COLUMNS = (
    "lot_id",
    "sim_run_id",
    "purchase_item_id",
    "item_id",
    "grade",
    "received_at",
    "original_qty_kg",
    "unit_cost_krw_per_kg",
    "storage_zone",
    "status",
    "derivation_status",
    "inspection_status",
)


class InboundStockError(RuntimeError):
    """이 모듈이 내는 실패의 조상."""


class LotIntegrityError(InboundStockError, ValueError):
    """Lot 과 Receipt 가 서로를 배반한다. **조용히 고치지 않는다.**"""


class LotConflict(InboundStockError, ValueError):
    """같은 Receipt 에 **다른 사실**의 Lot 이 이미 있다.

    🔴 덮지도 버리지도 않는다 — 그 Lot 의 원가·등급으로 이미 판매·평가가 돌았을 수
       있어, 갈아 끼우면 그 계산들이 소리 없이 근거를 잃는다.
    """


class ScheduleIntegrityError(InboundStockError, ValueError):
    """일정 두 칸이 B-1 을 어기고 있어 걷어낼 수 없다.

    🔴 **한쪽만 지우지 않는다.** `in_transit` 에서만 빼면 `confirmed_inbound` 에 유령
       일정이 남아 점유가 계속 계산되고, 반대면 B-1 이
       `IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE` 로 다음 날을 세운다.
    """


@dataclass(frozen=True)
class InboundStockResult:
    """`materialize_inspected_inbound` 의 결과. **작게 둔다.**

    🔴 **`applied=False` 는 "할 일이 없었다" 가 아니라 "이번 호출이 새로 만든 것이
       없다" 다.** 재실행이 정상적으로 여기로 온다.
    """

    #: 이번 호출이 Lot 이나 Move 를 **새로 만들었나.**
    applied: bool
    receipt_status: ReceiptStatus
    #: `accepted = 0` 이면 `None` — 만들 재고가 없었다는 뜻이다.
    lot_id: str | None
    move_id: str | None
    accepted_qty_kg: Decimal
    #: 이번 호출이 일정에서 그 `inbound_id` 를 실제로 걷어냈나.
    schedule_cleared: bool


def lot_id_for(*, receipt_id: str) -> str:
    """accepted Lot 의 PK. **순수 계산이고 결정론이다.**

    ```text
    LOT-{receipt_id}
    ```

    ★ `receipt_id` 가 이미 실행(`sim_run_id`)과 입고(`inbound_id`) 정체성을 담고
      있어 접두사만 얹으면 된다 (`inspection_id_for` 와 같은 결).

    🔴 난수 · 시계 · 시퀀스를 쓰지 않는다. 같은 Receipt 는 몇 번을 불러도 같은 값이다.
    """
    if not receipt_id or not receipt_id.strip():
        raise LotIntegrityError(f"lot_id 를 지을 수 없다 — receipt_id 가 비었다: {receipt_id!r}")
    return f"LOT-{receipt_id}"


def move_id_for(*, lot_id: str) -> str:
    """입고 Move 의 멱등 키. **Lot 에 뿌리를 둔다.**

    ★ `record_inventory_move` 가 `move_id` 로 이미 멱등하다 — 같은 키가 다시 오면
      사실을 대조하고 `applied=False` 를 돌려준다. 그 장치를 그대로 쓴다.
    """
    if not lot_id or not lot_id.strip():
        raise LotIntegrityError(f"move_id 를 지을 수 없다 — lot_id 가 비었다: {lot_id!r}")
    return f"MOVE-IN-{lot_id}"


def _cell(row: Any, index: int, name: str) -> Any:
    """row_factory 가 튜플이든 매핑이든 한 칸을 꺼낸다."""
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _one_row(cursor: Any, 무엇: str) -> Any | None:
    """0 · 1 · 2+ 를 가른다. **첫 행을 고르지 않는다.**"""
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise LotIntegrityError(f"{무엇} 이 둘 이상이다 — 어느 것이 진짜인지 여기서 고르지 않는다.")
    return rows[0]


def _receipt_facts(conn: Any, schema: sql.Identifier, *, receipt_id: str) -> dict[str, Any]:
    """Receipt 의 상태 · 도착일 · 검수 수량. **PK 로 한 행을 읽는다.**"""
    이름 = (
        "receipt_status",
        "sim_run_id",
        "inbound_id",
        "purchase_item_id",
        "item_id",
        "arrived_at",
        "accepted_qty_kg",
        "hold_qty_kg",
        "rejected_qty_kg",
    )
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT receipt_status, sim_run_id, inbound_id, purchase_item_id, item_id,
                       arrived_at, accepted_qty_kg, hold_qty_kg, rejected_qty_kg
                FROM {}.inbound_receipts
                WHERE receipt_id = %s
                """
            ).format(schema),
            (receipt_id,),
        )
        row = _one_row(cursor, f"receipt_id={receipt_id!r} 인 Receipt")
    if row is None:
        raise LotIntegrityError(f"재고화할 Receipt 가 없다: receipt_id={receipt_id!r}")
    return {name: _cell(row, index, name) for index, name in enumerate(이름)}


def _storage_zone_of(conn: Any, schema: sql.Identifier, *, item_id: str) -> str:
    """이 품목의 보관 Zone. **`item_storage_policies` 가 주인이다.**

    🔴 **품목명으로 하드코딩하지 않는다.** 기존 80 Lot 의 `storage_zone` 이 품목마다
       이 표의 값과 **정확히 일치한다**(실측) — 즉 여기가 그 칸의 권위 출처다.

    ⚠️ 행이 없으면 **멈춘다.** 기본 Zone 을 고르면 그 추측이 로트의 보관 조건이 되고,
       신선도 계산(`operational_limit_days`)도 같은 표에서 오므로 함께 어긋난다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT storage_zone FROM {}.item_storage_policies WHERE item_id = %s").format(
                schema
            ),
            (item_id,),
        )
        row = _one_row(cursor, f"item_id={item_id!r} 의 보관 정책")
    if row is None:
        raise LotIntegrityError(
            f"보관 정책이 없어 storage_zone 을 정할 수 없다: item_id={item_id!r}."
            " 기본 Zone 을 고르지 않는다 — 그 추측이 로트의 보관 조건이 된다."
        )
    zone = _cell(row, 0, "storage_zone")
    if not isinstance(zone, str) or not zone.strip():
        raise LotIntegrityError(f"item_storage_policies.storage_zone 을 읽을 수 없다: {zone!r}")
    return zone


def _existing_lot(conn: Any, schema: sql.Identifier, *, receipt_id: str) -> dict[str, Any] | None:
    """이 Receipt 로 이미 만든 Lot. `inbound_receipt_id` 가 그 연결이다."""
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT lot_id, sim_run_id, purchase_item_id, item_id, grade, received_at,
                       original_qty_kg, unit_cost_krw_per_kg, storage_zone, status,
                       derivation_status, inspection_status
                FROM {}.inventory_lots
                WHERE inbound_receipt_id = %s
                ORDER BY lot_id
                LIMIT {}
                """
            ).format(schema, sql.Literal(_AMBIGUITY_PROBE_LIMIT)),
            (receipt_id,),
        )
        row = _one_row(cursor, f"receipt_id={receipt_id!r} 로 만든 Lot")
    if row is None:
        return None
    return {name: _cell(row, index, name) for index, name in enumerate(_LOT_COLUMNS)}


def _assert_same_purchase_reference(
    receipt: Mapping[str, Any], purchase_detail: PurchaseDetail, *, receipt_id: str
) -> None:
    """Receipt 가 든 매입 참조와 이번에 받은 매입 상세가 **같은 줄인가.**

    🔴 **두 출처를 섞지 않는다.** `receipt[...] or purchase_detail[...]` 같은 폴백을
       쓰면, 다른 매입 줄의 단가·등급으로 Lot 이 서고 그 값이 그대로 재고 원가가 된다.
       Receipt 의 두 칸은 **Receipt 를 만들 때 바로 그 매입 상세에서 온 값**이라
       달라질 수가 없다 — 다르면 호출자가 엉뚱한 상세를 들고 온 것이다.

    ⚠️ **NULL 도 정상으로 보지 않는다.** 정상 생성 경로(`create_arrived_receipt`)는
       둘 다 반드시 채운다. 비어 있다면 그 Receipt 는 이 파이프라인이 만든 것이 아니다.

    ★ **DML 이 나가기 전에 부른다.** 여기서 멈추면 트랜잭션이 살아 있어 바깥이
      다른 일을 이어갈 수 있다.
    """
    for 칸, 받은값 in (
        ("purchase_item_id", purchase_detail.purchase_item_id),
        ("item_id", purchase_detail.item_id),
    ):
        적힌값 = receipt[칸]
        if not 적힌값:
            raise LotIntegrityError(
                f"Receipt 에 {칸} 가 없다 (receipt_id={receipt_id!r})."
                " 정상 생성 경로는 둘 다 채운다 — 비어 있으면 이 파이프라인이 만든"
                " Receipt 가 아니다. 매입 상세 값으로 대신 채우지 않는다."
            )
        if 적힌값 != 받은값:
            raise LotIntegrityError(
                f"Receipt 의 {칸} 와 받은 매입 상세가 다르다"
                f" (receipt_id={receipt_id!r}): receipt={적힌값!r} detail={받은값!r}."
                " 다른 매입 줄의 단가·등급으로 Lot 을 세우지 않는다."
            )


def _assert_same_lot(기존: Mapping[str, Any], 이번: Mapping[str, Any], *, receipt_id: str) -> None:
    """기존 Lot 이 이번에 만들려던 것과 같은 사실인가.

    ★ `Decimal` · `date` 값으로 비교한다 — 문자열이면 `100` 과 `100.000000` 이 갈려
      **정상 재실행이 Conflict 로 뒤집힌다.**
    """
    다른것 = {
        칸: (기존[칸], 이번[칸])
        for 칸 in (
            "sim_run_id",
            "purchase_item_id",
            "item_id",
            "original_qty_kg",
            "unit_cost_krw_per_kg",
            "grade",
            "received_at",
        )
        if 기존[칸] != 이번[칸]
    }
    if 다른것:
        raise LotConflict(
            f"같은 Receipt 에 다른 사실의 Lot 이 이미 있다 (receipt_id={receipt_id!r}):"
            f" {다른것!r}. 덮지도 버리지도 않는다 — 그 Lot 의 원가·등급으로 이미"
            " 판매·평가가 돌았을 수 있다."
        )


def _insert_lot(conn: Any, schema: sql.Identifier, 값: Mapping[str, Any]) -> None:
    """accepted Lot 을 **`remaining_qty_kg = 0` 으로** 세운다.

    🔴 **처음부터 `remaining = accepted` 로 넣고 IN 을 또 더하지 않는다.** 그러면
       잔량이 두 배가 되고, 원장(`inventory_moves`)과 잔량이 어긋난 채로 남는다 —
       `ledger.py` 가 존재하는 이유가 정확히 그것이다. 잔량을 바꾸는 것은 원장뿐이다.

    ⚠️ `derivation_status` 는 **적지 않는다.** 그 칸의 뜻이 *"Burn-in Lot 이 어떤
       파생규칙으로 생성됐는지"* 라, 실제로 도착한 Lot 은 NULL 이 맞다.
       위치·팔레트도 적치 단계의 사실이라 손대지 않는다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.inventory_lots (
                    lot_id, sim_run_id, purchase_item_id, item_id, grade, received_at,
                    original_qty_kg, remaining_qty_kg, unit_cost_krw_per_kg,
                    storage_zone, status, inspection_status, inbound_receipt_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s)
                """
            ).format(schema),
            (
                값["lot_id"],
                값["sim_run_id"],
                값["purchase_item_id"],
                값["item_id"],
                값["grade"],
                값["received_at"],
                값["original_qty_kg"],
                값["unit_cost_krw_per_kg"],
                값["storage_zone"],
                "ACTIVE",
                "PASS",
                값["inbound_receipt_id"],
            ),
        )


def _assert_existing_move(
    conn: Any,
    schema: sql.Identifier,
    *,
    move_id: str,
    sim_run_id: str,
    lot_id: str,
    quantity_kg: Decimal,
    moved_at: date,
) -> None:
    """이미 재고화를 주장하는 Receipt 에 그 입고 Move 가 **실재하는가.**

    🔴 **완료 상태에서는 없는 Move 를 새로 만들지 않는다.** `record_inventory_move`
       를 무조건 부르면 Move 가 없을 때 **조용히 만들어 버린다** — 그러면 사라진
       원장 기록이 있었다는 사실조차 안 남고, 잔량도 그때 다시 올라간다.

    ★ **읽기만 한다. 원장 업무 로직을 복제하지 않는다.** 잔량 계산·수량 규칙은 여전히
      `ledger.py` 소유이고, 여기서는 *"완료라고 적힌 것이 사실인가"* 만 확인한다.

    ⚠️ 2건 이상은 `inventory_moves_pkey` 가 막으므로 DB 계약을 믿는다.
    """
    이름 = (
        "sim_run_id",
        "lot_id",
        "move_type",
        "quantity_kg",
        "moved_at",
        "reason_code",
        "sale_item_id",
    )
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT sim_run_id, lot_id, move_type, quantity_kg, moved_at,
                       reason_code, sale_item_id
                FROM {}.inventory_moves
                WHERE move_id = %s
                """
            ).format(schema),
            (move_id,),
        )
        row = _one_row(cursor, f"move_id={move_id!r} 인 Move")
    if row is None:
        raise LotIntegrityError(
            f"재고화가 끝났다는 Receipt 인데 입고 Move 가 없다 (move_id={move_id!r})."
            " 여기서 새로 만들어 조용히 복구하지 않는다 — 사라진 원장 기록이 있었다는"
            " 사실조차 안 남는다."
        )
    적힌 = {name: _cell(row, index, name) for index, name in enumerate(이름)}
    기대 = {
        "sim_run_id": sim_run_id,
        "lot_id": lot_id,
        "move_type": "IN",
        "quantity_kg": quantity_kg,
        "moved_at": moved_at,
        "reason_code": _IN_REASON_CODE,
        "sale_item_id": None,
    }
    다른것 = {칸: (적힌[칸], 기대[칸]) for 칸 in 기대 if 적힌[칸] != 기대[칸]}
    if 다른것:
        raise LotIntegrityError(
            f"기존 입고 Move 가 이번 사실과 다르다 (move_id={move_id!r}): {다른것!r}."
            " 고치지 않는다 — 그 수량으로 잔량이 이미 움직였다."
        )


def _mark_putaway_done(conn: Any, schema: sql.Identifier, *, receipt_id: str) -> None:
    """Receipt 를 `PUTAWAY_DONE` 으로 넘긴다. **수량은 손대지 않는다.**

    ★ 수량은 검수 단계가 이미 옮겼고, 그것이 권위값이다.
    ⚠️ `CLOSED` 로 가지 않는다 — 보류·거부 정리는 이 판의 범위가 아니다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.inbound_receipts
                SET receipt_status = %s, updated_at = now()
                WHERE receipt_id = %s
                """
            ).format(schema),
            ("PUTAWAY_DONE", receipt_id),
        )


# ── 일정 정리 ───────────────────────────────────────────────────────────


def _fixture_row(
    conn: Any, schema: sql.Identifier, *, sim_run_id: str, as_of: date, usage_scope: str
) -> tuple[Any, Any]:
    """그날 fixture 행을 **잠그고** 두 목록을 읽는다.

    🔴 **`FOR UPDATE` 가 일정 정리의 동시성 방어다.** 읽고-고치고-쓰는 사이에 승인
       전이(`transition.persist_inventory`)가 끼어들면 이번에 걷어낸 행이 되살아나거나
       그쪽 승인분이 사라진다 — 같은 행을 같은 방식으로 잠근다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT in_transit_json, confirmed_inbound_json
                FROM {}.logistics_runtime_fixture
                WHERE sim_run_id = %s AND as_of = %s AND usage_scope = %s
                FOR UPDATE
                """
            ).format(schema),
            (sim_run_id, as_of, usage_scope),
        )
        row = _one_row(cursor, "그날 runtime fixture 행")
    if row is None:
        raise ScheduleIntegrityError(
            f"정리할 물류 runtime fixture 행이 없다"
            f" (sim_run_id={sim_run_id}, as_of={as_of}, usage_scope={usage_scope})."
        )
    return _cell(row, 0, "in_transit_json"), _cell(row, 1, "confirmed_inbound_json")


def _찾는다(목록: Sequence[Any] | None, inbound_id: str) -> list[dict[str, Any]]:
    return [
        행 for 행 in (목록 or []) if isinstance(행, dict) and 행.get("inbound_id") == inbound_id
    ]


def _clear_schedule(
    conn: Any,
    schema: sql.Identifier,
    *,
    sim_run_id: str,
    as_of: date,
    usage_scope: str,
    inbound_id: str,
) -> bool:
    """두 칸에서 그 `inbound_id` 를 **함께** 뺀다. 이미 없으면 아무것도 안 한다.

    🔴 **B-1 을 지우기 전에 다시 검증한다.** 두 칸의 `item` · 수량 · 날짜가 어긋난
       상태를 조용히 지우면, 어긋나 있었다는 사실조차 안 남는다.

    🔴 **한쪽에만 있으면 멈춘다.** 그 상태가 이미 B-1 위반이고, 남은 쪽을 마저 지우면
       위반을 덮는 것이 된다.

    ⚠️ **`None`(UNRESOLVED)을 `[]` 로 바꾸지 않는다.** 그 세 상태는 다른 사실이다.
    """
    in_transit, confirmed = _fixture_row(
        conn, schema, sim_run_id=sim_run_id, as_of=as_of, usage_scope=usage_scope
    )
    운송중 = _찾는다(in_transit, inbound_id)
    확정 = _찾는다(confirmed, inbound_id)

    if not 운송중 and not 확정:
        return False  # ★ 이미 걷혔다. 재실행의 정상 경로다.
    if len(운송중) != 1 or len(확정) != 1:
        raise ScheduleIntegrityError(
            f"일정 두 칸이 짝을 이루지 않아 걷어낼 수 없다 (inbound_id={inbound_id!r}):"
            f" in_transit {len(운송중)}건 · confirmed_inbound {len(확정)}건."
            " 한쪽만 지우면 그 불일치를 덮는 것이 된다."
        )

    # ★ B-1 이 대조하는 그 네 값을 여기서 다시 본다.
    운송 = InTransitItem.model_validate(운송중[0])
    일정 = ScheduledQuantity.model_validate(확정[0])
    if (
        일정.item != 운송.item
        or 일정.quantity_kg != 운송.quantity_kg
        or 일정.date != 운송.expected_arrival_date
    ):
        raise ScheduleIntegrityError(
            f"일정 두 칸의 사실이 다르다 (inbound_id={inbound_id!r}):"
            f" in_transit={운송!r} confirmed_inbound={일정!r}. 조용히 지우지 않는다."
        )

    남은_운송 = [행 for 행 in (in_transit or []) if 행 not in 운송중]
    남은_확정 = [행 for 행 in (confirmed or []) if 행 not in 확정]

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.logistics_runtime_fixture
                SET in_transit_json = %s,
                    in_transit_status = %s,
                    confirmed_inbound_json = %s,
                    confirmed_inbound_status = %s,
                    updated_at = NOW()
                WHERE sim_run_id = %s AND as_of = %s AND usage_scope = %s
                """
            ).format(schema),
            (
                Jsonb(남은_운송),
                # ★ 마지막 행을 걷어내면 *"확인했고 0 건"* 이다 — `None` 이 아니다.
                "CONFIRMED" if 남은_운송 else "CONFIRMED_ZERO",
                Jsonb(남은_확정),
                "CONFIRMED" if 남은_확정 else "CONFIRMED_ZERO",
                sim_run_id,
                as_of,
                usage_scope,
            ),
        )
    return True


# ── 본체 ────────────────────────────────────────────────────────────────


def materialize_inspected_inbound(
    conn: Any,
    *,
    as_of: date,
    receipt_id: str,
    purchase_detail: PurchaseDetail,
    usage_scope: str = USAGE_SCOPE,
) -> InboundStockResult:
    """검수 끝난 입고를 재고로 만들고 일정을 걷는다. **멱등하다.**

    ```text
    ① 잠금 · 상태 확인
    ② fixture 행 FOR UPDATE       ← 원장 잠금보다 **먼저**
    ③ accepted > 0 이면 Lot (remaining 0) → record_inventory_move(IN)
    ④ Receipt PUTAWAY_DONE
    ⑤ 일정 두 칸에서 inbound_id 제거
    ```

    🔴 **상태가 생성 권한을 가른다.**

    ```text
    INSPECTED               아직 재고화 전이다 — Lot · Move 를 만들 수 있다
                            이미 같은 사실로 있으면 멱등 재실행이다
    PUTAWAY_DONE · CLOSED   이미 재고화를 **주장하는** 상태다
                            accepted > 0 이면 Lot 과 Move 가 **반드시 있어야** 한다
                            없으면 무결성 오류 — 새로 만들어 조용히 복구하지 않는다
                            accepted = 0 이면 둘 다 없는 것이 정상이다
    ARRIVED · INSPECTING    아직 대상이 아니다 — 검수 사실 없이 재고를 만들면
                            수용량을 우리가 정하는 셈이 된다
    ```

    ⚠️ **`accepted = 0` 인데 Lot 이 있으면 모순이다.** 검수가 하나도 안 받았다는데
       재고가 선 것이라, 수량 분기와 무관하게 기존 Lot 을 먼저 본다.

    🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.**

    :param as_of: 정리할 fixture 행의 날짜. **마스터가 정하는 달력값**이다.
    :param purchase_detail: 원가·등급의 권위 출처 (`fetch_purchase_detail` 결과).
    """
    schema = sql.Identifier(get_db_schema())

    # ── ① 도착 전역 잠금이 먼저다 ─────────────────────────────────────
    with conn.cursor() as cursor:
        lock_arrival_writes(cursor)

    receipt = _receipt_facts(conn, schema, receipt_id=receipt_id)
    상태 = receipt["receipt_status"]
    if 상태 not in _READY_TO_MATERIALIZE | _ALREADY_MATERIALIZED:
        raise LotIntegrityError(
            f"재고화할 수 없는 Receipt 상태다: {상태!r} (receipt_id={receipt_id!r})."
            " 검수 사실 없이 재고를 만들면 수용량을 우리가 정하는 셈이 된다."
        )

    검수 = find_inspection(conn, receipt_id=receipt_id)
    if 검수 is None:
        raise LotIntegrityError(
            f"검수 사실이 없다 (receipt_id={receipt_id!r}, receipt_status={상태!r})."
            " 수용 수량의 주인은 검수다 — 여기서 지어내지 않는다."
        )
    _assert_receipt_matches(receipt, 검수.outcome, receipt_id=receipt_id)
    # 🔴 **DML 전에** Receipt 가 든 매입 참조와 받은 상세가 같은 줄인지 본다.
    _assert_same_purchase_reference(receipt, purchase_detail, receipt_id=receipt_id)
    accepted = 검수.outcome.accepted_qty_kg
    완료상태 = 상태 in _ALREADY_MATERIALIZED

    # ── ② 일정 행을 **원장 잠금보다 먼저** 잡는다 ─────────────────────
    sim_run_id = receipt["sim_run_id"]
    inbound_id = receipt["inbound_id"]
    if not inbound_id:
        raise ScheduleIntegrityError(
            f"Receipt 에 inbound_id 가 없어 일정을 걷을 수 없다: receipt_id={receipt_id!r}"
        )
    _fixture_row(conn, schema, sim_run_id=sim_run_id, as_of=as_of, usage_scope=usage_scope)

    # ── ③ 기존 Lot 은 **수량·상태와 무관하게** 먼저 본다 ──────────────
    #    ★ `accepted = 0` 인데 Lot 이 있는 것도 모순이라, 그 분기 안에서만 보면 못 잡는다.
    기존 = _existing_lot(conn, schema, receipt_id=receipt_id)
    lot_id: str | None = None
    move_id: str | None = None
    applied = False

    if accepted <= 0:
        if 기존 is not None:
            raise LotIntegrityError(
                f"수용 수량이 0 인데 재고 Lot 이 있다"
                f" (receipt_id={receipt_id!r}, lot_id={기존['lot_id']!r})."
                " 검수가 하나도 안 받았다는데 재고가 선 것이라 모순이다."
            )
    else:
        기대 = {
            "sim_run_id": sim_run_id,
            # 🔴 Receipt 와 상세가 이미 같다는 것을 위에서 확인했다 — 섞지 않는다.
            "purchase_item_id": purchase_detail.purchase_item_id,
            "item_id": purchase_detail.item_id,
            # 🔴 매입이 적은 등급 그대로다. NULL 이면 NULL 이다.
            "grade": purchase_detail.grade,
            "received_at": receipt["arrived_at"],
            "original_qty_kg": accepted,
            "unit_cost_krw_per_kg": purchase_detail.unit_price_krw_per_kg,
        }
        if 기존 is not None:
            # ★ **보관 Zone 을 다시 조회하지 않는다.** 이미 선 Lot 의 Zone 은 확정된
            #   역사적 사실이라, 정책이 나중에 바뀌거나 지워졌다고 과거 입고 재실행이
            #   실패하면 안 된다.
            _assert_same_lot(기존, 기대, receipt_id=receipt_id)
            lot_id = 기존["lot_id"]
        elif 완료상태:
            # 🔴 완료라고 적힌 상태에서 없는 재고를 새로 만들어 복구하지 않는다.
            raise LotIntegrityError(
                f"재고화가 끝났다는 Receipt 인데 Lot 이 없다"
                f" (receipt_id={receipt_id!r}, receipt_status={상태!r})."
                " 여기서 새로 만들면 사라진 재고가 있었다는 사실조차 안 남는다."
            )
        else:
            lot_id = lot_id_for(receipt_id=receipt_id)
            _insert_lot(
                conn,
                schema,
                {
                    **기대,
                    "lot_id": lot_id,
                    # ★ **신규 Lot 일 때만** 정책표를 본다.
                    "storage_zone": _storage_zone_of(conn, schema, item_id=purchase_detail.item_id),
                    "inbound_receipt_id": receipt_id,
                },
            )
            applied = True

        # ── ④ 잔량을 바꾸는 것은 원장뿐이다 ───────────────────────────
        move_id = move_id_for(lot_id=lot_id)
        if 완료상태:
            # 🔴 **읽어서 확인만 한다.** `record_inventory_move` 를 부르면 Move 가
            #    없을 때 새로 만들어 버린다 — 완료 상태에서는 그것이 조용한 복구다.
            _assert_existing_move(
                conn,
                schema,
                move_id=move_id,
                sim_run_id=sim_run_id,
                lot_id=lot_id,
                quantity_kg=accepted,
                moved_at=receipt["arrived_at"],
            )
        else:
            # ★ `record_inventory_move` 가 `move_id` 로 이미 멱등하다 — 재실행이면
            #   사실을 대조하고 `applied=False` 를 돌려준다. 여기서 다시 세지 않는다.
            move = record_inventory_move(
                conn,
                move_id=move_id,
                sim_run_id=sim_run_id,
                lot_id=lot_id,
                move_type="IN",
                quantity_kg=accepted,
                moved_at=receipt["arrived_at"],
                reason_code=_IN_REASON_CODE,
            )
            applied = applied or move.applied

    # ── ⑤⑥ Receipt 와 일정 ───────────────────────────────────────────
    if 상태 in _READY_TO_MATERIALIZE:
        _mark_putaway_done(conn, schema, receipt_id=receipt_id)
        상태 = "PUTAWAY_DONE"
    schedule_cleared = _clear_schedule(
        conn,
        schema,
        sim_run_id=sim_run_id,
        as_of=as_of,
        usage_scope=usage_scope,
        inbound_id=inbound_id,
    )

    return InboundStockResult(
        applied=applied,
        receipt_status=상태,
        lot_id=lot_id,
        move_id=move_id,
        accepted_qty_kg=accepted,
        schedule_cleared=schedule_cleared,
    )


def _assert_receipt_matches(
    receipt: Mapping[str, Any], outcome: InspectionOutcome, *, receipt_id: str
) -> None:
    """Receipt 에 옮겨진 수량이 검수와 같은가.

    ⚠️ 검수 단계가 이미 옮겼어야 하는 값이다. 다르면 둘 중 하나가 틀린 것이고,
       그 위에서 재고를 만들면 **틀린 수량이 가용재고가 된다.**
    """
    if (
        receipt["accepted_qty_kg"] != outcome.accepted_qty_kg
        or receipt["hold_qty_kg"] != outcome.hold_qty_kg
        or receipt["rejected_qty_kg"] != outcome.reject_qty_kg
    ):
        raise LotIntegrityError(
            f"Receipt 수량이 검수와 다르다 (receipt_id={receipt_id!r}):"
            f" receipt=({receipt['accepted_qty_kg']}, {receipt['hold_qty_kg']},"
            f" {receipt['rejected_qty_kg']}) 검수={outcome!r}."
            " 어느 쪽이 맞는지 여기서 고르지 않는다."
        )
