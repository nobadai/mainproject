"""
execution_calendar.py — 실행일 축을 **봉투에 실을 모양**으로 만든다.

매입은 회차일을 `as_of + offset` 으로만 만든다 (`package_scenarios.split_offsets`).
요일도 공휴일도 안 본다 — 실측(2026-09-05)으로 `app/purchase_agent/` 전체에
`weekday()` · `holiday` · 영업일 판정이 **0곳**이다. 그래서 `as_of` 가 화요일이고
3분할 D=12 면 2회차가 **토요일**에 선다.

★ **인용으로는 안 풀린다.** `is_execution_day` 를 매입이 import 해도 `calendar` 를
  못 준다 — 매입은 봉투만 받는 파트라 DB 를 못 부른다. 달력 없이 인용하면 **주말만**
  피하고, 그건 매입이 애초에 걱정한 그 자리다 (*"설·추석에 같은 일이 난다"*).

★ **그래서 값으로 싣는다.** `inbound_lead_days`(N4) · `purchase_payment_days`(N5) 와
  같은 모양이다 — **값은 아는 쪽이 공급하고 계산은 쓰는 쪽이 한다.** 달력도 값이다.

```text
마스터   비영업일 목록 + 그 목록이 덮는 지평       ← 이 모듈
매입     목록에 있으면 다음 날로 민다              ← 매입 몫
```

🔴 **주말도 목록에 넣는다. 그래서 이름이 `holidays` 가 아니다.**

  ```python
  # 매입이 쓰게 되는 전부
  if day.isoformat() in non_execution_days: ...
  ```

  주말을 빼면 매입이 `weekday()` 를 다시 갖고, **판정이 두 곳**이 된다 — 마스터의
  `is_execution_day` 와 매입의 `weekday()`. 같은 사실의 주인이 둘이 되면 언젠가
  갈리고, 갈린 날 아무도 어느 쪽이 맞는지 말해 주지 않는다.

  ⚠️ 주말이 들어 있는 목록을 `holidays` 라 부르면 다음 사람이 *"주말은 따로 봐야겠네"*
    라고 읽는다. 이름이 규율을 지운다.

🔴 **`horizon_end` 가 없으면 목록을 못 쓴다.**

  ```text
  지평 안인데 목록에 없다   🟢 영업일이다
  지평 밖                   🔴 모른다 — 영업일도 비영업일도 아니다
  ```

  `cap_by_date` / `cap_by_date_window_days` 와 **같은 문제**다 (`contracts/core.py:700`
  이 그 규약을 길게 옮겨 적고 있다 — *"창 밖은 0 도 무한대도 아니다"*). 가르지 못하면
  받는 쪽이 지평 밖을 **영업일로** 읽고, 밀어야 할 날을 안 밀고, 아무도 그것을 모른다.

  ★ **그래서 목록과 지평을 한 덩어리로 낸다.** 두 필드로 갈라 실으면 한쪽만 오는 날이
    온다 — `cap_by_date` 짝이 갈려서 물류가 IO Contract §6 을 따로 설명해야 했다.

★ **지평은 `MAX_WALK_DAYS` 다. 새 상수를 만들지 않는다.**

  ```text
  회차일 상한   coverage_days.max = 18     constraints.yaml:178 (매입 설정)
                split_offsets 최대 offset < coverage_days
  연휴 최대     5일                        calendar_walk.py 실측
  밀기 상한     MAX_WALK_DAYS = 31         calendar_walk.py
  ```

  `18 + 5 = 23 < 31` — 최악의 경우도 지평 안에서 끝난다. 그 상수의 뜻이 *"이 이상
  걸으면 달력이 틀린 것"* 이라 지평 상한으로도 정확히 맞고, `day_open` 의 뒤로 걷기와
  `next_execution_day` 의 앞으로 걷기가 이미 그 하나를 쓴다. **여기서 넷째 상수를
  만들면 그중 하나만 바뀌는 날이 온다.**

🔴 **이 모듈은 회차일 축 하나만 답한다.**

  ```text
  회차일   as_of + offset     🟢 여기가 답한다      시장이 서야 산다
  도착일   회차일 + N4        🟡 따라 밀린다        도착일 자체는 안 본다 (물류 축)
  지급일   매입일 + N5        🔴 안 민다            재무가 calendar day 로 확정
  ```

  ★ **창고가 토요일에 물건을 받는지는 물류 소관**이지 실행일 축이 아니다. 여기서 밀면
    마스터가 물류 규약을 대신 정하는 것이 된다.

  ★ **지급일은 재무가 `8/27` 에 calendar day 로 확정했고** 매입 코드 세 곳이 그렇게
    적고 있다 (`package_scenarios.py:909` · `self_check.py:563` · `state.py:54`).
    봉투가 그것을 뒤집으면 안 된다.

⚠️ **이 모듈은 DB 를 부르지 않는다.** `HolidayCalendar` 를 받기만 하고, 그것을 만드는
  것은 `holiday_calendar.py` 의 일이다 — `execution_day.py` 와 같은 규율이다.

⚠️ **`CalendarNotCovered` 를 잡지 않는다.** 잡아서 그 날을 영업일로 넘기면 *"달력이
  끊긴 것"* 과 *"영업일인 것"* 이 같아진다. 부르는 쪽(`service.py`)이 정한다 — 봉투를
  **안 싣고** `skipped_checks` 에 못 봤다고 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.master.calendar_walk import MAX_WALK_DAYS
from app.master.execution_day import HolidayCalendar, is_execution_day

__all__ = [
    "ExecutionCalendarEnvelope",
    "build_execution_calendar",
]


@dataclass(frozen=True)
class ExecutionCalendarEnvelope:
    """비영업일 목록과 **그 목록이 덮는 지평**. 둘은 갈라지지 않는다.

    ★ 짝을 두 필드로 실으면 한쪽만 오는 날이 온다. 덩어리면 그 날이 안 온다.
    """

    non_execution_days: tuple[date, ...]
    """`as_of` 부터 `horizon_end` 까지 중 **판단을 안 도는 날.** 주말 + 공휴일이다.

    ★ 오름차순이고 중복이 없다. 받는 쪽이 정렬을 다시 하지 않아도 된다.
    """

    horizon_end: date
    """위 목록이 덮는 **마지막 날 (포함).** 이 날 뒤는 답하지 않는다.

    🔴 *"비영업일이 없다"* 가 아니라 *"모른다"* 다.
    """

    def covers(self, day: date) -> bool:
        """이 봉투가 `day` 를 답할 수 있는가. 지평 밖이면 거짓이다."""
        return day <= self.horizon_end

    def as_payload(self) -> dict[str, Any]:
        """봉투에 실을 모양. **날짜를 문자열로 편다.**

        ★ payload 는 JSON 으로 오가고, 매입이 `inbound_lead_days` 를 읽는 자리와 같은
          규율이다 — `adapter.py` 가 `cap_by_date` 의 키를 문자열로 강제한다.
        """
        return {
            "non_execution_days": [day.isoformat() for day in self.non_execution_days],
            "horizon_end": self.horizon_end.isoformat(),
        }


def build_execution_calendar(
    as_of: date, *, calendar: HolidayCalendar | None = None
) -> ExecutionCalendarEnvelope:
    """`as_of` 부터 `MAX_WALK_DAYS` 일까지의 비영업일을 모은다. **양 끝 포함.**

    ★ **`as_of` 자신을 뺀 목록이 아니다.** 1회차 offset 이 0 이라 `as_of` 가 회차일이
      되고, 받는 쪽이 그 날만 따로 판정하게 두면 판정이 또 두 곳이 된다.

      ⚠️ 실제로 `as_of` 가 목록에 들어가는 일은 마스터 경로에서는 없다 — 문 앞에서
        비영업일이면 Flow 가 시작되지 않는다. **그래도 넣는다.** 안 들어간다는 사실에
        기대면, 문 앞 판정이 바뀌는 날 이 목록이 조용히 거짓말한다.

    ★ **`calendar` 를 안 주면 주말만 걸린다** — `is_execution_day` 의 기본값 그대로다.
      공휴일 축이 빠진 봉투를 만드는 셈이라 마스터 경로는 항상 달력을 준다.

    :raises CalendarNotCovered: 지평 안의 어떤 평일을 달력이 안 덮을 때. **잡지 않는다** —
        조용히 영업일로 넘기면 *"달력이 끊긴 것"* 과 *"영업일인 것"* 이 같아진다.
    """
    horizon_end = as_of + timedelta(days=MAX_WALK_DAYS)
    closed: list[date] = []
    day = as_of
    while day <= horizon_end:
        if not is_execution_day(day, calendar=calendar):
            closed.append(day)
        day = day + timedelta(days=1)
    return ExecutionCalendarEnvelope(non_execution_days=tuple(closed), horizon_end=horizon_end)
