"""LangGraph 배선 검증 — 실제로 그래프를 돌린다.

★ 로직이 아니라 **배선**을 본다: 노드 순서 · 순수 코드 라우팅 · H1 중단점.
  LLM 은 붙이지 않는다(결정론 selector 주입) — 배선 검증에 LLM 이 끼면 원인을 못 가린다.
"""

from datetime import date

import pytest

from app.orchestrator.contracts_core import (
    Band,
    CheckResult,
    ClipResult,
    CriticFinding,
    CriticVerdict,
    FinanceSnapshot,
    MinimalScenario,
    PipelineState,
    T0Snapshot,
    T2Reply,
)
from app.orchestrator.graph_langgraph import build_graph, run_cycle_a


def _snapshot() -> T0Snapshot:
    return T0Snapshot(
        as_of=date(2026, 8, 25),
        run_seq=1,
        forecasts=(),
        spot_price_krw_per_kg={"배추": 1200.0},
        inventory_available_kg={"배추": 0.0},
        warehouse_free_kg=10000.0,
        confirmed_orders_kg={},
        finance=FinanceSnapshot(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        budget_envelope_krw=20_000_000.0,
        price_basis="AUCTION",
        contract_price_basis="AUCTION",
        snapshot_id="SNAP-1",
        inbound_lead_days=1,
    )


def _scenario(sid: str, qty: float) -> MinimalScenario:
    return MinimalScenario(
        scenario_id=sid,
        strategy_type="quantity",
        stance="기준",
        qty_kg={"배추": qty},
        unit_price_krw_per_kg={"배추": 1200.0},
    )


def _purchase_agent(snapshot, feedback):
    del snapshot, feedback
    return [_scenario("SCN-1", 3000.0), _scenario("SCN-2", 6000.0)]


def _dept_agents(calls: list[str]) -> dict:
    def sales(snapshot, scenarios):
        del snapshot, scenarios
        calls.append("sales")
        return T2Reply(
            dept="sales",
            as_of=date(2026, 8, 25),
            checks=(
                CheckResult(
                    check_id="sales.floor",
                    dept="sales",
                    verdict="ok",
                    kind="hard",
                    reason="계약 최소",
                    evidences=(),
                    floor_kg={"배추": 1000.0},
                ),
            ),
            reasoning="계약 물량이 필요합니다.",
            item="배추",
        )

    def inventory(snapshot, scenarios):
        del snapshot, scenarios
        calls.append("inventory")
        return T2Reply(
            dept="inventory",
            as_of=date(2026, 8, 25),
            checks=(
                CheckResult(
                    check_id="inv.cap",
                    dept="inventory",
                    verdict="ok",
                    kind="hard",
                    reason="가용",
                    evidences=(),
                    cap_kg={"배추": 8000.0},
                    cap_total_kg=8000.0,
                ),
            ),
            reasoning="창고 여유가 부족합니다.",
        )

    def finance(snapshot, scenarios):
        del snapshot, scenarios
        calls.append("finance")
        return T2Reply(
            dept="finance",
            as_of=date(2026, 8, 25),
            checks=(
                CheckResult(
                    check_id="fin.cap",
                    dept="finance",
                    verdict="ok",
                    kind="hard",
                    reason="한도",
                    evidences=(),
                    cap_amount_krw=20_000_000.0,
                ),
            ),
            reasoning="지급 일정이 빠듯합니다.",
        )

    return {"sales": sales, "inventory": inventory, "finance": finance}


def _verdict(*, passed: bool, findings=()) -> CriticVerdict:
    """route 는 findings 에서 파생되는 property 다 — 직접 넣을 수 없다."""
    return CriticVerdict(
        as_of=date(2026, 8, 25),
        run_seq=1,
        scenario_id="SCN-1",
        passed=passed,
        findings=findings,
    )


def _passing_critic(state, clip):
    del state, clip
    return _verdict(passed=True)


def _deterministic_selector(state):
    return [r.scenario_id for r in state.clip_results if not r.infeasible]


def _wiring(calls, executed, *, critic=_passing_critic, approver=None):
    return {
        "purchase_agent": _purchase_agent,
        "dept_agents": _dept_agents(calls),
        "critic": critic,
        "executor": lambda state, sid: executed.append(sid),
        "selector": _deterministic_selector,
        "approver": approver,
    }


# --- 배선 -------------------------------------------------------------------
def test_all_three_depts_are_called_once():
    """T2 팬아웃 — 부서당 정확히 1회 (§3.1)."""
    calls, executed = [], []
    run_cycle_a(_snapshot(), **_wiring(calls, executed), thread_id="t-depts")
    assert sorted(calls) == ["finance", "inventory", "sales"]


def test_graph_stops_before_h1_without_approver():
    """★ 승인자가 없으면 H1 직전에서 멈춘다 — 승인 없이 T4 로 갈 수 없다 (§4)."""
    calls, executed = [], []
    app = build_graph(**_wiring(calls, executed))
    config = {"configurable": {"thread_id": "t-interrupt"}}
    app.invoke({"pipeline": PipelineState(snapshot=_snapshot())}, config)

    snapshot = app.get_state(config)
    assert snapshot.next == ("h1_approval",)  # 다음 노드가 H1 에서 대기
    assert executed == []  # T4 는 실행되지 않았다


def test_approval_resumes_and_commits():
    """사람이 승인하면 재개되어 T4 까지 간다."""
    calls, executed = [], []
    state = run_cycle_a(
        _snapshot(),
        **_wiring(calls, executed, approver=lambda s: s.ranked_ids[0]),
        thread_id="t-approve",
    )
    assert state.approved_scenario_id == "SCN-1"
    assert executed == ["SCN-1"]
    assert state.log.end_code == "E1_APPROVED"


def test_hold_choice_does_not_commit():
    """사람이 보류하면 실행 계층으로 넘어가지 않는다."""
    calls, executed = [], []
    state = run_cycle_a(
        _snapshot(),
        **_wiring(calls, executed, approver=lambda s: None),
        thread_id="t-hold",
    )
    assert state.approved_scenario_id is None
    assert executed == []
    assert state.log.end_code == "E2_HELD"


def test_band_and_clipping_survive_the_graph():
    """배선을 바꿔도 Core 결과는 그대로다 — 로직은 건드리지 않았다."""
    calls, executed = [], []
    state = run_cycle_a(
        _snapshot(),
        **_wiring(calls, executed, approver=lambda s: s.ranked_ids[0]),
        thread_id="t-core",
    )
    assert isinstance(state.band, Band)
    assert state.band.cap_total_kg == 8000.0
    assert [r.scenario_id for r in state.clip_results] == ["SCN-1", "SCN-2"]
    assert all(isinstance(r, ClipResult) for r in state.clip_results)


def test_critic_fail_routes_back_and_respects_budget():
    """Critic FAIL 회송은 예산 안에서만 돈다 — 무한 루프가 되지 않는다."""
    calls, executed = [], []

    def failing_critic(state, clip):
        del state, clip
        # L3_band_axis → FAIL_ROUTING 상 T3_combine 회송
        return _verdict(
            passed=False,
            findings=(
                CriticFinding(
                    layer="L3_band_axis",
                    check_id="band.cap.배추",
                    detail="밴드 축 재검산 불일치",
                ),
            ),
        )

    state = run_cycle_a(
        _snapshot(),
        **_wiring(calls, executed, critic=failing_critic, approver=lambda s: None),
        thread_id="t-fail",
    )
    from app.orchestrator.graph import MAX_POST_LOOP

    assert state.log.a.post_loop_used <= MAX_POST_LOOP
    assert executed == []


def test_llm_selector_is_the_default():
    """selector 를 주지 않으면 LLM selector 가 붙는다 (T3-5, §5.3)."""
    import app.orchestrator.graph_langgraph as mod

    made = []
    original = mod.make_selector
    mod.make_selector = lambda: made.append(1) or (lambda s: [])
    try:
        build_graph(
            purchase_agent=_purchase_agent,
            dept_agents=_dept_agents([]),
            critic=_passing_critic,
            executor=lambda state, sid: None,
        )
    finally:
        mod.make_selector = original
    assert made == [1]


@pytest.mark.parametrize("node", ["t1_purchase", "t3_combine", "t3_select", "critic", "t4_commit"])
def test_expected_nodes_exist(node):
    app = build_graph(
        purchase_agent=_purchase_agent,
        dept_agents=_dept_agents([]),
        critic=_passing_critic,
        executor=lambda state, sid: None,
        selector=_deterministic_selector,
    )
    assert node in app.get_graph().nodes
