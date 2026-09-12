"""Observe — 창고를 **읽기만** 한다. 판정도 계산식도 여기서 만들지 않는다.

```text
스냅샷 (repository.get_current_logistics_read)   잔량 · 상태 · 신선도 · 예약/할당 · 용량
회전   (turnover.load_lot_turnover)              품목 ID · 판매우선 경계
원장   (historical_repository.onhand_by_lot_at)  대조용 as_of 잔량
표     (agent.exceptions.live_exceptions)        지금 살아 있는 Exception
```

🔴 **재고 계산기를 다시 만들지 않는다.** 신선도는 `turnover.freshness_days_of`,
   미확정 물량은 `tools._sellable_lot_contributions`, 창고 사용률은
   `tools.calculate_window_capacity_usage` 가 낸 값 **그대로** 담는다 — 탐지기가
   4 Mode 회신과 다른 숫자를 보면 *"조회는 괜찮다는데 Exception 은 위험하다"* 가
   성립하고, 그때 사람이 믿을 값이 없다.

🔴 **스냅샷은 자기 커넥션으로 읽는다.** `repository` 계층이 그렇게 생겼고
   (정의서 §1.2-13 의 "한 호출 한 번"), 이 함수의 `conn` 은 **쓰기 트랜잭션**의
   것이다. 검사에서 갈아 끼울 수 있도록 `read_fn` 으로 열어 둔다 —
   `maintain_fn` · `approve_fn` 과 같은 규율이다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from app.logistics.agent.exceptions import live_exceptions
from app.logistics.agent.schemas import (
    ObservedCapacity,
    ObservedLot,
    ObservedPolicy,
    WarehouseObservation,
    derive_observed_as_of,
)
from app.logistics.historical_repository import AdjustMoveNotSupported, onhand_by_lot_at
from app.logistics.repository import LogisticsRead, get_current_logistics_read
from app.logistics.rules import (
    CAPACITY_TIGHT_POLICY_UNRESOLVED,
    FRESHNESS_PRESSURE_POLICY_UNRESOLVED,
)
from app.logistics.tools import (
    _commitment_axes,
    _sellable_lot_contributions,
    calculate_window_capacity_usage,
)
from app.logistics.turnover import load_lot_turnover

__all__ = [
    "CAPACITY_WINDOW_USAGE_UNRESOLVED",
    "LEDGER_ADJUST_UNSUPPORTED",
    "OBSERVATION_INCONSISTENT",
    "OUTBOUND_COMMITMENTS_UNRESOLVED",
    "SNAPSHOT_AS_OF_MISMATCH",
    "TURNOVER_LOT_UNRESOLVED",
    "ObservationNotReady",
    "observe",
]

#: 예약·할당 축을 못 읽었다. 🔴 **0 건 확인과 다르다** — 못 읽은 축을 0 으로 놓으면
#: 이미 팔린 재고가 *"아무도 안 잡았다"* 로 보이고, 신선도 압박이 **없는 문제를
#: 만들어 낸다.** 그래서 이 사실이 있으면 Lot 별 미확정 물량이 전부 `None` 이다.
OUTBOUND_COMMITMENTS_UNRESOLVED = "OUTBOUND_COMMITMENTS_UNRESOLVED"
#: 창 사용률을 못 셈했다 (리드타임 · 보장 용량 · 입고 예정 미확정).
CAPACITY_WINDOW_USAGE_UNRESOLVED = "CAPACITY_WINDOW_USAGE_UNRESOLVED"
#: 캐시 잔량(`inventory_lots.remaining_qty_kg`)과 원장 누계가 다르다 (상세설계 §5.1).
#: 🔴 **예외를 내지 않는다** — 탐지를 세우는 대신 사실만 적는다.
OBSERVATION_INCONSISTENT = "OBSERVATION_INCONSISTENT"
#: 방향을 모르는 `ADJUST` 이동이 있어 원장 대조를 건너뛰었다.
LEDGER_ADJUST_UNSUPPORTED = "LEDGER_ADJUST_UNSUPPORTED"
#: 스냅샷에는 있는데 회전 조회에 없는 Lot — 품목 ID·판매우선 경계를 못 붙였다.
TURNOVER_LOT_UNRESOLVED = "TURNOVER_LOT_UNRESOLVED"
#: 스냅샷 기준일이 요청 기준일과 다르다.
SNAPSHOT_AS_OF_MISMATCH = "SNAPSHOT_AS_OF_MISMATCH"


class ObservationNotReady(RuntimeError):
    """관측을 세울 수 없다. 🔴 **여기서 삼키지 않는다** — 트랜잭션 주인이 값으로 옮긴다.

    ★ 부재(그날 fixture 가 없다)는 `repository` 가 이미 `LookupError` 로 내고, 이
      예외는 **받은 것이 요청과 다를 때**다. 둘 다 `master/inspection.py` 에서
      `FAILED` 한 줄이 된다 — 하루는 계속 간다.
    """


def observe(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    read_fn: Callable[..., LogisticsRead] = get_current_logistics_read,
) -> WarehouseObservation:
    """그 실행 · 그날의 창고 상태 한 벌. **아무것도 바꾸지 않는다.**

    :param conn: 쓰기 트랜잭션의 커넥션. 여기서는 **읽기에만** 쓴다 (회전 · 원장 ·
        Exception 표). 🔴 커밋도 롤백도 안 한다.
    :param read_fn: 스냅샷 경계. 🔴 기본값이 실제 함수 자체다 — `None` 을 안 받는다.
    """
    read = read_fn(as_of=as_of, sim_run_id=sim_run_id)
    snapshot = read.snapshot
    if snapshot.as_of != as_of:
        raise ObservationNotReady(
            f"{SNAPSHOT_AS_OF_MISMATCH}: 요청 {as_of} · 스냅샷 {snapshot.as_of}"
            " — 기준일이 섞인 관측으로 문제를 열지 않는다"
        )

    uncertainties: list[str] = []

    # ── 예약·할당 축 — **`build_inventory_by_item` 과 같은 눈** ───────────
    #
    # 🔴 `_sellable_lot_contributions` 를 그대로 부른다. 품목 합계(판매 가능량)와
    #    Lot 별 «아직 아무도 안 잡은 몫» 이 같은 함수에서 나와야, 매입에 나가는
    #    가용재고와 이 Exception 이 말하는 위험재고가 **같은 재고**를 가리킨다.
    axes = _commitment_axes(snapshot)
    if axes is None:
        uncertainties.append(OUTBOUND_COMMITMENTS_UNRESOLVED)
        미확정: dict[str, Decimal] = {}
    else:
        allocated_by_lot, _ = axes
        미확정 = {
            lot.lot_id: 기여
            for lot, 기여 in _sellable_lot_contributions(snapshot, allocated_by_lot)
        }

    # ── 회전 축 — 품목 ID 와 판매우선 경계 ───────────────────────────────
    #
    # ★ 스냅샷은 품목 **이름**만 싣고(`InventoryLotSnapshot.item`), severity 가 쓰는
    #   `sell_priority_remaining_days` 도 없다. 같은 WHERE(`잔량 > 0` ·
    #   `received_at <= as_of`)를 쓰는 기존 조회를 그대로 빌린다.
    회전 = {
        one.lot_id: one for one in load_lot_turnover(conn, sim_run_id=sim_run_id, as_of=as_of)
    }

    lots: list[ObservedLot] = []
    for lot in snapshot.on_hand_by_lot:
        회전행 = 회전.get(lot.lot_id)
        if 회전행 is None:
            uncertainties.append(f"{TURNOVER_LOT_UNRESOLVED}:{lot.lot_id}")
        lots.append(
            ObservedLot(
                lot_id=lot.lot_id,
                item=lot.item,
                item_id=None if 회전행 is None else 회전행.item_id,
                status=lot.status,
                received_at=lot.received_at,
                remaining_qty_kg=lot.available_qty_kg,
                # 🔴 **판매 가용이 아닌 Lot 에는 이 값이 없다.** 비-ACTIVE·신선도
                #    만료 Lot 은 애초에 팔 수 없어 «아직 안 잡힌 몫» 이 성립하지
                #    않는다 — 0 으로 적으면 «다 잡혔다» 로 읽힌다.
                uncommitted_kg=미확정.get(lot.lot_id),
                remaining_freshness_days=lot.remaining_freshness_days,
                effective_freshness_limit_days=lot.effective_freshness_limit_days,
                sell_priority_remaining_days=(
                    None if 회전행 is None else 회전행.sell_priority_remaining_days
                ),
                storage_zone=lot.storage_zone,
                observed_as_of=lot.received_at,
            )
        )

    # ── 용량 축 ─────────────────────────────────────────────────────────
    usage = calculate_window_capacity_usage(snapshot, as_of)
    if usage is None:
        uncertainties.append(CAPACITY_WINDOW_USAGE_UNRESOLVED)
    if snapshot.capacity_tight_ratio is None:
        uncertainties.append(CAPACITY_TIGHT_POLICY_UNRESOLVED)
    if snapshot.freshness_pressure_ratio is None:
        uncertainties.append(FRESHNESS_PRESSURE_POLICY_UNRESOLVED)
    capacity = ObservedCapacity(
        used_kg=snapshot.used_capacity_kg,
        guaranteed_kg=snapshot.guaranteed_capacity_kg,
        burst_kg=snapshot.burst_capacity_kg,
        window_usage_ratio=usage,
    )
    policy = ObservedPolicy(
        freshness_pressure_ratio=snapshot.freshness_pressure_ratio,
        capacity_tight_ratio=snapshot.capacity_tight_ratio,
    )

    uncertainties.extend(_ledger_disagreements(conn, sim_run_id=sim_run_id, as_of=as_of, lots=lots))

    return WarehouseObservation(
        sim_run_id=sim_run_id,
        as_of=as_of,
        # 🔴 **Lot 축만 센다.** 용량 축은 정책에 유효일이 없어 관측일이 `None` 이고,
        #    그 사실은 용량 Exception 의 근거에서 다시 드러난다 (§18).
        observed_as_of=derive_observed_as_of([one.observed_as_of for one in lots]),
        lots=tuple(lots),
        capacity=capacity,
        policy=policy,
        open_exceptions=live_exceptions(conn, sim_run_id=sim_run_id),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
    )


def _ledger_disagreements(
    conn: Any, *, sim_run_id: str, as_of: date, lots: list[ObservedLot]
) -> list[str]:
    """캐시 잔량과 원장 누계를 맞대어 본다. 🔴 **탐지를 막지 않는다.**

    ★ **`remaining_qty_kg` 는 파생 캐시다** (`repository` 가 적어 둔 경고). 정본은
      `inventory_moves` 이고, 둘이 갈리면 그날의 모든 판정이 조용히 틀린다 — 그래서
      문제를 열기 전에 한 번 맞대어 보고, 다르면 **사실만 적는다.**

    ⚠️ 원장에 이동이 없는 Lot 은 키에 없다 (0 이 아니라 «움직인 적 없음»). 그런 Lot 은
       대조 대상이 아니다 — 입고 IN 이 아직 안 적힌 상태를 불일치로 세면, 입고 직후
       점검 칸이 매일 같은 경고를 낸다.
    """
    try:
        원장 = onhand_by_lot_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    except AdjustMoveNotSupported:
        return [LEDGER_ADJUST_UNSUPPORTED]
    갈림 = [
        f"{OBSERVATION_INCONSISTENT}:{one.lot_id}"
        for one in lots
        if one.lot_id in 원장 and 원장[one.lot_id] != one.remaining_qty_kg
    ]
    return 갈림
