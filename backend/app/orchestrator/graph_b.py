# ─────────────────────────────────────────────────────────────────────────────
# STATUS: 🔴 LEGACY — 아직 지우지 못한다 (2026-08-26)
#   사이클 B 조립. 판매가 2차 MVP 라 1차 범위 밖이다.
#   ⚠️ `test_critic_v0_4.py` 와 `run_day_stub.py` 가 `build_cycle_b_hooks` 를 쓴다.
# ─────────────────────────────────────────────────────────────────────────────
"""
graph_b.py — 사이클 B 파이프라인 S1→H2 (담당: 이현서)

═══════════════════════════════════════════════════════════════════════
 정의서 §3.1 사이클 B · 판매 결정

     S1  판매 배분 후보 2~4안 생성          [영업 = 제안자]
     S2  제약 회신 — 재고 · 재무 동시        [조언자]
     S3  조정 — 후보 클리핑 · 공용 자원 결합 [조율]
         검증 → 실패 시 S1 또는 S3 으로 회송
     H2  사람 승인

 ★ graph.py(사이클 A)와 같은 노드 규약을 쓴다 —
   `def node(state: PipelineState) -> PipelineState`.
   러너는 cycle.py 의 run_subcycle 하나를 공유한다.

 ★★ **사이클을 넘나드는 회송은 없다** (§3.8).
   B 에서 실패해도 A 로 돌아가지 않는다. 이미 승인·실행된 조달 결정을
   되돌리지 않기 때문이다. 그래서 이 파일의 회송 대상은 S1·S3 뿐이다.

 ★★★ **S1 은 오늘 승인된 매입을 보지 않는다** (§3.5).
   오늘의 판매 후보는 on_hand 뿐이다. overlay(cycle_b_state)는
   재고 cap 과 재무 시급도를 통해 **S2 에서만** 반영된다.
   S1 에 넘기면 아직 오지 않은 물량이 판매 후보가 되어 납기 위반으로 이어진다.
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from app.orchestrator.contracts_core import (
    AdjustAttempt,
    CycleBState,
    Dept,
    PipelineState,
    T0Snapshot,
    T2Reply,
)
from app.orchestrator.cycle import CycleHooks
from app.orchestrator.outbound import (
    clip_allocations,
    combine_outbound_band,
    detect_allocation_collapse,
)

MAX_PRE_LOOP_B = 2
MAX_POST_LOOP_B = 2


# ---------------------------------------------------------------------------
# 외부 의존 — 전부 주입한다
# ---------------------------------------------------------------------------


class SalesAgent(Protocol):
    """S1. 영업 에이전트 본체 — 후보 생성 · 거래처 반응 추정 · 기대 성과."""

    def __call__(self, snapshot: T0Snapshot, retry: bool) -> list[Any]: ...


class CycleBDeptAgent(Protocol):
    """S2. 재고 · 재무. overlay 를 받는다는 점이 사이클 A 와 다르다."""

    def __call__(
        self, snapshot: T0Snapshot, state: CycleBState | None, allocations: list[Any]
    ) -> T2Reply: ...


class AllocationSelector(Protocol):
    """S3-5. 후보 순위. 사이클 A 의 Selector 와 같은 제약 — 숫자를 만들지 않는다."""

    def __call__(self, state: PipelineState) -> list[str]: ...


CriticBRunner = Callable[[PipelineState, Any], Any]
Approver = Callable[[PipelineState], str | None]


# ---------------------------------------------------------------------------
# S1 — 판매 배분 후보 생성
# ---------------------------------------------------------------------------


def node_s1_propose(state: PipelineState, agent: SalesAgent) -> PipelineState:
    """
    ★ 넘기는 것은 `snapshot` 뿐이다. `state.cycle_b_state` 를 넘기지 않는다.

      §3.5 표 — 영업의 overlay 범위는 "없음 · 전부 반영하지 않는다"이다.
      오늘 승인분은 리드타임 뒤에 도착하므로 오늘 팔 수 없다.
    """
    retry = state.log.b.pre_loop_used > 0
    result = agent(state.snapshot, retry)

    # ★ v1.2.3 — 영업은 후보와 함께 **사실 보고**를 낸다 (영업 IO 명세 §5).
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


# ---------------------------------------------------------------------------
# S2 — 제약 회신 (재고 · 재무)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# S3 — 조정
# ---------------------------------------------------------------------------


def node_s3_combine(state: PipelineState) -> PipelineState:
    """S3-1 결합 → S3-3 클리핑 → S3-4 수렴 감지. 전부 룰. (B 에 교착 판정은 없다)"""
    # ★ v1.2.7 — 사이클 A 와 같은 규약. 밴드는 하루 한 번 결합해 정본으로 둔다.
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


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 훅 조립
# ---------------------------------------------------------------------------


def build_cycle_b_hooks(
    *,
    sales_agent: SalesAgent,
    dept_agents: dict[Dept, CycleBDeptAgent],
    selector: AllocationSelector,
    approver: Approver,
    critic: CriticBRunner | None = None,
) -> CycleHooks:
    """
    사이클 B 훅 묶음. `cycle.run_day(snapshot, hooks_a, hooks_b)` 에 넘긴다.

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
