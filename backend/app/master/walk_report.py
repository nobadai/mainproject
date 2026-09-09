"""walk_report.py — **걷기 성적표.** 「행이 없다」를 두 가지로 가른다.

★ **표를 새로 만들지 않았다** (2026-09-09 · `Master 19.0`). 걷기 성적표는 이미
  `master_agent_runs` 안에 있고, `sim_run_id` 로 가르면 나온다.

🔴 **이 판의 값은 하나다 — 표는 없는 것을 말할 수 없다.**

  ```text
  그날 행   실행일인가   판정                        뜻
  있음      ○           WALKED                     🟢 돌았다
  없음      ✗           SKIPPED_OFF_DAY            🟢 안 도는 날이다 (주말·공휴일)
  없음      ○           NO_ROW_ON_EXECUTION_DAY    🔴 실행일인데 행이 없다
  있음      ✗           ROW_ON_OFF_DAY             🟡 안 도는 날인데 행이 있다
  ```

  `NO_ROW_ON_EXECUTION_DAY` 를 세려면 **범위를 받아 빈 날을 계산**해야 한다.
  조회 결과만 보면 안 돈 날은 그냥 없는 날이고, 없는 날은 세어지지 않는다.

★ `ROW_ON_OFF_DAY` 는 사고가 아니다. 실측(2026-09-09)으로 `01-24`·`01-31` 이
  그것이고, 그 행들은 *"실행일이 아니다"* 를 사유로 달고 정상적으로 서 있다.
  **정상인데 눈에 띄어야 하는 자리**라 이름을 따로 준다.

🔴 **이 넷을 `end_code` 와 섞지 않는다.**

  ```text
  end_code   "그 판단이 어떻게 끝났나"   E1_APPROVED · E4_NOT_STARTED · SL1 …
  판정        "그 날이 어떻게 됐나"       WALKED · SKIPPED_OFF_DAY …
  ```

  축이 다르다. 한 칸에 접으면 *"E4 인 날"* 과 *"행이 없는 날"* 이 같아지고, 그
  순간 관문이 정상적으로 막은 날과 아무도 안 돌린 날을 다시 못 가른다 — `#465`
  가 방금 가른 것이 바로 그 둘이다.

🔴 **쓰기가 없다.** 조회뿐이고, 이 모듈은 행을 남기지 않는다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from pydantic import BaseModel

from app.master.execution_day import CalendarNotCovered, HolidayCalendar, is_execution_day
from app.master.run_repository import DayRunCount, check_walk_scope, count_runs_by_day

__all__ = [
    "NO_ROW_ON_EXECUTION_DAY",
    "ROW_ON_OFF_DAY",
    "SKIPPED_OFF_DAY",
    "UNKNOWN_CALENDAR",
    "WALKED",
    "WalkDay",
    "WalkReport",
    "walk_report",
]

#: 🟢 실행일에 행이 있다. 돌았다.
WALKED = "WALKED"
#: 🟢 안 도는 날이라 행이 없다. 공백이 아니다.
SKIPPED_OFF_DAY = "SKIPPED_OFF_DAY"
#: 🔴 실행일인데 행이 없다. **지금까지 아무도 이것을 셀 수 없었다.**
NO_ROW_ON_EXECUTION_DAY = "NO_ROW_ON_EXECUTION_DAY"
#: 🟡 안 도는 날인데 행이 있다. 사고가 아니라 눈에 띄어야 하는 자리다.
ROW_ON_OFF_DAY = "ROW_ON_OFF_DAY"
#: ⚠️ 달력이 그 날을 안 덮어 **실행일인지 모른다.** 평일로 단정하지 않는다.
UNKNOWN_CALENDAR = "UNKNOWN_CALENDAR"


class WalkDay(BaseModel):
    """걷기의 하루. **행 수와 뜻을 따로 낸다.**

    ⚠️ **행 수를 뜻으로 읽지 마라.** `01-31` 에 행이 4건이었지만 그 4행은 전부
      *"실행일이 아니다"* 를 사유로 달고 있었다. 행이 있다는 것은 개장일이라는
      뜻이 아니다 — 그래서 `runs` 와 `is_execution_day` 와 `end_codes` 가 서로
      다른 칸에 있다.
    """

    as_of: date
    #: §2 의 판정 하나. `end_code` 가 아니다 — 축이 다르다.
    verdict: str
    #: 이 날 판단을 도는 날인가. **`None` 은 모른다** (`UNKNOWN_CALENDAR`).
    is_execution_day: bool | None
    #: 그날 행 수. 0 이면 표에 그날 행이 없었다는 뜻이다.
    runs: int = 0
    #: 그날 나온 종료코드와 건수.
    end_codes: dict[str, int] = {}
    #: 그날 나온 품목.
    items: list[str] = []
    #: 장부 관문 행(`#465`)이 있었나.
    gate_blocked: bool = False


class WalkReport(BaseModel):
    """한 걷기의 성적표. **물어본 범위 밖은 여기 없다.**

    ★ `summary` 는 **날 수**다 — 행 수가 아니다. 둘을 한 칸에 담으면 하루에 여러
      품목을 돈 날이 여러 날처럼 읽힌다.
    """

    sim_run_id: str
    start: date
    end: date
    #: 🔴 **공휴일을 봤나.** 거짓이면 주말만 갈랐고, 설·추석이
    #: `NO_ROW_ON_EXECUTION_DAY` 로 나온다 — 그건 없는 공백이다.
    holiday_calendar_used: bool
    days: list[WalkDay]
    #: 판정별 **날 수**. 안 나온 판정은 칸이 없다.
    summary: dict[str, int]


def _days(start: date, end: date) -> list[date]:
    """범위 안의 날 전부. **여기가 "없는 날"을 만드는 유일한 자리다.**

    🔴 범위 밖은 만들지 않는다. 표에 그 걷기의 행이 범위 밖에 있어도
      `count_runs_by_day` 가 이미 안 가져온다 — 두 자리가 같은 범위를 본다.
    """
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def _verdict(*, has_rows: bool, runs_today: bool) -> str:
    """§2 의 네 판정. **행 여부와 실행일 여부, 둘만 본다.**

    ⚠️ 종료코드를 안 본다. `end_code` 는 *"그 판단이 어떻게 끝났나"* 이고 여기는
      *"그 날이 어떻게 됐나"* 다.
    """
    if runs_today:
        return WALKED if has_rows else NO_ROW_ON_EXECUTION_DAY
    return ROW_ON_OFF_DAY if has_rows else SKIPPED_OFF_DAY


def walk_report(
    *,
    sim_run_id: str,
    start: date,
    end: date,
    calendar: HolidayCalendar | None = None,
) -> WalkReport:
    """한 걷기가 어땠는지. 범위 안의 **모든 날**에 판정이 붙는다.

    🔴 **`sim_run_id` 가 필수다** (`run_repository.count_runs_by_day` 와 같은
       이유). 안 좁히면 손으로 부른 1,206행이 섞여 들어오고, 그 순간 성적표가
       걷기가 아닌 **표 전체**를 채점한다.

    🔴 **`calendar` 를 안 주면 공휴일을 모른다.** `is_execution_day` 의 태도
       그대로다 — 그때는 주말만 가르고, **모른다는 것을 결과에 적는다**
       (`holiday_calendar_used`). 안 적으면 설날이 `NO_ROW_ON_EXECUTION_DAY` 로
       나와 **없는 공백**을 만든다.

    ⚠️ **달력이 하루를 못 덮어도 성적표 전체를 죽이지 않는다.** 그 날만
      `UNKNOWN_CALENDAR` 로 두고 나머지는 계속 낸다 — `service.py` 가 이미 그
      태도다 (*"달력이 죽었다고 매입 판단을 멈추지 않는다"*). 못 덮은 날을
      평일로 단정하면 **달력이 끊긴 것과 실행일인 것이 같아진다.**

    :raises ValueError: `sim_run_id` 가 비었거나 `end` 가 `start` 보다 앞일 때
        (`check_walk_scope`).
    """
    # 🔴 **집계보다 먼저 본다.** 여기서 안 보면 뒤집힌 범위가 빈 성적표로 나가고,
    #    그건 *"물음이 틀렸다"* 가 아니라 *"걷기가 비었다"* 로 읽힌다 — 이 판이
    #    가르려는 접힘과 같은 종류다.
    check_walk_scope(sim_run_id=sim_run_id, start=start, end=end)

    counted: dict[date, DayRunCount] = {
        row["as_of"]: row for row in count_runs_by_day(sim_run_id=sim_run_id, start=start, end=end)
    }

    days: list[WalkDay] = []
    for day in _days(start, end):
        row = counted.get(day)
        try:
            runs_today: bool | None = is_execution_day(day, calendar=calendar)
        except CalendarNotCovered:
            runs_today = None
            verdict = UNKNOWN_CALENDAR
        else:
            verdict = _verdict(has_rows=row is not None, runs_today=runs_today)

        days.append(
            WalkDay(
                as_of=day,
                verdict=verdict,
                is_execution_day=runs_today,
                runs=0 if row is None else row["runs"],
                end_codes={} if row is None else dict(row["end_codes"]),
                items=[] if row is None else list(row["items"]),
                gate_blocked=False if row is None else row["gate_blocked"],
            )
        )

    return WalkReport(
        sim_run_id=sim_run_id,
        start=start,
        end=end,
        holiday_calendar_used=calendar is not None,
        days=days,
        summary=dict(Counter(day.verdict for day in days)),
    )
