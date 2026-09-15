"""예측 질의응답 — 도구 넷. **전부 SELECT 다.**

🔴 **숫자는 여기서만 나온다.** 답변 문장이 어떻게 바뀌든, 값은 이 네 함수가 읽은
   것이어야 한다. 지어낸 숫자가 섞이면 그 답은 쓸 수 없다.

두 창고를 읽는다 (`app/ml/db.py` 의 연결 두 개).

```text
서비스 창고  haetdeul.ml_price_forecasts   D+1~D+18 (달력일) · 매입 파트가 읽는 표
원본 창고    prediction_log                 채점 기록 · 리드 0(당일)
```

★ **당일 값이 전달표에 없다.** `offset_days` 가 1~18 이라 «오늘» 칸이 아예 없다.
  그래서 오늘을 물으면 원본 창고의 리드 0 을 읽고, **답에 출처를 밝힌다.**
  두 표는 축이 달라서(달력일 vs 영업일) 출처를 안 밝히면 나중에
  «왜 두 값이 다르냐» 가 나온다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.ml.db import fetch_all, fetch_one, get_db_schema

#: 운영 번들만 읽는다. 실험 번들(`ops-*` 붙임표)이 섞이면 조용히 다른 값이 나온다.
OPS_MODEL = {"AUC": "ops_auc", "WHSL": "ops_whsl", "RTL": "ops_rtl"}

#: 봉인 개봉(2026-09-01) 실측 절대 오차. **화면과 같은 값을 쓰기 위한 표다.**
#:
#: 조건 — 운영 모델 · 홀드아웃 2024~2025 · 486 기준일 · 리드타임 3 이상.
#: 경락가 세 칸은 화면(`app/api/forecast/query.py::_ACCURACY`)에 그대로 적혀 있고,
#: 나머지 여섯 칸은 같은 실행의 값이다 (`CLAUDE.md` 절대 오차표).
SEALED_ACCURACY: dict[tuple[str, str], dict[str, Any]] = {
    ("AUC", "배추"):  {"avg": "963원", "err": "190원", "pct": "19.7"},
    ("AUC", "무"):    {"avg": "771원", "err": "144원", "pct": "18.6"},
    ("AUC", "양파"):  {"avg": "1,098원", "err": "98원", "pct": "9.0"},
    ("WHSL", "배추"): {"avg": "1,603원", "err": "296원", "pct": "18.5"},
    ("WHSL", "무"):   {"avg": "1,108원", "err": "158원", "pct": "14.3"},
    ("WHSL", "양파"): {"avg": "1,347원", "err": "99원", "pct": "7.3"},
    ("RTL", "배추"):  {"avg": "4,814원", "err": "614원", "pct": "12.7"},
    ("RTL", "무"):    {"avg": "2,497원", "err": "241원", "pct": "9.7"},
    ("RTL", "양파"):  {"avg": "2,231원", "err": "184원", "pct": "8.3"},
}

#: 이 값이 나온 실행. 답변에 조건 없이 숫자만 적지 않기 위해 같이 내보낸다.
SEALED_SOURCE = "봉인 개봉 2026-09-01 · 홀드아웃 2024~2025 · 486 기준일 · 리드타임 3 이상"


_LATEST_BASE_SQL = """
SELECT MAX(base_dt) AS base_dt
  FROM {schema}.ml_price_forecasts
 WHERE (%s::date IS NULL OR base_dt <= %s::date)
"""

_ROWS_SQL = """
SELECT base_dt, target_dt, offset_days, src_lead_biz_d,
       predicted, lower, upper, current_price, unit,
       is_filled, is_gated, gate_reason, band_method,
       use_recommended, quality_note,
       market_name, grade_name, spec_desc,
       model_version, generated_at
  FROM {schema}.ml_price_forecasts
 WHERE item_nm = %s AND target_kind = %s AND base_dt = %s
   AND target_dt = ANY(%s)
 ORDER BY offset_days
"""

#: 당일(리드 0). 원본 창고에는 있고 전달표에는 없다.
_TODAY_SQL = """
SELECT base_dt, target_dt, lead_biz_d,
       pred_prc AS predicted, pred_lo AS lower, pred_hi AS upper,
       anchor_prc AS current_price, unit, gated AS is_gated, gate_reason,
       band_method, model_ver AS model_version, model_created_at
  FROM prediction_log
 WHERE model_ver = %s AND item_nm = %s AND base_dt = %s AND lead_biz_d = 0
 LIMIT 1
"""

_LATEST_SOURCE_BASE_SQL = """
SELECT MAX(base_dt) AS base_dt FROM prediction_log WHERE model_ver = %s
"""

_USABILITY_SQL = """
SELECT use_recommended, quality_note, band_method, is_gated, gate_reason
  FROM {schema}.ml_price_forecasts
 WHERE item_nm = %s AND target_kind = %s
 ORDER BY base_dt DESC, offset_days
 LIMIT 1
"""


def latest_base_date(as_of: date | None = None) -> date | None:
    """전달표의 최신 기준일. `as_of` 를 주면 그 날 이하에서 고른다 (미래를 안 본다)."""
    schema = get_db_schema()
    row = fetch_one(_LATEST_BASE_SQL.format(schema=schema), (as_of, as_of))
    return row["base_dt"] if row else None


def forecast_rows(item: str, kind: str, base_dt: date, targets: list[date]) -> list[dict[str, Any]]:
    """전달표에서 여러 대상일을 **한 번에** 읽는다. 날짜마다 묻지 않는다."""
    if not targets:
        return []
    schema = get_db_schema()
    return fetch_all(_ROWS_SQL.format(schema=schema), (item, kind, base_dt, list(targets)))


def today_row(item: str, kind: str) -> dict[str, Any] | None:
    """당일(리드 0) 한 행. **원본 창고**에서 읽는다 — 전달표에는 없는 값이다."""
    model = OPS_MODEL[kind]
    latest = fetch_one(_LATEST_SOURCE_BASE_SQL, (model,), source=True)
    if not latest or not latest["base_dt"]:
        return None
    return fetch_one(_TODAY_SQL, (model, item, latest["base_dt"]), source=True)


def accuracy(item: str, kind: str) -> dict[str, Any] | None:
    """그 조합이 얼마나 맞히나. **화면에 적힌 값과 같은 표를 쓴다.**

    🔴 `prediction_log` 로 다시 재지 않는다. 거기엔 실험용 모델이 섞여 있고,
       리드·기간을 어떻게 자르느냐에 따라 값이 달라진다. 실제로 같은 조합을
       전 구간으로 집계하니 15.1% 가 나왔다 — 화면은 19.7% 를 적고 있다.
       **한 사실에 두 숫자가 돌아다니면 어느 쪽이 맞는지 아무도 모른다.**

    출처: 봉인 개봉(2026-09-01) · 운영 모델 · 홀드아웃 2024~2025 · 486 기준일 ·
    리드타임 3 이상. 화면 `app/api/forecast/query.py` 의 `_ACCURACY` 와 같은 값이고,
    거기에 없는 중도매가·소매가는 같은 실행의 나머지 여섯 칸이다.
    """
    return SEALED_ACCURACY.get((kind, item))


def usability(item: str, kind: str) -> dict[str, Any] | None:
    """판단에 써도 되는 조합인가. `use_recommended=False` 면 쓰지 말라는 뜻이다."""
    schema = get_db_schema()
    return fetch_one(_USABILITY_SQL.format(schema=schema), (item, kind))
