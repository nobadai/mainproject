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

import httpx
from psycopg import errors as pg_errors

from app.ml.console_proxy import console_origin
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
#:
#: ★ **사람 말로 적는다** (마스터 요청 · 2026-09-15). 전에는 «봉인 개봉 · 홀드아웃 ·
#:   리드타임 3 이상» 이었는데 그건 우리끼리 쓰는 말이라 화면에서 읽히지 않는다.
#:   **조건을 뺀 것이 아니라 같은 조건을 아는 말로** 바꿨다 — 언제 · 무엇으로 · 몇 일치인지가
#:   그대로 들어 있다.
SEALED_SOURCE = "2026-09-01 에 2024~2025 실제 가격 486일치로 채점한 값입니다"


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

# ── 배치 결과 · 점검 보고서 ────────────────────────────────────────────
#
# 🔴 **시간대가 표마다 다르다** (2026-09-16 실측).
#
# ```text
# batch_run.started_at    timestamptz · UTC 로 앉아 있다  -> AT TIME ZONE 으로 돌린다
# agent_report.ran_at     timestamp   · 이미 한국 시간이다 -> 그대로 자른다
# ```
#
# 한국 09:00 이 UTC 자정이라(CLAUDE.md §9) **UTC 로 날짜를 자르면 하루가 밀린다.**
# 09:00 배치가 전날 것으로 세어진다.

#: 배치 상태를 사람 말로. **답 문장에 코드 값을 적지 않는다** (마스터 요청).
BATCH_STATUS_LABEL: dict[str, str] = {
    "ok": "정상",
    "fail": "실패",
    "partial": "일부 실패",
    "running": "도는 중",
}

#: 배치 단계 이름을 사람 말로. `run_batch.py` 의 `STAGES` 설명과 같은 말이다.
#:
#: ★ 모르는 단계가 오면 **이름을 그대로 적는다.** 단계가 늘었을 때 «실패한 단계가
#:   있는데 무엇인지 안 알려주는» 답이 되는 것보다 낫다.
STAGE_LABEL: dict[str, str] = {
    "collect_auction": "경락가 수집",
    "collect_price": "중도매·소매가 수집",
    "collect_weather": "기상 수집",
    "collect_volume": "반입량 수집",
    "collect_econ": "경제지표 수집",
    "precheck": "수집 검사",
    "rebuild": "학습표 재생성",
    "predict": "추론",
    "shadow": "그림자 실행",
    "load": "예측 적재",
    "score": "실제값 채점",
    "push": "매입 파트로 넘기기",
}

#: 그날 AI 점검 보고서. 사람이 읽을 글이 `body` 에 통째로 들어 있다.
CHECK_REPORT = "claude_check"

#: 재학습 이야기를 담은 보고서 둘. 앞이 «후보를 찾았나», 뒤가 «견줘 보니 어떤가».
RETRAIN_REPORTS: tuple[str, ...] = ("재학습판정", "재학습검증")

_BATCH_RUN_SQL = """
SELECT run_id, started_at, finished_at, status, host, n_ok, n_fail, note
  FROM batch_run
 WHERE (started_at AT TIME ZONE 'Asia/Seoul')::date = %s
 ORDER BY started_at DESC
 LIMIT 1
"""

_FAILED_STAGE_SQL = """
SELECT seq, stage, ok, duration_s, message
  FROM batch_run_stage
 WHERE run_id = %s AND ok = false
 ORDER BY seq
"""

_REPORT_SQL = """
SELECT id, name, verdict, ran_at, payload, body
  FROM agent_report
 WHERE name = %s AND ran_at::date = %s
 ORDER BY ran_at DESC
 LIMIT 1
"""

#: 그날 같은 이름의 보고서 **전부**. 시간 오름차순 — 돈 순서가 읽는 순서다.
_REPORTS_SQL = """
SELECT id, name, verdict, ran_at, payload, body
  FROM agent_report
 WHERE name = %s AND ran_at::date = %s
 ORDER BY ran_at
"""

# ── 지금 무엇이 도나 ──────────────────────────────────────────────────
#
# 🔴 **이름으로는 알 수 없다.** `ops_auc` · `ops_whsl` · `ops_rtl` 은 모델을
#   갈아 끼워도 **그대로 둔다** — 매입 파트 필터가 이름 정확히 일치라 바꾸면
#   에러 없이 0건이 된다 (CLAUDE.md §5.11).
#
#   그래서 «현재 모델» 은 **이름 + 만든 날 + 학습 끝** 셋이 있어야 가려진다.
#   실제로 2026-09-15 저녁에 소매가 모델이 바뀌었는데, 이름이 같아 답에
#   그 사실이 한 글자도 안 남았다.

