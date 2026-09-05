"""disposal.py — 사람이 확정한 폐기만 실행한다 (3-D1).

```text
disposal_candidate = true   폐기 검토가 필요하다 — 아직 아무것도 안 바뀐다
   → 사람이 confirm_disposal(...) 을 명시적으로 부른다
   → DISPOSE Move
   → remaining_qty_kg 감소 · 창고 점유 감소
```

🔴 **자동 폐기가 없다.** Scheduler 도, Agent 도, 회전 상태도 이 함수를 부르지 않는다.
   `turnover.py` 는 후보만 계산하고 이 파일을 임포트하지 않는다 — 그 단방향이
   *"저절로 재고가 사라지는 경로"* 를 없애는 유일한 장치다.

🔴 **폐기대기와 폐기는 다르다.**

```text
disposal_candidate = true   remaining 그대로 · Move 없음 · Capacity 점유 그대로
                            판매 가용에서만 빠진다 (Legacy 신선도 규칙이 이미 그렇다)
confirm_disposal 이후        remaining 감소 · DISPOSE Move · Capacity 감소
```

🔴 **회전목표 초과는 폐기 근거가 아니다.** `STORAGE_TARGET_EXCEEDED` 만으로는 통과
   시키지 않는다 — 그것은 *"언제 팔고 싶은가"* 이지 *"팔 수 있는가"* 가 아니다.
   근거는 `turnover.is_disposal_candidate` 하나뿐이고, 그것은 이미 있는 판매불가
   기준(`remaining_freshness_days <= 0`)을 재사용한다.

🔴 **예약·할당된 재고를 없애지 않는다.**

```text
lot_disposable = Lot remaining − 그 Lot 의 살아있는 할당 (ALLOCATED · PICKED)
```

   ★ **품목 축을 따로 재지 않는 이유가 있다.** 폐기대기 Lot 은
     `outbound._available_lots` 가 이미 빼서 예약도 할당도 **새로 잡을 수 없다** —
     즉 이 Lot 을 없애도 판매 가능 재고가 줄지 않는다. 품목 전체 여유량으로 재면
     *"다른 Lot 에 여유가 있나"* 라는 **상관없는 값**으로 폐기를 막게 된다.

   ★ 판매 가능하던 시절에 붙은 할당은 위 `lot_disposable` 이 그대로 보호한다.

   ⚠️ **이 단순화는 폐기대기 Lot 을 가용에서 빼는 규칙에 기대고 있다.** 그 규칙이
      바뀌면 품목 축 검사를 되살려야 한다.

🔴 **잠금 순서는 출고와 같다.**

```text
① 출고/재고확보 전역 (20260905, 3)   outbound.lock_outbound_writes  ← 재사용
② 원장 전역 (20260905, 1)            _record_disposal_move 안에서
③ Lot 행 FOR UPDATE                   〃
```

   ★ **새 잠금을 만들지 않았다.** 폐기는 예약·할당과 같은 자원(가용재고)을 다투므로
     그쪽과 **같은 줄**에 서야 한다. 순서도 출고와 같아 역전이 생길 자리가 없다.

⚠️ **수량 감소의 정본은 원장이다.** 이 파일에 `UPDATE inventory_lots SET
   remaining_qty_kg` 이 없다 — `_record_disposal_move` 가 그 일을 한다.

⚠️ **되돌리는 경로가 없다.** 폐기 취소·환입·`ADJUST_IN` 은 이 판의 범위 밖이고,
   실사도 제외돼 있다. 그래서 검증을 전부 **쓰기 전에** 건다.

★ **스키마 실측 (2026-09-05).**

```text
inventory_moves   reason_code 에 CHECK 이 없다 — 어휘를 DB 가 강제하지 않는다
                  기존 DISPOSE 2건의 reason_code = 'MVP_DEMO_FIXTURE_CORRECTION'
                  ↑ 씨앗 보정용이지 **업무 폐기 사유가 아니다**
                  🔴 그래서 업무 사유 어휘를 여기서 짓지 않고 **호출자가 준다**
                  ⚠️ 확정자를 적을 칸이 없다 (`confirmed_by` 컬럼 부재) — 보고 대상
inventory_lots    status CHECK = ACTIVE · DEPLETED · DISPOSED · HOLD
```
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.ledger import _record_disposal_move
from app.logistics.outbound import lock_outbound_writes
from app.logistics.turnover import load_lot_turnover

__all__ = [
    "DisposalBlocked",
    "DisposalError",
    "DisposalIntegrityError",
    "DisposalResult",
    "InvalidDisposalRequest",
    "confirm_disposal",
    "disposal_move_id_for",
]

#: 🔴 **아직 창고에서 안 나간** 할당 상태. `outbound._HOLDING_ALLOCATION` 과 같은 뜻이다.
#:
#: ★ `SHIPPED` 는 빼지 않는다 — 그 몫은 원장 OUT 이 이미 `remaining_qty_kg` 에서
#:   덜어냈다. 여기서 또 빼면 같은 수량을 두 번 차감한다.
_HOLDING_ALLOCATION: tuple[str, ...] = ("ALLOCATED", "PICKED")

#: 🔴 **아직 재고를 잡고 있는** 예약 상태.
_HOLDING_RESERVATION: tuple[str, ...] = ("RESERVED", "PARTIALLY_ALLOCATED", "ALLOCATED")


class DisposalError(RuntimeError):
    """이 모듈이 내는 실패의 조상."""


class InvalidDisposalRequest(DisposalError, ValueError):
    """요청이 계약이나 수량 한도를 어긴다. **DML 전에 막는다.**"""


class DisposalBlocked(DisposalError, ValueError):
    """폐기할 근거가 없다.

    🔴 **회전목표 초과는 근거가 아니다.** `disposal_candidate` 가 참이어야 하고,
       그 값의 유일한 출처는 Legacy 판매불가 기준이다.
    """


class DisposalIntegrityError(DisposalError, ValueError):
    """같은 폐기 참조에 **다른 사실**이 이미 있거나, 대상 Lot 이 없다."""


@dataclass(frozen=True)
class DisposalResult:
    """`confirm_disposal` 의 결과.

    🔴 `applied=False` 는 *"이 호출이 새 DISPOSE 를 남기지 않았다"* 다 — 같은 폐기가
       이미 적혀 있었다는 뜻이지 폐기가 없었다는 뜻이 아니다.
    """

    applied: bool
    move_id: str
    lot_id: str
    disposed_qty_kg: Decimal
    #: 이 호출이 끝난 시점의 Lot 잔량.
    remaining_qty_kg: Decimal
    #: 전량 폐기로 `DISPOSED` 까지 갔나.
    lot_status: str


def disposal_move_id_for(*, disposal_id: str) -> str:
    """폐기 Move 의 멱등 키. **순수 계산이고 결정론이다.**

    ```text
    MOVE-DISPOSE-{disposal_id}
    ```

    🔴 **`lot_id` 만으로 짓지 않는다.** 한 Lot 을 여러 번 나눠 폐기할 수 있어서,
       `lot_id` 기반 ID 는 두 번째 부분 폐기를 **첫 번째의 재실행으로 오인**한다.

    ★ **`disposal_id` 는 호출자가 준다.** 저장소에 폐기 참조 표가 없어(실측:
      `inventory_count_*` 도 폐기 승인 표도 행 0) 물류가 채번 규칙을 지어내지
      않는다 — 그 값을 정하는 자리는 폐기를 승인하는 쪽이다.

    🔴 난수 · 시계 · 시퀀스를 쓰지 않는다.
    """
    if not disposal_id or not disposal_id.strip():
        raise InvalidDisposalRequest(f"disposal_id 가 비었다: {disposal_id!r}")
    return f"MOVE-DISPOSE-{disposal_id}"


def _require_text(값: Any, *, 칸: str) -> str:
    if not isinstance(값, str) or not 값.strip():
        raise InvalidDisposalRequest(f"{칸} 가 비었다: {값!r}")
    return 값


def _quantity(값: Any) -> Decimal:
    """폐기 수량을 좁힌다. **float 도 비유한값도 받지 않는다.**

    ★ `ledger._quantity` · `outbound._quantity` 와 같은 규율이다 — `NaN` 은 부등식을
      조용히 통과해 한도 검사를 무력화한다.
    """
    if isinstance(값, bool) or not isinstance(값, Decimal):
        raise InvalidDisposalRequest(
            f"quantity_kg 는 Decimal 이어야 한다 (받은 것: {값!r} · {type(값).__name__})."
        )
    if not 값.is_finite():
        raise InvalidDisposalRequest(f"quantity_kg 가 유한한 수가 아니다: {값!r}")
    if 값 <= 0:
        raise InvalidDisposalRequest(f"quantity_kg 는 0보다 커야 한다 (받은 것: {값})")
    return 값


def _cell(row: Any, index: int, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _lot_disposable_qty(
    conn: Any, schema: sql.Identifier, *, sim_run_id: str, lot_id: str
) -> tuple[Decimal, str]:
    """이 Lot 에서 **없애도 되는 양**과 현재 상태.

    ```text
    lot_disposable = remaining_qty_kg − 그 Lot 의 살아있는 할당 합
    ```

    ★ `outbound._available_lots` 의 Lot 축과 **같은 뜻**이다. 저쪽은 품목 전체를
      한 번에 훑는 목록이고 여기는 Lot 하나라, 같은 의미를 좁게 다시 적었다.
    """
    query = sql.SQL(
        """
        SELECT l.remaining_qty_kg, l.status,
               COALESCE((
                   SELECT SUM(a.allocated_qty_kg)
                   FROM {schema}.inventory_allocations a
                   JOIN {schema}.inventory_reservations r
                     ON r.reservation_id = a.reservation_id
                   WHERE a.lot_id = l.lot_id
                     AND a.status = ANY(%(alloc)s)
                     AND r.status = ANY(%(rsv)s)
               ), 0) AS held_qty_kg
        FROM {schema}.inventory_lots l
        WHERE l.sim_run_id = %(sim)s AND l.lot_id = %(lot)s
        """
    ).format(schema=schema)
    with conn.cursor() as cursor:
        cursor.execute(
            query,
            {
                "sim": sim_run_id,
                "lot": lot_id,
                "alloc": list(_HOLDING_ALLOCATION),
                "rsv": list(_HOLDING_RESERVATION),
            },
        )
        rows = cursor.fetchall()
    if not rows:
        raise DisposalIntegrityError(
            f"폐기할 Lot 이 없다 (sim_run_id={sim_run_id!r}, lot_id={lot_id!r})."
        )
    if len(rows) > 1:
        raise DisposalIntegrityError(f"같은 lot_id 가 둘 이상이다: {lot_id!r}")
    remaining = Decimal(_cell(rows[0], 0, "remaining_qty_kg"))
    status = _cell(rows[0], 1, "status")
    held = Decimal(_cell(rows[0], 2, "held_qty_kg"))
    return remaining - held, status


def _existing_disposal(conn: Any, schema: sql.Identifier, *, move_id: str) -> dict[str, Any] | None:
    """같은 폐기 참조가 이미 적혀 있나. **읽기만 한다.**

    ★ **`note` 까지 읽는다.** 원장의 멱등 판정(`ledger._IDENTITY_COLUMNS`)이 `note` 를
      포함하므로 폐기 재실행도 같은 눈으로 봐야 한다 — 여기서 빼면 같은 참조에 다른
      설명이 붙어도 통과하고, 그 차이는 어디에도 안 남는다.
    """
    이름 = ("sim_run_id", "lot_id", "move_type", "quantity_kg", "moved_at", "reason_code", "note")
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT sim_run_id, lot_id, move_type, quantity_kg, moved_at,
                       reason_code, note
                FROM {}.inventory_moves
                WHERE move_id = %s
                """
            ).format(schema),
            (move_id,),
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    return {name: _cell(rows[0], index, name) for index, name in enumerate(이름)}


