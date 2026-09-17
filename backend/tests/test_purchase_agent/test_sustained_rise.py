"""지속 상승 판정 — 정의 교체 (2026-09-17 · 정책 결정 충환) 를 잠근다.

```text
기간     D+1..D+14 (ci_judgment_day)
출발점   current_price (ML 앵커)
지점     앵커 + (is_filled 아님 AND quality 게이트 아님) 행 · lead_time 게이트 행은 넣는다
판정     앵커 외 지점 < 최소 수 → 보류 · 실제 하락 → 탈락 · 마지막 <= 앵커 → 보합 · 그 밖 → 통과
공용     ① 축(가격 경로)과 ④ 진입이 같은 함수를 부른다
```

★ **실제 기록 fixture 넷 + 합성 입력**으로 잰다. fixture 는 ``v_ml_price_forecast`` 당일 배치를
  읽기만 해서 옮긴 것이고(``fixtures/sustained_rise_real_windows.json`` · 값 무수정), 기대값은
  **이 파일에 사람이 읽은 사실로** 적는다 — 판정 함수가 낸 답을 정답으로 옮겨 적지 않는다.

🔴 **이 검사가 재는 것은 «정의대로 도는가» 다.** 경제성 · 분할의 우수성은 재지 않는다.
"""

import copy
import json
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes import classify_situation as 축_모듈
from app.purchase_agent.nodes import split_plan as 진입_모듈
from app.purchase_agent.nodes.classify_situation import (
    TREND_DECLINED,
    TREND_NO_NET_RISE,
    TREND_RISING,
    TREND_WITHHELD,
    WITHHELD_INSUFFICIENT_POINTS,
    WITHHELD_MISSING_ANCHOR,
    WITHHELD_MISSING_VALUE,
    WITHHELD_SHORT_HORIZON,
    SustainedRise,
    compute_allowed_axes,
    compute_rise_rate_2w,
    judge_sustained_rise,
    sustained_rise_sentence,
)
from app.purchase_agent.nodes.package_scenarios import _entry_miss_reason
from app.purchase_agent.nodes.split_plan import evaluate_split_entry
from app.purchase_agent.schemas import TIMING_AXIS
from app.purchase_agent.state import build_initial_state

FIXTURE = Path(__file__).parent / "fixtures" / "sustained_rise_real_windows.json"
MOCK_AS_OF = date(2026, 8, 21)


def _실제(as_of: str, item: str) -> dict:
    windows = json.loads(FIXTURE.read_text(encoding="utf-8"))["windows"]
    [window] = [w for w in windows if w["as_of"] == as_of and w["item"] == item]
    return {k: v for k, v in window.items() if not k.startswith("_")}


def _합성(
    values: list[float | None],
    *,
    anchor: float | None = 100,
    filled: set[int] = frozenset(),
    gates: dict[int, str] | None = None,
    horizon: int = 18,
) -> dict:
    """D+1 부터의 예측값. 모자란 뒤쪽은 마지막 값으로 채운다 (창 밖이라 판정에 안 닿게)."""
    gates = gates or {}
    padded = list(values) + [values[-1]] * (horizon - len(values))
    daily = [
        {
            "date": (MOCK_AS_OF + timedelta(days=k)).isoformat(),
            "predicted": value,
            "lower": None if value is None else value * 0.97,
            "upper": None if value is None else value * 1.03,
            "is_filled": k in filled,
            "is_gated": k in gates,
            "gate_reason": gates.get(k),
        }
        for k, value in enumerate(padded, start=1)
    ]
    return {"current_price": anchor, "daily": daily, "horizon_days": horizon}


@pytest.fixture(scope="module")
def constraints() -> dict:
    return load_constraints()


def _옛_정의(forecast: dict, day: int) -> bool:
    """교체 전 ``is_sustained_rise`` — 달력 14행 엄격 증가. **비교 기준으로만** 옮겨 적는다."""
    return all(a < b for a, b in pairwise(r["predicted"] for r in forecast["daily"][:day]))


# ── 실제 기록 fixture ───────────────────────────────────────────────────────


