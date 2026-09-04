"""노드 통과 기록 → ``ExecutionMetadata.used_tools`` (M-1 §6 · 전달_2차 §2).

봉투 검증이 **정상 회신인데 쓴 Tool이 없으면** ``E-PLAN-EMPTY``로 잡는다 — 실행 계획이
비면 "어떻게 이 답이 나왔나"를 재현할 수 없기 때문이다 (정의서 §1.2-11).

**노드가 아니라 Tool 이름으로 담는다.** 마스터 Registry에 올린 것은 6종이고 그래프는
7노드라 1:1이 아니다 — ⑥ 조립과 ⑦ 자기검증은 소비자에게 한 덩어리(*"안을 만들고
검증해서 돌려준다"*)라 Tool 하나로 합쳐진다.
"""

from typing import Any

#: 노드 이름 → M-1 §10에 제출한 Tool 이름. ⑥⑦이 한 Tool로 합쳐진다.
NODE_TO_TOOL: dict[str, str] = {
    "classify_situation": "assess_market_situation",
    "collect_context": "collect_market_context",
    "draft_plan": "draft_purchase_quantities",
    "split_plan": "plan_split_purchase",
    "allocate_sourcing": "allocate_grade_mix",
    "package_scenarios": "compose_and_verify_scenarios",
    "self_check": "compose_and_verify_scenarios",
}


class ToolRecorder:
    """그래프가 실제로 지나간 노드를 순서대로 담는다.

    **Registry의 6종 전부가 아니라 그 실행에서 부른 것만** 담는다 (전달_2차 §2).
    ``collect_market_context``가 빠진 날이 *"불확실한 날에만 문서를 읽는다"*를 이력으로
    보여 주는 값이 된다 — 그래프의 조건부 간선이 그 사실을 만든다.

    ⚠️ **④ ``split_plan``·⑤ ``allocate_sourcing``은 미진입·미적용인 날에도 담긴다.**
    두 노드는 조건부 간선이 아니라 무조건 지나며 **판정을 수행하기** 때문이다.
    "판정했는데 미진입"과 "판정 자체를 안 함"은 다른 사실이고, 후자는 ② 하나뿐이다.
    🔴 **``provenance.split.entry_miss_reason``이라는 필드는 없다** (2026-09-04 전수
    확인). 이 줄이 오래 그것을 가리키고 있었는데, 출력 스키마에 ``provenance``라는
    칸 자체가 없다 — 찾으러 간 사람이 못 찾는다.

    실제로는 ``package_scenarios._entry_miss_reason()``이 만든 **문장이 risks 안에
    끼어 나간다** (``package_scenarios.py:1103`` · 2026-09-04 기준 — *"timing 축
    안이지만 분할 미진입(…)으로 일괄"*). 줄 번호는 밀리므로 **함수 이름으로 찾을 것.**

    ⚠️ **enum도 아니다.** 숫자를 품은 자유 문장이라(*"최대안 N kg < 임계 M kg"*)
    그대로는 코드값이 될 수 없다 — ``{code, detail}`` 두 칸으로 갈라야 enum이 된다.
    """

    def __init__(self) -> None:
        self._nodes: list[str] = []

    def record(self, node: str) -> None:
        self._nodes.append(node)

    @property
    def used_tools(self) -> tuple[str, ...]:
        """실행 순서대로, **중복 제거**. ⑥⑦이 같은 Tool이라 이어서 두 번 담긴다."""
        tools: list[str] = []
        for node in self._nodes:
            tool = NODE_TO_TOOL.get(node)
            if tool is not None and tool not in tools:
                tools.append(tool)
        return tuple(tools)

    @property
    def tool_order(self) -> tuple[int, ...]:
        """``used_tools``와 **길이가 같아야 한다** — 다르면 ``ContractViolation``."""
        return tuple(range(1, len(self.used_tools) + 1))


def wrap(node: Any, name: str, recorder: ToolRecorder | None) -> Any:
    """노드를 기록기로 감싼다. ``recorder``가 없으면 **원본을 그대로 돌려준다.**

    기본값이 ``None``인 것이 949건 불변의 근거다 — 어댑터를 거치지 않는 호출은
    감싸는 층 자체를 만나지 않는다.
    """
    if recorder is None:
        return node

    def traced(state: Any) -> Any:
        recorder.record(name)
        return node(state)

    return traced
