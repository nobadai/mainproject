"""가격 예측 탭 — 값을 읽어오는 곳.

소유: **ML 파트 (우리)**.

★ **우리가 쓰던 화면(`localhost:3100`)의 예측 탭을 그대로 옮겼습니다.**

★ **원본 창고(`prediction_log`)를 읽습니다.** 서비스 창고
  (`haetdeul.ml_price_forecasts`)에는 실제값·채점이 없습니다.
  «얼마나 틀렸나» 를 보이려면 채점이 있어야 합니다.

★ **`model_ver` 를 정확히 걸러야 합니다.**

      ops_auc  ops_whsl  ops_rtl        운영 (밑줄)
      ops-auc-old · ops-auc-ung · …     실험 (붙임표)

  섞으면 성능이 통째로 틀립니다 — 2026-09-01 에 배추 경락가 오차를 19.7%
  대신 **35.1%** 로 잘못 보고한 적이 있습니다.

★ **DB 가 안 붙어도 화면은 떠야 합니다.** 안 붙으면 예시값으로 떨어지고
  「예시값」 딱지가 붙습니다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from app.api.forecast.schema import (
    BaseDateOption,
    ChartPoint,
    ForecastTab,
    ItemCard,
    KindOption,
)
from app.api.primitives import (
    Band,
    CalendarAxis,
    Chart,
    Column,
    Day,
    Note,
    Series,
    Source,
    Table,
)

log = logging.getLogger(__name__)

ITEMS = ("배추", "무", "양파")
KINDS = ("auc", "whsl", "rtl")

#: 운영 모델만. **붙임표(`ops-…`)는 실험이라 섞으면 안 된다.**
OPS = ("ops_auc", "ops_whsl", "ops_rtl")

#: 리드타임 게이트. **2026-09-09 에 껐습니다** — 0 이면 게이트 없음.
#:
#: ★ 그 전에는 `LT<3` 이면 모델 대신 앵커를 그대로 내보냈습니다. 매입이
#:   오늘 밤 경매를 두고 판단해야 하는데 «어제값» 을 받으면 쓸 값이 없어
#:   껐습니다. 화면도 그때 만든 자리(회색 칸 · 「어제값(게이트)」)가 남아
#:   있었는데 같이 걷었습니다.
GATE_LEAD = 0

#: 리드 0 = **기준일 그날**. 가락 경매는 그날 밤에 열리고 배치는 아침에
#: 도니, 당일 경매도 아직 안 일어난 일이라 예측 대상입니다.
TODAY_LEAD = 0

#: 이 날 이전 예측은 경락가에 포장 규격이 섞여 있었다 (2026-08-27 발견).
SPEC_FIX_AT = "2026-08-28"

#: 기준일 목록 상한. 닿으면 «잘렸다» 고 같이 알린다.
BASE_DATE_LIMIT = 400

_KIND_META = (
    KindOption(kind="auc", label="경락가", role="매입 — 경매에서 사는 값"),
    KindOption(kind="whsl", label="중도매가", role="중도매 — 도매상이 파는 값"),
    KindOption(kind="rtl", label="소매가", role="매도 — 소비자가 사는 값"),
)
_KIND_LABEL = {k.kind: k.label for k in _KIND_META}

SPEC_DESC = {
    "배추": "망/파렛트 10kg",
    "무": "상자/파렛트 20kg",
    "양파": "망/파렛트 15kg",
}

#: 봉인 개봉(2026-09-01) 실측. **우리 오차를 화면에 그대로 적는다.**
#: ★ 오차율은 글자로 둔다 — 수로 두면 9.0 이 화면에서 «9» 로 줄어든다.
_ACCURACY = [
    {"item": "배추", "avg": "963원", "err": "190원", "pct": "19.7", "band": "q03~q97"},
    {"item": "무", "avg": "771원", "err": "144원", "pct": "18.6", "band": "q03~q97"},
    {"item": "양파", "avg": "1,098원", "err": "98원", "pct": "9.0", "band": "q02~q98"},
]

#: 예시값 — 창고에 못 붙었을 때만 쓴다.
_DEMO = {
    "배추": {"p": 930, "lo": 830, "hi": 1030},
    "무": {"p": 660, "lo": 600, "hi": 720},
    "양파": {"p": 785, "lo": 755, "hi": 815},
}

_SQL_BASE_DATES = """
    SELECT base_dt, COUNT(*) AS n, COUNT(actual_prc) AS scored,
           MIN(created_at) AS made_at
      FROM prediction_log
     WHERE model_ver = ANY(%s)
     GROUP BY base_dt
     ORDER BY base_dt DESC
     LIMIT %s
