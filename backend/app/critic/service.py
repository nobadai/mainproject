"""Critic A/B 검증 서비스.

★ DB 미접근. 요청 본문만으로 오케 T3 결합·클리핑을 재현한 뒤 6레이어로 검증한다.
  Critic 은 숫자를 바꾸지 않는다 - 판정(status)·발견(findings)·커버리지만 낸다.
"""

from __future__ import annotations

from app.critic.critic_v0_4 import (
    CriticVerdictV04,
    DeptMeta,
    run_critic_b,
    run_critic_v04,
)
from app.critic.llm.judge import JudgeRunner, make_rationale_judge
from app.critic.schemas import (
    ConcernOut,
    CriticProcurementRequest,
    CriticSalesRequest,
    CriticVerdictOut,
    DeptReplyIn,
    FindingOut,
    ScenarioIn,
)
from app.orchestrator.band import clip_all, combine_band
from app.orchestrator.contracts_core import (
    ITEMS,
    ApprovedPurchaseCommitment,
    ArrivalLeg,
    ChannelLeg,
    CheckResult,
    Evidence,
    FinanceSnapshot,
    LotConstraint,
    MinimalAllocation,
    MinimalScenario,
    OutboundLeg,
    SourcingLot,
    SplitLeg,
    T0Snapshot,
    T2Reply,
)
from app.orchestrator.outbound import clip_allocations, combine_outbound_band


def _snapshot(req: CriticProcurementRequest) -> T0Snapshot:
    """Critic 이 읽는 최소 스냅샷. 나머지 필수 필드는 안전한 기본값으로 채운다.

    Critic 이 실제로 읽는 것: as_of · snapshot_id · price_basis · contract_price_basis
    · inbound_lead_days. 그 외 필드는 검증에서 참조하지 않는다.
    """
    zero_finance = FinanceSnapshot(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return T0Snapshot(
        as_of=req.as_of,
        run_seq=req.run_seq,
        forecasts=(),
        spot_price_krw_per_kg=req.spot_price_krw_per_kg or {},
        inventory_available_kg={},
        warehouse_free_kg=0.0,
        confirmed_orders_kg={},
        finance=zero_finance,
        budget_envelope_krw=0.0,
        price_basis=req.price_basis,
        contract_price_basis=req.contract_price_basis,
        snapshot_id=req.snapshot_id or "",
        inbound_lead_days=req.inbound_lead_days,
    )


def _to_evidence(e) -> Evidence:
    return Evidence(
        claim=e.claim,
        source=e.source,
        ref_ids=tuple(e.ref_ids),
        value=e.value,
        unit=e.unit,
        evidence_grade=e.evidence_grade,
        evidence_detail=e.evidence_detail,
    )


def _to_check(chk, dept: str) -> CheckResult:
    return CheckResult(
        check_id=chk.check_id,
        dept=dept,  # type: ignore[arg-type]
        verdict=chk.verdict,
        kind=chk.kind,
        reason=chk.reason,
        evidences=tuple(_to_evidence(e) for e in chk.evidences),
        floor_kg=chk.floor_kg,
        cap_kg=chk.cap_kg,
        cap_total_kg=chk.cap_total_kg,
        cap_amount_krw=chk.cap_amount_krw,
        cap_by_date_kg=chk.cap_by_date_kg,
        allow_loose_cap=chk.allow_loose_cap,
        severity=chk.severity,
    )


def _evidence_resolver(replies: list[DeptReplyIn]):
    """회신이 제출한 evidence 로 {ref_id: value} 를 만든다.

    ★ 소스 DB 재조회 계층이 아니므로 값의 진위는 회신을 신뢰한다. Critic 은 근거의
      구조·바인딩(ref_id 존재, 대조 대상 존재)을 검증한다.
    """
    mapping: dict[str, float] = {}
    for reply in replies:
        for chk in reply.checks:
            for e in chk.evidences:
                for rid in e.ref_ids:
                    mapping.setdefault(rid, e.value)
    return lambda rid: mapping.get(rid)


def _to_reply(reply: DeptReplyIn, as_of) -> T2Reply:
    # ★ Critic 은 회신 as_of 로 스냅샷 바인딩을 대조한다. 요청 as_of 로 맞춘다.
    return T2Reply(
        dept=reply.dept,
        as_of=as_of,
        checks=tuple(_to_check(c, reply.dept) for c in reply.checks),
        reasoning=reply.reasoning,
        item=reply.item,
        runtime_status=reply.runtime_status,
    )


def _group_replies(replies: list[DeptReplyIn], as_of) -> dict:
    grouped: dict[str, list[T2Reply]] = {}
    for reply in replies:
        grouped.setdefault(reply.dept, []).append(_to_reply(reply, as_of))
    return {d: (v[0] if len(v) == 1 else v) for d, v in grouped.items()}


def _single_replies(replies: list[DeptReplyIn], as_of) -> dict:
    """부서당 단일 회신(검증용). 여러 회신이면 마지막을 쓴다."""
    return {r.dept: _to_reply(r, as_of) for r in replies}


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
        price_basis=scenario.price_basis if hasattr(scenario, "price_basis") else "AUCTION",
    )


