"""영업 Agent 계산 실행과 실행이력 저장·조회 흐름.

동결된 요청 입력만으로 계산하고, 실행 중 원본 DB를 다시 조회하지 않는다.
DB는 계산 결과의 실행이력을 저장·조회하는 용도로만 쓴다.

입력·출력은 팀 공통 I/O 계약(캐논)의 구조에 맞춘다. 다만 미구현 결과를 정상 업무 결과처럼
채우지 않는다.
- 계산 가능한 값은 실제값으로 낸다(floor_vector, band, 확정 의무량).
- 값이 미정이면 null로 둔다(0으로 대체하지 않는다).
- 목록 결과가 아직 없으면 빈 배열로 둔다(가짜 후보·경고를 만들지 않는다).
- 검사가 미구현이면 passed=null과 명시적 skip_reason(*_NOT_IMPLEMENTED)으로 표시한다.
- runtime_status는 캐논 출력 본문에 없어 응답에는 넣지 않고 실행이력 DB 컬럼 용도로만 내부 계산한다.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from app.sales.run_repository import (
    get_sales_agent_run,
    list_sales_agent_runs,
    save_sales_agent_run,
)
from app.sales.schemas import (
    FloorVectorEntry,
    HardConstraintResult,
    RuntimeStatus,
    SalesAgentRunResponse,
    SalesAllocationInput,
    SalesAllocationReply,
    SalesBand,
    SalesCycle,
    SalesFloorInput,
    SalesFloorReply,
    SalesProposalMeta,
    SuggestedAdjustment,
)
from app.sales.tools import build_floor_vector, resolve_today_floor


def run_floor_reply(request: SalesFloorInput) -> SalesFloorReply:
    """사이클 A 매입 하한을 계산하고 캐논 T2 회신으로 조립해 실행이력을 저장한다."""
    snapshot = request.sales_snapshot
    floor_vector = build_floor_vector(snapshot)
    today_floor = resolve_today_floor(floor_vector, snapshot.inbound_lead_days, snapshot.as_of)
    binding_delivery_date = _resolve_binding_date(
        floor_vector, snapshot.inbound_lead_days, snapshot.as_of, today_floor
    )
    runtime_status: RuntimeStatus = (
        "READY" if snapshot.inbound_lead_days is not None else "RUNTIME_NOT_READY"
    )

    reply = SalesFloorReply(
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        policy_version=snapshot.policy_version,
        item=snapshot.item,
        # 소프트 경고·제약 판정(SAL-09~10)이 미구현이라 자기 회신 상태를 아직 평가하지 못한다.
        verdict=None,
        band=SalesBand(today_floor_kg=today_floor, binding_delivery_date=binding_delivery_date),
        floor_vector=[
            FloorVectorEntry(date=due_date, kg=kg) for due_date, kg in floor_vector.items()
        ],
        # 하드 제약 3종은 미구현이라 가짜 통과 없이 미구현으로 표시한다.
        hard_constraints=_not_implemented_hard_constraints(),
        # 소프트 경고 판정이 미구현이라 빈 목록으로 둔다.
        soft_warnings=[],
        suggested_adjustment=SuggestedAdjustment(
            axis="quantity",
            action="floor",
            # 미결(None)이면 0으로 대체하지 않는다. 0과 null은 다른 상태다.
            min_qty_kg=today_floor,
        ),
    )
    save_sales_agent_run(
        cycle="PROCUREMENT",
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        runtime_status=runtime_status,
        request_payload=request.model_dump(mode="json"),
        response_payload=reply.model_dump(mode="json"),
    )
    return reply


def run_allocation(request: SalesAllocationInput) -> SalesAllocationReply:
    """사이클 B 판매 배분 제안을 캐논 S1 SalesProposal로 조립해 실행이력을 저장한다.

    후보 생성·충당 가능량 계산(SAL-08~10)이 미구현이라 candidates는 빈 목록,
    coverable_kg는 null로 둔다. 가짜 후보나 사실이 아닌 충당량을 만들지 않는다.
    확정 의무량(confirmed_obligation_kg)만 스냅샷에서 직접 집계한 실제값이다.
    """
    snapshot = request.sales_snapshot
    approval_id = request.approved_purchase.approval_id if request.approved_purchase else None

    # confirmed_orders는 비-nullable이라 항상 리스트다. []는 "확정 주문 없음"이라는 사실이다.
    confirmed_obligation = sum((o.qty_kg for o in snapshot.confirmed_orders), start=Decimal(0))

    reply = SalesAllocationReply(
        meta=SalesProposalMeta(
            as_of=snapshot.as_of,
            item=snapshot.item,
            snapshot_id=snapshot.snapshot_id,
            approval_id=approval_id,
            policy_version=snapshot.policy_version,
            agent_version="v1.3",
        ),
        candidates=[],
        confirmed_obligation_kg=confirmed_obligation,
        # 납기·신선도·예약을 반영한 정확한 충당량 계산이 미구현이라 사실값을 낼 수 없다.
        coverable_kg=None,
        no_feasible_reason="CANDIDATE_GENERATION_NOT_IMPLEMENTED",
    )
    save_sales_agent_run(
        cycle="SALES",
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        # 후보 생성·충당량이 미구현이라 완성된 S1 결과가 아니다. READY로 과장하지 않는다.
        runtime_status="RUNTIME_NOT_READY",
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


def _not_implemented_hard_constraints() -> list[HardConstraintResult]:
    """캐논 T2 하드 제약 3종. 판정 로직(SAL-09)이 미구현이라 모두 미구현으로 표시한다.

    입력이 null이라 건너뛴 상태가 아니라 검사 자체가 아직 없는 상태이므로,
    passed=null과 *_NOT_IMPLEMENTED skip_reason으로 두 상태를 구분한다.
    """
    return [
        HardConstraintResult(
            code=code, basis="BASE", passed=None, skip_reason=f"{code}_CHECK_NOT_IMPLEMENTED"
        )
        for code in ("CONFIRMED_DEMAND_TOTAL", "DELIVERY_DEADLINE", "DAILY_OUTBOUND_CAPACITY")
    ]
