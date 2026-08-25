"""⑤ allocate_sourcing — 등급 배분 스코어링 (상세설계 §4-⑤ · 백로그 E3-1).

**계산만 한다** (규칙 6). 단가 이득과 신선도 리스크를 견줘 중품을 얼마나 태울지 정하고,
그 판단이 순수 함수 다섯 개로 쪼개져 있다. 조합 트레이드오프의 LLM 판단(E3-2)은 이 결과를
**입력**으로 받는다 — 아래 결과를 다시 계산하지 않는다.

§4-⑤ Epic 3 확정(8/25) 두 가지가 이 파일의 구조를 정했다:

1. **'평시' 기준은 constraints의 선언 상수**다. 과거 시세 이력 포트가 계약에 없기 때문이다.
   대신 판정을 ``baseline_spread()`` 한 함수로 격리해, 실데이터 전환 시 그 함수 본문만
   직전 N일 통계로 바꾸면 되게 했다.
2. **평시 중품 비중 상수는 두지 않는다.** 배분은 스코어링의 출력이다 — 같은 함수가
   입력(스프레드)만으로 평시엔 상품 수렴, 확대일엔 중품 확대로 갈린다.

**출력은 비율이다** (kg이 아니다). 안별 총량이 달라 절대 수량은 ⑥이 만든다. ⑤가 kg을
만들지 않으므로 사중 일치의 수량 축을 여기서 깨뜨릴 수단 자체가 없다.
"""

from datetime import date
from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes._guards import require_positive
from app.purchase_agent.nodes.draft_plan import fixed_market_quotes
from app.purchase_agent.schemas import FIXED_MARKET
from app.purchase_agent.state import PurchaseAgentState


def grade_spread(quotes: list[dict], top_grade: str, mid_grade: str) -> float | None:
    """등급 스프레드 = ``(P상 − P중) / P상``. 두 등급이 다 있어야 성립한다.

    한쪽이 없으면 **None**이다 — 0이 아니다 (규칙 3). 0은 "두 등급 값이 같다"는 확정이고
    None은 "잴 수 없다"다. 0으로 채우면 확대 판정이 조용히 "미확대"로 통과한다.
    """
    prices = {quote["grade"]: quote["price"] for quote in fixed_market_quotes(quotes)}
    if top_grade not in prices or mid_grade not in prices:
        return None
    top = require_positive(prices[top_grade], f"quote[{top_grade}]")
    return (top - prices[mid_grade]) / top


def baseline_spread(item: str, constraints: dict) -> float | None:
    """평시 스프레드 기준선. **실데이터 전환 시 바뀌는 유일한 함수다** (§4-⑤ Epic 3 확정 1).

    지금은 constraints의 선언값(SIM_FIXED)을 읽는다. 과거 시세 이력 포트가 IO명세 §1의
    계약 6개에 없어 통계를 낼 수 없기 때문이다. 이력이 생기면 이 함수 본문만 "직전 N일
    스프레드의 중앙값"으로 바꾼다 — 호출부(``is_spread_widened``)는 그대로다.

    품목이 표에 없으면 None. 기준선 없이 "평시 대비 확대"를 판정할 수 없다 (규칙 3).
    """
    return constraints["grade"]["baseline_grade_spread"].get(item)


def is_spread_widened(spread: float | None, baseline: float | None, widening_ratio: float) -> bool:
    """스프레드가 평시 대비 확대됐는가 — 중품 활용 **검토** 진입 게이트 (상세설계 §7).

    §7 임계표 "등급 스프레드 확대 = 평시 대비 +50%".

    ⚠️ 둘 중 하나라도 None이면 **False**다. 이건 "확대가 아니다"라는 판정이 아니라
    "판정하지 못했으니 진입하지 않는다"는 보수적 기본값이다. 못 잰 사유는 호출부가
    ``blocked_by``로 따로 기록해 risks에 싣는다 — 여기서 조용히 삼키지 않는다.
    """
    if spread is None or baseline is None:
        return False
    return spread >= baseline * (1 + widening_ratio)


