"""④ split_plan — 통과 스텁 (상세설계 §4-④, 구현은 Epic 3 / 백로그 E3-2)."""

from typing import Any

from app.purchase_agent.state import PurchaseAgentState


def split_plan(state: PurchaseAgentState) -> dict[str, Any]:
    """분할 유형을 고른다. 지금은 항상 **일괄**(분할 없음)이다.

    §4-④가 "스텁 단계에서는 split_plan을 단일 원소로 시작(분할 없음)"이라고 명시한 그대로다.
    구현하면 고정 목록 {일괄, 2분할, 3분할}에서 **선택**하고(생성 아님) 회차별 수량·날짜를
    배분한다. 트레이드오프는 "상승장 분할 = 평균단가 손해 vs 로트 나이 분산 = 폐기리스크 감소".

    ``None``을 돌려주는 이유: 여기서 절대 수량을 정할 수 없다. 안별 총량이 다르므로(보수 2,571
    ~ 공격 8,727kg) 회차 수량은 ⑥이 안별로 materialize한다. 이 노드가 소유하는 건 **유형**이고,
    그 층위는 IO명세 feedback의 ``keep: ["sourcing_ratio", "split_type"]``과 같다.
    """
    return {"split_plan": None}
