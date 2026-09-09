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

⚠️ **미래 대비 함수를 미리 만들지 않는다.** `inbound_schedule_at`(WP-2) ·
   `reservation_state_at` · `outbound_schedule_at`(WP-3) 은 그 축의 정본 컬럼
   (`inbound_schedules` · `inventory_reservations.released_as_of`)이 아직 없어
   **지금 만들면 지어낸 값이 된다.** 그 WP 에서 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.repository import _normalize_grade
from app.logistics.turnover import LotTurnover, _lot_turnover_from_row

__all__ = [
    "CAPACITY_BASIS_CURRENT_ACTIVE_POLICY",
    "AdjustMoveNotSupported",
    "FactCoverage",
    "HistoricalCapacity",
    "HistoricalLot",
    "HistoricalLotState",
    "HistoricalPalletPosition",
    "HistoricalReceipt",
    "HistoricalReceiptState",
    "fact_coverage",
    "lot_state_at",
    "onhand_by_lot_at",
    "onhand_total_by_day",
    "pallet_position_at",
    "receipt_state_at",
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
class FactCoverage:
    """이 실행이 **기록으로 덮고 있는 날짜 구간.** 새 컬럼을 만들지 않고 사실에서 센다.

    ★ `sim_runs.last_completed_as_of` 같은 컬럼을 만들지 않는다 — 마지막 완료일은
      Master 소유(#19)이고 물류가 그 값을 가질 이유가 없다. 여기서 재는 것은
      *"이 실행에 기록된 사실이 언제부터 언제까지 있나"* 하나다.
    """

    first_fact_on: date | None
    last_fact_on: date | None

    def covers(self, as_of: date) -> bool:
        """`as_of` 가 기록 구간 안인가.

        🔴 **밖이면 «0 kg» 이 아니라 «모른다» 다.** 마지막 사실 뒤의 날은 원장을
           더해도 마지막 잔고가 나오지만, 그것은 *"그날 창고가 그랬다"* 가 아니라
           *"그 뒤로 아무것도 기록되지 않았다"* 는 뜻이다. 시뮬레이션이 그날까지
           걸어가지 않았을 뿐이므로 사실로 내밀지 않는다.
        """
        if self.first_fact_on is None or self.last_fact_on is None:
            return False
        return self.first_fact_on <= as_of <= self.last_fact_on


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
_LEDGER_AGGREGATE = sql.SQL(
    """
    SELECT m.lot_id,
           COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type = 'IN'), 0)
             - COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type IN ('OUT', 'DISPOSE')), 0)
               AS balance_kg,
           COALESCE(SUM(m.quantity_kg) FILTER (WHERE m.move_type = 'DISPOSE'), 0) AS disposed_kg,
           count(*) FILTER (WHERE m.move_type = 'ADJUST')::int AS adjust_count
    FROM {schema}.inventory_moves m
    WHERE m.sim_run_id = %(sim)s
      AND m.moved_at <= %(as_of)s
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
                   COALESCE(mv.adjust_count, 0) AS adjust_count
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
                state=_lot_state(balance=balance, disposed_kg=_decimal(row["disposed_kg"])),
                # ★ `turnover` 가 쓰는 그 함수에 **그 시점 잔량**만 바꿔 넣는다.
                #   신선도·회전 공식을 여기서 다시 적으면 두 답이 갈린다.
                turnover=_lot_turnover_from_row({**row, "remaining_qty_kg": balance}, as_of=as_of),
            )
        )
    return tuple(lots)


def _lot_state(*, balance: Decimal, disposed_kg: Decimal) -> HistoricalLotState:
    if disposed_kg > 0 and balance <= 0:
        return "DISPOSED"
    if balance <= 0:
        return "DEPLETED"
    return "ACTIVE"


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


def fact_coverage(conn: Any, *, sim_run_id: str) -> FactCoverage:
    """이 실행에 기록된 사실의 **처음과 끝 날짜.** 컬럼이 아니라 사실에서 센다.

    ★ 세는 대상은 물류가 소유한 날짜 사실 셋이다 — Lot 입고일 · 원장 이동일 ·
      Receipt 도착일. 다른 도메인 표를 물류 조회 범위 판정에 끌어오지 않는다.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT min(fact_on) AS first_fact_on, max(fact_on) AS last_fact_on
            FROM (
                SELECT received_at AS fact_on FROM {schema}.inventory_lots
                 WHERE sim_run_id = %(sim)s
                UNION ALL
                SELECT moved_at FROM {schema}.inventory_moves WHERE sim_run_id = %(sim)s
                UNION ALL
                SELECT arrived_at FROM {schema}.inbound_receipts WHERE sim_run_id = %(sim)s
            ) facts
            """
        ).format(schema=schema),
        {"sim": sim_run_id},
    )
    row = rows[0] if rows else {}
    return FactCoverage(
        first_fact_on=row.get("first_fact_on"), last_fact_on=row.get("last_fact_on")
    )
