"""ML 예측의 읽기·쓰기.

원본 창고에서 예측을 읽어 서비스 창고에 적재한다. 두 창고의 **날짜 세는 법이
다르다** — 그 변환이 이 파일의 핵심이다.

    원본 창고   개장일만 센다. 토·일·공휴일은 건너뛴다
    서비스 창고  달력 날짜를 그대로 센다 (D+1 ~ D+18)

원본의 18개장일은 달력으로 24~26일에 걸쳐 있다. 그래서 앞에서부터 18달력일을
잘라 쓰고, 장이 안 서는 날은 **직전 개장일 값을 그대로** 넣는다.
그 칸에는 ``is_filled`` 를 켜 둔다.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.ml.db import execute_many, fetch_all
from app.ml.schemas import HORIZON_DAYS, ITEMS, SPEC

KST = timezone(timedelta(hours=9))

#: 운영 모델만 읽는다. 실험 모델(model_*·old-*)이 섞이면 조용히 다른 값이 나간다.
OPS_MODELS = ("ops_auc", "ops_whsl", "ops_rtl")
KIND_OF = {"auc": "AUC", "whsl": "WHSL", "rtl": "RTL"}

_SOURCE_SQL = """
SELECT p.base_dt, p.target_dt, p.item_nm, p.lead_biz_d, p.target_kind, p.unit,
       p.anchor_prc, p.pred_prc, p.pred_lo, p.pred_hi, p.gated, p.gate_reason,
       p.model_ver, p.model_created_at,
       q.use_recommended, q.note AS quality_note
  FROM prediction_log p
  LEFT JOIN ref_prediction_quality q
         ON q.target_kind = p.target_kind AND q.item_nm = p.item_nm
 WHERE p.base_dt = %s
   AND p.model_ver = ANY(%s)
   AND p.item_nm = ANY(%s)
 ORDER BY p.target_kind, p.item_nm, p.lead_biz_d
"""

_LATEST_SQL = """
SELECT MAX(base_dt) AS base_dt FROM prediction_log WHERE model_ver = ANY(%s)
"""

_UPSERT_SQL = """
INSERT INTO {schema}.ml_price_forecasts
 (base_dt, item_nm, target_kind, offset_days, target_dt,
  predicted, lower, upper, current_price, unit, model_version, generated_at,
  src_lead_biz_d, is_filled, is_gated, gate_reason,
  market_name, grade_name, spec_desc, unit_weight_kg, quality_note, use_recommended)
VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s,%s)
ON CONFLICT (base_dt, item_nm, target_kind, offset_days) DO UPDATE SET
  target_dt=EXCLUDED.target_dt, predicted=EXCLUDED.predicted,
  lower=EXCLUDED.lower, upper=EXCLUDED.upper,
  current_price=EXCLUDED.current_price, unit=EXCLUDED.unit,
  model_version=EXCLUDED.model_version, generated_at=EXCLUDED.generated_at,
  src_lead_biz_d=EXCLUDED.src_lead_biz_d, is_filled=EXCLUDED.is_filled,
  is_gated=EXCLUDED.is_gated, gate_reason=EXCLUDED.gate_reason,
  market_name=EXCLUDED.market_name, grade_name=EXCLUDED.grade_name,
  spec_desc=EXCLUDED.spec_desc, unit_weight_kg=EXCLUDED.unit_weight_kg,
  quality_note=EXCLUDED.quality_note, use_recommended=EXCLUDED.use_recommended
"""


def latest_base_date() -> date | None:
    """원본 창고에서 예측이 있는 가장 최근 기준일."""
    rows = fetch_all(_LATEST_SQL, (list(OPS_MODELS),), source=True)
    return rows[0]["base_dt"] if rows else None


def read_source(base_dt: date, items: tuple[str, ...] = ITEMS) -> list[dict[str, Any]]:
    """원본 창고의 개장일 기준 예측을 읽는다."""
    return fetch_all(_SOURCE_SQL, (base_dt, list(OPS_MODELS), list(items)), source=True)


def to_calendar_rows(rows: list[dict[str, Any]], base_dt: date) -> list[tuple]:
    """개장일 예측을 달력일 D+1~D+18 로 다시 늘어놓는다.

    장이 안 서는 날은 직전 개장일 값을 그대로 쓰고 ``is_filled`` 를 켠다.
    첫 개장일 이전 칸은 만들지 않는다 — 없는 값을 지어내지 않는다.
    """
    generated = _generated_at(rows, base_dt)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["target_kind"], row["item_nm"]), []).append(row)

    out: list[tuple] = []
    for (kind, item), group in grouped.items():
        group.sort(key=lambda r: r["target_dt"])
        spec = SPEC[KIND_OF[kind]]
        by_date = {r["target_dt"]: r for r in group}
        current: dict[str, Any] | None = None
        for offset in range(1, HORIZON_DAYS + 1):
            day = base_dt + timedelta(days=offset)
            hit = by_date.get(day)
            if hit is not None:
                current, filled = hit, False
            elif current is not None:
                filled = True
            else:
                continue
            out.append((
                base_dt, item, KIND_OF[kind], offset, day,
                round(float(current["pred_prc"])),
                round(float(current["pred_lo"])),
                round(float(current["pred_hi"])),
                round(float(current["anchor_prc"])),
                current["unit"], current["model_ver"], generated,
                current["lead_biz_d"], filled, bool(current["gated"]),
                current["gate_reason"],
                spec["market"], spec["grade"], spec["desc"].get(item),
                spec["kg"].get(item),
                current["quality_note"], current["use_recommended"],
            ))
    return out


def upsert(rows: list[tuple], schema: str) -> int:
    """서비스 창고에 적재한다. 같은 기준일을 다시 넣으면 덮어쓴다."""
    if not rows:
        return 0
    return execute_many(_UPSERT_SQL.format(schema=schema), rows)


def _generated_at(rows: list[dict[str, Any]], base_dt: date) -> datetime:
    """예측을 만든 시각. 없으면 기준일 06:00 KST 로 둔다.

    **기준일보다 나중일 수 없다.** 나중이면 미래 정보로 과거를 맞힌 것이
    되어 성적이 무효가 된다. 계약 검사(``Forecast._no_lookahead``)도 막는다.
    """
    stamps = [r["model_created_at"] for r in rows if r.get("model_created_at")]
    fallback = datetime.combine(base_dt, time(6, 0), tzinfo=KST)
    if not stamps:
        return fallback
    latest = max(stamps)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=KST)
    return latest if latest.date() <= base_dt else fallback