"""

_SQL_ROWS = """
    SELECT lead_biz_d, target_dt, anchor_prc, pred_prc, pred_lo, pred_hi,
           actual_prc, abs_pct_err, gated, gate_reason
      FROM prediction_log
     WHERE model_ver = ANY(%s) AND base_dt = %s AND target_kind = %s AND item_nm = %s
     ORDER BY lead_biz_d
"""

#: 세 품목의 **기준일 그날 값** (리드 0).
#:
#: ★ 전에는 «게이트를 지난 첫 리드» 를 썼습니다. 게이트가 리드 1~2 를
#:   어제값으로 덮던 때라 그게 첫 모델값이었습니다. 게이트를 끄고 리드 0 을
#:   만든 지금은 **기준일 그날**이 맞습니다 — 오늘 9월 9일인데 카드에
#:   9월 14일 값이 뜨고 있었습니다.
#:
#: ★ 그날 값이 없으면 가장 가까운 리드로 떨어집니다 (`ORDER BY lead_biz_d`).
_SQL_CARDS = """
    SELECT DISTINCT ON (item_nm)
           item_nm, lead_biz_d, target_dt, pred_prc, pred_lo, pred_hi, gated
      FROM prediction_log
     WHERE model_ver = ANY(%s) AND base_dt = %s AND target_kind = %s
       AND item_nm = ANY(%s) AND lead_biz_d >= %s
     ORDER BY item_nm, lead_biz_d
"""

_SQL_QUALITY = """
    SELECT target_kind, item_nm, use_recommended, note
      FROM ref_prediction_quality
     ORDER BY target_kind, item_nm
