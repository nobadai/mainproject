"""③ draft_plan — 커버일수 D 기반 수량 초안 (상세설계 §4-③).

수량은 전부 계산이 소유한다 (규칙 6). LLM 몫은 "하드 제약 안에서 어떤 조합이 나은가"라는
트레이드오프 판단이며 Epic 3에서 붙는다 — 그때도 아래 클립 결과를 **입력**으로 받는다.
"""

from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes._guards import require_non_empty, require_positive
from app.purchase_agent.nodes.classify_situation import estimate_daily_demand
from app.purchase_agent.schemas import FIXED_MARKET
from app.purchase_agent.state import PurchaseAgentState


def fixed_market_quotes(market_quotes: list[dict]) -> list[dict]:
    """가락 시세만 남긴다.

    ``market``을 버리고 등급·가격만 보면 **다른 시장의 가격을 가락 가격으로 둔갑**시킬 수
    있다. 지금 mock은 전부 가락이라 결과가 같지만, 필터가 없으면 그 사실에 기대는 코드가 된다.
    """
    return require_non_empty(
        [quote for quote in market_quotes if quote["market"] == FIXED_MARKET],
        f"market_quotes[{FIXED_MARKET}]",
    )


def reference_unit_price(market_quotes: list[dict], reference_grade: str) -> int:
    """기준 등급의 당일 가락 시세. 없으면 가장 비싼 등급으로 보수적으로 잡는다.

    ⑤도 이 등급을 배분의 기준으로 삼는다 — 등급을 constraints에서 한 번만 읽어 양쪽에
    넘긴다 (규칙 7). ⑤가 더 싼 중품을 섞으면 실제 매입단가는 이 값보다 낮아지므로, 여기서
    낸 현금 상한은 **보수적인 쪽으로만** 어긋난다. 반대로 이 등급을 ⑤보다 싸게 잡으면
    ③이 살 수 있다고 계산한 양을 ⑦의 금액 검사가 컷하게 된다.
    """
    prices = {quote["grade"]: quote["price"] for quote in fixed_market_quotes(market_quotes)}
    chosen = prices.get(reference_grade, max(prices.values()))
    return require_positive(chosen, "reference_unit_price")


def warehouse_cap_kg(inventory: dict) -> int:
    """창고 여유 + 외부임차 한도. 상세설계 §4-⑦의 수량 하드 상한이다.

    ⚠️ §4-⑦은 이 검사를 ``check_warehouse_capacity()`` **공용 모듈**로 두고 매입·T3·Critic이
    import하라고 규정한다("자체 구현 금지 — 매입 통과, T3 FAIL 반복 방지"). 그 모듈이 아직
    없어서 지금은 여기 있다. 생기면 이 함수를 지우고 import로 바꾼다.
    """
    return inventory["warehouse_free_kg"] + inventory["rental_cap_kg"]


