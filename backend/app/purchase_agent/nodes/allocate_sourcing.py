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

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.llm.mix import MixDecision, MixSelector, build_mix_context
from app.purchase_agent.llm.schemas import MixCandidate
from app.purchase_agent.nodes._guards import require_positive
from app.purchase_agent.nodes.draft_plan import fixed_market_quotes, pending_value
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


#: 매입이 다루는 품목 (정의서 §1). 물류는 이보다 넓은 목록을 보낸다 — 실측에서
#: ``건고추``(90일)가 함께 왔다. 우리 품목이 아니면 정책을 읽지 않는다.
#:
#: ⚠️ 여기서 거르지 않아도 ``item`` 매칭이 이미 다른 품목을 떨어뜨린다. 그런데도 목록을
#:   두는 이유는 **"물류 목록에 있다"가 "매입이 다룬다"를 뜻하지 않는다**는 것을 코드가
#:   말하게 하기 위해서다. 나중에 품목이 늘 때 여기가 고칠 자리다.
PURCHASE_ITEMS = ("배추", "무", "피마늘", "양파")


def item_storage_policy(inventory: dict, item: str) -> dict | None:
    """물류가 보낸 품목 보관 정책 중 **이 품목 것**을 고른다.

    🔴 **첫 항목을 집으면 안 된다.** 물류는 4품목이 아니라 **5품목**을 한 목록에 담아
    보내고(2026-08-31 실측: 무·배추·양파·건고추·피마늘), **첫 항목이 무(14일)** 다.
    배추를 돌리며 ``policies[0]`` 을 읽으면 무의 보관한계로 중품 소진 창을 계산한다 —
    에러가 나지 않아 아무도 모른다. 실제로 이 함정을 밟은 사례가 보고됐다(#79).

    ``lots`` 에 품목 필터를 거는 ``adapter.absorb_inventory`` 와 같은 이유다. 다만
    policies 는 어댑터가 통째로 넘기므로(품목 축이 행 안에 있다) **읽는 쪽이 거른다.**
    """
    if item not in PURCHASE_ITEMS:
        return None
    for row in inventory.get("item_storage_policies") or []:
        if isinstance(row, Mapping) and row.get("item") == item:
            return dict(row)
    return None


def _positive_int(value: Any) -> int | None:
    """일 단위 정수만 받는다. 아니면 ``None`` — **조용히 고치지 않는다.**

    ⚠️ ``int()`` 에 그대로 넘기면 ``10.9`` 가 10으로 잘리고 ``"10"`` 이 통과하며
    ``"oops"`` 는 ``ValueError`` 로 노드를 죽인다 (Codex 교차검증). 잘린 값은 에러가
    나지 않아 **아무도 모르고**, 죽는 쪽은 봉투 하나가 그래프 전체를 멈춘다.

    ``bool`` 을 따로 막는 것은 ``True`` 가 1일로 통과하기 때문이다
    (``adapter._arrival_input_problems`` · ``schemas._reject_boolean`` 과 같은 이유).
    0과 음수도 거른다 — 보관한계가 0일이면 중품을 아예 못 쓰는데 그건 "확정된 0"이
    아니라 값이 잘못 온 것이다.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value != int(value) or value <= 0:
        return None
    return int(value)


def _ratio(value: Any) -> float | None:
    """``0 < factor <= 1`` 만 받는다. 아니면 ``None`` — 폴백으로 보낸다.

    ⚠️ 범위를 안 보면 조용히 틀린다 (Codex 교차검증, 전부 재현함).
    ``0`` 이나 음수는 "물류값 수신 완료"로 처리되어 **폴백도 고지도 없이** 중품 배분을
    0으로 만들고, ``1`` 초과는 중품 소진 한계를 상품 한계일보다 **길게** 만든다 —
    중품이 상품보다 오래 간다는 뜻이라 개념이 뒤집힌다.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0 < value <= 1:
        return None
    return float(value)


