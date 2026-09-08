"""fefo_allocation.py — 예약해 둔 몫을 **FEFO 순서로 자동 할당한다** (시뮬레이션 경로).

```text
reservation_id + as_of
   → 잠금
   → reservation_allocation_state    확보했는데 아직 Lot 을 안 고른 몫
   → recommend_fefo_candidates       ★ 잠금 안에서 다시 읽는다
   → 순서대로 필요한 만큼만 집는다
   → allocate_stock(FEFO_AUTO_SELECTED · LOGISTICS_FEFO_RULE)
```

🔴 **사람 경로를 덮지 않는다.** `outbound.allocate_stock` 은 그대로 *"사람이 고른
   Lot 과 수량"* 이고, 이 파일은 **그 함수를 부르는 또 하나의 호출자**일 뿐이다.
   검증·멱등·쓰기·상태 전이는 한 줄도 여기로 옮겨 오지 않았다.

   ★ **`inbound_execution` ↔ `simulated_inspection` 과 같은 모양이다.** 코어는
     사실을 받아 적고, 사실을 *만드는* 자동 규칙은 옆 파일에 서서 배선이 고른다.

🔴 **왜 마스터가 아니라 여기인가 — 잠금 계약 때문이다.**

  ```text
  outbound.py 모듈 계약
    ① 출고 전역 잠금 (20260905, 3)   ← 가장 먼저
    ② 가용량 **재계산**               잠금 밖에서 본 값을 믿지 않는다
    ③ 예약 / 할당 쓰기
  ```

  `recommend_fefo_candidates` 는 **잠금을 안 잡는 읽기**다. 마스터가 그것을 부르고
  밖에서 for 루프를 돌면 ②를 지킬 자리가 없다 — 후보를 본 시점과 쓰는 시점 사이가
  열려 있고, 그동안 남의 할당이 같은 Lot 을 가져갈 수 있다. 그러면 `allocate_stock`
  이 잠금 안에서 다시 세어 **거절**한다. 죽지는 않지만 매번 되풀이된다.

  ⚠️ **여기서는 ①→②→③ 이 한 트랜잭션 안에서 이어진다.** 잠금을 먼저 잡고 그 안에서
     후보를 읽으므로, 읽은 값이 쓰는 순간까지 그대로다.

★ **advisory 잠금을 두 번 잡는 것이 맞다.** `pg_advisory_xact_lock` 은 같은
  트랜잭션에서 재진입이 되므로 이 함수가 잡고 `allocate_stock` 이 또 잡아도
  막히지 않는다. 잠금 helper 를 복제하지 않고 `lock_outbound_writes` 를 그대로 쓴다.

🔴 **FEFO 정렬을 새로 만들지 않는다.** 순서의 주인은 `recommend_fefo_candidates`
   하나이고 (신선도 UNKNOWN 후행 → `remaining_freshness_days` ASC → `received_at`
   ASC → `lot_id` ASC), 이 파일은 그 튜플을 **받은 순서대로 소비만** 한다.
   신선도 식도 임계값도 여기서 다시 세지 않는다.

⚠️ **실출고를 부르지 않는다.** 할당까지다. `remaining_qty_kg` 를 줄이는 것은
   `ship_allocated_stock` 의 원장 OUT 뿐이고, 언제 내보낼지는 마스터가 정한다 —
   두 단계를 여기서 묶으면 *"할당은 됐는데 출고가 실패"* 를 표현할 수 없다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.logistics.outbound import (
    AllocationBasis,
    AllocationRequest,
    AllocationResult,
    OutboundIntegrityError,
    allocate_stock,
    lock_outbound_writes,
    recommend_fefo_candidates,
    reservation_allocation_state,
)

__all__ = [
    "ALLOCATION_BASIS",
    "DECIDED_BY",
    "allocate_reserved_stock_fefo",
]

#: 🔴 **사람이 없다.** 이 선택을 한 것은 사람이 아니라 FEFO 규칙이다.
#:
#: ★ `FEFO_TOOL_CONFIRMED` 로 적으면 *"사람이 추천을 따랐다"* 가 거짓으로 서고,
#:   `HUMAN_OVERRIDE` 로 적으면 없던 사람이 생긴다 — 셋째 값이 필요한 이유다.
#:   `ck_inventory_allocations_basis` 어휘 그대로이며 **Master ↔ Logistics 합의값**이다.
ALLOCATION_BASIS: AllocationBasis = "FEFO_AUTO_SELECTED"

#: 🔴 **`inventory_allocations.decided_by` 는 NOT NULL 인데 여기엔 사람이 없다.**
#:   없는 사람 이름을 짓는 대신 **무엇이 이 결정을 했는가**를 적는다.
#:
#: ⚠️ `ALLOCATION_BASIS` 와 뜻이 겹쳐 보이지만 다른 칸이다 — 하나는 *"어떤 근거로
#:    골랐나"*, 하나는 *"누가 골랐나"* 이고, 사람 경로에서는 둘이 실제로 갈린다
#:    (근거 `HUMAN_OVERRIDE` · 결정자 `WH-PLANNER-01`).
DECIDED_BY = "LOGISTICS_FEFO_RULE"


def allocate_reserved_stock_fefo(
    conn: Any,
    *,
    reservation_id: str,
    as_of: date,
    decided_at: datetime,
) -> AllocationResult:
    """예약이 **확보해 둔 몫**을 FEFO 순서로 Lot 에 붙인다.

    ```text
    목표량 = reserved_qty_kg − 이미 붙은 몫(ALLOCATED · PICKED · SHIPPED)
    후보    recommend_fefo_candidates 가 준 순서 그대로
    take   = min(candidate.available_qty_kg, 남은 목표량)
    ```

    ```text
    목표량 80 · LOT-A 30 · LOT-B 100 · LOT-C 50   (FEFO 순)
    → LOT-A 30 · LOT-B 50 · LOT-C 는 안 집는다
    ```

    🔴 **`required_qty_kg` 가 아니라 `reserved_qty_kg` 가 목표다.** 요구량은
       *"Sales 가 얼마를 원했나"* 이고 확보량은 *"물류가 얼마를 잡았나"* 다.
       부분 예약에서 둘이 갈리는데, 요구량을 목표로 두면 **잡은 적 없는 몫까지
       Lot 에 붙는다.**

    ★ **재실행이 안전하다.** 목표량이 0 이면 아무것도 안 쓰고 `applied=False` 로
      돌아선다 — 이미 다 붙은 예약을 다시 불러도 할당이 두 배가 되지 않는다.

    🔴 **이 예약이 이미 붙여 둔 Lot 은 건너뛴다.** 할당의 정체성이
       `(reservation_id, lot_id)` 한 쌍이고 `allocate_stock` 은 **이미 선 할당의
       수량을 덮지 않는다** (`ReservationConflict`). 그래서 top-up 뒤 같은 Lot 을
       다시 집으면 늘리는 대신 부딪힌다.

       ⚠️ 건너뛴 만큼은 **다음 FEFO 후보가 받는다.** 확보량이 사라지지 않는다.

    🔴 **모자라면 조용히 줄이지 않는다.** 후보를 다 훑고도 목표량이 남으면
       `OutboundIntegrityError` 다.

       ```text
       부분 **예약**   확보 자체를 못 한 것        정상이다 (reserve_available_stock)
       부분 **할당**   확보해 둔 몫을 Lot 에서 못 찾은 것   🔴 무결성 문제다
       ```

       ★ 둘을 같은 상황으로 보지 않는다. 뒤엣것은 같은 잠금 아래에서 일어날 수 없어야
         하고, 일어났다면 예약 축과 Lot 축이 어긋났다는 뜻이다.

    :param as_of: 가용·신선도 기준일. `recommend_fefo_candidates` 와 `allocate_stock`
        에 **같은 값**이 간다 — 두 곳이 다른 날짜로 보면 후보에는 있는 Lot 을 할당이
        거절한다.
    :param decided_at: 이 결정의 시각. **시계를 읽지 않고 호출자가 준다** (tz 필요) —
        같은 시뮬레이션을 다시 돌리면 같은 값이 나와야 한다.
    :raises OutboundIntegrityError: 예약이 없거나, 확보한 몫을 Lot 에서 못 채울 때.
    """
    # ── ① 잠금 먼저 ────────────────────────────────────────────────────
    with conn.cursor() as cursor:
        lock_outbound_writes(cursor)

    # ── ② 잠금 안에서 예약과 후보를 다시 읽는다 ────────────────────────
    상태 = reservation_allocation_state(conn, reservation_id=reservation_id)
    목표량 = 상태.unassigned_qty_kg
    if 목표량 <= 0:
        # ★ 이미 다 붙었다 — 재실행의 정상 경로다.
        return AllocationResult(
            applied=False,
            allocation_ids=(),
            reservation_status=상태.status,
            allocated_qty_kg=상태.assigned_qty_kg,
        )

    후보 = recommend_fefo_candidates(
        conn, sim_run_id=상태.sim_run_id, item_id=상태.item_id, as_of=as_of
    )

    # ── ③ 순서대로 필요한 만큼만 집는다 ────────────────────────────────
    요청: list[AllocationRequest] = []
    남은목표 = 목표량
    for 후보하나 in 후보:
        if 남은목표 <= 0:
            break
        # 🔴 이미 이 예약이 붙여 둔 Lot 이다. 수량을 늘리는 길이 없어 건너뛴다.
        if 후보하나.lot_id in 상태.assigned_lot_ids:
            continue
        집을것 = min(후보하나.available_qty_kg, 남은목표)
        if 집을것 <= 0:
            continue
        요청.append(AllocationRequest(lot_id=후보하나.lot_id, quantity_kg=집을것))
        남은목표 -= 집을것

    if 남은목표 > 0:
        raise OutboundIntegrityError(
            f"확보해 둔 몫을 Lot 에서 다 못 찾았다 ({reservation_id!r}):"
            f" 붙일 것 {목표량} · 못 찾은 것 {남은목표}"
            f" (확보 {상태.reserved_qty_kg} · 이미 붙은 것 {상태.assigned_qty_kg}"
            f" · 후보 {len(후보)}건). 조용히 줄이지 않는다 —"
            " 예약이 잡아 둔 몫이 Lot 축에 없다는 뜻이다."
        )

    # ── ④ 쓰기는 기존 코어가 한다 ──────────────────────────────────────
    return allocate_stock(
        conn,
        reservation_id=reservation_id,
        requests=요청,
        decided_by=DECIDED_BY,
        decided_at=decided_at,
        allocation_basis=ALLOCATION_BASIS,
        as_of=as_of,
    )
