"""오케스트레이터 T3(매입)·S3(판매) 결합·클리핑 서비스.

★ DB 미접근 (§5.1). 요청 본문의 부서 회신·후보만으로 순수 계산한다.
  도메인 로직은 band.py / outbound.py 에 있으므로 여기서는 얇게 감싼다.
"""

from __future__ import annotations

import math

from app.orchestrator.band import clip_all, combine_band, detect_deadlock
from app.orchestrator.contracts_core import (
    ITEMS,
    ChannelLeg,
    CheckResult,
    ClipResult,
    MinimalAllocation,
    MinimalScenario,
    OutboundLeg,
    SourcingLot,
    SplitLeg,
    T2Reply,
)
from app.orchestrator.interpretation import enrich_orchestrator_response
from app.orchestrator.outbound import (
    clip_allocations,
    combine_outbound_band,
    detect_allocation_collapse,
)
from app.orchestrator.schemas import (
    AllocationIn,
    BandCheckIn,
    BandOut,
    ClipResultOut,
    DayRequest,
    DayResponse,
    DeadlockOut,
    DeptReplyIn,
    OutboundBandOut,
    ProcurementRequest,
    ProcurementResponse,
    SalesRequest,
    SalesResponse,
    ScenarioIn,
)


def _finite(value: float | None) -> float | None:
    """INF(무한대 = 아무도 상한을 걸지 않음)를 JSON 안전한 None 으로 바꾼다."""
    if value is None or math.isinf(value):
        return None
    return value


# ---------------------------------------------------------------------------
# 요청 → 계약 객체
# ---------------------------------------------------------------------------
def _to_check(chk: BandCheckIn, dept: str) -> CheckResult:
    return CheckResult(
        check_id=chk.check_id,
        dept=dept,  # type: ignore[arg-type]
        verdict=chk.verdict,
        kind=chk.kind,
        reason=chk.reason,
        evidences=(),
        floor_kg=chk.floor_kg,
        cap_kg=chk.cap_kg,
        cap_total_kg=chk.cap_total_kg,
        cap_amount_krw=chk.cap_amount_krw,
        cap_by_date_kg=chk.cap_by_date_kg,
        allow_loose_cap=chk.allow_loose_cap,
        severity=chk.severity,
    )


def _to_reply(reply: DeptReplyIn) -> T2Reply:
    return T2Reply(
        dept=reply.dept,
        as_of=None,  # type: ignore[arg-type]  # 결합은 as_of 를 읽지 않는다
        checks=tuple(_to_check(c, reply.dept) for c in reply.checks),
        reasoning=reply.reasoning,
        item=reply.item,
        runtime_status=reply.runtime_status,
    )


def _group_replies(replies: list[DeptReplyIn]) -> dict:
    """부서별로 묶는다. 한 부서 회신이 여럿(영업 품목별)이면 리스트로 넘긴다."""
    grouped: dict[str, list[T2Reply]] = {}
    for reply in replies:
        grouped.setdefault(reply.dept, []).append(_to_reply(reply))
    return {dept: (items[0] if len(items) == 1 else items) for dept, items in grouped.items()}


def _to_scenario(scenario: ScenarioIn) -> MinimalScenario:
    split_plan = tuple(
        SplitLeg(
            offset_days=leg.offset_days,
            qty_kg=leg.qty_kg,
            expected_arrival_date=leg.expected_arrival_date,
        )
        for leg in scenario.split_plan
    )
    sourcing_plan = tuple(
        SourcingLot(
            item=lot.item,
            grade=lot.grade,
            qty_kg=lot.qty_kg,
            unit_price_krw_per_kg=lot.unit_price_krw_per_kg,
            ref_ids=tuple(lot.ref_ids),
            min_lot_kg=lot.min_lot_kg,
        )
        for lot in scenario.sourcing_plan
    )
    return MinimalScenario(
        scenario_id=scenario.scenario_id,
        strategy_type=scenario.strategy_type,
        stance=scenario.stance,
        qty_kg=scenario.qty_kg,
        unit_price_krw_per_kg=scenario.unit_price_krw_per_kg,
        split_plan=split_plan,
        sourcing_plan=sourcing_plan,
    )


def _to_allocation(alloc: AllocationIn) -> MinimalAllocation:
    legs = tuple(
        ChannelLeg(
            channel=leg.channel,
            item=leg.item,
            qty_kg=leg.qty_kg,
            unit_price_krw_per_kg=leg.unit_price_krw_per_kg,
            lot_ids=tuple(leg.lot_ids),
            due_date=leg.due_date,
        )
        for leg in alloc.legs
    )
    outbound = tuple(OutboundLeg(date=o.date, qty_kg=o.qty_kg) for o in alloc.outbound_by_date)
    return MinimalAllocation(
        allocation_id=alloc.allocation_id,
        strategy_type=alloc.strategy_type,
        legs=legs,
        expected_contribution_krw=alloc.expected_contribution_krw,
        outbound_by_date=outbound,
        estimation_confidence=alloc.estimation_confidence,
    )


# ---------------------------------------------------------------------------
# 계약 객체 → 응답
# ---------------------------------------------------------------------------
def _clip_out(result: ClipResult) -> ClipResultOut:
    return ClipResultOut(
        scenario_id=result.scenario_id,
        clipped_qty_kg=dict(result.clipped_qty_kg),
        total_kg=round(result.total_kg, 3),
        original_total_kg=round(result.original_total_kg, 3),
        clip_ratio=round(result.clip_ratio, 4),
        clipped=result.clipped,
        over_clipped=result.over_clipped,
        binding_constraints=list(result.binding_constraints),
        identity_problems=list(result.identity_problems),
        infeasible=result.infeasible,
        clipped_amount_krw=round(result.clipped_amount_krw, 2),
    )