def top_grade_shelf_days(inventory: dict, top_grade: str) -> int | None:
    """기준등급(상) 로트의 유통기한 = "상품 한계일".

    ⚠️ **추론이다.** 상세설계 §7 임계표는 "중품 소진 한계 = 상품 한계일 × 0.6"만 규정하고
    상품 한계일 값을 주지 않는다. 같은 표의 "보관한계(품목별) 배추 135일"은 저장성 기준이라
    다른 개념이고(``mocks/inventory.json._주의``), 값이 실재하는 곳은 재고 로트의
    ``shelf_life_days``뿐이다. 배추 10일 × 0.6 = 6일이 §4-⑤ 판단 예시의 "6일"과 일치한다.

    같은 품목·같은 등급의 로트면 유통기한이 같다고 보고 신규 매입분에도 그 값을 쓴다.
    **로트의 잔여신선도가 아니라 전체 유통기한**을 읽는 이유: 오늘 사는 물건은 새 물건이라
    기존 로트가 이미 쓴 날짜를 물려받지 않는다.

    로트가 여럿이면 **가장 짧은 유통기한**을 쓴다. IO명세 §1-③은 lots의 순서도, 같은 등급
    로트의 유통기한이 같다는 것도 보장하지 않는다 — 첫 로트를 집으면 같은 재고를 순서만
    바꿔 넣어도 배분이 달라진다. 짧은 쪽이 안전한 방향이다.

    등급별 보관한계 마스터가 생기면 **이 함수만** 교체한다. 상 등급 로트가 없으면
    None — 중품 소진 한계를 계산하지 않는다 (규칙 3).
    """
    days = [
        lot["shelf_life_days"]
        for lot in inventory.get("lots") or []
        if lot.get("grade") == top_grade and lot.get("shelf_life_days") is not None
    ]
    return min(days) if days else None


def near_term_demand_kg(
    orders: list[dict], as_of: str, within_days: float, *, since_days: float = 0
) -> int:
    """``[since_days, since_days + within_days]`` 구간에 납품하는 확정주문 합(kg).

    등급-신선도 매칭 필터다. §4-⑤ 판단 예시를 그대로 재현한다: as_of 8/21, 소진 한계 6일
    이면 **8/24 납품 12,000kg은 들어오고 8/29 납품 6,000kg은 빠진다**.

    **하한이 필요한 이유는 두 가지다.**

    1. 입고 전에 납기가 지난 주문은 이 매입분으로 채울 수 없다. ``since_days``는 입고까지
       걸리는 날(N4)이다 — 매입분이 창고에 도착하기 전 날짜는 후보가 아니다.
    2. 하한이 없으면 **납기가 이미 지난 주문**까지 근접 수요로 잡혀 상한이 부풀어 오른다.
       현재 mock 로더가 ``0 <= offset``으로 걸러 가려져 있지만, 실데이터 스냅샷에 연체
       주문이 하나라도 들어오면 그대로 새는 자리다.

    ⚠️ N4가 NULL이면 호출자가 ``since_days=0``으로 부른다 — **as_of 기준 근사**다.
    실제 창은 입고일 기준으로 이동하므로 소화 가능량이 이 값과 다를 수 있고, 그 사실은
    ⑥이 안의 risks에 싣는다 (규칙 3 · 정의서 §1.2-10).
    """
    today = date.fromisoformat(as_of)
    return sum(
        order["qty_kg"]
        for order in orders
        if since_days <= (date.fromisoformat(order["due_date"]) - today).days
        <= since_days + within_days
    )


def mid_grade_score(price_gain: float, freshness_risk: float, weights: dict) -> float:
    """중품 채택 스코어 = 가중 단가 이득 − 가중 신선도 리스크.

    양수면 "싸게 사는 이득이 빨리 털어야 하는 부담을 넘는다"다. 가중치는 constraints에 있고
    **튜닝 대상**이다 (§4-⑤ Epic 3 확정 2 — "가중치는 튜닝 대상, 절대값 고정 금지").
    """
    return weights["price_gain"] * price_gain - weights["freshness_risk"] * freshness_risk


