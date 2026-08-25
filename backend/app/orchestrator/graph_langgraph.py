"""LangGraph 배선 — 사이클 A (T1 → T2 → T3 → Critic → H1 → T4).

★ `graph.py` 의 로직은 한 줄도 건드리지 않는다.
  노드가 전부 `def node(state) -> state` 시그니처라 **배선만** 옮기면 된다.
  이 파일은 그래프를 조립할 뿐 판단하지 않는다.

핵심 3가지 (설계 참조본과 동일)
  ① 조건부 엣지 함수는 **전부 순수 코드**다. 라우팅에 LLM 이 개입하지 않는다.
  ② H1 사람 승인은 `interrupt_before` 로 처리한다. 승인 없이 T4 로 갈 수 없다.
  ③ T2 3부서는 팬아웃. 부서당 1회 호출 (§3.1).

상태 채널에 대하여
  `PipelineState` 는 FROZEN 계약의 가변 dataclass 이므로 LangGraph 스키마로 바로 쓸 수 없다
  (병렬 노드가 같은 키를 쓰면 InvalidUpdateError). 계약을 고치지 않고 **한 칸짜리 TypedDict**
  에 담아 넘기고, 리듀서는 같은 객체를 그대로 돌려준다 — 팬아웃 노드들이 `replies` 의
  서로 다른 키만 건드리므로 공유 변이가 안전하다.

  ⚠️ 동기 노드라 LangGraph 는 같은 superstep 안에서도 **순차 실행**한다. 이 팬아웃은
     구조적 분리이지 지연 단축이 아니다. 실제 병렬화는 async 노드 전환이 필요하다.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app.orchestrator import contracts_core
from app.orchestrator import graph as G
from app.orchestrator.contracts_core import Dept, PipelineState, T0Snapshot
from app.orchestrator.llm.selector import make_selector

DEPTS: tuple[Dept, ...] = ("sales", "inventory", "finance")

# 체크포인트에 실리는 계약 dataclass 들을 명시 허용한다.
# 기본(permissive)은 타입마다 경고를 뿌리고 **향후 버전에서 차단**된다 — 미리 못박아 둔다.
# ★ 목록을 손으로 적지 않는다. 계약에 dataclass 가 늘면 조용히 빠져 역직렬화가 dict 로
#   무너지고, 그 증상은 한참 뒤 노드에서 AttributeError 로 나타난다.
_CONTRACT_TYPES: tuple[type, ...] = tuple(
    obj
    for _, obj in inspect.getmembers(contracts_core, inspect.isclass)
    if dataclasses.is_dataclass(obj) and obj.__module__ == contracts_core.__name__
)


def default_checkpointer() -> MemorySaver:
    """계약 객체를 되살릴 수 있는 인메모리 체크포인터.

    ⚠️ 프로세스 메모리에만 남는다. 여러 워커·재시작을 넘겨 H1 승인을 이어받으려면
      영속 체크포인터(예: Postgres)로 교체해야 한다 — 지금은 단일 프로세스 전제다.
    """
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=_CONTRACT_TYPES))


def _keep(_old: PipelineState, new: PipelineState) -> PipelineState:
    """팬아웃 병합 — 노드들이 같은 객체를 변이하므로 마지막 것을 그대로 쓴다."""
    return new


class GraphState(TypedDict):
    """LangGraph 채널. 계약 객체를 그대로 실어 나른다."""

    pipeline: Annotated[PipelineState, _keep]


def build_graph(
    *,
    purchase_agent: G.PurchaseAgent,
    dept_agents: dict[Dept, G.DeptAgent],
    critic: G.CriticRunner,
    executor: G.Executor,
    selector: G.Selector | None = None,
    approver: G.Approver | None = None,
    checkpointer: Any | None = None,
):
    """사이클 A 그래프를 조립한다.

    selector 를 주지 않으면 **LLM selector** 가 기본으로 붙는다 (T3-5, §5.3).
    approver 를 주지 않으면 H1 에서 그래프가 멈춘다 — 사람이 값을 넣고 재개해야 한다.
    """
    llm_selector = selector or make_selector()

    def t1(s: GraphState) -> GraphState:
        return {"pipeline": G.node_t1_purchase(s["pipeline"], purchase_agent)}

    def make_dept_node(dept: Dept):
        """부서 1곳만 회신시킨다 — 부서당 1회 호출 (§3.1)."""

        def node(s: GraphState) -> GraphState:
            state = s["pipeline"]
            agent = dept_agents.get(dept)
            if agent is not None:
                state.replies[dept] = agent(state.snapshot, state.scenarios)
            return {"pipeline": state}

        return node

    def t2_join(s: GraphState) -> GraphState:
        state = s["pipeline"]
        state.log.note("T2: " + " / ".join(f"{d}={r.verdict}" for d, r in state.replies.items()))
        return {"pipeline": state}

    def t3_combine(s: GraphState) -> GraphState:
        return {"pipeline": G.node_t3_combine(s["pipeline"])}

    def t3_select(s: GraphState) -> GraphState:
        return {"pipeline": G.node_t3_select(s["pipeline"], llm_selector)}

    def critic_node(s: GraphState) -> GraphState:
        state, _ = G.node_critic(s["pipeline"], critic)
        return {"pipeline": state}

    def h1(s: GraphState) -> GraphState:
        state = s["pipeline"]
        if approver is None:  # 승인자 미주입 — 보류로 안전 종료 (§4 선택 모드)
            state.log.end_code = state.log.end_code or "E2_HELD"
            return {"pipeline": state}
        return {"pipeline": G.node_h1_approval(state, approver)}

    def t4(s: GraphState) -> GraphState:
        return {"pipeline": G.node_t4_commit(s["pipeline"], executor)}

    def deadlock_exit(s: GraphState) -> GraphState:
        state = s["pipeline"]
        state.log.end_code = "E3_REJECTED"
        state.log.note("교착 — T1 회송으로 풀리지 않아 종료")
        return {"pipeline": state}

    # ── 라우팅: 전부 순수 코드. LLM 개입 없음. ──────────────────
    def route_after_t3(s: GraphState) -> str:
        state = s["pipeline"]
        if state.deadlock is not None:
            return "deadlock_exit"
        _, retry = G.node_t3_feedback_gate(state)
        return "t1_purchase" if retry else "t3_select"

    def route_after_critic(s: GraphState) -> str:
        state = s["pipeline"]
        verdict = state.critic
        if verdict is None or verdict.passed:
            return "h1_approval"
        if state.log.a.post_loop_used >= G.MAX_POST_LOOP:
            return "h1_approval"  # 예산 소진 — 보류로 안전 종료
        return {
            "T1_purchase": "t1_purchase",
            "T2_dept": "t2_sales",
            "T3_combine": "t3_combine",
            # ★ 숫자 불변, 문장만 재작성 — 되돌릴 곳이 없으므로 사람에게 올린다
            "T3_rationale_only": "h1_approval",
        }.get(verdict.route, "h1_approval")

    builder = StateGraph(GraphState)
    builder.add_node("t1_purchase", t1)
    for dept in DEPTS:
        builder.add_node(f"t2_{dept}", make_dept_node(dept))
    builder.add_node("t2_join", t2_join)
    builder.add_node("t3_combine", t3_combine)
    builder.add_node("t3_select", t3_select)
    builder.add_node("critic", critic_node)
    builder.add_node("h1_approval", h1)
    builder.add_node("t4_commit", t4)
    builder.add_node("deadlock_exit", deadlock_exit)

    builder.add_edge(START, "t1_purchase")
    for dept in DEPTS:  # T2 팬아웃 → 조인
        builder.add_edge("t1_purchase", f"t2_{dept}")
        builder.add_edge(f"t2_{dept}", "t2_join")
    builder.add_edge("t2_join", "t3_combine")
    builder.add_conditional_edges(
        "t3_combine", route_after_t3, ["t1_purchase", "t3_select", "deadlock_exit"]
    )
    builder.add_edge("t3_select", "critic")
    builder.add_conditional_edges(
        "critic", route_after_critic, ["t1_purchase", "t2_sales", "t3_combine", "h1_approval"]
    )
    builder.add_edge("h1_approval", "t4_commit")
    builder.add_edge("t4_commit", END)
    builder.add_edge("deadlock_exit", END)

    # ★ H1 은 반드시 사람을 거친다 (§4 선택 모드).
    #   approver 를 준 경우에도 중단점은 남긴다 — 승인 없이 T4 로 갈 수 없다는 구조는 배선이 보장한다.
    return builder.compile(
        checkpointer=checkpointer or default_checkpointer(),
        interrupt_before=["h1_approval"],
    )


def run_cycle_a(
    snapshot: T0Snapshot,
    *,
    purchase_agent: G.PurchaseAgent,
    dept_agents: dict[Dept, G.DeptAgent],
    critic: G.CriticRunner,
    executor: G.Executor,
    selector: G.Selector | None = None,
    approver: G.Approver | None = None,
    thread_id: str = "cycle-a",
) -> PipelineState:
    """H1 중단점까지 돌린 뒤, approver 가 있으면 승인·T4 까지 재개한다.

    approver 가 없으면 H1 직전에서 멈춘 상태를 돌려준다 — 사람이 볼 시점이다.
    """
    app = build_graph(
        purchase_agent=purchase_agent,
        dept_agents=dept_agents,
        critic=critic,
        executor=executor,
        selector=selector,
        approver=approver,
    )
    config = {"configurable": {"thread_id": thread_id}}
    app.invoke({"pipeline": PipelineState(snapshot=snapshot)}, config)

    if approver is not None:  # 사람 승인 자리를 대신 채우고 재개
        app.invoke(None, config)
    return app.get_state(config).values["pipeline"]