#: 지금 예측을 내고 있는 번들을 만든 시각. **최신 기준일 행에서 읽는다** —
#: 옛 기준일 행에는 교체 전 번들의 시각이 그대로 남아 있다 (실측: `ops_rtl` 이
#: 2026-09-15 까지 09-08 번들, 09-16 부터 09-12 번들).
_MODEL_NOW_SQL = """
SELECT model_ver, base_dt, model_created_at
  FROM prediction_log
 WHERE model_ver = %s
 ORDER BY base_dt DESC
 LIMIT 1
"""

#: 교체 이력. **아직 없는 표다** (2026-09-16 실측 · 두 창고 다 없음).
#: 다른 일꾼이 만들고 있어 칸 이름이 바뀔 수 있으므로 `*` 로 받아 키로 읽는다.
_CUTOVER_SQL = """
SELECT DISTINCT ON (kind) *
  FROM model_cutover
 ORDER BY kind, swapped_at DESC
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


def today_row(item: str, kind: str, base_dt: date | None = None) -> dict[str, Any] | None:
    """당일(리드 0) 한 행. **원본 창고**에서 읽는다 — 전달표에는 없는 값이다.

    🔴 **기준일을 받아 그날 것만 읽는다** (2026-09-15 · 룩어헤드를 막으려고 고침).
      전에는 원본 창고의 **전체 최신 기준일**을 읽었다. 화면(3000)이 2월을 걷고
      있어도 9월 15일 값이 나갔다 — 에러 없이 **미래 값이 섞이는** 자리였다.

      `base_dt` 는 그래프가 `as_of` 이하에서 고른 기준일이다. 안 주면 예전처럼
      최신을 읽는다 — 시험용 입구(`GET /ml/qa`)는 as_of 가 없어서다.
    """
    model = OPS_MODEL[kind]
    if base_dt is None:
        latest = fetch_one(_LATEST_SOURCE_BASE_SQL, (model,), source=True)
        if not latest or not latest["base_dt"]:
            return None
        base_dt = latest["base_dt"]
    return fetch_one(_TODAY_SQL, (model, item, base_dt), source=True)


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


def batch_run(on: date) -> dict[str, Any] | None:
    """그날 배치 **한 행**. 여러 번 돌았으면 **마지막 것**을 준다.

    ★ 하루에 두 행이 있는 날이 실제로 있다 (2026-09-14 실측 · run 72 · 73).
      토요일 06:00 경제지표 작업과 09:00 일별 배치가 같은 한국 날짜에 든다.
      물어보는 사람이 아는 «오늘 배치» 는 나중 것이므로 시작 시각 내림차순 첫 행이다.
    """
    return fetch_one(_BATCH_RUN_SQL, (on,), source=True)


def failed_stages(run_id: int) -> list[dict[str, Any]]:
    """그 실행에서 **실패한 단계만**. 성공한 단계는 세지 않는다 — 개수는 배치 행에 있다."""
    return fetch_all(_FAILED_STAGE_SQL, (run_id,), source=True)


def agent_report(name: str, on: date) -> dict[str, Any] | None:
    """그날 그 이름의 보고서 **한 건** (가장 나중 것).

    ★ 하루에 한 건만 나오는 보고서용이다 (`claude_check`). 여러 건 나오는
      재학습 보고서는 `agent_reports()` 로 **전부** 읽는다.
    """
    return fetch_one(_REPORT_SQL, (name, on), source=True)


def agent_reports(name: str, on: date) -> list[dict[str, Any]]:
    """그날 그 이름의 보고서 **전부** (돈 순서).

    🔴 **하루에 여러 건이 남는다.** `재학습판정`·`재학습검증` 은 가격 종류마다
      한 건씩 나온다 (2026-09-16 실측 · 판정 3건 · 검증 2건). 마지막 하나만
      읽었더니 **경락가 검증의 «후보가 나쁩니다» 가 답에서 통째로 빠졌다.**

    🔴 **payload 에 가격 종류 칸이 아직 없다** (2026-09-16 실측). 어느 종류인지는
      제목 맨 앞 낱말에만 있고, 검증 보고서는 그것도 없다 — 답에서 «(가격 종류
      미상)» 으로 적는다. 지어내지 않는다.
    """
    return fetch_all(_REPORTS_SQL, (name, on), source=True)


def current_models() -> dict[str, Any]:
    """지금 도는 모델 셋. 돌려주는 것은 `{"models": [...], "cutover_read": ...}`.

    ```text
    이름 · 만든 날   prediction_log 의 최신 기준일 행       (늘 있다)
    학습 끝 · 교체   model_cutover 의 kind 별 최신 행       (아직 없을 수 있다)
    ```

    `cutover_read` 는 셋 중 하나다.

    ```text
    ok      읽었다 (행이 없을 수는 있다)
    absent  표가 아직 없다        -> 답에 «교체 이력 없음»
    error   읽다가 터졌다          -> 답에 그렇게 적는다
    ```

    🔴 **표가 없다고 죽지 않는다.** 이름과 만든 날만으로도 답할 것이 있다.
    """
    cutovers: dict[str, dict[str, Any]] = {}
    cutover_read = "ok"
    try:
        for row in fetch_all(_CUTOVER_SQL, source=True):
            cutovers[str(row.get("kind") or "").upper()] = row
    except pg_errors.UndefinedTable:
        cutover_read = "absent"
    except Exception:                                        # noqa: BLE001
        cutover_read = "error"

    models: list[dict[str, Any]] = []
    for kind, model_ver in OPS_MODEL.items():
        now = fetch_one(_MODEL_NOW_SQL, (model_ver,), source=True) or {}
        cut = cutovers.get(kind) or {}
        models.append({
            "kind": kind,
            "model_ver": model_ver,
            "created_at": now.get("model_created_at"),
            "base_dt": now.get("base_dt"),
            "train_end": cut.get("new_train_end"),
            "last_swapped_at": cut.get("swapped_at"),
            "last_swap_note": cut.get("note"),
            #   ★ 시각을 아는가 (`time_known`). 백필한 행은 백업 폴더 **이름**에서
            #     날짜만 되짚은 것이 있어 시각이 `00:00` 으로 앉아 있다.
            #
            #   🔴 **칸이 아직 없을 수 있다** (2026-09-16 실측: 없다). `SELECT *` 로
            #     읽으니 그때는 열쇠가 아예 안 오고 `None` 이 된다 — `False`(모른다)
            #     와 다른 값이다. 답에서도 다르게 적는다.
            "last_swap_time_known": cut.get("time_known"),
        })
    return {"models": models, "cutover_read": cutover_read}


# ── 바꿀 모델이 있나 ──────────────────────────────────────────────────
#
# 🔴 **이것만 DB 가 아니라 HTTP 다.** 나머지는 전부 SELECT 인데, 이 물음의 답은
#   우리 DB 에 없다. 재학습 그래프의 체크포인트와 배치가 남긴 파일이 ML 저장소
#   쪽에 있고, 둘을 **겹쳐야** 답이 나온다.
#
#   ```text
#   파일(_retrain_pending.json)   무엇을 견줬나 — 배치가 찍어 둔 그때의 사진
#   그래프 체크포인트              아직 사람 답을 기다리나 — 지금 이 순간
#   ```
#
#   사람이 「모델 업데이트」 를 누르면 **그래프만 바뀌고 파일은 그대로 남는다.**
#   파일만 보면 누른 뒤에도 계속 «바꿔야 합니다» 가 뜬다 — 실제로 그랬고
#   ML 쪽 `/retrain/pending` 이 2026-09-09 에 그것을 고쳤다. 그 창구를 그대로
#   쓴다. 여기서 다시 판정하지 않는다.
#
#   그래서 `agent_report` 로는 대신할 수 없다. 보고서는 «후보가 낫다» 까지만
#   말하고 «아직 안 눌렀다» 는 모른다.

#: **짧게 기다린다.** 이건 사람이 채팅에서 답을 기다리는 길목이다. ML 콘솔이
#: 안 떠 있을 때 30초를 붙들면 답 전체가 그만큼 늦는다 — 못 읽으면 한 줄로
#: 말하고 넘어가는 편이 낫다 (`_perf_section`).
PENDING_TIMEOUT = httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=2.0)

#: 사람이 눌러야 할 결정이 있는 가격 종류. **여기 없는 값은 버린다** —
#: 버튼을 만들 수 없는 종류를 답에 적으면 누를 수 없는 버튼이 나간다.
PENDING_KINDS = ("auc", "whsl", "rtl")


def retrain_pending() -> list[dict[str, Any]]:
    """사람이 눌러야 할 재학습 결정. 없으면 빈 목록, **못 읽으면 터진다.**

    🔴 **못 읽은 것을 «없다» 로 돌려주지 않는다.** 둘은 다른 사실이고, 답에
      적는 말도 다르다 — 앞은 «확인 불가», 뒤는 «후보 없음» 이다. 여기서
      섞으면 ML 콘솔이 죽어 있는 동안 화면이 «바꿀 것 없음» 으로 보인다.
    """
    origin, _hint = console_origin()
    with httpx.Client(timeout=PENDING_TIMEOUT) as client:
        response = client.get(f"{origin}/retrain/pending")
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError(f"/retrain/pending 이 사전이 아닌 것을 줬습니다: {type(body)}")
    return [
        row for row in (body.get("pending") or [])
        if isinstance(row, dict) and str(row.get("kind") or "").lower() in PENDING_KINDS
    ]
