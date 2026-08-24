"""
run_day_stub.py — 하루 전체(T0 → 사이클 A → H1 → 사이클 B → H2 → T4) 완주 스크립트

    python -m haetdeul.tests.run_day_stub

★ run_stub.py 는 사이클 A 만 돈다. 이 스크립트가 **처음으로 하루를 끝까지** 돈다.
  v1.2 까지 `run_day()` 는 정의만 돼 있고 호출부가 없었다.

의존성 없음(표준 라이브러리만). LLM 호출 0회 — 선택기는 기여이익 순 정렬 룰이다.
"""

from __future__ import annotations

from fixtures import _std_replies, make_scenarios, make_snapshot
from fixtures_cycle_b import (
    CASES_B,
    finance_reply_b,
    inventory_reply_b,
    make_sales_facts,
)

from app.orchestrator.band import clip_all, combine_band, detect_deadlock
from app.orchestrator.cycle import CycleHooks, run_day
from app.orchestrator.graph_b import build_cycle_b_hooks

BAR = "=" * 78


# ---------------------------------------------------------------------------
# 사이클 A 훅 — 기존 graph.py 노드를 그대로 쓰지 않고 최소 배선으로 대체
#   (A 는 run_stub.py 가 이미 상세히 돌린다. 여기서는 B 로 넘기는 것이 목적이다.)
# ---------------------------------------------------------------------------


def _hooks_a(scenarios, replies):
    def propose(s):
        s.scenarios = scenarios
        return s

    def advise(s):
        s.replies = replies
        return s

    def adjust(s):
        s.band = combine_band(s.replies)
        dl = detect_deadlock(s.band, {i: 1500.0 for i in s.snapshot.confirmed_orders_kg})
        if dl is not None:
            s.deadlock = dl
            s.log.a.deadlock = dl
            return s
        s.clip_results = clip_all(s.scenarios, s.band)
        s.log.a.clip_results = tuple(s.clip_results)
        s.ranked_ids = [r.scenario_id for r in s.clip_results if not r.infeasible]
        s.log.a.candidate_count = len(s.ranked_ids)
        s.log.note(
            "T1~T3: " + " | ".join(f"{r.scenario_id} {r.total_kg:,.0f}kg" for r in s.clip_results)
        )
        return s

    return CycleHooks(
        cycle="A",
        propose=propose,
        advise=advise,
        adjust=adjust,
        feedback_gate=lambda s: (s, False),
        verify=lambda s: (s, False),
        approve=lambda s: s.ranked_ids[0] if s.ranked_ids else None,
    )


# ---------------------------------------------------------------------------
# 사이클 B 훅
# ---------------------------------------------------------------------------


def _hooks_b(allocations, shared_outbound_kg):
    def sales_agent(snapshot, retry):
        # ★ 영업은 후보와 **사실 보고**를 함께 낸다 (영업 IO 명세 §5).
        return allocations, make_sales_facts()

    def inventory_agent(snapshot, state, allocs):
        return inventory_reply_b(shared_outbound_kg=shared_outbound_kg, state=state)

    def finance_agent(snapshot, state, allocs):
        return finance_reply_b(state, snapshot.base_cash_priority)

    def selector(state):
        """기대 기여이익 큰 순. **룰이다 — LLM 아님.**"""
        by_id = {a.allocation_id: a for a in state.scenarios}
        live = [r for r in state.clip_results if not r.infeasible]
        return [
            r.scenario_id
            for r in sorted(
                live,
                key=lambda r: by_id[r.scenario_id].expected_contribution_krw,
                reverse=True,
            )
        ]

    return build_cycle_b_hooks(
        sales_agent=sales_agent,
        dept_agents={"inventory": inventory_agent, "finance": finance_agent},
        selector=selector,
        approver=lambda s: s.ranked_ids[0] if s.ranked_ids else None,
        critic=None,  # Critic B 러너는 P1-4. 여기서는 배선만 확인한다.
    )


# ---------------------------------------------------------------------------


def main() -> None:
    snap = make_snapshot()
    replies_a = _std_replies()
    scenarios_a = make_scenarios()

    for name, case in CASES_B.items():
        print(f"\n{BAR}\n■ {name}  —  기대: {case['expect']}\n{BAR}")

        result = run_day(
            snap,
            _hooks_a(scenarios_a, replies_a),
            _hooks_b(case["allocations"], case["shared_outbound_kg"]),
        )

        for n in result.cycle_a.log.a.notes:
            print(f"    A| {n}")
        if result.cycle_b is not None:
            for n in result.cycle_b.log.a.notes + result.cycle_b.log.b.notes:
                print(f"    B| {n}")

        log = result.cycle_a.log
        commit = result.cycle_b.cycle_b_state.commitment if result.cycle_b else None
        print(f"    → H1 승인 : {result.cycle_a.approved_scenario_id}")
        if commit is not None:
            print(
                f"    → 승인 약정: {commit.total_qty_kg:,.0f}kg / "
                f"{commit.total_amount_krw:,.0f}원 / 도착 {len(commit.arrival_schedule)}회차"
            )
        print(f"    → H2 승인 : {result.cycle_b.approved_scenario_id if result.cycle_b else None}")
        print(
            f"    → end_code={result.end_code}  end_cycle={log.end_cycle}  "
            f"unmet={log.has_unmet_obligation}  llm={log.llm_calls}"
        )
        if result.reason:
            print(f"    → 사유: {result.reason}")


if __name__ == "__main__":
    main()
