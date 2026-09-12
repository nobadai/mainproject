"""공용 Read-only Tool 8개 — **조사하는 쪽이 무엇을 묻든 같은 구현을 지난다.**

```text
STATUS / Interactive Query   "지금 상태가 무엇인가"          → 같은 8개
Exception Investigate        "왜 생겼고 무엇을 할 수 있나"    → 같은 8개
```

🔴 **새 계산기가 아니라 기존 함수의 wrapper 다.** 잔량은 원장
   (`historical_repository`), 신선도·회전은 `turnover`, 용량은 `tools`, 예약·할당은
   `reservation_state_at`, 입고 예정은 `inbound_schedules` 가 낸 값 **그대로** 담는다 —
   Tool 이 새 숫자를 내면 회신·Exception·화면이 같은 창고를 두고 서로 다른 말을 한다.

🔴 **전부 읽기 전용이다.** 쓰기 문장도, `commit` · `rollback` 도, Exception 상태 변경도
   여기 없다. LLM 이 DB 를 바꾸는 경로를 만들지 않는 것이 이 층의 존재 이유다 (§9).

🔴 **`as_of` 가 장식이 아니다.** `inventory_lots.remaining_qty_kg` 는 **지금** 잔량이라
   과거 조회에 쓰면 오늘 소진된 재고가 그날에도 없던 것으로 보인다
   (`repository.get_current_logistics_read` 의 경고 그대로). 그래서 물리 사실은 전부
   원장·사건에서 되살리고, 되살릴 수 없는 축(정책)은 **그 사실을 결과에 적는다.**

⚠️ **LLM 도 Planner 도 여기 없다.** 이 파일은 평범한 read-only 파이썬 함수 묶음이고,
   모델에 선언하는 일(Commit 4)은 감싸는 쪽이 한다 — Registry 도 Plugin 도 안 만든다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from app.logistics import tools as calc
from app.logistics.agent.exceptions import live_exceptions_at
from app.logistics.agent.observe import _status_observed_as_of
from app.logistics.agent.schemas import (
    COMMITMENT_OBSERVED_AS_OF,
    POLICY_OBSERVED_AS_OF,
    ExceptionRow,
    derive_observed_as_of,
)
from app.logistics.historical_repository import (
    CAPACITY_BASIS_CURRENT_ACTIVE_POLICY,
    AdjustMoveNotSupported,
    HistoricalLot,
    HistoricalReservation,
    LedgerLotState,
    ledger_state_by_lot,
    lot_state_at,
    reservation_state_at,
)
from app.logistics.inbound_schedules import InboundScheduleView, load_schedule_views
from app.logistics.outbound_schedules import confirmed_outbound_at
from app.logistics.repository import (
    LogisticsRead,
    get_active_logistics_policy,
    get_current_logistics_read,
)
from app.logistics.schemas import InventoryLogisticsSnapshot, InventoryLotSnapshot
from app.logistics.turnover import ItemPolicy, fefo_sort_key, load_item_policy

__all__ = [
    "ACTION_UNSUPPORTED",
    "CAPACITY_WINDOW_UNRESOLVED",
    "IMPACT_INPUT_MISSING",
    "ITEM_NOT_FOUND",
    "LEDGER_ADJUST_UNSUPPORTED",
    "LOT_NOT_FOUND",
    "POLICY_NOT_HISTORICAL",
    "SNAPSHOT_AS_OF_MISMATCH",
    "SUPPORTED_ACTIONS",
    "ActionImpact",
    "CapacityContext",
    "Feasibility",
    "InboundPlan",
    "InboundScheduleFact",
    "ItemLots",
    "LotFact",
    "LotView",
    "OpenExceptions",
    "PolicyView",
    "ReservationFact",
    "SalesCommitments",
    "ToolAnswer",
    "estimate_action_impact",
    "get_capacity_context",
    "get_inbound_schedule",
    "get_item_lots",
    "get_lot",
    "get_open_exceptions",
    "get_policy",
    "get_sales_commitments",
]


# ---------------------------------------------------------------------------
# 어휘 — 못 본 것 · 못 잰 것
# ---------------------------------------------------------------------------

#: 그 실행에 그 Lot 이 없다. 🔴 **0kg Lot 으로 만들지 않는다** — «없다» 와 «비었다» 는
#: 다른 사실이고, 뒤엣것으로 답하면 남의 실행 Lot 을 물은 것도 정상 응답이 된다.
LOT_NOT_FOUND = "LOT_NOT_FOUND"
#: 품목 자체가 `items` 에 없다.
ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
#: 방향을 모르는 `ADJUST` 이동이 있어 원장을 못 셈했다. 그날 물리 사실 전체가 미지다.
LEDGER_ADJUST_UNSUPPORTED = "LEDGER_ADJUST_UNSUPPORTED"
#: 스냅샷 기준일이 요청 기준일과 다르다.
SNAPSHOT_AS_OF_MISMATCH = "SNAPSHOT_AS_OF_MISMATCH"
#: 창 사용률·cap 을 못 셈했다 (리드타임 · 보장 용량 · 입고 예정 미확정).
CAPACITY_WINDOW_UNRESOLVED = "CAPACITY_WINDOW_UNRESOLVED"
#: 🔴 **정책은 과거로 되살릴 수 없다.** 세 정책 표에 유효일 칸이 없어 *"그날 그
#: 정책이었나"* 를 알 수 없다 (`historical_repository.CAPACITY_BASIS_CURRENT_ACTIVE_POLICY`
#: 가 같은 한계를 이미 응답에 적는다). 지금 활성 정책을 쓰되 **그 사실을 적는다.**
POLICY_NOT_HISTORICAL = "POLICY_NOT_HISTORICAL"
#: 카탈로그에 없는 행동이다 (§10.1).
ACTION_UNSUPPORTED = "ACTION_UNSUPPORTED"
#: 그 행동의 영향을 셈할 입력이 모자란다.
IMPACT_INPUT_MISSING = "IMPACT_INPUT_MISSING"


Feasibility = Literal["FEASIBLE", "INFEASIBLE", "UNRESOLVED", "UNSUPPORTED"]

#: 영향을 셈할 수 있는 행동. **§10.1 행동 카탈로그와 같은 어휘다.**
#: 🔴 Proposal 을 여기서 만들지 않는다 — 카탈로그 이름만 공유한다 (Commit 5).
SUPPORTED_ACTIONS: tuple[str, ...] = (
    "SALES_PRIORITY_REQUEST",
    "PURCHASE_ADJUST_REQUEST",
    "ACCEPT_RISK",
    "DISPOSAL_REQUEST",
)


# ---------------------------------------------------------------------------
# 공통 결과 — **무엇을 · 언제 · 어디서 · 무엇을 못 알았나**
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ToolAnswer:
    """모든 Tool 결과의 공통 칸. **사실만으로는 부족한 네 가지를 함께 낸다.**

    ```text
    sim_run_id · as_of   어느 실행 · 어느 날의 사실인가
    observed_as_of       그 사실을 **언제부터 알 수 있었나** (§18 · None = 못 쟀다)
    uncertainties        무엇을 못 봤나 — 🔴 조용히 삼키지 않는다
    source_refs          어느 표·함수에서 왔나
    ```

    ★ 네 칸이 빠지면 조사하는 쪽이 *"확인했고 문제 없음"* 과 *"확인을 못 했음"* 을
      구별할 수 없다 — 그 구별이 이 층의 값어치다.
    """

    sim_run_id: str
    as_of: date
    observed_as_of: date | None
    uncertainties: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class LotFact:
    """`as_of` 시점의 Lot 하나. **잔량도 상태도 원장에서 나온다.**

    ⚠️ `remaining_qty_kg` 는 `inventory_lots.remaining_qty_kg`(지금 값)가 **아니다** —
       `IN − OUT − DISPOSE` 누계다 (`historical_repository.lot_state_at`).
    """

    lot_id: str
    item_id: str
    item: str | None
    grade: str | None
    storage_zone: str | None
    #: 원장에서 **유도한** 상태 (`ACTIVE` · `DEPLETED` · `DISPOSED`).
    #: 🔴 `inventory_lots.status`(지금 값)를 과거로 쓰지 않는다.
    status: str
    received_at: date
    remaining_qty_kg: Decimal
    unit_cost_krw_per_kg: Decimal | None
    remaining_freshness_days: int | None
    effective_freshness_limit_days: int | None
    turnover_status: str | None
    sell_priority: bool
    sell_priority_remaining_days: int | None
    disposal_candidate: bool
    #: 그날 살아 있던 할당의 합 (`ALLOCATED`). 🔴 `SHIPPED` 는 안 센다 — 나간 몫은
    #: 원장 OUT 이 잔량에서 이미 덜어냈다 (`outbound` 와 같은 규율).
    committed_kg: Decimal
    #: 아직 아무도 안 잡은 몫. 🔴 **판매 가용이 아닌 Lot 은 `None`** 이다 —
    #: `tools._sellable_lot_contributions` 와 같은 경계다 (비-ACTIVE · 신선도 만료).
    uncommitted_kg: Decimal | None
    #: §18.2 — 사실마다 따로 든다.
    remaining_qty_observed_as_of: date | None
    status_observed_as_of: date | None

    @property
    def freshness_remaining_ratio(self) -> Decimal | None:
        """잔여 ÷ **유효** 한계. `agent.schemas.ObservedLot` 와 같은 식이다."""
        remaining = self.remaining_freshness_days
        limit = self.effective_freshness_limit_days
        if remaining is None or limit is None or limit <= 0:
            return None
        return Decimal(remaining) / Decimal(limit)


@dataclass(frozen=True, kw_only=True)
class LotView(ToolAnswer):
    """`get_lot` — 없으면 `lot` 이 `None` 이고 사유가 `uncertainties` 에 있다."""

    lot: LotFact | None


@dataclass(frozen=True, kw_only=True)
class ItemLots(ToolAnswer):
    """`get_item_lots` — **FEFO 순서다** (`turnover.fefo_sort_key`)."""

    item_id: str
    lots: tuple[LotFact, ...]


@dataclass(frozen=True, kw_only=True)
class OpenExceptions(ToolAnswer):
    """`get_open_exceptions` — **그날 살아 있던** 문제들."""

    exceptions: tuple[ExceptionRow, ...]


@dataclass(frozen=True, kw_only=True)
class ReservationFact:
    """그날 살아 있던 예약 한 줄. `historical_repository` 가 유도한 값 그대로다."""

    reservation_id: str
    item_id: str
    sale_id: str | None
    sale_date: date | None
    due_date: date | None
    required_qty_kg: Decimal
    reserved_qty_kg: Decimal
    allocated_qty_kg: Decimal
    shipped_qty_kg: Decimal
    unallocated_qty_kg: Decimal
    status: str
    #: 그날 이 예약이 잡고 있던 Lot 들 (`lot_id` → 수량). `SHIPPED` 는 빠진다.
    allocated_by_lot: Mapping[str, Decimal]


@dataclass(frozen=True, kw_only=True)
class SalesCommitments(ToolAnswer):
    """`get_sales_commitments` — 이 품목을 지금 누가 잡고 있나 · 언제 나가나."""

    item_id: str
    live_reservations: tuple[ReservationFact, ...]
    #: 아직 Lot 을 안 고른 예약 잔여의 합.
    unallocated_kg: Decimal
    #: 가장 이른 납품 예정일. 없으면 `None`.
    next_due_date: date | None
    #: `as_of` 뒤의 확정 출고 (`sales` · `sale_items`). 날짜 → 수량.
    confirmed_outbound_by_date: Mapping[date, Decimal]


@dataclass(frozen=True, kw_only=True)
class PolicyView(ToolAnswer):
    """`get_policy` — 🔴 `observed_as_of` 는 **언제나 `None`** 이다 (유효일 칸 없음)."""

    item_id: str | None
    item_policy: ItemPolicy | None
    #: 실행 전역 정책값 (`agent_policy_config`). 키 → 값.
    agent_policy: Mapping[str, Any]
    #: `LogisticsPolicy.source_refs` 그대로.
    policy_source_refs: Mapping[str, str]
    policy_version: str


@dataclass(frozen=True, kw_only=True)
class CapacityContext(ToolAnswer):
    """`get_capacity_context` — 🔴 **CAPACITY_PRESSURE 와 같은 함수·같은 숫자다.**"""

    used_kg: Decimal
    guaranteed_kg: Decimal | None
    burst_kg: Decimal | None
    #: 보장 용량 − 물리 점유. 보장치를 모르면 `None`, 넘겼으면 0 에서 멈춘다.
    available_kg: Decimal | None
    #: `tools.calculate_window_capacity_usage` 결과 그대로.
    window_usage_ratio: Decimal | None
    #: `tools.calculate_cap_by_date` 결과 그대로. 못 셈하면 빈 사전이고 사유가 남는다.
    cap_by_date: Mapping[date, Decimal]
    inbound_lead_days: int | None
    capacity_tight_ratio: Decimal | None
    #: 🔴 정책을 과거로 못 되살린다는 표시 (`CURRENT_ACTIVE_POLICY`).
    capacity_basis: str


@dataclass(frozen=True, kw_only=True)
class InboundScheduleFact:
    """그날 살아 있던 입고 예정 한 건 + 그날까지의 계보."""

    inbound_id: str
    purchase_id: str
    item_id: str
    item: str
    quantity_kg: Decimal
    expected_arrival_date: date
    created_as_of: date
    has_receipt: bool
    #: Lot 과 원장 IN 이 **둘 다** 섰나. 참이면 미래 점유에서 빠진다.
    stock_applied: bool


@dataclass(frozen=True, kw_only=True)
class InboundPlan(ToolAnswer):
    """`get_inbound_schedule` — `days` 는 `cap_by_date` 창을 넘지 않는다.

    ⚠️ 이름이 `inbound_schedules.InboundSchedule` 과 다르다 — 그쪽은 **표 한 행**이고
       이쪽은 **Tool 한 번의 답**이다. 같은 이름을 두 뜻으로 쓰지 않는다.
    """

    days: int
    schedules: tuple[InboundScheduleFact, ...]


@dataclass(frozen=True, kw_only=True)
class ActionImpact(ToolAnswer):
    """`estimate_action_impact` — 🔴 **실행하지 않는다. 숫자를 지어내지도 않는다.**

    ```text
    FEASIBLE     기존 결정론 계산기가 «가능» 이라고 답했다
    INFEASIBLE   〃              «불가» 라고 답했다
    UNRESOLVED   입력이 모자라 못 쟀다      ← 0 으로 메우지 않는다
    UNSUPPORTED  카탈로그에 없는 행동이다
    ```

    ⚠️ `None` 인 칸은 **0 이 아니다.** 판매가·매입가처럼 물류 장부에 없는 값은
       물류가 셈할 수 없고, 그 사실을 `assumptions` 에 적는다.
    """

    action: str
    feasibility: Feasibility
    #: 이 행동이 실제로 움직이는 양.
    affected_kg: Decimal | None
    #: 창고 여유의 변화 (양수 = 자리가 는다).
    capacity_delta_kg: Decimal | None
    #: 🔴 물류 장부의 **취득단가**로만 셈한다 (`inventory_lots.unit_cost_krw_per_kg`).
    estimated_loss_krw: Decimal | None
    freshness_days_left: int | None
    assumptions: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 내부 — 물리 사실 한 벌. **여덟 Tool 이 같은 눈으로 본다**
# ---------------------------------------------------------------------------

#: 판매 가용으로 인정하는 상태. `tools._AVAILABLE_LOT_STATUS` 와 **같은 값이어야 한다.**
_ACTIVE = "ACTIVE"

_LOT_SOURCES = ("inventory_moves", "inventory_lots", "item_storage_policies")
_COMMITMENT_SOURCES = ("inventory_reservations", "inventory_allocations", "sales")


@dataclass(frozen=True)
class _Warehouse:
    """그날의 물리 사실 한 벌. **내부 전용이다 — 결과 타입이 아니다.**"""

    lots: tuple[LotFact, ...]
    reservations: tuple[HistoricalReservation, ...]
    uncertainties: tuple[str, ...]

    def by_lot(self, lot_id: str) -> LotFact | None:
        return next((lot for lot in self.lots if lot.lot_id == lot_id), None)


def _warehouse_at(conn: Any, *, sim_run_id: str, as_of: date) -> _Warehouse:
    """그날의 Lot·예약 사실. 🔴 **여기 한 곳이 원장을 읽는다.**

    ★ 여덟 Tool 이 각자 원장을 읽으면 같은 하루를 두고 서로 다른 잔량을 말할 수 있다 —
      `get_lot` 이 500kg 이라는데 `get_capacity_context` 는 700kg 로 세는 날이 그것이다.

    ⚠️ **`observe()` 를 통째로 부르지 않는다.** 저쪽은 탐지를 위한 한 벌이라 Exception
       표까지 읽고 Current 스냅샷을 눈으로 삼는다 — Lot 하나를 묻는 자리에 그것을
       부르면 범위가 너무 크고, 무엇보다 **과거를 못 본다.**
    """
    try:
        historical_lots = lot_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)
        ledger_state = ledger_state_by_lot(conn, sim_run_id=sim_run_id, as_of=as_of)
    except AdjustMoveNotSupported:
        # 방향을 모르는 이동이 섞이면 그날 잔량 자체가 못 세는 값이다 — fail-closed.
        return _Warehouse(lots=(), reservations=(), uncertainties=(LEDGER_ADJUST_UNSUPPORTED,))

    reservations = reservation_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    committed_by_lot = _allocated_by_lot(reservations)

    lots = tuple(
        _lot_fact(
            lot,
            last_moved_at=_last_moved_at(ledger_state, lot.lot_id),
            committed_by_lot=committed_by_lot,
        )
        for lot in historical_lots
    )
    return _Warehouse(lots=lots, reservations=reservations, uncertainties=())


def _last_moved_at(ledger_state: Mapping[str, LedgerLotState], lot_id: str) -> date | None:
    state = ledger_state.get(lot_id)
    return None if state is None else state.last_moved_at


def _allocated_by_lot(reservations: Sequence[HistoricalReservation]) -> dict[str, Decimal]:
    """그날 살아 있던 할당을 Lot 축으로 모은다. **`ALLOCATED` 만 센다.**

    🔴 `SHIPPED` 는 빼고 `RELEASED` 도 뺀다 — 앞엣것은 원장 OUT 이 잔량에서 이미
       덜어냈고(두 번 빼면 없는 재고가 생긴다), 뒤엣것은 돌려준 몫이다.
       `repository.get_outbound_commitments` 의 `_HOLDING_ALLOCATION` 과 같은 경계다.
    """
    totals: dict[str, Decimal] = {}
    for reservation in reservations:
        for allocation in reservation.allocations:
            if allocation.state != "ALLOCATED":
                continue
            previous = totals.get(allocation.lot_id, Decimal(0))
            totals[allocation.lot_id] = previous + allocation.allocated_qty_kg
    return totals


def _lot_fact(
    lot: HistoricalLot, *, last_moved_at: date | None, committed_by_lot: Mapping[str, Decimal]
) -> LotFact:
    committed_kg = committed_by_lot.get(lot.lot_id, Decimal(0))
    turnover = lot.turnover
    remaining_freshness_days = turnover.remaining_freshness_days
    # 🔴 `tools._sellable_lot_contributions` 와 **같은 경계다.** 비-ACTIVE 와 신선도가
    #    0 이하로 **확인된** Lot 은 애초에 팔 수 없어 «아직 안 잡힌 몫» 이 성립하지
    #    않는다 — 0 으로 적으면 «다 잡혔다» 로 읽힌다.
    sellable = lot.state == _ACTIVE and not (
        remaining_freshness_days is not None and remaining_freshness_days <= 0
    )
    return LotFact(
        lot_id=lot.lot_id,
        item_id=lot.item_id,
        item=lot.item_name,
        grade=lot.grade,
        storage_zone=lot.storage_zone,
        status=lot.state,
        received_at=lot.received_at,
        remaining_qty_kg=lot.remaining_qty_kg,
        unit_cost_krw_per_kg=lot.unit_cost_krw_per_kg,
        remaining_freshness_days=remaining_freshness_days,
        effective_freshness_limit_days=turnover.effective_freshness_limit_days,
        turnover_status=turnover.turnover_status,
        sell_priority=turnover.sell_priority,
        sell_priority_remaining_days=turnover.sell_priority_remaining_days,
        disposal_candidate=turnover.disposal_candidate,
        committed_kg=committed_kg,
        uncommitted_kg=(
            max(Decimal(0), lot.remaining_qty_kg - committed_kg) if sellable else None
        ),
        remaining_qty_observed_as_of=last_moved_at,
        status_observed_as_of=_state_observed_as_of(
            lot.state, received_at=lot.received_at, last_moved_at=last_moved_at
        ),
    )


def _state_observed_as_of(
    state: str, *, received_at: date | None, last_moved_at: date | None
) -> date | None:
    """원장에서 **유도한** 상태의 관측일. 규칙의 주인은 `observe` 다 (§18.2).

    🔴 **`DEPLETED` 한 갈래만 다르고, 다른 이유가 있다.** `observe` 가 보는 것은
       `inventory_lots.status`(캐시)이고 그 어휘로 `DEPLETED` 를 적는 writer 가 없어
       날짜를 못 댄다. 여기 `DEPLETED` 는 **원장 잔량이 0** 이라는 유도 결과라,
       0 이 된 날이 곧 마지막 이동일이다 — 같은 이름이 다른 근거를 가진 자리다.
    """
    if state == "DEPLETED":
        return last_moved_at
    return _status_observed_as_of(state, received_at=received_at, last_moved_at=last_moved_at)


def _lots_observed_as_of(lots: Iterable[LotFact]) -> date | None:
    """Lot 묶음의 물리 사실 관측일 = 잔량 관측일 중 **가장 늦은 것**.

    🔴 하나라도 못 대면 전체가 `None` 이다 — 합계의 관측일은 가장 약한 고리를 따른다.
    """
    return derive_observed_as_of([lot.remaining_qty_observed_as_of for lot in lots])


# ---------------------------------------------------------------------------
# ① get_open_exceptions
# ---------------------------------------------------------------------------


def get_open_exceptions(conn: Any, *, sim_run_id: str, as_of: date) -> OpenExceptions:
    """그날 살아 있던 운영 Exception 전부. **조사의 출발점이다.**

    🔴 **`live_exceptions`(지금 값)를 그대로 내지 않는다.** 그러면 두 가지로 틀린다 —
       ① `opened_as_of > as_of` 인 **미래 문제**가 과거 답에 섞이고(look-ahead),
       ② 그날 열려 있다가 그 뒤 닫힌 문제가 통째로 빠진다. 그래서 표를 두 날짜
       (`opened_as_of` · `resolved_as_of`)로 자르는 `live_exceptions_at` 을 쓴다.

    ⚠️ 행의 `observed_as_of` 는 **탐지가 적어 둔 값**이다 (§18). 지금은 근거에 정책값이
       섞여 전부 `NULL` 이라, 이 Tool 의 관측일도 `None` 이다 — 그것이 계산 결과다.
    """
    rows, uncertainties = live_exceptions_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    return OpenExceptions(
        sim_run_id=sim_run_id,
        as_of=as_of,
        observed_as_of=derive_observed_as_of([row.observed_as_of for row in rows]),
        uncertainties=uncertainties,
        source_refs=("logistics_exceptions",),
        exceptions=rows,
    )


# ---------------------------------------------------------------------------
# ② get_lot · ③ get_item_lots
# ---------------------------------------------------------------------------


def get_lot(conn: Any, *, sim_run_id: str, as_of: date, lot_id: str) -> LotView:
    """그 Lot 의 `as_of` 시점 사실. **없으면 «없다» 고 답한다.**

    🔴 **남의 실행 Lot 은 여기 없다.** 실행 축이 `lot_state_at` 의 `WHERE` 에 있어
       `sim_run_id` 가 다르면 애초에 안 나온다 — 0kg 으로 답하지 않는다.
    """
    warehouse = _warehouse_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    lot = warehouse.by_lot(lot_id)
    uncertainties = (
        warehouse.uncertainties
        if lot is not None
        else (*warehouse.uncertainties, f"{LOT_NOT_FOUND}:{lot_id}")
    )
    return LotView(
        sim_run_id=sim_run_id,
        as_of=as_of,
        observed_as_of=None if lot is None else lot.remaining_qty_observed_as_of,
        uncertainties=uncertainties,
        source_refs=(*_LOT_SOURCES, *_COMMITMENT_SOURCES),
        lot=lot,
    )


def get_item_lots(conn: Any, *, sim_run_id: str, as_of: date, item_id: str) -> ItemLots:
    """같은 품목의 Lot 들. **FEFO 순서다 — 다음에 나갈 것이 맨 앞이다.**

    ★ **정렬 키를 다시 적지 않는다.** `turnover.fefo_sort_key` 가 주인이고 실제 자동
      출고(`outbound.recommend_fefo_candidates`)도 그것을 쓴다 — 두 벌로 적으면
      조사가 말한 순서와 실제로 나가는 순서가 갈린다.

    ⚠️ **잔량 0 Lot 도 낸다.** 걸러내는 것은 읽는 쪽의 판단이지 사실이 아니다
       (`lot_state_at` 과 같은 태도).
    """
    warehouse = _warehouse_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    selected_lots = [lot for lot in warehouse.lots if lot.item_id == item_id]
    selected_lots.sort(
        key=lambda lot: fefo_sort_key(
            remaining_freshness_days=lot.remaining_freshness_days,
            received_at=lot.received_at,
            lot_id=lot.lot_id,
        )
    )
    uncertainties = (
        warehouse.uncertainties
        if selected_lots
        else (*warehouse.uncertainties, f"{ITEM_NOT_FOUND}:{item_id}")
    )
    return ItemLots(
        sim_run_id=sim_run_id,
        as_of=as_of,
        observed_as_of=_lots_observed_as_of(selected_lots),
        uncertainties=uncertainties,
        source_refs=(*_LOT_SOURCES, *_COMMITMENT_SOURCES),
        item_id=item_id,
        lots=tuple(selected_lots),
    )


# ---------------------------------------------------------------------------
# ④ get_sales_commitments
# ---------------------------------------------------------------------------


def get_sales_commitments(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    item_id: str,
    outbound_fn: Callable[..., list[Any]] | None = None,
) -> SalesCommitments:
    """이 품목을 판매 쪽이 얼마나 잡고 있나 · 언제 나가나.

    🔴 **판매 코드를 부르지 않는다.** 이름에 Sales 가 들어가도 읽는 것은 물류가 이미
       쓰는 예약·할당 표다 (`reservation_state_at`). 판매 서비스를 부르면 조사 하나가
       남의 부서 상태를 바꿀 수 있는 길이 열린다.

    🔴 **관측일은 `None` 이다.** `outbound.cancel_allocation` 이 취소에 업무 날짜를 안
       남겨 «이 그림이 언제부터 참이었나» 를 못 댄다 (`COMMITMENT_OBSERVED_AS_OF`).
       Tool 이 생겼다는 이유로 그 상수를 날짜로 바꾸지 않는다.
    """
    reservations = [
        reservation
        for reservation in reservation_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)
        if reservation.item_id == item_id and reservation.state == "HOLDING"
    ]
    # 🔴 **품목 이름을 예약에서 줍지 않는다.** 예약이 0건인 날에도 이름은 있어야
    #    한다 — 없으면 아래 걸러내기가 통째로 열려 **남의 품목 출고**가 섞인다.
    policy = load_item_policy(conn, item_id=item_id)
    item_name = None if policy is None else policy.item_name
    uncertainties = () if policy is not None else (f"{ITEM_NOT_FOUND}:{item_id}",)

    outbound_by_date: dict[date, Decimal] = {}
    if item_name is not None:
        scheduled = (outbound_fn or confirmed_outbound_at)(
            conn, sim_run_id=sim_run_id, as_of=as_of
        )
        for line in scheduled:
            if line.item != item_name:
                continue
            previous = outbound_by_date.get(line.date, Decimal(0))
            outbound_by_date[line.date] = previous + line.quantity_kg

    due_dates = [
        reservation.due_date for reservation in reservations if reservation.due_date is not None
    ]
    return SalesCommitments(
        sim_run_id=sim_run_id,
        as_of=as_of,
        observed_as_of=COMMITMENT_OBSERVED_AS_OF,
        uncertainties=uncertainties,
        source_refs=(*_COMMITMENT_SOURCES, "sale_items"),
        item_id=item_id,
        live_reservations=tuple(
            _reservation_fact(reservation) for reservation in reservations
        ),
        unallocated_kg=sum(
            (reservation.unallocated_qty_kg for reservation in reservations), start=Decimal(0)
        ),
        next_due_date=min(due_dates) if due_dates else None,
        confirmed_outbound_by_date=dict(sorted(outbound_by_date.items())),
    )


def _reservation_fact(reservation: HistoricalReservation) -> ReservationFact:
    return ReservationFact(
        reservation_id=reservation.reservation_id,
        item_id=reservation.item_id,
        sale_id=reservation.sale_id,
        sale_date=reservation.sale_date,
        due_date=reservation.due_date,
        required_qty_kg=reservation.required_qty_kg,
        reserved_qty_kg=reservation.reserved_qty_kg,
        allocated_qty_kg=reservation.allocated_qty_kg,
        shipped_qty_kg=reservation.shipped_qty_kg,
        unallocated_qty_kg=reservation.unallocated_qty_kg,
        status=reservation.status,
        allocated_by_lot=_allocated_by_lot([reservation]),
    )


# ---------------------------------------------------------------------------
# ⑤ get_policy
# ---------------------------------------------------------------------------


def get_policy(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    item_id: str | None = None,
    policy_fn: Callable[[], Any] | None = None,
) -> PolicyView:
    """지금 계산에 쓰는 정책값. 🔴 **과거로 되살릴 수 없고, 그 사실을 적는다.**

    ```text
    item_storage_policies    보관한계 · 중 등급 계수      유효일 칸 없음
    item_turnover_policies   회전목표 · 판매우선 경계      유효일 칸 없음
    agent_policy_config      용량 · 리드타임 · 임계 비율    유효일 칸 없음
    ```

    🔴 **`as_of` 를 관측일로 쓰지 않는다.** 축을 맞추려고 받기는 하지만, 받았다는
       이유로 *"그날 이 정책이었다"* 고 답하면 안 잰 것이 잰 것으로 세어진다 —
       `POLICY_OBSERVED_AS_OF` 가 `None` 인 이유 그대로다. 대신 `POLICY_NOT_HISTORICAL`
       을 언제나 사유에 적는다.
    """
    policy = (policy_fn or get_active_logistics_policy)()
    item_policy = None if item_id is None else load_item_policy(conn, item_id=item_id)
    uncertainties = [POLICY_NOT_HISTORICAL]
    if item_id is not None and item_policy is None:
        uncertainties.append(f"{ITEM_NOT_FOUND}:{item_id}")
    # 🔴 `vars()` 로 긁지 않는다 — pydantic 내부 표현에 기대면 계약이 아니라
    #    구현에 붙는다. 모델이 스스로 내는 값만 쓴다.
    values = policy.model_dump(exclude={"source_refs", "policy_version", "usage_scope"})
    return PolicyView(
        sim_run_id=sim_run_id,
        as_of=as_of,
        # 🔴 계산해서 낸 `None` 이다. 기본값을 둔 것이 아니다 (§18).
        observed_as_of=POLICY_OBSERVED_AS_OF,
        uncertainties=tuple(uncertainties),
        source_refs=("agent_policy_config", "item_storage_policies", "item_turnover_policies"),
        item_id=item_id,
        item_policy=item_policy,
        agent_policy=values,
        policy_source_refs=dict(policy.source_refs),
        policy_version=policy.policy_version,
    )


# ---------------------------------------------------------------------------
# ⑥ get_capacity_context
# ---------------------------------------------------------------------------


def get_capacity_context(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    read_fn: Callable[..., LogisticsRead] = get_current_logistics_read,
) -> CapacityContext:
    """창고 여유 한 벌. 🔴 **`CAPACITY_PRESSURE` 와 같은 함수를 부른다.**

    ```text
    창 구성   tools.build_cap_window                  as_of + lead 부터 18일
    날짜별    tools.calculate_cap_by_date             보장 용량 − 예상 점유
    사용률    tools.calculate_window_capacity_usage   1 − min(cap)/guaranteed
    ```

    🔴 **공식을 새로 적지 않는다.** 같은 창고 상태를 두고 Exception 은 «빡빡하다» 인데
       Tool 은 «여유 있다» 로 답하는 날이 오면 사람이 믿을 값이 없다.

    ★ **점유 축만 원장으로 바꿔 넣는다.** 스냅샷의 나머지(보장 용량 · 리드타임 · 입고
      예정 · 확정 출고)는 이미 `as_of` 로 잘려 오지만(`repository._schedule_lists`),
      Lot 잔량만은 **지금 값**이다 — `historical_repository.lot_state_at` 이
      `_lot_turnover_from_row` 에 그 시점 잔량만 갈아 끼우는 것과 **같은 수법**이다.

    ⚠️ 정책 축은 과거로 못 간다 — `capacity_basis` 가 그 한계를 말한다.
    """
    warehouse = _warehouse_at(conn, sim_run_id=sim_run_id, as_of=as_of)
    read = read_fn(as_of=as_of, sim_run_id=sim_run_id)
    uncertainties = [*warehouse.uncertainties, POLICY_NOT_HISTORICAL]
    if read.snapshot.as_of != as_of:
        uncertainties.append(f"{SNAPSHOT_AS_OF_MISMATCH}:{read.snapshot.as_of}")

    used_kg = sum((lot.remaining_qty_kg for lot in warehouse.lots), start=Decimal(0))
    snapshot = _as_of_snapshot(read.snapshot, lots=warehouse.lots, used_kg=used_kg)

    usage_ratio = calc.calculate_window_capacity_usage(snapshot, as_of)
    window = calc.build_cap_window(snapshot, as_of)
    cap_by_date: dict[date, Decimal] = {}
    if window is None:
        uncertainties.append(CAPACITY_WINDOW_UNRESOLVED)
    else:
        try:
            cap_by_date = calc.calculate_cap_by_date(snapshot, window)
        except ValueError as error:
            uncertainties.append(f"{CAPACITY_WINDOW_UNRESOLVED}:{error}")
    if usage_ratio is None and CAPACITY_WINDOW_UNRESOLVED not in uncertainties:
        uncertainties.append(CAPACITY_WINDOW_UNRESOLVED)

    guaranteed_kg = snapshot.guaranteed_capacity_kg
    return CapacityContext(
        sim_run_id=sim_run_id,
        as_of=as_of,
        # 점유는 원장이 날짜를 주지만 창·임계는 정책이라 결과는 `None` 이다 (§18.2).
        observed_as_of=derive_observed_as_of(
            [_lots_observed_as_of(warehouse.lots), POLICY_OBSERVED_AS_OF]
        ),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
        source_refs=(*_LOT_SOURCES, "agent_policy_config", "inbound_schedules", "sales"),
        used_kg=used_kg,
        guaranteed_kg=guaranteed_kg,
        burst_kg=snapshot.burst_capacity_kg,
        available_kg=None if guaranteed_kg is None else max(Decimal(0), guaranteed_kg - used_kg),
        window_usage_ratio=usage_ratio,
        cap_by_date=cap_by_date,
        inbound_lead_days=snapshot.inbound_lead_days,
        capacity_tight_ratio=snapshot.capacity_tight_ratio,
        # 🔴 **정본 상수를 그대로 쓴다.** 같은 뜻의 문자열을 두 벌 적으면
        #    한쪽만 바뀌는 날 화면과 조사가 다른 한계를 말한다.
        capacity_basis=CAPACITY_BASIS_CURRENT_ACTIVE_POLICY,
    )


def _as_of_snapshot(
    snapshot: InventoryLogisticsSnapshot, *, lots: Sequence[LotFact], used_kg: Decimal
) -> InventoryLogisticsSnapshot:
    """스냅샷의 **점유 축만** 그날 값으로 바꾼다. 나머지는 그대로 둔다.

    ⚠️ `used_capacity_kg` 와 `on_hand_by_lot` 은 **함께** 바꾼다 — 한쪽만 바꾸면
       `_replay_occupancy_by_item` 이 품목 버킷을 짚는 합과 총량이 갈린다.
    """
    return snapshot.model_copy(
        update={
            "on_hand_by_lot": [
                InventoryLotSnapshot(
                    lot_id=lot.lot_id,
                    item=lot.item or lot.item_id,
                    grade=lot.grade,
                    available_qty_kg=lot.remaining_qty_kg,
                    received_at=lot.received_at,
                    unit_cost_krw_per_kg=lot.unit_cost_krw_per_kg,
                    remaining_freshness_days=lot.remaining_freshness_days,
                    effective_freshness_limit_days=lot.effective_freshness_limit_days,
                    status=lot.status,
                    storage_zone=lot.storage_zone,
                )
                for lot in lots
                if lot.remaining_qty_kg > 0
            ],
            "used_capacity_kg": used_kg,
        }
    )


# ---------------------------------------------------------------------------
# ⑦ get_inbound_schedule
# ---------------------------------------------------------------------------


def get_inbound_schedule(
    conn: Any, *, sim_run_id: str, as_of: date, days: int | None = None
) -> InboundPlan:
    """그날 살아 있던 입고 예정. **정본은 `inbound_schedules` 표다.**

    ```text
    살아 있다   created_as_of <= as_of · (cancelled_as_of IS NULL OR > as_of)
    창          expected_arrival_date <= as_of + days
    ```

    🔴 **fixture JSON 을 정본으로 되돌리지 않는다.** 그 축은 이미 표로 옮겨졌고
       (`repository._schedule_lists` 가 그 표를 읽는다), 여기서 JSON 을 다시 읽으면
       취소된 일정이 살아 돌아온다.

    :param days: 창 길이. 기본이자 상한이 `cap_by_date` 창(`18`)이다 — 용량 판정이
        보는 창보다 멀리 보면 «판정에 안 들어간 입고» 가 조사에 섞인다.
    """
    window_days = (
        calc.CAP_BY_DATE_WINDOW_DAYS
        if days is None
        else max(0, min(days, calc.CAP_BY_DATE_WINDOW_DAYS))
    )
    window_end = as_of + timedelta(days=window_days)
    views = [
        view
        for view in load_schedule_views(conn, sim_run_id=sim_run_id, as_of=as_of)
        if view.expected_arrival_date <= window_end
    ]
    return InboundPlan(
        sim_run_id=sim_run_id,
        as_of=as_of,
        # ✅ 입고 일정은 **업무 날짜가 있다** — 장부에 선 날(`created_as_of`)이다.
        observed_as_of=derive_observed_as_of([view.created_as_of for view in views]),
        uncertainties=(),
        source_refs=("inbound_schedules", "purchase_items", "items"),
        days=window_days,
        schedules=tuple(_schedule_fact(view) for view in views),
    )


def _schedule_fact(view: InboundScheduleView) -> InboundScheduleFact:
    return InboundScheduleFact(
        inbound_id=view.inbound_id,
        purchase_id=view.purchase_id,
        item_id=view.item_id,
        item=view.item_name,
        quantity_kg=view.quantity_kg,
        expected_arrival_date=view.expected_arrival_date,
        created_as_of=view.created_as_of,
        has_receipt=view.has_receipt,
        stock_applied=view.stock_applied,
    )


# ---------------------------------------------------------------------------
# ⑧ estimate_action_impact
# ---------------------------------------------------------------------------


def estimate_action_impact(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    action: str,
    parameters: Mapping[str, Any],
    read_fn: Callable[..., LogisticsRead] = get_current_logistics_read,
) -> ActionImpact:
    """가정한 행동의 영향만 **읽는다**. 🔴 **아무것도 실행하지 않는다.**

    ```text
    SALES_PRIORITY_REQUEST   그 Lot 의 미확정 물량이 나간다
    DISPOSAL_REQUEST         그 Lot 의 일부가 버려진다        ← 손실은 취득단가로만
    PURCHASE_ADJUST_REQUEST  예정 입고가 늘거나 준다          ← 가능 여부는 cap_by_date
    ACCEPT_RISK              **아무것도 안 움직인다** (기록형)
    ```

    🔴 **카탈로그 밖 행동의 영향을 지어내지 않는다** — `UNSUPPORTED` 로 답한다.
       모르는 행동에 그럴듯한 숫자를 붙이면 그 숫자가 제안의 근거가 된다.

    🔴 **판매가·매입가를 셈하지 않는다.** 물류 장부에 없는 값이라 `None` 이고,
       왜 `None` 인지를 `assumptions` 에 적는다 (0 은 «손실 없음» 이라는 주장이다).
    """
    if action not in SUPPORTED_ACTIONS:
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action=action,
            feasibility="UNSUPPORTED",
            uncertainties=(f"{ACTION_UNSUPPORTED}:{action}",),
            assumptions=(f"카탈로그에 없는 행동이다 — 지원: {', '.join(SUPPORTED_ACTIONS)}",),
        )
    if action == "PURCHASE_ADJUST_REQUEST":
        return _purchase_adjust_impact(
            conn, sim_run_id=sim_run_id, as_of=as_of, parameters=parameters, read_fn=read_fn
        )
    return _lot_action_impact(
        conn, sim_run_id=sim_run_id, as_of=as_of, action=action, parameters=parameters
    )


def _lot_action_impact(
    conn: Any, *, sim_run_id: str, as_of: date, action: str, parameters: Mapping[str, Any]
) -> ActionImpact:
    """Lot 하나를 대상으로 하는 세 행동. **대상이 없으면 셈하지 않는다.**"""
    lot_id = parameters.get("lot_id")
    if not isinstance(lot_id, str) or not lot_id:
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action=action,
            feasibility="UNRESOLVED",
            uncertainties=(f"{IMPACT_INPUT_MISSING}:lot_id",),
        )
    view = get_lot(conn, sim_run_id=sim_run_id, as_of=as_of, lot_id=lot_id)
    lot = view.lot
    if lot is None:
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action=action,
            feasibility="UNRESOLVED",
            uncertainties=view.uncertainties,
        )

    if action == "ACCEPT_RISK":
        # 🔴 **상태를 바꾸지 않는 기록형이다** (§10.1). 0 은 여기서 추측이 아니라 사실이다.
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action=action,
            feasibility="FEASIBLE",
            observed_as_of=lot.remaining_qty_observed_as_of,
            affected_kg=Decimal(0),
            capacity_delta_kg=Decimal(0),
            estimated_loss_krw=Decimal(0),
            freshness_days_left=lot.remaining_freshness_days,
            assumptions=("위험을 안고 간다는 기록만 남는다 — 재고도 자리도 안 움직인다",),
            uncertainties=view.uncertainties,
        )

    if action == "DISPOSAL_REQUEST":
        requested_kg = _quantity(parameters.get("qty_kg"))
        if requested_kg is None:
            return _impact(
                sim_run_id=sim_run_id,
                as_of=as_of,
                action=action,
                feasibility="UNRESOLVED",
                uncertainties=(*view.uncertainties, f"{IMPACT_INPUT_MISSING}:qty_kg"),
            )
        affected_kg = min(requested_kg, lot.remaining_qty_kg)
        unit_cost = lot.unit_cost_krw_per_kg
        assumptions = [
            "폐기 손실은 장부 취득단가 × 수량이다 — 판매 기회손실은 물류 장부에 없다"
        ]
        if unit_cost is None:
            assumptions.append("취득단가를 못 읽어 손실을 셈하지 않았다")
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action=action,
            feasibility="FEASIBLE" if requested_kg <= lot.remaining_qty_kg else "INFEASIBLE",
            observed_as_of=lot.remaining_qty_observed_as_of,
            affected_kg=affected_kg,
            capacity_delta_kg=affected_kg,
            estimated_loss_krw=None if unit_cost is None else affected_kg * unit_cost,
            freshness_days_left=lot.remaining_freshness_days,
            assumptions=tuple(assumptions),
            uncertainties=view.uncertainties,
        )

    # SALES_PRIORITY_REQUEST
    uncommitted_kg = lot.uncommitted_kg
    if uncommitted_kg is None:
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action=action,
            feasibility="UNRESOLVED",
            uncertainties=(*view.uncertainties, f"{IMPACT_INPUT_MISSING}:uncommitted_kg"),
            assumptions=("판매 가용이 아닌 Lot 이라 «아직 안 잡힌 몫» 이 성립하지 않는다",),
        )
    return _impact(
        sim_run_id=sim_run_id,
        as_of=as_of,
        action=action,
        feasibility="FEASIBLE" if uncommitted_kg > 0 else "INFEASIBLE",
        observed_as_of=lot.remaining_qty_observed_as_of,
        affected_kg=uncommitted_kg,
        capacity_delta_kg=uncommitted_kg,
        estimated_loss_krw=None,
        freshness_days_left=lot.remaining_freshness_days,
        assumptions=(
            "판매가는 물류 장부에 없다 — 매출·이익 영향은 Sales 가 셈한다",
            "미확정 물량이 전부 나간다고 본 상한이다",
        ),
        uncertainties=view.uncertainties,
    )


def _purchase_adjust_impact(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    parameters: Mapping[str, Any],
    read_fn: Callable[..., LogisticsRead],
) -> ActionImpact:
    """예정 입고를 늘리거나 줄인다. **가능 여부는 `cap_by_date` 가 답한다.**

    🔴 **새 용량 판정을 만들지 않는다.** `scenario_engine.validate_purchase_scenarios`
       가 쓰는 그 `calculate_cap_by_date` 를 그대로 본다 — 매입 시나리오 판정과
       조사가 서로 다른 여유를 말하면 안 된다.

    ⚠️ **매입 단가를 모른다.** 금액 영향은 `None` 이고 그 사실을 가정에 적는다.
    """
    delta_kg = _quantity(parameters.get("qty_delta_kg"), allow_negative=True)
    if delta_kg is None:
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action="PURCHASE_ADJUST_REQUEST",
            feasibility="UNRESOLVED",
            uncertainties=(f"{IMPACT_INPUT_MISSING}:qty_delta_kg",),
        )
    capacity = get_capacity_context(conn, sim_run_id=sim_run_id, as_of=as_of, read_fn=read_fn)
    if not capacity.cap_by_date:
        return _impact(
            sim_run_id=sim_run_id,
            as_of=as_of,
            action="PURCHASE_ADJUST_REQUEST",
            feasibility="UNRESOLVED",
            uncertainties=capacity.uncertainties,
            capacity_delta_kg=-delta_kg,
            assumptions=("창 여유를 못 셈해 가능 여부를 판정하지 않았다",),
        )
    # 🔴 **창에서 가장 빡빡한 날로 본다.** 도착일이 정해지지 않은 조정이라
    #    특정 날짜를 골라 잡으면 그 선택이 곧 판정이 된다.
    tightest_cap = min(capacity.cap_by_date.values())
    feasibility: Feasibility = (
        "FEASIBLE" if delta_kg <= 0 or delta_kg <= tightest_cap else "INFEASIBLE"
    )
    return _impact(
        sim_run_id=sim_run_id,
        as_of=as_of,
        action="PURCHASE_ADJUST_REQUEST",
        feasibility=feasibility,
        observed_as_of=capacity.observed_as_of,
        affected_kg=abs(delta_kg),
        capacity_delta_kg=-delta_kg,
        estimated_loss_krw=None,
        assumptions=(
            f"창 {len(capacity.cap_by_date)}일 중 가장 빡빡한 날의 여유 {tightest_cap}kg 와 견줬다",
            "매입 단가는 물류 장부에 없다 — 금액 영향은 Purchase 가 셈한다",
        ),
        uncertainties=capacity.uncertainties,
    )


def _quantity(value: Any, *, allow_negative: bool = False) -> Decimal | None:
    """수량 인자 하나. 🔴 **문자열을 float 으로 지나게 하지 않는다.**"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        quantity = value
    elif isinstance(value, int | str):
        try:
            quantity = Decimal(value)
        except (ArithmeticError, ValueError):
            return None
    else:
        return None
    if not allow_negative and quantity <= 0:
        return None
    return quantity


def _impact(
    *,
    sim_run_id: str,
    as_of: date,
    action: str,
    feasibility: Feasibility,
    observed_as_of: date | None = None,
    affected_kg: Decimal | None = None,
    capacity_delta_kg: Decimal | None = None,
    estimated_loss_krw: Decimal | None = None,
    freshness_days_left: int | None = None,
    assumptions: tuple[str, ...] = (),
    uncertainties: tuple[str, ...] = (),
) -> ActionImpact:
    return ActionImpact(
        sim_run_id=sim_run_id,
        as_of=as_of,
        observed_as_of=observed_as_of,
        uncertainties=uncertainties,
        source_refs=(*_LOT_SOURCES, "agent_policy_config"),
        action=action,
        feasibility=feasibility,
        affected_kg=affected_kg,
        capacity_delta_kg=capacity_delta_kg,
        estimated_loss_krw=estimated_loss_krw,
        freshness_days_left=freshness_days_left,
        assumptions=assumptions,
    )
