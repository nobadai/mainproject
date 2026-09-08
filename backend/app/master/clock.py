"""
clock.py — **벽시계를 읽는 단 하나의 자리.**

🔴 **이 저장소는 앱 전체에서 벽시계를 금지한다.** 여러 파일이 명시적으로 적어 뒀다.

```text
logistics/simulated_inspection.py:41   "시계를 읽지 않는다"
purchase_agent/mocks/README.md:64      "date.today() 가 없다 (규칙 1)"
master/revalidation.py                 "서버 타임존에 답이 끌려가면 안 된다"
```

★ **스케줄러가 그 규율의 유일한 예외이고, 이 파일이 그 예외를 혼자 진다.**

  스케줄러는 *"오늘이 며칠인가"* 를 물어야만 시작할 수 있다. 아무도 `as_of` 를
  안 넘겨 주기 때문이다 — 09:30 에 깨어나는 것 말고는 입력이 없다.

  ★ **그 아래로는 `as_of` 를 인자로만 흘린다.** 그래야 백테스트와 운영이 **같은
    코드**로 돈다. 179 영업일 걷기가 `as_of` 를 인자로 넘겨 도는 것과 똑같고,
    **다른 것은 `as_of` 하나뿐이다.** 아래 어딘가가 시계를 한 번 더 읽으면 그
    지점부터 백테스트가 오늘 날짜로 답하기 시작하고, 성적이 조용히 무효가 된다.

🔴 **시간대를 반드시 명시한다** — `ZoneInfo("Asia/Seoul")`.

  `date.today()` 는 프로세스가 도는 기계의 로케일을 따른다. 서버가 UTC 면
  **09:30 KST 가 00:30 UTC** 라 날짜가 하루 밀린다.

  ```text
  실제 시각                     date.today()   today_in_seoul()
  2026-09-08 09:30 KST 배치      2026-09-08     2026-09-08     서버가 KST 일 때
  같은 순간 = 2026-09-08 00:30Z  2026-09-08     2026-09-08     🟢 UTC 여도 같다
  2026-09-08 08:00 KST           2026-09-07 🔴  2026-09-08     서버 UTC · 전날 23:00Z
  ```

  ★ 하루가 밀리면 개장 · 실행일 · 도착일이 전부 달력일로 도는 표에서 *"안 열린 날"*
    이 되어 그날 판단이 통째로 막힌다.

⚠️ **갈아 끼울 수 있게 두되 기본값을 `None` 으로 두지 않는다.**

  이 저장소는 `None` 을 *"안 돌렸다"* 로 쓴다 (`verifier.py` 의 `critic=None` 이
  *"Critic 을 안 돌렸다"* 인 것과 같은 이력). 시계에 `None` 을 허용하면
  *"시계를 안 읽었다"* 와 *"기본 시계를 읽었다"* 가 같은 값이 된다.
  그래서 기본값이 **실제 함수 자체**다 — `verifier.py` 가 `critic` 기본값을
  `run_critic_procurement` 자체로 둔 것과 같은 모양이다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

__all__ = ["SEOUL", "seoul_now", "today_in_seoul"]

#: 회사가 하루를 세는 시간대. **고정 오프셋이 아니라 지역명으로 둔다.**
#:
#: ★ `timezone(timedelta(hours=9))` 로도 지금은 같은 값이 나오지만, 그것은
#:   *"UTC+9"* 라는 뜻이지 *"서울"* 이라는 뜻이 아니다. 이름을 두면 규칙이
#:   바뀌는 날 tzdata 가 따라오고, 오프셋을 박으면 우리가 못 따라온다.
SEOUL = ZoneInfo("Asia/Seoul")


def seoul_now() -> datetime:
    """지금 이 순간을 **서울 시각으로** 읽는다. 🔴 저장소에서 벽시계를 읽는 유일한 줄.

    ★ 이 한 줄이 밖으로 새지 않는지는 `tests/master/test_clock_is_the_only_wall_clock.py`
      가 `app/master/` 전체를 AST 로 훑어 지킨다.
    """
    return datetime.now(SEOUL)


def today_in_seoul(now: Callable[[], datetime] = seoul_now) -> date:
    """스케줄러가 깨어난 날. **`as_of` 의 단일 출처다.**

    ★ **함수로 둔다.** 값으로 박으면 프로세스가 자정을 넘겨도 어제로 남고, 부르는
      곳마다 각자 계산하면 한 실행 안에서 날이 갈릴 수 있다.

    ★ **받은 시각을 서울로 옮겨서 날짜를 뗀다.** 대역이 UTC 시각을 넣어도 KST
      날짜가 나와야 한다 — 그것이 UTC 서버에서 도는 실제 모양이기 때문이다.

    :param now: 지금 시각을 주는 함수. 검사가 고정 시각을 넣을 수 있게 열어 둔다.
        🔴 **`None` 을 받지 않는다** — 모듈 docstring 의 이유 그대로다.
    """
    moment = now()
    if moment.tzinfo is None:
        raise ValueError("시간대 없는 시각으로는 날짜를 못 뗀다 — 어느 지역의 그 시각인지가 없다")
    return moment.astimezone(SEOUL).date()