def evaluate_mid_grade(state: PurchaseAgentState, constraints: dict) -> dict[str, Any]:
    """중품을 얼마나 태울지 판단한다. 배분의 근거 전체를 dict 하나로 돌려준다.

    두 게이트를 **모두** 통과해야 채택한다:

    1. 스프레드 확대 (§7 "+50% 확대 시 중품 활용 검토") — 진입
    2. 스코어 > 0 (단가 이득 > 신선도 리스크) — 채택

    AND인 게 실제로 필요하다. 양파처럼 보관한계가 길면 ``freshness_risk``가 0으로 clamp돼
    스코어만으로는 **평시에도 중품 100%**가 된다. 확대 게이트가 그걸 막는다.

    상한은 스코어가 아니라 수요 형상이 정한다 — ``근접 납품량 / 총량``.
    """
    grade_cfg = constraints["grade"]
    top_grade = constraints["allocation"]["reference_grade"]
    mid_grade = grade_cfg["mid_grade"]
    facts: dict[str, Any] = {
        "top_grade": top_grade,
        "mid_grade": mid_grade,
        "ratio": 0.0,
        "widened": False,
        "blocked_by": None,
    }

    spread = grade_spread(state["market_quotes"], top_grade, mid_grade)
    baseline = baseline_spread(state["item"], constraints)
    facts["spread"] = spread
    facts["baseline"] = baseline
    if spread is None:
        facts["blocked_by"] = f"당일 시세에 {top_grade}·{mid_grade} 두 등급이 모두 있지 않다"
        return facts
    if baseline is None or baseline <= 0:
        # 0은 "평시엔 등급 간 가격차가 없다"는 뜻인데, 그러면 "평시 대비 +50%"라는 판정
        # 자체가 성립하지 않는다 (0 × 1.5 = 0이라 어떤 스프레드든 확대로 통과한다).
        # 미확정과 같이 취급해 계산을 막는다 (규칙 3).
        facts["blocked_by"] = f"{state['item']} 평시 스프레드 기준선이 미확정이거나 0 이하"
        return facts

    facts["widened"] = is_spread_widened(
        spread, baseline, constraints["triggers"]["grade_spread_widening_ratio"]
    )

    top_shelf = top_grade_shelf_days(state["inventory"], top_grade)
    facts["top_shelf_days"] = top_shelf
    if top_shelf is None:
        facts["blocked_by"] = f"{top_grade} 등급 로트가 없어 상품 한계일을 알 수 없다"
        return facts

    shelf_ratio = grade_cfg["mid_grade_shelf_ratio"]
    facts["shelf_ratio"] = shelf_ratio
    facts["shelf_days"] = top_shelf * shelf_ratio

    # 신선도 리스크 = 확정주문 창 중 중품이 감당하지 못하는 기간의 비율.
    # 분모를 order_window_days로 두는 이유: ③이 같은 창의 평균으로 수요를 냈다 —
    # 두 계산이 다른 창을 보면 "얼마를 언제까지 팔 수 있는가"의 기준이 어긋난다.
    window = constraints["demand"]["order_window_days"]
    facts["freshness_risk"] = max(0.0, 1 - facts["shelf_days"] / window)
    facts["score"] = mid_grade_score(spread, facts["freshness_risk"], grade_cfg["score_weights"])

    # N4(입고 소요일)가 확정되면 소진 창이 입고일 기준으로 **이동**한다 — 입고 전 납기는
    # 이 매입분으로 채울 수 없고, 뒤쪽은 그만큼 밀린다. NULL이면 0으로 채우지 않고
    # "as_of 기준 근사"임을 facts에 남겨 ⑥이 risks에 싣게 한다 (규칙 3).
    lead_days = constraints["pending"]["inbound_lead_days"]
    facts["arrival_basis_assumed"] = lead_days is None
    facts["near_qty_kg"] = near_term_demand_kg(
        state["confirmed_orders"]["orders"],
        state["date"],
        facts["shelf_days"],
        since_days=0 if lead_days is None else lead_days,
    )

    facts["cap_ratio"] = _cap_ratio(facts["near_qty_kg"], state, facts)
    if facts["cap_ratio"] is None:
        return facts
    if facts["widened"] and facts["score"] > 0:
        facts["ratio"] = facts["cap_ratio"]
    return facts


def _cap_ratio(near_qty_kg: int, state: PurchaseAgentState, facts: dict) -> float | None:
    """중품 비중 상한. **두 분모 중 작은 쪽**을 쓴다.

    - ``확정주문 총량``: 수요 형상 그대로의 상한
    - ``가장 큰 안의 총량``: 비율을 kg으로 되돌렸을 때 근접 납품량을 넘지 않게 하는 상한

    두 번째가 없으면 상한이 **커버일수 D에 인질로 잡힌다**. D가 확정주문 창(14일)을 넘으면
    총량이 확정주문보다 커져 ``비중 × 총량 > 근접 납품량``이 되는데, 지금 D 매핑(최대 12)에서
    안 드러날 뿐 ``coverage_days.max``는 18이다. 튜닝 한 번으로 조용히 깨질 자리라 여기서 막는다.
    """
    total_kg = state["confirmed_orders"]["total_kg"]
    if total_kg <= 0:
        # 확정주문이 0이면 상한을 만들 분모가 없다. 나눗셈으로 터지는 대신 배정을 접는다 —
        # 수요가 없는 날 등급 배분은 판단할 것 자체가 없다.
        facts["blocked_by"] = "확정주문이 없어 근접 납품 비중을 낼 수 없다"
        return None
    totals = [draft["total_qty_kg"] for draft in state["base_plan"]["drafts"]]
    largest = max([total for total in totals if total > 0], default=0)
    caps = [near_qty_kg / total_kg]
    if largest > 0:
        caps.append(near_qty_kg / largest)
    return min(1.0, *caps)


