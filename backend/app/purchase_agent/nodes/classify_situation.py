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

    🔴 **못 읽는 것은 둘뿐이다 — ``is_gated`` 는 이미 와 있다** (실측 2026-09-03)::

          use_recommended   뷰가 내지만 payload 가 안 고른다      ← 마스터 몫
          is_gated          ✅ **온다** — daily 원소 안에 있다     ← 우리가 안 읽을 뿐
          gate_reason       표엔 있고 **뷰가 daily 에 안 넣는다**  ← 뷰 몫

      ``v_ml_price_forecast`` 의 ``daily`` 원소는 여섯 칸이다 (뷰 DDL
      ``database/10_domain_schema.sql``)::

          date · predicted · lower · upper · is_filled · is_gated

      ``master/inputs.py`` 의 ``_forecast_payload`` 가 *"키를 고르기만 하고 값은
      손대지 않는다"* 며 ``daily`` 를 통째로 넘기므로, **우리 State 까지 그대로
      온다.** 12-31 배추 관통 실측::

          state["forecast"]["daily"][13] =
            {"date": "2026-01-14", "lower": 604, "predicted": 761, "upper": 1107,
             "is_filled": false, "is_gated": false}

      ``gate_reason`` 만 다르다. 표 ``ml_price_forecasts`` 에는 컬럼이 있는데
      (AUC 실측: ``lead_time`` 75행 · ``NULL`` 303행) 뷰의 ``jsonb_build_object``
      가 그 칸을 안 만든다 — 고칠 자리는 ML 이 아니라 **뷰**다.

    🔴 **이 자리에 셋 다 "읽고 싶어도 못 읽는다" 고 적었었다 (2026-09-03 정정).**

      틀린 표는 이랬다::

          is_gated          행(offset)별   DailyPoint   ← ML 몫
          gate_reason       행(offset)별   DailyPoint   ← ML 몫

      **``DailyPoint`` 를 근거로 삼은 것이 틀렸다.** 그 모델(``app/ml/schemas.py``)
      은 네 칸에 ``extra="forbid"`` 라 실제로 ``is_gated`` 를 못 담는다 — 그건 맞다.
      다만 **마스터는 그 모델을 안 거친다.** 뷰를 직접 읽어 ``daily`` 를 그대로
      나른다(현서님 지적 2026-09-03). 없는 경로의 한계를 보고 "안 온다"고 적었다.

      ⚠️ **한 판 앞에서 같은 실수를 이미 고쳤었다.** *"payload 에 칸이 없다"* 를
        층별로 가르면서, 가른 뒤에도 **실제로 오는지는 안 재봤다.** 층을 나눈 것이
        곧 확인은 아니다.

      순서가 있다 (정정판)::

          ①  뷰       daily 에 gate_reason 을 더한다
          ②  마스터   use_recommended 를 _FORECAST_ENVELOPE_KEYS 에 더한다
          ③  매입     is_gated 는 **지금 당장** 읽을 수 있다 — 판정 **앞에** 건다

      🟢 **③이 ①②를 안 기다린다.** ``is_gated`` 하나만으로도 *"이 행을 쓰지 말라"*
        는 표시는 읽힌다. 사유(``gate_reason``)와 조합 판정(``use_recommended``)이
        붙으면 더 정확해질 뿐이다. 남을 기다릴 이유가 없다.

      ``use_recommended`` 처리는 IO명세 §8 이 **#57** 로 배정해 뒀다.

      ⚠️ **#57 은 본문이 아니라 코멘트에 있다.** 본문(*"rise_rate 분모를 당일 시세
        조회로 전환"*)에는 ``use_recommended`` 가 **0건**이라, 제목만 보면 딴 이슈로
        읽힌다. 실제 배정은 코멘트 넷이다::

            2026-08-27 05:23   "착수 시 수신 검증에 is_filled(판정일)·use_recommended 처리 포함"
            2026-08-27 08:45   ML 실계약 확정 — 같은 문장
            2026-08-27 09:20   "is_filled 확정 편입 … use_recommended 와 함께 수신 검증에 포함"
            2026-09-01 04:48   남는 작업 셋 — "is_filled(판정일 복사값) · use_recommended 처리"

    ★ 같은 가족인 ``is_filled`` 는 **다른 방식으로 막아 뒀다** — 판정일이 주(週)의
      배수라 복사값을 안 밟는다(``judgment_row`` · ``test_judgment_day.py``).
      그쪽은 **날짜 선택으로** 피했고, 나머지는 **아직 안 피했다.**

      ⚠️ 이 자리에 *"이 셋은 아직 안 피했다"* 라고 적었었다 (2026-09-03 정정).
        ``is_gated`` 가 위에서 빠지므로 남는 것은 둘이다.

      🔴 **``is_filled`` 도 payload 에 온다** — 날짜 선택은 *"안 밟는다"* 이지
        *"못 본다"* 가 아니었다. 지금은 **아무도 안 본다**::

            판정일이 복사값인가   is_filled 로 그 자리에서 볼 수 있다   ← 안 본다
            그런 배치가 오는가    -m db 검사가 잰다                     ← 사람이 손으로

        날짜 선택은 **오늘의 데이터**가 안전하다는 것이고, 새 ``base_dt`` 가 그 가정을
        깨는 날은 사람이 ``-m db`` 를 돌려야 안다(``test_judgment_day.py`` 머리말).
        값이 payload 에 있으므로 그 창은 런타임에서 닫을 수 있다 — ``is_gated`` ③과
        같은 자리이고, 둘 다 *"읽기만 하면 되는데 안 읽는다"* 다.
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