def _rank(results: list[ClipResult]) -> tuple[list[str], str | None]:
    """실행 가능한 후보를 수량 큰 순으로 세운다.

    ★ 실제 순위는 LLM selector 가 낸다 (§3.5). 여기서는 결정론적 대체 정렬만 제공한다.
    """
    feasible = [r for r in results if not r.infeasible]
    ranked = [r.scenario_id for r in sorted(feasible, key=lambda r: -r.total_kg)]
    return ranked, (ranked[0] if ranked else None)


def _soft_warnings(replies: list[DeptReplyIn]) -> list[str]:
    out: list[str] = []
    for reply in replies:
        for chk in reply.checks:
            if chk.kind == "soft" and chk.verdict != "ok" and chk.reason:
                out.append(f"[{reply.dept}] {chk.reason}")
    return out


# ---------------------------------------------------------------------------
# 엔드포인트 서비스
# ---------------------------------------------------------------------------
def run_procurement(request: ProcurementRequest) -> ProcurementResponse:
    """T3 — 부서 밴드 결합 → 교착 판정 → 후보 클리핑 → 순위."""
    items = tuple(request.items) if request.items else ITEMS
    band = combine_band(_group_replies(request.replies), items=items)

    unit_price = request.spot_price_krw_per_kg or {}
    if not unit_price:
        for scenario in request.scenarios:
            unit_price = {**scenario.unit_price_krw_per_kg, **unit_price}
    deadlock = detect_deadlock(band, unit_price) if band.usable else None

    scenarios = [_to_scenario(s) for s in request.scenarios]
    clips = clip_all(scenarios, band) if band.usable else []
    ranked, recommended = _rank(clips)

    band_out = BandOut(
        floor_kg={i: round(v, 3) for i, v in band.floor_kg.items()},
        cap_kg={i: _finite(v) for i, v in band.cap_kg.items()},
        cap_total_kg=_finite(band.cap_total_kg),
        cap_amount_krw=_finite(band.cap_amount_krw),
        cap_by_date_kg={d: round(v, 3) for d, v in band.cap_by_date_kg.items()},
        contributors=dict(band.contributors),
        not_ready=list(band.not_ready),
        usable=band.usable,
    )
    response = ProcurementResponse(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        runtime_status="RUNTIME_NOT_READY" if band.not_ready else "READY",
        band=band_out,
        deadlock=(
            DeadlockOut(
                code=deadlock.code,
                detail=deadlock.detail,
                item=deadlock.item,
                shortfall=round(deadlock.shortfall, 3),
                unit=deadlock.unit,
                responsible_checks=list(deadlock.responsible_checks),
            )
            if deadlock is not None
            else None
        ),
        clip_results=[_clip_out(r) for r in clips],
        ranked_ids=ranked,
        recommended_id=recommended,
        soft_warnings=_soft_warnings(request.replies),
    )
    # T3-5 — 유일한 LLM 지점. 실패해도 위 결정론 결과가 그대로 남는다.
    return enrich_orchestrator_response(response)


def run_sales(request: SalesRequest) -> SalesResponse:
    """S3 — 공용 출고 밴드 결합 → 후보 클리핑 → 수렴 감지 → 순위."""
    items = tuple(request.items) if request.items else ITEMS
    replies = {reply.dept: _to_reply(reply) for reply in request.replies}
    band = combine_outbound_band(replies, items=items)

    allocations = [_to_allocation(a) for a in request.allocations]
    clips = clip_allocations(allocations, band)
    ranked, recommended = _rank(clips)

    band_out = OutboundBandOut(
        cap_kg={i: _finite(v) for i, v in band.cap_kg.items()},
        cap_total_kg=_finite(band.cap_total_kg),
        cap_total_effective_kg=_finite(band.cap_total_effective_kg),
        contributors=dict(band.contributors),
        soft_notes=list(band.soft_notes),
    )
    response = SalesResponse(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        runtime_status="READY",
        outbound_band=band_out,
        clip_results=[_clip_out(r) for r in clips],
        ranked_ids=ranked,
        recommended_id=recommended,
        variant_collapsed=detect_allocation_collapse(clips),
        soft_warnings=_soft_warnings(request.replies),
    )
    # S3 선정 — 매입과 같은 SelectionService 를 통과한다.
    return enrich_orchestrator_response(response)


def run_day(request: DayRequest) -> DayResponse:
    """하루 전체 — 매입 코어 → 판매 코어. end_code 는 코어 기준 간이 판정."""
    proc = run_procurement(request.procurement)
    sales = run_sales(request.sales) if request.sales is not None else None

    if not proc.band.usable:
        end_code = "E4_NOT_STARTED"
        reason = f"부서 미가동: {', '.join(proc.band.not_ready)}"
    elif proc.deadlock is not None:
        end_code = "E3_REJECTED"
        reason = proc.deadlock.detail
    elif not proc.ranked_ids and (sales is None or not sales.ranked_ids):
        end_code = "E2_HELD"
        reason = "실행 가능한 매입·판매 후보 없음"
    else:
        end_code = "E1_APPROVED"
        reason = ""

    return DayResponse(
        as_of=request.procurement.as_of,
        end_code=end_code,
        reason=reason,
        procurement=proc,
        sales=sales,
    )