def top_grade_shelf_days(inventory: dict, top_grade: str, item: str) -> int | None:
    """기준등급(상)의 "상품 한계일".

    **값의 출처는 둘이고 물류가 우선이다.**

    1. 물류 ``item_storage_policies[item].operational_limit_days`` — 운영 보관한계.
       실측값이고 품목 마스터에서 온다 (배추 10 · 무 14 · 양파 30 · 피마늘 30).
    2. 재고 로트의 ``shelf_life_days`` — 물류가 이 키를 싣지 않을 때의 경로다.

    ⚠️ **값이 와도 등급을 특정하지 못하면 None이다.** 이 함수가 답하는 것은
    *"이 품목의 보관한계"* 가 아니라 *"기준등급 물건의 한계일"* 이다. 로트 ``grade`` 가
    전부 ``None`` 이면(#69 등급 어휘 미확정) 어느 등급의 한계일인지 말할 수 없다 —
    품목 정책값을 그 자리에 끼워 넣으면 **근거 없는 결론**이 된다 (규칙 3).
    마스터도 이 상태를 예상하고 있다 (``master/verifier.py`` — "값을 쓰면서도 결론이
    안 난다"는 원인 ③).

    ⚠️ ``operational_limit_days`` 는 상세설계 §7 임계표의 **"보관한계(품목별) 배추 135일"
    과 다른 개념이다.** 그쪽은 저장성 축이고 ``_freshness_cap_kg`` 가 쓴다. 이름이
    비슷해 섞기 쉬운데, 섞으면 배추 상한이 135일 기준에서 10일 기준으로 바뀌어
    매입 가능량이 9분의 1이 된다. 물류도 #90에서 "기존 Lot 의 잔여 신선도와 다른 값"
    이라고 적었다 — 세 문서가 같은 말을 한다.

    로트가 여럿이면 **가장 짧은 유통기한**을 쓴다. IO명세 §1-③은 lots의 순서도, 같은 등급
    로트의 유통기한이 같다는 것도 보장하지 않는다 — 첫 로트를 집으면 같은 재고를 순서만
    바꿔 넣어도 배분이 달라진다. 짧은 쪽이 안전한 방향이다.
    """
    graded = [lot for lot in inventory.get("lots") or [] if lot.get("grade") == top_grade]
    if not graded:
        # 기준등급 로트를 특정하지 못했다 — 사유는 shelf_days_block_reason 이 갈라 적는다.
        return None

    policy = item_storage_policy(inventory, item)
    limit = _positive_int(policy.get("operational_limit_days")) if policy else None
    if limit is not None:
        return limit

    days = [lot["shelf_life_days"] for lot in graded if lot.get("shelf_life_days") is not None]
    return min(days) if days else None


def mid_grade_shelf_ratio(inventory: dict, item: str, constraints: dict) -> tuple[float, bool]:
    """중품 소진 계수. **물류 값이 정본이고, 없으면 설계 기본값 + 고지.**

    돌려주는 둘째 값은 *"기본값으로 떨어졌는가"* 다 — ⑥이 risks 에 싣는다 (규칙 3).

    ⚠️ **읽는 자리를 하나로 모은 함수다.** 전에는 호출부가
    ``constraints["grade"]["mid_grade_shelf_ratio"]`` 를 직접 읽었는데, 물류가 같은 개념을
    ``medium_grade_factor`` 로 보내면서 **같은 값(0.6)이 두 곳에 생겼다.** 값이 같아
    갈라져도 티가 나지 않는 상태이고, N4 에서 실제로 겪은 실패 모드다
    (``pending_value`` docstring 이 경고했고 이 파일이 그대로 재현했다).

    yaml 쪽 키 이름에 ``_fallback`` 을 붙인 것은 **어느 쪽이 정본인지 이름이 말하게**
    하기 위해서다. 값을 코드에 박지 않는 이유는 규칙 7이다.
    """
    policy = item_storage_policy(inventory, item)
    factor = _ratio(policy.get("medium_grade_factor")) if policy else None
    if factor is not None:
        return factor, False
    # 값이 **왔지만 못 쓰는** 경우도 폴백이다 — 고지 문구가 "받지 못해"라고만 적히면
    # 그 차이가 지워지지만, 화면이 알아야 할 것은 "물류 값으로 셈하지 않았다" 하나다.
    return float(constraints["grade"]["mid_grade_shelf_ratio_fallback"]), True