"""


def _fetch(sql: str, params: tuple):
    """원본 창고 조회. 못 읽으면 None — 부르는 쪽이 예시값으로 떨어진다.

    ★ 통째로 잡는 것이 맞습니다. 여기서 무슨 일이 나든 **화면은 떠야** 하고,
      대신 「예시값」 딱지가 붙습니다. 예외를 골라 잡으면 안 골라낸 하나
      때문에 화면이 통째로 죽습니다.
    """
    try:
        from app.ml.db import fetch_all

        return fetch_all(sql, params, source=True)
    except Exception as error:  # noqa: BLE001  DB 미연결 · 표 없음 둘 다 여기로
        log.info("원본 창고를 못 읽어 예시값을 씁니다: %s", error)
        return None


def _num(v) -> float | None:
    return None if v is None else float(v)


_STEPS = (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000)


def _scale(values: list[float]) -> tuple[float, float, list[float]]:
    """값에 맞춰 세로 눈금을 잡는다.

    ★ 품목마다 자리가 다르고(배추 900원대 · 무 600원대) 계절마다 크게
      움직입니다. **고정 눈금을 쓰면 선이 화면 밖으로 나갑니다.**
    """
    if not values:
        return 0.0, 100.0, [0.0, 50.0, 100.0]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.2, high * 0.06, 1.0)
    low, high = low - pad, high + pad
    step = next((s for s in _STEPS if (high - low) / s <= 5), _STEPS[-1])
    low = step * int(low // step)
    high = step * (int(high // step) + 1)
    ticks = [low + step * i for i in range(int((high - low) / step) + 1)]
    return low, high, ticks[1:] or ticks


def _demo_tab(as_of: date, kind: str, item: str) -> ForecastTab:
    """창고에 못 붙었을 때. 모양만 같고 값은 예시다."""
    target = as_of + timedelta(days=1)
    days = [Day(date=as_of.isoformat(), dow="", market_open=True, survey=True),
            Day(date=target.isoformat(), dow="", market_open=True, survey=True)]
    cards = [
        ItemCard(
            item=name, grade="특급", spec=SPEC_DESC[name], target_date=target.isoformat(),
            predicted=v["p"], lower=v["lo"], upper=v["hi"],
            ci_width=round((v["hi"] - v["lo"]) / v["p"], 3),
            review=(v["hi"] - v["lo"]) / v["p"] >= 0.15, use_recommended=True,
        )
        for name, v in _DEMO.items()
    ]
    chosen = next(c for c in cards if c.item == item)
    y_min, y_max, ticks = _scale([float(chosen.lower), float(chosen.upper)])
    return ForecastTab(
        kinds=list(_KIND_META), selected_kind=kind,
        items=list(ITEMS), selected=item,
        base_dates=[BaseDateOption(base_dt=as_of.isoformat(), total=0, scored=0,
                                   pre_fix=False)],
        selected_base_dt=as_of.isoformat(), base_dates_truncated=False,
        cards=cards,
        axis=CalendarAxis(as_of=as_of.isoformat(), as_of_index=0, days=days),
        chart=Chart(
            label=f"{item} {_KIND_LABEL[kind]}", y_min=y_min, y_max=y_max, y_ticks=ticks,
            series=[Series(name="예측", data=[None, float(chosen.predicted)], tone="info")],
            bands=[Band(name="예측 범위", hi=[None, float(chosen.upper)],
                        lo=[None, float(chosen.lower)], tone="info")],
        ),
        rows=Table(columns=[Column(key="lead", label="리드타임")], rows=[],
                   empty_text="예측 데이터 저장소에 연결할 수 없어 표를 표시하지 못합니다"),
        gate_lead=GATE_LEAD,
        accuracy=_accuracy_table(), quality=_quality_table(False), caveat=_caveat(),
        source=Source(
            filled=False, owner="ML",
            note="원본 데이터 저장소에 연결할 수 없어 예시값을 보여줍니다 — .env 파일의 ML_SOURCE_DB_* 설정을 확인하세요",
        ),
    )


def _accuracy_table() -> Table:
    return Table(
        columns=[
            Column(key="item", label="품목"),
            Column(key="avg", label="평균 실제가", align="right", mono=True),
            Column(key="err", label="평균 오차", align="right", mono=True),
            Column(key="pct", label="오차율", align="right", mono=True),
            Column(key="band", label="범위"),
        ],
        rows=list(_ACCURACY),
    )


def _quality_table(live: bool) -> Table:
    rows: list[dict] | None = None
    if live:
        got = _fetch(_SQL_QUALITY, ())
        if got:
            rows = [
                {
                    "kind": _KIND_LABEL.get(r["target_kind"], r["target_kind"]),
                    "item": r["item_nm"],
                    "use": "예" if r["use_recommended"] else "아니오",
                    "why": r["note"] or "",
                }
                for r in got
                if r["item_nm"] in ITEMS
            ]
    if not rows:
        rows = [
            {"kind": "경락가", "item": "배추", "use": "예", "why": "두 구간 모두 +13.1%"},
            {"kind": "경락가", "item": "무", "use": "예", "why": "+2.0% / +0.8%"},
            {"kind": "경락가", "item": "양파", "use": "예", "why": "+4.5% / +14.6%"},
            {"kind": "중도매가", "item": "무", "use": "아니오",
             "why": "홀드아웃에서 음수 (−7.3%). 한 해가 만든 값이었다"},
        ]
    return Table(
        columns=[
            Column(key="kind", label="가격"),
            Column(key="item", label="품목"),
            Column(key="use", label="써도 됨"),
            Column(key="why", label="이유"),
        ],
        rows=rows,
        empty_text="품질 진단 기록이 없습니다",
    )


def _caveat() -> Note:
    return Note(
        tone="warn",
        text=(
            "★ **가운데 예측 수치 하나만 믿고 사면 안 됩니다.** 14영업일 뒤(D+14) 기준, "
            "실제 가격이 예측보다 4.7% 이상 비쌌던 날이 배추는 57%, 무는 46%, 양파는 "
            "38%나 되었습니다. 예측 범위에서 가장 높은 금액을 최악의 상황으로 잡고 "
            "매입하세요."
        ),
    )


def base_dates(kind: str) -> tuple[list[BaseDateOption], bool]:
    got = _fetch(_SQL_BASE_DATES, (list(OPS), BASE_DATE_LIMIT))
    if not got:
        return [], False
    out = [
        BaseDateOption(
            base_dt=r["base_dt"].isoformat(),
            total=r["n"], scored=r["scored"],
            pre_fix=str(r["made_at"])[:10] < SPEC_FIX_AT,
        )
        for r in got
    ]
    return out, len(out) >= BASE_DATE_LIMIT


def build(as_of: date, item: str, kind: str = "auc", base_dt: str | None = None) -> ForecastTab:
    quality = _quality_table(True)
    quality_rows = quality.rows
    dates, truncated = base_dates(kind)
    if not dates:
        return _demo_tab(as_of, kind, item)

    #  고른 기준일. 없으면 as_of 이하의 가장 최근 것 — 미래 정보를 안 쓴다.
    chosen_dt = base_dt if base_dt and any(d.base_dt == base_dt for d in dates) else next(
        (d.base_dt for d in dates if d.base_dt <= as_of.isoformat()), dates[0].base_dt
    )
    picked = next(d for d in dates if d.base_dt == chosen_dt)

    #  ★ `prediction_log.target_kind` 는 **소문자**다 (`auc` · `whsl` · `rtl`).
    #    서비스 창고(`ml_price_forecasts`)는 대문자라 헷갈리기 쉽다 —
    #    대문자로 물으면 오류 없이 **0행**이 와서 조용히 예시값으로 떨어진다.
    rows = _fetch(_SQL_ROWS, (list(OPS), chosen_dt, kind, item))
    card_rows = _fetch(_SQL_CARDS, (list(OPS), chosen_dt, kind, list(ITEMS), TODAY_LEAD))
    if not rows or not card_rows:
        return _demo_tab(as_of, kind, item)

    # ── 카드 세 장 ────────────────────────────────────────────────────
    #   ★ **배추 · 무 · 양파 차례로** 놓는다 (`ITEMS`). DB 가 주는 차례는
    #     이름순이라 «무» 가 앞에 왔다. 배추가 물량도 제일 많고 우리
    #     성적도 제일 좋은 품목이라 먼저 보이는 게 맞다.
    order = {name: i for i, name in enumerate(ITEMS)}
    card_rows = sorted(card_rows, key=lambda r: order.get(r["item_nm"], 99))

    cards = []
    for r in card_rows:
        #   ★ **자르지 말고 반올림한다** (2026-09-09 고침).
        #
        #     전에는 `int()` 였다. 그런데 그래프는 화면에서 `Math.round` 로
        #     그린다. 같은 값(422.535)을 서버는 **422**, 그래프는 **423** 으로
        #     보여 «카드와 그래프가 다르다» 는 말이 나왔다.
        #
        #     ★ 자르는 쪽은 방향까지 틀리다. 매입가는 **늘 조금씩 싸게**
        #       보이게 되어, 사는 사람이 실제보다 싸다고 믿는다.
        p, lo, hi = (round(float(r["pred_prc"])), round(float(r["pred_lo"])),
                     round(float(r["pred_hi"])))
        ci = round((hi - lo) / p, 3) if p else 0.0
        cards.append(ItemCard(
            item=r["item_nm"], grade="특급",
            spec=SPEC_DESC.get(r["item_nm"]) if kind == "auc" else None,
            target_date=r["target_dt"].isoformat(),
            predicted=p, lower=lo, upper=hi, unit="원/kg",
            ci_width=ci, review=ci >= 0.15, use_recommended=True,
            gated=bool(r["gated"]),
        ))

    # ── 축: 대상일 그대로 ─────────────────────────────────────────────
    #
    #  ★ **기준일을 따로 앞에 붙이지 않습니다.** 리드 0 의 대상일이 곧
    #    기준일이라, 붙이면 같은 날이 두 칸이 됩니다. 전에는 리드가 1 부터
    #    시작해 «기준일이 어디 갔나» 가 됐고 그래서 앵커를 맨 앞에 놓았는데,
    #    이제 그 자리에 진짜 예측이 들어갑니다.
    #
    #  ★ 회색 칸은 **모델을 안 쓴 칸**입니다. 지금은 품질 차단(`gated`)뿐이고
    #    리드타임 게이트는 껐습니다.
    _DOW = ("월", "화", "수", "목", "금", "토", "일")
    days = []
    for r in rows:
        d = r["target_dt"]
        days.append(Day(
            date=d.isoformat(), dow=_DOW[d.weekday()],
            market_open=not r["gated"], survey=True,
        ))

    anchor = _num(rows[0]["anchor_prc"])
    pred: list[float | None] = []
    lo_s: list[float | None] = []
    hi_s: list[float | None] = []
    actual: list[float | None] = []
    for r in rows:
        pred.append(_num(r["pred_prc"]))
        lo_s.append(_num(r["pred_lo"]))
        hi_s.append(_num(r["pred_hi"]))
        actual.append(_num(r["actual_prc"]))

    seen = [v for v in pred + actual + lo_s + hi_s if v is not None]
    y_min, y_max, ticks = _scale(seen)

    chart = Chart(
        label=f"{item} {_KIND_LABEL[kind]} · 기준일부터 18영업일",
        y_min=y_min, y_max=y_max, y_ticks=ticks,
        shade_label="모델을 안 쓴 칸 — 품질 차단으로 어제 가격이 나감",
        series=[
            Series(name="예측 가격", data=pred, tone="info", end_dot=True),
            Series(name="실제 가격", data=actual, tone="warn", width=1.6, dashed=True),
            #  ★ 출발점을 가로선으로 깐다. 모델이 여기서 위로 갔나 아래로
            #    갔나가 한눈에 보인다 — 매입은 그 방향으로 판단한다.
            Series(name="출발점 (어제 가격과 최근 7일 평균을 섞은 값)",
                   data=[anchor] * len(pred) if anchor else [],
                   tone="neutral", dashed=True, width=1, opacity=0.6),
        ],
        bands=[Band(name="예측 범위", hi=hi_s, lo=lo_s, tone="info")],
        x_labels=[d.date[5:] for d in days],
    )

    #  ★ 그래프가 쓰는 **원시 수치**. 표(`table`)와 같은 줄인데 글자가 아니라
    #    수다. 표는 사람이 읽으라고 「600–855」처럼 자릿점을 찍어 두는데,
    #    그래프는 좌표를 계산해야 해서 숫자가 필요하다. 화면이 글자를 다시
    #    숫자로 되돌리게 하면 자릿점과 단위 때문에 조용히 틀린다.
    points = [
        ChartPoint(
            lead=r["lead_biz_d"],
            target_dt=r["target_dt"].isoformat(),
            pred=_num(r["pred_prc"]),
            lo=_num(r["pred_lo"]),
            hi=_num(r["pred_hi"]),
            actual=_num(r["actual_prc"]),
            err_pct=_num(r["abs_pct_err"]),
            anchor=anchor,
            gated=bool(r["gated"]),
        )
        for r in rows
    ]

    # ── 리드타임별 표 ─────────────────────────────────────────────────
    table = Table(
        columns=[
            Column(key="lead", label="며칠 뒤", align="right", mono=True),
            Column(key="target", label="대상일", mono=True),
            Column(key="pred", label="예측값", align="right", mono=True),
            Column(key="band", label="예상 범위", align="right", mono=True),
            Column(key="actual", label="실제값", align="right", mono=True),
            Column(key="err", label="오차율", align="right", mono=True),
            Column(key="src", label="값 출처"),
        ],
        rows=[
            {
                "lead": r["lead_biz_d"],
                "target": r["target_dt"].isoformat()[5:],
                #   ★ 카드·그래프와 **같은 방식으로** 다듬는다. 한 화면 안에서
                #     같은 값이 다르게 보이면 안 된다.
                "pred": f"{round(float(r['pred_prc'])):,}",
                "band": f"{round(float(r['pred_lo'])):,}–{round(float(r['pred_hi'])):,}",
                "actual": (None if r["actual_prc"] is None
                           else f"{round(float(r['actual_prc'])):,}"),
                "err": None if r["abs_pct_err"] is None else f"{float(r['abs_pct_err']):.1f}",
                "src": "어제 가격 (차단됨)" if r["gated"] else "모델",
            }
            for r in rows
        ],
        empty_text="선택한 조건에 해당하는 예측이 없습니다",
    )

    #  ★ 「판정 근거」 는 이 조합을 써도 되는지에 대한 말이다.
    #    게이트 사유(`lead_time`)를 여기 적으면 «판정 근거 lead_time» 이 되어
    #    읽는 사람이 무슨 말인지 모른다. 실제로 그렇게 나왔다.
    quality_note = None
    for r in quality_rows:
        if r["kind"] == _KIND_LABEL[kind] and r["item"] == item:
            quality_note = r["why"] or None
            break

    notice = None
    if picked.pre_fix:
        notice = Note(
            tone="warn",
            text=(
                "**이 날은 옛날 계산 방식으로 만든 예측입니다.** 2026-08-27에 경락가에서 "
                "여러 포장 규격이 섞여 계산되던 문제를 바로잡았으나, 이 예측은 고치기 "
                "전에 만들어졌습니다. 다른 날 수치와 서로 이어지지 않으니 **다른 날과 "
                "나란히 놓고 비교하지 마세요.** 실제로 전달되었던 기록이라 삭제하지 않고 "
                "남겨둡니다."
            ),
        )

    return ForecastTab(
        kinds=list(_KIND_META), selected_kind=kind,
        items=list(ITEMS), selected=item,
        base_dates=dates, selected_base_dt=chosen_dt, base_dates_truncated=truncated,
        notice=notice, cards=cards,
        axis=CalendarAxis(as_of=chosen_dt, as_of_index=0, days=days),
        chart=chart, rows=table, points=points, gate_lead=GATE_LEAD,
        quality_note=quality_note,
        accuracy=_accuracy_table(), quality=quality, caveat=_caveat(),
        source=Source(
            filled=True, owner="ML",
            note=f"prediction_log · {', '.join(OPS)} · 기준일 {chosen_dt}",
        ),
    )
