"""⑤ allocate_sourcing — 최소 스텁 (상세설계 §4-⑤, 구현은 Epic 3 / 백로그 E3-1)."""

from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.draft_plan import fixed_market_quotes
from app.purchase_agent.schemas import FIXED_MARKET
from app.purchase_agent.state import PurchaseAgentState


def allocate_sourcing(state: PurchaseAgentState) -> dict[str, Any]:
    """등급 배분. 지금은 **전량 상품** 한 줄이다.

    구현하면 1단계 계산(등급별 단가 × 보관한계 × 납품 일정 매칭 필터)으로 후보를 스코어링하고,
    2단계 LLM이 조합 트레이드오프를 판단한다 — "중품이 싸고 스프레드가 넓지만 6일 내 소진
    필요, 8/24 납품분엔 배정 가능, 8/29분은 상품으로".

    **비율로 둔다.** 안별 총량이 달라 절대 수량은 ⑥이 만든다 — ③의 현금 제약도 같은
    ``REFERENCE_GRADE`` 단가를 쓰므로 두 노드가 같은 등급을 본다. 여기서 다른 등급을 고르면
    ③이 계산한 수량 상한과 ⑦이 검사할 금액이 어긋난다.

    단가는 **당일 시세에 실재하는 값**만 쓴다 (규칙 4) — mock에서 지어내지 않는다.
    """
    reference_grade = load_constraints()["allocation"]["reference_grade"]
    # 가락 시세만 본다 — 다른 시장 가격을 가락으로 표기하면 규칙 4("당일 시세 실재값")가
    # 형식만 통과하고 내용이 거짓이 된다.
    quotes = fixed_market_quotes(state["market_quotes"])
    prices = {quote["grade"]: quote["price"] for quote in quotes}
    grade = reference_grade if reference_grade in prices else max(prices, key=prices.get)
    return {
        "sourcing_plan": [
            {
                "market": FIXED_MARKET,
                "grade": grade,
                "ratio": 1.0,
                "grade_unit_price": prices[grade],
            }
        ]
    }
