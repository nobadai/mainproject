"""① classify_situation + compute_allowed_axes (계산, LLM 없음) — 상세설계 §4-①."""

import operator
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, NamedTuple

from app.purchase_agent.config import ci_width_threshold, load_constraints
from app.purchase_agent.nodes._guards import pending_value, require_positive
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

      ``max_price`` 는 재무 상한이라(규칙 5) ``None`` 이면 **보수안이 통째로 판정
      불가**가 된다. 사유를 안 보고 표시만 봤을 때 생기는 일이다.
      🔴 컷 기준이 아니다 — 컷은 ``cut_unit_price`` 가 한다 (`#394` 로 갈라졌다).

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

    🔴 **분모 ``current_price`` 는 오늘 시세가 아니다 — 모델의 출발점(앵커)이다**
      (0.4×어제 + 0.6×최근 7 거래일 평균 · ML 회신 2026-09-10). **시세로 바꾸지 마라.**
      ``predicted`` 가 그 앵커에서 출발하므로 **같은 기준선끼리 비교하는 것**이고, 당일
      시세를 넣으면 출처가 다른 두 시리즈가 섞여 배추 기준 12.2%p 갈린다. 산식과 재현값은
      ``quotes.py`` 머리말에 있다.
    """
    current = require_positive(forecast["current_price"], "current_price")
    return judgment_row(forecast, ci_judgment_day)["predicted"] / current - 1


def is_sustained_rise(forecast: dict, ci_judgment_day: int) -> bool:
    """지속 상승 궤적인가 — 판정일까지 ``predicted``가 단조 증가하는가."""
    predicted = [row["predicted"] for row in forecast["daily"][:ci_judgment_day]]
    return all(earlier < later for earlier, later in pairwise(predicted))


def coverage_by_label(situation: str, constraints: dict) -> dict[str, int]:
    """그날 **실제로 만들 안**과 그 커버일수 D (상세설계 §4-③ · 규칙 4).

    ``uncertain`` 이면 공격안을 빼고 돌려준다 — 구간이 넓은 날엔 선매입을 제안하지
    않는다.

    ★ **①과 ③이 같은 목록을 써야 해서 여기 둔다.** ``estimate_daily_demand`` 와 같은
      자리이고 이유도 같다: ①은 timing 축 게이팅용 추정 총량에, ③은 만들 안 목록에
      쓴다. **두 곳이 각자 판단하면 축은 열렸는데 그 안이 없는 모순이 난다** — 실제로
      그랬다 (`#340`).

    🔴 **③이 정본인데 물리적으로는 여기 있다.** ``draft_plan`` 이 이미 이 모듈을
      import 하므로 (``estimate_daily_demand``) 반대로 두면 순환이 된다. 도메인
      규칙의 주인은 ③이고, 이 함수는 그 규칙을 **한 곳에 적어 둔 것**이다.

    ⚠️ 순서를 보존한다 — ``by_label`` 의 선언 순서가 곧 안이 나가는 순서이고,
      ⑥ ``assign_axes`` 가 ``labels[-1]`` 로 마지막 안을 집는다.

    ⚠️ *"공격"* 은 ③이 쓰던 그대로 여기 적는다. 선언(`constraints.yaml`)으로 빼는
      것이 규칙 7에 맞지만 **이 판의 목적은 두 곳이 갈리지 않게 하는 것**이라
      범위를 넓히지 않는다 — 그때는 ③·⑥의 ``aggressive_axis`` 와 함께 본다.
    """
    return {
        label: days
        for label, days in constraints["coverage_days"]["by_label"].items()
        if not (situation == "uncertain" and label == "공격")
    }


#: 분할 진입을 **판정하지 못한** 사유. 문장을 상수로 두는 이유는 ⑦ ``ARRIVAL_SKIP_REASONS``
#: 와 같다 — ①이 판정하고 ③이 고지하므로, 문면이 두 곳에 흩어지면 한쪽만 바뀐다.
#:
#: ⚠️ **「창 밖」 갈래를 두지 않는다.** ⑦ ``_unknown_reason`` 은 누락과 창 밖을 가르는데,
#:   여기가 보는 날짜는 **창의 첫날**(물류 ``build_cap_window`` 의 ``start = as_of + N4``)
#:   하나라 창 밖이 될 수 없다. 갈래를 만들면 **일어나지 않는 사유**가 문장으로 남는다.
SPLIT_ENTRY_UNKNOWN = {
    "no_lead": (
        "그날 분할 진입 조건(도착일 창고 여유)을 판정하지 않았다 — 입고 소요일이 정해지지 "
        "않아 도착일을 계산할 수 없다. 여유를 0으로 가정하지 않았다"
    ),
    "no_cap": (
        "그날 분할 진입 조건(도착일 창고 여유)을 판정하지 않았다 — 물류에서 날짜별 입고 "
        "여유를 받지 못했다. 여유를 0으로 가정하지 않았다"
    ),
    "missing": (
        "그날 분할 진입 조건(도착일 창고 여유)을 판정하지 않았다 — 받은 날짜별 여유에 "
        "도착일 {day} 칸이 없다. 여유를 0으로 가정하지 않았다"
    ),
}


class SplitEntryCap(NamedTuple):
    """분할 진입의 기준값 — **도착일 하루의 창고 여유**. 값과 "못 봤다"를 나눠 담는다.

    ⑦ ``ArrivalCapacity`` 와 같은 모양이고 이유도 같다: 한 값으로 뭉치면 호출부가
    *"이게 판정인가 미판정인가"* 를 문면으로 가르게 되고, 문구를 다듬는 날 판정이
    조용히 뒤집힌다.
    """

    cap_kg: float | None = None
    """그 도착일에 물류가 받아 줄 수 있는 양. ``None`` 이면 판정하지 않는다."""

    arrival_date: str | None = None
    """``as_of + N4``. N4 가 미결이면 ``None`` 이다."""

    unknown_reason: str | None = None
    """값을 못 본 사유. 채워지면 ③이 risks 에 싣는다 — 컷 사유가 아니다."""


def split_entry_cap(state: PurchaseAgentState, constraints: dict) -> SplitEntryCap:
    """분할 진입 임계 = ``cap_by_date[as_of + N4]`` (물류 회신 2026-09-11 · ``#308``).

    ★ **왜 고정 수(``20,000kg``)가 아닌가.** 그 수는 *"이만큼 크면 나눠 사자"* 였는데,
      나눠야 하는 진짜 이유는 크기가 아니라 **하루에 다 못 들어간다**는 것이다. 실측에서
      그 임계는 **한 번도 안 섰다** — 품목별 최대가 배추 8,727 · 무 9,429 · 양파
      10,286kg 이라 전부 미만이고, 관통 672셀 중 진입은 1건뿐이었다 (2026-09-12).
      기준을 도착일 여유로 바꾸면 *"그날 들어갈 자리가 없으니 날짜를 나눈다"* 가 된다.

    🟢 **물류가 동의한 형태다.** 다만 조건 셋이 붙었다 — ``cap_by_date`` 는 물류 정본으로만
      두고, **회차 수·실행 계획은 매입이 정하며**, 나눈 뒤 각 회차 도착일을 다시
      ``cap_by_date`` 로 검증한다. 셋째는 이미 서 있다 (⑥ ``cap_constrained_quantities`` ·
      ⑦ ``check_arrival_capacity``).

    🔴 **모르면 안 연다** (규칙 3). 여유를 못 받았거나 N4 가 미결이면 ``0`` 으로 채우지
      않는다 — ``0`` 으로 채우면 «그날 한 톨도 안 들어간다» 가 되어 **모든 날 분할이
      열린다.** 미결은 판정을 막아야지 판정을 만들면 안 된다.

    ⚠️ **``0`` 은 채우는 값이 아니라 받은 값일 수 있다.** 관통 2,743셀 중 **1,620셀**이
      도착일 여유 ``0`` 이고 (2026-09-12 실측), 그것은 확정된 0이라 판정 대상이다 —
      그날은 어떤 양도 안 들어가므로 진입 조건이 선다. 다만 그 1,620셀은 **전부 안이
      0개인 보류일**이라(``E2_HELD``) 실제로 열릴 안이 없다.
    """
    lead_days = pending_value(state, constraints, "inbound_lead_days")
    if lead_days is None:
        return SplitEntryCap(unknown_reason=SPLIT_ENTRY_UNKNOWN["no_lead"])
    arrival = (date.fromisoformat(state["date"]) + timedelta(days=int(lead_days))).isoformat()
    cap_by_date = (state.get("inventory") or {}).get("cap_by_date")
    if cap_by_date is None:
        return SplitEntryCap(arrival_date=arrival, unknown_reason=SPLIT_ENTRY_UNKNOWN["no_cap"])
    cap = cap_by_date.get(arrival)
    if cap is None:
        return SplitEntryCap(
            arrival_date=arrival,
            unknown_reason=SPLIT_ENTRY_UNKNOWN["missing"].format(day=arrival),
        )
    return SplitEntryCap(cap_kg=float(cap), arrival_date=arrival)


def compute_allowed_axes(state: PurchaseAgentState, situation: str, constraints: dict) -> list[str]:
    """그날 허용되는 ``strategy_type`` 목록 (정의서 §3.5.1 · 상세설계 §4-①).

    규칙이 목록을 계산하고 LLM은 그 안에서만 고른다 — "억지 분할·무의미한 분산"을 원천 차단한다.
    최종 중복 검사(전 안 동일 축이면 반려)는 여기가 아니라 ⑦ self_check 몫이다(§3.5.1-3).
    """
    forecast = state["forecast"]
    day = constraints["situation"]["ci_judgment_day"]
    axes = ["quantity"]  # 수량 축은 항상 허용된다

    # timing: "도착일에 다 안 들어감 OR 지속 상승 궤적" 중 하나만 충족해도 열린다.
    #
    # 🔴 **앞 조건이 「총량 임계 초과」에서 바뀌었다** (`#308` · 2026-09-12). 나눠 사야 하는
    #   이유는 «크다» 가 아니라 «하루에 다 못 들어간다» 라, 기준을 물류가 낸 도착일
    #   여유로 옮겼다 — 근거·실측은 ``split_entry_cap`` docstring 에 있다.
    #
    # 총량은 ③이 내기 전이라 아직 없으므로, **그날 실제로 만들 안들** 중 최대 D 로
    # 만든 추정 총량으로 판정한다.
    #
    # ⚠️ **uncertain 이면 공격 라벨을 뺀다** (2026-09-07 · `#340`).
    #
    #   전에는 ``max(by_label)`` 을 그냥 썼다. 그러면 공격안이 없는 날에도 D=12 로
    #   재서 추정 총량이 **실제의 2.4배**가 된다::
    #
    #       ① 8,608kg  (717.3 × 12)
    #       ③ 3,587kg  (717.3 × 5)   ← 실제로 만드는 안
    #
    #   ★ **③(draft_plan)이 정본이다** — 그쪽이 *"구간이 넓은 날엔 공격안을 만들지
    #     않는다"* 를 이미 적었고, ①이 그걸 안 봤다.
    #
    #   🔴 그리고 이 값이 ``by_volume`` 에만 쓰인다. ``by_trend`` 는 아래 세 줄에서
    #     ``situation`` 을 이미 쓰고 있었다 — 순서 문제가 아니었다.
    daily_demand = estimate_daily_demand(state["confirmed_orders"], constraints)
    max_coverage = max(coverage_by_label(situation, constraints).values())
    estimated_total_kg = daily_demand * max_coverage
    # 🔴 **못 보면 안 연다** (규칙 3). 여유를 0으로 채우면 «그날 한 톨도 안 들어간다» 가
    #   되어 **모든 날 축이 열린다** — 미결이 판정을 만드는 자리다. 못 본 사실은 ③이
    #   risks 로 고지한다 (``_deferred_checks`` · ``SPLIT_ENTRY_UNKNOWN``).
    arrival_cap = split_entry_cap(state, constraints)
    by_volume = arrival_cap.cap_kg is not None and estimated_total_kg >= arrival_cap.cap_kg
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
                                    🔴 504조합에서는 27건이 true — 전부 공휴일이다 (#384)

      **계열이 늘거나 AUC 에 quality 가 생기는 날 자리가 이미 있다.** 값이 오고
      계산도 되니 에러가 안 나는 종류라, 그날 아무도 모르는 것이 원래 문제였다.

    ★ ``is_filled`` 는 **판정에 안 쓴다 — 다만 고지는 한다** (2026-09-07 정정 · ``#384``).

      전에는 *"판정일이 주(週)의 배수라 복사값을 안 밟는다"* 를 근거로 고지도 안 했다.
      🔴 **그 근거가 위 21조합에서만 참이었다.** 504조합으로 넓히니 D+14 에 **27건**이
      복사값이고, 전부 **``target_dt`` 가 공휴일**이다 (``base_dt`` 는 정상 개장일).

      ★ 주기 가정 자체는 살아 있다 — ``base_dt + 14`` 는 같은 요일이라 **주말**을
        안 밟는다. 다만 **공휴일은 요일과 무관하다.**

      ⚠️ 그래도 **판정에는 안 쓴다.** 복사값이라고 틀린 값이 아니고, ML 이 이 값으로
        무엇을 하라는 지시를 준 적도 없다. ⑥이 문장만 붙인다
        (``package_scenarios._judgment_day_risks`` · ``max_price`` 창은 별도로 ``_forecast_risks``).
    """
    constraints = load_constraints()
    rules = constraints["situation"]
    # 🔴 **임계는 품목별이다** — 그래서 이 노드가 ``state["item"]`` 을 읽는다.
    #   전에는 안 읽었다. 품목이 판정에 안 들어가던 시절의 흔적이고, 임계가 갈리는
    #   순간부터는 **어느 품목의 임계인지**가 판정의 일부다.
    #   없으면 기본값으로 안 떨어지고 멈춘다 (``ThresholdNotDeclared`` · 규칙 3).
    #   여기까지 온 요청은 어댑터가 문 앞에서 같은 조건을 이미 봤다
    #   (``adapter.validate_payload`` → ``RUNTIME_NOT_READY``) — 이 줄은 그 뒤의
    #   백스톱이다. mock 경로(``build_initial_state``)는 문을 안 지나므로 여기서 처음 걸린다.
    threshold = ci_width_threshold(state["item"], constraints)
    ci_width = compute_ci_width(state["forecast"], rules["ci_judgment_day"])
    exceeds = _COMPARISONS[rules["ci_width_comparison"]]
    situation = "uncertain" if exceeds(ci_width, threshold) else "stable"
    return {
        "situation": situation,
        "allowed_axes": compute_allowed_axes(state, situation, constraints),
    }
