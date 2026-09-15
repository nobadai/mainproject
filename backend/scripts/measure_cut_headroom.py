"""매입 컷의 **여유율**을 두 축 × 두 창으로 잰다. **읽기만 한다.**

`CLAUDE.md` 규칙 5 가 여는 순서를 정해 두었다 — *"먼저 D=12 칸을 창 기준으로 다시
뽑는 것이 앞이다. 그 수 없이 분위를 정하면 2026-09-10 에 무른 판을 같은 근거로 다시
하는 것이 된다."* 이 스크립트가 그 「다시 뽑는 것」이다.

```bash
uv run python scripts/measure_cut_headroom.py            # 표를 낸다
uv run python scripts/measure_cut_headroom.py --csv PATH # ML CSV 의 actual 과 대조
```

## 🔴 이 스크립트가 **안 하는 것** 넷

① **읽기 전용이다.** `SELECT` 만 한다 — 쓰기 경로가 없다.

② **판정 코드가 아니다.** ``compute_cut_unit_price`` 를 부르지도 바꾸지도 않는다.
   산식을 여기에 **다시 적는데**, 그건 복제가 아니라 *"코드가 실제로 무엇을 보는가"*
   를 바깥에서 재는 것이다. 값을 바꿀 때 고칠 자리는 여전히 그 함수 하나다.

③ **검사로 안 잠근다.** 왜인가 —
   이 산출물은 **값**이고 자료가 늘면 매일 움직인다 (`actual` 이 하루씩 더 찬다).
   값을 검사에 박으면 규칙 8 이 금지한 **값 비교**가 되고, 자료가 는 날 검사가
   *"틀렸다"* 고 운다. 그때 고쳐지는 것은 코드가 아니라 **검사에 적힌 숫자**다.
   ★ 잠그는 것은 **값이 정해진 뒤 그 값을 읽는 자리**이지 재는 도구가 아니다.

④ **분위를 안 정한다.** q80 으로 갈지 · 품목별로 가를지 · 표를 자동갱신할지는
   이 수를 **보고 나서** 정한다.

## 산식

```text
실효 여유율(현행 컷)   max(upper[:D])  / max(predicted[:D]) − 1
창 기준 초과분(실측)   max(actual[:D]) / max(predicted[:D]) − 1
분위                   초과분 분포에서 **현행 실효 여유율이 몇 분위인가**
```

★ ``actual`` 은 **ML 이 준 채점 SQL 그대로**다 (2026-09-10 11:32 회신 §1.1) —
  금액합 ÷ 물량합이지 **단가 평균이 아니다.** 포장 목록이 품목마다 다르고(무만 「상자」),
  무는 2018년에 18kg → 20kg 로 표준이 바뀌어 날짜로 갈라야 한다.

## 두 축이 왜 둘인가

```text
달력 축   base_dt 별 offset_days 1..D        ← **우리 컷이 실제로 보는 창**
          is_filled 복사행을 **포함**한다. ``daily[:D]`` 와 같은 자리다
조사 축   같은 표를 src_lead_biz_d <= k 로 자른다
          ← 2026-09-10 표(배추 14.0 / 27.8 / 50.2)가 선 자리
```

🔴 **09-10 판이 무너진 이유가 「다른 축의 수를 같은 자리에 썼다」**였다. 하루짜리 LT
분위를 창 자리에 쓰려다 배추 D=12 에서 다섯 배를 틀릴 뻔했다. 그래서 **두 축을 나란히
낸다** — 갈리면 *"어느 쪽이 맞다"* 가 아니라 **갈린다는 사실 자체가 산출물**이다.

## ⚠️ 표본을 늘 같이 낸다

주말·공휴일에는 `actual` 이 없다. ``max(actual[:D])`` 는 **실재하는 날의 최대**이고,
칸마다 **base_dt 수와 기여한 날 수**를 같이 적는다. 안 적으면 「D=12 인데 실은 8날」이
숨는다.

★ 접속 정보는 ``backend/.env`` 에서 읽는다. 여기에 호스트도 비밀번호도 안 적는다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.finance.db import fetch_all, get_db_schema

#: 커버일수. `constraints.yaml` 의 `coverage_days` 와 같은 셋이지만 **여기서 판정하지
#: 않는다** — 표의 열 이름일 뿐이다.
COVERAGE = (2, 5, 12)

ITEMS = ("배추", "무", "양파")

#: 재는 창 셋. 이름과 경계를 한자리에 둔다.
#:
#: 🔴 「전 구간」은 **2026-09-10 표를 재현하려고** 둔 자리다. 그때 쓴 원본이 ML CSV 였고
#:   그 CSV 가 `2026-01-02 ~ 09-10` 전부를 담고 있다. 걷기 창만으로는 그 표가 안 나온다 —
#:   **배추가 여름에 폭등해서 창이 길수록 최대가 끌려 올라가기 때문**이다.
WINDOWS = {
    "걷기 창": ("2026-01-01", "2026-03-31"),
    "ML 권고 창": ("2026-09-04", "2099-12-31"),
    "전 구간(09-10 재현용)": ("2026-01-01", "2026-09-30"),
}

#: ML 채점 SQL 그대로 (2026-09-10 11:32 회신 §1.1). **금액합 ÷ 물량합**이다.
_ACTUAL_SQL = """
SELECT auction_date AS dt, item_name AS item_nm,
       (SUM(trade_amount_krw) / NULLIF(SUM(trade_volume_kg), 0)) AS prc
  FROM source_raw.auction_prices_daily
 WHERE wholesale_market_code = '110001'
   AND grade_code            = '11'
   AND avg_auction_price_krw_per_kg > 0
   AND trade_volume_kg > 0
   AND (   (item_name = '배추' AND package_name IN ('그물망', '파렛트'))
        OR (item_name = '무'   AND package_name IN ('상자',   '파렛트'))
        OR (item_name = '양파' AND package_name IN ('그물망', '파렛트')))
   AND (   (item_name = '배추' AND unit_weight_kg = 10)
        OR (item_name = '무' AND (
                (auction_date <  DATE '2018-01-01' AND unit_weight_kg = 18)
             OR (auction_date >= DATE '2018-01-01' AND unit_weight_kg = 20)))
        OR (item_name = '양파' AND unit_weight_kg = 15))
 GROUP BY 1, 2
