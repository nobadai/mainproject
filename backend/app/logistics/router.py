"""재고·물류 Agent API Router + 운영 콘솔 API Router.

🔴 **두 묶음이 한 파일에 있지만 계약은 다르다.**

```text
Agent 경로     POST /procurement · POST /sales · GET /runs · GET /runs/{run_id}
               Master/Adapter 가 부른다. 이 파일의 콘솔 작업이 **건드리지 않는다**
콘솔 경로      GET /inventory · /inbound · /warehouse · /outbound · /transport/quote
               + 최소 Command 넷. 운영 화면이 부른다
```

   ⚠️ 운영 화면이 `/procurement` · `/sales` 를 직접 부르지 않는다 — 그쪽은 Agent
      판정 경로이고 화면 조회용이 아니다.

★ **Router 는 얇다.** Query·Body 검증과 도메인 예외의 HTTP 번역만 하고, 판매가능량 ·
  회전 · FEFO · Capacity 계산은 하나도 여기 없다 (`console_service` 가 조립한다).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import ValidationError

from app.logistics.console_schemas import (
    ConsoleAllocateRequest,
    ConsoleAllocateResponse,
    ConsoleFefoResponse,
    ConsoleInboundResponse,
    ConsoleInventoryMovesResponse,
    ConsoleInventoryResponse,
    ConsoleOutboundResponse,
    ConsolePlacementOptionsResponse,
    ConsolePlacementRequest,
    ConsolePlacementResponse,
    ConsoleReleaseRequest,
    ConsoleReleaseResponse,
    ConsoleShipRequest,
    ConsoleShipResponse,
    ConsoleTransportQuoteResponse,
    ConsoleWarehouseResponse,
)
from app.logistics.console_service import (
    allocate_reservation,
    get_inbound_console,
    get_inventory_console,
    get_inventory_moves_console,
    get_outbound_console,
    get_placement_options_console,
    get_reservation_fefo_console,
    get_transport_quote_console,
    get_warehouse_console,
    place_lot,
    release_reservation_console,
    ship_reservation,
)
from app.logistics.ledger import (
    InvalidMoveQuantity,
    MoveIdConflict,
    MoveLineTotalMismatch,
    OriginalQuantityExceeded,
    RemainingQuantityInsufficient,
    UnsupportedMoveType,
)
from app.logistics.outbound import (
    InvalidOutboundRequest,
    OutboundIntegrityError,
    ReservationConflict,
    ReservationStatus,
)
from app.logistics.schemas import (
    FinalVerdict,
    LogisticsAgentRunResponse,
    LogisticsCycle,
    LogisticsProcurementResponse,
    LogisticsSalesRequest,
    LogisticsSalesResponse,
    PurchaseAgentOutput,
    RuntimeStatus,
)
from app.logistics.service import (
    get_logistics_run,
    list_logistics_runs,
    run_logistics_procurement,
    run_logistics_sales,
)
from app.logistics.transport import (
    AmbiguousRate,
    AmbiguousRoute,
    InvalidTransportRequest,
    VehicleTooLargeToSplit,
)
from app.logistics.warehouse import (
    InvalidPlacementRequest,
    PalletNotEmptyable,
    PlacementConflict,
    SpecItemMismatch,
    WarehouseIntegrityError,
    ZonePolicyUnresolved,
)

router = APIRouter(prefix="/logistics", tags=["logistics"])

#: 🔴 **모호함은 부재가 아니다.** 둘 다 `LookupError` 라 **먼저** 잡아야 한다 —
#:   순서를 바꾸면 *"계약이 둘이라 못 고른다"* 가 *"계약이 없다"* 로 나간다.
_AMBIGUOUS_ERRORS = (AmbiguousRoute, AmbiguousRate)

#: 업무 요청이 규칙을 어긴 것 — 데이터가 깨진 것이 아니다.
_BAD_REQUEST_ERRORS = (
    InvalidOutboundRequest,
    InvalidPlacementRequest,
    InvalidTransportRequest,
    InvalidMoveQuantity,
    OriginalQuantityExceeded,
    PalletNotEmptyable,
    RemainingQuantityInsufficient,
    SpecItemMismatch,
    UnsupportedMoveType,
    VehicleTooLargeToSplit,
    # ⚠️ 정책 부재다. *"모든 Zone 금지"* 가 아니라 **먼저 정해야 한다**는 뜻이라
    #    404(없다)로 내지 않는다 — 화면이 그 둘을 다르게 다뤄야 한다.
    ZonePolicyUnresolved,
)

#: 같은 ID 에 다른 사실이 있거나 불변식이 깨진 것.
_CONFLICT_ERRORS = (
    MoveIdConflict,
    MoveLineTotalMismatch,
    OutboundIntegrityError,
    PlacementConflict,
    ReservationConflict,
    WarehouseIntegrityError,
)


@contextmanager
def _domain_errors() -> Iterator[None]:
    """도메인 예외를 HTTP 로 옮긴다. **판정을 다시 하지 않는다.**

    ```text
    모호(둘 이상)        409   ★ LookupError 지만 부재가 아니다 — 먼저 잡는다
    잘못된 업무 요청      400
    무결성 · 사실 충돌    409
    조회 대상 없음        404   repository 의 fixture 0건도 여기다
    그 밖의 ValueError    409   repository 의 fixture 2건 이상이 여기다
    ```

    🔴 **`ValidationError` 는 통과시킨다.** pydantic 의 그것은 `ValueError` 라서
       안 막으면 **우리 쪽 계약 버그가 409 로 위장**된다.
    """
    try:
        yield
    except _AMBIGUOUS_ERRORS as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    except _BAD_REQUEST_ERRORS as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except _CONFLICT_ERRORS as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValidationError:
        raise
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/procurement",
    response_model=LogisticsProcurementResponse,
    summary="Logistics A 날짜별 입고 가능 Band 조회",
)
def review_logistics_procurement(request: PurchaseAgentOutput) -> LogisticsProcurementResponse:
    """Purchase v0.4와 실제 Snapshot으로 Logistics A Reply를 반환한다."""
    return run_logistics_procurement(request)


@router.post(
    "/sales",
    response_model=LogisticsSalesResponse,
    summary="Logistics B 출고 Capacity 및 Lot Constraint 조회",
)
def review_logistics_sales(request: LogisticsSalesRequest) -> LogisticsSalesResponse:
    """H1 승인 매입을 미래 입고로 Overlay한 Logistics B Reply를 반환한다."""
    return run_logistics_sales(request)


@router.get(
    "/runs",
    response_model=list[LogisticsAgentRunResponse],
    summary="Logistics Agent 실행이력 목록 조회",
)
def get_logistics_runs(
    cycle: LogisticsCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[LogisticsAgentRunResponse]:
    """cycle, as_of, runtime_status 필터로 최근 실행이력을 반환한다."""
    return list_logistics_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )


@router.get(
    "/runs/{run_id}",
    response_model=LogisticsAgentRunResponse,
    summary="Logistics Agent 실행이력 단건 조회",
)
def get_logistics_run_by_id(run_id: UUID) -> LogisticsAgentRunResponse:
    """run_id에 해당하는 실행이력을 반환한다."""
    try:
        return get_logistics_run(run_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Logistics Agent run was not found",
        ) from error


# ═══════════════════════════════════════════════════════════════════════════
#  운영 콘솔 Read API
#
#  🔴 위의 Agent 경로(procurement · sales · runs)를 **하나도 건드리지 않는다.**
#     아래는 화면이 부르는 조회 계약이고 저쪽은 Master/Adapter 계약이다.
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/inventory",
    response_model=ConsoleInventoryResponse,
    summary="운영 콘솔 재고 조회 (품목 카드 · Lot · Capacity)",
)
def read_logistics_inventory(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    item_id: Annotated[str | None, Query(min_length=1)] = None,
) -> ConsoleInventoryResponse:
    """품목별 현재고·판매가능량과 Lot 상태, 창고 kg Capacity 를 한 번에 반환한다.

    🔴 만료 Lot(`remaining_freshness_days <= 0`)은 판매가능량에서만 빠지고
       현재고와 `used_capacity_kg` 에는 그대로 남는다.
    """
    with _domain_errors():
        return get_inventory_console(sim_run_id=sim_run_id, as_of=as_of, item_id=item_id)


@router.get(
    "/inventory/moves",
    response_model=ConsoleInventoryMovesResponse,
    summary="운영 콘솔 재고 이동 이력 조회",
)
def read_logistics_inventory_moves(
    sim_run_id: Annotated[str, Query(min_length=1)],
    lot_id: Annotated[str | None, Query(min_length=1)] = None,
    item_id: Annotated[str | None, Query(min_length=1)] = None,
    moved_from: Annotated[date | None, Query(alias="from")] = None,
    moved_to: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConsoleInventoryMovesResponse:
    """저장된 IN · OUT · DISPOSE 원장을 최신순으로 반환한다. **쓰지 않는다.**"""
    with _domain_errors():
        return get_inventory_moves_console(
            sim_run_id=sim_run_id,
            lot_id=lot_id,
            item_id=item_id,
            moved_from=moved_from,
            moved_to=moved_to,
            limit=limit,
        )


@router.get(
    "/inbound",
    response_model=ConsoleInboundResponse,
    summary="운영 콘솔 입고 조회 (운송 중 · Receipt · 도착 자격)",
)
def read_logistics_inbound(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> ConsoleInboundResponse:
    """운송 중 일정과 Receipt·검수·재고반영, 도착 자격 네 갈래 건수를 반환한다.

    ⚠️ `in_transit = null`(미확인)과 `[]`(0건 확인)은 다른 사실이다.
       `in_transit_status` 가 그 둘을 가른다.
    """
    with _domain_errors():
        return get_inbound_console(sim_run_id=sim_run_id, as_of=as_of)


@router.get(
    "/warehouse",
    response_model=ConsoleWarehouseResponse,
    summary="운영 콘솔 창고 조회 (Zone 자리 · Lot 위치)",
)
def read_logistics_warehouse(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
) -> ConsoleWarehouseResponse:
    """Zone 자리 사정과 Lot 물리 위치를 반환한다.

    🔴 Zone Capacity 단위는 **Pallet Position** 이다. kg Capacity 와 섞지 않는다.

    ⚠️ `lot_locations` 는 `pallet_events` 를 `as_of` 까지 재생한 결과이고,
       `zones` 는 지금 창고의 자리 정원이다 — 응답의 `*_time_basis` 가 가른다.
    """
    with _domain_errors():
        return get_warehouse_console(sim_run_id=sim_run_id, as_of=as_of)


@router.get(
    "/warehouse/placement-options",
    response_model=ConsolePlacementOptionsResponse,
    summary="운영 콘솔 Pallet 배치 후보 조회",
)
def read_logistics_placement_options(
    sim_run_id: Annotated[str, Query(min_length=1)],
    lot_id: Annotated[str, Query(min_length=1)],
) -> ConsolePlacementOptionsResponse:
    """이 Lot 을 둘 수 있는 Zone 과 실제 빈 자리를 반환한다.

    🔴 **추천 순위도 자동 선택도 없다.** 고르는 것은 사람이다.
    """
    with _domain_errors():
        return get_placement_options_console(sim_run_id=sim_run_id, lot_id=lot_id)


@router.get(
    "/outbound",
    response_model=ConsoleOutboundResponse,
    summary="운영 콘솔 출고 조회 (Reservation · Allocation)",
)
def read_logistics_outbound(
    sim_run_id: Annotated[str, Query(min_length=1)],
    as_of: date,
    status_filter: Annotated[ReservationStatus | None, Query(alias="status")] = None,
) -> ConsoleOutboundResponse:
    """`as_of` 시점의 예약과 그 아래 할당. 0건이면 `reservations: []` 가 정상이다.

    ★ **예약·할당 축을 `as_of` 로 되살린다** (WP-3 ·
      `reservation_time_basis = HISTORICAL_AS_OF`). 저장된 두 `status` 칸은 지금
      값이라 안 읽고, 판매 납품일 · `released_as_of` · `decided_at` · 원장 OUT 으로
      유도한다.

    ⚠️ **`status` 필터도 유도된 상태에 걸린다.** 지금 DB 값으로 거르면 그날 살아
       있던 예약이 «오늘 놓아줬다» 는 이유로 과거 화면에서 사라진다.
    """
    with _domain_errors():
        return get_outbound_console(
            sim_run_id=sim_run_id, as_of=as_of, status=status_filter
        )


@router.get(
    "/outbound/{reservation_id}/fefo",
    response_model=ConsoleFefoResponse,
    summary="운영 콘솔 FEFO 후보 조회",
)
def read_logistics_reservation_fefo(
    reservation_id: str,
    as_of: date,
) -> ConsoleFefoResponse:
    """이 예약에 쓸 FEFO 후보를 반환한다. 🔴 **추천만 한다 — 할당하지 않는다.**

    실행 축과 품목은 예약 행에서 읽는다. 호출자가 넘기면 남의 실행 Lot 이 붙는다.
    """
    with _domain_errors():
        return get_reservation_fefo_console(reservation_id=reservation_id, as_of=as_of)


@router.get(
    "/transport/quote",
    response_model=ConsoleTransportQuoteResponse,
    summary="운영 콘솔 운송 견적 조회",
)
def read_logistics_transport_quote(
    shipment_qty_kg: Annotated[Decimal, Query(gt=0)],
    logistics_contract_id: Annotated[str | None, Query(min_length=1)] = None,
    body_type: Annotated[str | None, Query(min_length=1)] = None,
) -> ConsoleTransportQuoteResponse:
    """계약 baseline 기반 운송 견적. **재고도 원장도 건드리지 않는다.**

    ⚠️ `standard_minutes` 는 정본이 없어 항상 `null` 이다. ETA 를 지어내지 않는다.
    """
    with _domain_errors():
        return get_transport_quote_console(
            shipment_qty_kg=shipment_qty_kg,
            logistics_contract_id=logistics_contract_id,
            body_type=body_type,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  운영 콘솔 Command API — **넷뿐이다**
#
#  🔴 폐기(`disposal.confirm_disposal`) · Pallet 비우기 · Reservation 생성은
#     이번 범위가 아니다. 도메인 함수는 그대로 두고 API 로 잇지 않는다.
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/warehouse/placements",
    response_model=ConsolePlacementResponse,
    # 🔴 201 로 고정하지 않는다. 도메인이 멱등이라 같은 요청이 다시 오면
    #    `applied=false` 로 **아무것도 만들지 않고** 돌아온다 — 그때 201 Created 는
    #    거짓이다. 만들었는지는 본문 `applied` 가 말한다.
    summary="운영 콘솔 Pallet 배치",
)
def create_logistics_placement(request: ConsolePlacementRequest) -> ConsolePlacementResponse:
    """Lot 을 Pallet 한 장에 올려 자리에 앉힌다. 🔴 **재고는 1g 도 안 움직인다.**

    Zone 허용 · 자리 수 한도 · 멱등 검사는 `warehouse.place_lot_on_pallet` 안에 있다 —
    여기서 복제하지 않는다.
    """
    with _domain_errors():
        return place_lot(
            pallet_id=request.pallet_id,
            sim_run_id=request.sim_run_id,
            lot_id=request.lot_id,
            location_id=request.location_id,
            occurred_at=request.occurred_at,
            recorded_by=request.recorded_by,
            packaging_spec_id=request.packaging_spec_id,
            note=request.note,
        )


@router.post(
    "/outbound/{reservation_id}/allocations",
    response_model=ConsoleAllocateResponse,
    # 🔴 `/warehouse/placements` 와 같은 이유로 201 을 고정하지 않는다 — 멱등
    #    재실행은 `applied=false` 이고 그때 새 할당은 생기지 않는다.
    summary="운영 콘솔 Lot 할당 확정",
)
def create_logistics_allocation(
    reservation_id: str,
    request: Annotated[ConsoleAllocateRequest, Body()],
) -> ConsoleAllocateResponse:
    """사람이 고른 Lot 과 수량으로 할당을 확정한다.

    🔴 `allocation_basis` 에 기본값이 없다 — FEFO 후보를 봤다는 사실과 그것을
       따랐다는 사실은 다르고, 기본값은 묻지도 않고 뒤엣것을 장부에 적는다.

    ⚠️ 원장 OUT 은 여기서 나가지 않는다. 잔량은 실출고 때 움직인다.
    """
    with _domain_errors():
        return allocate_reservation(
            reservation_id=reservation_id,
            requests=request.requests,
            decided_by=request.decided_by,
            decided_at=request.decided_at,
            allocation_basis=request.allocation_basis,
            as_of=request.as_of,
        )


@router.post(
    "/outbound/{reservation_id}/ship",
    response_model=ConsoleShipResponse,
    summary="운영 콘솔 실출고",
)
def ship_logistics_reservation(
    reservation_id: str,
    request: Annotated[ConsoleShipRequest, Body()],
) -> ConsoleShipResponse:
    """할당된 몫을 실제로 내보낸다. 🔴 **여기서만 원장 OUT 이 나가고 잔량이 준다.**"""
    with _domain_errors():
        return ship_reservation(
            reservation_id=reservation_id,
            shipped_at=request.shipped_at,
            sale_item_id=request.sale_item_id,
        )


@router.post(
    "/outbound/{reservation_id}/release",
    response_model=ConsoleReleaseResponse,
    summary="운영 콘솔 Reservation 해제",
)
def release_logistics_reservation(
    reservation_id: str,
    request: Annotated[ConsoleReleaseRequest, Body()],
) -> ConsoleReleaseResponse:
    """잡아 둔 몫을 놓아준다.

    ⚠️ 이미 `SHIPPED` 인 할당이 있으면 도메인이 막는다 — 나간 재고를 예약 취소로
       되돌리지 않는다 (환입은 이 판의 범위가 아니다).
    """
    with _domain_errors():
        return release_reservation_console(
            reservation_id=reservation_id,
            released_as_of=request.released_as_of,
            status=request.status,
        )
