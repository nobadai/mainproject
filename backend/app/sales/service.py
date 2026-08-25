"""영업 Agent 계산 실행과 실행이력 저장·조회 흐름.

동결된 요청 입력만으로 계산하고, 실행 중 원본 DB를 다시 조회하지 않는다.
DB는 계산 결과의 실행이력을 저장·조회하는 용도로만 쓴다.
"""

from datetime import date, timedelta
from uuid import UUID

from app.sales.run_repository import (
    get_sales_agent_run,
    list_sales_agent_runs,
    save_sales_agent_run,
)
from app.sales.schemas import (
    FloorVectorEntry,
    RuntimeStatus,
    SalesAgentRunResponse,
    SalesAllocationInput,
    SalesAllocationReply,
    SalesCycle,
    SalesFloorInput,
    SalesFloorReply,
    StrategicInventoryEntry,
)
from app.sales.tools import (
    build_floor_vector,
    resolve_today_floor,
    strategic_inventory_by_date,
)


def run_floor_reply(request: SalesFloorInput) -> SalesFloorReply:
    """사이클 A 매입 하한을 계산하고 실행이력을 저장한다."""
    floor_vector = build_floor_vector(request)
    today_floor = resolve_today_floor(floor_vector, request.inbound_lead_days, request.as_of)
    binding_delivery_date = _resolve_binding_date(
        floor_vector, request.inbound_lead_days, request.as_of, today_floor
    )
    runtime_status: RuntimeStatus = (
        "READY" if request.inbound_lead_days is not None else "RUNTIME_NOT_READY"
    )

    reply = SalesFloorReply(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        item=request.item,
        runtime_status=runtime_status,
        today_floor_kg=today_floor,
        binding_delivery_date=binding_delivery_date,
        floor_vector=[
            FloorVectorEntry(date=due_date, kg=kg) for due_date, kg in floor_vector.items()
        ],
    )
    save_sales_agent_run(
        cycle="PROCUREMENT",
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        runtime_status=runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=reply.model_dump(mode="json"),
    )
    return reply


def run_allocation(request: SalesAllocationInput) -> SalesAllocationReply:
    """사이클 B 날짜별 전략 판매 가능 재고를 계산하고 실행이력을 저장한다."""
    inventory_by_date = strategic_inventory_by_date(request)

    reply = SalesAllocationReply(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        item=request.item,
        runtime_status="READY",
        strategic_inventory_by_date=[
            StrategicInventoryEntry(date=target, kg=kg) for target, kg in inventory_by_date.items()
        ],
    )
    save_sales_agent_run(
        cycle="SALES",
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        runtime_status="READY",
        request_payload=request.model_dump(mode="json"),
        response_payload=reply.model_dump(mode="json"),
    )
    return reply


def get_sales_run(run_id: UUID) -> SalesAgentRunResponse:
    """실행이력 한 건을 조회한다."""
    return SalesAgentRunResponse.model_validate(get_sales_agent_run(run_id))


def list_sales_runs(
    *,
    cycle: SalesCycle | None = None,
    as_of: date | None = None,
    snapshot_id: str | None = None,
    runtime_status: RuntimeStatus | None = None,
    limit: int = 100,
) -> list[SalesAgentRunResponse]:
    """실행이력 목록을 조회한다."""
    rows = list_sales_agent_runs(
        cycle=cycle,
        as_of=as_of,
        snapshot_id=snapshot_id,
        runtime_status=runtime_status,
        limit=limit,
    )
    return [SalesAgentRunResponse.model_validate(row) for row in rows]


def _resolve_binding_date(floor_vector, inbound_lead_days, as_of, today_floor) -> date | None:
    """오늘 구속 하한을 만든 납기일. 하한이 0이거나 미결이면 None."""
    if inbound_lead_days is None or today_floor is None or today_floor <= 0:
        return None
    window_end = as_of + timedelta(days=inbound_lead_days)
    binding = [
        due_date
        for due_date, kg in floor_vector.items()
        if due_date <= window_end and kg == today_floor
    ]
    return min(binding) if binding else None