def _ratio_line(grade: str, prices: dict[str, int], ratio: float) -> dict[str, Any]:
    """비율 한 줄. 단가는 **당일 시세에 실재하는 값**만 쓴다 (규칙 4) — 지어내지 않는다."""
    return {
        "market": FIXED_MARKET,
        "grade": grade,
        "ratio": ratio,
        "grade_unit_price": prices[grade],
    }


def _yields_positive_kg(state: PurchaseAgentState, ratio: float) -> bool:
    """이 비율이 **모든 안에서** 1kg 이상으로 떨어지는가.

    ⑥의 materialize는 ``round(총량 × 비율)``로 kg을 만들고 마지막 줄에 잔량을 준다. 어느
    한 줄이라도 0kg이면 스키마의 ``qty_kg > 0``이 **제안 전체**를 ValidationError로 죽인다 —
    의미 없는 한 줄 때문에 그날 산출물이 통째로 사라진다.

    **가장 작은 안만 보면 안 된다.** 마지막 줄은 ``총량 − round(총량 × 비율)``이라 큰 안에서
    0이 되는 조합이 따로 있다. 안별로 전부 확인한다 — ⑤가 내는 비율 하나를 세 안이 공유하기
    때문에, 한 안에서만 깨져도 그 안이 통째로 사라진다.
    """
    drafts = state["base_plan"]["drafts"]
    totals = [draft["total_qty_kg"] for draft in drafts if draft["total_qty_kg"] > 0]
    return bool(totals) and all(round(total * ratio) >= 1 for total in totals)


def allocate_sourcing(state: PurchaseAgentState) -> dict[str, Any]:
    """등급 배분 비율을 정한다 (계산 전용 — E3-1).

    E3-2에서 LLM이 붙는 자리는 여기다: ``evaluate_mid_grade``가 낸 사실들(스프레드·소진
    한계일·근접 납품량·스코어)을 프롬프트로 주고 **조합 트레이드오프**를 판단하게 한다 —
    "중품이 130원 싸지만 6일 내 소진 필요, 8/24 납품분엔 배정 가능, 8/29분은 상품으로.
    혼합 비율은?" 숫자는 그때도 계산이 소유한다 (규칙 6). 지금은 rule_only다.
    """
    constraints = load_constraints()
    quotes = fixed_market_quotes(state["market_quotes"])
    prices = {quote["grade"]: quote["price"] for quote in quotes}

    top_grade = constraints["allocation"]["reference_grade"]
    # 기준등급 시세가 없으면 가장 비싼 등급으로 보수적으로 간다. **기준등급이 아니므로**
    # 실제로 배정한 등급을 facts에 남긴다 — ⑥의 risks가 "기준등급으로 배정"이라고 적으면
    # 형식만 맞고 내용이 거짓인 근거가 나간다.
    base_grade = top_grade if top_grade in prices else max(prices, key=prices.get)

    decision = evaluate_mid_grade(state, constraints)
    decision["base_grade"] = base_grade
    mid_ratio = decision["ratio"]

    min_share = constraints["grade"]["min_share"]
    mid_ok = mid_ratio >= min_share and _yields_positive_kg(state, mid_ratio)
    # 잔여분도 같은 검사를 받아야 한다. 근접 납품이 확정주문 전부인 날(양파·피마늘)은
    # 상한이 1.0이 되어 기준등급 줄이 **0이 된다** — 그건 오류가 아니라 "전량 중품"이다.
    base_ok = _yields_positive_kg(state, 1.0 - mid_ratio)

    if mid_ok and base_ok:
        # **중품이 먼저다.** ⑥이 마지막 줄에 잔량을 흡수시키므로, 중품을 끝에 두면 반올림
        # 나머지가 신선도 상한을 넘길 수 있다. 상품이 흡수하면 중품은 항상
        # round(총량 × 비율) 이하로 유지된다.
        lines = [
            _ratio_line(decision["mid_grade"], prices, mid_ratio),
            # 1.0 - mid_ratio로 **구성**한다. 두 비율을 각자 계산해서 더하면 부동소수점
            # 합이 1에서 밀려 ⑥의 합계 검사(1e-9)에 걸릴 수 있다.
            _ratio_line(base_grade, prices, 1.0 - mid_ratio),
        ]
    elif mid_ok:
        decision = {**decision, "ratio": 1.0}
        lines = [_ratio_line(decision["mid_grade"], prices, 1.0)]
    else:
        # 비율 0을 **줄로 내보내지 않는다.** ⑥의 _validate_ratios가 ratio > 0을 요구한다.
        # 평시 출력이 스텁 시절과 동일해지는 것도 이 경로다.
        decision = {**decision, "ratio": 0.0}
        lines = [_ratio_line(base_grade, prices, 1.0)]

    lines[0] = {**lines[0], "decision": decision}
    return {"sourcing_plan": lines}
