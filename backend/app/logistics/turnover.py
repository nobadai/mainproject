"""turnover.py — 회전관리 · 판매우선 Signal · 폐기대기 판정 (3-D1).

```text
received_at → 경과일 → remaining_turnover_days → turnover_status → sell_priority
remaining_freshness_days <= 0                                   → disposal_candidate
```

🔴 **두 축을 섞지 않는다. 이것이 이 파일의 존재 이유다.**

```text
회전관리 (신규)                     물리·운영 신선도 (Legacy)
item_turnover_policies              item_storage_policies
operational_turnover_target_days    operational_limit_days
remaining_turnover_days             remaining_freshness_days
turnover_status                     freshness_pressure_ratio
→ 판매를 언제 하고 싶은가            → 지금 팔 수 있는 재고인가
```

   ⚠️ 둘은 **동시에 다른 답을 낼 수 있어야 한다.**

   ```text
   remaining_turnover_days  = -2   → STORAGE_TARGET_EXCEEDED · sell_priority=true
   remaining_freshness_days =  8   → 판매 가능 · disposal_candidate=false
   ```

🔴 **`STORAGE_TARGET_EXCEEDED` 는 판매불가가 아니다.**

```text
STORAGE_TARGET_EXCEEDED  ≠ 판매불가  ≠ 상함  ≠ Shelf-Life 종료  ≠ 폐기
```

   회사 내부 회전목표를 넘겼다는 뜻뿐이다 (Persona 05 §4). 그래서 이 상태만으로
   가용재고에서 빼지도, `disposal_candidate` 로 잇지도 **않는다.**

★ **어휘는 Persona 05 §4 그대로다.** 로직정의 v0.1 의 `PRESSURE` ·
  `TARGET_EXCEEDED` 는 쓰지 않는다 — 같은 것을 두 이름으로 부르면 계약이 갈린다.

⚠️ **`QUALITY_REVIEW_REQUIRED` 를 만들지 않는다.** Persona 어휘에는 있지만 그것은
   사람이 품질을 보고 내리는 판단이고, 날짜로 자동 생성할 수 있는 값이 아니다.

🔴 **정책이 없는 품목을 조회에서 떨어뜨리지 않는다.** `item_turnover_policies` 는
   실측 **3품목뿐**이고(`ITEM-GEONGOCHU` · `ITEM-PIMANUL` 없음) DDL 주석도 그것을
   경고한다. `LEFT JOIN` 으로 읽고, 정책이 없으면 `turnover_status=None` 으로 둔다 —
   모르는 것을 `NORMAL` 로 적으면 *"확인했고 정상"* 이라는 하지 않은 확인이 남는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "LotTurnover",
    "TurnoverStatus",
    "derive_turnover_status",
    "elapsed_days",
    "freshness_days_of",
    "is_disposal_candidate",
    "load_lot_turnover",
    "remaining_turnover_days",
    "sell_priority_of",
]


#: 회전 상태. **Persona 05 §4 어휘 그대로다.**
#:
#: ```text
#: NORMAL                   판매우선 없음
#: SELL_PRIORITY            판매를 우선 검토해야 하는 물류 Signal
#: STORAGE_TARGET_EXCEEDED  회사 내부 회전목표 초과 (판매불가 아님)
#: ```
TurnoverStatus = Literal["NORMAL", "SELL_PRIORITY", "STORAGE_TARGET_EXCEEDED"]

#: `sell_priority` 가 참인 상태들. `NORMAL` 만 거짓이다.
_SELL_PRIORITY_STATUSES: frozenset[str] = frozenset({"SELL_PRIORITY", "STORAGE_TARGET_EXCEEDED"})

_LOT_TURNOVER_COLUMNS = (
    "lot_id",
    "item_id",
    "received_at",
    "remaining_qty_kg",
    "grade",
    "operational_limit_days",
    "medium_grade_factor",
    "turnover_target_days",
    "sell_priority_remaining_days",
)


@dataclass(frozen=True)
class LotTurnover:
    """Lot 하나의 회전·신선도 파생값. **아무것도 바꾸지 않는 계산 결과다.**"""

    lot_id: str
    item_id: str
    received_at: date
    remaining_qty_kg: Decimal
    #: `as_of − received_at`. **미래 입고일이면 음수다** — 0 으로 보정하지 않는다.
    elapsed_days: int
    #: 회전 정책이 없는 품목이면 `None`. 0 으로 채우지 않는다.
    remaining_turnover_days: int | None
    turnover_status: TurnoverStatus | None
    #: 🔴 `turnover_status` 가 `None` 이면 **거짓**이다 — 모르는 것을 신호로 올리지 않는다.
    sell_priority: bool
    #: Legacy 신선도 축. 정책이 없으면 `None`.
    remaining_freshness_days: int | None
    #: 🔴 **회전목표와 무관하다.** 근거는 Legacy 판매불가 기준 하나뿐이다.
    disposal_candidate: bool


def elapsed_days(*, received_at: date, as_of: date) -> int:
    """입고 후 경과 일수. **달력일 기준이다.**

    ```text
    received_at == as_of  → 0
    미래 입고일            → 음수 (그대로 둔다)
    ```

    🔴 **음수를 0 으로 조용히 보정하지 않는다.** 미래 입고일은 데이터가 이상하다는
       신호인데, 0 으로 뭉개면 그 Lot 이 *"오늘 들어온 정상 재고"* 로 보인다.
    """
    return (as_of - received_at).days


def remaining_turnover_days(*, target_days: int, received_at: date, as_of: date) -> int:
    """회전목표까지 남은 일수.

    ```text
    remaining = operational_turnover_target_days − (as_of − received_at)
    ```

    ⚠️ **`operational_limit_days` 를 쓰지 않는다.** 그 값은 Legacy 신선도 축이고,
       두 값은 지금 숫자가 같아도 뜻이 다르다 (Persona 05 §8.1).
    """
    return target_days - elapsed_days(received_at=received_at, as_of=as_of)


def derive_turnover_status(
    *, remaining_days: int, sell_priority_remaining_days: int
) -> TurnoverStatus:
    """회전 상태를 정한다. **경계가 계약이다.**

    ```text
    remaining <= 0                              STORAGE_TARGET_EXCEEDED
    0 < remaining <= sell_priority_remaining_days  SELL_PRIORITY
    그 외                                        NORMAL
    ```

    ★ `remaining == 0` 은 **목표를 채운 날**이라 `STORAGE_TARGET_EXCEEDED` 다 —
      그날부터 회전목표를 넘긴 것으로 본다.
    """
    if remaining_days <= 0:
        return "STORAGE_TARGET_EXCEEDED"
    if remaining_days <= sell_priority_remaining_days:
        return "SELL_PRIORITY"
    return "NORMAL"


def sell_priority_of(status: TurnoverStatus | None) -> bool:
    """이 상태가 판매우선 Signal 인가.

    🔴 **가격·할인·판매량을 정하지 않는다.** 물류는 *"우선 검토해야 한다"* 는 사실만
       내고, 그 다음 행동은 Sales 소유다 (Persona 05 §7.1).

    ★ `None`(정책 없음)은 **거짓**이다 — 모르는 것을 신호로 올리지 않는다.
    """
    return status in _SELL_PRIORITY_STATUSES


def is_disposal_candidate(*, remaining_freshness_days: int | None) -> bool:
    """폐기 검토가 필요한 재고인가.

    🔴 **회전목표와 아무 관계가 없다.** `STORAGE_TARGET_EXCEEDED` 를 여기 잇지 않는다.

    ★ **근거는 이미 있는 판매불가 기준 하나뿐이다** —
      `tools.build_inventory_by_item` 이 `remaining_freshness_days <= 0` Lot 을
      가용재고에서 빼고 있다. 그 **기존 사실을 재사용**할 뿐, 새 물리 Shelf-Life
      숫자를 만들지 않는다 (`item_turnover_policies.physical_storage_limit_days` 는
      실측 전부 NULL · `NOT_FIXED` 다).

    ⚠️ **뜻을 좁게 읽어야 한다.** *"과학적으로 부패했다"* 가 아니라
       *"현재 Legacy 계약상 판매 대상에서 빠져 폐기 검토가 필요하다"* 다.

    ★ `None` 은 **거짓**이다 — 확인되지 않은 것을 후보로 올리지 않는다 (`0 != null`).
    """
    return remaining_freshness_days is not None and remaining_freshness_days <= 0


def _cell(row: Any, index: int, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def freshness_days_of(행: Mapping[str, Any], *, as_of: date) -> int | None:
    """Legacy 신선도 잔여. **`repository._inventory_lot_from_row` 와 같은 식이다.**

    ★ **공개해 둔 이유가 있다.** 출고(`outbound._available_lots`)도 같은 값을 봐야
      판매 가용에서 빠진 Lot 을 예약·할당이 다시 잡지 않는다. 두 벌로 만들면
      한쪽만 고쳐지는 날이 온다.

    :param 행: `operational_limit_days` · `medium_grade_factor` · `grade` ·
        `received_at` 을 가진 매핑.

    🔴 **새 유통기한 공식을 만들지 않는다.** 등급 판단도 저쪽과 같이 정규화 결과
       기준이고, 정규화표가 비어 있어 `상품` 계열은 `None` 이 된다 — 즉 `중` 계수는
       지금 실제로 걸리지 않는다. 그 사실을 여기서 바꾸지 않는다.
    """
    limit = 행["operational_limit_days"]
    if limit is None:
        return None
    from app.logistics.repository import _normalize_grade

    factor = 행["medium_grade_factor"]
    if _normalize_grade(행["grade"]) == "중" and factor is not None:
        limit = int(Decimal(limit) * Decimal(factor))
    return int(limit) - elapsed_days(received_at=행["received_at"], as_of=as_of)


def _lot_turnover_from_row(행: Mapping[str, Any], *, as_of: date) -> LotTurnover:
    target = 행["turnover_target_days"]
    priority_days = 행["sell_priority_remaining_days"]
    지난날 = elapsed_days(received_at=행["received_at"], as_of=as_of)

    remaining: int | None = None
    status: TurnoverStatus | None = None
    if target is not None and priority_days is not None:
        remaining = int(target) - 지난날
        status = derive_turnover_status(
            remaining_days=remaining, sell_priority_remaining_days=int(priority_days)
        )

    freshness = freshness_days_of(행, as_of=as_of)
    return LotTurnover(
        lot_id=행["lot_id"],
        item_id=행["item_id"],
        received_at=행["received_at"],
        remaining_qty_kg=행["remaining_qty_kg"],
        elapsed_days=지난날,
        remaining_turnover_days=remaining,
        turnover_status=status,
        sell_priority=sell_priority_of(status),
        remaining_freshness_days=freshness,
        disposal_candidate=is_disposal_candidate(remaining_freshness_days=freshness),
    )


def load_lot_turnover(
    conn: Any, *, sim_run_id: str, as_of: date, lot_id: str | None = None
) -> tuple[LotTurnover, ...]:
    """창고에 남아 있는 Lot 들의 회전·신선도 파생값. **읽기만 한다.**

    🔴 **`item_turnover_policies` 를 `LEFT JOIN` 한다.** 그 표는 실측 3품목뿐이라
       `INNER JOIN` 하면 계약 밖 품목의 재고가 **조회에서 통째로 사라진다**
       (DDL 주석이 같은 경고를 적어 두었다).

    ★ **`repository` 의 Lot 조회와 같은 눈으로 고른다** — `remaining_qty_kg > 0` ·
      `received_at <= as_of`. 상태로 거르지 않는다: 검수·격리 재고도 공간을
      점유하고 회전 시계도 돈다.

    ⚠️ **아무것도 바꾸지 않는다.** 가용재고 판정도 여기서 하지 않는다 — 그것은
       `tools.build_inventory_by_item` 몫이고, 이 함수는 **파생 사실만** 낸다.

    :param lot_id: 주면 그 Lot 하나만 읽는다 (폐기 확정이 쓴다).
    """
    schema = sql.Identifier(get_db_schema())
    조건 = sql.SQL("AND l.lot_id = %(lot_id)s") if lot_id else sql.SQL("")
    query = sql.SQL(
        """
        SELECT l.lot_id, l.item_id, l.received_at, l.remaining_qty_kg, l.grade,
               sp.operational_limit_days, sp.medium_grade_factor,
               tp.operational_turnover_target_days AS turnover_target_days,
               tp.sell_priority_remaining_days
        FROM {schema}.inventory_lots l
        LEFT JOIN {schema}.item_storage_policies sp ON sp.item_id = l.item_id
        LEFT JOIN {schema}.item_turnover_policies tp ON tp.item_id = l.item_id
        WHERE l.sim_run_id = %(sim)s
          AND l.received_at <= %(as_of)s
          AND l.remaining_qty_kg > 0
          {조건}
        ORDER BY l.lot_id
        """
    ).format(schema=schema, 조건=조건)

    with conn.cursor() as cursor:
        cursor.execute(query, {"sim": sim_run_id, "as_of": as_of, "lot_id": lot_id})
        rows = cursor.fetchall()

    return tuple(
        _lot_turnover_from_row(
            {name: _cell(row, index, name) for index, name in enumerate(_LOT_TURNOVER_COLUMNS)},
            as_of=as_of,
        )
        for row in rows
    )