def _mark_disposed(conn: Any, schema: sql.Identifier, *, sim_run_id: str, lot_id: str) -> None:
    """전량 폐기된 Lot 을 `DISPOSED` 로 넘긴다.

    🔴 **부분 폐기에는 붙이지 않는다.** 30kg 만 버린 Lot 은 여전히 살아 있는 재고다.

    ★ **`OUT` 으로 0 이 된 Lot 과 뜻이 다르다.** 저쪽은 팔려 나간 것이라 `ACTIVE` 로
      남고(원장은 상태를 안 바꾼다), 이쪽은 버려진 것이라 `DISPOSED` 다. 두 0 을 같은
      상태로 적으면 *"왜 없어졌나"* 를 나중에 구별할 수 없다.

    ⚠️ 수량은 건드리지 않는다 — 그것은 원장이 이미 했다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.inventory_lots
                SET status = 'DISPOSED'
                WHERE sim_run_id = %s AND lot_id = %s AND remaining_qty_kg = 0
                """
            ).format(schema),
            (sim_run_id, lot_id),
        )


def confirm_disposal(
    conn: Any,
    *,
    disposal_id: str,
    sim_run_id: str,
    lot_id: str,
    quantity_kg: Decimal,
    disposed_at: date,
    reason_code: str,
    as_of: date,
    note: str | None = None,
) -> DisposalResult:
    """사람이 확정한 폐기를 실행한다. **이번 판에서 유일하게 재고를 없애는 경로다.**

    ```text
    ① 입력 검증                        DB 를 안 만진다
    ② 출고/재고확보 전역 잠금            예약·할당과 같은 줄에 선다
    ③ 같은 폐기가 이미 있나              있으면 사실 대조 후 applied=False
    ④ 폐기대기 근거 확인                 disposal_candidate 가 참이어야 한다
    ⑤ Lot 폐기 가능량 검사               살아있는 할당은 건드리지 않는다
    ⑥ _record_disposal_move → 잔량 감소
    ⑦ 잔량 0 이면 Lot status = DISPOSED
    ```

    🔴 **자동으로 불리면 안 된다.** Scheduler · Agent · 회전 상태가 이 함수를 부르지
       않는다. 되돌릴 경로가 없어서(`ADJUST_IN` 없음 · 실사 제외) **사람이 명시적으로
       부르는 것**이 유일한 안전장치다.

    ⚠️ **재실행은 과거 사실을 다시 판정하지 않는다.** 같은 `disposal_id` 가 오면
       ④⑤ 를 건너뛰고 기존 Move 와 사실만 대조한다 — 오늘 후보가 아니게 됐다고
       어제 적은 폐기가 틀린 것이 되지는 않는다.

    :param disposal_id: 폐기 건의 정체성. **호출자가 준다** — 부분 폐기를 여러 번 할
        수 있어 `lot_id` 로는 가를 수 없다.
    :param reason_code: 폐기 사유. **호출자가 준다** — DB CHECK 이 없고 기존
        `MVP_DEMO_FIXTURE_CORRECTION` 은 씨앗 보정용이라, 물류가 업무 어휘를 짓지 않는다.
    :param as_of: 폐기대기 판정 기준일. 신선도 잔여를 이 날짜로 센다.
    :raises DisposalBlocked: 폐기대기 근거가 없을 때.
    :raises InvalidDisposalRequest: 수량이 계약이나 한도를 어길 때.
    :raises DisposalIntegrityError: 같은 참조에 다른 사실이 있거나 Lot 이 없을 때.
    """
    # ── ① 검증 ────────────────────────────────────────────────────────
    move_id = disposal_move_id_for(disposal_id=disposal_id)
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(lot_id, 칸="lot_id")
    _require_text(reason_code, 칸="reason_code")
    quantity = _quantity(quantity_kg)

    schema = sql.Identifier(get_db_schema())

    # ── ② 예약·할당과 같은 줄에 선다 ──────────────────────────────────
    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    # ── ③ 이미 적힌 폐기인가 ──────────────────────────────────────────
    기존 = _existing_disposal(conn, schema, move_id=move_id)
    if 기존 is not None:
        다른것 = {
            칸: (기존[칸], 값)
            for 칸, 값 in (
                ("sim_run_id", sim_run_id),
                ("lot_id", lot_id),
                ("move_type", "DISPOSE"),
                ("quantity_kg", quantity),
                ("moved_at", disposed_at),
                ("reason_code", reason_code),
                # ★ 원장 멱등 판정과 같은 눈이다 — 같은 참조에 다른 설명이 붙으면
                #   그것도 다른 사실이다.
                ("note", note),
            )
            if 기존[칸] != 값
        }
        if 다른것:
            raise DisposalIntegrityError(
                f"같은 폐기 참조에 다른 사실이 있다 (disposal_id={disposal_id!r}):"
                f" {다른것!r}. 덮지 않는다 — 이미 없앤 재고를 다시 셈하지 않는다."
            )
        # ★ 재실행에서는 후보 판정을 **다시 하지 않는다** — 오늘 후보가 아니게 됐다고
        #   어제 적은 폐기가 틀린 것이 되지 않는다. 현재 잔량만 되읽어 돌려준다.
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT remaining_qty_kg, status FROM {}.inventory_lots"
                    " WHERE sim_run_id = %s AND lot_id = %s"
                ).format(schema),
                (sim_run_id, lot_id),
            )
            row = cursor.fetchall()[0]
        return DisposalResult(
            applied=False,
            move_id=move_id,
            lot_id=lot_id,
            disposed_qty_kg=quantity,
            remaining_qty_kg=Decimal(_cell(row, 0, "remaining_qty_kg")),
            lot_status=_cell(row, 1, "status"),
        )

    # ── ④ 폐기대기 근거 ───────────────────────────────────────────────
    후보 = load_lot_turnover(conn, sim_run_id=sim_run_id, as_of=as_of, lot_id=lot_id)
    if not 후보:
        raise DisposalIntegrityError(
            f"폐기할 Lot 이 없다 (sim_run_id={sim_run_id!r}, lot_id={lot_id!r}, as_of={as_of})."
        )
    lot = 후보[0]
    if not lot.disposal_candidate:
        raise DisposalBlocked(
            f"폐기대기 대상이 아니다 (lot_id={lot_id!r}, as_of={as_of}):"
            f" 잔여 신선도 {lot.remaining_freshness_days}"
            f" · 회전상태 {lot.turnover_status}."
            " 🔴 회전목표 초과(STORAGE_TARGET_EXCEEDED)만으로는 폐기하지 않는다 —"
            " 그것은 판매불가가 아니다."
        )

    # ── ⑤ Lot 폐기 가능량 ─────────────────────────────────────────────
    lot_disposable, _ = _lot_disposable_qty(conn, schema, sim_run_id=sim_run_id, lot_id=lot_id)
    if quantity > lot_disposable:
        raise InvalidDisposalRequest(
            f"이 Lot 에서 없앨 수 있는 양을 넘는다 (lot_id={lot_id!r}):"
            f" 요청 {quantity} · 가능 {lot_disposable}."
            " 이미 출고에 배정된 몫은 없애지 않는다."
        )
    # ★ 품목 전체 여유량은 여기서 재지 않는다 — 폐기대기 Lot 은 판매 가용 풀에
    #   없어서, 없애도 팔 수 있는 양이 줄지 않는다. 모듈 docstring 참고.

    # ── ⑥ 잔량을 줄이는 것은 원장뿐이다 ───────────────────────────────
    move = _record_disposal_move(
        conn,
        move_id=move_id,
        sim_run_id=sim_run_id,
        lot_id=lot_id,
        quantity_kg=quantity,
        moved_at=disposed_at,
        reason_code=reason_code,
        note=note,
    )

    # ── ⑦ 전량 폐기만 DISPOSED ────────────────────────────────────────
    상태 = "ACTIVE"
    if move.remaining_qty_kg == 0:
        _mark_disposed(conn, schema, sim_run_id=sim_run_id, lot_id=lot_id)
        상태 = "DISPOSED"
    return DisposalResult(
        applied=move.applied,
        move_id=move_id,
        lot_id=lot_id,
        disposed_qty_kg=quantity,
        remaining_qty_kg=move.remaining_qty_kg,
        lot_status=상태,
    )