def test_실제_기록_복사행과_보합이_섞여도_앵커에서_오르면_지속_상승(constraints) -> None:
    """2026-03-27 무 (걷기 판단 507건 중 · 기록 stable) — **옛 정의는 거짓, 새 정의는 참.**

    앵커 442 · lead_time 게이트 넷(= 442) · 복사 행 넷 · 모델 예측 455 → 456 보합·상승.
    옛 정의는 복사 행과 게이트 행이 앞 행과 같은 값이라 ``<`` 가 구조적으로 거짓이었다.
    """
    forecast = _실제("2026-03-27", "무")
    day = constraints["situation"]["ci_judgment_day"]
    result = judge_sustained_rise(forecast, constraints)
    assert _옛_정의(forecast, day) is False
    assert result.verdict == TREND_RISING and result.holds
    assert result.anchor == 442
    filled_dates = {r["date"] for r in forecast["daily"][:day] if r["is_filled"]}
    assert filled_dates == {"2026-03-28", "2026-03-29", "2026-04-04", "2026-04-05"}
    assert not filled_dates & {d for d, _ in result.points}, "복사 행이 지점에 들어갔다"
    assert len(result.points) == 10
    # lead_time 게이트 행 두 개(03-30 · 03-31)는 지점에 **남는다** — 기존 합의
    assert [v for d, v in result.points if d in ("2026-03-30", "2026-03-31")] == [442, 442]
    # 상승률은 이 판정과 **따로** 본다 — +3.2% 라 ① 가격 경로는 임계에서 닫힌다
    assert compute_rise_rate_2w(forecast, day) < constraints["triggers"]["pre_purchase_rise_rate"]


def test_실제_기록_상승률_10퍼센트를_넘어도_실제_하락이면_가격_경로가_닫힌다(constraints) -> None:
    """2026-08-21 무 (507건 중 · stable) — D+14 +13.6% 인데 08-26 663 → 08-27 661 로 내려간다."""
    forecast = _실제("2026-08-21", "무")
    day = constraints["situation"]["ci_judgment_day"]
    assert compute_rise_rate_2w(forecast, day) >= constraints["triggers"]["pre_purchase_rise_rate"]
    result = judge_sustained_rise(forecast, constraints)
    assert result.verdict == TREND_DECLINED
    assert result.first_decline == ("2026-08-26", 663, "2026-08-27", 661)
    state = _축_state(forecast)
    assert TIMING_AXIS not in compute_allowed_axes(state, "stable", constraints)

    # 🔴 **닫은 것이 궤적인지** — 하락만 걷으면(663 이후를 663 이상으로) 같은 날 열린다.
    #   값 비교가 아니라 입력을 바꿔 판정이 따라오는지 본다 (규칙 8 과 같은 결).
    고친 = copy.deepcopy(forecast)
    for row in 고친["daily"][:day]:
        if row["date"] > "2026-08-26" and not row["is_filled"]:
            row["predicted"] = max(row["predicted"], 663)
    assert judge_sustained_rise(고친, constraints).verdict == TREND_RISING
    assert TIMING_AXIS in compute_allowed_axes(_축_state(고친), "stable", constraints)


def test_실제_기록_복사행의_낮은_값은_하락으로_세지_않는다(constraints) -> None:
    """2026-09-11 배추 (507건 중 · 게이트 제거 뒤 금요일) — D+1·D+2 복사 행 709 가 앵커 762 아래다.

    첫 하락은 복사 행(09-12)이 아니라 **첫 모델 예측(09-14 713)** 에서 잡혀야 한다.
    """
    forecast = _실제("2026-09-11", "배추")
    result = judge_sustained_rise(forecast, constraints)
    assert result.verdict == TREND_DECLINED
    assert result.first_decline == (None, 762, "2026-09-14", 713)

    # 복사 행만 낮고 모델 예측은 앵커 위에서 오르면 — 통과한다
    고친 = copy.deepcopy(forecast)
    for row in 고친["daily"]:
        if not row["is_filled"]:
            row["predicted"] = 762 + int(row["date"][-2:])
    assert [r["predicted"] for r in 고친["daily"][:2]] == [709, 709]
    assert judge_sustained_rise(고친, constraints).verdict == TREND_RISING


