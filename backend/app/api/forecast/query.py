"""가격 예측 탭 — 값을 읽어오는 곳.

소유: **ML 파트 (우리)**.

★ 여기만 우리가 실제 값에 붙어 있습니다. 다른 다섯 탭의 `query.py` 는
  아직 예시값입니다 (`Source.filled = False`).

★ **DB 가 안 붙어도 화면은 떠야 합니다.** 안 붙으면 예시값으로 떨어지고
  `Source.filled = False` 가 되어 화면에 「예시값」 딱지가 붙습니다.
  화면이 통째로 죽는 것보다, 예시라고 적힌 화면이 뜨는 편이 낫습니다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from app.api.calendar import build_axis
from app.api.forecast.schema import ForecastTab, ItemCard
from app.api.primitives import (
    Band,
    Chart,
    Column,
    Note,
    Series,
    Source,
    Table,
)

log = logging.getLogger(__name__)

ITEMS = ("배추", "무", "양파")

#: 그래프 세로 눈금을 품목마다 다르게 잡는다. 셋을 같은 눈금에 두면
#: 배추(900원대)와 무(600원대)가 겹쳐 보인다.
_SCALE = {
    "배추": (700, 1100, [800, 900, 1000, 1100]),
    "무": (500, 800, [500, 600, 700, 800]),
    "양파": (700, 850, [700, 750, 800, 850]),
}

#: 예시값 — 실제 값을 못 읽을 때만 쓴다. 데모 HTML 에서 옮겨 적었다.
_DEMO = {
    "배추": {"p": 930, "lo": 830, "hi": 1030, "ci": 0.11,
             "actual": [880, 895, 910, None, 905, None, None, 915, 920],
             "prev": [None, 890, 900, None, 915, None, None, 900, 925]},
    "무": {"p": 660, "lo": 600, "hi": 720, "ci": 0.19,
           "actual": [640, 645, 655, None, 650, None, None, 648, 650],
           "prev": [None, 650, 640, None, 660, None, None, 655, 645]},
    "양파": {"p": 785, "lo": 755, "hi": 815, "ci": 0.08,
             "actual": [770, 775, 780, None, 778, None, None, 782, 780],
             "prev": [None, 772, 778, None, 781, None, None, 779, 784]},
}

#: 봉인 개봉(2026-09-01) 실측. **우리 오차를 화면에 그대로 적는다.**
#: 이걸 안 적으면 예측선이 정답처럼 보인다.
#: ★ 오차율은 **글자로** 둔다. 수로 두면 9.0 이 화면에서 «9» 로 줄어
#:   양파만 소수점이 사라진다 — 표에서 자릿수가 어긋나 보인다.
_ACCURACY = [
    {"item": "배추", "avg": "963원", "err": "190원", "pct": "19.7", "band": "q03~q97"},
    {"item": "무", "avg": "771원", "err": "144원", "pct": "18.6", "band": "q03~q97"},
    {"item": "양파", "avg": "1,098원", "err": "98원", "pct": "9.0", "band": "q02~q98"},
]


def _blank(n: int) -> list[None]:
    return [None] * n


def _demo_card(item: str, target_date: str) -> ItemCard:
    d = _DEMO[item]
    return ItemCard(
        item=item,
        grade="특",
        spec={"배추": "그물망·파렛트 10kg", "무": "상자·파렛트 20kg",
              "양파": "그물망·파렛트 15kg"}[item],
        target_date=target_date,
        predicted=d["p"], lower=d["lo"], upper=d["hi"],
        ci_width=d["ci"], review=d["ci"] >= 0.15, use_recommended=True,
    )


def _real_card(item: str, as_of: date) -> ItemCard | None:
    """진짜 예측을 읽어본다. 못 읽으면 None — 부르는 쪽이 예시값으로 떨어진다."""
    try:
        from app.ml.service import get_forecast
    except Exception:  # pragma: no cover - ml 모듈이 없는 환경
        return None
    try:
        fc = get_forecast(item, as_of, "AUC")
    except Exception as error:  # DB 미연결 · 그날 예측 없음 둘 다 여기로 온다
        log.info("예측을 못 읽어 예시값을 씁니다 (%s): %s", item, error)
        return None
    first = fc.daily[0]
    ci = round((first.upper - first.lower) / first.predicted, 3) if first.predicted else 0.0
    return ItemCard(
        item=item, grade=fc.grade_name or "특", spec=fc.spec_desc,
        target_date=first.date.isoformat(),
        predicted=first.predicted, lower=first.lower, upper=first.upper,
        unit=fc.unit, ci_width=ci, review=ci >= 0.15,
        use_recommended=bool(fc.use_recommended),
    )


def build(as_of: date, item: str) -> ForecastTab:
    axis = build_axis(as_of)
    n = len(axis.days)
    i_asof = axis.as_of_index
    i_next = i_asof + 1
    target_date = axis.days[i_next].date

    cards, filled = [], True
    for name in ITEMS:
        card = _real_card(name, as_of)
        if card is None:
            filled = False
            card = _demo_card(name, target_date)
        cards.append(card)

    chosen = next(c for c in cards if c.item == item)
    demo = _DEMO[item]

    # 실측선 — as_of 까지만. 그 뒤는 아직 일어나지 않은 일이라 비운다.
    actual = list(demo["actual"])[: i_asof + 1] + _blank(n - i_asof - 1)
    prev = list(demo["prev"])[: i_asof + 1] + _blank(n - i_asof - 1)

    hi, lo, point = _blank(n), _blank(n), _blank(n)
    hi[i_next], lo[i_next], point[i_next] = chosen.upper, chosen.lower, chosen.predicted

    y_min, y_max, ticks = _SCALE[item]
    chart = Chart(
        label=f"{item} 경락가 · 특등급",
        y_min=y_min, y_max=y_max, y_ticks=ticks, y_unit="",
        series=[
            Series(name="실측", data=actual, tone="info", end_dot=True),
            Series(name="전일 예측", data=prev, tone="info", opacity=0.45, dashed=True, width=1.4),
            Series(name="내일 예측", data=point, tone="info", width=0),
        ],
        bands=[Band(name="예측 구간", hi=hi, lo=lo, tone="info")],
        note=Note(
            tone="neutral",
            text=(
                "빈 칸은 0 이 아니라 **경매가 없던 날**입니다. "
                "회색으로 눕힌 날이 휴장일입니다."
            ),
        ),
    )

    accuracy = Table(
        columns=[
            Column(key="item", label="품목"),
            Column(key="avg", label="평균 실제가", align="right", mono=True),
            Column(key="err", label="평균 오차", align="right", mono=True),
            Column(key="pct", label="오차율 %", align="right", mono=True),
            Column(key="band", label="구간"),
        ],
        rows=list(_ACCURACY),
        note=Note(
            tone="warn",
            text=(
                "★ 1,000원짜리를 배추는 197원, 무는 186원 틀립니다. "
                "먼 날짜일수록 더 틀립니다 — D+5 13.0% · D+14 17.8% · D+18 19.8%. "
                "홀드아웃 2024~2025 · 486 기준일 실측입니다."
            ),
        ),
    )

    quality = Table(
        columns=[
            Column(key="kind", label="가격"),
            Column(key="item", label="품목"),
            Column(key="use", label="써도 되나"),
            Column(key="why", label="근거"),
        ],
        rows=[
            {"kind": "경락가", "item": "배추", "use": "예", "why": "두 구간 모두 +13.1%"},
            {"kind": "경락가", "item": "무", "use": "예", "why": "+2.0% / +0.8%"},
            {"kind": "경락가", "item": "양파", "use": "예", "why": "+4.5% / +14.6%"},
            {"kind": "중도매가", "item": "무", "use": "아니오",
             "why": "홀드아웃에서 음수 (−7.3%). 한 해가 만든 값이었다"},
        ],
        note=Note(
            tone="neutral",
            text="「아니오」인 조합은 어제 가격을 그대로 쓰는 편이 낫습니다.",
        ),
    )

    return ForecastTab(
        axis=axis, items=list(ITEMS), selected=item, cards=cards,
        chart=chart, accuracy=accuracy, quality=quality,
        caveat=Note(
            tone="warn",
            text=(
                "★ **가운데 값 하나만 보고 사면 안 됩니다.** 실제가가 예측보다 "
                "4.7% 넘게 비쌌던 날이 D+14 기준 배추 57% · 무 46% · 양파 38% 입니다. "
                "구간의 위쪽 값으로 최악을 잡으세요."
            ),
        ),
        source=Source(
            filled=filled, owner="ML",
            note=("haetdeul.ml_price_forecasts 에서 읽었습니다"
                  if filled else
                  "예측을 못 읽어 예시값을 그립니다 — DB 연결과 그날 예측을 확인하세요"),
        ),
    )