"""


def load_actuals() -> dict[tuple[dt.date, str], float]:
    """(날짜, 품목) → 실제 경락가. **ML 채점 SQL 그대로**."""
    return {
        (r["dt"], r["item_nm"]): float(r["prc"])
        for r in fetch_all(_ACTUAL_SQL)
        if r["prc"] is not None
    }


def load_forecasts(schema: str) -> list[dict[str, Any]]:
    """AUC 예측 전 행. ``is_filled`` 복사행을 **거르지 않는다** — 컷이 그것을 본다."""
    return fetch_all(f"""
        SELECT base_dt, item_nm, offset_days, target_dt,
               predicted, upper, src_lead_biz_d, is_filled
          FROM {schema}.ml_price_forecasts
         WHERE target_kind = 'AUC' AND item_nm = ANY(%s)
         ORDER BY base_dt, item_nm, offset_days
    """, (list(ITEMS),))


def _cut(rows: list[dict[str, Any]], axis: str, d: int) -> list[dict[str, Any]]:
    """창을 자른다. **축이 둘이라 자르는 법도 둘이다.**"""
    if axis == "달력":
        return [r for r in rows if 1 <= int(r["offset_days"]) <= d]
    lead = [r for r in rows if r["src_lead_biz_d"] is not None]
    return [r for r in lead if 1 <= int(r["src_lead_biz_d"]) <= d]


def _quantile_of(values: list[float], x: float) -> float:
    """``x`` 가 분포에서 몇 분위인가 (0~100)."""
    if not values:
        return float("nan")
    return 100.0 * sum(1 for v in values if v <= x) / len(values)


def measure(
    forecasts: list[dict[str, Any]],
    actuals: dict[tuple[dt.date, str], float],
    lo: str,
    hi: str,
    cutoff: dt.date,
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """한 창에 대해 (축 × 품목 × D) 칸을 낸다.

    ``cutoff`` 는 **실측이 실제로 찬 마지막 날**이다. 창이 그보다 뒤로 뻗으면 그 칸은
    안 센다 — 「아직 안 온 값」과 「그날 장이 안 섰다」는 다른 사실이다 (규칙 3).
    """
    by_key: dict[tuple[dt.date, str], list[dict[str, Any]]] = defaultdict(list)
    for r in forecasts:
        if lo <= r["base_dt"].isoformat() <= hi:
            by_key[(r["base_dt"], r["item_nm"])].append(r)

    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for axis in ("달력", "조사"):
        for item in ITEMS:
            for d in COVERAGE:
                headroom: list[float] = []   # 현행 컷의 실효 여유율
                excess: list[float] = []     # 실측 초과분
                bases = 0
                days = 0
                for (base_dt, itm), rows in by_key.items():
                    if itm != item:
                        continue
                    win = _cut(rows, axis, d)
                    if not win:
                        continue
                    pred = [float(r["predicted"]) for r in win]
                    up = [float(r["upper"]) for r in win]
                    if not pred or max(pred) <= 0:
                        continue
                    bases += 1
                    headroom.append(max(up) / max(pred) - 1.0)
                    #  🔴 **창이 자료 끝을 넘으면 안 센다.** 주말·공휴일에 실측이 없는 것은
                    #    정상이지만(그날 장이 안 선다), 아직 안 온 날은 **나중에 올 값**이라
                    #    그 창의 최대가 과소평가된다. 둘을 갈라야 한다.
                    if max(r["target_dt"] for r in win) > cutoff:
                        continue
                    got = [
                        actuals[(r["target_dt"], itm)]
                        for r in win
                        if (r["target_dt"], itm) in actuals
                    ]
                    #  ★ 창 안 **거래가 선 날**의 최대다. 주말은 애초에 값이 없다.
                    if got:
                        days += len(got)
                        excess.append(max(got) / max(pred) - 1.0)
                out[(axis, item, d)] = {
                    "headroom": statistics.median(headroom) if headroom else None,
                    "q80": _pct(excess, 80) if excess else None,
                    "quantile": (
                        _quantile_of(excess, statistics.median(headroom))
                        if excess and headroom else None
                    ),
                    "bases": bases,
                    "full": len(excess),
                    "days": days,
                }
    return out


def _pct(values: list[float], p: int) -> float:
    """``p`` 분위. 표본이 적어도 돌아야 하므로 손으로 센다."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo_i, hi_i = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo_i] + (s[hi_i] - s[lo_i]) * (k - lo_i)