def test_실제_기록_전부_quality_게이트면_하락이_아니라_보류(constraints) -> None:
    """2026-08-21 양파 (예측 배치 전수에서 찾음 · 507건 밖) — 14행이 전부 quality 게이트다.

    옛 정의로는 «엄격 증가 아님» 이라 **하락과 같은 거짓**이었다. 지금은 판정을 안 한다.
    """
    forecast = _실제("2026-08-21", "양파")
    day = constraints["situation"]["ci_judgment_day"]
    assert all("quality" in (r["gate_reason"] or "") for r in forecast["daily"][:day])
    result = judge_sustained_rise(forecast, constraints)
    assert result.verdict == TREND_WITHHELD
    assert result.withheld_reason == WITHHELD_INSUFFICIENT_POINTS
    assert result.points == () and result.first_decline is None
    sentence = sustained_rise_sentence(result.verdict, result.withheld_reason)
    assert "보류" in sentence and "아님" not in sentence and "내려가는" not in sentence

    state = _축_state(forecast)
    assert TIMING_AXIS not in compute_allowed_axes(state, "stable", constraints)
    decision = evaluate_split_entry(_진입_state(forecast), constraints)
    assert decision["by_trend"] is False
    assert decision["trend_verdict"] == TREND_WITHHELD
    assert decision["trend_withheld_reason"] == WITHHELD_INSUFFICIENT_POINTS


# ── 합성 입력 — 정의의 갈래 하나씩 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("values", "kwargs", "verdict"),
    [
        pytest.param([101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114],
                     {}, TREND_RISING, id="엄격_증가"),
        pytest.param([100, 100, 105, 105, 105, 110, 110, 110, 110, 111, 111, 111, 111, 111],
                     {}, TREND_RISING, id="앵커와_같은_값에서_출발한_보합_섞인_상승"),
        pytest.param([99, 105, 110, 110, 110, 110, 110, 110, 110, 110, 110, 110, 110, 110],
                     {}, TREND_DECLINED, id="앵커에서_첫날_하락"),
        pytest.param([105, 110, 109, 120, 120, 120, 120, 120, 120, 120, 120, 120, 120, 120],
                     {}, TREND_DECLINED, id="중간_1원_하락"),
        pytest.param([100] * 14, {}, TREND_NO_NET_RISE, id="전부_앵커와_같음"),
        pytest.param([100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 130],
                     {}, TREND_NO_NET_RISE, id="D15_상승은_창_밖"),
        pytest.param([101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 90],
                     {}, TREND_DECLINED, id="D14_하락은_창_안"),
        pytest.param([101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 90],
                     {}, TREND_RISING, id="D15_하락은_창_밖"),
    ],
)
def test_판정_갈래(constraints, values, kwargs, verdict) -> None:
    assert judge_sustained_rise(_합성(values, **kwargs), constraints).verdict == verdict


def test_복사행은_빼고_게이트는_사유로_가른다(constraints) -> None:
    """복사 행 · quality 는 빠지고 lead_time 은 남는다 — 같은 낮은 값 하나로 세 갈래를 잰다."""
    values = [101, 102, 90, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114]
    assert judge_sustained_rise(_합성(values), constraints).verdict == TREND_DECLINED
    assert judge_sustained_rise(_합성(values, filled={3}), constraints).verdict == TREND_RISING
    for reason, verdict in (
        ("quality", TREND_RISING),
        ("lead_time+quality", TREND_RISING),  # 복합값도 부분 문자열로 뺀다 (``is_gate_excluded``)
        ("lead_time", TREND_DECLINED),  # 🔴 기존 합의 — lead_time 게이트 행은 판정에 쓴다
    ):
        forecast = _합성(values, gates={3: reason})
        assert judge_sustained_rise(forecast, constraints).verdict == verdict, reason


def test_표식이_없는_행은_일반_지점이다(constraints) -> None:
    """``is_filled``/``gate_reason`` 칸이 **없으면 빼지 않는다**.

    칸이 없다는 것은 «복사 행이다» 가 아니다 (``is_gate_excluded`` 선례와 같다).
    """
    forecast = _합성([101, 102, 90, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114])
    for row in forecast["daily"]:
        for key in ("is_filled", "is_gated", "gate_reason"):
            row.pop(key)
    assert judge_sustained_rise(forecast, constraints).verdict == TREND_DECLINED


