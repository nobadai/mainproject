"""Observe — 창고를 **읽기만** 한다. 판정도 계산식도 여기서 만들지 않는다.

```text
스냅샷 (repository.get_current_logistics_read)     잔량 · 상태 · 신선도 · 예약/할당 · 용량
회전   (turnover.load_lot_turnover)                품목 ID · 판매우선 경계
원장   (historical_repository.ledger_state_by_lot)  잔량 대조 · **잔량의 관측일**
표     (agent.exceptions.live_exceptions)          지금 살아 있는 Exception
```

🔴 **원장을 읽는 이유가 둘이다.** 하나는 캐시 대조이고, 다른 하나는 **날짜**다.
   `inventory_lots.remaining_qty_kg` 는 파생 캐시라 자기 관측일이 없다 — *"지금
   500kg 이다"* 를 언제부터 알 수 있었나에 답하는 것은 그 Lot 의 마지막 이동일뿐이다
   (상세설계 §18). 입고일로 메우면 D8 까지 나간 재고가 D1 부터 알던 사실이 된다.

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
from app.logistics.historical_repository import (
    AdjustMoveNotSupported,
    LedgerLotState,
    ledger_state_by_lot,
)
from app.logistics.repository import LogisticsRead, get_current_logistics_read
from app.logistics.rules import (
    CAPACITY_TIGHT_POLICY_UNRESOLVED,
    FRESHNESS_PRESSURE_POLICY_UNRESOLVED,
)
from app.logistics.schemas import InventoryLotSnapshot
from app.logistics.tools import (
    _commitment_axes,
    _sellable_lot_contributions,
    calculate_window_capacity_usage,
)
from app.logistics.turnover import load_lot_turnover

__all__ = [
    "CAPACITY_WINDOW_USAGE_UNRESOLVED",
    "LEDGER_ADJUST_UNSUPPORTED",
    "LEDGER_MOVE_UNRESOLVED",
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
#: 방향을 모르는 `ADJUST` 이동이 있어 원장 대조를 건너뛰었다. 🔴 그날은 **모든 Lot 의
#: 잔량 관측일이 `None`** 이다 — 대조도 날짜도 같은 원장에서 나온다.
LEDGER_ADJUST_UNSUPPORTED = "LEDGER_ADJUST_UNSUPPORTED"
#: 스냅샷에는 잔량이 있는데 원장에 그 Lot 의 이동이 하나도 없다.
#: 🔴 **잔량의 관측일을 못 댄다** — production 경로로 선 Lot 이면 날 수 없는 일이다
#: (`inbound_stock._insert_lot` 이 0 으로 세우고 `ledger` 만 잔량을 올린다).
LEDGER_MOVE_UNRESOLVED = "LEDGER_MOVE_UNRESOLVED"
#: 스냅샷에는 있는데 회전 조회에 없는 Lot — 품목 ID·판매우선 경계를 못 붙였다.
TURNOVER_LOT_UNRESOLVED = "TURNOVER_LOT_UNRESOLVED"
#: 스냅샷 기준일이 요청 기준일과 다르다.
SNAPSHOT_AS_OF_MISMATCH = "SNAPSHOT_AS_OF_MISMATCH"


#: Lot 상태 어휘 중 **관측일을 댈 수 있는 둘.** 나머지(`DEPLETED` · `HOLD`)는
#: production writer 가 없어 되살릴 사건이 없다 (`historical_repository` 의 같은 주석).
_ACTIVE = "ACTIVE"
_DISPOSED = "DISPOSED"


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

    # ── 원장 축 — 잔량의 **관측일**이 여기서 온다 ────────────────────────
    #
    # 🔴 Lot 루프보다 **먼저** 읽는다. 잔량과 그 잔량의 날짜는 한 사실의 두 면이라
    #    따로 붙이면 서로 다른 순간을 읽게 된다.
    ledger_state, ledger_uncertainties = _ledger_state(conn, sim_run_id=sim_run_id, as_of=as_of)
    uncertainties.extend(ledger_uncertainties)

    # ── 예약·할당 축 — **`build_inventory_by_item` 과 같은 눈** ───────────
    #
    # 🔴 `_sellable_lot_contributions` 를 그대로 부른다. 품목 합계(판매 가능량)와
    #    Lot 별 «아직 아무도 안 잡은 몫» 이 같은 함수에서 나와야, 매입에 나가는
    #    가용재고와 이 Exception 이 말하는 위험재고가 **같은 재고**를 가리킨다.
    axes = _commitment_axes(snapshot)
    if axes is None:
        uncertainties.append(OUTBOUND_COMMITMENTS_UNRESOLVED)
        uncommitted_by_lot: dict[str, Decimal] = {}
    else:
        allocated_by_lot, _ = axes
        uncommitted_by_lot = {
            lot.lot_id: contribution
            for lot, contribution in _sellable_lot_contributions(snapshot, allocated_by_lot)
        }

    # ── 회전 축 — 품목 ID 와 판매우선 경계 ───────────────────────────────
    #
    # ★ 스냅샷은 품목 **이름**만 싣고(`InventoryLotSnapshot.item`), severity 가 쓰는
    #   `sell_priority_remaining_days` 도 없다. 같은 WHERE(`잔량 > 0` ·
    #   `received_at <= as_of`)를 쓰는 기존 조회를 그대로 빌린다.
    turnover_by_lot = {
        one.lot_id: one for one in load_lot_turnover(conn, sim_run_id=sim_run_id, as_of=as_of)
    }

    lots: list[ObservedLot] = []
    for lot in snapshot.on_hand_by_lot:
        turnover_row = turnover_by_lot.get(lot.lot_id)
        if turnover_row is None:
            uncertainties.append(f"{TURNOVER_LOT_UNRESOLVED}:{lot.lot_id}")
        last_moved_at, move_uncertainties = _quantity_observed_as_of(ledger_state, lot=lot)
        uncertainties.extend(move_uncertainties)
        lots.append(
            ObservedLot(
                lot_id=lot.lot_id,
                item=lot.item,
                item_id=None if turnover_row is None else turnover_row.item_id,
                status=lot.status,
                received_at=lot.received_at,
                remaining_qty_kg=lot.available_qty_kg,
                # 🔴 **판매 가용이 아닌 Lot 에는 이 값이 없다.** 비-ACTIVE·신선도
                #    만료 Lot 은 애초에 팔 수 없어 «아직 안 잡힌 몫» 이 성립하지
                #    않는다 — 0 으로 적으면 «다 잡혔다» 로 읽힌다.
                uncommitted_kg=uncommitted_by_lot.get(lot.lot_id),
                remaining_freshness_days=lot.remaining_freshness_days,
                effective_freshness_limit_days=lot.effective_freshness_limit_days,
                sell_priority_remaining_days=(
                    None if turnover_row is None else turnover_row.sell_priority_remaining_days
                ),
                storage_zone=lot.storage_zone,
                remaining_qty_observed_as_of=last_moved_at,
                status_observed_as_of=_status_observed_as_of(
                    lot.status, received_at=lot.received_at, last_moved_at=last_moved_at
                ),
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

    return WarehouseObservation(
        sim_run_id=sim_run_id,
        as_of=as_of,
        # 🔴 **잔량 축만 센다.** `used_capacity_kg` 가 이 Lot 들의 잔량 합이라
        #    (`repository`) 그 사실의 관측일이 곧 이 값이다. 정책 축·예약 축은 여기
        #    안 든다 — 그 축들의 `None` 은 각 근거에서 따로 드러난다 (§18).
        inventory_observed_as_of=derive_observed_as_of(
            [one.remaining_qty_observed_as_of for one in lots]
        ),
        lots=tuple(lots),
        capacity=capacity,
        policy=policy,
        open_exceptions=live_exceptions(conn, sim_run_id=sim_run_id),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
    )


def _ledger_state(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[dict[str, LedgerLotState] | None, list[str]]:
    """그날까지의 원장 한 벌. **못 읽으면 `None` 이고, 그때 잔량 관측일이 전부 없다.**

    🔴 **`ADJUST` 를 만나면 날짜도 함께 포기한다.** 방향을 모르는 이동이 섞이면 그 Lot 의
       잔량 자체가 못 세는 값이 되고, 못 세는 값의 «언제부터» 는 더 못 댄다.
    """
    try:
        return ledger_state_by_lot(conn, sim_run_id=sim_run_id, as_of=as_of), []
    except AdjustMoveNotSupported:
        return None, [LEDGER_ADJUST_UNSUPPORTED]


def _quantity_observed_as_of(
    ledger_state: dict[str, LedgerLotState] | None, *, lot: InventoryLotSnapshot
) -> tuple[date | None, list[str]]:
    """이 Lot 의 잔량을 **언제부터 알 수 있었나** = 마지막 원장 이동일.

    ```text
    원장을 못 읽었다           None            (사유는 이미 적혔다)
    그 Lot 의 이동이 없다      None + 사유      production 경로면 날 수 없는 일이다
    캐시 ≠ 원장 누계           None + 사유      갈린 값의 «언제부터» 는 못 댄다
    그 밖                      max(moved_at)
    ```

    ★ **`remaining_qty_kg` 는 파생 캐시다** (`repository` 가 적어 둔 경고). 정본은
      `inventory_moves` 이고, 둘이 갈리면 그날의 모든 판정이 조용히 틀린다 — 그래서
      문제를 열기 전에 한 번 맞대어 보고, 다르면 **사실만 적고 날짜는 비운다.**

    🔴 **탐지를 막지는 않는다.** 날짜가 없다고 문제를 안 여는 것이 아니다 — 문제는
       열되 *"이 근거의 관측일은 모른다"* 를 그대로 남긴다.
    """
    if ledger_state is None:
        return None, []
    ledger_row = ledger_state.get(lot.lot_id)
    if ledger_row is None:
        return None, [f"{LEDGER_MOVE_UNRESOLVED}:{lot.lot_id}"]
    if ledger_row.balance_kg != lot.available_qty_kg:
        return None, [f"{OBSERVATION_INCONSISTENT}:{lot.lot_id}"]
    return ledger_row.last_moved_at, []


def _status_observed_as_of(
    status: str, *, received_at: date | None, last_moved_at: date | None
) -> date | None:
    """이 Lot 의 **상태**를 언제부터 알 수 있었나. 어휘마다 근거가 다르다.

    ```text
    ACTIVE    received_at     Lot INSERT 가 적는 값이다 (inbound_stock._insert_lot).
                              🔴 되돌리는 writer 가 없다 — 원장은 상태를 안 건드리고
                                 (ledger._update_remaining), 잔량이 0 이 돼도 그대로다
    DISPOSED  마지막 이동일    잔량을 0 으로 만든 DISPOSE 의 날
                              (disposal._mark_disposed 가 같은 판에서 적는다)
    그 밖      None            DEPLETED · HOLD 는 production writer 가 하나도 없다
    ```

    🔴 **없는 상태를 날짜로 메우지 않는다.** 쓰는 코드가 없는 어휘는 되살릴 사건도
       없다 — `historical_repository.HistoricalLotState` 가 `HOLD` 를 빼는 것과 같은 판단이다.
    """
    if status == _ACTIVE:
        return received_at
    if status == _DISPOSED:
        return last_moved_at
    return None
