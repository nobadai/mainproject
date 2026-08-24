"""그래프가 들고 다니는 상태와 그 초기값 (상세설계 v1.1 §3).

**필드는 §3 State 정의를 그대로 옮긴 것이다.** 임의로 늘리지 않는다 — 이 State는 노드끼리만
쓰는 내부 구조가 아니라 설계 문서가 규정한 계약이고, 필드가 늘면 어느 노드가 무엇을 읽는지
문서로 추적할 수 없게 된다.

**노드는 State 전체가 아니라 바꿀 키만 담은 dict를 반환한다** — LangGraph 런타임이 병합한다.
그래서 이 TypedDict가 ``total=True``인데도 부분 갱신이 성립한다.

**ports는 여기서 한 번만 부른다.** ``ports.py`` docstring이 약속한 "T0 스냅샷 생성 시점에만
호출되고 T1 이후 노드는 스냅샷에서 읽는다"가 이 모듈이다. 노드가 직접 ports를 부르면
같은 사이클 안에서 값이 달라질 수 있고(T0~T2 시점 차), 그러면 사중 일치가 무너진다.
"""

from datetime import date
from typing import Literal, TypedDict

from app.purchase_agent import ports
from app.purchase_agent.config import load_constraints


class PurchaseAgentState(TypedDict):
    """상세설계 §3. 주석의 ★는 문서 원문 표기를 그대로 옮긴 것이다."""

    # ── 입력 (T0 스냅샷에서 주입) ───────────────────────────────────────────
    date: str  # as_of. 노드는 이 값만 보고, 벽시계를 읽지 않는다 (규칙 1)
    item: Literal["배추", "무", "피마늘", "양파"]
    forecast: dict  # 경락가 예측 (daily는 D+1 ~ D+18)
    market_quotes: list[dict]  # 가락 등급별 당일 경락가
    inventory: dict
    confirmed_orders: dict
    item_mix_ratio: dict  # ★ 품목 비중 (mix 축 게이팅용)
    contract_price: float  # ★ 계약단가 (참조값 — 마진 표시용, 컷 아님)
    margin_defense_floor_rate: float  # ★ 구간별 방어선 (참조값)
    projected_cash_min: int  # ★ 향후 N일 최저 현금
    feedback: dict | None  # 오케스트레이터 재조정 요청 (§6, 전부 기각 시만)

    # ── 중간 산출 ───────────────────────────────────────────────────────────
    situation: Literal["stable", "uncertain"]
    context_docs: list[dict]  # 주입된 문서 (published_at <= as_of)
    context_loop_count: int  # max 3
    allowed_axes: list[str]  # ★ 그날 허용 strategy_type (규칙 계산)
    coverage_days: int  # ★ 커버일수 D
    base_plan: dict  # 수량·타이밍 초안
    split_plan: list[dict] | None  # 분할 계획 (timing 축)
    sourcing_plan: list[dict]  # 등급 배분

    # ── 출력 ────────────────────────────────────────────────────────────────
    scenarios_final: list[dict]
    confidence: Literal["high", "medium", "low"]
    rejected_reasons: list[dict]  # {label, reason} — 출력 스키마와 동일 형 (v1.1 정정)
    proposal: dict | None  # ⑦이 조립·재검증한 최종 산출물


def build_initial_state(
    item: str, as_of: date, *, feedback: dict | None = None
) -> PurchaseAgentState:
    """T0 스냅샷을 만든다 — 6개 포트를 각각 한 번씩만 호출한다.

``item_mix_ratio`` · ``contract_price`` · ``margin_defense_floor_rate``는 IO명세 §1의
    계약 포트 6개에 없다. 그래도 **외부 입력이므로 ports를 거친다**(규칙 2) —
    ``get_snapshot_extras``가 그 잠정 경계이고, 스냅샷 형식이 확정되면 거기서만 바뀐다.

    중간 산출 필드는 채우지 않는다 — 각 노드가 자기 몫을 반환한다. 다만 ``context_docs``와
    ``context_loop_count``는 stable 경로에서 ② 노드를 건너뛰므로 빈 값으로 시작해야 하고,
    ``rejected_reasons``는 어느 노드든 append할 수 있어야 하므로 빈 목록으로 둔다.
    ``coverage_days``·``situation`` 같은 값은 **0이나 빈 문자열로 채우지 않는다** — 미결과
    확정된 값을 구분해야 하기 때문이다 (규칙 3).
    """
    constraints = load_constraints()
    # 창·지평을 파라미터로 받지 않는다. 여기서 쓴 창과 ③이 나눌 창이 달라지면 수량이
    # 조용히 틀어지므로, 양쪽 모두 constraints.yaml 한 곳에서 읽는다 (규칙 7).
    order_days = constraints["demand"]["order_window_days"]
    cash_horizon_days = constraints["cash"]["horizon_days"]
    extras = ports.get_snapshot_extras(item, as_of)
    return {  # type: ignore[return-value]  # 중간·출력 필드는 노드가 채운다
        "date": as_of.isoformat(),
        "item": item,
        "forecast": ports.get_forecast(item, as_of),
        "market_quotes": ports.get_market_quotes(item, as_of),
        "inventory": ports.get_inventory(item, as_of),
        "confirmed_orders": ports.get_confirmed_orders(item, as_of, days=order_days),
        "item_mix_ratio": extras["item_mix_ratio"],
        "contract_price": extras["contract_price"],
        "margin_defense_floor_rate": extras["margin_defense_floor_rate"],
        "projected_cash_min": ports.get_projected_cash_min(as_of, cash_horizon_days),
        "feedback": feedback,
        "context_docs": [],
        "context_loop_count": 0,
        "rejected_reasons": [],
        "proposal": None,
    }
