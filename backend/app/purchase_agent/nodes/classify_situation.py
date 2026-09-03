"""① classify_situation + compute_allowed_axes (계산, LLM 없음) — 상세설계 §4-①."""

import operator
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes._guards import require_positive
from app.purchase_agent.state import PurchaseAgentState

#: ``ci_width_comparison`` 문자열 → 실제 연산. 임계와 비교 방향을 둘 다 파일에서 읽어야
#: 한쪽만 바뀌었을 때 판정이 조용히 뒤집히지 않는다.
_COMPARISONS: dict[str, Callable[[float, float], bool]] = {">=": operator.ge, ">": operator.gt}


def judgment_row(forecast: dict, ci_judgment_day: int) -> dict[str, Any]:
    """판정 기준일의 예측 한 줄. ``daily``가 **D+1부터** 시작하므로 index는 ``day - 1``이다.

    상세설계 §4-①이 D+14 단일로 확정했다. index를 직접 쓰지 않고 이 함수를 거치게 한 이유:
    daily의 시작이 D+0으로 바뀌면 판정이 하루 밀린 채 조용히 돈다 — 고칠 지점을 하나로 모은다.
    """
    daily = forecast["daily"]
    if len(daily) < ci_judgment_day:
        # 지평이 짧으면 IndexError 대신 "무엇이 모자란가"를 말하고 멈춘다.
        raise ValueError(
            f"forecast horizon {len(daily)}일로는 D+{ci_judgment_day} 판정을 할 수 없다"
        )
    return daily[ci_judgment_day - 1]


def compute_ci_width(forecast: dict, ci_judgment_day: int) -> float:
    """``ci_width = (upper − lower) / predicted`` — 판정 기준일 한 줄로 계산한다."""
    row = judgment_row(forecast, ci_judgment_day)
    return (row["upper"] - row["lower"]) / require_positive(row["predicted"], "predicted")


def compute_rise_rate_2w(forecast: dict, ci_judgment_day: int) -> float:
    """2주 후 상승률. 판정 기준일과 **같은 날**을 본다.

    §4-①이 D+14를 고른 근거가 "상황 분류와 상승률이 하나의 질문이 된다"이므로, 두 값이
    다른 날을 보면 그 근거가 깨진다.
    """
    current = require_positive(forecast["current_price"], "current_price")
    return judgment_row(forecast, ci_judgment_day)["predicted"] / current - 1


def is_sustained_rise(forecast: dict, ci_judgment_day: int) -> bool:
    """지속 상승 궤적인가 — 판정일까지 ``predicted``가 단조 증가하는가."""
    predicted = [row["predicted"] for row in forecast["daily"][:ci_judgment_day]]
    return all(earlier < later for earlier, later in pairwise(predicted))


def compute_allowed_axes(state: PurchaseAgentState, situation: str, constraints: dict) -> list[str]:
    """그날 허용되는 ``strategy_type`` 목록 (정의서 §3.5.1 · 상세설계 §4-①).

    규칙이 목록을 계산하고 LLM은 그 안에서만 고른다 — "억지 분할·무의미한 분산"을 원천 차단한다.
    최종 중복 검사(전 안 동일 축이면 반려)는 여기가 아니라 ⑦ self_check 몫이다(§3.5.1-3).
    """
    forecast = state["forecast"]
    day = constraints["situation"]["ci_judgment_day"]
    axes = ["quantity"]  # 수량 축은 항상 허용된다

    # timing: "총량 임계 초과 OR 지속 상승 궤적" 중 하나만 충족해도 열린다.
    # 총량은 ③이 내기 전이라 아직 없으므로, 최대 D(공격)로 만든 **추정 총량**으로 판정한다.
    daily_demand = estimate_daily_demand(state["confirmed_orders"], constraints)
    max_coverage = max(constraints["coverage_days"]["by_label"].values())
    estimated_total_kg = daily_demand * max_coverage
    by_volume = estimated_total_kg >= constraints["triggers"]["split_entry_qty_kg"]
    # 선매입 트리거는 상승률과 구간 폭을 함께 본다 (백로그 임계표) — 구간 폭 조건이 곧 stable이다.
    by_trend = (
        situation == "stable"
        and compute_rise_rate_2w(forecast, day) >= constraints["triggers"]["pre_purchase_rise_rate"]
        and is_sustained_rise(forecast, day)
    )
    if by_volume or by_trend:
        axes.append("timing")

    # mix: 한 품목이 임계 이상을 차지하면 품목 조합의 의미가 사라진다.
    # 현재 배추 81.2% > 0.70이라 자동 제외되고, 편중이 완화되면 코드 변경 없이 부활한다.
    ratios = state["item_mix_ratio"].values()
    if ratios and max(ratios) < constraints["concentration"]["item_threshold"]:
        axes.append("mix")

    return axes