def _fmt(x: float | None, suffix: str = "%") -> str:
    return "—" if x is None else f"{x * 100:.1f}{suffix}"


def render(results: dict[str, dict[tuple[str, str, int], dict[str, Any]]]) -> None:
    print()
    for win_name, cells in results.items():
        print(f"┌─ {win_name}  {WINDOWS[win_name][0]} ~ {WINDOWS[win_name][1]}")
        print(f"│  {'축':<5}{'품목':<5}{'D':>3}  {'실효 여유율':>11}{'창 q80':>10}"
              f"{'분위':>8}   {'base_dt':>8}{'완전창':>7}{'기여날':>7}")
        for axis in ("달력", "조사"):
            for item in ITEMS:
                for d in COVERAGE:
                    c = cells[(axis, item, d)]
                    q = "—" if c["quantile"] is None else f"{c['quantile']:.0f}분위"
                    print(f"│  {axis:<5}{item:<5}{d:>3}  {_fmt(c['headroom']):>11}"
                          f"{_fmt(c['q80']):>10}{q:>8}   "
                          f"{c['bases']:>8}{c['full']:>7}{c['days']:>7}")
        print("└─")
        print()


def compare_csv(path: Path, actuals: dict[tuple[dt.date, str], float]) -> None:
    """ML 이 준 CSV 의 ``actual`` 과 우리 SQL 이 낸 값을 **대조**한다.

    🔴 다르면 그 사실을 적는다 — 같아야 2026-09-10 표와 이을 수 있다.
    """
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    same = diff = missing = 0
    worst = (0.0, "")
    for r in rows:
        if not r["actual"].strip():
            continue
        key = (dt.date.fromisoformat(r["target_dt"]), r["item_nm"])
        ours = actuals.get(key)
        if ours is None:
            missing += 1
            continue
        theirs = float(r["actual"])
        gap = abs(ours - theirs) / theirs if theirs else 0.0
        if gap < 0.005:
            same += 1
        else:
            diff += 1
            if gap > worst[0]:
                worst = (gap, f"{key[0]} {key[1]}  우리 {ours:.1f} vs ML {theirs:.1f}")
    total = same + diff + missing
    print(f"CSV 대조 ({path.name})  대조 {total:,}건")
    print(f"   0.5% 안에서 같다   {same:,}")
    print(f"   다르다             {diff:,}" + (f"   최대 {worst[0]*100:.2f}%  {worst[1]}"
                                               if diff else ""))
    print(f"   우리에게 없다      {missing:,}")
    print()


