"""Historical Reader — **선택한 `as_of` 시점의 사실을 원장·사건으로 되살린다.**

```text
Current  (지금 이 순간)     inventory_lots.remaining_qty_kg · *.status · pallets.current_location_id
                            → repository.get_current_logistics_read  (Agent Runtime 소유)
Historical (as_of 시점)     inventory_moves · pallet_events · inbound_receipts · inbound_inspections
                            → 이 파일                                 (화면 조회 소유)
```

🔴 **Current Cache 를 과거 값으로 읽지 않는다. 이 파일의 존재 이유가 그것이다.**

   `inventory_lots.remaining_qty_kg` 는 **지금 잔량**이고 과거 잔량이 아니다.
   실측(2026-09-09 · `SIM-BURNIN-202512`)에서 그 차이가 그대로 드러났다.

   ```text
   as_of        캐시 조회   원장 복원
   2026-01-05      0 kg      294.4 kg
   2026-01-09      0 kg      806.4 kg
   2026-01-17      0 kg      806.4 kg
   2026-02-05      0 kg    6,452.4 kg
   ```

   84 Lot 이 전부 잔량 0 (2026-09-12 폐기)이라 `remaining_qty_kg > 0` 조건이
   **모든 과거 날짜에서 0 행**을 냈다. 캐시는 지금 원장과 정확히 일치한다
   (불일치 0 건) — 틀린 것은 값이 아니라 **그 값을 과거에 쓴 것**이다.

🔴 **Cutoff 규칙 둘. 섞지 않는다.**

```text
DATE 컬럼        col <= as_of                      moved_at · received_at · arrived_at
TIMESTAMPTZ 컬럼 col <  (as_of + 1일) 00:00 KST     inspected_at · occurred_at · decided_at
```

   `occurred_at::date` 로 자르지 않는다 — 서버 timezone 에 따라 하루가 밀린다.
   `created_at` · `updated_at` · `recorded_at` 은 **감사용 벽시각**이라 시뮬레이션
   사실일로 쓰지 않는다.

🔴 **`ADJUST` 는 계산하지 않고 멈춘다.** writer 가 없고 `ADJUST_IN` / `ADJUST_OUT`
   로 갈리기 전까지 부호 계약이 없다. 방향을 넘겨짚어 그린 선은 틀렸다는 것도
   알려 주지 않는다 — 그래서 `AdjustMoveNotSupported` 로 명시적으로 실패한다.

⚠️ **미래 대비 함수를 미리 만들지 않는다.** 그 축의 정본이 서기 전에 만들면
   지어낸 값이 된다 — 그래서 각 WP 가 정본을 세운 뒤에 함수를 만들었다.

```text
inbound_schedule_at    W3-2   inbound_schedules 가 섰다      → inbound_schedules.py 소유
reservation_state_at   WP-3   released_as_of 가 섰다         → 이 파일
outbound_schedule_at   WP-3   sales · sale_items 가 정본이다  → 이 파일
```
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.outbound_schedules import confirmed_outbound_at
from app.logistics.repository import LOGISTICS_POLICY_USAGE_SCOPE, _normalize_grade
from app.logistics.schemas import ScheduledQuantity
from app.logistics.turnover import LotTurnover, _lot_turnover_from_row

__all__ = [
    "CAPACITY_BASIS_CURRENT_ACTIVE_POLICY",
    "AdjustMoveNotSupported",
    "HistoricalCapacity",
    "HistoricalLot",
    "HistoricalLotState",
    "HistoricalPalletPosition",
    "HistoricalReceipt",
    "HistoricalReceiptState",
    "HistoricalReservation",
    "HistoricalReservationState",
    "ReceiptLineageAmbiguous",
    "RuntimeSnapshotCoverage",
    "lot_state_at",
    "onhand_by_lot_at",
    "onhand_total_by_day",
    "outbound_schedule_at",
    "pallet_position_at",
    "receipt_state_at",
    "reservation_state_at",
    "runtime_coverage_at",
    "snapshot_days_between",
    "timestamp_cutoff",
]

#: 시뮬레이션 달력의 시간대. `sim_time.phase_instant` 와 같은 시간대다.
_KST = ZoneInfo("Asia/Seoul")

#: 용량 정책에 유효일 컬럼이 없다는 사실을 응답에 적는 값. **정책 이력 표를 만들지
#: 않는다** — 한계를 숨기지 않고 그대로 말하는 쪽을 고른다.
CAPACITY_BASIS_CURRENT_ACTIVE_POLICY = "CURRENT_ACTIVE_POLICY"

#: 유도되는 Lot 상태. 🔴 **`HOLD` 가 없다** — 그 상태를 쓰는 writer 가 하나도 없어
#: (`ledger.py` 도 잔량 0 에 `DEPLETED` 를 안 적는다) 되살릴 사건이 없다.
#: 없는 것을 만들지 않는다 — `inventory_lot_events` 는 이번 MVP 에서 만들지 않는다.
HistoricalLotState = Literal["ACTIVE", "DEPLETED", "DISPOSED"]

#: 유도되는 예약 상태. 🔴 **어휘가 둘뿐이다** — 그날 잡고 있었나 아닌가.
#: `RESERVED` · `PARTIALLY_ALLOCATED` · `ALLOCATED` 세 값은 **할당 진행도**라
#: 예약 축의 사건이 아니고, 되살릴 사건 기록(`inventory_reservation_events`)이 없다.
#: 그 진행도를 과거로 알고 싶으면 할당 축(`decided_at` · OUT Move)을 본다.
HistoricalReservationState = Literal["HOLDING", "RELEASED"]

#: 유도되는 Receipt 상태. 🔴 **`INSPECTING` · `CLOSED` 가 없다** — 그 둘은 사건이
#: 아니라 진행 표시라 되살릴 사실이 없다. Current 화면 어휘로 남는다.
HistoricalReceiptState = Literal["ARRIVED", "INSPECTED", "PUTAWAY_DONE"]


class AdjustMoveNotSupported(RuntimeError):
    """`ADJUST` 이동이 조회 범위에 있다. **부호를 모르므로 계산하지 않는다.**

    🔴 0 으로 치거나 `IN` 으로 넘겨짚지 않는다. 그렇게 그린 재고 곡선은
       틀렸다는 것조차 알려 주지 않는다. `ADJUST_IN` / `ADJUST_OUT` 로 갈리는 날
       (`23_inventory_move_type_split.sql`) 이 예외가 사라진다.
    """


@dataclass(frozen=True)
class HistoricalLot:
    """`as_of` 시점의 Lot 하나. **잔량도 상태도 원장에서 나온다.**"""

    lot_id: str
    item_id: str
    item_name: str | None
    #: `repository._normalize_grade` 를 지난 값. Lot 생성 시 정해지는 정적 속성이라
    #: 원장이 아니라 행에서 읽어도 과거가 왜곡되지 않는다.
    grade: str | None
    storage_zone: str | None
    received_at: date
    #: 🔴 **`IN − OUT − DISPOSE` 누계다.** `remaining_qty_kg` 컬럼이 아니다.
    remaining_qty_kg: Decimal
    state: HistoricalLotState
    #: 신선도·회전 파생값. `turnover` 가 정본이고 여기서 다시 계산하지 않는다 —
    #: 넣어 주는 것은 **그 시점 잔량**뿐이다.
    turnover: LotTurnover


@dataclass(frozen=True)
class HistoricalReceipt:
    """`as_of` 시점의 입고 Receipt 하나. **`receipt_status` 컬럼을 읽지 않는다.**"""

    receipt_id: str
    inbound_id: str | None
    item_id: str
    item_name: str | None
    arrived_at: date
    ordered_qty_kg: Decimal | None
    accepted_qty_kg: Decimal | None
    hold_qty_kg: Decimal | None
    rejected_qty_kg: Decimal | None
    fact_source: str
    state: HistoricalReceiptState
    inspection_id: str | None
    inspection_verdict: str | None
    inspected_qty_kg: Decimal | None
    lot_id: str | None
    in_move_id: str | None

    @property
    def stock_applied(self) -> bool:
        """Lot 과 원장 IN 이 **둘 다** `as_of` 까지 있어야 재고가 섰다고 본다."""
        return self.lot_id is not None and self.in_move_id is not None


@dataclass(frozen=True)
class HistoricalPalletPosition:
    """`as_of` 시점의 Pallet 자리. **`pallets.current_location_id` 를 안 읽는다.**

    ⚠️ **Pallet `status` 는 유도하지 않는다.** 사건 어휘(`CREATED` · `RELOCATED` ·
       `HOLD_MOVED` · `EMPTIED`)와 상태 어휘(`ACTIVE` · `HOLD` · `EMPTIED` ·
       `DISPOSED`)가 1:1 이 아니다 — `move_pallet` 은 상태를 그대로 두고 사건만
       적는다. 사건이 증명하는 것은 **자리**뿐이라 자리만 되살린다.
    """

    pallet_id: str
    lot_id: str
    location_id: str | None
    zone_id: str | None
    last_event_type: str
    occurred_at: datetime

    @property
    def occupies_position(self) -> bool:
        return self.location_id is not None


@dataclass(frozen=True)
class HistoricalCapacity:
    """창고 kg Capacity. 🔴 **정책에 유효일이 없어 과거로 되살릴 수 없다.**"""

    used_capacity_kg: Decimal
    guaranteed_capacity_kg: Decimal | None
    burst_capacity_kg: Decimal | None
    #: `CURRENT_ACTIVE_POLICY` — 지금 활성 정책을 그대로 썼다는 표시.
    capacity_basis: str = CAPACITY_BASIS_CURRENT_ACTIVE_POLICY


@dataclass(frozen=True)
class HistoricalReservation:
    """`as_of` 시점의 예약 하나. **살아 있었나를 `released_as_of` 하나로 가른다.**

    🔴 **`inventory_reservations.status` 를 과거 정본으로 안 쓴다.** 그 칸은 **지금**
       값이라, 오늘 놓아준 예약이 과거 화면에서도 놓아준 것으로 보인다 —
       `remaining_qty_kg` 를 과거 잔량으로 쓰면 안 되는 것과 정확히 같은 잘못이다.

    ⚠️ **`created_at` · `updated_at` 도 안 쓴다.** 벽시각이라 DB 를 손본 시각이지
       시뮬레이션 사실일이 아니다 (`released_as_of` 를 만든 이유가 그것이다).
    """

    reservation_id: str
    sim_run_id: str
    item_id: str
    item_name: str | None
    sale_id: str | None
    required_qty_kg: Decimal
    reserved_qty_kg: Decimal
    #: 🔴 유도값이다. 저장된 `status` 가 아니다.
    state: HistoricalReservationState
    released_as_of: date | None
    #: 그날까지 이 예약이 Lot 에 붙여 둔 몫 (`decided_at < cutoff` · 취소 안 된 것).
    allocated_qty_kg: Decimal
    #: 그날까지 원장 OUT 으로 실제 나간 몫 (`MOVE-OUT-{allocation_id}` · `moved_at <= as_of`).
    shipped_qty_kg: Decimal


class ReceiptLineageAmbiguous(RuntimeError):
    """한 Receipt 에 검수·Lot·원장 IN 이 **둘 이상** 붙어 있다. 무결성 위반이다.

    🔴 **하나를 고르지 않는다.** `inbound_inspections.receipt_id` 에도
       `inventory_lots.inbound_receipt_id` 에도 UNIQUE 가 없어 스키마상 둘이 설 수
       있고, `inspections.find_inspection` 은 그때 이미 멈춘다
       (`InspectionIntegrityError`). Historical Reader 만 JOIN 곱을 그대로 흘려
       **같은 Receipt 를 여러 줄로** 내보내면 깨진 계보가 정상 화면으로 그려진다.

    ⚠️ 앞 행이 뒤 행을 덮게 두는 것도 고르는 것이다 — 그래서 값이 아니라 예외다.
    """


@dataclass(frozen=True)
class RuntimeSnapshotCoverage:
    """그날 이 실행의 **Agent Runtime Snapshot 행이 있나** (`logistics_runtime_fixture`).

    🔴 **물리 사실(Lot · Move · Receipt)의 최소~최대 날짜로 대신 판정하지 않는다.**
       그 판정은 **양쪽으로** 틀린다 (실측 `SIM-BURNIN-202512` · 2026-09-09).

    ```text
    물리 사실 구간   2025-12-02 ~ 2026-09-12   285 일
    사실이 있는 날                              24 일   ← 구간의 8%
    스냅샷 행                                  254 일

    구간 안인데 스냅샷이 없는 날                31 일   ① 안 연 날을 «열렸다» 고 한다
      2025-12-02 ~ 2025-12-30  씨앗 구간 (원장 이동 215 건)
      2026-01-03 · 2026-01-04  스냅샷이 실제로 비어 있는 이틀
    스냅샷은 있는데 물리 사실이 없는 날         245 일   ② 열린 날을 «모른다» 고 한다
    ```

       ★ ② 가 특히 중요하다 — **입·출고가 0 건인 정상 하루는 Lot 도 Move 도
         Receipt 도 안 남긴다.** 물리 사실로는 그런 날을 증명할 방법이 **아예 없다.**

    ★ **축은 `(sim_run_id, as_of, usage_scope, is_active)` 다** —
      `uq_log_runtime_fixture` · `repository.get_active_logistics_runtime_fixture` 와
      같은 축이라 *"열렸다고 본 행"* 과 *"읽은 행"* 이 갈리지 않는다.

    ⚠️ `first_as_of` · `last_as_of` 는 **사람에게 보여 줄 맥락**일 뿐 판정 근거가
       아니다. 판정은 `has_snapshot` 하나가 하고, 그 값은 하루를 정확히 본다.
    """

    as_of: date
    #: 🔴 이 값 하나가 `NO_DATA` 판정이다. 구간 비교를 하지 않는다.
    has_snapshot: bool
    first_as_of: date | None
    last_as_of: date | None


def timestamp_cutoff(as_of: date) -> datetime:
    """TIMESTAMPTZ 컬럼용 상한. **`< cutoff` 로 쓴다 (`<=` 아니다).**

    ```text
    cutoff = (as_of + 1 달력일) 00:00 Asia/Seoul
    ```

    🔴 **`col::date <= as_of` 로 쓰지 않는다.** 그 비교는 서버 `TimeZone` 설정에
       따라 하루가 밀리고, 밀린 것을 아무도 알아채지 못한다. tz-aware 파라미터를
       넘겨 DB 가 같은 순간을 보게 한다.
    """
    return datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=_KST)


def _schema() -> sql.Identifier:
    return sql.Identifier(get_db_schema())


def _rows(conn: Any, query: sql.Composed, params: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


#: 원장 누계 한 조각. **`ADJUST` 는 더하지 않고 세기만 한다** — 세어 둔 것을 보고
#: 호출부가 멈춘다.
#:
#: 🔴 **잔량을 0 으로 만든 날(= 마지막 이동일)의 종류를 함께 센다.** `_lot_state` 가
#:    «폐기로 비었나» 를 가르는 데 그 하루가 필요하다 — 총 폐기량만 보면 **부분 폐기
#:    뒤 판매로 소진된 Lot** 을 폐기된 Lot 과 구별할 수 없다 (실측 2건).
_LEDGER_AGGREGATE = sql.SQL(
    """
    SELECT m.lot_id,
           COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type = 'IN'), 0)
             - COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type IN ('OUT', 'DISPOSE')), 0)
               AS balance_kg,
           COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type = 'DISPOSE'), 0) AS disposed_kg,
           count(*) FILTER (WHERE m.move_type = 'ADJUST')::int AS adjust_count,
           count(*) FILTER (
               WHERE m.move_type = 'DISPOSE' AND m.moved_at = m.last_moved_on
           )::int AS dispose_on_last_day,
           count(*) FILTER (
               WHERE m.move_type = 'OUT' AND m.moved_at = m.last_moved_on
           )::int AS out_on_last_day
    FROM (
        SELECT mv.lot_id, mv.move_type, mv.quantity_kg, mv.moved_at,
               max(mv.moved_at) OVER (PARTITION BY mv.lot_id) AS last_moved_on
        FROM {schema}.inventory_moves mv
        WHERE mv.sim_run_id = %(sim)s
          AND mv.moved_at <= %(as_of)s
    ) m
    GROUP BY m.lot_id
    """
)


def onhand_by_lot_at(conn: Any, *, sim_run_id: str, as_of: date) -> dict[str, Decimal]:
    """`as_of` 시점의 Lot 별 잔량. **정본은 `inventory_moves` 다.**

    ```text
    balance(lot, as_of) = Σ IN − Σ OUT − Σ DISPOSE      (moved_at <= as_of)
    ```

    ★ 이동이 하나도 없는 Lot 은 **키에 없다** (0 이 아니라 «움직인 적 없음»).
      Lot 목록과 합칠 때 그 자리를 0 으로 읽을지 호출부가 정한다.

    :raises AdjustMoveNotSupported: 범위 안에 `ADJUST` 가 있을 때.
    """
    rows = _rows(
        conn,
        _LEDGER_AGGREGATE.format(schema=_schema()),
        {"sim": sim_run_id, "as_of": as_of},
    )
    _reject_adjust(rows, sim_run_id=sim_run_id, as_of=as_of)
    return {row["lot_id"]: _decimal(row["balance_kg"]) for row in rows}


def _reject_adjust(rows: list[dict[str, Any]], *, sim_run_id: str, as_of: date) -> None:
    lots = [row["lot_id"] for row in rows if int(row.get("adjust_count") or 0) > 0]
    if lots:
        raise AdjustMoveNotSupported(
            "방향을 모르는 ADJUST 이동이 있어 과거 잔량을 계산하지 않는다 "
            f"(sim_run_id={sim_run_id!r} · as_of={as_of} · lot_id={sorted(lots)})."
        )


def lot_state_at(conn: Any, *, sim_run_id: str, as_of: date) -> tuple[HistoricalLot, ...]:
    """`as_of` 시점에 존재한 Lot 전부 — 잔량 · 상태 · 신선도 · 회전.

    ```text
    존재      received_at <= as_of
    DISPOSED  DISPOSE Move 가 as_of 까지 있다
    DEPLETED  원장 잔량 == 0
    ACTIVE    그 외
    ```

    🔴 **`inventory_lots.status` 를 읽지 않는다.** 그 칸은 Current 값이고, 지금
       실측은 84 Lot 중 77 이 `DEPLETED` · 6 이 `DISPOSED` 다 — 그대로 과거 화면에
       실으면 2026-01-05 의 살아 있던 재고가 전부 «소진» 으로 보인다.

    ⚠️ **잔량 0 인 Lot 도 돌려준다.** 걸러내는 것은 화면의 판단이지 사실이 아니다.

    ★ **보관·회전 정책은 `LEFT JOIN` 이다.** `INNER JOIN` 하면 정책이 없는 품목의
      **실물 재고가 조회에서 통째로 사라진다** (`turnover.load_lot_turnover` 의
      같은 경고). 정책이 없다는 이유로 있는 재고를 지우지 않는다.

    :raises AdjustMoveNotSupported: 범위 안에 `ADJUST` 가 있을 때.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT l.lot_id, l.item_id, i.item_name, l.grade, l.storage_zone, l.received_at,
                   sp.operational_limit_days, sp.medium_grade_factor,
                   tp.operational_turnover_target_days AS turnover_target_days,
                   tp.sell_priority_remaining_days,
                   COALESCE(mv.balance_kg, 0) AS balance_kg,
                   COALESCE(mv.disposed_kg, 0) AS disposed_kg,
                   COALESCE(mv.adjust_count, 0) AS adjust_count,
                   COALESCE(mv.dispose_on_last_day, 0) AS dispose_on_last_day,
                   COALESCE(mv.out_on_last_day, 0) AS out_on_last_day
            FROM {schema}.inventory_lots l
            JOIN {schema}.items i ON i.item_id = l.item_id
            LEFT JOIN {schema}.item_storage_policies sp ON sp.item_id = l.item_id
            LEFT JOIN {schema}.item_turnover_policies tp ON tp.item_id = l.item_id
            LEFT JOIN ({ledger}) mv ON mv.lot_id = l.lot_id
            WHERE l.sim_run_id = %(sim)s
              AND l.received_at <= %(as_of)s
            ORDER BY l.lot_id
            """
        ).format(schema=schema, ledger=_LEDGER_AGGREGATE.format(schema=schema)),
        {"sim": sim_run_id, "as_of": as_of},
    )
    _reject_adjust(rows, sim_run_id=sim_run_id, as_of=as_of)

    lots: list[HistoricalLot] = []
    for row in rows:
        balance = _decimal(row["balance_kg"])
        lots.append(
            HistoricalLot(
                lot_id=row["lot_id"],
                item_id=row["item_id"],
                item_name=row["item_name"],
                grade=_normalize_grade(row["grade"]),
                storage_zone=row["storage_zone"],
                received_at=row["received_at"],
                remaining_qty_kg=balance,
                state=_lot_state(
                    balance=balance,
                    dispose_on_last_day=int(row["dispose_on_last_day"] or 0),
                    out_on_last_day=int(row["out_on_last_day"] or 0),
                ),
                # ★ `turnover` 가 쓰는 그 함수에 **그 시점 잔량**만 바꿔 넣는다.
                #   신선도·회전 공식을 여기서 다시 적으면 두 답이 갈린다.
                turnover=_lot_turnover_from_row({**row, "remaining_qty_kg": balance}, as_of=as_of),
            )
        )
    return tuple(lots)


def _lot_state(
    *, balance: Decimal, dispose_on_last_day: int, out_on_last_day: int
) -> HistoricalLotState:
    """Lot 상태를 **쓰는 쪽 규칙 그대로** 되살린다.

    ```text
    잔량 > 0                                 ACTIVE
    잔량 0 · 비운 날에 DISPOSE 있고 OUT 없음   DISPOSED
    그 외 (잔량 0)                            DEPLETED
    ```

    🔴 **«폐기 이동이 있었나» 로 갈라서는 안 된다.** `disposal._mark_disposed` 는
       **그 DISPOSE 가 잔량을 0 으로 만들었을 때만** `DISPOSED` 를 적는다 —
       *"부분 폐기에는 붙이지 않는다"* 가 그 함수 첫 줄이고, `WHERE … AND
       remaining_qty_kg = 0` 이 SQL 로도 그것을 막는다. 총 폐기량이 0 보다 크다는
       것만 보면 **30kg 만 버리고 70kg 는 정상 출고한 Lot** 이 «전량 폐기» 로 둔갑한다.

    ```text
    실측 (SIM-BURNIN-202512 · 2026-09-09)
      DISPOSE 이동이 있는 Lot        8
        DB status = DISPOSED         6
        DB status = DEPLETED         2   ← 부분 폐기 뒤 OUT 으로 소진됐다
    ```

       종전 판정은 그 2건을 `DISPOSED` 로 냈다. 위 규칙은 8/8 을 DB 와 같게 낸다.

    ⚠️ **같은 날 OUT 과 DISPOSE 가 함께 있으면 `DISPOSED` 라고 하지 않는다.**
       `inventory_moves.moved_at` 은 DATE 라 하루 안의 순서가 없어(실측: 그런 Lot 2건)
       어느 쪽이 잔량을 0 으로 만들었는지 증명할 수 없다. 증명 안 되는 «폐기» 를
       적기보다 *"비었다"* 까지만 말한다 — 모르는 것을 아는 척하지 않는 그 규율이다.
    """
    if balance > 0:
        return "ACTIVE"
    if dispose_on_last_day > 0 and out_on_last_day == 0:
        return "DISPOSED"
    return "DEPLETED"


def receipt_state_at(conn: Any, *, sim_run_id: str, as_of: date) -> tuple[HistoricalReceipt, ...]:
    """`as_of` 시점의 입고 Receipt — **상태를 세 사건에서 유도한다.**

    ```text
    ARRIVED       arrived_at <= as_of
    INSPECTED     검수 inspected_at < cutoff
    PUTAWAY_DONE  그 Receipt 의 Lot 과 원장 IN 이 as_of 까지 있다
    ```

    🔴 **`receipt_status` 컬럼을 읽지 않는다.** 실측 4건이 전부 `PUTAWAY_DONE` 인데,
       그 값을 과거 화면에 실으면 도착만 한 날에도 «입고 완료» 로 보인다.
       `inbound_receipt_events` 표를 만들지 않는 근거가 바로 사건 셋으로 4/4 가
       유도된다는 실측이다.

    🔴 **한 Receipt 가 두 줄로 나오면 멈춘다.** 아래 세 `LEFT JOIN` 은 전부 1:N 이
       **가능한** 관계다 — `inbound_inspections.receipt_id` 에도
       `inventory_lots.inbound_receipt_id` 에도 UNIQUE 가 없다(DDL 실측).
       깨지면 JOIN 곱으로 같은 Receipt 가 여러 `HistoricalReceipt` 가 되어
       화면이 도착 건수를 **부풀린 채 정상으로** 그린다.

       ★ `inspections.find_inspection` 이 같은 상황을 이미 `0 / 1 / 2행 이상` 으로
         갈라 2행 이상에서 멈춘다(`InspectionIntegrityError` · *"어느 것이 진짜인지
         여기서 고르지 않는다"*). Historical Reader 도 같은 규율을 지킨다 —
         **읽기라고 무결성 방어를 빼지 않는다.**

    :raises ReceiptLineageAmbiguous: 한 `receipt_id` 가 두 줄 이상으로 돌아올 때.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT r.receipt_id, r.inbound_id, r.item_id, i.item_name, r.arrived_at,
                   r.ordered_qty_kg, r.accepted_qty_kg, r.hold_qty_kg, r.rejected_qty_kg,
                   r.fact_source,
                   ins.inspection_id, ins.verdict AS inspection_verdict, ins.inspected_qty_kg,
                   l.lot_id, mv.move_id AS in_move_id
            FROM {schema}.inbound_receipts r
            LEFT JOIN {schema}.items i ON i.item_id = r.item_id
            LEFT JOIN {schema}.inbound_inspections ins
                   ON ins.receipt_id = r.receipt_id
                  AND ins.inspected_at < %(cutoff)s
            LEFT JOIN {schema}.inventory_lots l
                   ON l.inbound_receipt_id = r.receipt_id
                  AND l.sim_run_id = r.sim_run_id
                  AND l.received_at <= %(as_of)s
            LEFT JOIN {schema}.inventory_moves mv
                   ON mv.lot_id = l.lot_id
                  AND mv.sim_run_id = r.sim_run_id
                  AND mv.move_type = 'IN'
                  AND mv.moved_at <= %(as_of)s
            WHERE r.sim_run_id = %(sim)s
              AND r.arrived_at <= %(as_of)s
            ORDER BY r.arrived_at DESC, r.receipt_id
            """
        ).format(schema=schema),
        {"sim": sim_run_id, "as_of": as_of, "cutoff": timestamp_cutoff(as_of)},
    )
    _reject_ambiguous_receipts(rows, sim_run_id=sim_run_id, as_of=as_of)
    return tuple(
        HistoricalReceipt(
            receipt_id=row["receipt_id"],
            inbound_id=row["inbound_id"],
            item_id=row["item_id"],
            item_name=row["item_name"],
            arrived_at=row["arrived_at"],
            ordered_qty_kg=row["ordered_qty_kg"],
            accepted_qty_kg=row["accepted_qty_kg"],
            hold_qty_kg=row["hold_qty_kg"],
            rejected_qty_kg=row["rejected_qty_kg"],
            fact_source=row["fact_source"],
            state=_receipt_state(row),
            inspection_id=row["inspection_id"],
            inspection_verdict=row["inspection_verdict"],
            inspected_qty_kg=row["inspected_qty_kg"],
            lot_id=row["lot_id"],
            in_move_id=row["in_move_id"],
        )
        for row in rows
    )


def _reject_ambiguous_receipts(
    rows: list[dict[str, Any]], *, sim_run_id: str, as_of: date
) -> None:
    """한 `receipt_id` 가 두 줄 이상이면 멈춘다. **하나를 고르지 않는다.**

    ★ 세 JOIN(검수 · Lot · 원장 IN) 중 **어디서** 늘었는지까지 적는다 — 그래야
      고칠 사람이 어느 표를 볼지 안다.
    """
    seen: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        seen.setdefault(row["receipt_id"], []).append(row)
    겹친것 = {receipt_id: found for receipt_id, found in seen.items() if len(found) > 1}
    if not 겹친것:
        return
    상세 = []
    for receipt_id, found in sorted(겹친것.items()):
        상세.append(
            f"{receipt_id}: {len(found)}줄"
            f" (inspection {sorted({r['inspection_id'] for r in found})!r}"
            f" · lot {sorted({r['lot_id'] for r in found})!r}"
            f" · in_move {sorted({r['in_move_id'] for r in found})!r})"
        )
    raise ReceiptLineageAmbiguous(
        "한 Receipt 에 검수·Lot·원장 IN 이 둘 이상 붙어 있다"
        f" (sim_run_id={sim_run_id!r} · as_of={as_of}): {' / '.join(상세)}."
        " 어느 것이 진짜인지 여기서 고르지 않는다 —"
        " 앞 행이 뒤 행을 덮게 두는 것도 고르는 것이다."
    )


def _receipt_state(row: dict[str, Any]) -> HistoricalReceiptState:
    if row["lot_id"] is not None and row["in_move_id"] is not None:
        return "PUTAWAY_DONE"
    if row["inspection_id"] is not None:
        return "INSPECTED"
    return "ARRIVED"


def pallet_position_at(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[HistoricalPalletPosition, ...]:
    """`as_of` 시점의 Pallet 자리 — **`pallet_events` 를 재생한다.**

    cutoff 이전 **마지막 사건**이 그날의 자리다. `EMPTIED` 면 자리가 없다.

    🔴 **`pallets.current_location_id` 를 과거 위치로 쓰지 않는다.** 그 칸은 지금
       위치이고, 실측 3장은 전부 2026-09-12 에 `EMPTIED` 되어 지금 자리가 없다.

    ⚠️ **사건이 하나도 없는 Pallet 은 빼고 돌려준다.** 그날 그 Pallet 은 기록상
       존재하지 않았다 — 없는 자리를 지어내지 않는다. 실측 `CREATED` 3건은
       벽시각(2026-09-04)이라 2026-01 대 조회에서는 자연히 빠지고, 그 사실은
       *"그때는 Pallet 기록이 없었다"* 로 화면에 나가야 한다.

    ★ 실행 격리는 `pallets → inventory_lots.sim_run_id` 로 한다 — `pallet_events`
      에 `sim_run_id` 컬럼이 없다. 컬럼을 새로 만들지 않는다.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT DISTINCT ON (p.pallet_id)
                   p.pallet_id, p.lot_id,
                   e.event_type, e.to_location_id, e.occurred_at,
                   sl.zone_id
            FROM {schema}.pallets p
            JOIN {schema}.inventory_lots l
              ON l.lot_id = p.lot_id
             AND l.sim_run_id = %(sim)s
            JOIN {schema}.pallet_events e
              ON e.pallet_id = p.pallet_id
             AND e.occurred_at < %(cutoff)s
            LEFT JOIN {schema}.storage_locations sl ON sl.location_id = e.to_location_id
            ORDER BY p.pallet_id, e.occurred_at DESC, e.pallet_event_id DESC
            """
        ).format(schema=schema),
        {"sim": sim_run_id, "cutoff": timestamp_cutoff(as_of)},
    )
    return tuple(
        HistoricalPalletPosition(
            pallet_id=row["pallet_id"],
            lot_id=row["lot_id"],
            # `EMPTIED` 는 자리를 돌려준 사건이다 — `to_location_id` 가 NULL 이다.
            location_id=row["to_location_id"],
            zone_id=row["zone_id"],
            last_event_type=row["event_type"],
            occurred_at=row["occurred_at"],
        )
        for row in rows
    )


def capacity_at(
    *,
    used_capacity_kg: Decimal,
    guaranteed_capacity_kg: Decimal | None,
    burst_capacity_kg: Decimal | None,
) -> HistoricalCapacity:
    """창고 kg Capacity 한 벌. 🔴 **한계를 `capacity_basis` 로 말한다.**

    `agent_policy_config` 에 유효일 컬럼이 없어 «그날 그 정책이었나» 를 알 수 없다.
    유효일 컬럼을 새로 만드는 것은 이번 범위가 아니므로(`07 §15`), 지금 활성
    정책을 쓰되 **그 사실을 응답에 적는다.** 조용히 과거 값인 척하지 않는다.

    ★ `used_capacity_kg` 는 호출부가 넘긴 **그 시점 원장 합**이다 — 캐시 합이 아니다.
    """
    return HistoricalCapacity(
        used_capacity_kg=used_capacity_kg,
        guaranteed_capacity_kg=guaranteed_capacity_kg,
        burst_capacity_kg=burst_capacity_kg,
    )


def onhand_total_by_day(
    conn: Any, *, sim_run_id: str, start: date, end: date
) -> dict[date, Decimal]:
    """`start`~`end` 각 날 **마지막 시점**의 창고 전체 보유량.

    ```text
    opening(start−1)  =  Σ(moved_at <  start)
    on_hand(D)        =  on_hand(D−1) + net(D)
    ```

    🔴 **현재 잔량을 앵커로 잡고 거슬러 올라가지 않는다.** 종전 `_onhand_series` 는
       `remaining_qty_kg` 합을 오늘 칸에 놓고 역산했는데, 그 앵커가 캐시라
       **모든 과거 칸이 같이 틀렸다.** 여기서는 시작 잔고부터 앞으로 더한다.

    ⚠️ **`LIMIT` 을 두지 않는다.** 종전에는 `limit=1000` 으로 원장을 자른 뒤 합을
       냈다 — 잘린 줄이 하나라도 있으면 선 전체가 조용히 틀어진다.

    :raises AdjustMoveNotSupported: 범위 안에 `ADJUST` 가 있을 때.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT m.moved_at,
                   COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type = 'IN'), 0)
                     - COALESCE(SUM(m.quantity_kg)
                                FILTER (WHERE m.move_type IN ('OUT', 'DISPOSE')), 0) AS net_kg,
                   count(*) FILTER (WHERE m.move_type = 'ADJUST')::int AS adjust_count
            FROM {schema}.inventory_moves m
            WHERE m.sim_run_id = %(sim)s
              AND m.moved_at <= %(end)s
            GROUP BY m.moved_at
            ORDER BY m.moved_at
            """
        ).format(schema=schema),
        {"sim": sim_run_id, "end": end},
    )
    adjust_days = [row["moved_at"] for row in rows if int(row["adjust_count"] or 0) > 0]
    if adjust_days:
        raise AdjustMoveNotSupported(
            "방향을 모르는 ADJUST 이동이 있어 재고 추이를 계산하지 않는다 "
            f"(sim_run_id={sim_run_id!r} · moved_at={sorted(adjust_days)})."
        )

    net_by_day = {row["moved_at"]: _decimal(row["net_kg"]) for row in rows}
    running = sum((qty for day, qty in net_by_day.items() if day < start), Decimal(0))

    series: dict[date, Decimal] = {}
    day = start
    while day <= end:
        running += net_by_day.get(day, Decimal(0))
        series[day] = running
        day += timedelta(days=1)
    return series


def runtime_coverage_at(conn: Any, *, sim_run_id: str, as_of: date) -> RuntimeSnapshotCoverage:
    """그날 이 실행의 Runtime Snapshot 행이 있나 — **하루를 정확히 본다.**

    🔴 **구간(MIN~MAX)으로 판정하지 않는다.** 근거는 `RuntimeSnapshotCoverage`
       docstring 의 실측 세 줄이다 (구간 안 미개장 31일 · 사실 없는 정상일 245일).

    ★ 함께 돌려주는 `first_as_of` · `last_as_of` 는 화면 문구용 맥락이다 —
      *"2028-01-17 은 이 실행에 없다"* 만 적으면 어디를 물어야 할지 알 수 없다.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT
                bool_or(f.as_of = %(as_of)s) AS has_snapshot,
                min(f.as_of) AS first_as_of,
                max(f.as_of) AS last_as_of
            FROM {schema}.logistics_runtime_fixture f
            WHERE f.sim_run_id = %(sim)s
              AND f.usage_scope = %(scope)s
              AND f.is_active
            """
        ).format(schema=schema),
        {"sim": sim_run_id, "as_of": as_of, "scope": LOGISTICS_POLICY_USAGE_SCOPE},
    )
    row = rows[0] if rows else {}
    return RuntimeSnapshotCoverage(
        as_of=as_of,
        # 🔴 행이 하나도 없으면 `bool_or` 가 NULL 이다 — `None` 을 참으로 읽지 않는다.
        has_snapshot=bool(row.get("has_snapshot")),
        first_as_of=row.get("first_as_of"),
        last_as_of=row.get("last_as_of"),
    )


def snapshot_days_between(
    conn: Any, *, sim_run_id: str, start: date, end: date
) -> frozenset[date]:
    """`start`~`end` 중 **Runtime Snapshot 이 실제로 있는 날들.**

    🔴 **그래프의 칸마다 그날을 따로 물어야 한다.** 원장 누계는 어떤 날짜에도 숫자를
       내고, 첫 사실 이전 구간에서는 그 숫자가 **0** 이다. 그 0 은
       *"확인했고 재고가 없다"* 가 아니라 *"그날을 모른다"* 인데, 화면 계약에서 그
       둘은 다른 값이다 — 안 가르면 **안 연 날이 «재고 0kg» 선으로 그려진다.**

    ★ 창이 12칸이라 한 질의로 집합을 받아 온다 — 칸마다 묻지 않는다.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT f.as_of
            FROM {schema}.logistics_runtime_fixture f
            WHERE f.sim_run_id = %(sim)s
              AND f.usage_scope = %(scope)s
              AND f.is_active
              AND f.as_of BETWEEN %(start)s AND %(end)s
            """
        ).format(schema=schema),
        {
            "sim": sim_run_id,
            "scope": LOGISTICS_POLICY_USAGE_SCOPE,
            "start": start,
            "end": end,
        },
    )
    return frozenset(row["as_of"] for row in rows)


# ── 출고 축 (WP-3) ──────────────────────────────────────────────────────


def reservation_state_at(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[HistoricalReservation, ...]:
    """`as_of` 시점의 예약 — **살아 있었나를 `released_as_of` 로 유도한다.**

    ```text
    released_as_of IS NULL       HOLDING     아직 놓아준 적이 없다
    released_as_of >  as_of      HOLDING     그날에는 아직 살아 있었다
    released_as_of <= as_of      RELEASED    그날에는 이미 놓아준 뒤다
    ```

    🔴 **`status` 컬럼을 읽지 않는다.** 실측(2026-09-09) 8행이 전부 `ALLOCATED`
       인데, 그 값을 과거 화면에 실으면 **아직 예약도 안 한 날에도 «할당 완료»** 로
       보인다. `receipt_state_at` 이 `receipt_status` 를 안 읽는 것과 같은 규율이다.

    ⚠️ **`created_at` 으로 «그날 예약이 있었나» 를 자르지 않는다.** 그것은 벽시각이라
       DB 를 손본 시각이다. 예약의 **생성 시뮬레이션 날짜 칸은 아직 없고**, 지어내지
       않는다 — 대신 그 예약이 실제로 만든 사실(할당 · OUT Move)의 시간축으로
       진행도를 유도한다. 그래서 이 함수는 **그 실행의 예약 전부**를 돌려주고
       `state` 로 그날 잡고 있었는지를 가른다.

       ★ 이것이 «없는 사실을 지어내지 않는다» 의 실제 모습이다. 놓아준 날은 알 수
         있게 됐고(M3), 잡은 날은 아직 모른다 — 아는 것만 답한다.

    ```text
    allocated_qty_kg   decided_at < cutoff 인 살아있는 할당의 합
    shipped_qty_kg     그 할당의 원장 OUT 중 moved_at <= as_of 인 것의 합
    ```

       🔴 **할당도 `status` 로 안 센다.** `decided_at`(시뮬레이션 시간축)과 원장 OUT
          두 사건으로 유도한다 — `inventory_allocations.status` 역시 지금 값이다.
          다만 **취소된 할당은 뺀다**: 취소 시점 칸이 없어 «그날 취소돼 있었나» 를
          알 수 없고, WP-3 이 되살리기를 **같은 날 안으로 묶었으므로**(
          `outbound._되살려도_되는_날인지_본다`) 날짜를 넘긴 취소는 뒤집히지 않는다.

    ★ **축은 `(sim_run_id, as_of)` 다.** 실행을 안 좁히면 남의 실행 예약이 섞인다.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT r.reservation_id,
                   r.sim_run_id,
                   r.item_id,
                   i.item_name,
                   r.sale_id,
                   r.required_qty_kg,
                   r.reserved_qty_kg,
                   r.released_as_of,
                   COALESCE(a.allocated_qty_kg, 0) AS allocated_qty_kg,
                   COALESCE(a.shipped_qty_kg, 0)   AS shipped_qty_kg
            FROM {schema}.inventory_reservations r
            LEFT JOIN {schema}.items i ON i.item_id = r.item_id
            LEFT JOIN (
                SELECT al.reservation_id,
                       SUM(al.allocated_qty_kg) AS allocated_qty_kg,
                       SUM(COALESCE(mv.quantity_kg, 0)) AS shipped_qty_kg
                FROM {schema}.inventory_allocations al
                LEFT JOIN {schema}.inventory_moves mv
                       ON mv.move_id = 'MOVE-OUT-' || al.allocation_id
                      AND mv.move_type = 'OUT'
                      AND mv.moved_at <= %(as_of)s
                WHERE al.decided_at < %(cutoff)s
                  AND al.status <> 'CANCELLED'
                GROUP BY al.reservation_id
            ) a ON a.reservation_id = r.reservation_id
            WHERE r.sim_run_id = %(sim)s
            ORDER BY r.reservation_id
            """
        ).format(schema=schema),
        {"sim": sim_run_id, "as_of": as_of, "cutoff": timestamp_cutoff(as_of)},
    )
    return tuple(
        HistoricalReservation(
            reservation_id=row["reservation_id"],
            sim_run_id=row["sim_run_id"],
            item_id=row["item_id"],
            item_name=row["item_name"],
            sale_id=row["sale_id"],
            required_qty_kg=_decimal(row["required_qty_kg"]),
            reserved_qty_kg=_decimal(row["reserved_qty_kg"]),
            state=_reservation_state(row["released_as_of"], as_of=as_of),
            released_as_of=row["released_as_of"],
            allocated_qty_kg=_decimal(row["allocated_qty_kg"]),
            shipped_qty_kg=_decimal(row["shipped_qty_kg"]),
        )
        for row in rows
    )


def _reservation_state(
    released_as_of: date | None, *, as_of: date
) -> HistoricalReservationState:
    """`released_as_of` 한 칸으로 그날 상태를 가른다. **벽시각을 안 본다.**

    ★ `released_as_of == as_of` 는 **RELEASED** 다 — 그날부터 놓아준 것이다
      (`inbound_schedules.cancelled_as_of` 와 같은 경계다).
    """
    if released_as_of is None or released_as_of > as_of:
        return "HOLDING"
    return "RELEASED"


def outbound_schedule_at(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[ScheduledQuantity, ...]:
    """`as_of` 에서 보였어야 하는 **미래 확정 출고**를 판매 정본에서 되살린다.

    🔴 **`confirmed_outbound_json` 을 안 읽는다 (WP-3).** 그 칸은 판매 확정이 채우는
       경로가 하나도 없어 실측 254행 전부 `[]` 였다 — 비어 있는 옛 정본을 과거 화면에
       실으면 *"그날 미래 출고가 없었다"* 가 **확인된 사실처럼** 나간다.

    ```text
    원천   sales · sale_items                    ← Current 축과 같은 정본
    축     sim_run_id · sale_date > as_of
    상태   order_status IN (CONFIRMED, READY)
    ```

    ⚠️ **`sales.order_status` 는 지금 값이다.** 그래서 이 함수는 *"그날 그 판매가
       확정 상태였나"* 를 정확히는 못 답한다 — 판매 상태의 시뮬레이션 날짜 칸이
       없어서다. **그 한계를 숨기지 않는다**: 지금 취소된 판매는 과거 화면에서도
       안 보이고, 지금 배송된 판매는 그 납품일 이전 화면에서 미래 출고로 안 선다.

       ★ 그럼에도 fixture JSON 보다 낫다. 저쪽은 **아무도 안 쓰는 빈 칸**이라 늘
         «0 건» 이고, 이쪽은 적어도 실재하는 판매 사실을 축으로 삼는다. 판매 상태
         이력 표는 판매 소유라 물류가 만들지 않는다 (파트 경계).

    ★ **Current 경로와 같은 함수를 쓴다** (`outbound_schedules.confirmed_outbound_at`).
      두 벌로 적으면 화면과 Runtime 이 다른 미래를 그린다.
    """
    return tuple(confirmed_outbound_at(conn, sim_run_id=sim_run_id, as_of=as_of))