#: ``shelf_life_days``를 못 읽은 사유. ``None``이 하나인데 원인이 넷이라 갈라 적는다.
#:
#: ⚠️ 전에는 호출부가 원인과 무관하게 *"{등급} 등급 로트가 없다"*로 적었다. 물류 경로에서
#:   실제 원인은 **키 자체가 안 실린 것**인데(#76 미결), 그렇게 쓰면 *"재고에 상 등급이
#:   없구나"*로 읽힌다 — 없는 사실을 만들어 낸다. 침묵도 오답이지만 **틀린 사유는 더 나쁘다.**
_SHELF_DAYS_MISSING_KEY = (
    "품목 보관한계를 어느 쪽으로도 받지 못했다 — 로트에 shelf_life_days가 없고 "
    "operational_limit_days 미수신. 중품 소진 한계를 계산하지 않았다"
)

#: 등급 어휘가 확정되지 않아 기준등급을 특정하지 못한 상태 (#69).
#:
#: 🔴 **봉투 최상위 키 이름을 쓰고, 미결 어휘를 그 뒤 12자 안에 둔다.**
#:   마스터의 SUPPLIED-BUT-UNRESOLVED 검사는 두 관문을 통과해야 울린다
#:   (``master/verifier.py._check_supplied_but_unused``).
#:
#:   1. 키가 ``supplied`` 에 있어야 한다 — 그 집합은 **봉투 payload 의 최상위 키**로만
#:      만들어진다. ``operational_limit_days`` 는 ``item_storage_policies[]`` **안에**
#:      있어 최상위가 아니다. 그 이름으로 적으면 대조 자체가 일어나지 않는다
#:      (2026-08-31 실측: 사유는 나갔는데 concerns 0건).
#:   2. ``re.escape(key) + r".{0,12}?(?:미확정|미결|싣지 않았다|받지 못)"`` 에 걸려야 한다.
#:
#:   그래서 **최상위 키(``item_storage_policies``)로 말하고, 실제로 쓴 필드는 뒤에 밝힌다.**
#:   문구를 손볼 때 이 둘을 깨지 않는다 — 테스트가 실제 패턴과 실제 봉투 키로 잠근다.
#:
#: 이 상태가 마스터가 말하는 **원인 ③**이다: 값은 쓰는데 다른 입력이 없어 결론이 안 난다.
#: 원인 ①(다른 곳을 본다)·②(값이 그 자리까지 안 온다)와 구분되어야 물류에 잘못된
#: 문의가 가지 않는다 — 물류는 보낼 것을 다 보냈다.
_SHELF_DAYS_GRADE_UNRESOLVED = (
    "item_storage_policies 반영했으나 결론 미결 — operational_limit_days는 받았고, "
    "보유 로트의 등급이 모두 미상이라(#69) 기준등급 한계일을 특정할 수 없다"
)


