"""Critic 시나리오 하네스 — **테스트 픽스처다. 앱 코드가 아니다.**

★ 이 파일은 `app/master/cycle.py` · `cycle_graph.py` · `cycle_graph_b.py` 에서
  **테스트가 실제로 부르는 것만** 옮겨 온 것이다 (2026-09-08). 그 셋은 지웠다.

```text
전     앱 폴더에 죽은 사이클 3파일이 있고, 앱 진입점에서 도달 0
       테스트가 그것을 받침대로 써서 지우지 못한다
지금   받침대는 픽스처 옆(`fixtures.py` · `fixtures_cycle_b.py` · `run_day_stub.py`)
       앱 폴더에는 산 코드만 남는다
```

🔴 **여기 있는 것을 프로덕션에서 부르지 않는다.** 마스터의 산 경로는
  `app/master/flow.py` 이고, H1 승인 약정의 주인은 `app/master/commitment.py` 다.
  이 파일의 `build_cycle_commitment` 는 **다른 타입**(`ApprovedPurchaseCommitment`)을 만드는
  옛 사이클 것이며, Critic 에게 먹일 시나리오를 짓는 데만 쓴다.

⚠️ **옮기면서 안 부르는 것은 버렸다.** T1·T2·T3-select·Critic·H1·T4 노드와
  `run_daily_cycle`, 사이클 A 의 사전 회송 게이트, Protocol 선언들이 그렇다.
  남은 것은 아래 세 묶음뿐이다.

```text
CycleHooks · run_subcycle · run_day · build_cycle_commitment   (옛 cycle.py)
node_t3_combine                                          (옛 cycle_graph.py)
S1~S3 노드 · node_critic_b · build_cycle_b_hooks         (옛 cycle_graph_b.py)
```

두 사이클은 **같은 형태**를 갖는다.

    제안 → 제약 → 조정 → 검증 → 승인
    A:  T1     T2     T3    Critic   H1     [매입 제안 · 영업·재고·재무 조언]
    B:  S1     S2     S3    Critic   H2     [영업 제안 · 재고·재무 조언]

그래서 러너를 하나로 두고 훅만 바꿔 끼운다. 다만 **사이클을 넘나드는 회송은 없다**
(§3.6.3) — B 에서 FAIL 이 나도 A 로 돌아가지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.contracts.core import (
    AdjustAttempt,
    ApprovedPurchaseCommitment,
    ArrivalLeg,
    Cycle,
    CycleBState,
    CycleLog,
    Dept,
    EndCode,
    PipelineState,
    T0Snapshot,
    compute_has_unmet_obligation,
    is_bankrupt,
    resolve_end_code,
)
from app.master.band import (
    check_occupancy_detailed,
    clip_all,
    combine_band,
    detect_deadlock,
    detect_variant_collapse,
    is_structurally_narrow,
)
from app.master.outbound import (
    clip_allocations,
    combine_outbound_band,
    detect_allocation_collapse,
)

# ---------------------------------------------------------------------------
# 사이클 훅 — A/B 가 갈리는 지점만 주입한다
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleHooks:
    """
    한 사이클의 5단계. 이름만 다르고 자리는 같다.

    propose   : T1 매입 시나리오 2~3안  /  S1 판매 배분 후보 2~4안
    advise    : T2 3부서 병렬          /  S2 2부서 병렬 (재고·재무)
    adjust    : T3 밴드 클리핑·안분     /  S3 공용 자원 결합 검사
    verify    : Critic 사이클 A 검증    /  Critic 사이클 B 검증
    approve   : H1                     /  H2
    """

    cycle: Cycle
    propose: Callable[[PipelineState], PipelineState]
    advise: Callable[[PipelineState], PipelineState]
    adjust: Callable[[PipelineState], PipelineState]
    feedback_gate: Callable[[PipelineState], tuple[PipelineState, bool]]
    verify: Callable[[PipelineState], tuple[PipelineState, bool]]
    approve: Callable[[PipelineState], str | None]
    max_pre_loop: int = 2
    max_post_loop: int = 2

    re_advise: Callable[[PipelineState], PipelineState] | None = None
    """Critic 이 `T2_dept` 로 회송했을 때만 쓰는 **정정** 경로.

    `advise` 와 다르다. `advise` 는 하루 한 번의 제약 회신이고(§3.6.1),
    이건 그 회신이 계약을 어겼을 때(근거 누락·축 침범) 고쳐 받는 경로다(§3.8).
    주입하지 않으면 부서 정정 없이 재조정만 돈다."""


# ---------------------------------------------------------------------------
# 러너
# ---------------------------------------------------------------------------


def run_subcycle(
    snapshot: T0Snapshot,
    hooks: CycleHooks,
    cycle_b_state: CycleBState | None = None,
) -> PipelineState:
    """한 사이클을 끝에서 끝까지 돌린다. 사이클 밖으로 나가지 않는다.

    ★ cycle_b_state 를 **훅이 돌기 전에** 붙인다. 끝난 뒤에 붙이면 S1·S2·S3·검증이
      전부 원본 스냅샷만 보고 돌아 overlay 는 아무도 읽지 않는 장식이 된다 (§3.2.3).
    """
    state = PipelineState(snapshot=snapshot)
    state.cycle_b_state = cycle_b_state
    state.log = CycleLog(
        as_of=snapshot.as_of,
        run_seq=snapshot.run_seq,
        snapshot_id=snapshot.snapshot_id,
        policy_version=snapshot.policy_version,
    )

    # ── T1/S1 제안 → T2/S2 제약 회신 ──────────────────────────────
    state = hooks.propose(state)
    state = hooks.advise(state)

    # ★ advise 는 **루프 밖에서 한 번만** 호출한다 (§3.1 · §3.6.1).
    #   "각 부서는 시나리오와 무관하게 하루에 한 번만 회신한다" (§3.6.1)
    #   회송이 바꾸는 것은 매입 시나리오뿐이고, 밴드는 그대로다.
    #   그래서 되돌림은 T1 만 다시 돈다 (§3.1 "검증 전에 T1으로 되돌린다").
    while True:
        state = hooks.adjust(state)

        if state.deadlock is not None:
            break

        state, retry = hooks.feedback_gate(state)
        if retry:
            state = hooks.propose(state)  # ★ T1 만. T2 회신은 재사용한다
            continue

        state, refail = hooks.verify(state)
        if not refail:
            break

        # ── Critic 회송 — 라우팅에 따라 다시 도는 단계가 다르다 ──
        #   T1_purchase        시나리오 자체가 문제 → T1 재생성
        #   T3_combine         결합이 문제 → adjust 만
        #   T3_rationale_only  숫자는 그대로, 문장만 → adjust 만
        #   T2_dept            부서 회신의 형식·근거 문제 → re_advise (있을 때만)
        route = getattr(state.critic, "route", None)
        if route == "T1_purchase":
            state = hooks.propose(state)
        elif route == "T2_dept" and hooks.re_advise is not None:
            state = hooks.re_advise(state)

    # ── 승인 (H1 / H2) — 오케스트레이터가 올린다 ────────────────
    if state.deadlock is None and state.log.end_code != "E2_HELD":
        side = state.log.side(hooks.cycle)
        side.recommended_id = state.ranked_ids[0] if state.ranked_ids else None
        choice = hooks.approve(state)
        if choice:
            state.approved_scenario_id = choice
            side.approved_id = choice
    return state


# ---------------------------------------------------------------------------
# 하루 전체 — A → B → T4
# ---------------------------------------------------------------------------


@dataclass
class DayResult:
    cycle_a: PipelineState
    cycle_b: PipelineState | None
    end_code: EndCode
    reason: str = ""


def run_day(
    snapshot: T0Snapshot,
    hooks_a: CycleHooks,
    hooks_b: CycleHooks | None = None,
) -> DayResult:
    """
    **B 가 A 뒤에 오는 이유는 순서 의존 때문이 아니다** (§3.1).
    오늘의 판매 후보(on_hand)는 오늘의 매입 결정에 영향받지 않는다 —
    오늘 도착분은 T0 입고 처리에서 이미 반영됐다.
    """
    a = run_subcycle(snapshot, hooks_a)

    # ★ §3.2.3 — 사이클 B 는 T0 Snapshot + H1 Commitment overlay 로 돈다.
    #   스냅샷은 불변이고 Delta 를 겹친다. H1 이 매입 0 또는 반려면
    #   commitment = None 이고 B 는 T0 만으로 돈다.
    commitment = build_cycle_commitment(a)
    b_state = CycleBState(snapshot=snapshot, commitment=commitment)
    b = run_subcycle(snapshot, hooks_b, cycle_b_state=b_state) if hooks_b else None

    a_empty = a.approved_scenario_id is None
    b_empty = b is None or b.approved_scenario_id is None

    # ★ has_unmet_obligation 산출 주체는 S3 다. 부서 플래그를 그대로 받지 않는다.
    fulfilled = _fulfilled_qty(b)
    unmet = compute_has_unmet_obligation(snapshot.confirmed_orders_kg, fulfilled)

    end = resolve_end_code(
        approved=not (a_empty and b_empty),
        base_state_violated=a.log.base_state_violated
        or (b.log.base_state_violated if b else False),
        has_unmet_obligation=unmet,
        both_cycles_empty=a_empty and b_empty,
    )

    reason = ""
    if end == "E5_NO_FEASIBLE_PLAN":
        # ★ E5 는 AI 에게 넘기지 않는다 (§5.0). 원인을 기록하고 사람에게 즉시 올린다.
        reason = (
            "확정 납품 의무가 있으나 조달·판매 어느 쪽으로도 충족할 수 없음. "
            f"A={_why(a)} / B={_why(b)}"
        )
    # ── 하루에 한 행 (유저플로우 §⑧) ────────────────────────
    log = a.log
    if b is not None:
        # ★ B 노드는 자기 사이클의 컬럼군(`log.side("B")` = `.b`)에 직접 쓴다.
        log.b = b.log.b
    log.end_code = end
    log.end_reason = reason
    log.has_unmet_obligation = unmet
    log.end_cycle = "NONE" if end == "E1_APPROVED" else ("A" if a_empty else "B")

    fin = snapshot.finance
    if is_bankrupt(fin.projected_cash_min_krw, fin.minimum_operating_cash_krw):
        # 파산선은 0원이 아니다 — minimum_cash_balance 기준 (§⑥-5)
        log.end_reason = (log.end_reason + " / " if log.end_reason else "") + (
            f"파산선 이탈: projected_cash_min {fin.projected_cash_min_krw:,.0f}원 "
            f"< minimum_cash_balance {fin.minimum_operating_cash_krw:,.0f}원"
        )
    return DayResult(a, b, end, log.end_reason)


def _fulfilled_qty(b) -> dict:
    """
    품목별 납품 충족량. 영업이 낸 `coverable_kg`(사실 보고)를 우선한다.

    승인된 배분의 `qty_by_item` 을 충족량으로 보면, 영업 IO 명세 §5 가
    **"allocation 에 확정 주문분은 포함하지 않는다"**고 정했으므로 확정 납품분이
    통째로 빠져 **매일 미충족으로 나온다.**
    """
    if b is None:
        return {}
    facts = getattr(b, "sales_facts", None)
    if facts is not None and facts.coverable_kg:
        return dict(facts.coverable_kg)

    if b.approved_scenario_id is None:
        return {}
    alloc = next(
        (s for s in b.scenarios if getattr(s, "allocation_id", None) == b.approved_scenario_id),
        None,
    )
    if alloc is None:
        return {}
    # 폴백 — 영업이 사실 보고를 안 낸 경우. 출고분만 센다 (HOLD 제외).
    getter = getattr(alloc, "outbound_qty_by_item", None)
    return dict(getter) if getter is not None else dict(alloc.qty_by_item)


def build_cycle_commitment(a: PipelineState) -> ApprovedPurchaseCommitment | None:
    """H1 승인 결과를 Delta 로 만든다.

    🔴 **프로덕션의 승인 약정 주인은 `app/master/commitment.py` 다.** 이건 옛 사이클이
      만들던 `ApprovedPurchaseCommitment` 로, Critic 에게 먹일 overlay 를 짓는 데만 쓴다.

    N4(inbound_lead_days) · N5(purchase_payment_days) 가 미결이면
    날짜 필드는 None 으로 둔다. 0 으로 채우지 않는다 (§1.2-10).
    """
    if a.approved_scenario_id is None:
        return None
    clip = next((r for r in a.clip_results if r.scenario_id == a.approved_scenario_id), None)
    if clip is None:
        return None

    snap = a.snapshot
    lead = snap.inbound_lead_days
    from datetime import timedelta

    # ★ 승인안의 회차별 도착일을 그대로 약정에 옮긴다 (§3.6.5).
    #   총 리드타임 하나로 계산하면 2회 분할(D+2 / D+5) 안을 승인해도 전량이 D+2 에
    #   도착한 것이 되고, **분할 매입이 창고 부담을 분산시키는 효과가 사라진다.**
    arrivals: list[ArrivalLeg] = []
    for idx, leg in enumerate(clip.clipped_split_plan or (), 1):
        eta = leg.expected_arrival_date
        if eta is None:
            if lead is None:
                arrivals = []  # N4 미결 — 0 으로 대체하지 않는다 (§1.2-10)
                break
            eta = snap.as_of + timedelta(days=leg.offset_days + lead)
        arrivals.append(ArrivalLeg(date=eta, qty_kg=sum(leg.qty_kg.values()), split_index=idx))

    if arrivals:
        # 수량 잔차 보정 — 회차 합이 총량과 어긋나면 계약이 __post_init__ 에서 막는다.
        drift = clip.total_kg - sum(a_.qty_kg for a_ in arrivals)
        if abs(drift) > 1e-9:
            last = arrivals[-1]
            arrivals[-1] = ArrivalLeg(last.date, last.qty_kg + drift, last.split_index)
        first_arrival = min(a_.date for a_ in arrivals)
    else:
        first_arrival = (snap.as_of + timedelta(days=lead)) if lead is not None else None

    return ApprovedPurchaseCommitment(
        approval_id=f"H1-{snap.as_of}-{snap.run_seq}",
        as_of=snap.as_of,
        total_amount_krw=clip.clipped_amount_krw,
        total_qty_kg=clip.total_kg,
        payment_date=None,  # N5 미결 — 0 으로 채우지 않는다
        expected_arrival_date=first_arrival,  # **최초** 도착일
        source_scenario_id=clip.scenario_id,
        ref_ids=(snap.snapshot_id or f"T0-{snap.as_of}",),
        arrival_schedule=tuple(arrivals),
    )


def _why(state: PipelineState | None) -> str:
    if state is None:
        return "미실행"
    if state.deadlock:
        return state.deadlock.code
    if state.log.end_code == "E2_HELD":
        return "보류"
    return "후보 0"


# ---------------------------------------------------------------------------
# 사이클 A · T3 결합 — 옛 cycle_graph.py 에서 이 노드 하나만 쓴다
# ---------------------------------------------------------------------------


def node_t3_combine(state: PipelineState) -> PipelineState:
    """T3-1 결합 → T3-3 교착 판정 → T3-2 클리핑 → T3-4 붕괴 감지. 전부 룰."""
    # ★ 밴드는 **하루에 한 번만** 결합하고 그대로 둔다.
    #   부서 회신이 하루 한 번으로 고정됐으므로 `combine_band` 는 replies 만의
    #   순수 함수이고 회송해도 **같은 값이 나온다.** 매 회차 다시 결합해
    #   `log.a.band` 에 덮어쓰면 "그날의 제약이 무엇이었나"가 마지막 값 하나로
    #   뭉개지고, 회송 이력도 사라진다.
    if state.band is None:
        band = combine_band(state.replies)
        state.band = band
        state.log.a.band = band
    else:
        band = state.band

    # ★ 부서가 돌지 못한 날은 클리핑하지 않는다. 그 부서의 상한이 통째로 빠져
    #   밴드가 실제보다 넓기 때문이다. 조용히 진행하면 **재고가 죽은 날 무제한
    #   매입이 통과한다.** 교착(E3)이 아니라 E4 다 — 실행 환경 문제다.
    if not band.usable:
        state.log.end_code = "E4_NOT_STARTED"
        state.log.note(
            "T3: 부서 미가동 — "
            + ", ".join(
                f"{d}({band.contributors.get(f'not_ready.{d}', '?')})" for d in band.not_ready
            )
            + " · 밴드가 불완전하므로 클리핑하지 않는다"
        )
        return state

    # 교착은 클리핑보다 먼저 본다. 밴드가 비었으면 클리핑에 의미가 없다.
    price = _representative_price(state)
    dl = detect_deadlock(band, price)
    if dl is not None:
        state.deadlock = dl
        state.log.a.deadlock = dl
        state.log.note(f"T3: 교착 {dl.code} — {dl.detail}")
        return state

    state.clip_results = clip_all(state.scenarios, band)
    # ★ 완료된 결과로 계속 덮되(마지막이 정본), 회차 이력은 attempts 에 쌓는다.
    state.log.a.clip_results = tuple(state.clip_results)
    state.log.a.attempts = state.log.a.attempts + (
        AdjustAttempt(
            seq=len(state.log.a.attempts) + 1,
            trigger=(
                "INITIAL"
                if not state.log.a.attempts
                else "POST_CRITIC"
                if state.log.a.post_loop_used
                else "PRE_LOOP"
            ),
            scenario_ids=tuple(r.scenario_id for r in state.clip_results),
            total_kg_by_id={r.scenario_id: round(r.total_kg, 1) for r in state.clip_results},
            binding=tuple(sorted({b for r in state.clip_results for b in r.binding_constraints})),
            reason=(state.feedback.reason_code if state.feedback else ""),
        ),
    )
    state.variant_collapsed = detect_variant_collapse(state.clip_results)
    state.log.a.variant_collapsed = state.variant_collapsed

    # B5 구조적 협소 밴드 · C-4 과도 클리핑 경고
    state.structural_narrow = is_structurally_narrow(band)
    state.log.a.structural_narrow = state.structural_narrow
    state.log.a.over_clipped_ids = tuple(
        r.scenario_id for r in state.clip_results if r.over_clipped
    )
    if state.log.a.over_clipped_ids:
        state.log.note(
            f"T3: C-4 과도 클리핑 경고 (clip_ratio<0.30) — "
            f"{', '.join(state.log.a.over_clipped_ids)} "
            f"※ 드랍하지 않고 경고만 (§1.2-7)"
        )
    bad = [p for r in state.clip_results for p in r.identity_problems]
    if bad:
        state.log.note("T3: ⚠ 삼중 일치 잔차 — " + " / ".join(bad[:3]))

    # ★ 결합 검사 2번: 날짜별 창고 점유 (§3.5.4 · §3.6.7).
    #   여기가 T3(결합) 지점이다. 감사 지점은 Critic 이 따로 돈다.
    for r in state.clip_results:
        occ = check_occupancy_detailed(r, band, state.snapshot)
        if occ.problems:
            state.log.note(f"T3: 창고 점유 초과 {r.scenario_id} — {occ.problems[0]}")
        elif not occ.ran:
            # ★ 빈 결과를 통과로 읽지 않는다. 미검사는 미검사로 기록한다.
            state.log.note(
                f"T3: 창고 점유 미검사 {r.scenario_id} — "
                + (occ.skipped[0] if occ.skipped else "사유 불명")
            )
    state.log.note(
        "T3: "
        + " | ".join(
            f"{r.scenario_id} {sum(r.qty_kg.values()):,.0f}→{r.total_kg:,.0f}kg"
            + (f" [{','.join(r.binding_constraints)}]" if r.binding_constraints else "")
            for r in state.clip_results
        )
    )
    return state


def _representative_price(state: PipelineState) -> dict[str, float]:
    """
    교착 판정용 대표 단가. 시나리오 중 최저 단가를 쓴다 —
    '가장 싸게 사도 floor 를 못 채운다'가 진짜 교착이기 때문이다.
    """
    if not state.scenarios:
        return dict(state.snapshot.spot_price_krw_per_kg)
    out: dict[str, float] = {}
    for s in state.scenarios:
        for i, p in s.unit_price_krw_per_kg.items():
            out[i] = min(out.get(i, float("inf")), float(p))
    return out


# ---------------------------------------------------------------------------
# 사이클 B — S1 → S2 → S3 → 검증 → H2
#
#  ★★ **사이클을 넘나드는 회송은 없다** (§3.8). B 에서 실패해도 A 로 돌아가지
#     않는다. 그래서 여기의 회송 대상은 S1·S3 뿐이다.
#  ★★★ **S1 은 오늘 승인된 매입을 보지 않는다** (§3.5). overlay 는 재고 cap 과
#     재무 시급도를 통해 **S2 에서만** 반영된다.
# ---------------------------------------------------------------------------

MAX_PRE_LOOP_B = 2
MAX_POST_LOOP_B = 2

#: S1. 영업 에이전트 본체 — `(snapshot, retry)` 를 받아 후보를 낸다.
SalesAgent = Callable[[T0Snapshot, bool], Any]
#: S2. 재고 · 재무. overlay(`CycleBState`)를 받는다는 점이 사이클 A 와 다르다.
CycleBDeptAgent = Callable[[T0Snapshot, CycleBState | None, list[Any]], Any]
#: S3-5. 후보 순위. 사이클 A 와 같은 제약 — 숫자를 만들지 않는다.
AllocationSelector = Callable[[PipelineState], list[str]]
CriticBRunner = Callable[[PipelineState, Any], Any]
Approver = Callable[[PipelineState], str | None]


def node_s1_propose(state: PipelineState, agent: SalesAgent) -> PipelineState:
    """
    ★ 넘기는 것은 `snapshot` 뿐이다. `state.cycle_b_state` 를 넘기지 않는다.

      §3.5 표 — 영업의 overlay 범위는 "없음 · 전부 반영하지 않는다"이다.
      오늘 승인분은 리드타임 뒤에 도착하므로 오늘 팔 수 없다.
    """
    retry = state.log.b.pre_loop_used > 0
    result = agent(state.snapshot, retry)

    # ★ 영업은 후보와 함께 **사실 보고**를 낸다 (영업 IO 명세 §5).
    #   (candidates, SalesFacts) 튜플이면 분리하고, 리스트면 후보만 온 것으로 본다.
    if isinstance(result, tuple) and len(result) == 2:
        state.scenarios, state.sales_facts = result
    else:
        state.scenarios = result

    state.log.b.candidate_count = len(state.scenarios)
    if not state.scenarios and state.sales_facts is not None:
        # 후보 0 이 곧 비상은 아니다. 사유를 기록하고 판정은 S3 가 한다 (§5.0).
        state.log.b.single_option_reason = state.sales_facts.no_feasible_reason
        state.log.note(f"S1: 후보 0 — {state.sales_facts.no_feasible_reason}")
    else:
        state.log.note(f"S1: 판매 배분 후보 {len(state.scenarios)}안 생성 (retry={retry})")
    return state


def node_s2_advise(
    state: PipelineState,
    agents: dict[Dept, CycleBDeptAgent],
) -> PipelineState:
    """
    ★ 부서당 1회 (§3.6.1). 후보 수와 무관하게 하루 한 번만 묻는다.

    ★ **여기가 overlay 가 실제로 쓰이는 유일한 지점이다.**
      재고는 승인 매입의 입고 예정을 반영해 날짜별 여유를 갱신하고,
      재무는 승인 유출을 반영해 회수 시급도를 다시 낸다 (§3.5).
    """
    state.replies = {
        dept: fn(state.snapshot, state.cycle_b_state, state.scenarios)
        for dept, fn in agents.items()
    }
    state.log.note(
        "S2: "
        + " / ".join(f"{d}={r.verdict}" for d, r in state.replies.items())
        + (
            " [overlay 적용]"
            if state.cycle_b_state and state.cycle_b_state.commitment
            else " [overlay 없음 — H1 미승인]"
        )
    )
    return state


def node_s3_combine(state: PipelineState) -> PipelineState:
    """S3-1 결합 → S3-3 클리핑 → S3-4 수렴 감지. 전부 룰. (B 에 교착 판정은 없다)"""
    # ★ 사이클 A 와 같은 규약. 밴드는 하루 한 번 결합해 정본으로 둔다.
    if state.outbound_band is None:
        band = combine_outbound_band(state.replies)
        state.outbound_band = band
    else:
        band = state.outbound_band
    # SubcycleLog.band 는 Band(사이클 A 전용) 타입이라 여기에 넣지 않는다.
    # OutboundBand 는 state.outbound_band 로만 흐르고, 로그에는 클리핑 결과가 남는다.

    # ★ 사이클 B 에는 교착 판정이 없다 (outbound.py S3-2 주석 참조).
    #   확정 납품 의무를 못 채우는 것은 교착이 아니라 정상 상태다 —
    #   부족분은 며칠 뒤 도착할 매입으로 채운다. 의무 충족 판정은
    #   run_day 가 compute_has_unmet_obligation() 으로 따로 한다 (§5.0).
    state.clip_results = clip_allocations(state.scenarios, band)
    state.log.b.clip_results = tuple(state.clip_results)
    state.log.b.attempts = state.log.b.attempts + (
        AdjustAttempt(
            seq=len(state.log.b.attempts) + 1,
            trigger=(
                "INITIAL"
                if not state.log.b.attempts
                else "POST_CRITIC"
                if state.log.b.post_loop_used
                else "PRE_LOOP"
            ),
            scenario_ids=tuple(r.scenario_id for r in state.clip_results),
            total_kg_by_id={
                r.scenario_id: round(sum(r.clipped_qty_kg.values()), 1) for r in state.clip_results
            },
            binding=tuple(sorted({b for r in state.clip_results for b in r.binding_constraints})),
        ),
    )
    state.variant_collapsed = detect_allocation_collapse(state.clip_results)
    state.log.b.variant_collapsed = state.variant_collapsed
    if state.variant_collapsed:
        # B 에는 strategy_type 축이 없으므로 붕괴 유형은 항상 QUANTITY 다.
        state.log.b.collapse_type = "QUANTITY"

    for note in band.soft_notes:
        state.log.note(f"S3: {note}")

    state.log.note(
        "S3: "
        + " | ".join(
            f"{r.scenario_id} {sum(r.qty_kg.values()):,.0f}→{sum(r.clipped_qty_kg.values()):,.0f}kg"
            + (f" [{','.join(r.binding_constraints)}]" if r.binding_constraints else "")
            for r in state.clip_results
        )
    )
    return state


def node_s3_feedback_gate(state: PipelineState) -> tuple[PipelineState, bool]:
    """
    사전 회송 (S3 → S1). 검증 **전에** 돌린다.

    ★ 회송 조건이 사이클 A 보다 단순하다.
      A 는 밴드 폭·붕괴·과도 클리핑을 따졌지만, B 의 밴드는 cap 두 개뿐이라
      "전 후보가 실행 불가"인 경우 외에는 재생성해도 같은 상한에 걸린다.
    """
    if state.deadlock is not None:
        return state, False

    live = [r for r in state.clip_results if not r.infeasible]
    if live:
        return state, False

    if state.log.b.pre_loop_used >= MAX_PRE_LOOP_B:
        state.log.note(f"S3: 사전 루프 예산 소진 ({MAX_PRE_LOOP_B}) — 판매 0 으로 진행")
        return state, False

    state.log.b.pre_loop_used += 1
    state.log.note(f"S3→S1 회송 #{state.log.b.pre_loop_used}: 전 후보가 출고 상한 밖")
    return state, True


def node_s3_select(state: PipelineState, selector: AllocationSelector) -> PipelineState:
    """후보 순위. 붕괴가 남아 있으면 대표 1안으로 접는다 (사이클 A 와 같은 규약)."""
    feasible = [r for r in state.clip_results if not r.infeasible]
    if not feasible:
        state.ranked_ids = []
        return state

    if state.variant_collapsed:
        state.ranked_ids = [feasible[0].scenario_id]
        state.log.b.single_option_reason = "후보 수렴 — 대표 1안 + [보류] 2지선다"
        state.log.note("S3: 후보 수렴 — 단일안 + [보류]를 H2 선택지로 제시. LLM 선정 생략")
    else:
        state.ranked_ids = (
            selector(state) if len(feasible) > 1 else [r.scenario_id for r in feasible]
        )
        state.log.b.llm_calls += 1 if len(feasible) > 1 else 0

    state.log.b.candidate_count = len(state.ranked_ids)
    return state


def node_critic_b(state: PipelineState, critic: CriticBRunner | None) -> tuple[PipelineState, bool]:
    """
    반환 두 번째 값이 True 면 회송이 필요하다.

    ★ 회송 대상은 S1 · S3 뿐이다. **A 로 넘어가지 않는다** (§3.8).
    """
    if critic is None or not state.ranked_ids:
        return state, False

    top = next(r for r in state.clip_results if r.scenario_id == state.ranked_ids[0])
    verdict = critic(state, top)
    state.critic = verdict
    state.log.b.critic_verdicts = state.log.b.critic_verdicts + (verdict,)

    if getattr(verdict, "passed", True):
        status = getattr(verdict, "status", "PASS")
        state.log.note(f"Critic(B): {status} ({top.scenario_id})")
        return state, False

    detail = "; ".join(f.detail for f in getattr(verdict, "findings", ()))
    state.log.note(f"Critic(B): FAIL — {detail}")
    if state.log.b.post_loop_used >= MAX_POST_LOOP_B:
        state.log.note("Critic(B) 재조정 예산 소진 — 판매 보류로 안전 종료")
        return state, False
    state.log.b.post_loop_used += 1
    return state, True


def build_cycle_b_hooks(
    *,
    sales_agent: SalesAgent,
    dept_agents: dict[Dept, CycleBDeptAgent],
    selector: AllocationSelector,
    approver: Approver,
    critic: CriticBRunner | None = None,
) -> CycleHooks:
    """
    사이클 B 훅 묶음. `run_day(snapshot, hooks_a, hooks_b)` 에 넘긴다.

    ★ H2 도 오케스트레이터가 올린다 (§3.4 요건 ③).
      영업 에이전트가 사람에게 직접 올라가는 경로는 없다.
    """

    def _adjust(state: PipelineState) -> PipelineState:
        state = node_s3_combine(state)
        if state.deadlock is not None:
            return state
        return node_s3_select(state, selector)

    return CycleHooks(
        cycle="B",
        propose=lambda s: node_s1_propose(s, sales_agent),
        advise=lambda s: node_s2_advise(s, dept_agents),
        adjust=_adjust,
        feedback_gate=node_s3_feedback_gate,
        verify=lambda s: node_critic_b(s, critic),
        approve=approver,
        max_pre_loop=MAX_PRE_LOOP_B,
        max_post_loop=MAX_POST_LOOP_B,
    )
