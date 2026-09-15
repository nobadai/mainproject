"""ML 예측 서비스.

적재와 조회 두 가지를 한다.

    push_forecasts()  원본 창고 -> 서비스 창고. 배치가 하루 한 번 부른다
    get_forecast()    서비스 창고에서 계약 형태로 읽는다.
                      purchase_agent.ports.get_forecast 가 이걸 쓰면 된다
"""

from datetime import date
from typing import Any

from app.ml import repository
from app.ml.db import fetch_all, get_db_schema
from app.ml.schemas import HORIZON_DAYS, ITEMS, DailyPoint, Forecast, TargetKind

_READ_SQL = """
SELECT base_dt, item_nm, target_kind, offset_days, target_dt,
       predicted, lower, upper, current_price, unit,
       model_version, generated_at, is_filled, is_gated,
       market_name, grade_name, spec_desc, quality_note, use_recommended
  FROM {schema}.ml_price_forecasts
 WHERE item_nm = %s AND target_kind = %s AND base_dt <= %s
   AND base_dt = (SELECT MAX(base_dt) FROM {schema}.ml_price_forecasts
                   WHERE item_nm = %s AND target_kind = %s AND base_dt <= %s)
 ORDER BY offset_days
"""


def push_forecasts(base_dt: date | None = None, items: tuple[str, ...] = ITEMS) -> dict[str, Any]:
    """원본 창고의 예측을 서비스 창고로 옮긴다.

    같은 기준일을 다시 부르면 덮어쓴다. 배치가 여러 번 돌아도 안전하다.
    """
    if base_dt is None:
        base_dt = repository.latest_base_date()
    if base_dt is None:
        raise RuntimeError("원본 창고에 예측이 없습니다. 배치가 돌았는지 확인하세요.")

    source = repository.read_source(base_dt, items)
    if not source:
        raise RuntimeError(f"{base_dt} 예측이 없습니다.")

    rows = repository.to_calendar_rows(source, base_dt)
    n = repository.upsert(rows, get_db_schema())
    filled = sum(1 for r in rows if r[13])
    return {
        "base_dt": base_dt.isoformat(),
        "source_rows": len(source),
        "loaded_rows": n,
        "filled_rows": filled,
        "items": sorted({r[1] for r in rows}),
    }


def get_forecast(item: str, as_of: date, target_kind: TargetKind = "AUC") -> Forecast:
    """계약 형태로 예측을 돌려준다.

    ``as_of`` **이하** 의 가장 최근 기준일을 쓴다. 그날 예측이 없으면
    전날 것을 준다 — 미래 정보를 쓰지 않으면서 값이 비지 않게 한다.
    """
    schema = get_db_schema()
    sql = _READ_SQL.format(schema=schema)
    rows = fetch_all(sql, (item, target_kind, as_of, item, target_kind, as_of))
    if not rows:
        raise LookupError(f"{item}·{target_kind}·{as_of} 이전 예측이 없습니다.")

    head = rows[0]
    return Forecast(
        as_of=head["base_dt"],
        item=head["item_nm"],
        target_kind=head["target_kind"],
        unit=head["unit"],
        current_price=head["current_price"],
        horizon_days=HORIZON_DAYS,
        model_version=head["model_version"],
        generated_at=head["generated_at"],
        daily=[
            DailyPoint(date=r["target_dt"], predicted=r["predicted"],
                       lower=r["lower"], upper=r["upper"])
            for r in rows
        ],
        market_name=head["market_name"],
        grade_name=head["grade_name"],
        spec_desc=head["spec_desc"],
        use_recommended=head["use_recommended"],
        quality_note=head["quality_note"],
        filled_count=sum(1 for r in rows if r["is_filled"]),
    )
