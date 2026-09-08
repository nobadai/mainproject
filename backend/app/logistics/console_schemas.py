"""재고·물류 운영 콘솔 API 계약 (Read + 최소 Command).

★ **이 파일은 계산하지 않는다.** 값의 정본은 전부 기존 도메인 모듈이고
  (`tools` · `turnover` · `outbound` · `warehouse` · `transport`), 여기는 그 결과를
  HTTP 로 실어 나르는 모양만 정한다.

🔴 **`null` · `0` · `[]` 를 서로 바꾸지 않는다.**

```text
null   모름 · 값 없음 · 확인되지 않음
0      확인 결과 0
[]     확인 결과 0건
```

   그래서 아래 여러 칸이 `| None` 이다. 특히 `available_qty_kg` 와
   `inbound_receipts` 의 수량 넷은 **0 으로 메우면 안 되는 자리**다 —
   앞은 판정에 필요한 축을 못 읽었다는 뜻이고, 뒤는 DDL 주석이
   *"미입력(NULL)은 0 으로 보지 않는다"* 로 못박은 칸이다.

🔴 **세 상태를 섞지 않는다.**

```text
SELL_PRIORITY            먼저 판매 검토
STORAGE_TARGET_EXCEEDED  내부 회전목표 초과   ★ != 폐기 · != 판매불가
disposal_candidate       신선도 만료 (remaining_freshness_days <= 0)
```
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.logistics.outbound import AllocationBasis, AllocationStatus, ReservationStatus
from app.logistics.schemas import RuntimeSourceStatus
from app.logistics.turnover import TurnoverStatus

#: `available_qty_kg` 를 못 낸 이유. `tools.build_inventory_by_item` 이 `None` 을
#: 돌려주는 세 경로와 1:1 이다 — 어느 축을 못 읽었는지 화면이 알아야 한다.
AvailableQtyUnresolvedReason = Literal[
    "CONFIRMED_OUTBOUND_UNRESOLVED",
    "CONFIRMED_OUTBOUND_ITEM_MISSING",
    "OUTBOUND_COMMITMENTS_UNRESOLVED",
]

#: Lot 이 지금 자리에 앉아 있나. `pallets` 에 살아있는(ACTIVE·HOLD) 행이 있으면 PLACED.
Placement = Literal["PLACED", "UNPLACED"]

#: 이 품목의 Zone 정책을 아는가. **정책 없음(UNRESOLVED)과 전부 금지는 다르다**
#: (`warehouse._zone_allowed` 의 세 상태와 같은 규율).
ZonePolicyStatus = Literal["CONFIRMED", "UNRESOLVED"]


class ConsoleModel(BaseModel):
    """콘솔 계약 공통 설정. 계약에 없는 칸을 조용히 흘리지 않는다."""

    model_config = ConfigDict(extra="forbid")


# ── 재고 ────────────────────────────────────────────────────────────────


class ConsoleInventoryItem(ConsoleModel):
    """품목 한 줄. **현재고와 판매가능량은 다른 값이다.**"""

    item_id: str
    item_name: str
    #: 물리 실재량. 만료 Lot 도 창고에 있으면 여기 들어간다.
    on_hand_qty_kg: Decimal
    #: 판매가능량 — `tools.build_inventory_by_item` 정본.
    #: 🔴 못 읽은 축이 있으면 `None` 이다. **0 으로 바꾸지 않는다.**
    available_qty_kg: Decimal | None
    #: 🔴 **지금 실제로 재고를 잡고 있는 양**이다 — 예약 행에 적힌 확보량의 합이 아니다.
    #:
    #:    ```text
    #:    reserved_qty_kg = allocated_qty_kg + unallocated_reserved_qty_kg
    #:    ```
    #:
    #:    항등식인 것이 계약이다. 전량 출고가 끝난 예약은 셋 다 0 이 된다
    #:    (`ship_allocated_stock` 이 예약 행의 `reserved_qty_kg` 를 안 줄이기 때문에
    #:    원래 값을 합하면 나간 재고를 아직 잡고 있는 것으로 보인다).
    #:
    #:    ⚠️ `ConsoleReservation.reserved_qty_kg` 와 **뜻이 다르다** — 저쪽은 예약
    #:       행에 적힌 DB 값 그대로다.
    reserved_qty_kg: Decimal
    #: 그 예약들이 **아직 안 내보낸** Lot 할당량 (ALLOCATED · PICKED).
    allocated_qty_kg: Decimal
    #: 잡아 뒀지만 아직 Lot 을 안 고른 몫. 이미 배정한 몫(SHIPPED 포함)을 뺀 값이다.
    unallocated_reserved_qty_kg: Decimal
    #: 🔴 **잡고 있는 양이 0 보다 큰 예약 수**다. 상태가 `ALLOCATED` 로 남아 있어도
    #:    전량 출고가 끝났으면 세지 않는다 — 기준은 상태 어휘가 아니라 수량이다.
    active_reservation_count: int
    #: `turnover.sell_priority` 가 참인 Lot 수. **폐기와 무관하다.**
    sell_priority_lot_count: int
    #: `remaining_freshness_days <= 0` 이며 잔량이 남은 Lot 수.
    expired_lot_count: int
    expired_qty_kg: Decimal
    #: `turnover.is_disposal_candidate` 가 참인 Lot 수. 만료 기준과 같은 판정이라
    #: `expired_lot_count` 와 같은 값이 나온다 — 두 이름을 화면이 함께 쓰기에 둘 다 싣는다.
    disposal_candidate_lot_count: int


class ConsoleInventoryLot(ConsoleModel):
    """Lot 한 줄. 파생값은 전부 `turnover.load_lot_turnover` 가 만든 것이다."""

    lot_id: str
    item_id: str
    item_name: str | None
    #: 🔴 `repository._normalize_grade` 를 지난 값이다. 정규화표에 없는 raw 등급은
    #:    `None` 이 된다 — 임의 치환(`상품 → 상`)을 하지 않는다.
    grade: str | None
    remaining_qty_kg: Decimal
    received_at: date
    status: str | None
    #: 🔴 **`ConsoleZone.zone_id` 와 다른 어휘다. 조인하지 않는다.**
    #:    이 칸의 주인은 `item_storage_policies.storage_zone` 이고(실측 `COLD_HUMID_0_3`
    #:    계열), 창고 Zone 의 주인은 `warehouse_zones.zone_id` 다(실측
    #:    `HIGH_HUMIDITY_COLD` 계열). Lot 의 물리 위치는 `lot_locations[].zone_id` 로 본다.
    storage_zone: str | None
    remaining_freshness_days: int | None
    remaining_turnover_days: int | None
    #: 회전 정책이 없는 품목이면 `None`. **`NORMAL` 로 채우지 않는다.**
    turnover_status: TurnoverStatus | None
    sell_priority: bool
    disposal_candidate: bool


class ConsoleCapacity(ConsoleModel):
    """창고 kg Capacity. **Pallet Position 축과 다른 단위다.**"""

    #: 🔴 만료 Lot 도 잔량이 남아 있으면 여기 포함된다 — 판매불가 != 창고에서 사라짐.
    used_capacity_kg: Decimal
    guaranteed_capacity_kg: Decimal | None
    burst_capacity_kg: Decimal | None


class ConsoleInventoryResponse(ConsoleModel):
    sim_run_id: str
    as_of: date
    items: list[ConsoleInventoryItem]
    lots: list[ConsoleInventoryLot]
    capacity: ConsoleCapacity
    #: `available_qty_kg` 가 전부 `None` 일 때만 채워진다. 그 외에는 `None`.
    available_qty_unresolved_reason: AvailableQtyUnresolvedReason | None = None


# ── 재고 이동 ───────────────────────────────────────────────────────────


class ConsoleInventoryMove(ConsoleModel):
    """원장 한 줄. **조회만 한다 — 이 API 는 Move 를 만들지 않는다.**"""

    move_id: str
    lot_id: str
    item_id: str | None
    item_name: str | None
    move_type: str
    quantity_kg: Decimal
    moved_at: date
    reason_code: str
    note: str | None
    sale_item_id: str | None


class ConsoleInventoryMovesResponse(ConsoleModel):
    sim_run_id: str
    moves: list[ConsoleInventoryMove]


# ── 입고 ────────────────────────────────────────────────────────────────


class ConsoleInTransitItem(ConsoleModel):
    inbound_id: str | None
    #: 🔴 마스터 규약이 이 값을 안 넘긴다 — `None` 이 **확정된 정상 상태**다
    #:    (`transition.build_next_inventory`). 그래서 도착일이 와도 `blocked` 로 갈린다.
    purchase_id: str | None
    item: str
    quantity_kg: Decimal
    expected_arrival_date: date | None


class ConsoleInboundReceipt(ConsoleModel):
    """Receipt + 검수 + 재고반영을 한 줄로 모은 것."""

    inbound_id: str | None
    receipt_id: str
    item_id: str
    item_name: str | None
    arrived_at: date
    #: 🔴 넷 다 nullable 이다. **NULL 을 0 으로 바꾸지 않는다** (DDL 주석).
    ordered_qty_kg: Decimal | None
    accepted_qty_kg: Decimal | None
    hold_qty_kg: Decimal | None
    rejected_qty_kg: Decimal | None
    receipt_status: str
    fact_source: str
    inspection_id: str | None
    inspection_verdict: str | None
    inspected_qty_kg: Decimal | None
    lot_id: str | None
    in_move_id: str | None
    #: Lot 과 원장 IN 이 **둘 다** 있을 때만 참.
    stock_applied: bool


class ConsoleArrivalSummary(ConsoleModel):
    """`arrival.select_due_inbound` 의 네 갈래 건수.

    ⚠️ `due_count = 0` 이 정상일 수 있다 — `due` 는 `purchase_id` 까지 있어야 하는데
       마스터가 그 값을 안 넘기기로 확정했다. 도착일이 온 건은 `blocked` 로 나온다.
    """

    source_status: RuntimeSourceStatus
    due_count: int
    blocked_count: int
    not_due_count: int
    unresolved_count: int
    overdue_count: int


class ConsoleInboundResponse(ConsoleModel):
    sim_run_id: str
    as_of: date
    in_transit_status: RuntimeSourceStatus
    #: 🔴 `None`(미확인) 과 `[]`(0건 확인)은 다른 사실이다. `in_transit_status` 가 가른다.
    in_transit: list[ConsoleInTransitItem] | None
    receipts: list[ConsoleInboundReceipt]
    arrival_summary: ConsoleArrivalSummary


# ── 창고 ────────────────────────────────────────────────────────────────


class ConsoleZone(ConsoleModel):
    """Zone 한 줄. 🔴 **단위는 kg 이 아니라 Pallet Position 이다.**"""

    zone_id: str
    zone_code: str
    zone_name: str
    zone_kind: str
    purpose: str
    #: `is_active` 인 자리 수.
    total_positions: int
    #: 그 자리에 앉은 ACTIVE·HOLD Pallet 수.
    occupied_positions: int
    free_positions: int


class ConsoleLotLocation(ConsoleModel):
    lot_id: str
    item_id: str
    item_name: str | None
    remaining_qty_kg: Decimal
    pallet_id: str | None
    pallet_status: str | None
    zone_id: str | None
    location_id: str | None
    placement: Placement


class ConsoleWarehouseResponse(ConsoleModel):
    sim_run_id: str
    zones: list[ConsoleZone]
    lot_locations: list[ConsoleLotLocation]


class ConsolePlacementZone(ConsoleModel):
    zone_id: str
    zone_name: str
    allowed: bool
    is_default: bool


class ConsoleFreeLocation(ConsoleModel):
    zone_id: str
    location_id: str
    location_kind: str
    lane_code: str | None
    rack_code: str | None
    bay_code: str | None
    level_no: int | None
    position_no: int


class ConsolePlacementOptionsResponse(ConsoleModel):
    """사람이 자리를 고를 때 보는 것. 🔴 **추천도 자동선택도 하지 않는다.**"""

    sim_run_id: str
    lot_id: str
    item_id: str
    #: 🔴 `UNRESOLVED` 는 *"이 품목의 Zone 정책이 아예 없다"* 이고 *"전부 금지"* 가 아니다.
    #:    그때 `zones` · `free_locations` 는 비지만, 배치가 금지라는 뜻이 아니라
    #:    **먼저 정책을 정해야 한다**는 뜻이다 (`warehouse._zone_allowed` 와 같은 규율).
    zone_policy_status: ZonePolicyStatus
    zones: list[ConsolePlacementZone]
    #: `allowed` Zone 의 빈 자리만. 금지 Zone 의 자리는 싣지 않는다.
    free_locations: list[ConsoleFreeLocation]


# ── 출고 ────────────────────────────────────────────────────────────────


class ConsoleAllocation(ConsoleModel):
    allocation_id: str
    lot_id: str
    pallet_id: str | None
    allocated_qty_kg: Decimal
    allocation_basis: AllocationBasis
    decided_by: str
    decided_at: datetime
    status: AllocationStatus
    note: str | None


class ConsoleReservation(ConsoleModel):
    """예약 한 줄과 그 아래 할당들.

    🔴 **`allocated_qty_kg` 와 `unallocated_qty_kg` 의 분모가 다르다. 일부러다.**

    ```text
    allocated_qty_kg    ALLOCATED · PICKED           아직 창고에서 안 나간 몫
    unallocated_qty_kg  reserved − (ALLOCATED · PICKED · SHIPPED)
                                                     아직 Lot 을 안 고른 몫
    ```

       `SHIPPED` 를 앞에서는 빼고 뒤에서는 넣는다 — 나간 몫은 이미 원장 OUT 이
       잔량에서 덜어냈고(그래서 '잡고 있는 양'이 아니다), 그 예약이 더 이상 새로
       잡아 둘 필요도 없다. `outbound.item_free_stock_qty` 와 **같은 규율**이다.
    """

    reservation_id: str
    item_id: str
    item_name: str | None
    sale_id: str | None
    required_qty_kg: Decimal
    #: ⚠️ **예약 행에 적힌 DB 값 그대로다.** `ConsoleInventoryItem.reserved_qty_kg`
    #:    (지금 잡고 있는 양)와 뜻이 다르다 — 전량 출고 뒤에도 이 값은 안 줄어든다.
    reserved_qty_kg: Decimal
    allocated_qty_kg: Decimal
    unallocated_qty_kg: Decimal
    due_date: date | None
    status: ReservationStatus
    allocations: list[ConsoleAllocation]


class ConsoleOutboundResponse(ConsoleModel):
    sim_run_id: str
    #: 0건이면 `[]` 다. **더미를 만들지 않는다.**
    reservations: list[ConsoleReservation]


class ConsoleFefoCandidate(ConsoleModel):
    lot_id: str
    #: 🔴 *"이 Lot 에서 아직 다른 할당에 안 묶인 물리량"* 이다.
    #:    **추가로 예약할 수 있는 양이 아니다** (`recommend_fefo_candidates` 주석).
    available_qty_kg: Decimal
    remaining_freshness_days: int | None
    received_at: date
    #: DB raw 등급 그대로다 — FEFO 후보는 정규화하지 않는다.
    grade: str | None


class ConsoleFefoResponse(ConsoleModel):
    """FEFO 추천. 🔴 **고르지 않는다 — 자동 Allocation 이 아니다.**"""

    reservation_id: str
    sim_run_id: str
    item_id: str
    #: 이 예약이 아직 Lot 을 안 고른 몫. `unallocated_qty_kg` 와 같은 식이다.
    remaining_reservation_qty_kg: Decimal
    candidates: list[ConsoleFefoCandidate]


# ── 운송 견적 ───────────────────────────────────────────────────────────


class ConsoleTransportQuoteResponse(ConsoleModel):
    """`transport.plan_fixed_route_transport` 결과 그대로. **아무것도 쓰지 않는다.**"""

    logistics_contract_id: str
    distance_km: Decimal
    vehicle_class: str
    body_type: str
    vehicle_operational_payload_kg: Decimal
    shipment_qty_kg: Decimal
    trip_count: int
    fixed_fee_per_trip_krw: Decimal
    estimated_cost_krw: Decimal
    #: 🔴 정본이 스키마에 없다. 항상 `null` 이다 — 거리÷속도로 ETA 를 지어내지 않는다.
    standard_minutes: int | None
    contract_baseline_cost_krw: Decimal | None
    contract_vehicle_class: str | None


# ── Command ─────────────────────────────────────────────────────────────


class ConsolePlacementRequest(ConsoleModel):
    """Pallet 배치. 🔴 **재고는 1g 도 움직이지 않는다.**"""

    pallet_id: str = Field(min_length=1)
    sim_run_id: str = Field(min_length=1)
    lot_id: str = Field(min_length=1)
    location_id: str = Field(min_length=1)
    occurred_at: datetime
    #: `pallet_events.recorded_by` 가 NOT NULL 이다. 물류가 지어내지 않는다.
    recorded_by: str = Field(min_length=1)
    packaging_spec_id: str | None = None
    note: str | None = None


class ConsolePlacementResponse(ConsoleModel):
    #: `False` 는 실패가 아니라 **멱등 재실행**이다 (같은 사실이 이미 있다).
    applied: bool
    pallet_id: str
    location_id: str
    zone_id: str
    status: str


class ConsoleAllocationRequestItem(ConsoleModel):
    lot_id: str = Field(min_length=1)
    quantity_kg: Decimal = Field(gt=0)


class ConsoleAllocateRequest(ConsoleModel):
    """사람이 고른 Lot 과 수량."""

    requests: list[ConsoleAllocationRequestItem] = Field(min_length=1)
    decided_by: str = Field(min_length=1)
    #: 시간대를 단 datetime 이어야 한다. 서버가 시계를 읽지 않는다.
    decided_at: datetime
    #: 🔴 **기본값이 없다.** FEFO 후보를 불러 봤다는 사실과 그 추천을 따랐다는 사실은
    #:    다르다 — 기본값을 두면 묻지도 않고 뒤엣것을 장부에 적는다.
    allocation_basis: AllocationBasis
    as_of: date


class ConsoleAllocateResponse(ConsoleModel):
    applied: bool
    reservation_id: str
    allocation_ids: list[str]
    reservation_status: ReservationStatus
    allocated_qty_kg: Decimal


class ConsoleShipRequest(ConsoleModel):
    shipped_at: date
    #: Sales 가 소유한 참조다. 물류가 만들거나 뜯지 않는다.
    sale_item_id: str | None = None


class ConsoleShipResponse(ConsoleModel):
    """실출고. 🔴 **여기서만 원장 OUT 이 나가고 잔량이 준다.**"""

    applied: bool
    reservation_id: str
    shipped_allocation_ids: list[str]
    move_ids: list[str]
    shipped_qty_kg: Decimal


class ConsoleReleaseRequest(ConsoleModel):
    #: 놓아주는 상태만 받는다.
    status: Literal["RELEASED", "CANCELLED"]


class ConsoleReleaseResponse(ConsoleModel):
    applied: bool
    reservation_id: str
    status: ReservationStatus
    required_qty_kg: Decimal