def load_from_csv(path: Path) -> tuple[list[dict[str, Any]], dict[tuple[dt.date, str], float]]:
    """ML 재측정 CSV 를 **예측·실측 둘 다**의 원본으로 읽는다.

    🔴 2026-09-10 표가 선 자리가 여기다. 우리 DB 로 낸 수와 갈리면 **자료가 다른 것인지
      산식이 다른 것인지**를 이것으로 가른다 — 같은 산식을 두 원본에 걸어 보는 것이다.

    ⚠️ CSV 에는 ``is_filled`` 복사행이 **없다** (조사축 원본이다). 그래서 달력 축으로
      자르면 창 안 행 수가 DB 보다 적다. 그 사실이 곧 두 축의 차이다.
    """
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    forecasts = [
        {
            "base_dt": dt.date.fromisoformat(r["base_dt"]),
            "item_nm": r["item_nm"],
            "offset_days": int(r["offset_days"]),
            "target_dt": dt.date.fromisoformat(r["target_dt"]),
            "predicted": float(r["predicted"]),
            "upper": float(r["upper"]),
            "src_lead_biz_d": int(r["lead_biz_d"]),
            "is_filled": False,
        }
        for r in rows
    ]
    actuals = {
        (dt.date.fromisoformat(r["target_dt"]), r["item_nm"]): float(r["actual"])
        for r in rows
        if r["actual"].strip()
    }
    return forecasts, actuals


def _stamp() -> str:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=False).stdout.strip()
    return sha or "(git 없음)"


def main() -> None:
    ap = argparse.ArgumentParser(description="컷 여유율을 두 축 × 두 창으로 잰다 (읽기 전용)")
    ap.add_argument("--csv", type=Path, help="ML 재측정 CSV 와 actual 을 대조한다")
    ap.add_argument("--source", choices=("db", "csv"), default="db",
                    help="예측·실측의 원본. csv 로 두면 2026-09-10 표가 선 자리를 그대로 잰다")
    ap.add_argument("--at", default="", help="표에 박을 실측 시각 (예: 2026-09-14 01:3x KST)")
    args = ap.parse_args()

    if args.source == "csv":
        if not args.csv:
            ap.error("--source csv 는 --csv 경로가 있어야 한다")
        forecasts, actuals = load_from_csv(args.csv)
        origin = f"CSV {args.csv.name}"
        schema = "—"
    else:
        schema = get_db_schema()
        actuals = load_actuals()
        forecasts = load_forecasts(schema)
        origin = "실 DB"

    #  🔴 「어느 커밋 · 어느 자료 · 어느 시각」을 표에 박는다. 안 박으면 다음 사람이
    #    이 수가 언제 것인지 못 되짚는다 (2026-09-12 에 실제로 그 일이 났다).
    cutoff = max(k[0] for k in actuals) if actuals else dt.date(1900, 1, 1)
    print(f"원본 {origin} · 기준 커밋 {_stamp()} · 스키마 {schema} · "
          f"예측 {len(forecasts):,}행 · 실측 {len(actuals):,}칸 (마지막 {cutoff})")
    if args.at:
        print(f"실측 시각 {args.at}")

    results = {
        name: measure(forecasts, actuals, lo, hi, cutoff)
        for name, (lo, hi) in WINDOWS.items()
    }
    render(results)

    if args.csv and args.source == "db":
        compare_csv(args.csv, actuals)


if __name__ == "__main__":
    main()
