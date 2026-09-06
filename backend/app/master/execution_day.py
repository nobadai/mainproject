"""
execution_day.py — **실행일은 평일만**. 토·일에는 안 돈다.

시뮬레이션은 하루씩 앞으로 걸어가며 매입 판단을 만든다. 그 걸음을 **평일에만**
딛는다.

```text
월 화 수 목 금 토 일 월
 ●  ●  ●  ●  ●  ○  ○  ●     ● 실행일 (판단한다)   ○ 쉬는 날 (안 돈다)
```

★ **왜 주말을 거르는가.** 토·일에는 시장이 안 서서 **ML 예측이 없다.** 실측
  (2026-09-04)으로 ML 예측 기준일 12개가 전부 평일이고 주말 기준일은 0건이다.
  주말 대상일 값은 전부 `is_filled=True` — 직전 개장일 값의 복사다. 없는 값을
  복사본으로 채워 판단하면, 그 판단은 시장을 본 것이 아니라 **금요일을 두 번 본
  것**이다.

🔴 **이 모듈로 경과일수를 세면 안 된다.**

  주말에 **판단을 안 할 뿐**, 주말이 사라지는 것은 아니다. 재고는 토·일에도
  늙고, 도착일·지급일도 달력일로 온다. 그래서 금요일 다음 실행일은 월요일이고
  **그 사이는 3일**이다 — 1일이 아니다.

  ```text
  실행일        평일만          ← 이 모듈이 답한다
  경과일수      달력일 그대로    ← 이 모듈은 답하지 않는다
  ```

  경과일수의 주인은 **`app/master/verifier.py` 의 `_day_gap`** 이다 (123행,
  docstring: *"`YYYY-MM-DD` 두 개의 일수 차이. calendar day 다 — 영업일 보정
  없음 (N5)."*). 같은 사실의 주인이 둘이 되면 언젠가 둘이 갈리고, 갈린 날
  아무도 어느 쪽이 맞는지 말해 주지 않는다. 여기서 날짜 차를 세는 코드를 쓰지
  않는 이유이고, `tests/master/test_execution_day.py` 가 이 파일의 **원문을
  읽어** 그것을 막는다.

★ **공휴일은 달력을 주면 거른다 — 안 주면 못 거른다** (`#282`).

  ```text
  calendar 를 안 주면   주말만 본다        ← 오늘까지의 동작 그대로다
  calendar 를 주면      주말 + 공휴일
  ```

  기본값이 **오늘 동작**인 것이 이 포트의 전부다. 달력을 안 주는 호출은 하나도
  안 바뀐다. 설·추석·대체공휴일에도 달력 없이 물으면 이 모듈은 평일이라고 답한다.

  **같은 한계가 이미 적혀 있다** — `app/purchase_agent/constraints.yaml:62`
  (*"⚠️ **주말만 피한다 — 공휴일은 못 피한다.**"*). 그 자리는 아직 그대로다.

🔴 **이 모듈은 DB 를 부르지 않는다. 앞으로도 안 부른다.**

  ```text
  ① 검사가 DB 를 필요로 하게 된다 — 지금은 순수해서 가짜가 필요 없다
  ② 매입이 이 함수를 인용할 예정이다 (마스터 회신 §4.4)
     매입은 봉투만 받는 파트라 DB 를 못 부른다
  ```

  달력을 읽는 것은 `app/master/holiday_calendar.py` 의 일이고, 여기는 **그것을
  받는 구멍(`HolidayCalendar`)까지**다.

⚠️ **매입에 달력을 어떻게 전달할지는 `#282` 가 정하지 않았다.** 봉투에 실을지는
  별건이다. 이 모듈이 연 것은 포트뿐이고, 오늘 그 포트에 무언가를 꽂는 곳은
  `app/master/service.py` 한 곳이다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from app.master.calendar_walk import MAX_WALK_DAYS

__all__ = [
    "CalendarNotCovered",
    "ExecutionDayNotFound",
    "HolidayCalendar",
    "is_execution_day",
    "next_execution_day",
]

# `date.weekday()` 가 토요일에 주는 값. 이 값 미만이 평일이다 (월 0 … 금 4).
_SATURDAY = 5


class CalendarNotCovered(LookupError):
    """**그 날을 달력에서 못 봤다.** 공휴일이 아니라는 뜻이 **아니다.**

    🔴 **없음과 안 함을 구분한다.** 달력이 안 덮는 날을 *"평일"* 로 단정하면
       **달력이 끊긴 것과 평일인 것이 같아진다** — 그때부터 아무도 둘을 구분할
       수 없다. 그래서 구현체는 조용히 거짓을 돌려주지 않고 이것을 던진다.

    ★ **이 모듈은 이것을 잡지 않는다.** 잡아서 평일로 넘기면 위에 적은 그 일이
      벌어지고, 잡아서 공휴일로 넘기면 달력이 죽었다고 판단이 멈춘다. **부르는
      쪽이 정한다** — `service.py` 는 주말 판정만 계속 돌리고 *"공휴일 축을 못
      봤다"* 를 `skipped_checks` 에 남긴다.
    """


class ExecutionDayNotFound(RuntimeError):
    """상한까지 걸어도 실행일이 안 나왔다. **막고 사유를 낸다.**

    ★ 연휴가 길어서가 아니다 (가장 긴 실측 연휴가 닷새다 — `calendar_walk.py`).
      상한을 넘었다는 것은 **달력이 틀렸다**는 뜻에 가깝다. 걷기를 늘려서 지나가면
      틀린 달력이 그대로 판단에 들어간다.
    """


class HolidayCalendar(Protocol):
    """공휴일 축. **이 날이 공휴일인가** 하나만 답한다.

    🔴 **판정 근거는 `holiday_nm` 하나다** (`#282` §A). 구현체가 `ml_calendar_days`
       를 읽더라도 `is_open` · `is_survey` · `status` · `has_batch` 는 안 본다.

       ```text
       is_open      뜻이 흔들린다 — 실측 2026-01-02 는 is_open=False 인데
                    holiday_nm 이 없고 배치는 있다. 2026-01-03(토)은 is_open=True
       has_batch    `#242` 가 이미 따로 본다 — 같은 사실을 두 곳에서 판정하지 않는다
       ```

    :raises CalendarNotCovered: 달력을 못 읽었거나 그 날이 표에 없을 때.
        **거짓을 돌려주지 않는다.**
    """

    def is_holiday(self, day: date) -> bool: ...


def is_execution_day(day: date, *, calendar: HolidayCalendar | None = None) -> bool:
    """이 날 판단을 도는가. 월~금이면 참, 토·일이면 거짓.

    ★ **`calendar` 를 안 주면 날짜만 본다** — 공휴일은 모른다. 오늘까지의 동작이다.

    ★ **주말을 먼저 본다.** 토·일이면 달력에 묻지 않는다 — 답이 안 바뀌는데 묻기만
      하면, 달력이 안 덮는 주말에 `CalendarNotCovered` 가 나서 *"토요일인지도
      모르겠다"* 가 된다. 순수 함수로 답할 수 있는 것은 순수 함수가 답한다.

    :raises CalendarNotCovered: 평일인데 달력이 그 날을 안 덮을 때 (`calendar` 를
        준 경우에만). 조용히 평일로 넘기지 않는다.
    """
    if day.weekday() >= _SATURDAY:
        return False
    if calendar is None:
        return True
    return not calendar.is_holiday(day)


def next_execution_day(day: date, *, calendar: HolidayCalendar | None = None) -> date:
    """`day` **다음**의 실행일. `day` 자신은 세지 않는다.

    ```text
    목 → 금   금 → 월   토 → 월   일 → 월
    ```

    ⚠️ 금요일에 물으면 월요일이 나오지만, 그 사이가 **1일이라는 뜻이 아니다.**
      달력으로는 3일이다. 이 함수는 *"다음에 언제 도는가"* 만 답하고
      *"며칠 지났는가"* 는 답하지 않는다 (모듈 docstring 의 🔴).

    🔴 **상한이 있다** (`calendar_walk.MAX_WALK_DAYS`). 주말·공휴일이 이어져도 걷기는
       그 안에서 끝난다. `day_open.py` 가 뒤로 걸을 때 쓰는 것과 **같은 상수**이고,
       같은 이유다 — 그 이유는 `calendar_walk.py` 에 한 번만 적혀 있다.

    :raises ExecutionDayNotFound: 상한까지 걸어도 실행일이 없을 때.
    :raises CalendarNotCovered: 걷는 도중 달력이 안 덮는 날을 만났을 때.
    """
    following = day
    for _ in range(MAX_WALK_DAYS):
        following = following + timedelta(days=1)
        if is_execution_day(following, calendar=calendar):
            return following
    raise ExecutionDayNotFound(
        f"{day.isoformat()} 부터 {MAX_WALK_DAYS}일을 걸어도 실행일이 없다 —"
        " 연휴가 그렇게 길지 않으므로 달력이 틀렸을 가능성이 크다. 걷기를 늘리지 않는다"
    )