def shelf_days_block_reason(inventory: dict, top_grade: str, item: str) -> str:
    """``top_grade_shelf_days``가 ``None``을 돌려준 **이유**.

    호출부가 ``blocked_by``에 그대로 싣는다 — risks 로 나가 *"무엇을 못 봤는지"*가
    사용자에게 남는다 (§3.7.6 · 규칙 3). 사유를 안 남기면 "중품을 검토하고 안 쓴 것"과
    "검토 자체를 못 한 것"이 같은 화면으로 보인다.

    ⚠️ **순서가 곧 사실 판정이다.** 전에는 "보관한계 미수신"이 등급 검사보다 먼저였는데,
    ``operational_limit_days`` 가 배선된 지금 실물은 **값이 와 있다.** 그 상태에서 먼저
    걸리면 *"물류가 안 줬다"* 는 **거짓 사유**가 나간다 — 침묵도 오답이지만 틀린 사유는
    더 나쁘다.

    ⚠️ **판정 기준을 ``top_grade_shelf_days`` 와 맞춘다.** 전에는 "키가 있는가"로 봤는데
    그쪽은 "값이 있는가"로 본다. 두 기준이 갈리면 사유가 거짓이 된다 — 상 등급 로트의
    ``shelf_life_days`` 가 ``None`` 이면 *"상 등급 로트가 없어"* 라고 답했다. **로트는
    있다.** 없는 것은 값이다 (Codex 교차검증, 재현함).
    """
    lots = inventory.get("lots")
    if lots is None:
        return "재고 로트를 받지 못해 상품 한계일을 알 수 없다"
    if not lots:
        return "보유 로트가 없어 상품 한계일을 알 수 없다"

    policy = item_storage_policy(inventory, item)
    limit = _positive_int(policy.get("operational_limit_days")) if policy else None
    has_limit = limit is not None

    # ① 등급을 못 밝힌 것과 ② 그 등급이 없는 것은 다르다 — 값 이야기보다 먼저 가른다.
    graded = [lot for lot in lots if lot.get("grade") == top_grade]
    if not graded:
        if all(lot.get("grade") is None for lot in lots):
            # 값이 왔는지에 따라 사유가 갈린다 — 안 온 값을 "반영"이라 적지 않는다.
            return (
                _SHELF_DAYS_GRADE_UNRESOLVED
                if has_limit
                else "보유 로트의 등급이 모두 미상이라 상품 한계일을 알 수 없다"
            )
        return f"{top_grade} 등급 로트가 없어 상품 한계일을 알 수 없다"

    # ③ 기준등급 로트는 있다 — 여기까지 왔다면 없는 것은 **값**이다.
    if not has_limit and all(lot.get("shelf_life_days") is None for lot in graded):
        return _SHELF_DAYS_MISSING_KEY
    return f"{top_grade} 등급 로트의 상품 한계일을 읽지 못했다"


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

    top_shelf = top_grade_shelf_days(state["inventory"], top_grade, state["item"])
    facts["top_shelf_days"] = top_shelf
    if top_shelf is None:
        facts["blocked_by"] = shelf_days_block_reason(
            state["inventory"], top_grade, state["item"]
        )
        return facts

    shelf_ratio, ratio_fallback = mid_grade_shelf_ratio(
        state["inventory"], state["item"], constraints
    )
    facts["shelf_ratio"] = shelf_ratio
    facts["shelf_ratio_fallback"] = ratio_fallback
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
    # ``constraints["pending"]``을 직접 읽지 않는다 — 그 값은 "아직 아무도 안 줬다"는
    # **기본값**이고, 물류가 보내면 State의 값이 정답이다. 직접 읽으면 어댑터 경로에서
    # N4가 와 있어도 못 보고 "미확정"을 고지한다 (``pending_value`` docstring —
    # "두 곳을 각자 읽으면 한쪽만 바뀐다"가 실제로 일어난 자리).
    lead_days = pending_value(state, constraints, "inbound_lead_days")
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


#: 자주 쓰는 배수의 **읽기 좋은 이름**. constraints의 배수 목록을 복제하는 게 아니라
#: 이름을 붙여줄 뿐이다 — 목록에 없는 배수는 아래에서 id를 유도하므로 **조용히 누락되지
#: 않는다**. (Codex 교차검증: 여기가 YAML 단일 소스를 복제하던 자리였다.)
_CANDIDATE_LABELS = {
    0.0: ("BASE_ONLY", "전량 기준등급"),
    0.5: ("MID_HALF", "중품을 상한의 절반만"),
    1.0: ("MID_CAPPED", "중품을 상한만큼"),
}


def candidate_label(fraction: float) -> tuple[str, str]:
    """배수 → ``(id, 설명)``. **모르는 배수도 후보가 된다.**

    이름 표에 없으면 배수에서 id를 유도한다. 표를 단일 소스처럼 쓰면 constraints에
    ``0.25``를 넣었을 때 그 후보가 아무 말 없이 사라진다 — 규칙 7이 막으려는 형태다.
    """
    known = _CANDIDATE_LABELS.get(fraction)
    if known is not None:
        return known
    percent = f"{fraction:.2f}".rstrip("0").rstrip(".").replace(".", "_")
    return f"MID_F{percent}", "중품을 상한의 일부만"