def test_지점이_모자라면_하락이어도_보류다(constraints) -> None:
    """앵커 외 지점 1개가 앵커보다 낮아도 **하락으로 안 센다** — 판정할 지점이 모자라다."""
    all_filled_but = {k for k in range(1, 15)}
    one = _합성([90] * 14, filled=all_filled_but - {5})
    result = judge_sustained_rise(one, constraints)
    assert result.verdict == TREND_WITHHELD
    assert result.withheld_reason == WITHHELD_INSUFFICIENT_POINTS
    assert len(result.points) == 1

    two = _합성([110] * 14, filled=all_filled_but - {5, 9})
    assert judge_sustained_rise(two, constraints).verdict == TREND_RISING


@pytest.mark.parametrize(
    ("forecast", "reason"),
    [
        pytest.param(_합성([110] * 14, anchor=None), WITHHELD_MISSING_ANCHOR, id="앵커_없음"),
        pytest.param(_합성([110] * 14, anchor=0), WITHHELD_MISSING_ANCHOR, id="앵커_0"),
        pytest.param(_합성([110] * 14, anchor=True), WITHHELD_MISSING_ANCHOR, id="앵커_bool"),
        pytest.param(_합성([110] * 13, horizon=13), WITHHELD_SHORT_HORIZON, id="창_13일"),
        pytest.param(_합성([101, None] + [110] * 12), WITHHELD_MISSING_VALUE, id="판정_행_값_없음"),
    ],
)
def test_데이터가_모자라면_보류하고_하락과_가른다(constraints, forecast, reason) -> None:
    result = judge_sustained_rise(forecast, constraints)
    assert (result.verdict, result.withheld_reason) == (TREND_WITHHELD, reason)
    assert result.first_decline is None


def test_복사행의_빈_값은_판정을_막지_않는다(constraints) -> None:
    """빠질 행의 값이 비었으면 **어차피 안 쓰는 값**이다 — 보류 사유가 아니다."""
    forecast = _합성([101, None] + [110] * 12, filled={2})
    assert judge_sustained_rise(forecast, constraints).verdict == TREND_RISING


# ── 규칙 8 — 선언을 바꾸면 판정이 따라온다 ─────────────────────────────────


def test_최소_지점_수는_선언에서_읽는다(constraints) -> None:
    all_filled_but = {k for k in range(1, 15)}
    two = _합성([110] * 14, filled=all_filled_but - {5, 9})
    assert judge_sustained_rise(two, constraints).verdict == TREND_RISING
    사본 = copy.deepcopy(constraints)
    사본["triggers"]["sustained_rise_min_model_points"] = 3
    result = judge_sustained_rise(two, 사본)
    assert result.verdict == TREND_WITHHELD
    assert result.withheld_reason == WITHHELD_INSUFFICIENT_POINTS


def test_판정_기간은_선언에서_읽는다(constraints) -> None:
    forecast = _합성([101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 90])
    assert judge_sustained_rise(forecast, constraints).verdict == TREND_DECLINED
    사본 = copy.deepcopy(constraints)
    사본["situation"]["ci_judgment_day"] = 13
    assert judge_sustained_rise(forecast, 사본).verdict == TREND_RISING


# ── ① 축과 ④ 진입이 같은 판정 함수를 쓴다 ─────────────────────────────────────────


def _축_state(forecast: dict) -> dict:
    """① 축 계산에 쓰는 State. mock 에는 N4 · ``cap_by_date`` 가 없어 수량 경로가 «못 봤다» 로
    닫힌다 — **가격 경로 하나만** 남긴다."""
    state = build_initial_state("배추", MOCK_AS_OF)
    state["forecast"] = forecast
    return state


def _진입_state(forecast: dict) -> dict:
    state = _축_state(forecast)
    state["allowed_axes"] = ["quantity", TIMING_AXIS]
    state["base_plan"] = {
        "drafts": [{"label": "공격", "raw_qty_kg": 1_000, "total_qty_kg": 1_000}]
    }
    return state


