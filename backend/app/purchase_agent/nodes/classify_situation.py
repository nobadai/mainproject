"""① classify_situation + compute_allowed_axes (계산, LLM 없음) — 상세설계 §4-①."""

import operator
from collections.abc import Callable, Mapping
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

    🔴 **D+14는 바꿔도 되는 값이 아니다 — 주(週)의 배수여야 한다.**
      장이 안 서는 날은 직전 개장일 값이 복사되고 **예측 구간까지 복사되므로**, 주기를
      벗어난 날을 고르면 그날이 아니라 **전 장날의 불확실성**을 재게 된다.
      **근거와 실측표는 ``constraints.yaml`` 의 ``situation.ci_judgment_day`` 에 있다** —
      여기 옮겨 적지 않는다(한쪽만 바뀐다). 잠그는 검사는 ``test_judgment_day.py``.

      ⚠️ 이 제약은 **2026-08-27 #57 코멘트로 이미 들어와 있었고 6일간 코드에 안 옮겨져
      있었다.** 2026-09-03 에 실측으로 확인하고 검사로 잠갔다.
    """
    daily = forecast["daily"]
    if len(daily) < ci_judgment_day:
        # 지평이 짧으면 IndexError 대신 "무엇이 모자란가"를 말하고 멈춘다.
        raise ValueError(
            f"forecast horizon {len(daily)}일로는 D+{ci_judgment_day} 판정을 할 수 없다"
        )
    return daily[ci_judgment_day - 1]


#: 판단에서 **빼야 하는** 게이트 사유. ML 회신 2026-08-27 (#57 코멘트 09:49) ::
#:
#:     quality      → 제외    값 자체를 못 믿는다
#:     lead_time    → 사용    "값이 나쁜 게 아니라 어제 가격이 이미 정답에 가까운 구간"
#:     None         → 사용    게이트가 안 걸렸다
#:
#: 🔴 **부분 문자열로 본다.** 표에 ``lead_time+quality`` 복합값이 25건 있어
#:   ``== "quality"`` 로 비교하면 그 25건을 놓친다 (실측 2026-09-03).
EXCLUDED_GATE_REASON = "quality"


def is_gate_excluded(row: Mapping[str, Any]) -> bool:
    """이 예측 행을 판단에서 빼야 하나 — **``gate_reason`` 으로만 본다.**

    🔴 **``is_gated`` 를 안 본다.** ML 이 *"둘은 다른 축"* 이라고 확정했다 (ⓒ · 8/27) —
      ``is_gated`` 는 **출처**(모델이 냈나, 어제 가격을 그대로 썼나)이고
      ``use_recommended`` 는 **사용 권고**다. 게이트됐다는 것 자체는 배제 사유가 아니다.

    ⚠️ **``is_gated`` 로 걸렀다면 터졌다.** 실측(3품목 × 7배치)::

          보수(D=2) 창 21개  →  전부 100% gated (AUC 는 offset 1~5 가 lead_time)
          gated 를 빼면      →  max_price 가 21조합에서 None

      ``max_price`` 는 컷 기준이라(규칙 5) ``None`` 이면 **보수안이 통째로 판정
      불가**가 된다. 사유를 안 보고 표시만 봤을 때 생기는 일이다.

    ★ **값이 없으면 제외하지 않는다** (규칙 3). mock 예측에는 이 칸이 아예 없고,
      *"게이트 정보가 없다"* 와 *"게이트가 quality 다"* 는 다른 사실이다.
    """
    reason = row.get("gate_reason")
    return isinstance(reason, str) and EXCLUDED_GATE_REASON in reason


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

    🟢 **"이 예측을 써도 되나"를 먼저 묻는다** (#213 · 2026-09-04).

      ML 이 신뢰도 플래그 넷을 붙여 보내고 **넷 다 payload 에 온다**::

          use_recommended   조합(품목 × 계열)별   forecast 최상위    이 예측을 쓸 수 있나
          gate_reason       행(offset)별         daily 원소 안      왜 게이트됐나
          is_gated          행별                 daily 원소 안      출처 (모델 vs 어제 값)
          is_filled         행별                 daily 원소 안      장이 안 선 날의 복사값

      층이 다르다 (#67 본문)::

          ML    use_recommended · gate_reason   "이 예측을 쓸 수 있나"   ← 앞
          매입  ci_width                        "얼마나 자신 있나"       ← 뒤

      **앞 질문은 어댑터가 한다** (``adapter.validate_forecast``). 여기까지 온
      예측은 이미 *"써도 된다"* 가 확인된 것이라, 이 노드는 뒤 질문만 한다.

    🔴 **이 자리에 "셋 다 읽고 싶어도 못 읽는다" 고 적었었다 (2026-09-04 정정).**

      그때 적은 순서는 이랬다::

          ①  뷰       daily 에 gate_reason 을 더한다        ✅ #220 (09-03 19:20)
          ②  마스터   use_recommended 를 나른다             ✅ #208 (09-03 17:50)
          ③  매입     읽어서 판정 앞에 건다                  ← 이 판

      ①②는 **우리가 "못 읽는다"고 적던 그날 남이 이미 끝냈다.** 우리 정정 커밋이
      ②보다 19분 늦었고, 남이 고친 것을 안 보고 우리 판단을 옮겨 적었다.
      ⚠️ 그리고 ②의 처방도 틀렸었다 — 마스터는 ``_FORECAST_ENVELOPE_KEYS`` 가 아니라
      ``_forecast_payload`` 에 넣었다. 앞은 *"ML 봉투에서 내려보내는 필드"* 라
      **ML 이 안 보낸 키를 얹으면 받는 쪽이 ML 이 준 것으로 읽는다.**

    ⚠️ **지금은 아무것도 안 걸린다 — 우연이 아니라 우리가 AUC 만 보기 때문이다.**
      실측 3품목 × 7배치 = 21조합 (2026-09-04)::

          use_recommended = false   양파 × WHSL 하나뿐. AUC 는 21조합 다 true
          gate_reason = quality     WHSL 에만 101건. AUC 는 lead_time 75건뿐
          판정일(D+14) is_gated     21조합 다 false
          판정일(D+14) is_filled    21조합 다 false

      **계열이 늘거나 AUC 에 quality 가 생기는 날 자리가 이미 있다.** 값이 오고
      계산도 되니 에러가 안 나는 종류라, 그날 아무도 모르는 것이 원래 문제였다.

    ★ ``is_filled`` 는 **판정에 안 쓴다.** 판정일이 주(週)의 배수라 복사값을 안 밟고
      (``judgment_row`` · ``test_judgment_day.py``), ML 도 이 값으로 무엇을 하라는
      지시를 준 적이 없다. 대신 ``max_price`` 창에는 섞이므로 ⑥이 고지만 붙인다
      (``package_scenarios._forecast_risks``).
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
