"""
market_calendar.py — **가락이 서는가.** 회차일 축의 원천이다.

🔴 **`holiday_calendar.py` 와 같은 표를 읽지만 다른 사실을 읽는다.**

```text
holiday_calendar   dt · holiday_nm   "공휴일인가"      마스터 문 앞 실행일 판정 (#282 §A)
market_calendar    dt · is_open      "장이 서는가"     매입 회차일 봉투 (#303 후속)
```

★ **한 표에 축이 둘이고, 소비자도 둘이다.** 같은 사실의 주인이 둘인 것이 아니라 **다른
  사실을 각자 읽는 것**이다. 두 물음이 실제로 다른 날을 가리킨다.

  ```text
  마스터   "그날 ML 예측이 있어 판단을 도는가"   장은 서도 예측이 없으면 안 돈다
  매입     "그날 시장에서 살 수 있는가"          예측이 없어도 장이 서면 살 수 있다
  ```

🔴 **`#282 §A` 를 뒤집는 것이 아니다.** 그 결정은 *마스터 문 앞 판정*에 관한 것이고
  거기서는 `holiday_nm` 이 맞다 — 토·일에는 ML 예측이 없기 때문이다(실측: 주말 기준일
  0건). 이 모듈은 **새 소비자**이지 그 결정의 예외가 아니다.

★ **왜 갈아탔나 — 실측 (2026년 365일 전수 · 2026-09-05)**

  ```text
                          주말+holiday_nm      is_open
  실제 못 사는 날 67일     맞힘 64               맞힘 67
  살 수 있는데 민다        56일  🔴             0일
  못 사는데 안 민다         3일  🔴             0일
  ```

  ⚠️ **과잉 56의 몸통이 토요일 45일이다.** 토요일에는 가락이 선다.

  ```text
  ml_calendar_days 732행 (2025-09-04 ~ 2027-09-05)   is_open NULL = 0건

          토요일 개장 / 휴장     일요일 개장
  2025         15 / 2                 0
  2026         45 / 7                 0
  2027         34 / 2                 0
  ```

  ★ **그래서 요일 판정을 하지 않는다.** 일요일은 3년 내내 개장 0일이라 `is_open` 이
    이미 잡고, 토요일은 대부분 개장이라 요일로 밀면 **틀린다.** 주말을 따로 보태면
    과잉 56이 그대로 남는다 — 합집합이 아니라 **갈아타는** 이유다.

  ★ 그리고 `holiday_nm` 으로는 **원리상 못 잡는 날**이 있다. `2026-01-02`(금) ·
    `02-19`(목) · `07-08`(수) 는 공휴일이 아닌데 장이 안 선다 (셋 다 `is_survey=t`).

⚠️ **`is_open` 이 "뜻이 흔들린다" 던 제 판단은 틀렸다** (2026-09-05 철회).

  `2026-01-02` 가 `is_open=f` 인데 공휴일이 아닌 것과 `2026-01-03`(토)이 `is_open=t`
  인 것을 근거로 삼았는데, **둘 다 정확한 값**이다 — 앞은 평일 휴장이고 뒤는 토요일
  개장이다. **칸이 흔들린 것이 아니라 "공휴일인가" 축을 대고 읽어서 흔들려 보였다.**

🔴 **`is_open` 하나만 읽는다.** `is_survey` · `status` · `has_batch` · `holiday_nm` 은
  안 본다 — `holiday_calendar.py` 가 반대 방향으로 지키는 것과 같은 규율이고, 조회
  모양이 그것을 거든다 (`SELECT *` 를 안 쓴다).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from psycopg import sql

from app.finance.db import fetch_all, get_db_schema
from app.master.execution_day import CalendarNotCovered
from app.master.holiday_calendar import TABLE

__all__ = ["MarketCalendar", "MlMarketDays", "get_market_calendar", "reset"]

Rows = list[dict[str, Any]]


class MarketCalendar(Protocol):
    """개장 축. **이 날 장이 서는가** 하나만 답한다.

    :raises CalendarNotCovered: 표를 못 읽었거나 그 날이 표에 없을 때.
        **거짓을 돌려주지 않는다** — `HolidayCalendar` 와 같은 태도다.
    """

    def is_market_open(self, day: date) -> bool: ...


def _read_table() -> Rows:
    """표를 통째로 한 번 읽는다. **날짜와 개장 여부만** 가져온다.

    ★ 표 이름은 `holiday_calendar.TABLE` 에서 가져온다. 두 모듈이 같은 표를 읽으므로
      **ML 이 표 이름을 바꾸면 한 자리만 고치면 된다.**
    """
    query = sql.SQL("SELECT dt, is_open FROM {}.{} ORDER BY dt").format(
        sql.Identifier(get_db_schema()), sql.Identifier(TABLE)
    )
    return fetch_all(query)


class MlMarketDays:
    """`ml_calendar_days` 로 답하는 개장 축. `MarketCalendar` 구현체다.

    ★ **`MlCalendarDays` 와 같은 모양이다** — 성공만 캐시하고, 못 읽으면 *"장이 선다"*
      로 만들지 않고 `CalendarNotCovered` 를 던진다. 두 축이 같은 규율 아래 있어야
      한쪽만 조용히 무너지는 날이 안 온다.

    ⚠️ **캐시를 따로 든다.** 두 축이 한 표를 읽지만 캐시를 공유하지 않는다 — 공유하면
      `SELECT` 목록을 합쳐야 하고, 그러면 *"안 가져온 칸으로는 판정할 수 없다"* 는
      규율이 두 모듈에서 동시에 풀린다. 732행을 두 번 읽는 값보다 그쪽이 비싸다.
    """

    def __init__(self, *, read: Callable[[], Rows] | None = None) -> None:
        #: 조회 자리. 검사는 여기에 가짜를 꽂아 **DB 없이** 돈다.
        self._read = _read_table if read is None else read
        self._open: dict[date, bool] | None = None
        self._covers: tuple[date, date] | None = None

    def is_market_open(self, day: date) -> bool:
        """이 날 가락이 서는가. **`is_open` 그대로.**

        :raises CalendarNotCovered: 표를 못 읽었거나 그 날이 표에 없을 때.
        """
        opens = self._loaded()
        if day not in opens:
            raise CalendarNotCovered(
                f"{day.isoformat()} 이 달력에 없다 ({TABLE} 는 {self._range()} 를 덮는다)"
                " — 장이 서는지 아닌지를 이 표로는 말할 수 없다"
            )
        return opens[day]

    def _loaded(self) -> dict[date, bool]:
        if self._open is not None:
            return self._open
        try:
            rows = self._read()
        except Exception as exc:
            # 🔴 **못 읽은 것을 "장이 선다" 로 만들지 않는다.** 그렇게 하면 휴장일에
            #    회차를 세우고, 아무도 그것을 모른다.
            raise CalendarNotCovered(
                f"{TABLE} 를 못 읽었다 ({type(exc).__name__}: {exc}) — 개장 축이 없다"
            ) from exc
        opens = {row["dt"]: bool(row["is_open"]) for row in rows}
        if not opens:
            raise CalendarNotCovered(f"{TABLE} 가 비어 있다 — 달력이 안 심겼다")
        self._open = opens
        self._covers = (min(opens), max(opens))
        return opens

    def _range(self) -> str:
        if self._covers is None:
            return "범위 모름"
        first, last = self._covers
        return f"{first.isoformat()}~{last.isoformat()}"


# ── 프로세스 하나에 달력 하나 ───────────────────────────────────────────

_MARKET: MlMarketDays | None = None


def get_market_calendar() -> MlMarketDays:
    """이 프로세스의 개장 축.

    ★ **만드는 것은 안 터진다.** 표를 읽는 것은 첫 `is_market_open` 때다 —
      `get_calendar()` 와 같은 이유다.
    """
    global _MARKET
    if _MARKET is None:
        _MARKET = MlMarketDays()
    return _MARKET


def reset() -> None:
    """캐시를 비운다. 검사용이고, 날이 바뀐 프로세스에도 쓸 수 있다."""
    global _MARKET
    _MARKET = None
