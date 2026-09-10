"""outbound_schedules.py — **미래 확정 출고를 판매 정본에서 읽는다** (WP-3).

```text
~WP-2   미래 확정 출고   logistics_runtime_fixture.confirmed_outbound_json
WP-3~   미래 확정 출고   sales · sale_items                        ← 이 파일
```

🔴 **왜 fixture JSON 을 안 읽나 — 그것은 업무 정본이 아니다.**

   그 칸은 사람이 심는 Runtime Snapshot 의 한 칸이고, 판매 확정이 그 칸을 채우는
   경로가 **하나도 없다.** 실측(2026-09-09)에서 254행 전부 `[]` · `CONFIRMED_ZERO`
   였다. 정상 출고 흐름은 이미 `sales → sale_items → inventory_reservations →
   inventory_allocations → inventory_moves OUT` 으로 완성돼 있고, 미래 확정 출고의
   사실은 그 사슬의 맨 앞(`sales`)에 있다.

🔴 **판매 정책을 물류가 새로 정하지 않는다.**

   ```text
   납품일     sales.sale_date                       DDL 주석 '판매/납품 기준일.'
   나갈 것    order_status IN ('CONFIRMED','READY')  DELIVERED 는 이미 나갔고
                                                     CANCELLED 는 나가면 안 된다
   ```

   두 규칙 다 **마스터 `outbound_flow.due_sale_items` 가 이미 쓰고 있는 어휘**를
   그대로 가져온 것이다. 물류가 자기 판단으로 상태 집합을 넓히거나 좁히지 않는다 —
   그렇게 하면 같은 판매가 두 파트에서 다르게 읽힌다.

🔴 **`sale_date > as_of` — 오늘은 «미래 출고» 가 아니다.**

   그날 나갈 몫은 마스터 출고 흐름이 그날 처리하고, 그 결과가 예약·할당 축으로
   내려온다. 여기서 다시 세면 **같은 판매가 두 축에 잡힌다** (이중 차감이 정확히
   그 모양이었다 — `tools.build_inventory_by_item` 참조).

⚠️ **판매가 읽기 함수를 제공하지 않아 직접 조회한다.** `app/sales/**` 는 쓰기
   (`persistence.py`)와 봉투(`contracts/sales_logistics.py`)만 내고 «확정 미래
   판매» 를 읽어 주는 계약이 없다. 판매 소유 영역을 고칠 수 없어(파트 경계) 물류가
   최소 조회를 갖는다 — **어휘는 위 두 줄 그대로 빌려 쓴다.**

🔴 **`sim_run_id` 축을 반드시 건다.** 이 값이 없으면 다른 실행의 판매가 이 실행의
   Capacity 를 깎는다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.schemas import ScheduledQuantity

__all__ = [
    "CONFIRMED_SALE_STATUS",
    "confirmed_outbound_at",
]

#: 아직 나가지 않은 확정 판매의 상태. 🔴 **마스터 `outbound_flow.due_sale_items` 와
#: 같은 표다.** 두 곳이 다른 집합을 쓰면 같은 판매가 Capacity 에는 잡히고 출고에는
#: 안 잡히는(혹은 그 반대) 상태가 된다.
CONFIRMED_SALE_STATUS: tuple[str, ...] = ("CONFIRMED", "READY")


def confirmed_outbound_at(
    conn: Any, *, sim_run_id: str, as_of: date
) -> list[ScheduledQuantity]:
    """`as_of` 에서 보이는 **미래 확정 출고**를 품목·날짜로 모은다.

    ```text
    축      sim_run_id = 이 실행     · sale_date > as_of
    상태    order_status IN (CONFIRMED, READY)
    값      품목별 · 날짜별 quantity_kg 합
    ```

    ★ **품목 이름으로 낸다.** `ScheduledQuantity.item` 은 `on_hand_by_lot[].item` ·
      `InventoryByItem.item` 과 같은 축이어야 하고 그 둘이 `items.item_name` 이다
      (`tools._replay_occupancy_by_item` 이 그 이름으로 버킷을 짚는다).

    🔴 **`item` 이 `None` 인 행을 만들지 않는다.** `sale_items.item_id` 가 `items`
       를 FK 로 가리키므로 이름 없는 출고가 나올 수 없다 — Partial Output 경로
       (`tools.has_unattributed_confirmed_outbound`)는 이 원천에서 안 켜진다.

    ★ 빈 목록은 *"0건 확인"* 이다. 못 읽은 것(`None`)과 다르다 — 예외를 삼키지 않고
      그대로 올린다. 조립부(`repository._build_logistics_runtime_fixture`)가
      `UNRESOLVED` 만 `None` 으로 낸다.

    :param conn: 호출자가 쥔 커넥션. **커밋도 롤백도 하지 않는다.**
    """
    if not isinstance(sim_run_id, str) or not sim_run_id.strip():
        raise ValueError(
            f"미래 확정 출고 조회에 쓸 수 없는 sim_run_id 다: {sim_run_id!r}."
            " 어느 실행의 판매인지 없이 물으면 남의 실행 판매가 이 실행 Capacity 를 깎는다."
        )

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT s.sale_date        AS schedule_date,
                       i.item_name        AS item_name,
                       SUM(si.quantity_kg) AS quantity_kg
                FROM {}.sales s
                JOIN {}.sale_items si ON si.sale_id = s.sale_id
                JOIN {}.items i ON i.item_id = si.item_id
                WHERE s.sim_run_id = %s
                  AND s.sale_date > %s
                  AND s.order_status = ANY(%s)
                GROUP BY s.sale_date, i.item_name
                ORDER BY s.sale_date, i.item_name
                """
            ).format(schema, schema, schema),
            (sim_run_id, as_of, list(CONFIRMED_SALE_STATUS)),
        )
        rows = cursor.fetchall()

    return [
        ScheduledQuantity(
            date=_cell(row, 0, "schedule_date"),
            item=_cell(row, 1, "item_name"),
            quantity_kg=Decimal(str(_cell(row, 2, "quantity_kg"))),
        )
        for row in rows
    ]


def _cell(row: Any, index: int, name: str) -> Any:
    """`dict_row` 든 튜플이든 같은 값을 낸다 (`inbound_stock._cell` 과 같은 규율)."""
    if isinstance(row, dict):
        return row[name]
    return row[index]