def build_mix_candidates(
    state: PurchaseAgentState, cap_ratio: float, constraints: dict
) -> list[tuple[str, float, str]]:
    """규칙이 만드는 후보 집합 — ``(id, 중품 비율, 설명)``.

    **LLM은 이 목록 밖을 고를 수 없다.** 백로그 E3-2 DoD("장기 보관 계획+중품 과다 조합
    회피")가 LLM의 판단력이 아니라 **여기서** 지켜지는 이유다: 모든 후보가 ``cap_ratio``
    이하라 "중품 과다"는 구조적으로 만들어질 수 없고, LLM이 무엇을 골라도 상한을 못 넘는다.

    걸러내는 것 두 가지 — E3-1이 이미 아프게 배운 자리다:

    * ``min_share`` 미만은 배분이 아니다
    * ``_yields_positive_kg``를 **안별 전수**로 통과해야 한다. 한 줄이라도 0kg이면
      스키마(``qty_kg > 0``)가 **제안 전체**를 죽인다

    중복 비율은 하나로 합친다 — ``cap_ratio``가 0에 가까우면 0.5배와 1.0배가 같은 값이
    되고, 그러면 LLM에게 구분 불가능한 선택지를 내미는 꼴이다.
    """
    min_share = constraints["grade"]["min_share"]
    candidates: list[tuple[str, float, str]] = []
    seen: set[float] = set()
    for fraction in constraints["grade"]["mix_candidate_fractions"]:
        # **반올림하지 않는다.** round(cap × 1.0, 6)은 cap을 미세하게 **넘길 수 있고**
        # (0.6666666… → 0.666667), 그러면 "모든 후보가 상한 이하"라는 불변이 깨진다.
        # 중복 판정에만 반올림 키를 쓰고 값은 정확히 유지한다.
        ratio = cap_ratio * fraction
        key = round(ratio, 9)
        if key in seen:
            continue
        if ratio > 0 and (ratio < min_share or not _yields_positive_kg(state, ratio)):
            continue
        # 잔여분(기준등급)도 같은 검사를 받아야 한다. 잔여가 0kg이면 그 줄이 사라지는 게
        # 아니라 제안이 죽는다 — 100% 중품은 별도 경로로만 허용한다 (아래 라인 구성부).
        if 0 < ratio < 1 and not _yields_positive_kg(state, 1.0 - ratio):
            continue
        candidate_id, summary = candidate_label(fraction)
        seen.add(key)
        candidates.append((candidate_id, ratio, summary))
    return candidates


def _mix_signals(facts: dict) -> tuple[list[str], list[str]]:
    """LLM에 넘길 신호·사실. **숫자를 넣지 않는다** (§4-⑤ E3-2 "입력 컨텍스트도 기호화")."""
    signals: list[str] = []
    facts_text: list[str] = []
    if facts.get("widened"):
        signals.append("GRADE_SPREAD_WIDENED")
        facts_text.append("등급 스프레드가 평시보다 확대됐다.")
    if facts.get("score", 0) > 0:
        signals.append("MID_GRADE_SCORE_POSITIVE")
        facts_text.append("중품의 단가 이득이 신선도 리스크를 넘는다.")
    if facts.get("cap_ratio") is not None and facts["cap_ratio"] < 1.0:
        signals.append("NEAR_TERM_DEMAND_LIMITED")
        facts_text.append("근접 납품량이 전량을 감당하지 못해 중품 상한이 걸려 있다.")
    if facts.get("arrival_basis_assumed"):
        signals.append("ARRIVAL_DATE_ASSUMED")
        facts_text.append("입고 소요일이 미확정이라 소진 창을 오늘 기준으로 근사했다.")
    return signals, facts_text


