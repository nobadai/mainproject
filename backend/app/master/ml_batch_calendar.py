"""
ml_batch_calendar.py — **그날 ML 예측 배치가 도는가.** 스케줄러 판단 축의 원천이다.

🔴 **`market_calendar.py` 와 같은 표를 읽지만 다른 사실을 읽는다** (2026-09-13).

```text
holiday_calendar    dt · holiday_nm   "공휴일인가"          마스터 문 앞 실행일 판정
market_calendar     dt · is_open      "장이 서는가"         매입 회차일 봉투 · 스케줄러 ①
ml_batch_calendar   dt · is_survey    "예측 배치가 도는가"   스케줄러 NO_ML_BATCH
```

★ **한 표에 축이 셋이고, 셋이 실제로 다른 날을 가리킨다.** 같은 사실의 주인이 셋인
  것이 아니라 **다른 사실을 각자 읽는 것**이다.

  ```text
  토요일 대부분      is_open=t · is_survey=f    장은 서는데 예측 배치가 없다
  2026-01-02(금)     is_open=f · is_survey=t    공휴일이 아닌데 장이 안 선다
  ```

★★ **왜 세웠나 — 스케줄러가 「배치가 원래 없는 날」과 「예측이 늦는 날」을 못 갈랐다.**

  `is_open` 만 보면 토요일은 *"장이 선다"* 이고, 게이트는 `NONE_READY` 다. 그래서
  스케줄러가 그 날을 **ML 이 늦는 날**로 읽고 마감까지 기다린 뒤 `E4_NOT_STARTED` 로
  적었다 (`SIM-CHAIN-V9` 1~3월 · 그런 날 14일 · 매입 E4 42건).

  ```text
  ML 확인 (2026-09-13)   is_survey 는 「그날 예측 배치가 도는가」 축이다
  실측 걷기 구간 90일     is_survey == ml_price_forecasts 에 그날 행이 있는가   90 / 90
  ```

🔴 **`market_calendar.py` 에 이 칸을 안 넣는다.** 그 모듈이 답하는 질문은 *"그날
  시장에서 살 수 있는가"* 이고, 매입 봉투가 그 답을 쓴다. 한 객체가 두 질문에 답하면
  한쪽 소비자가 다른 쪽 칸으로 판정하는 날이 온다.

🔴 **요일로 판정하지 않는다.** 토요일에도 배치가 도는 날이 있을 수 있고 평일에도 없는
  날이 있다 (설날 · 대체공휴일). 달력이 말하는 그대로 읽는다.

🔴 **`is_survey` 하나만 읽는다.** `is_open` · `holiday_nm` · `status` · `has_batch` 는
  안 본다 — 조회 모양이 그것을 거든다 (`SELECT *` 를 안 쓴다).

🔴 **비어 있는 칸을 거짓으로 접지 않는다.** `is_survey` 가 NULL 인 날을 *"배치가
  없다"* 로 읽으면 그 날 판단이 **조용히** 빠진다. 못 읽은 것으로 던진다
  (`CalendarNotCovered`) — 스케줄러가 그것을 `BLOCKED` 로 받는다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from psycopg import sql

from app.finance.db import fetch_all, get_db_schema
from app.master.execution_day import CalendarNotCovered
from app.master.holiday_calendar import TABLE

__all__ = ["MlBatchCalendar", "MlBatchDays", "get_ml_batch_calendar", "reset"]

Rows = list[dict[str, Any]]


class MlBatchCalendar(Protocol):
    """배치 축. **이 날 ML 예측 배치가 도는가** 하나만 답한다.

    :raises CalendarNotCovered: 표를 못 읽었거나 그 날이 표에 없거나 칸이 비었을 때.
        **거짓을 돌려주지 않는다** — `MarketCalendar` 와 같은 태도다.
    """

    def has_ml_batch(self, day: date) -> bool: ...


def _read_table() -> Rows:
    """표를 통째로 한 번 읽는다. **날짜와 배치 여부만** 가져온다.

    ★ 표 이름은 `holiday_calendar.TABLE` 에서 가져온다. 세 모듈이 같은 표를 읽으므로
      **ML 이 표 이름을 바꾸면 한 자리만 고치면 된다.**
    """
    query = sql.SQL("SELECT dt, is_survey FROM {}.{} ORDER BY dt").format(
        sql.Identifier(get_db_schema()), sql.Identifier(TABLE)
    )
    return fetch_all(query)


class MlBatchDays:
    """`ml_calendar_days.is_survey` 로 답하는 배치 축. `MlBatchCalendar` 구현체다.

    ★ **`MlMarketDays` 와 같은 모양이다** — 성공만 캐시하고, 못 읽으면 *"배치가 있다"*
      로도 *"없다"* 로도 만들지 않고 `CalendarNotCovered` 를 던진다.

    ⚠️ **캐시를 따로 든다.** 이유는 `MlMarketDays` 가 적어 둔 그대로다 — 공유하면
      `SELECT` 목록을 합쳐야 하고, 그러면 *"안 가져온 칸으로는 판정할 수 없다"* 는
      규율이 두 모듈에서 동시에 풀린다.
    """

    def __init__(self, *, read: Callable[[], Rows] | None = None) -> None:
        #: 조회 자리. 검사는 여기에 가짜를 꽂아 **DB 없이** 돈다.
        self._read = _read_table if read is None else read
        self._batch: dict[date, bool | None] | None = None
        self._covers: tuple[date, date] | None = None

    def has_ml_batch(self, day: date) -> bool:
        """이 날 ML 예측 배치가 도는가. **`is_survey` 그대로.**

        :raises CalendarNotCovered: 표를 못 읽었거나 그 날이 표에 없거나 칸이 비었을 때.
        """
        batches = self._loaded()
        if day not in batches:
            raise CalendarNotCovered(
                f"{day.isoformat()} 이 달력에 없다 ({TABLE} 는 {self._range()} 를 덮는다)"
                " — 그날 예측 배치가 도는지를 이 표로는 말할 수 없다"
            )
        answer = batches[day]
        if answer is None:
            # 🔴 **NULL 을 "배치가 없다" 로 접지 않는다.** 접으면 그 날 판단이 조용히
            #    빠지고, 사람은 그것을 *"원래 배치가 없는 날"* 로 읽는다.
            raise CalendarNotCovered(
                f"{day.isoformat()} 의 is_survey 가 비었다 — 그날 예측 배치가 도는지 모른다"
            )
        return answer

    def _loaded(self) -> dict[date, bool | None]:
        if self._batch is not None:
            return self._batch
        try:
            rows = self._read()
        except Exception as exc:
            # 🔴 **못 읽은 것을 "배치가 없다" 로 만들지 않는다.** 그렇게 하면 DB 가 죽은
            #    날 판단이 통째로 빠지고, 그 사실이 `NO_ML_BATCH` 로 정상처럼 보인다.
            raise CalendarNotCovered(
                f"{TABLE} 를 못 읽었다 ({type(exc).__name__}: {exc}) — 배치 축이 없다"
            ) from exc
        batches: dict[date, bool | None] = {}
        for row in rows:
            칸 = row["is_survey"]
            batches[row["dt"]] = None if 칸 is None else bool(칸)
        if not batches:
            raise CalendarNotCovered(f"{TABLE} 가 비어 있다 — 달력이 안 심겼다")
        self._batch = batches
        self._covers = (min(batches), max(batches))
        return batches

    def _range(self) -> str:
        if self._covers is None:
            return "범위 모름"
        first, last = self._covers
        return f"{first.isoformat()}~{last.isoformat()}"


# ── 프로세스 하나에 달력 하나 ───────────────────────────────────────────

_BATCH: MlBatchDays | None = None


def get_ml_batch_calendar() -> MlBatchDays:
    """이 프로세스의 배치 축.

    ★ **만드는 것은 안 터진다.** 표를 읽는 것은 첫 `has_ml_batch` 때다 —
      `get_market_calendar()` 와 같은 이유다.
    """
    global _BATCH
    if _BATCH is None:
        _BATCH = MlBatchDays()
    return _BATCH


def reset() -> None:
    """캐시를 비운다. 검사용이고, 날이 바뀐 프로세스에도 쓸 수 있다."""
    global _BATCH
    _BATCH = None