def estimate_daily_demand(confirmed_orders: dict, constraints: dict) -> float:
    """일평균 확정수요 = ``total_kg ÷ order_window_days`` (상세설계 §4-③, Epic 2 확정).

    **안전재고 20%를 곱하지 않는다.** §4-③이 "기존 '확정주문 + 안전재고 20%'는 D≈2.4의
    특수 케이스였고 D 방식이 그 일반화"라고 명시하므로 둘 다 적용하면 이중 계상이다.

    ①과 ③이 같은 식을 써야 해서 여기 둔다 — ①은 timing 축 게이팅용 추정 총량에,
    ③은 안별 수량에 쓴다. 두 곳이 각자 계산하면 축은 열렸는데 수량은 임계 미만인 모순이 난다.
    """
    window = require_positive(constraints["demand"]["order_window_days"], "order_window_days")
    return confirmed_orders["total_kg"] / window


def classify_situation(state: PurchaseAgentState) -> dict[str, Any]:
    """신뢰구간 폭으로 stable/uncertain을 가르고, 그날 허용 축을 계산한다.

    🔴 **"이 예측을 써도 되나"를 우리는 안 묻는다** (실측 2026-09-03).
      ML 이 신뢰도 플래그 셋을 붙여 보내는데 **매입은 하나도 읽지 않는다**::

          use_recommended   이 조합에서 우리 모델이 "어제 값 그대로"보다 나은가
          is_gated          이 행을 판단에 쓰지 말라는 표시
          gate_reason       그 사유 — lead_time(쓸 수 있다) / quality(빼라)

      층이 다르기 때문이다 (#67 본문)::

          ML    use_recommended · is_gated   "이 예측을 쓸 수 있나"   ← 앞
          매입  ci_width                     "얼마나 자신 있나"       ← 뒤

      **앞 질문을 건너뛰고 뒤 질문만 하고 있다.**

    ⚠️ **지금은 안 걸린다 — 우연이 아니라 우리가 AUC 만 보기 때문이다.**

          use_recommended = false   양파 × WHSL(중도매) 하나뿐. AUC 는 세 품목 다 true
          gate_reason = quality     WHSL 에만 있다. AUC 는 lead_time 뿐
          is_gated (AUC)            offset 1~5 에만. 판정일 D+14 는 false

      계열이 늘거나 AUC 가 false 가 되는 날 **아무도 모른다** — 값이 오고 계산도 되니
      에러가 안 난다.

    🔴 **읽고 싶어도 지금은 못 읽는다.** ``ml/service`` 의 ``DailyPoint`` 가
      ``date/predicted/lower/upper`` 넷만 담고, 마스터 ``_FORECAST_ENVELOPE_KEYS`` 에도
      없다. **payload 에 칸이 없다.** 배선이 먼저다 — ``use_recommended`` 처리는
      IO명세 §8 이 #57 로 배정해 뒀다.

    ★ 같은 가족인 ``is_filled`` 는 **다른 방식으로 막아 뒀다** — 판정일이 주(週)의
      배수라 복사값을 안 밟는다(``judgment_row`` · ``test_judgment_day.py``).
      그쪽은 **날짜 선택으로** 피했고, 이 셋은 **아직 안 피했다.**
    """
    constraints = load_constraints()
    rules = constraints["situation"]
    ci_width = compute_ci_width(state["forecast"], rules["ci_judgment_day"])
    exceeds = _COMPARISONS[rules["ci_width_comparison"]]
    situation = "uncertain" if exceeds(ci_width, rules["ci_width_threshold"]) else "stable"
    return {
        "situation": situation,
        "allowed_axes": compute_allowed_axes(state, situation, constraints),
    }
