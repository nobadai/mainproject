"""holiday_calendar.py — 실행일 판정의 **공휴일 축**. `v_ml_batch_days` 를 읽는다.

`execution_day.py` 는 순수하다. 이 파일이 그 모듈의 `HolidayCalendar` 포트에 꽂히는
**유일한 실물**이고, DB 를 아는 쪽은 여기뿐이다.

```text
execution_day   is_execution_day(day, calendar=…)   순수 · SQL 없음
holiday_calendar   v_ml_batch_days 를 읽는다        여기만 DB 를 안다
```

▣ **판정 근거는 `holiday_nm` 하나다** (`#282` §A)

```text
🟢 쓴다      holiday_nm IS NOT NULL  →  공휴일이다
🔴 안 쓴다   is_open · status · has_batch
```

★ **`is_open` 은 뜻이 흔들린다.** 실측(2026-09-04)에서 `2026-01-02` 가
  `is_open=False` 인데 `holiday_nm` 은 없고 배치는 있다. `2026-01-03`(토)은
  `is_open=True` 다. 뷰의 주석대로 `is_open` 은 **가락 경매일**이고 토요일도 참이다.
  우리가 묻는 것은 *"공휴일인가"* 하나이므로 **그 이름의 칸만** 본다.

★ **`has_batch` 는 `#242` 가 이미 따로 본다.** 같은 사실을 두 곳에서 판정하지 않는다.

▣ **소유는 ML 이다.** 표도 뷰도 ML 이 만들고 ML 이 채운다
  (`database/ml_calendar_days.sql`). 여기서는 **읽기만** 한다.

🔴 **뷰가 없거나 그 날이 표에 없으면 "공휴일이 아니다" 라고 하지 않는다.**

  `CalendarNotCovered` 를 던진다. 조용히 거짓을 돌려주면 **달력이 끊긴 것과 평일인
  것이 같아진다** — 그때부터 아무도 둘을 구분할 수 없다.

  ⚠️ **이것은 이론이 아니다.** 뷰가 `WHERE c.dt <= CURRENT_DATE` 로 잘려 있어
    **미래 날짜는 애초에 안 들어온다.** 실측(2026-09-04):

    ```text
    haetdeul.ml_calendar_days    2025-09-04 ~ 2027-09-04   731행   ← 표는 2년치
    haetdeul.v_ml_batch_days     2025-09-04 ~ 2026-09-04   366행   ← 뷰는 오늘까지
    ```

    그래서 *"내일이 공휴일인가"* 는 이 뷰로 못 답한다. 시뮬레이션은 과거를 걷기
    때문에 관통 날짜(2026-01-05 · 01-06 · 01-07)는 전부 덮이지만, 오늘·미래를 묻는
    호출은 덮이지 않는다 — **그 사실이 예외로 드러나는 것이 이 설계의 요점이다.**
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from psycopg import sql

from app.finance.db import fetch_all, get_db_schema
from app.master.execution_day import CalendarNotCovered

__all__ = ["MlBatchDayCalendar", "get_calendar", "reset"]

#: ML 이 소유하는 뷰. **읽기만 한다.**
VIEW = "v_ml_batch_days"

Rows = list[dict[str, Any]]


def _read_view() -> Rows:
    """뷰를 통째로 한 번 읽는다. **날짜와 공휴일 이름만** 가져온다.

    ★ `SELECT *` 를 안 쓴다. 다른 칸을 안 가져오면 **나중에 누가 그 칸으로 판정하는
      일이 생기지 않는다** — `is_open` 으로 판정하지 않겠다는 결정(§A)을 조회 모양이
      거들게 한다.

    ★ 한 번에 다 읽는다. 실측 366행이라 날짜마다 묻는 것보다 싸고, 무엇보다
      **덮는 범위를 알 수 있다** — 범위를 모르면 *"이 날이 없다"* 를 사유로 못 적는다.
    """
    query = sql.SQL("SELECT dt, holiday_nm FROM {}.{} ORDER BY dt").format(
        sql.Identifier(get_db_schema()), sql.Identifier(VIEW)
    )
    return fetch_all(query)


class MlBatchDayCalendar:
    """`v_ml_batch_days` 로 답하는 공휴일 축. `HolidayCalendar` 구현체다.

    🔴 **프로세스 안에서 캐시한다.** `is_execution_day` 는 문 앞에서 매 요청 불린다 —
       캐시가 없으면 매입 실행마다 커넥션이 하나 더 열린다.

    ★ **성공만 캐시한다.** 실패까지 캐시하면 DB 가 잠깐 끊긴 뒤 **프로세스를 다시
      띄울 때까지** 공휴일 축이 죽은 채로 돈다. 실패는 다음 호출에서 다시 읽어 본다 —
      매입 실행은 이미 DB 를 여러 번 읽으므로 재시도 비용이 새로 생기는 것도 아니다.

    ⚠️ **캐시는 늙는다.** 뷰가 `CURRENT_DATE` 까지만 나오므로, 오래 떠 있는 프로세스는
      자정이 지나도 **어제까지의 달력**을 들고 있다. 그 날은 `CalendarNotCovered` 가
      되고 주말 축만 돈다 — 틀린 답을 내는 것이 아니라 **못 봤다고 말한다.** 새 날이
      필요하면 `reset()` 이 있다.
    """

    def __init__(self, *, read: Callable[[], Rows] | None = None) -> None:
        #: 조회 자리. 검사는 여기에 가짜를 꽂아 **DB 없이** 돈다.
        self._read = _read_view if read is None else read
        self._holidays: dict[date, bool] | None = None
        self._covers: tuple[date, date] | None = None

    def is_holiday(self, day: date) -> bool:
        """이 날이 공휴일인가. **`holiday_nm` 이 있으면 참.**

        :raises CalendarNotCovered: 뷰를 못 읽었거나 그 날이 표에 없을 때.
        """
        holidays = self._loaded()
        if day not in holidays:
            raise CalendarNotCovered(
                f"{day.isoformat()} 이 달력에 없다 ({VIEW} 는 {self._range()} 를 덮는다)"
                " — 공휴일인지 아닌지를 이 뷰로는 말할 수 없다"
            )
        return holidays[day]

    def _loaded(self) -> dict[date, bool]:
        if self._holidays is not None:
            return self._holidays
        try:
            rows = self._read()
        except Exception as exc:
            # 🔴 **못 읽은 것을 "공휴일이 없다" 로 만들지 않는다.** 어떤 이유로 못 읽었든
            #    답은 같다 — *"이 뷰로는 말할 수 없다."*
            raise CalendarNotCovered(
                f"{VIEW} 를 못 읽었다 ({type(exc).__name__}: {exc}) — 공휴일 축이 없다"
            ) from exc
        holidays = {row["dt"]: row["holiday_nm"] is not None for row in rows}
        if not holidays:
            # ★ 빈 표는 "공휴일이 하나도 없다" 가 아니라 **달력이 안 심겼다**는 사실이다.
            raise CalendarNotCovered(f"{VIEW} 가 비어 있다 — 달력이 안 심겼다")
        self._holidays = holidays
        self._covers = (min(holidays), max(holidays))
        return holidays

    def _range(self) -> str:
        if self._covers is None:
            return "범위 모름"
        first, last = self._covers
        return f"{first.isoformat()}~{last.isoformat()}"


# ── 프로세스 하나에 달력 하나 ───────────────────────────────────────────

_CALENDAR: MlBatchDayCalendar | None = None


def get_calendar() -> MlBatchDayCalendar:
    """이 프로세스의 공휴일 축.

    ★ **만드는 것은 안 터진다.** 뷰를 읽는 것은 첫 `is_holiday` 때다 — 달력이 죽었다는
      사실은 판정 자리에서 `CalendarNotCovered` 로 드러나야지, 문 앞에서 예외로
      터지면 **달력 때문에 매입 판단이 멈춘다.**
    """
    global _CALENDAR
    if _CALENDAR is None:
        _CALENDAR = MlBatchDayCalendar()
    return _CALENDAR


def reset() -> None:
    """캐시를 비운다. 검사용이고, 날이 바뀐 프로세스에도 쓸 수 있다."""
    global _CALENDAR
    _CALENDAR = None
