"""holiday_calendar.py — 실행일 판정의 **공휴일 축**. `ml_calendar_days` 를 읽는다.

`execution_day.py` 는 순수하다. 이 파일이 그 모듈의 `HolidayCalendar` 포트에 꽂히는
**유일한 실물**이고, DB 를 아는 쪽은 여기뿐이다.

```text
execution_day   is_execution_day(day, calendar=…)   순수 · SQL 없음
holiday_calendar   ml_calendar_days 를 읽는다       여기만 DB 를 안다
```

▣ **판정 근거는 `holiday_nm` 하나다** (`#282` §A)

```text
🟢 쓴다      holiday_nm IS NOT NULL  →  공휴일이다
🔴 안 쓴다   is_open · status · has_batch
```

★ **`is_open` 은 뜻이 흔들린다.** 실측(2026-09-04)에서 `2026-01-02` 가
  `is_open=False` 인데 `holiday_nm` 은 없고 배치는 있다. `2026-01-03`(토)은
  `is_open=True` 다. 표의 주석대로 `is_open` 은 **가락 경매일**이고 토요일도 참이다.
  우리가 묻는 것은 *"공휴일인가"* 하나이므로 **그 이름의 칸만** 본다.

★ **`has_batch` 는 `#242` 가 이미 따로 본다.** 같은 사실을 두 곳에서 판정하지 않는다.

▣ **소유는 ML 이다.** 표도 뷰도 ML 이 만들고 ML 이 채운다 — 둘 다
  `database/ml_calendar_days.sql` 한 파일에 있다. 여기서는 **읽기만** 한다.
  ML 이 뷰만 보고 있다가 놀라지 않도록 적어 둔다 — **마스터가 읽는 것은 표다.**

▣ **왜 뷰가 아니라 표인가** (`#298`). 처음에는 `v_ml_batch_days` 를 읽었다.

  ```text
  ① 옮겼다 — 실측(2026-09-05)
     haetdeul.ml_calendar_days   2025-09-04 ~ 2027-09-05   732행   공휴일 44일
     haetdeul.v_ml_batch_days    2025-09-04 ~ 2026-09-05   367행   공휴일 22일
     뷰는 WHERE c.dt <= CURRENT_DATE 로 잘려 있어 **공휴일의 절반을 가렸다.**

  ② 뷰가 틀린 것이 아니다 — 그 컷은 **배치 상태**(has_batch · status)의 제약이다.
     미래의 배치 상태는 뜻이 없으므로 배치 축에는 그 컷이 맞다.
     **공휴일 축이 한 뷰에 묶여서 그 제약을 물려받았을 뿐이다.**
     공휴일의 주인은 표이고, 뷰는 그 표에 배치 축을 얹은 것이다.

  ③ 그래도 CalendarNotCovered 는 남는다 — 2027-09-05 밖은 여전히 못 답한다.
     **덮는 범위가 넓어진 것이지 무한해진 것이 아니다.**
  ```

  ⚠️ **이것은 이론이 아니다.** 뷰였을 때 `2026-09-05`(토)에 이런 문장이 남았다.

  ```text
  공휴일 축: 다음 실행일을 찾다 달력 밖으로 나갔다 — 2026-09-07 이 달력에 없다
  ```

  금요일 거절 문구가 *"다음 실행일은 월요일"* 이라 하는데 **그 월요일이 공휴일이어도
  몰랐다.**

🔴 **표를 못 읽거나 그 날이 표에 없으면 "공휴일이 아니다" 라고 하지 않는다.**

  `CalendarNotCovered` 를 던진다. 조용히 거짓을 돌려주면 **달력이 끊긴 것과 평일인
  것이 같아진다** — 그때부터 아무도 둘을 구분할 수 없다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from psycopg import sql

from app.finance.db import fetch_all, get_db_schema
from app.master.execution_day import CalendarNotCovered

__all__ = ["MlCalendarDays", "get_calendar", "reset"]

#: ML 이 소유하는 **표**. 공휴일의 주인이다. **읽기만 한다.**
TABLE = "ml_calendar_days"

Rows = list[dict[str, Any]]


def _read_table() -> Rows:
    """표를 통째로 한 번 읽는다. **날짜와 공휴일 이름만** 가져온다.

    ★ `SELECT *` 를 안 쓴다. 다른 칸을 안 가져오면 **나중에 누가 그 칸으로 판정하는
      일이 생기지 않는다** — `is_open` 으로 판정하지 않겠다는 결정(§A)을 조회 모양이
      거들게 한다. 표에는 `is_survey` 도 있으므로 그 규율이 더 필요하다.

    ★ 한 번에 다 읽는다. 실측 732행이라 날짜마다 묻는 것보다 싸고, 무엇보다
      **덮는 범위를 알 수 있다** — 범위를 모르면 *"이 날이 없다"* 를 사유로 못 적는다.
    """
    query = sql.SQL("SELECT dt, holiday_nm FROM {}.{} ORDER BY dt").format(
        sql.Identifier(get_db_schema()), sql.Identifier(TABLE)
    )
    return fetch_all(query)


class MlCalendarDays:
    """`ml_calendar_days` 로 답하는 공휴일 축. `HolidayCalendar` 구현체다.

    🔴 **프로세스 안에서 캐시한다.** `is_execution_day` 는 문 앞에서 매 요청 불린다 —
       캐시가 없으면 매입 실행마다 커넥션이 하나 더 열린다.

    ★ **성공만 캐시한다.** 실패까지 캐시하면 DB 가 잠깐 끊긴 뒤 **프로세스를 다시
      띄울 때까지** 공휴일 축이 죽은 채로 돈다. 실패는 다음 호출에서 다시 읽어 본다 —
      매입 실행은 이미 DB 를 여러 번 읽으므로 재시도 비용이 새로 생기는 것도 아니다.

    ⚠️ **캐시는 늙는다.** ML 이 매일 표를 갱신하므로 오래 떠 있는 프로세스는 **읽은
      시점의 달력**을 들고 있다. 다만 표가 앞으로 1년치를 덮으므로 자정을 넘겨도
      오늘·내일이 빠지지 않는다 — 뷰를 읽던 때와 달라진 점이다(`#298`). 그래도 범위
      끝에 닿으면 `CalendarNotCovered` 가 되고 주말 축만 돈다 — 틀린 답을 내는 것이
      아니라 **못 봤다고 말한다.** 새로 읽어야 하면 `reset()` 이 있다.
    """

    def __init__(self, *, read: Callable[[], Rows] | None = None) -> None:
        #: 조회 자리. 검사는 여기에 가짜를 꽂아 **DB 없이** 돈다.
        self._read = _read_table if read is None else read
        self._holidays: dict[date, bool] | None = None
        self._covers: tuple[date, date] | None = None

    def is_holiday(self, day: date) -> bool:
        """이 날이 공휴일인가. **`holiday_nm` 이 있으면 참.**

        :raises CalendarNotCovered: 표를 못 읽었거나 그 날이 표에 없을 때.
        """
        holidays = self._loaded()
        if day not in holidays:
            raise CalendarNotCovered(
                f"{day.isoformat()} 이 달력에 없다 ({TABLE} 는 {self._range()} 를 덮는다)"
                " — 공휴일인지 아닌지를 이 표로는 말할 수 없다"
            )
        return holidays[day]

    def _loaded(self) -> dict[date, bool]:
        if self._holidays is not None:
            return self._holidays
        try:
            rows = self._read()
        except Exception as exc:
            # 🔴 **못 읽은 것을 "공휴일이 없다" 로 만들지 않는다.** 어떤 이유로 못 읽었든
            #    답은 같다 — *"이 표로는 말할 수 없다."*
            raise CalendarNotCovered(
                f"{TABLE} 를 못 읽었다 ({type(exc).__name__}: {exc}) — 공휴일 축이 없다"
            ) from exc
        holidays = {row["dt"]: row["holiday_nm"] is not None for row in rows}
        if not holidays:
            # ★ 빈 표는 "공휴일이 하나도 없다" 가 아니라 **달력이 안 심겼다**는 사실이다.
            raise CalendarNotCovered(f"{TABLE} 가 비어 있다 — 달력이 안 심겼다")
        self._holidays = holidays
        self._covers = (min(holidays), max(holidays))
        return holidays

    def _range(self) -> str:
        if self._covers is None:
            return "범위 모름"
        first, last = self._covers
        return f"{first.isoformat()}~{last.isoformat()}"


# ── 프로세스 하나에 달력 하나 ───────────────────────────────────────────

_CALENDAR: MlCalendarDays | None = None


def get_calendar() -> MlCalendarDays:
    """이 프로세스의 공휴일 축.

    ★ **만드는 것은 안 터진다.** 표를 읽는 것은 첫 `is_holiday` 때다 — 달력이 죽었다는
      사실은 판정 자리에서 `CalendarNotCovered` 로 드러나야지, 문 앞에서 예외로
      터지면 **달력 때문에 매입 판단이 멈춘다.**
    """
    global _CALENDAR
    if _CALENDAR is None:
        _CALENDAR = MlCalendarDays()
    return _CALENDAR


def reset() -> None:
    """캐시를 비운다. 검사용이고, 날이 바뀐 프로세스에도 쓸 수 있다."""
    global _CALENDAR
    _CALENDAR = None
