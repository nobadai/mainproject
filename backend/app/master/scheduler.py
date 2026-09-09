"""
scheduler.py — **깨어났을 때 무엇을 할지 정하고, 그 답을 따른다.**

ML 이 매일 아침 09:23 쯤 예측을 적재한다. 이 모듈은 09:30 부터 5분 간격으로 깨어나
그것을 받고, 10:30 까지 안 오면 **한 번 돌려 `E4_NOT_STARTED` 로 확정 기록**한다.

```text
시작   09:30 KST      SCHEDULE_START
간격   5분             SCHEDULE_INTERVAL
마감   10:30 KST      SCHEDULE_DEADLINE
```

🔴 **여기에 데몬이 없다. 잠자는 루프도 cron 파일도 없다.**

  이 판은 **"깨어났을 때 무엇을 할지"**(`plan_next_action`)와 **"그 답을 따르는
  실행부"**(`run_scheduled_day`)까지다. 실제로 깨우는 것 — OS 스케줄러든 컨테이너
  진입점이든 — 은 **다음 판**이고, 이 파일에 들어오지 않는다.

  ★ **왜 가르나.** 잠자는 루프를 여기 두면 검사가 그것을 못 지난다. 09:30 부터
    10:35 까지 전 구간을 재려면 검사가 **시각을 주입**할 수 있어야 하는데, 루프
    안에 시계가 박혀 있으면 검사는 진짜로 한 시간을 기다리거나 아무것도 못 잰다.
    `plan_next_action` 이 순수 함수라 `now` 를 인자로 받고, 그래서 전 구간이
    한 스위트 안에서 돈다.

★ **상태를 저장하지 않는다.** 재시도 횟수를 어디에도 안 적는다 — `now` 와 마감만
  비교하면 몇 번째 깨어났든 같은 답이 나온다. 적어 두면 그 값이 두 번째 정본이
  되고, 프로세스가 죽는 날 그것부터 틀린다.

---

🔴 **두 축을 둘 다 본다.**

```text
① 달력   market_calendar.is_market_open(as_of)
           False               → NOT_A_MARKET_DAY
           CalendarNotCovered  → BLOCKED (fail-closed · 달력이 이미 그렇게 던진다)

② 게이트  forecast_gate.day_forecast_readiness(...)
           ALL_READY / SOME_READY → RUN_NOW
           NONE_READY             → 마감 전 WAIT · 마감 뒤 RUN_AND_RECORD
           UNREADABLE             → BLOCKED
```

⚠️ **왜 달력도 보나.** 게이트만 보면 *"ML 이 늦은 날"* 과 *"오늘은 배치가 아예 없는
  날"* 이 **둘 다 `NONE_READY`** 라 구별이 안 된다. 앞은 기다려야 하고 뒤는 기다릴
  이유가 없다. 휴장일에 열두 번 깨어나 열두 번 `NONE_READY` 를 보고 마지막에
  `E4` 를 적으면, 나중에 *"몇 날을 못 돌았나"* 를 셀 때 휴장일이 실패로 섞인다.

★ **그래도 1년에 여섯 날은 헛기다린다.** 달력은 열렸는데(`is_open=true`) ML 배치가
  없는 날이다 (노동절 · 어린이날 등 · 매입 실측). 두 축으로도 그 날은 못 가른다 —
  달력이 *"장은 선다"* 고 하고 예측만 없기 때문이다. 받아들이되
  **`RUN_AND_RECORD` 의 사유에 그 사실을 적는다** (`_NO_ML_BATCH`). 그래야 그
  여섯 날이 나중에 눈에 띄고, 사람이 달력 쪽을 볼지 ML 쪽을 볼지 안다.

---

🔴 **다섯 어휘를 섞지 않는다.**

```text
RUN_NOW            예측이 왔다 → 지금 돈다
WAIT               아직 안 왔고 **마감 전** → 5분 뒤 다시 · **판단을 안 돌린다**
RUN_AND_RECORD     마감이 지났는데도 안 왔다 → 한 번 돌려 E4 로 확정 기록
NOT_A_MARKET_DAY   달력이 "오늘 안 선다" → 아무것도 안 한다
BLOCKED            달력이나 게이트를 **못 읽었다** → 재시도로 안 풀린다
```

🔴 **`WAIT` 중에는 판단을 절대 안 돌린다.** 돌리면 09:30 부터 10:30 까지 열두 번
  깨어나며 `E4_NOT_STARTED` 가 **열두 건** 쌓이고, *"몇 날을 못 돌았나"* 가 거짓이
  된다. 확정 기록은 마감 뒤 **한 번**이다.

🔴 **`BLOCKED` 를 `WAIT` 로 접지 않는다.** 접으면 DB 가 죽은 날 스케줄러가 영원히
  재시도하고 **그 사실이 아무 데도 안 남는다.** 없는 것과 못 읽은 것을 가르는
  §1.2-10 이 여기서도 그대로다 (`forecast_gate` · `day_gate` 와 같은 규율).

---

🔴 **서비스 함수를 직접 부른다. HTTP 를 태우지 않는다.**

```text
open_day(as_of)           day_open.py
receive_arrivals(as_of)   inbound.py
issue_receivables(as_of)  receivable.py  ← 🔴 수금보다 앞이다
collect_receipts(as_of)   collection.py
run_procurement(...)      service.py     ← 품목마다
```

  ★ 179 영업일 걷기가 `run_procurement` 을 직접 부른다. 스케줄러도 **같은 자리**를
    불러야 백테스트와 운영이 같은 코드다. 여기서 `TestClient` 나 `httpx` 를 태우면
    운영만 라우터를 한 겹 더 지나고, 그 겹에서 갈리는 날 백테스트가 못 잡는다.

🔴 **벽시계는 `clock` 에서만 읽는다.** 이 모듈은 `clock.seoul_now` ·
  `clock.today_in_seoul` 을 **부르는 쪽**이지 새로 읽는 쪽이 아니다
  (`tests/master/test_clock_is_the_only_wall_clock.py` 가 AST 로 지킨다).

---

🔴 **장부가 안 선 날에는 판단을 안 돌린다. 그 결정이 여기서 났다.**

  `inbound.py` 의 `receive_arrivals` 가 이렇게 적어 두고 답을 미뤄 뒀다.

  > ⚠️ **입고가 `FAILED` · `BLOCKED` 인데 매입 판단을 계속하면 현재고와 capacity 가
  >   실제보다 적게 반영된 상태로 판단한다.** 받았어야 할 물건이 장부에 없는 채로
  >   *"창고가 비었으니 더 사자"* 가 나온다.
  >
  > ★ **부르는 쪽이 정한다.** 이 함수는 상태를 값으로 돌려주고, `run_procurement`
  >   진행 여부는 그것을 본 orchestration 의 결정이다.

  ★★ **그 "부르는 쪽" 이 이 파일이다** (`collection.py` 의 `CollectionOut` 도 같은
    말을 같은 이유로 적어 뒀다 — *"들어왔어야 할 현금이 장부에 없는 채로 매입 판단이
    돈다"*). 그래서 `run_scheduled_day` 가 **개장과 판단 사이에서** 막는다. 두 모듈은
    그대로 두고 답만 여기서 낸다 — 상태의 주인은 여전히 입고·수금이다.

```text
입고(InboundOut.status) 또는 수금(CollectionOut.status) 이
  BLOCKED · FAILED   →  🔴 그날 판단을 안 돌린다 · **출고도 안 돌린다**
```

🔴 **출고는 장부 관문 뒤, 판단 뒤다.**

```text
개장 → 입고 → 수금 → [장부 관문] → 판단 → **출고**
```

  ★ **왜 판단 뒤인가.** 오늘 산 것이 오늘 나가지 않는다 — 도착이 며칠 뒤다. 그래서
    출고가 보는 재고는 판단이 만든 매입과 무관하고, 순서를 바꿔도 결과가 같아야
    하는데 **같지 않게 보이는 날**이 생긴다. 판단 뒤에 두면 그 물음이 없다.

  ★★ **왜 관문 뒤인가.** 장부가 안 선 날에 출고까지 하면 **재고가 두 번 틀린다** —
    입고가 안 들어온 채로 물건이 나가고, 그 위에서 다음 날 판단이 선다. 판단을
    막는 이유가 그대로 출고를 막는 이유다.

🔴 **무엇을 안 막는지가 더 중요하다.**

```text
RECEIVED / COLLECTED   정상
NOTHING_DUE            **확인했고 낼 것이 없다** — 정상이다. 절대 막지 않는다
NOT_OPENED             하루가 안 열렸다 — 그러면 개장에서 이미 멈춰 여기까지 안 온다
NOT_ATTEMPTED          단계를 안 탔다 — 앞 단계에서 이미 멈춘 것이다
```

  ★★ `NOTHING_DUE` 를 막으면 **대부분의 날이 멈춘다.** 그 어휘를 만든 이유가 정확히
    *"없는 것과 못 한 것은 다르다"* 이고, 여기서 접으면 그 구분이 통째로 무의미해진다.

⚠️ **개장은 그대로다.** 개장이 실패하면 그 뒤를 안 하고, 판단이 실패해도 개장을
  되돌리지 않는다. 이 관문은 **개장과 판단 사이**에만 선다.

🔴 **관문이 막은 날에는 행 하나를 남긴다** (2026-09-09). 나머지는 여전히 값뿐이다.

```text
관문이 막았다        🟢 master_agent_runs 에 행이 있다 (persistence.record_ledger_gap)
그날을 안 돌렸다      🔴 여전히 행이 없다
돌렸는데 적재가 실패   🔴 여전히 행이 없다
```

  ★ **왜 관문만 먼저인가.** 매입 화면이 셋을 *"사유를 남긴 실행이 없습니다"* 한
    문구로 그린다. 관문이 막은 것은 **정상 동작**이고 나머지 둘은 아니라, 정상을
    먼저 갈라내야 남은 둘이 눈에 띈다.

  ⚠️ **세는 것은 아직 안 된다.** `end_code='E4_NOT_STARTED'` 한 값에 예측 미도착 ·
    어댑터 미등록 · 장부 관문이 같이 앉는다. 그것은 `Master 상세 19.0` 이 풀 자리다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from app.master import clock, persistence
from app.master.clock import SCHEDULE_DEADLINE, SCHEDULE_INTERVAL, SCHEDULE_START
from app.master.collection import collect_receipts
from app.master.commitment import ITEM_CODES
from app.master.day_open import open_day
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, day_forecast_readiness
from app.master.inbound import receive_arrivals
from app.master.market_calendar import MarketCalendar, get_market_calendar
from app.master.outbound_flow import ship_due_sales
from app.master.receivable import issue_receivables
from app.master.schemas import ProcurementRunRequest
from app.master.service import run_procurement

__all__ = [
    "DAILY_POLICY_VERSION",
    "SCHEDULE_DEADLINE",
    "SCHEDULE_INTERVAL",
    "SCHEDULE_START",
    "DayRunOutcome",
    "ItemRunOutcome",
    "ScheduledAction",
    "SchedulerAction",
    "daily_request_id",
    "deadline_at",
    "ledger_gap_request_id",
    "plan_next_action",
    "run_scheduled_day",
    "scheduled_items",
    "wake_up",
]

logger = logging.getLogger(__name__)

# ── 시각 상수 ───────────────────────────────────────────────────────────
#
# 🔴 **선언은 `clock.py` 에 있다** (2026-09-08 에 옮겼다). 여기서 다시 세지 않고
#    이름만 다시 내보낸다 — `sim_time` 이 기준점을 가져갈 때 `scheduler` 를 통과하면
#    `sim_time → scheduler → service` 고리가 생기기 때문이다.
#
# ★ 값의 **뜻**(언제 깨우고 언제 마감하나)은 그대로 이 파일의 것이다. `clock.py` 는
#   표준 라이브러리만 들이는 leaf 라 그 숫자를 두는 자리일 뿐이다.

#: 하루 실행이 싣는 정책 판. **부르는 쪽이 바꿀 수 있게 인자로도 열어 둔다.**
DAILY_POLICY_VERSION = "v1.3-PROVISIONAL"

#: `RUN_AND_RECORD` 사유에 반드시 들어가는 문장. **1년에 여섯 날을 눈에 띄게 한다.**
#:
#: ★ 문자열을 상수로 둔 이유는 검사가 이 문장을 찾기 때문이다. 사유를 손으로 다시
#:   쓰면 철자가 갈리고, 그러면 그 여섯 날을 나중에 못 센다.
_NO_ML_BATCH = "달력은 열렸는데 ML 배치가 없었다"

#: 🔴 **이 상태면 그날 판단을 안 돌린다.** 입고·수금 둘 다 같은 표를 쓴다.
#:
#: ```text
#: BLOCKED   받을(들어올) 대상이 있는데 못 했다 — 장부가 실제보다 적다
#: FAILED    하려다 터졌다 — 아무것도 안 바뀌었다
#: ```
#:
#: 🔴 **`NOTHING_DUE` 는 여기 없다. 넣으면 대부분의 날이 멈춘다.**
#:   *"확인했고 낼 것이 없다"* 는 정상이고, 그 어휘를 만든 이유가 정확히
#:   *"없는 것과 못 한 것은 다르다"* 이다.
#:
#: ⚠️ **`NOT_OPENED` · `NOT_ATTEMPTED` 도 여기 없다.** 둘 다 *"앞에서 이미 멈췄다"*
#:   이고, 개장 관문이 먼저 돌아서서 여기까지 오지도 않는다.
_LEDGER_GAP_STATUSES: frozenset[str] = frozenset({"BLOCKED", "FAILED"})

#: 판단을 안 돌린 이유의 앞머리. **검사가 이 문장을 찾는다** (`_NO_ML_BATCH` 와 같은
#: 이유다 — 손으로 다시 쓰면 철자가 갈리고 그러면 막힌 날을 나중에 못 센다).
_LEDGER_GAP = "장부가 안 서서 판단을 안 돌린다"

#: 장부 관문 행의 업무 키 꼬리. **품목 자리에 들어간다.**
#:
#: 🔴 **품목 이름과 겹치면 안 된다.** 겹치는 순간 그날 그 품목의 판단 행과 게이트
#:   행이 같은 업무 키를 갖고, `get_run_by_request_id` 가 둘을 못 가른다.
#:   계약 품목은 한글 이름이라 이 꼬리와 같아질 수 없고, 그것을 검사가 잠근다.
_LEDGER_GAP_REQUEST_SUFFIX = "LEDGER-GAP"

#: 스케줄러가 답할 수 있는 **전부**. 여섯 번째를 만들지 않는다.
SchedulerAction = Literal[
    "RUN_NOW",
    "WAIT",
    "RUN_AND_RECORD",
    "NOT_A_MARKET_DAY",
    "BLOCKED",
]


def deadline_at(as_of: date) -> datetime:
    """그날의 마감 시각. **서울 시각으로 붙인다.**

    🔴 **시간대를 여기서 새로 만들지 않는다.** `clock.SEOUL` 을 가져다 쓴다 —
      `timezone(timedelta(hours=9))` 를 따로 들면 값이 같아서 아무도 못 보다가
      규칙이 바뀌는 날 조용히 갈린다 (`revalidation.py` 가 그랬다 · 2026-09-08).
    """
    return datetime.combine(as_of, SCHEDULE_DEADLINE, tzinfo=clock.SEOUL)


def scheduled_items() -> tuple[str, ...]:
    """오늘 돌 품목. **`ITEM_CODES` 를 정렬해서 낸다. 목록을 다시 세지 않는다.**

    ★ `ITEM_CODES` 는 `frozenset` 이라 순서가 없다. 순서를 정해 두지 않으면 같은 날
      두 번 돌 때 품목 순서가 갈리고, 실패한 품목을 비교하기가 어려워진다.
    """
    return tuple(sorted(ITEM_CODES))


def daily_request_id(as_of: date, item: str) -> str:
    """`REQ-DAILY-20260908-배추`. **날짜와 품목만으로 정해진다.**

    🔴 **시각을 넣지 않는다.** 넣으면 같은 날 두 번 깨어날 때 id 가 갈리고,
      `master_agent_runs_run_request_unique` 가 두 번째를 못 막는다 — 멱등이
      인덱스가 아니라 *"두 번 안 깨우기"* 에 걸리게 된다.
    """
    return f"REQ-DAILY-{as_of:%Y%m%d}-{item}"


def ledger_gap_request_id(as_of: date) -> str:
    """`REQ-DAILY-20260908-LEDGER-GAP`. **하루 단위 키다 — 품목이 없다.**

    🔴 **`daily_request_id` 를 못 쓴다.** 저쪽은 품목별인데 장부 관문은 하루를
      통째로 돌려세운다. 품목을 하나 골라 넣으면 *"배추 때문에 막혔다"* 라는 없는
      사실이 생기고, 전부에 넣으면 같은 사실이 품목 수만큼 쌓인다.

    🔴 **시각을 안 넣는다** (`daily_request_id` 와 같은 이유). 넣으면 같은 날 두 번
      깨어날 때 키가 갈리고, 그러면 *"그날 게이트 행이 이미 있나"* 를 물을 수가 없다.
    """
    return f"REQ-DAILY-{as_of:%Y%m%d}-{_LEDGER_GAP_REQUEST_SUFFIX}"


# ── ① 결정 — 순수 함수 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ScheduledAction:
    """이번에 깨어나서 **무엇을 할지**. 부작용 없이 만들어진다.

    ★ **품목 내역을 같이 담는다.** `SOME_READY` 를 `RUN_NOW` 로 접어도 어느 품목이
      빠졌는지는 남아야 한다 — 빠진 품목은 그 자리에서 `MISSING → E4` 로 정직하게
      남고, 그 사실을 나중에 세려면 여기 있어야 한다.
    """

    as_of: date
    now: datetime
    action: SchedulerAction
    reason: str
    #: 마감 시각. **답과 함께 낸다** — 왜 `WAIT` 인지가 이 값과의 비교이기 때문이다.
    deadline: datetime
    #: 예측이 온 품목.
    ready_items: tuple[str, ...] = ()
    #: 확인했고 아직 안 온 품목. 🔴 `unreadable_items` 와 섞지 않는다.
    not_yet_items: tuple[str, ...] = ()
    #: 못 물어본 품목.
    unreadable_items: tuple[str, ...] = ()
    #: 다음 깨어남까지. **`WAIT` 일 때만 값이 있다.**
    retry_after: timedelta | None = None

    @property
    def should_run(self) -> bool:
        """이번에 판단을 돌리는가.

        🔴 **`WAIT` · `NOT_A_MARKET_DAY` · `BLOCKED` 는 안 돈다.** 특히 `WAIT` 은
          *"아직"* 이지 *"못"* 이 아니라, 여기서 돌면 `E4` 가 열두 건 쌓인다.
        """
        return self.action in ("RUN_NOW", "RUN_AND_RECORD")


def plan_next_action(
    *,
    now: datetime,
    as_of: date,
    calendar: MarketCalendar,
    gate_result: DayForecastReadiness,
) -> ScheduledAction:
    """이번에 깨어나서 무엇을 할지. **순수 함수다 — 아무것도 안 돌린다.**

    ★ **`now` 를 인자로 받는다.** 그래서 검사가 09:29 · 09:30 · 10:29 · 10:30 ·
      10:35 를 한 스위트 안에서 전부 지날 수 있다. 여기서 시계를 읽으면 검사는
      *"지금 몇 시인가"* 에 답이 끌려가고, CI 가 도는 시각마다 결과가 달라진다.

    ★ **두 축을 다 받는다.** 달력은 물어봐야 알아서 객체로 받고(`CalendarNotCovered`
      가 여기서 튄다), 게이트는 이미 답이 나와 있어 값으로 받는다.

    :param calendar: 개장 축. `is_market_open` 하나만 부른다.
    :param gate_result: `day_forecast_readiness` 의 답. **미리 계산해서 넘긴다** —
        이 함수가 DB 를 타면 순수 함수가 아니게 되고, 검사가 대역을 끼울 자리가
        인자가 아니라 monkeypatch 가 된다.
    """
    if now.tzinfo is None:
        raise ValueError(
            "시간대 없는 시각으로는 마감을 못 잰다 — 어느 지역의 10:30 인지가 없다."
            " clock.seoul_now() 가 주는 값을 그대로 넘겨야 한다"
        )
    deadline = deadline_at(as_of)

    # ── ① 달력 ──────────────────────────────────────────────────────
    #
    # 🔴 **달력을 먼저 본다.** 휴장일이면 예측이 없는 것이 정상이고, 그 날 `NONE_READY`
    #    를 보고 기다리면 한 시간을 헛 깨어난 뒤 `E4` 까지 적는다.
    try:
        is_open = calendar.is_market_open(as_of)
    except CalendarNotCovered as exc:
        # 🔴 **fail-closed.** 달력을 못 읽은 것을 *"장이 선다"* 로도 *"안 선다"* 로도
        #    만들지 않는다. 재시도로 안 풀리므로 `WAIT` 이 아니다.
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="BLOCKED",
            reason=f"개장 달력을 못 읽었다: {exc}",
            deadline=deadline,
        )
    if not is_open:
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="NOT_A_MARKET_DAY",
            reason=f"{as_of.isoformat()} 은 장이 서지 않는다 — 기다릴 예측이 없다",
            deadline=deadline,
        )

    ready = gate_result.ready_items
    not_yet = gate_result.not_yet_items
    unreadable = gate_result.unreadable_items

    # ── ② 게이트 ────────────────────────────────────────────────────
    if gate_result.readiness == "UNREADABLE":
        # 🔴 **`WAIT` 으로 접지 않는다.** DB 가 죽은 날 영원히 재시도하고 그 사실이
        #    아무 데도 안 남는 것이 정확히 이 한 줄에 달려 있다.
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="BLOCKED",
            reason=f"예측 게이트를 못 읽었다: {', '.join(unreadable)}",
            deadline=deadline,
            ready_items=ready,
            not_yet_items=not_yet,
            unreadable_items=unreadable,
        )

    if gate_result.readiness in ("ALL_READY", "SOME_READY"):
        # ★ **`SOME_READY` 도 돈다.** 빠진 품목은 `load_forecast` 가 `MISSING` 을 내고
        #   매입이 `RUNTIME_NOT_READY` → `E4` 로 정직하게 남긴다. 그 품목이 무엇인지는
        #   `not_yet_items` 가 나른다.
        missing_note = f" (빠진 품목: {', '.join(not_yet)})" if not_yet else ""
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="RUN_NOW",
            reason=f"예측이 왔다: {', '.join(ready)}{missing_note}",
            deadline=deadline,
            ready_items=ready,
            not_yet_items=not_yet,
            unreadable_items=unreadable,
        )

    # ── ③ NONE_READY — 마감 하나만 본다 ────────────────────────────
    #
    # ★ **상태를 안 본다.** 몇 번째 깨어남인지, 앞서 몇 번 기다렸는지를 안 세고
    #   `now` 와 마감만 비교한다. 그래서 프로세스가 죽었다 살아나도 답이 같다.
    if now >= deadline:
        return ScheduledAction(
            as_of=as_of,
            now=now,
            action="RUN_AND_RECORD",
            reason=(
                f"{_NO_ML_BATCH} — 마감({deadline:%H:%M})까지 예측이 안 와서"
                " 한 번 돌려 E4_NOT_STARTED 로 확정 기록한다"
            ),
            deadline=deadline,
            ready_items=ready,
            not_yet_items=not_yet,
            unreadable_items=unreadable,
        )
    return ScheduledAction(
        as_of=as_of,
        now=now,
        action="WAIT",
        reason=f"아직 예측이 안 왔다 — 마감({deadline:%H:%M}) 전이라 기다린다",
        deadline=deadline,
        ready_items=ready,
        not_yet_items=not_yet,
        unreadable_items=unreadable,
        retry_after=SCHEDULE_INTERVAL,
    )


# ── ② 실행 — 답을 따르기만 한다 ────────────────────────────────────────


@dataclass(frozen=True)
class ItemRunOutcome:
    """품목 하나의 실행 결과. **터진 것도 값으로 남는다.**"""

    item: str
    request_id: str
    #: `RAN` 은 판단이 끝까지 돌았다는 뜻이다. **좋은 답이었다는 뜻이 아니다** —
    #: `E4_NOT_STARTED` 도 `RAN` 이다. 못 돈 것은 `FAILED` 다.
    status: Literal["RAN", "FAILED"]
    #: 그 실행의 종료 코드. **못 돌았으면 `None`** — 코드가 없었다는 뜻이다.
    end_code: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class DayRunOutcome:
    """하루 실행 한 번의 결과. **어느 단계에서 무엇이 됐는지가 다 남는다.**

    ⚠️ 단계 상태의 `NOT_ATTEMPTED` 는 *"안 했다"* 다. `FAILED` 와 섞지 않는다 —
      개장이 막혀 안 한 것과 해 보고 터진 것은 다른 사실이다 (`DayOpenOut` 의
      `collection_seed_status` 와 같은 어휘).
    """

    as_of: date
    action: SchedulerAction
    reason: str
    day_open_status: str = "NOT_ATTEMPTED"
    inbound_status: str = "NOT_ATTEMPTED"
    #: 채권 발행 단계. 🔴 **수금보다 앞이다** — 채권이 서야 수금할 것이 있다.
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `ReceivableOut.status` 의 다섯 값을 그대로
    #:   싣고, 단계를 안 탄 날은 이 클래스가 이미 쓰는 `NOT_ATTEMPTED` 다.
    receivable_status: str = "NOT_ATTEMPTED"
    collection_status: str = "NOT_ATTEMPTED"
    #: 판단 단계를 **탔는가**. 🔴 좋은 답이 나왔다는 뜻이 아니다 — 품목별 결과는
    #: `items` 가 나른다 (`ItemRunOutcome.status` 와 같은 어휘를 쓴다).
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `NOT_ATTEMPTED` 는 이 클래스가 이미 세 단계에
    #:   쓰는 말이고 `RAN` 은 `ItemRunOutcome` 이 이미 쓰는 말이다. 단계를 안 탄
    #:   사실을 `items == ()` 으로만 두면 *"품목 목록이 비었다"* 와 구별이 안 된다.
    procurement_status: str = "NOT_ATTEMPTED"
    #: 출고 단계를 **탔는가**. 🔴 `procurement_status` 와 **같은 모양·같은 어휘**다
    #: (`RAN` · `NOTHING_DUE` · `FAILED` · `NOT_ATTEMPTED`).
    #:
    #: ★ **어휘를 새로 만들지 않았다.** `NOTHING_DUE` 는 입고·수금이 이미 쓰는 말이고
    #:   나머지 셋은 이 클래스가 이미 쓴다. 판매 품목별 결과는 `OutboundOut.items` 가
    #:   나르고, 여기 다시 담지 않는다 — 같은 사실의 주인은 하나다.
    outbound_status: str = "NOT_ATTEMPTED"
    items: tuple[ItemRunOutcome, ...] = ()
    #: 단계별 사유. 사람이 읽을 자리다.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed_items(self) -> tuple[str, ...]:
        """터진 품목. **나머지는 계속 돌았다.**"""
        return tuple(one.item for one in self.items if one.status == "FAILED")


def run_scheduled_day(
    action: ScheduledAction,
    *,
    policy_version: str = DAILY_POLICY_VERSION,
    open_day_fn: Callable[..., Any] = open_day,
    receive_fn: Callable[..., Any] = receive_arrivals,
    issue_fn: Callable[..., Any] = issue_receivables,
    collect_fn: Callable[..., Any] = collect_receipts,
    procure_fn: Callable[..., Any] = run_procurement,
    outbound_fn: Callable[..., Any] = ship_due_sales,
    items: Sequence[str] | None = None,
) -> DayRunOutcome:
    """결정을 따른다. **여기에는 판단이 없다.**

    ```text
    개장 → 입고 → 채권 → 수금 → [장부 관문] → 판단(품목마다) → 출고
    ```

    🔴 **채권이 수금보다 앞이다.** 채권이 서야 수금할 것이 있다. 지금 데이터는
      결제조건이 30일이라 같은 날 수금될 일이 없지만, **순서가 계약**이다.

    🔴 **출고가 판단 뒤이고 관문 뒤다.** 오늘 산 것은 오늘 안 나가고(도착이 며칠
      뒤다), 장부가 안 선 날에 물건을 내보내면 재고가 두 번 틀린다. 관문에서
      돌아서면 `outbound_status` 는 `NOT_ATTEMPTED` 로 남는다.

    🔴 **`should_run` 이 아니면 아무것도 안 부른다.** `WAIT` 중에 판단을 돌리면
      `E4_NOT_STARTED` 가 열두 건 쌓인다 — 이 한 줄이 그것을 막는다.

    🔴 **개장이 실패하면 그 뒤를 안 한다.** 상태 행이 없으면 입고 · 수금 · 판단이
      적을 자리가 없고, 그런데도 부르면 `NOTHING_DUE` 가 나가 *"오늘은 올 게
      없었다"* 로 읽힌다.

    🔴 **판단이 실패해도 개장을 되돌리지 않는다.** 하루가 열린 것은 사실이고, 판단이
      실패한 것은 별개 사실이다 (`#400` 에서 같은 판단을 했다). 되돌리면 다음 날이
      *"어제가 안 열렸다"* 위에 서고, 그게 판단 실패보다 크다.

    🔴 **한 품목이 터져도 나머지는 계속 돈다.** 배추가 터졌다고 무와 양파를 안 돌면
      하루가 통째로 빈다. 터진 것은 `items` 에 `FAILED` 로 남는다.

    🔴 **입고 · 채권 · 수금이 `BLOCKED` · `FAILED` 면 판단을 안 돌린다.** `inbound.py` 가
      *"부르는 쪽이 정한다"* 로 넘겨 둔 답을 이 파일이 낸다 (모듈 docstring 에 원문을
      인용해 뒀다). 장부가 실제보다 적은 채로 판단하면 **과매입이 나는데 에러는 안
      난다** — 재고가 적게 보이면 더 사고, 현금이 적게 보이면 `projected_cash_min`
      이 틀린다.

      ★ **조용히 건너뛰지 않는다.** 무엇이 막았는지가 `notes` 에 남고, 판단 단계를
        안 탔다는 사실은 `procurement_status` 가 든다.

      🔴 **`NOTHING_DUE` 는 막지 않는다.** *"확인했고 낼 것이 없다"* 는 정상이고,
        막으면 대부분의 날이 멈춘다.

    :param items: 돌 품목. 안 주면 `scheduled_items()` — **목록을 다시 세지 않는다.**
    """
    if not action.should_run:
        # 🔴 여기서 돌아선다. **서비스 함수를 하나도 안 부른다.**
        return DayRunOutcome(as_of=action.as_of, action=action.action, reason=action.reason)

    as_of = action.as_of
    notes: list[str] = []

    # ── 개장 ────────────────────────────────────────────────────────
    try:
        opened = open_day_fn(as_of)
    except Exception as exc:  # noqa: BLE001 - 개장 실패가 500 으로 올라가면 안 된다.
        return DayRunOutcome(
            as_of=as_of,
            action=action.action,
            reason=action.reason,
            day_open_status="FAILED",
            notes=(f"개장이 터졌다: {type(exc).__name__}: {exc}",),
        )
    day_open_status = str(getattr(opened, "status", "FAILED"))
    if day_open_status not in ("OPENED", "ALREADY_OPENED"):
        # ★ **`ALREADY_OPENED` 는 통과다** — *"할 일이 없었다"* 이지 *"못 했다"* 가
        #   아니다. 같은 날 두 번 깨어나면 두 번째가 여기로 온다.
        return DayRunOutcome(
            as_of=as_of,
            action=action.action,
            reason=action.reason,
            day_open_status=day_open_status,
            notes=(f"하루가 안 열려서 뒤를 안 한다: {getattr(opened, 'reason', '')}",),
        )

    # ── 입고 ────────────────────────────────────────────────────────
    inbound_status, note = _stage("입고", lambda: receive_fn(as_of))
    notes.append(note)

    # ── 채권 — 🔴 **수금보다 앞이다** ───────────────────────────────
    #
    # ★ 채권이 서야 수금할 것이 있다. 순서를 뒤집으면 같은 날 발생·수금되는 계약이
    #   생기는 순간 **수금할 채권이 아직 없는 상태**에서 수금이 돈다.
    receivable_status, note = _stage("채권", lambda: issue_fn(as_of))
    notes.append(note)

    # ── 수금 ────────────────────────────────────────────────────────
    collection_status, note = _stage("수금", lambda: collect_fn(as_of))
    notes.append(note)

    # ── 장부 관문 — 개장과 판단 **사이** ────────────────────────────
    #
    # 🔴 여기서 돌아서면 `procure_fn` 을 **한 번도 안 부른다.** 개장은 그대로 둔다 —
    #    하루가 열린 것은 사실이고, 판단을 안 돌린 것은 별개 사실이다.
    if _ledger_gap(inbound_status, receivable_status, collection_status):
        gap_reason = _ledger_gap_note(inbound_status, receivable_status, collection_status)
        notes.append(gap_reason)
        # ── 🔴 **행 하나를 남긴다. 판단은 여전히 안 돌린다** (2026-09-09) ──
        #
        # ★ **왜 남기나.** 안 남기면 이 날이 화면에서 *"사유를 남긴 실행이
        #   없습니다"* 로 보이고, 그 문구는 **안 돌린 날**과 **적재가 실패한 날**도
        #   똑같이 낸다. 관문이 막은 것은 정상 동작이고 나머지 둘은 아니다.
        #
        # ★ **문장을 다시 짓지 않는다.** 위에서 만든 `gap_reason` 을 그대로 넘긴다 —
        #   두 벌이 되면 한쪽만 고치는 날 화면과 이력이 갈린다.
        #
        # 🔴 **여기서도 `procure_fn` 은 안 부른다.** 남기는 것은 행이지 판단이 아니다.
        try:
            persistence.record_ledger_gap(
                request_id=ledger_gap_request_id(as_of),
                as_of=as_of,
                policy_version=policy_version,
                reason=gap_reason,
                inbound_status=inbound_status,
                receivable_status=receivable_status,
                collection_status=collection_status,
            )
        except Exception:  # 이력 때문에 걷기가 멈추면 안 된다.
            # ★ `try_save_run` 이 이미 삼키지만 여기서 한 번 더 잡는다 — 그날 결과가
            #   적재의 약속에 걸리면 안 된다 (`_stage` 와 같은 태도).
            logger.exception("장부 관문 행 적재가 터졌다 - 그날 결과는 그대로 나간다")
        return DayRunOutcome(
            as_of=as_of,
            action=action.action,
            reason=action.reason,
            day_open_status=day_open_status,
            inbound_status=inbound_status,
            receivable_status=receivable_status,
            collection_status=collection_status,
            notes=tuple(notes),
        )

    # ── 판단 ────────────────────────────────────────────────────────
    results: list[ItemRunOutcome] = []
    for item in scheduled_items() if items is None else tuple(items):
        request_id = daily_request_id(as_of, item)
        try:
            response = procure_fn(
                ProcurementRunRequest(
                    as_of=as_of,
                    policy_version=policy_version,
                    request_id=request_id,
                    item=item,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 한 품목이 하루를 세우면 안 된다.
            results.append(
                ItemRunOutcome(
                    item=item,
                    request_id=request_id,
                    status="FAILED",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        results.append(
            ItemRunOutcome(
                item=item,
                request_id=request_id,
                status="RAN",
                end_code=str(getattr(response, "end_code", "")) or None,
            )
        )

    # ── 출고 — 🔴 **장부 관문 뒤 · 판단 뒤** ────────────────────────
    #
    # ★ 여기 오기 전에 관문이 이미 돌아섰을 수 있고, 그러면 이 줄에 아예 안 온다 —
    #   그것이 *"장부가 안 선 날에는 출고도 안 한다"* 이다.
    outbound_status, note = _stage("출고", lambda: outbound_fn(as_of))
    notes.append(note)

    return DayRunOutcome(
        as_of=as_of,
        action=action.action,
        reason=action.reason,
        day_open_status=day_open_status,
        inbound_status=inbound_status,
        receivable_status=receivable_status,
        collection_status=collection_status,
        procurement_status="RAN",
        outbound_status=outbound_status,
        items=tuple(results),
        notes=tuple(notes),
    )


def _ledger_gap(inbound_status: str, receivable_status: str, collection_status: str) -> bool:
    """장부가 안 섰는가. **입고 · 채권 · 수금을 다 본다.**

    🔴 **한쪽만 보면 다른 쪽 구멍이 그대로 열려 있다.** 입고가 막히면 재고와 capacity
      가 적게 반영되고, 수금이 막히면 현금이 적게 반영되고, **채권이 안 서면
      `receivables_krw` 가 적게 잡힌다** — 셋 다 매입 판단이 보는 값이고, 어느 쪽이
      틀려도 에러 없이 틀린 답이 나온다.
    """
    return any(
        status in _LEDGER_GAP_STATUSES
        for status in (inbound_status, receivable_status, collection_status)
    )


def _ledger_gap_note(inbound_status: str, receivable_status: str, collection_status: str) -> str:
    """막은 이유. 🔴 **입고·채권·수금 상태를 다 적는다 — 어느 쪽이 막았는지 보이게.**

    ⚠️ 하나만 적으면 화면이 *"장부가 안 섰다"* 까지만 말하고, 사람이 물류를 볼지
      판매를 볼지 재무를 볼지 모른 채 세 곳을 다 뒤진다.
    """
    return (
        f"{_LEDGER_GAP} (입고: {inbound_status} · 채권: {receivable_status}"
        f" · 수금: {collection_status})"
    )


def _stage(name: str, call: Callable[[], Any]) -> tuple[str, str]:
    """입고 · 채권 · 수금 한 단계. **예외를 값으로 옮긴다.**

    ★ 두 함수 다 예외를 안 내보낸다고 적어 뒀지만 여기서 한 번 더 잡는다 —
      판단의 진행 여부가 그 약속에 걸리면 안 된다 (`_seed_collection` 과 같은 태도).
    """
    try:
        out = call()
    except Exception as exc:  # noqa: BLE001
        return "FAILED", f"{name}이 터졌다: {type(exc).__name__}: {exc}"
    status = str(getattr(out, "status", "FAILED"))
    return status, f"{name}: {status} {getattr(out, 'reason', '')}".strip()


# ── ③ 진입점 — 시계를 읽는 유일한 자리 ─────────────────────────────────


def wake_up(
    *,
    now: Callable[[], datetime] = clock.seoul_now,
    calendar: Callable[[], MarketCalendar] = get_market_calendar,
    readiness: Callable[[date], DayForecastReadiness] = lambda as_of: day_forecast_readiness(
        as_of=as_of
    ),
    policy_version: str = DAILY_POLICY_VERSION,
    open_day_fn: Callable[..., Any] = open_day,
    receive_fn: Callable[..., Any] = receive_arrivals,
    issue_fn: Callable[..., Any] = issue_receivables,
    collect_fn: Callable[..., Any] = collect_receipts,
    procure_fn: Callable[..., Any] = run_procurement,
    outbound_fn: Callable[..., Any] = ship_due_sales,
) -> DayRunOutcome:
    """한 번 깨어났다. **결정하고, 그 답을 따른다.**

    🔴 **이 함수는 안 잔다.** 5분 뒤 다시 부르는 것은 밖의 일이고 (`retry_after` 가
      그 간격을 말해 준다), 여기에 `sleep` 이 들어오면 검사가 못 지난다.

    ★ **시계를 여기서 한 번만 읽는다.** 읽은 시각으로 `as_of` 와 마감 비교를 둘 다
      한다 — 두 번 읽으면 자정을 넘기는 순간 날짜와 시각이 서로 다른 날을 가리킨다.

    ⚠️ **기본값이 실제 함수 자체다. `None` 이 아니다** (`clock.py` · `verifier.py` 와
      같은 규율). `None` 을 허용하면 *"안 줬다"* 와 *"기본을 줬다"* 가 같은 값이 된다.
    """
    moment = now()
    as_of = clock.today_in_seoul(lambda: moment)
    action = plan_next_action(
        now=moment,
        as_of=as_of,
        calendar=calendar(),
        gate_result=readiness(as_of),
    )
    return run_scheduled_day(
        action,
        policy_version=policy_version,
        open_day_fn=open_day_fn,
        receive_fn=receive_fn,
        issue_fn=issue_fn,
        collect_fn=collect_fn,
        procure_fn=procure_fn,
        outbound_fn=outbound_fn,
    )