def _select_mix(
    state: PurchaseAgentState,
    facts: dict,
    constraints: dict,
    selector: MixSelector | None,
) -> tuple[float, MixDecision | None]:
    """후보를 만들고 LLM에게 고르게 한다. **숫자는 후보의 것을 그대로 쓴다.**

    기본안(``default``)은 **규칙이 고르던 값**이다 — LLM이 꺼져 있든 전면 실패하든 이
    함수가 돌려주는 비율이 E3-1 시절과 같아진다. 회귀가 아니라 무변화다.
    """
    rule_ratio = facts["ratio"]
    # ``cap_ratio``는 **키 자체가 없을 수 있다** — evaluate_mid_grade가 blocked_by를 세우고
    # 조기 반환하는 경로(상 등급 로트 없음·기준선 미확정 등)에서는 거기까지 가지 않는다.
    # 그때는 중품 배정 자체를 안 하는 날이므로 고를 후보도 없다.
    cap_ratio = facts.get("cap_ratio")
    if cap_ratio is None or selector is None:
        return rule_ratio, None
    # **규칙이 중품을 태우기로 한 날에만 묻는다.** E3-1의 AND 게이트(확대 ∧ 스코어>0)가
    # "중품을 쓸 것인가"를 소유하고, LLM은 "쓴다면 얼마나"만 판단한다.
    #
    # 게이팅을 "후보 ≥ 2"로만 두면 안 된다 — 실측 결과 cap_ratio는 스프레드와 무관하게
    # 근접 납품량으로 계산되므로 **평시에도 후보가 3개** 나오고, 4앵커 × 4품목 = 16회
    # 전부 호출된다(백로그 비용 완화책이 무력화). 여기가 그 계산과 실제가 갈린 자리다.
    #
    # 평시에 LLM에게 문을 열어주면 E3-1의 양파 반례가 부활한다: 양파는 신선도 리스크가
    # 0으로 눌려 스코어만 보면 평시에도 100% 중품이 "합리적"으로 보인다. 그 판단을
    # 막는 게 확대 게이트이고, LLM이 그걸 우회할 수 있으면 게이트가 사라진 것이다.
    if rule_ratio <= 0:
        return rule_ratio, None

    candidates = build_mix_candidates(state, cap_ratio, constraints)
    by_id = {candidate_id: ratio for candidate_id, ratio, _ in candidates}
    # 규칙이 고르던 비율에 해당하는 후보를 기본안으로 삼는다. 없으면 LLM을 부르지 않는다 —
    # 고를 목록에 기본안이 없으면 실패 시 돌아갈 자리가 사라진다.
    # 정확 비교다. 후보 비율이 ``cap × fraction``이고 규칙이 채택한 값이 ``cap``이므로
    # ``cap × 1.0``이 정확히 일치한다 — 반올림을 끼우면 그 등식이 깨진다.
    default_id = next((cid for cid, ratio in by_id.items() if ratio == rule_ratio), None)
    if default_id is None or len(candidates) < 2:
        return rule_ratio, None

    signals, facts_text = _mix_signals(facts)
    context = build_mix_context(
        state["item"],
        spread_widened=bool(facts.get("widened")),
        shelf_days=facts.get("shelf_days"),
        shelf_tight=facts.get("cap_ratio", 1.0) < 1.0,
        signals=signals,
        facts=facts_text,
        candidates=[
            MixCandidate(candidate_id=cid, summary=summary)
            for cid, _, summary in candidates
        ],
    )
    decision = selector(context, default_id)
    if decision.candidate_id not in by_id:
        # **서비스 검증기만으로는 부족하다.** selector는 주입 가능한 콜러블이라 그 층을
        # 우회할 수 있고, 실제로 우회하면 비율만 규칙값으로 되돌아가고 결정 객체는 그대로
        # 남아 출력에 "없는 후보를 선택함"이라고 기록된다 — 라벨과 행동이 어긋난다
        # (Codex 교차검증 P2, 재현 확인). 노드가 자기 후보 집합으로 한 번 더 확인한다.
        #
        # ``None``으로 지우지 않는다. 그러면 고지까지 사라져 "판단자가 이상한 값을 줘서
        # 되돌렸다"는 사실이 소비자에게 안 보인다 — 조용히 넘기지 않는 게 이 프로젝트의
        # 규칙이다. 실패로 표시해 ⑥이 risks에 싣게 한다.
        return rule_ratio, replace(
            decision,
            candidate_id=default_id,
            reason="규칙 기본안",
            llm_status="FALLBACK",
            llm_fallback_used=True,
        )
    return by_id[decision.candidate_id], decision


def allocate_sourcing(
    state: PurchaseAgentState, *, selector: MixSelector | None = None
) -> dict[str, Any]:
    """등급 배분 비율을 정한다. **계산이 후보를 만들고 LLM은 고르기만 한다** (E3-1 + E3-2).

    ``evaluate_mid_grade``가 낸 사실들(스프레드·소진 한계일·근접 납품량·스코어)로 규칙이
    후보 집합을 만들고, LLM은 그중 **id 하나**를 고른다. 숫자는 계산이 소유한다 (규칙 6 ·
    §4-⑤ E3-2 확정) — LLM 출력 스키마에 비율 필드가 아예 없어 생성이 타입으로 불가능하다.

    ``selector``를 주입 가능하게 둔 이유: 테스트가 결정적이어야 한다. 기본값 ``None``은
    "LLM 없이 규칙만"이고, 그 경로가 E3-1의 산출물과 **완전히 같다**.
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
    mid_ratio, mix = _select_mix(state, decision, constraints, selector)
    decision["ratio"] = mid_ratio
    decision["mix"] = mix

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