def _dept_meta(req) -> dict:
    if not req.dept_meta:
        return {}
    return {
        dept: DeptMeta(inputs_used=m.inputs_used, produced_fields=tuple(m.produced_fields))
        for dept, m in req.dept_meta.items()
    }


def _rationale(req: CriticProcurementRequest | CriticSalesRequest) -> str:
    """L5 가 검사할 **결정 근거**.

    ★ 부서 회신(`reasoning`)을 쓰면 안 된다. 부서 문장은 클리핑 **이전**에 작성되므로
      클리핑 후에야 정해지는 binding_constraints 를 언급할 수 없고, 그것을 누락으로
      판정하면 정상 실행마다 E-LOGIC CONCERN 이 붙어 소음이 된다 (실측 확인).

      검사 대상은 오케 selector 가 쓴 문장(`rationale_per_id[선택안]`)이며 요청으로 받는다.
      Critic 은 설명문을 만들지 않는다 — 미제출이면 검사할 것이 없으므로 skipped 다.
    """
    return req.rationale.strip()


def _verdict_out(
    v: CriticVerdictV04,
    cycle: str,
    runtime_status: str,
    judge: JudgeRunner | None = None,
) -> CriticVerdictOut:
    """결정론 판정 + LLM 상태를 합쳐 응답으로 만든다.

    ⚠️ `skipped`(미검사 항목·coverage)와 `llm_status`(LLM 호출 결과)는 다른 것이다.
      L5 가 안 돌면 둘 다 나타난다 — 감추지 않는다 (설계서 §8).
    """
    llm_fields: dict = {}
    if judge is not None and judge.result is not None:
        result = judge.result
        llm_fields = result.model_dump(exclude={"interpretation"})
        llm_fields["interpretation"] = result.interpretation
    elif judge is not None:
        # 러너가 L5 까지 가지 못했다 (앞 레이어 FAIL 등). 호출 자체가 없었다.
        llm_fields = {"llm_status": "SKIPPED_TEMPLATE"}

    return CriticVerdictOut(
        **llm_fields,
        cycle=cycle,
        as_of=v.as_of,
        run_seq=v.run_seq,
        scenario_id=v.scenario_id,
        runtime_status=runtime_status,
        status=v.status,
        badge=v.badge(),
        coverage={k: tuple(val) for k, val in v.coverage.items()},
        coverage_ratio=v.coverage_ratio,
        findings=[
            FindingOut(
                layer=f.layer, check_id=f.check_id, detail=f.detail, dept=f.dept, route=f.route
            )
            for f in v.findings
        ],
        concerns=[
            ConcernOut(code=c.code, detail=c.detail, layer=c.layer, dept=c.dept) for c in v.concerns
        ],
        skipped=list(v.skipped),
        end_stage=v.end_stage,
    )


