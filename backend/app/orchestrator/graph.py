# ─────────────────────────────────────────────────────────────────────────────
# STATUS: 🔴 LEGACY — 아직 지우지 못한다 (2026-08-26)
#   T1~T4 노드와 훅 조립. 마스터가 호출 순서를 정하는 구조에서는 자리가 없다.
#   ⚠️ `test_critic_v0_4.py` 가 `node_t3_combine` 을 쓴다. cycle.py 와 함께 3단계에서 제거.
# ─────────────────────────────────────────────────────────────────────────────
"""
graph.py — 일일 파이프라인 T0→T4 (담당: 이현서)

★ 이 파일의 목적은 "끝에서 끝까지 한 바퀴 도는 것"이다 (계약서 §11).
  실제 값은 하나도 필요 없다. stub 이 도는 순간 지만·슬기·채훈님은
  "내 함수가 어디에 꽂히는지"를 눈으로 보게 되고, 그때부터 질문의 질이 달라진다.

노드는 전부 `def node(state: PipelineState) -> PipelineState` 시그니처다.
LangGraph 로 옮길 때 배선만 바꾸면 된다 (graph_langgraph.py 참조).

★★ 노드 시그니처에 session 인자가 없다.
   이것이 계약서 §5.1("오케스트레이터는 원본 DB를 읽지 않는다")의 구현체다.
   T0 수집만 예외이며, 그 결과물인 T0Snapshot 만 state 에 들어온다.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.orchestrator.band import (
    build_feedback,
    check_occupancy_detailed,
    clip_all,
    combine_band,
    detect_deadlock,
    detect_variant_collapse,
    is_structurally_narrow,
)
from app.orchestrator.contracts_core import (
    AdjustAttempt,
    ClipResult,
    CriticVerdict,
    Dept,
    FeedbackHint,
    PipelineState,
    T0Snapshot,
    T2Reply,
)

# 루프 예산 — 백테스트 후 실값 확정 (§10.2-4). 지금은 계약서 §5.3 관행값.
MAX_PRE_LOOP = 2
MAX_POST_LOOP = 2


# ---------------------------------------------------------------------------
# 외부 의존 — 전부 주입한다. stub 이든 실물이든 그래프는 모른다.
# ---------------------------------------------------------------------------


class PurchaseAgent(Protocol):
    def __call__(self, snapshot: T0Snapshot, feedback: FeedbackHint | None) -> list[Any]: ...


class DeptAgent(Protocol):
    def __call__(self, snapshot: T0Snapshot, scenarios: list[Any]) -> T2Reply: ...


class Selector(Protocol):
    """T3-5. 오케스트레이터의 유일한 LLM 지점. 숫자를 출력하지 않는다."""

    def __call__(self, state: PipelineState) -> list[str]: ...


class CriticRunner(Protocol):
    def __call__(self, state: PipelineState, clip: ClipResult) -> CriticVerdict: ...


class Approver(Protocol):
    """H1 사람 승인. LangGraph 에서는 interrupt 로 대체된다."""

    def __call__(self, state: PipelineState) -> str | None: ...


class Executor(Protocol):
    """T4 실행 계층 — 모듈 분리 필수 (§5.4). purchases 쓰기 주인은 여기다."""

    def __call__(self, state: PipelineState, scenario_id: str) -> None: ...


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------


def node_t1_purchase(state: PipelineState, agent: PurchaseAgent) -> PipelineState:
    state.scenarios = agent(state.snapshot, state.feedback)
    state.log.a.candidate_count = len(state.scenarios)
    state.log.note(f"T1: 시나리오 {len(state.scenarios)}안 생성 (feedback={bool(state.feedback)})")
    return state


def node_t2_constraints(state: PipelineState, agents: dict[Dept, DeptAgent]) -> PipelineState:
    """3부서 병렬 · 부서당 1회 호출 (§3.1). 회신은 시나리오 무관이다."""
    for dept, agent in agents.items():
        state.replies[dept] = agent(state.snapshot, state.scenarios)
    state.log.note("T2: " + " / ".join(f"{d}={r.verdict}" for d, r in state.replies.items()))
    return state


def node_t3_combine(state: PipelineState) -> PipelineState:
    """T3-1 결합 → T3-3 교착 판정 → T3-2 클리핑 → T3-4 붕괴 감지. 전부 룰."""
    # ★ v1.2.7 — 밴드는 **하루에 한 번만** 결합하고 그대로 둔다.
    #
    #   v1.2.6 에서 부서 회신이 하루 한 번으로 고정됐으므로, `combine_band` 는
    #   replies 만의 순수 함수이고 회송해도 **같은 값이 나온다.**
    #   그런데도 매 회차 다시 결합해 `log.a.band` 에 덮어쓰면 "그날의 제약이
    #   무엇이었나"가 마지막 값 하나로 뭉개지고, 회송 이력도 사라진다.
    #
    #   결합은 완료 시점의 정본 하나, 회송 이력은 `log.a.attempts` 로 분리한다.
    if state.band is None:
        band = combine_band(state.replies)
        state.band = band
        state.log.a.band = band
    else:
        band = state.band

    # ★ v1.2.4 — 부서가 돌지 못한 날은 클리핑하지 않는다.
    #   그 부서의 상한이 통째로 빠져 밴드가 실제보다 넓기 때문이다.
    #   조용히 진행하면 **재고가 죽은 날 무제한 매입이 통과한다.**
    #   교착(E3)이 아니라 E4 다 — 회사 상태 문제가 아니라 실행 환경 문제다.
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
    # ★ v1.2.7 — 완료된 결과로 계속 덮되(마지막이 정본), 회차 이력은 attempts 에 쌓는다.
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

    # v0.2 — B5 구조적 협소 밴드 · C-4 과도 클리핑 경고
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

    # ★ v1.2.1 — 결합 검사 2번: 날짜별 창고 점유 (§3.5.4 · §3.6.7)
    #   v1.2 는 check_occupancy_by_date() 를 정의만 해 두고 **프로덕션에서 한 번도
    #   호출하지 않았다.** 호출부가 회귀 테스트 한 곳뿐이라, 문서가 선언한
    #   "T1·T3·Critic 3자 공용"이 실제로는 0자였다.
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


def node_t3_feedback_gate(state: PipelineState) -> tuple[PipelineState, bool]:
    """
    사전 feedback 루프 (§3.1). Critic **전에** 매입으로 회송한다.
    반환 두 번째 값이 True 면 T1 으로 되돌린다.
    """
    if state.deadlock is not None:
        return state, False  # 교착은 회송해도 풀리지 않는다

    # ★ v0.2 (B5) — 구조적으로 좁은 밴드는 회송해도 결과가 달라질 수 없다.
    #   재생성해봐야 같은 폭 안에서 만들어야 하므로 T1·T2 를 3배 호출하는 낭비다.
    if state.variant_collapsed and state.structural_narrow:
        state.log.note(
            f"T3: 밴드 폭이 구조적으로 좁음 (집계 여유 {state.band.aggregate_slack_kg:,.0f}kg "
            f"/ Σfloor {state.band.floor_total_kg:,.0f}kg) — 회송 생략, 단일안 폴백으로 직행"
        )
        state.feedback = None
        return state, False

    hint = build_feedback(
        state.clip_results,
        state.band,
        state.variant_collapsed,
        allowed_axes=state.snapshot.allowed_variant_axes,
    )
    if hint is None:
        state.feedback = None
        return state, False

    if state.log.a.pre_loop_used >= MAX_PRE_LOOP:
        state.log.note(f"T3: 사전 루프 예산 소진 ({MAX_PRE_LOOP}) — 현 상태로 진행")
        state.feedback = None
        return state, False

    # 진전 조건 — 예산이 남아도 직전과 같은 결과면 멈춘다
    prev = getattr(state, "_prev_signature", None)
    sig = tuple(sorted((r.scenario_id, round(r.total_kg, 1)) for r in state.clip_results))
    if prev == sig:
        state.log.note("T3: 진전 없음 — 사전 루프 조기 종료")
        state.feedback = None
        return state, False
    state._prev_signature = sig

    state.feedback = hint
    state.log.a.pre_loop_used += 1
    state.log.note(f"T3→T1 회송 #{state.log.a.pre_loop_used}: {hint.reason_code} / {hint.message}")
    return state, True


def node_t3_select(state: PipelineState, selector: Selector) -> PipelineState:
    """
    ★ 오케스트레이터의 유일한 LLM 지점.
      숫자는 T3-2 가 이미 확정했다. LLM 은 **순위만 정하고 사람이 읽을 문장을 쓴다.**
      선택 모드(§4)이므로 최종 선택은 사람이 한다.
    """
    feasible = [r for r in state.clip_results if not r.infeasible]

    if state.variant_collapsed:
        # 사전 루프를 다 쓰고도 붕괴가 남았다. 중복안을 사람에게 올리는 것은 기만이다.
        # 대표 1안으로 접고, H1 은 [실행 / 보류] 2지선다로 제시한다.
        # "이 결정을 오늘 실행할 것인가"는 언제나 진짜 선택이므로 원칙 7 을 만족한다.
        state.ranked_ids = [feasible[0].scenario_id] if feasible else []
        state.log.a.candidate_count = len(state.ranked_ids)
        state.log.note(
            "T3: 시나리오 붕괴 잔존 — 단일안 + [보류]를 H1 선택지로 제시 (원칙 7 유지). "
            "LLM 선정 생략."
        )
        return state

    state.ranked_ids = selector(state) if len(feasible) > 1 else [r.scenario_id for r in feasible]
    state.log.a.llm_calls += 1 if len(feasible) > 1 else 0
    state.log.a.candidate_count = len(state.ranked_ids)
    return state


def node_critic(state: PipelineState, critic: CriticRunner) -> tuple[PipelineState, bool]:
    """반환 두 번째 값이 True 면 FAIL 회송이 필요하다."""
    if not state.ranked_ids:
        return state, False
    top = next(r for r in state.clip_results if r.scenario_id == state.ranked_ids[0])
    verdict = critic(state, top)
    state.critic = verdict
    state.log.a.critic_verdicts = state.log.a.critic_verdicts + (verdict,)
    if verdict.passed:
        state.log.note(f"Critic: PASS ({top.scenario_id})")
        return state, False

    route = verdict.route
    state.log.note(f"Critic: FAIL → {route} :: " + "; ".join(f.detail for f in verdict.findings))
    if state.log.a.post_loop_used >= MAX_POST_LOOP:
        state.log.note("Critic 재조정 예산 소진 — 매입 보류로 안전 종료")
        state.log.end_code = "E2_HELD"
        return state, False
    state.log.a.post_loop_used += 1
    return state, True


def node_h1_approval(state: PipelineState, approver: Approver) -> PipelineState:
    if state.deadlock is not None:
        state.log.end_code = "E3_REJECTED"
        return state
    if state.log.end_code == "E2_HELD":
        return state

    choice = approver(state)
    if choice is None:
        state.log.end_code = "E2_HELD"
        state.log.note("H1: 사람이 보류를 선택")
    else:
        state.approved_scenario_id = choice
        state.log.a.approved_id = choice
        state.log.end_code = "E1_APPROVED"
    return state


def node_t4_commit(state: PipelineState, executor: Executor) -> PipelineState:
    if state.approved_scenario_id:
        executor(state, state.approved_scenario_id)
        state.log.note(f"T4: {state.approved_scenario_id} 실행 계층 전달")
    from datetime import datetime

    # FROZEN 계약의 started_at(datetime.utcnow 팩토리, naive)과 일관되게 naive UTC 사용
    state.log.ended_at = datetime.utcnow()  # noqa: DTZ003
    return state


# ---------------------------------------------------------------------------
# 러너 — LangGraph 도입 전까지 쓰는 순수 파이썬 구동체
# ---------------------------------------------------------------------------


def run_daily_cycle(
    snapshot: T0Snapshot,
    *,
    purchase_agent: PurchaseAgent,
    dept_agents: dict[Dept, DeptAgent],
    selector: Selector,
    critic: CriticRunner,
    approver: Approver,
    executor: Executor,
) -> PipelineState:
    state = PipelineState(snapshot=snapshot)

    while True:
        state = node_t1_purchase(state, purchase_agent)
        state = node_t2_constraints(state, dept_agents)
        state = node_t3_combine(state)

        if state.deadlock is not None:
            break

        state, retry = node_t3_feedback_gate(state)
        if retry:
            continue

        state = node_t3_select(state, selector)
        state, refail = node_critic(state, critic)
        if refail:
            # FAIL 라우팅. T3_rationale_only 는 숫자를 유지한 채 문장만 재작성한다.
            route = state.critic.route if state.critic else None
            if route == "T1_purchase":
                state.feedback = build_feedback(
                    state.clip_results,
                    state.band,
                    state.variant_collapsed,
                    allowed_axes=state.snapshot.allowed_variant_axes,
                )
                continue
            if route in ("T2_dept", "T3_combine"):
                state = node_t2_constraints(state, dept_agents)
                state = node_t3_combine(state)
                if state.deadlock is not None:
                    break
                state = node_t3_select(state, selector)
                state, _ = node_critic(state, critic)
            # T3_rationale_only 는 재선정 없이 그대로 사람에게 올린다 (숫자 불변)
        break

    state = node_h1_approval(state, approver)
    state = node_t4_commit(state, executor)
    return state


# ---------------------------------------------------------------------------
# 보조
# ---------------------------------------------------------------------------


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
