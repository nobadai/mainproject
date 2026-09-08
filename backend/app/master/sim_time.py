"""
sim_time.py — **시뮬레이션 하루 안에서 단계마다의 시각을 `as_of` 에서 파생한다.**

🔴 **`clock.py` 와 성격이 다르다. 섞지 마라.**

```text
clock.py    **벽시계를 읽는다**      "오늘이 며칠인가"      → 진입점 하나가 한 번
sim_time.py **as_of 에서 파생한다**  "그날의 이 단계는 몇 시로 적나"  → 시계를 안 읽는다
```

★ 그래서 이 파일은 `clock.seoul_now` 도 `clock.today_in_seoul` 도 **부르지 않는다.**
  `clock.SEOUL` 만 가져다 쓴다 — 시간대 상수의 주인은 `clock.py` 하나이고,
  `ZoneInfo("Asia/Seoul")` 를 여기서 새로 만들면 값이 같아서 아무도 못 보다가
  규칙이 바뀌는 날 조용히 갈린다 (`revalidation.py` 가 실제로 그랬다 · 2026-09-08).

## 왜 이 자리가 필요한가

물류의 `allocate_reserved_stock_fefo` 가 `decided_at` 을 **호출자에게서 받는다.**
물류는 시계를 읽지 않겠다고 했고, `inventory_allocations.decided_at` 은
**NOT NULL · 기본값 없음**이라 *"안 적는다"* 는 선택지가 없다.

```text
물류가 시계를 읽는다          ❌ 같은 시뮬레이션을 다시 돌리면 값이 달라진다
호출자가 datetime.now() 를 준다 ❌ 같은 문제를 한 칸 옮긴 것뿐이다
Master 가 as_of 에서 파생한다   🟢 같은 입력 → 같은 값. 시간축의 주인이 하나다
```

## 기준점 — 09:30 KST

기준점은 `clock.SCHEDULE_START` **그 값을 가져다 쓴다.** 여기서 `time(9, 30)`
을 새로 적지 않는다.

★ **왜 09:30 이 유일하게 근거 있는 기준점인가.** 스케줄러가 실제로 그 시각에
  깨어나 하루를 시작한다. 시뮬레이션이 재현하려는 하루가 바로 그 하루이므로,
  기준점을 다른 값으로 두면 **장부의 하루와 실제 배치의 하루가 갈린다.**

⚠️ 상수를 여기 복사해 두면 마감을 옮기는 날 한 군데를 빠뜨린다. 주인은
   `scheduler.py` 이고 여기는 **가리키기만 한다.**

## 배치 — 기준점 + (단계 순번 × 1분)

```text
DAY_OPEN  09:30    RECEIVE   09:31    COLLECT  09:32
JUDGE     09:33    ALLOCATE  09:34    SHIP     09:35
```

🔴 **이 분(分)은 순서 표지일 뿐 업무 시각이 아니다.** 실제 창고가 09:34 에
   할당하고 09:35 에 출고한다는 뜻이 **아니다.** 시뮬레이션 장부에서 **같은 날
   사건들의 순서가 읽히게** 하려는 것이고, 같은 입력을 다시 돌리면 **같은 값**이
   나오게 하려는 것이다.

★ **업무 시각을 지어내면 그 시각이 곧 업무 규칙이 된다 — 아무도 그것을 정한 적이
  없다.** `logistics/inbound_execution.py` 가 `inspected_at = datetime.now()` 와
  기본 provider 를 두고 같은 규율을 적어 뒀다.

## 어휘 — 여섯 단계는 **진입점 이름 그대로다**

```text
DAY_OPEN   개장    POST /master/days/{as_of}/open
RECEIVE    입고    POST /master/days/{as_of}/receive
COLLECT    수금    POST /master/days/{as_of}/collect
JUDGE      판단    POST /master/request
ALLOCATE   할당    logistics.allocate_reserved_stock_fefo
SHIP       출고    logistics.ship_allocated_stock
```

★ **새 어휘를 만들지 않았다.** 그리고 전부 **동사형**이라 상태 어휘
  (`ALLOCATED` · `SHIPPED` · `OPEN`)와 안 겹친다 — 단계와 상태가 같은 이름을 쓰면
  *"SHIP 단계"* 와 *"SHIPPED 상태"* 가 로그에서 구별이 안 된다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from app.master.clock import SCHEDULE_START, SEOUL

__all__ = ["PHASES", "SimPhase", "phase_instant"]

#: 시뮬레이션 하루의 단계. **이 순서가 곧 순번이다.**
SimPhase = Literal["DAY_OPEN", "RECEIVE", "COLLECT", "JUDGE", "ALLOCATE", "SHIP"]

#: 선언 순서. 🔴 **여기 순서를 바꾸면 장부의 사건 순서가 바뀐다.**
PHASES: tuple[SimPhase, ...] = (
    "DAY_OPEN",
    "RECEIVE",
    "COLLECT",
    "JUDGE",
    "ALLOCATE",
    "SHIP",
)

#: 단계 하나가 앞 단계와 벌어지는 간격. **순서를 읽히게 하는 최소값이다.**
#:
#: ⚠️ 이 값을 늘려도 업무적으로 달라지는 것은 없다. 늘리고 싶어진다면 그것은
#:   *"분에 의미를 주고 싶다"* 는 뜻이고, 모듈 docstring 이 그것을 금한다.
_STEP = timedelta(minutes=1)

#: 단계 → 순번. `PHASES` 에서 파생한다 — 순번을 손으로 적으면 둘이 갈린다.
_PHASE_ORDINAL: dict[str, int] = {name: i for i, name in enumerate(PHASES)}


def phase_instant(as_of: date, phase: SimPhase) -> datetime:
    """`as_of` 의 그 단계를 **timezone-aware KST datetime** 으로 준다.

    ★ **시계를 읽지 않는다.** 같은 `(as_of, phase)` 는 언제 불러도 같은 값이다.

    :param as_of: 시뮬레이션이 걷고 있는 영업일.
    :param phase: 여섯 단계 중 하나. 🔴 **모르는 값이면 막는다** — 조용히 기본값을
        내면 서로 다른 단계가 같은 시각으로 적히고, 장부에서 순서가 사라진다.
    :raises ValueError: `phase` 가 `PHASES` 에 없을 때.
    """
    ordinal = _PHASE_ORDINAL.get(phase)
    if ordinal is None:
        raise ValueError(f"모르는 단계다: {phase!r}. 아는 것은 {', '.join(PHASES)} 뿐이다")

    naive = datetime.combine(as_of, SCHEDULE_START) + ordinal * _STEP
    return naive.replace(tzinfo=SEOUL)
