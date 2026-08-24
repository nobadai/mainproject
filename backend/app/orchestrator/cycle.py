"""
cycle.py — 서브사이클 일반화 (v0.3, 정의서 v0.13 §3.1)

두 사이클은 **같은 형태**를 갖는다.

    제안 → 제약 → 조정 → 검증 → 승인
    A:  T1     T2     T3    Critic   H1     [매입 제안 · 영업·재고·재무 조언]
    B:  S1     S2     S3    Critic   H2     [영업 제안 · 재고·재무 조언]

그래서 러너를 하나로 두고 훅만 바꿔 끼운다. 다만 **사이클을 넘나드는 회송은 없다**
(§3.6.3) — B 에서 FAIL 이 나도 A 로 돌아가지 않는다. 이미 승인·실행된 조달 결정을
되돌리지 않기 때문이다. 이 성질 덕분에 러너가 사이클 안에서 닫힌 루프로 단순해진다.

★ 마스터 요건 (§3.2.1) 이 코드에서 지켜지는 방식
  ① 유일한 결합 지점  — 조정은 T3·S3 에서만. 둘 다 이 모듈이다.
  ② 시작과 끝을 쥔다  — T0 생성·배포 + T4 반영 + cycle_log 쓰기 주인
  ③ 사람 인터페이스   — H1·H2 **둘 다** 오케스트레이터가 올린다.
                        부서 에이전트가 사람에게 직접 올리지 않는다.
                        (v0.11 의 H6 영업→사람 직행 경로가 여기서 해소된다)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.orchestrator.contracts_core import (
    ApprovedPurchaseCommitment,
    ArrivalLeg,
    Cycle,
    CycleBState,
    CycleLog,
    EndCode,
    PipelineState,
    T0Snapshot,
    compute_has_unmet_obligation,
    is_bankrupt,
    resolve_end_code,
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
    """★ v1.2.6 — Critic 이 `T2_dept` 로 회송했을 때만 쓰는 **정정** 경로.

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

    ★ v1.2.1 — cycle_b_state 를 **훅이 돌기 전에** 붙인다.

      v1.2 는 run_subcycle 이 끝난 뒤에 `b.cycle_b_state = b_state` 로 붙였다.
      그래서 S1·S2·S3·검증이 전부 원본 스냅샷만 보고 돌았고, overlay 는
      아무도 읽지 않는 장식이었다 (§3.2.3 미이행).

      특히 재고의 cap_by_date 갱신이 빠지면, **오늘 대량 선매입을 승인한 날에도
      사이클 B 는 창고가 비어 있다고 보고 보유 판단을 한다.**
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

    # ★ v1.2.6 — advise 는 **루프 밖에서 한 번만** 호출한다 (§3.1 · §3.6.1).
    #
    #   "각 부서는 시나리오와 무관하게 하루에 한 번만 회신한다" (§3.6.1)
    #   "T2 제약 회신 — 3개 부서 동시 · **부서당 1회**"           (§3.1)
    #
    #   v1.2.5 까지는 회송할 때마다 advise 를 다시 불렀다. 회송 2회면 부서 호출이
    #   3회가 되어 조항 위반이다. 게다가 **부서 밴드는 스냅샷만의 함수**이므로
    #   (시나리오와 무관하니까) 세 번 물어도 같은 답이 온다 — 순수한 낭비다.
    #
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
        #
        #   ★ T2_dept 는 **밴드 재계산이 아니라 잘못된 회신의 정정**이다.
        #     "하루 한 번"은 부서 의견을 몇 번 묻느냐의 규약이지,
        #     계약 위반 회신을 고칠 기회까지 막는 조항은 아니다 (§3.8).
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
    다만 한 사이클 안에서 사람 승인을 두 번 받는 것보다 순차가
    운영·로그 추적에 단순하므로 A → B 로 고정한다.
    """
    a = run_subcycle(snapshot, hooks_a)

    # ★ v1.1 §3.2.3 — 사이클 B 는 T0 Snapshot + H1 Commitment overlay 로 돈다.
    #   스냅샷은 불변이고 Delta 를 겹친다. DB 재조회가 아니므로 §1.2-9 를 지킨다.
    #   H1 이 매입 0 또는 반려면 commitment = None 이고 B 는 T0 만으로 돈다.
    commitment = build_commitment(a)
    b_state = CycleBState(snapshot=snapshot, commitment=commitment)
    b = run_subcycle(snapshot, hooks_b, cycle_b_state=b_state) if hooks_b else None

    a_empty = a.approved_scenario_id is None
    b_empty = b is None or b.approved_scenario_id is None

    # ★ v1.2 — has_unmet_obligation 산출 주체는 S3(오케스트레이터)다.
    #   부서 플래그를 그대로 받지 않고 여기서 계산한다.
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
        # ★ v1.2.2 — B 노드는 자기 사이클의 컬럼군(`log.side("B")` = `.b`)에 직접 쓴다.
        #   v1.2 는 `b.log.a` 를 가져왔는데, 그건 B 훅도 `.a` 에 쓴다는 가정이었다.
        #   graph_b.py 는 `.b` 에 쓰므로 여기서도 `.b` 를 가져온다.
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
    품목별 납품 충족량.

    ★ v1.2.3 — 영업이 낸 `coverable_kg`(사실 보고)를 우선한다.

      v1.2.2 는 승인된 배분의 `qty_by_item` 을 충족량으로 봤는데, 영업 IO 명세 §5 가
      **"allocation 에 확정 주문분은 포함하지 않는다"**고 정했다. 전략 배분만 세면
      확정 납품분이 통째로 빠져 **매일 미충족으로 나온다.**

      영업은 판정하지 않고 `confirmed_obligation_kg` · `coverable_kg` 라는 사실만
      제출하며, 판정은 여기(S3)가 한다 (§5.0).
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


def build_commitment(a: PipelineState) -> ApprovedPurchaseCommitment | None:
    """
    H1 승인 결과를 Delta 로 만든다. **오케스트레이터만 만든다** —
    부서가 각자 H1 결과를 해석하면 사이클 B 에서 서로 다른 상태를 보게 된다.

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

    # ★ v1.2.1 — 승인안의 회차별 도착일을 그대로 약정에 옮긴다 (§3.6.5).
    #   v1.2 는 clipped_split_plan 을 보지 않고 총 리드타임 하나로 계산해,
    #   2회 분할(D+2 / D+5) 안을 승인해도 전량이 D+2 에 도착한 것이 됐다.
    #   그러면 cap_by_date_overlay 가 최초 도착일부터 전량을 차감하므로
    #   **분할 매입이 창고 부담을 분산시키는 효과가 통째로 사라진다.**
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
        expected_arrival_date=first_arrival,  # v1.2.1 — **최초** 도착일
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
