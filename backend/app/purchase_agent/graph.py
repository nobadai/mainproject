"""7노드 LangGraph 그래프 (상세설계 §4).

```
① classify_situation ─ stable ────────────────┐
                     └ uncertain → ② collect_context
                                              ▼
                                       ③ draft_plan
                                              ▼
                                       ④ split_plan
                                              ▼
                                     ⑤ allocate_sourcing
                                              ▼
                                    ⑥ package_scenarios
                                              ▼
                                       ⑦ self_check → END
```

조건부 분기가 하나 있다 — stable한 날은 ② 문서 루프를 건너뛴다. "문서를 읽을지 말지부터가
판단"이라 원라인 파이프라인이 아니라 그래프인 것이다 (§4).

**④는 구현 후에도 조건부 간선이 아니라 무조건 지나는 노드다** (E3-3). 진입 판정을 간선으로
빼면 미진입 시 ④가 아예 안 돌아 "왜 분할이 없는가"라는 사실이 ⑥에 도달하지 못한다.
④가 항상 돌면서 판정 근거를 실어 보내고, ⑥이 그걸 risks에 고지한다 — timing 라벨인데
회차가 하나인 상태를 소비자가 추적할 수 있어야 한다.
"""

from datetime import date
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.purchase_agent.llm.mix import MixSelector, make_mix_selector
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.collect_context import collect_context
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import package_scenarios
from app.purchase_agent.nodes.self_check import self_check
from app.purchase_agent.nodes.split_plan import split_plan
from app.purchase_agent.state import PurchaseAgentState, build_initial_state

#: 노드 이름 → 함수. 순서가 §4 그래프의 ①~⑦과 같다.
#: ⑤만 부분 적용으로 감싼다 — LLM 선택자는 **그래프가 조립할 때 한 번** 만들어 주입한다
#: (E3-2). 노드가 스스로 서비스를 만들면 테스트가 실 API를 타게 되고, 주입 지점도 사라진다.
NODES = {
    "classify_situation": classify_situation,
    "collect_context": collect_context,
    "draft_plan": draft_plan,
    "split_plan": split_plan,
    "allocate_sourcing": allocate_sourcing,
    "package_scenarios": package_scenarios,
    "self_check": self_check,
}


def route_after_classify(state: PurchaseAgentState) -> Literal["collect_context", "draft_plan"]:
    """uncertain일 때만 ② 문서 루프로 간다 (§4-②: "stable한 날은 이 노드를 건너뛴다")."""
    return "collect_context" if state["situation"] == "uncertain" else "draft_plan"


def build_graph(*, selector: MixSelector | None = None) -> Any:
    """7노드를 배선해 컴파일한다 (백로그 E2-1 DoD: "컴파일·통과 실행").

    ``selector``는 ⑤의 등급 조합 판단자다 (E3-2). ``None``이면 여기서 만든다 —
    설정이 LLM을 껐거나 키·서버가 없으면 그 선택자가 규칙 기본안을 돌려주므로,
    **팀원이 브랜치만 받아도 산출물이 그대로 나온다**. 테스트는 가짜 선택자를 꽂는다.
    """
    mix_selector = selector or make_mix_selector()
    builder = StateGraph(PurchaseAgentState)
    for name, node in NODES.items():
        if name == "allocate_sourcing":
            builder.add_node(
                name, lambda state: allocate_sourcing(state, selector=mix_selector)
            )
            continue
        builder.add_node(name, node)

    builder.add_edge(START, "classify_situation")
    builder.add_conditional_edges(
        "classify_situation",
        route_after_classify,
        {"collect_context": "collect_context", "draft_plan": "draft_plan"},
    )
    builder.add_edge("collect_context", "draft_plan")
    builder.add_edge("draft_plan", "split_plan")
    builder.add_edge("split_plan", "allocate_sourcing")
    builder.add_edge("allocate_sourcing", "package_scenarios")
    builder.add_edge("package_scenarios", "self_check")
    builder.add_edge("self_check", END)
    return builder.compile()


def run_purchase_agent(
    item: str,
    as_of: date,
    *,
    feedback: dict | None = None,
    selector: MixSelector | None = None,
) -> dict:
    """품목 하나에 대해 그래프를 한 번 돌리고 제안 JSON을 돌려준다.

    **read-only다** (규칙 2) — DB에 아무것도 쓰지 않고 반환이 전부다.
    **as_of는 주입받는다** (규칙 1) — 벽시계를 읽지 않으므로 과거 날짜로도 그대로 돈다.

    T1은 품목별로 이 그래프를 돌린 뒤 전사 시나리오로 조합한다(§4). 조합은 아직 범위 밖이다.
    """
    final_state = build_graph(selector=selector).invoke(
        build_initial_state(item, as_of, feedback=feedback)
    )
    return final_state["proposal"]