def test_옛_이름은_남지_않는다() -> None:
    """판정 입구가 둘이면 한쪽만 고쳐지는 날이 온다 — 옛 참/거짓 함수를 걷었다."""
    assert not hasattr(축_모듈, "is_sustained_rise")
    assert 진입_모듈.judge_sustained_rise is 축_모듈.judge_sustained_rise


@pytest.mark.parametrize(
    "verdict", [TREND_RISING, TREND_DECLINED, TREND_NO_NET_RISE, TREND_WITHHELD]
)
def test_축과_진입이_같은_함수의_답을_따른다(monkeypatch, constraints, verdict) -> None:
    """🔴 **판정 함수를 바꿔 끼우면 ① 과 ④ 가 같이 따라온다** — 둘 중 하나라도 자기 식을
    들고 있으면 여기서 갈린다. 입력은 +20% 로 꾸준히 오르는 예측이라 끼운 답만이 변수다."""
    forecast = _합성([100 + 2 * k for k in range(1, 15)])
    assert compute_rise_rate_2w(forecast, 14) >= constraints["triggers"]["pre_purchase_rise_rate"]
    calls: list[str] = []

    def 끼운_판정(forecast_arg, constraints_arg) -> SustainedRise:
        calls.append("called")
        return SustainedRise(verdict, None, 100, (("x", 1),), None)

    monkeypatch.setattr(축_모듈, "judge_sustained_rise", 끼운_판정)
    monkeypatch.setattr(진입_모듈, "judge_sustained_rise", 끼운_판정)
    opened = TIMING_AXIS in compute_allowed_axes(_축_state(forecast), "stable", constraints)
    decision = evaluate_split_entry(_진입_state(forecast), constraints)
    assert len(calls) == 2, "① 과 ④ 가 판정 함수를 한 번씩 불러야 한다"
    assert opened is decision["by_trend"] is (verdict == TREND_RISING)
    assert decision["trend_verdict"] == verdict


@pytest.mark.parametrize(
    "values",
    [
        pytest.param([100 + 2 * k for k in range(1, 15)], id="상승"),
        pytest.param(
            [100, 100, 104, 104, 108, 108, 112, 112, 116, 116, 120, 120, 125, 125], id="보합_상승"
        ),
        pytest.param([100 + 2 * k for k in range(1, 14)] + [127], id="끝_1원_하락"),
    ],
)
def test_축_가격_경로와_진입_궤적이_같은_입력에_같은_답(constraints, values) -> None:
    """stable · 상승률 ≥ 10% 인 날에는 ① 가격 경로 열림 == ④ ``by_trend`` 다."""
    forecast = _합성(values)
    assert compute_rise_rate_2w(forecast, 14) >= constraints["triggers"]["pre_purchase_rise_rate"]
    opened = TIMING_AXIS in compute_allowed_axes(_축_state(forecast), "stable", constraints)
    assert opened is evaluate_split_entry(_진입_state(forecast), constraints)["by_trend"]


# ── 기록 — 보류 · 하락 · 보합을 다른 문장으로 ───────────────────────────────


def test_진입_안_한_사유가_보류와_하락과_보합을_가른다(constraints) -> None:
    """⑥ 고지(``_entry_miss_reason``)가 ④ 판단 근거의 판정 칸을 읽는다."""
    문장 = {}
    for name, forecast in {
        "하락": _합성([99] + [110] * 13),
        "보합": _합성([100] * 14),
        "보류": _합성([110] * 14, anchor=None),
    }.items():
        decision = evaluate_split_entry(_진입_state(forecast), constraints)
        decision["cap_unknown_reason"] = "no_lead"
        문장[name] = _entry_miss_reason(decision)
    assert "지속 상승 궤적 아님" in 문장["하락"] and "내려가는 날" in 문장["하락"]
    assert "지속 상승 궤적 아님" in 문장["보합"] and "높지 않다" in 문장["보합"]
    assert "판정 보류" in 문장["보류"] and "아님" not in 문장["보류"]
    assert len(set(문장.values())) == 3