# ---------------------------------------------------------------------------
# Critic A - 매입 검증
# ---------------------------------------------------------------------------
def run_critic_procurement(req: CriticProcurementRequest) -> CriticVerdictOut:
    snapshot = _snapshot(req)
    items = tuple(req.items) if req.items else ITEMS

    band = combine_band(_group_replies(req.replies, req.as_of), items=items)
    scenarios = [_to_scenario(s) for s in req.scenarios]

    if not band.usable:
        # 부서 미가동 - 클리핑 불가. 검증할 수 있는 것이 없다.
        empty = CriticVerdictV04(
            as_of=req.as_of,
            run_seq=req.run_seq,
            scenario_id=req.target_scenario_id or "",
            status="FAIL",
            findings=(),
            skipped=(f"부서 미가동: {', '.join(band.not_ready)} - 밴드 미형성",),
            end_stage="CRITIC_A",
        )
        return _verdict_out(empty, "A", "RUNTIME_NOT_READY")

    clips = clip_all(scenarios, band)
    scen_by_id = {s.scenario_id: s for s in scenarios}
    clip_by_id = {c.scenario_id: c for c in clips}

    target = req.target_scenario_id
    if target is None:
        feasible = [c for c in clips if not c.infeasible]
        target = (feasible[0] if feasible else clips[0]).scenario_id

    unit_price = req.spot_price_krw_per_kg or {}
    if not unit_price:
        for s in req.scenarios:
            unit_price = {**s.unit_price_krw_per_kg, **unit_price}

    judge = make_rationale_judge(cycle="A")
    verdict = run_critic_v04(
        as_of=req.as_of,
        run_seq=req.run_seq,
        clip=clip_by_id[target],
        band=band,
        snapshot=snapshot,
        scenario=scen_by_id[target],
        replies=_single_replies(req.replies, req.as_of),
        unit_price=unit_price,
        verify_ctx={},
        check_fns={},
        resolve_evidence=_evidence_resolver(req.replies),
        dept_meta=_dept_meta(req),
        all_clips=clips,
        all_scenarios=scen_by_id,
        judge=judge,  # L5 - CONCERN 은 judge 가 붙어야 난다
        rationale=_rationale(req),
        cycle="A",
        unattended=req.unattended,
    )
    return _verdict_out(verdict, "A", "READY", judge)


# ---------------------------------------------------------------------------
# Critic B - 판매 검증 (L4-7~10)
# ---------------------------------------------------------------------------
def _to_allocation(alloc) -> MinimalAllocation:
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


def _commitment(req: CriticSalesRequest) -> ApprovedPurchaseCommitment | None:
    if req.commitment is None:
        return None
    arrivals = tuple(
        ArrivalLeg(date=a.date, qty_kg=a.qty_kg, split_index=a.split_index)
        for a in req.commitment.arrival_schedule
    )
    total = sum(a.qty_kg for a in arrivals)
    return ApprovedPurchaseCommitment(
        approval_id=req.commitment.approval_id,
        as_of=req.as_of,
        total_amount_krw=0.0,
        total_qty_kg=total,
        payment_date=None,
        expected_arrival_date=min(a.date for a in arrivals),
        source_scenario_id="",
        ref_ids=(req.snapshot_id or req.commitment.approval_id,),
        arrival_schedule=arrivals,
    )


def run_critic_sales(req: CriticSalesRequest) -> CriticVerdictOut:
    """S3 를 재현(combine_outbound_band → clip_allocations)한 뒤 대상 배분을 L4-7~10 으로 검증."""
    zero_finance = FinanceSnapshot(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    snapshot = T0Snapshot(
        as_of=req.as_of,
        run_seq=req.run_seq,
        forecasts=(),
        spot_price_krw_per_kg={},
        inventory_available_kg={},
        warehouse_free_kg=req.warehouse_free_kg,
        confirmed_orders_kg={},
        finance=zero_finance,
        budget_envelope_krw=0.0,
        snapshot_id=req.snapshot_id or "",
        confirmed_occupancy_by_date=req.confirmed_occupancy_by_date,
    )
    items = tuple(req.items) if req.items else ITEMS
    replies = _single_replies(req.replies, req.as_of)
    band = combine_outbound_band(replies, items=items)

    allocations = [_to_allocation(a) for a in req.allocations]
    clips = clip_allocations(allocations, band)
    alloc_by_id = {a.allocation_id: a for a in allocations}
    clip_by_id = {c.scenario_id: c for c in clips}

    target = req.target_allocation_id
    if target is None:
        feasible = [c for c in clips if not c.infeasible]
        target = (feasible[0] if feasible else clips[0]).scenario_id

    lots = [
        LotConstraint(
            lot_id=lot.lot_id,
            item=lot.item,
            available_qty_kg=lot.available_qty_kg,
            remaining_freshness_days=lot.remaining_freshness_days,
            status=lot.status,
        )
        for lot in req.lot_constraints
    ]

    judge = make_rationale_judge(cycle="B")
    verdict = run_critic_b(
        as_of=req.as_of,
        run_seq=req.run_seq,
        clip=clip_by_id[target],
        outbound_band=band,
        snapshot=snapshot,
        replies=replies,
        allocation=alloc_by_id.get(target),
        commitment=_commitment(req),
        lot_constraints=lots,
        dept_meta=_dept_meta(req),
        judge=judge,
        rationale=_rationale(req),
    )
    return _verdict_out(verdict, "B", "READY", judge)