def cash_cap_kg(projected_cash_min: int, unit_price: int, constraints: dict) -> int:
    """현금 상한을 수량으로 환산. ``projected_cash_min × 비율 ÷ 단가``."""
    budget = projected_cash_min * constraints["cash"]["max_purchase_ratio"]
    return int(budget // require_positive(unit_price, "unit_price"))


def _freshness_cap_kg(
    state: PurchaseAgentState, daily_demand: float, constraints: dict
) -> int | None:
    """보관한계 안에 소진 가능한 양. 품목 보관한계가 미확정이면 **None**을 돌려준다.

    None은 "제약 없음"이 아니라 **계산을 하지 않았다**는 뜻이다 (규칙 3). 0으로 채우면
    매입량이 0으로 눌리고, 큰 수로 채우면 검사가 있었던 것처럼 보인다 — 둘 다 거짓이다.
    호출자는 None을 받으면 클립하지 않고 그 사실을 risks에 남긴다.
    """
    shelf_life_days = constraints["shelf_life_days"].get(state["item"])
    if shelf_life_days is None:
        return None
    return int(daily_demand * shelf_life_days)


def draft_plan(state: PurchaseAgentState) -> dict[str, Any]:
    """안별 수량 초안을 만든다.

    ``수량 = 일평균 확정수요 × 커버일수 D`` 를 계산하고 하드 제약으로 클립한다.
    uncertain이면 공격(D=12)을 아예 만들지 않는다 (§4-③ · 규칙 4).
    """
    constraints = load_constraints()
    coverage = constraints["coverage_days"]
    daily_demand = estimate_daily_demand(state["confirmed_orders"], constraints)
    reference_grade = constraints["allocation"]["reference_grade"]
    unit_price = reference_unit_price(state["market_quotes"], reference_grade)

    warehouse_cap = warehouse_cap_kg(state["inventory"])
    cash_cap = cash_cap_kg(state["projected_cash_min"], unit_price, constraints)
    freshness_cap = _freshness_cap_kg(state, daily_demand, constraints)

    drafts = []
    for label, days in coverage["by_label"].items():
        if state["situation"] == "uncertain" and label == "공격":
            continue  # 구간이 넓은 날엔 공격안을 만들지 않는다
        drafts.append(
            _draft_one(
                label=label,
                days=days,
                daily_demand=daily_demand,
                caps={"창고": warehouse_cap, "현금": cash_cap, "신선도": freshness_cap},
                coverage=coverage,
            )
        )

    return {
        "coverage_days": coverage["by_label"]["기본"],  # §3 State는 대표 D 하나를 담는다
        "base_plan": {
            "daily_demand_kg": daily_demand,
            "reference_unit_price": unit_price,
            "drafts": drafts,
            "deferred_checks": _deferred_checks(constraints, freshness_cap, state["item"]),
        },
    }


def _draft_one(
    *, label: str, days: int, daily_demand: float, caps: dict, coverage: dict
) -> dict[str, Any]:
    """안 하나. 클립이 걸리면 어느 제약이 몇 kg으로 눌렀는지 남긴다."""
    if not coverage["min"] <= days <= coverage["max"]:
        span = f"[{coverage['min']}, {coverage['max']}]"
        raise ValueError(f"coverage_days {days} for {label!r} is outside {span}")

    raw_qty = round(daily_demand * days)
    binding = [(name, cap) for name, cap in caps.items() if cap is not None and cap < raw_qty]
    total_qty = min([raw_qty, *(cap for _, cap in binding)])
    return {
        "label": label,
        "coverage_days": days,
        "raw_qty_kg": raw_qty,
        "total_qty_kg": total_qty,
        "clipped_by": [
            {"constraint": name, "cap_kg": cap, "raw_qty_kg": raw_qty} for name, cap in binding
        ],
    }


def _deferred_checks(constraints: dict, freshness_cap: int | None, item: str) -> list[str]:
    """미결값 때문에 **계산하지 않은** 검사들. ⑥이 안별 risks에 싣는다.

    ``rejected_reasons``가 아니라 risks로 가는 이유: 소비자는 rejected_reasons를 "컷된 안의
    이력"으로 읽는다. "검사를 건너뛰었다"는 다른 의미라 그 필드에 섞으면 계약이 오염된다.
    """
    deferred = []
    if constraints["pending"]["inbound_lead_days"] is None:
        deferred.append(
            "입고일 기준 창고 점유 검사 보류 — inbound_lead_days(N4) 미확정이라 "
            "expected_arrival_date를 계산하지 않는다 (상세설계 §4-⑦)"
        )
    if constraints["pending"]["purchase_payment_days"] is None:
        deferred.append(
            "지급일 기준 현금 검사 보류 — purchase_payment_days(N5) 미확정이라 "
            "payment_date를 계산하지 않는다"
        )
    if freshness_cap is None:
        deferred.append(f"신선도 상한 검사 보류 — {item} 품목 보관한계가 constraints에 미확정")
    return deferred
