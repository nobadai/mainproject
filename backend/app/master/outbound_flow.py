"""
outbound_flow.py — **하루의 출고를 조립한다. 순서만 정하고 Lot 을 고르지 않는다.**

🔴 **물류가 순서를 마스터에 넘겼다** (물류 회신 2026-09-08).

  > Allocation 과 Shipment 함수 자체는 분리해서 유지하고, **실제 호출 순서는
  > Master 가 소유**하는 것으로 보겠습니다.
  > Master 는 어느 Reservation 을 실행할지만 정하고, Lot 선택 loop 는 Logistics
  > 내부에서 소유하겠습니다.

  ★ 엔진(`reserve_available_stock` · `allocate_reserved_stock_fefo` ·
    `ship_allocated_stock`) · 어휘(`app/contracts/sales_logistics.py`) · 시각
    (`app/master/sim_time.py`) 이 다 서 있는데 **부르는 곳이 0곳이었다.**
    이 파일이 그 자리다.

---

## 다섯 걸음

```text
① 그날 sale_date 인 판매의 sale_item 을 고른다
② reservation_id_for_sale_item(sale_item_id) 으로 예약 이름을 **계산**한다
③ reserve_confirmed_sale_available 로 **확보되는 만큼** 잡는다
④ allocate_reserved_stock_fefo(decided_at=phase_instant(as_of, "ALLOCATE"))
⑤ ship_allocated_stock(shipped_at=as_of, sale_item_id=…)
   그리고 그 판매의 **모든 품목**이 나갔으면 mark_sale_delivered
```

🔴 **`sales.sale_date` 가 납품일의 정본이다.** DDL 주석이 그렇게 정의한다.

```text
database/10_domain_schema.sql
  COMMENT ON COLUMN haetdeul.sales.sale_date IS '판매/납품 기준일.';
```

  ⚠️ **봉투에 납품일 칸을 두지 않기로 했다** (2026-09-08 · 물류·판매 합의 ·
    `app/contracts/sales_logistics.py` 가 그 결정을 적어 뒀다). 같은 날짜를
    복사해 두면 두 값이 갈리는 날이 온다. 그래서 여기서도 날짜를 저장하지 않고
    **읽어서 비교만** 한다.

🔴 **예약 이름은 저장하지 않고 계산한다.** `reservation_id_for_sale_item` 이
   결정론이라 적어 둘 이유가 없다 — 적어 두면 그 값이 두 번째 정본이 된다.

🔴 **`reserve_confirmed_sale` 이 아니라 `reserve_confirmed_sale_available` 이다.**
   앞엣것은 전량 아니면 예외를 던지는 사람 경로이고, 여기는 시뮬레이션 경로다.
   **부분 예약은 정상이다** — 물류가 그렇게 설계했고, 확보된 만큼만 나간다.

🔴 **`decided_at` 은 `sim_time.phase_instant(as_of, "ALLOCATE")` 다. 벽시계를
   읽지 않는다.** `clock.seoul_now` 도 부르지 않는다 — 같은 `as_of` 를 다시
   돌리면 장부에 같은 값이 적혀야 한다
   (`tests/master/test_clock_is_the_only_wall_clock.py` 가 AST 로도 지킨다).

---

## 🔴 실패 규율

```text
한 판매가 터져도 **나머지 판매는 계속 돈다** — 터진 것은 `items` 에 FAILED 로 남는다
그날 나갈 것이 없으면 **NOTHING_DUE** — BLOCKED 도 FAILED 도 아니다
예약이 부분만 확보되면 **그만큼만 나간다**
```

⚠️ **`ship` 이 실패해도 `allocate` 를 되돌리지 않는다.**

```text
할당   "어느 Lot 에서 뺄지 정했다"
출고   "나갔다"
```

  두 개는 **다른 사실**이다. 그래서 이 파일은 할당 뒤에 **커밋을 하나 둔다** —
  그래야 뒤이은 출고가 터져도 롤백이 할당까지 걷어 가지 않는다. 되돌리는 함수
  (`cancel_allocation`) 를 여기서 부르지도 않는다.

🔴 **한 판매에 품목이 여럿이면 그 판매의 모든 품목이 나간 뒤에만
   `mark_sale_delivered` 를 부른다.** 일부만 나갔는데 `DELIVERED` 로 적으면
   *"다 갔다"* 가 거짓으로 서고, 판매가 오늘 고친 그 문제가 되살아난다.

---

## 어휘 — 새로 만들지 않았다

```text
RAN           출고 단계를 **탔다**       `ItemRunOutcome.status` · `procurement_status`
FAILED        해 보고 터졌다             같은 곳
NOTHING_DUE   확인했고 나갈 것이 없다     `InboundOut` · `CollectionOut` — **날 단위**
SHORT         확보가 0kg 이라 나간 것이 없다   ← 여기서 새로 둔다 (품목 단위)
```

🔴 **`SHORT` 를 `NOTHING_DUE` 로 접지 않는다** (물류 PR #484 수신요청 §5.1).

```text
NOTHING_DUE   그날 나갈 판매가 **없다**
SHORT         나갈 판매가 **있었는데** 확보가 0이었다
```

  **없는 것과 해 봤는데 0인 것은 다른 사실이다.** 접으면 재고가 모자란 날과 주문이
  없는 날이 장부에서 같아 보인다.

★ **`SHIPPED` 를 상태 어휘로 만들지 않았다.** 그것은 물류가 이미
  `inventory_allocations.status` 에 쓰는 말이라, 단계 결과에 같은 낱말을 쓰면
  *"SHIP 단계"* 와 *"SHIPPED 상태"* 가 로그에서 구별이 안 된다
  (`sim_time.py` 가 단계 이름을 동사형으로 둔 것과 같은 이유다).

⚠️ **아직 예약이 0행이라 실물로는 안 돈다.** 판매가 확정 판매를 물류 경계로
   넘기기 시작하면 그날부터 이 자리가 돈다 — 그때까지 이 함수는 매일
   `NOTHING_DUE` 를 낸다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.contracts.sales_logistics import (
    SalesOutboundReservationRequest,
    reservation_id_for_sale_item,
)
from app.finance.db import get_connection, get_db_schema
from app.logistics.fefo_allocation import allocate_reserved_stock_fefo
from app.logistics.outbound import ship_allocated_stock
from app.logistics.sales_outbound import reserve_confirmed_sale_available
from app.master.sim_time import SimPhase, phase_instant
from app.sales.persistence import mark_sale_delivered

__all__ = [
    "ALLOCATE_PHASE",
    "DueSaleItem",
    "OutboundOut",
    "SaleItemOutcome",
    "due_sale_items",
    "fully_shipped_sales",
    "ship_due_sales",
]

#: 🔴 **할당 시각을 파생하는 단계 이름.** `sim_time.PHASES` 의 것을 그대로 쓴다.
#:
#: ★ 문자열을 상수로 둔 이유는 검사가 이 값을 찾기 때문이다. 손으로 다시 적으면
#:   철자가 갈리고, `phase_instant` 가 `ValueError` 를 내는 날까지 아무도 모른다.
ALLOCATE_PHASE: SimPhase = "ALLOCATE"


# ── ① 그날 나갈 것 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class DueSaleItem:
    """그날 나가야 하는 판매 품목 한 줄. **판매가 소유한 사실을 읽어 온 것뿐이다.**

    ⚠️ `sale_date` 를 들고 다니는 것은 **저장하려는 것이 아니라 비교하려는 것**이다.
      정본은 `sales.sale_date` 이고, 이 값은 그 행에서 방금 읽은 사본이다.
    """

    sale_id: str
    sale_item_id: str
    item_id: str
    sim_run_id: str
    quantity_kg: Decimal
    sale_date: date


def due_sale_items(conn: Any, *, as_of: date) -> tuple[DueSaleItem, ...]:
    """`as_of` 가 납품 기준일인 확정 판매의 품목들.

    🔴 **`order_status` 가 `CONFIRMED` · `READY` 인 것만 본다.** `DELIVERED` 는 이미
       나갔고 `CANCELLED` 는 나가면 안 된다 — `mark_sale_delivered` 가 받아 주는
       상태와 같은 표다.

    ⚠️ **`WHERE s.sale_date = %s` 는 덜 읽으려는 것이다.** 나가고 안 나가고를 실제로
       가르는 자리는 `_due_today` 한 줄이고, 그래서 그 판정이 DB 없이도 검사된다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT s.sale_id,
                       s.sim_run_id,
                       s.sale_date,
                       si.sale_item_id,
                       si.item_id,
                       si.quantity_kg
                  FROM {}.sales AS s
                  JOIN {}.sale_items AS si ON si.sale_id = s.sale_id
                 WHERE s.sale_date = %s
                   AND s.order_status IN ('CONFIRMED', 'READY')
                 ORDER BY s.sale_id, si.sale_item_id
                """
            ).format(schema, schema),
            [as_of],
        )
        rows = cursor.fetchall()
    return tuple(
        DueSaleItem(
            sale_id=row["sale_id"],
            sale_item_id=row["sale_item_id"],
            item_id=row["item_id"],
            sim_run_id=row["sim_run_id"],
            quantity_kg=Decimal(row["quantity_kg"]),
            sale_date=row["sale_date"],
        )
        for row in rows
    )


def _due_today(rows: Sequence[DueSaleItem], as_of: date) -> tuple[DueSaleItem, ...]:
    """오늘 나갈 것만 남긴다. 🔴 **`sales.sale_date` 가 정본이다.**

    ⚠️ 다른 날 것이 섞이면 **아직 안 팔 물건이 오늘 창고를 나간다.** 그러면 그날
      장부는 맞는데 그 앞뒤 날의 재고가 전부 틀린다 — 에러는 안 난다.
    """
    return tuple(row for row in rows if row.sale_date == as_of)


# ── ② 결과 어휘 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SaleItemOutcome:
    """판매 품목 하나의 출고 결과. **터진 것도 값으로 남는다.**

    ★ `RAN` 은 예약 → 할당 → 출고가 끝까지 돌았다는 뜻이다. **전량이 나갔다는 뜻이
      아니다** — 부분 예약이면 확보된 만큼만 나가고 그것이 정상이다. 실제로 얼마가
      나갔는지는 `shipped_qty_kg` 가, 얼마가 필요했는지는 `required_qty_kg` 가 나른다.

    🔴 **`SHORT` 는 `FAILED` 가 아니다** (물류 PR #484 수신요청 §5.1).
       `required=100, reserved=0` 은 **정상 사업 결과인 shortage** 다 — 해 봤는데
       확보가 0kg 이라 나간 것이 없다는 사실이지, 터진 것이 아니다.

    🔴 **`NOTHING_DUE` 를 재사용하지 않는다.** 그것은 날 단위 낱말이고
       *"그날 나갈 판매가 없다"* 는 뜻이다. `SHORT` 는 *"나갈 판매가 있었는데 확보가
       0이었다"* 다 — **없는 것과 해 봤는데 0인 것은 다른 사실이다.**
    """

    sale_id: str
    sale_item_id: str
    reservation_id: str
    status: Literal["RAN", "FAILED", "SHORT"]
    reason: str = ""
    shipped_qty_kg: Decimal = Decimal(0)
    #: 판매가 요구한 양 (`sale_items.quantity_kg`). **저장이 아니라 비교용 사본이다** —
    #: 완납 판정(`fully_shipped_sales`)이 이 값과 `shipped_qty_kg` 를 맞대 본다.
    required_qty_kg: Decimal = Decimal(0)


@dataclass(frozen=True)
class OutboundOut:
    """출고 단계 1회의 결과.

    ```text
    RAN           나갈 것이 있어서 한 건이라도 시도했다 — 품목별 결과는 `items`
    NOTHING_DUE   **확인했고 나갈 것이 없다** — 정상이다
    FAILED        시도조차 못 했다 (연결 · 조회 실패) — 아무것도 안 바뀌었다
    ```

    🔴 **`NOTHING_DUE` 를 `BLOCKED` 로도 `FAILED` 로도 접지 않는다.** 접으면 예약이
       아직 0행인 지금, **매일이 실패로 보인다.**
    """

    as_of: date
    status: Literal["RAN", "NOTHING_DUE", "FAILED"]
    reason: str = ""
    items: tuple[SaleItemOutcome, ...] = ()
    #: `mark_sale_delivered` 가 실제로 `DELIVERED` 로 옮긴 판매.
    delivered_sales: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed_items(self) -> tuple[str, ...]:
        """터진 판매 품목. **나머지는 계속 돌았다.**

        🔴 **`SHORT` 는 여기 안 들어온다.** 확보 0kg 은 터진 것이 아니라 사업 결과다.
        """
        return tuple(one.sale_item_id for one in self.items if one.status == "FAILED")

    @property
    def short_items(self) -> tuple[str, ...]:
        """확보가 0kg 이라 나간 것이 없는 판매 품목.

        ★ **결과에 보인다.** 안 보이면 *"그날은 아무 일도 없었다"* 와 구별되지 않고,
          다음 날 왜 같은 판매가 또 잡히는지 읽는 사람이 모른다.
        """
        return tuple(one.sale_item_id for one in self.items if one.status == "SHORT")


def fully_shipped_sales(results: Sequence[SaleItemOutcome]) -> tuple[str, ...]:
    """**모든 품목이 요구량만큼 나간** 판매만. 🔴 일부만 나갔으면 여기 안 들어온다.

    ⚠️ 한 판매에 품목이 셋인데 둘만 나간 날 `DELIVERED` 로 적으면, 그 판매는 영원히
      나머지 하나를 못 받는다 — 다음 날 `order_status` 필터가 그 판매를 아예 안
      집기 때문이다.

    🔴 **`status == "RAN"` 만으로는 완납이 아니다** (물류 PR #484 수신요청 §5.2).

      ```text
      RAN     출고 단계를 **탔다**
      완납    shipped_qty_kg >= required_qty_kg
      ```

      100kg 주문에 60kg 이 나가도 단계는 끝까지 돈다. 그것을 `DELIVERED` 로 닫으면
      나머지 40kg 이 영원히 안 나간다. **상태는 `RAN` 그대로 둔다** — 단계를 탄 것은
      사실이고, 부족한 것은 완납이 아니라는 사실뿐이다.

    ★ 부분 출고된 판매는 `DELIVERED` 가 안 되고 다음 날
      `order_status IN ('CONFIRMED','READY')` 필터에 **다시 잡혀** 나머지를 시도한다.
      의도한 동작이다 (`reserve_available_stock` 의 top-up 이 그것을 받는다).

    ★ 순서를 지킨다. 먼저 나온 판매가 먼저다 — 같은 날을 두 번 돌려도 목록이 같다.
    """
    order: list[str] = []
    ok: dict[str, bool] = {}
    for one in results:
        if one.sale_id not in ok:
            order.append(one.sale_id)
            ok[one.sale_id] = True
        ok[one.sale_id] = ok[one.sale_id] and _is_complete(one)
    return tuple(sale_id for sale_id in order if ok[sale_id])


def _is_complete(one: SaleItemOutcome) -> bool:
    """이 품목이 **요구량만큼 나갔는가.**"""
    return one.status == "RAN" and one.shipped_qty_kg >= one.required_qty_kg


# ── ③ 조립 ──────────────────────────────────────────────────────────────


def ship_due_sales(
    as_of: date,
    *,
    connect: Any = None,
    due_fn: Callable[..., Sequence[DueSaleItem]] = due_sale_items,
    reserve_fn: Callable[..., Any] = reserve_confirmed_sale_available,
    allocate_fn: Callable[..., Any] = allocate_reserved_stock_fefo,
    ship_fn: Callable[..., Any] = ship_allocated_stock,
    deliver_fn: Callable[..., Any] = mark_sale_delivered,
) -> OutboundOut:
    """`as_of` 에 나갈 판매를 **순서대로 내보낸다. Lot 은 안 고른다.**

    ★ **`receive_arrivals` · `collect_receipts` 와 같은 모양이다** — `as_of` 하나를
      받고, 예외를 밖으로 안 내고, 상태를 값으로 돌려준다.

    🔴 **Lot 선택 loop 가 여기 없다.** 그것은 `allocate_reserved_stock_fefo` 안에서
       잠금과 함께 돈다 (그 파일이 *"마스터가 밖에서 for 루프를 돌면 ②를 지킬 자리가
       없다"* 고 적어 뒀다). 이 함수가 정하는 것은 **어느 예약을 실행할지**뿐이다.

    :param due_fn: 그날 나갈 것을 읽는 자리. **검사가 대역을 끼우는 곳**이다.
    """
    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception as exc:  # noqa: BLE001 - 출고 실패가 그날을 통째로 세우면 안 된다.
        return OutboundOut(as_of=as_of, status="FAILED", reason=f"연결 실패: {exc}")

    try:
        try:
            rows = due_fn(conn, as_of=as_of)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            return OutboundOut(as_of=as_of, status="FAILED", reason=f"나갈 것을 못 읽었다: {exc}")

        due = _due_today(rows, as_of)
        if not due:
            return OutboundOut(
                as_of=as_of,
                status="NOTHING_DUE",
                reason=f"{as_of.isoformat()} 이 납품 기준일인 확정 판매가 없다",
            )

        # 🔴 **시각을 여기서 한 번 파생한다.** 같은 하루의 할당은 같은 시각으로 적힌다.
        decided_at = phase_instant(as_of, ALLOCATE_PHASE)

        results: list[SaleItemOutcome] = []
        for row in due:
            # 🔴 **한 판매가 터져도 여기서 안 멈춘다.** `_ship_one` 이 예외를 값으로
            #    옮기고, 다음 판매가 그대로 이어 돈다.
            results.append(
                _ship_one(
                    conn,
                    row,
                    as_of=as_of,
                    decided_at=decided_at,
                    reserve_fn=reserve_fn,
                    allocate_fn=allocate_fn,
                    ship_fn=ship_fn,
                )
            )

        notes: list[str] = []
        delivered = _mark_delivered(conn, results, deliver_fn=deliver_fn, notes=notes)
        failed = tuple(one.sale_item_id for one in results if one.status == "FAILED")
        short = tuple(one.sale_item_id for one in results if one.status == "SHORT")
        # 🔴 **두 사실을 한 문장에 합치지 않는다.** 터진 것과 확보 0kg 은 다른 일이라
        #    수를 더하면 읽는 사람이 왜 그랬는지 되짚을 수 없다.
        parts = [f"{len(failed)}건이 터졌다"] if failed else []
        if short:
            parts.append(f"{len(short)}건이 확보 0kg 이라 못 나갔다")
        reason = f"{len(results)}건 중 " + " · ".join(parts) if parts else ""
        return OutboundOut(
            as_of=as_of,
            status="RAN",
            reason=reason,
            items=tuple(results),
            delivered_sales=delivered,
            notes=tuple(notes),
        )
    finally:
        conn.close()


def _ship_one(
    conn: Any,
    row: DueSaleItem,
    *,
    as_of: date,
    decided_at: datetime,
    reserve_fn: Callable[..., Any],
    allocate_fn: Callable[..., Any],
    ship_fn: Callable[..., Any],
) -> SaleItemOutcome:
    """판매 품목 하나를 예약 → 할당 → 출고까지 태운다.

    🔴 **할당 뒤에 커밋이 하나 선다.** `ship` 이 터졌을 때 롤백이 할당까지 걷어 가면
       *"어느 Lot 에서 뺄지 정했다"* 는 사실이 사라진다 — 출고가 실패한 것과 할당이
       없던 것은 다른 사실이다.

    ★ **되돌리는 함수를 안 부른다.** `cancel_allocation` 은 이 파일에 임포트조차
      없다 — 할당을 물리는 것은 물류의 판단이지 출고 실패의 자동 결과가 아니다.
    """
    reservation_id = reservation_id_for_sale_item(row.sale_item_id)
    try:
        reserved = reserve_fn(
            conn,
            SalesOutboundReservationRequest(
                reservation_id=reservation_id,
                sim_run_id=row.sim_run_id,
                sale_id=row.sale_id,
                sale_item_id=row.sale_item_id,
                item_id=row.item_id,
                # 🔴 **판매 요구량 그대로다.** 모자라면 물류가 확보한 만큼만 잡고,
                #    못 잡은 몫은 `ReservationResult` 에 보이게 남는다.
                quantity_kg=row.quantity_kg,
                as_of=as_of,
            ),
        )
        conn.commit()

        if _reserved_qty_of(reserved) == 0:
            # 🔴 **없는 예약을 할당하지 않는다** (물류 §5.1). 예전에는 그대로
            #    `allocate` 로 가서 `OutboundIntegrityError` 가 났고, 그것이 `FAILED`
            #    로 적혔다 — **정상 사업 결과가 장애로 기록됐다.**
            return SaleItemOutcome(
                sale_id=row.sale_id,
                sale_item_id=row.sale_item_id,
                reservation_id=reservation_id,
                status="SHORT",
                reason=f"확보 0kg — 요구 {row.quantity_kg}kg",
                required_qty_kg=row.quantity_kg,
            )

        allocate_fn(
            conn,
            reservation_id=reservation_id,
            as_of=as_of,
            # 🔴 **벽시계가 아니다.** `sim_time` 이 `as_of` 에서 파생한 값이다.
            decided_at=decided_at,
        )
        # 🔴 **여기가 그 커밋이다.** 아래 출고가 터져도 할당은 남는다.
        conn.commit()

        shipped = ship_fn(
            conn,
            reservation_id=reservation_id,
            shipped_at=as_of,
            sale_item_id=row.sale_item_id,
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 한 판매가 하루를 세우면 안 된다.
        conn.rollback()
        return SaleItemOutcome(
            sale_id=row.sale_id,
            sale_item_id=row.sale_item_id,
            reservation_id=reservation_id,
            status="FAILED",
            reason=f"{type(exc).__name__}: {exc}",
            required_qty_kg=row.quantity_kg,
        )

    return SaleItemOutcome(
        sale_id=row.sale_id,
        sale_item_id=row.sale_item_id,
        reservation_id=reservation_id,
        status="RAN",
        shipped_qty_kg=Decimal(getattr(shipped, "shipped_qty_kg", 0) or 0),
        required_qty_kg=row.quantity_kg,
    )


def _reserved_qty_of(reserved: Any) -> Decimal | None:
    """`ReservationResult.reserved_qty_kg` — **물류가 실제로 확보한 양.**

    🔴 **칸이 없으면 `None` 이다. 0 이 아니다.** *"확보가 0이었다"* 와 *"얼마나
       확보됐는지 못 읽었다"* 는 다른 사실이라, 못 읽은 것을 0으로 접으면 물류가 칸
       이름을 바꾼 날 **모든 출고가 조용히 shortage 가 된다.** 못 읽었으면 예전대로
       할당까지 가고, 예약이 없으면 물류가 터뜨려 `FAILED` 로 **보이게** 남는다.
    """
    raw = getattr(reserved, "reserved_qty_kg", None)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (TypeError, ValueError, ArithmeticError):
        return None


def _mark_delivered(
    conn: Any,
    results: Sequence[SaleItemOutcome],
    *,
    deliver_fn: Callable[..., Any],
    notes: list[str],
) -> tuple[str, ...]:
    """모든 품목이 나간 판매만 `DELIVERED` 로 옮긴다.

    ⚠️ **판매 lifecycle 은 판매 것이다.** 이 함수는 판매가 내준 훅
      (`mark_sale_delivered` — *"caller-owned completion hook"*) 을 부르기만 하고,
      `sales.order_status` 를 직접 쓰지 않는다.

    ★ **여기서 터져도 출고를 되돌리지 않는다.** 물건은 이미 나갔다 — 나간 사실과
      판매 상태가 못 따라온 사실은 다르고, 뒤엣것은 `notes` 에 남는다.
    """
    delivered: list[str] = []
    for sale_id in fully_shipped_sales(results):
        try:
            deliver_fn(conn, sale_id=sale_id)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            notes.append(f"{sale_id} 를 DELIVERED 로 못 옮겼다: {type(exc).__name__}: {exc}")
            continue
        delivered.append(sale_id)
    return tuple(delivered)
