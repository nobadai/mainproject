"""
backtest_runner.py — **범위를 하루씩 걸으며 하루 실행을 부른다.**

```text
walk(sim_run_id=..., start=..., end=..., now=...)   start..end 를 하루씩 걷는다
                                                    개장일마다 run_scheduled_day 를 부른다
```

🔴 **실행 축이 인자다. 기본값이 없다** (2026-09-10). 179일을 **어느 실행에 쌓는지**가
   곧 그 곡선의 정체다 — 기본값을 두면 말 안 하고 번인(`SIM-BURNIN-202512`)에 쌓을 수
   있고, 사람이 심어 둔 30일 위에 179일이 겹쳐 앉는다.

🔴 **여기에 판단이 없다. 개장도 입고도 채권도 수금도 출고도 여기서 다시 짜지 않는다.**

  그 순서는 `scheduler.run_scheduled_day` 가 이미 안다. 이 파일은 **날짜 축**만
  진다 — 어느 날을 걷고 어느 날을 건너뛰고 어디서 멈추는가.

★ **왜 저장소에 세우나.** 179 영업일 걷기를 임시 스크립트로 돌려 성적을 냈는데,
  그 스크립트가 저장소 밖이라 다른 사람이 같은 걸음을 못 걷는다. 성적만 남고
  **그 성적을 낸 걸음이 안 남는 것**이 문제다.

⚠️ **그 임시 스크립트는 `run_procurement` 을 직접 불렀다.** 그래서 개장 · 입고 ·
  채권 · 수금 · 장부 관문이 한 번도 안 돌았고, *"사고 0건"* 은 **그 다섯 단계를 안 탄
  채로** 나온 숫자였다. 이 파일이 고치는 것이 정확히 그것이다.

```text
❌ run_procurement(ProcurementRunRequest(as_of=d, ...))   개장·입고·채권·수금·장부게이트를 건너뛴다
🟢 run_scheduled_day(action, ...)                          오늘 선 순서 전부를 안다
```

---

🔴 **승인은 명시로만 켠다** (2026-09-11).

```text
--auto-approve 를 **안 주면**   승인 함수가 이름조차 안 불린다 · 한 건도 안 선다
--auto-approve 를 주면          각 판단 **바로 뒤**에 규칙대로 승인이 선다
```

★★ **설정에 규칙이 있다고 켜지지 않는다.** *"있으니까 한다"* 는 암묵 스위치이고,
  그러면 설정을 실험하려고 넣은 사람이 **승인까지 하게 된다.**

⚠️ **반대로, 켰는데 규칙이 없으면 걷기 전에 막는다.** 조용히 걸으면 179일 뒤에
  0건이 나오고 사람은 그것을 *"돌았는데 해당이 없었구나"* 로 읽는다.

---

🔴 **물류 유지보수도 명시로만 켠다** (2026-09-11).

```text
--auto-maintain 를 **안 주면**   유지보수 함수가 이름조차 안 불린다 · 한 Lot 도 안 없앤다
--auto-maintain 를 주면          **개장 바로 뒤**에 그날 자리를 비운다
```

★★ **승인보다 더 조심할 자리다.** 승인은 `master_decisions` 에 한 줄이 남는
  것이지만 폐기는 **물건이 없어진다** — 물류가 *"되돌릴 경로가 없다(`ADJUST_IN`
  없음 · 실사 제외)"* 고 못박았다.

⚠️ **켰는데 버릴 것이 없어도 막지 않는다.** `--auto-approve` 와 축이 다르다 —
  저쪽은 규칙 파일이 있어야 성립하고, 이쪽은 확인할 설정이 없다. 버릴 것이 없는
  날은 사고가 아니라 정상이고, 그 사실은 `NOTHING_DUE` 가 말한다.

---

🔴 **`now` 를 인자로 받는다. 시계를 안 읽는다.**

  `plan_next_action` 이 마감(10:30)과 비교하는 값이 `now` 다. 이 파일이 시계를
  읽으면 *"마감 뒤"* 상태를 검사가 만들 수 없고, 걷기 결과가 **돌린 시각에 따라
  갈린다.**

  ★ **받은 시각에서 시각만 떼어 그날에 붙인다.** 걷는 날마다 마감 비교가 그날의
    10:30 을 봐야 하기 때문이다. 받은 `now` 를 그대로 179일에 다 쓰면 첫날 말고는
    전부 *"마감이 한참 지난 미래"* 가 된다.

  ⚠️ 시간대는 받은 값의 것을 그대로 나른다 (`timetz()`). 여기서 `ZoneInfo` 를
    새로 만들지 않는다 — 시간대의 주인도 `clock.py` 하나다.

---

🔴 **달력을 못 읽으면 멈춘다. 건너뛰지 않는다.**

```text
is_market_open(day) == False   → 건너뛴다 (skipped_days 에 남는다)
CalendarNotCovered             → 🔴 멈추고 사유를 낸다
```

★ 그 예외는 *"장이 서는지 아닌지를 이 표로는 말할 수 없다"* 는 뜻이다. 건너뛰면
  **모르는 날을 안 서는 날로** 바꾸는 것이고, 그러면 성적표의 분모가 조용히 줄어든다.

---

🔴 **하루가 터져도 다음 날을 계속 걷는다. 다만 상한을 둔다.**

  ★ 하루가 터졌다고 걷기를 멈추면 179일 중 3일째에서 끝나고 나머지를 못 본다.

  ⚠️ **그런데 상한이 없으면 반대로 망가진다.** 장부가 깨진 채 179일을 걸으면 사고
    목록이 179줄이고 아무도 안 읽는다. 그리고 그 179줄은 **한 가지 사실**이다 —
    첫날 깨진 것이 안 고쳐졌다는 것. 연속 실패가 상한에 닿으면 멈추고 사유를 낸다.

---

⚠️ **어휘를 새로 만들지 않았다.** 세는 값은 전부 `scheduler` 가 낸 것 그대로다
  (`DayRunOutcome.action` · `ItemRunOutcome.end_code` · `procurement_status` 의
  `NOT_ATTEMPTED` · `sales_status` 의 세 값 · `outbound_status` 의 네 값 ·
  `RetryOut.outcomes` 의 네 값 · `MaintenanceOut.outcomes` 가 나르는
  `MaintenanceOutcome` 의 네 값 · `failed_items`). 그 네 값은 **접지
  않고 그대로 센다** — `NOTHING_DUE`(없다)와 `FAILED`(못 했다)와
  `NOT_ATTEMPTED`(안 했다)를 묶으면 손익 곡선이 왜 평평한지를 성적표가 못 답한다.
  이 파일이 새로 만든 말은 걷기 자체에 관한 것
  뿐이다 (`skipped_days` · `incidents` · `stopped_reason`) — 그것은 `scheduler` 가
  모르는 사실이다. 스케줄러는 하루만 알지 **범위를 모른다.**
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.master.backfill import (
    BackfillRuleMissing,
    BackfillRules,
    SalesTermsRule,
    read_run_rules,
)
from app.master.bootstrap import wire_registries
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, day_forecast_readiness
from app.master.market_calendar import MarketCalendar, get_market_calendar
from app.master.sales_terms import read_run_sales_terms
from app.master.scheduler import (
    DAILY_POLICY_VERSION,
    DayRunOutcome,
    plan_next_action,
    run_scheduled_day,
)

__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "WalkIncident",
    "WalkResult",
    "format_summary",
    "main",
    "walk",
]

#: 연속 사고 상한. **닿으면 멈추고 사유를 낸다.**
#:
#: ★ **왜 5인가.** 영업일 닷새다. 한 주를 통째로 못 걸었으면 그 다음 날의 장부는
#:   이미 못 믿는다 — 안 받은 입고와 안 받은 수금이 닷새치 쌓인 위에서 판단이 돌고,
#:   거기서 나온 성적은 걷기의 성적이 아니라 **깨진 장부의 성적**이다.
#:
#: ⚠️ `calendar_walk.MAX_WALK_DAYS` 를 안 쓴다. 그 31 은 *"다음 실행일을 찾으러 앞으로
#:   걷는 상한"* 이고 축이 다르다 — 179일 걷기는 그 수를 당연히 넘긴다.
MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True)
class WalkIncident:
    """걷다 만난 사고 하나. **날짜와 사유만 든다.**

    ★ 사유 문장은 `scheduler` 가 낸 값을 그대로 옮긴다. 여기서 다시 이름 붙이면
      같은 사실에 이름이 둘이 된다.
    """

    as_of: date
    reason: str


@dataclass(frozen=True)
class WalkResult:
    """걷기 한 번의 결과.

    ⚠️ **`days` 는 실제로 하루 실행을 부른 날만 든다.** 휴장일은 `skipped_days` 로
      간다 — 둘을 섞으면 *"179일 중 몇 날을 돌았나"* 를 못 센다.
    """

    start: date
    end: date
    #: 하루 실행을 부른 날의 결과. **부른 순서 그대로.**
    days: tuple[DayRunOutcome, ...] = ()
    #: 달력이 *"안 선다"* 고 한 날.
    skipped_days: tuple[date, ...] = ()
    #: 사고 목록. **터진 날도 걷기는 이어졌다** (상한에 닿기 전까지).
    incidents: tuple[WalkIncident, ...] = ()
    #: 걷기가 끝까지 못 갔으면 멈춘 날. 끝까지 갔으면 `None`.
    stopped_at: date | None = None
    #: 멈춘 사유. 🔴 **`stopped_at` 과 짝이다** — 하나만 있으면 안 된다.
    stopped_reason: str | None = None
    #: 소요 시간(초). 벽시계가 아니라 단조 시계로 잰다 (아래 `ticks` 주석).
    elapsed_seconds: float = 0.0

    @property
    def completed(self) -> bool:
        """끝까지 걸었는가."""
        return self.stopped_reason is None

    @property
    def actions(self) -> Mapping[str, int]:
        """판단 분포. **`SchedulerAction` 값을 센다** — 새 이름을 안 붙인다."""
        return Counter(one.action for one in self.days)

    @property
    def end_codes(self) -> Mapping[str, int]:
        """품목 종료 코드 분포. **못 돈 품목은 `FAILED` 로 센다.**

        ★ `end_code` 가 `None` 인 것은 *"코드가 없었다"* 이고, 그 자리는 `status` 가
          이미 `FAILED` 라고 말한다 (`ItemRunOutcome` 의 어휘 그대로).
        """
        return Counter(
            one.end_code if one.end_code is not None else one.status
            for day in self.days
            for one in day.items
        )

    @property
    def sales_end_codes(self) -> Mapping[str, int]:
        """판매 판단의 품목 종료 코드 분포 (2026-09-10). 🔴 **매입과 한 칸에 안 담는다.**

        ★ **왜 `end_codes` 와 가르나.** 어휘가 다르다 — 매입은 `E1`·`E4`, 판매는
          `SL1`·`SL4` 다. 한 Counter 에 담으면 *"오늘 어느 사이클이 어떻게 끝났나"*
          가 두 어휘가 섞인 한 표가 되고, 어느 쪽 수가 는 것인지를 못 읽는다.

        🔴 **접지 않는다.** `SL1_PRESENTED` 를 *"돌았다"* 로 묶으면 걷기 179일에
          **후보가 실제로 나온 날이 며칠인지**를 성적표가 못 답한다 — 이 판이
          존재하는 이유가 그 숫자다.
        """
        return Counter(
            one.end_code if one.end_code is not None else one.status
            for day in self.days
            for one in day.sales_items
        )

    @property
    def sales_statuses(self) -> Mapping[str, int]:
        """판매 판단 단계 분포. 🔴 **세 값을 접지 않고 그대로 센다.**

        ```text
        RAN            돌았다
        FAILED         해 보고 터졌다 — 돈 품목이 하나도 없다   ← "못 했다"
        NOT_ATTEMPTED  거기까지 못 갔다                        ← "안 했다"
        ```

        ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다** — `outbound_status` 를
          성적표에 태울 때(`#446`) 배운 그것이다.
        """
        return Counter(one.sales_status for one in self.days)

    @property
    def receivable_statuses(self) -> Mapping[str, int]:
        """채권 발행 단계 분포. 🔴 **다섯 값을 접지 않고 그대로 센다.**

        ```text
        ISSUED         대상이 있었고 채권이 서 있다
        NOTHING_DUE    그날 확정된 판매가 없었다     ← "없다"
        BLOCKED        대상은 있는데 못 세웠다        ← "못 했다"
        NOT_OPENED     하루가 안 열려서 안 했다       ← "안 했다"
        FAILED         세워 보다 터졌다
        ```

        ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다.** `outbound_status` 를
          성적표에 태울 때(`#446`) 배운 것이 그것이다 — 단계는 도는데 화면이 그
          단계를 말하지 않으면 아무도 그 단계가 막힌 것을 모른다.
        """
        return Counter(one.receivable_status for one in self.days)

    @property
    def outbound_statuses(self) -> Mapping[str, int]:
        """출고 단계 분포. 🔴 **네 값을 접지 않고 그대로 센다.**

        ```text
        RAN            나갔다
        NOTHING_DUE    나갈 것이 없었다        ← "없다"
        FAILED         나가려다 못 나갔다      ← "못 했다"
        NOT_ATTEMPTED  거기까지 못 갔다        ← "안 했다"
        ```

        ⚠️ **`RAN` 만 세고 나머지를 묶으면 안 된다.** 손익 곡선이 평평할 때 그것이
          *"나갈 것이 없어서"* 인지 *"나가려다 못 나가서"* 인지를 성적표가 답해야
          하고, 묶는 순간 그 답이 사라진다.

        ★ `end_codes` 와 같은 모양이다 — `scheduler` 가 낸 값을 세기만 한다.
        """
        return Counter(one.outbound_status for one in self.days)

    @property
    def approval_statuses(self) -> Mapping[str, int]:
        """자동 승인 **단계** 분포 (2026-09-11). 🔴 **네 값을 접지 않고 그대로 센다.**

        ```text
        NOT_ATTEMPTED  안 켰다 — --auto-approve 를 안 줬다        ← "안 했다"
        RAN            승인 문까지 돌았다
        NO_RULE        켰는데 그 실행이 규칙을 안 들었다           ← "못 했다"
        FAILED         돌리다 터졌다
        ```

        ⚠️ **매입과 판매를 한 통에 센다.** 어느 사이클이 안 섰는지는 `days` 의 두
          칸이 그대로 들고 있고, 여기서 묻는 것은 *"며칠에 승인 단계가 돌았나"* 다.

        ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다** — `outbound_status` 를
          성적표에 태울 때(`#446`) 배운 그것이다.
        """
        return Counter(
            status
            for day in self.days
            for status in (day.procurement_approval_status, day.sales_approval_status)
        )

    @property
    def transition_outcomes(self) -> Mapping[str, int]:
        """미적용 전이 재시도의 **승인별** 결과 분포 (2026-09-11). 🔴 **넷을 접지 않는다.**

        ```text
        APPLIED         원장에 닿았다
        NOT_APPLIED     아직 쓸 것이 없다      ← "없다"
        FAILED          쓰려다 터졌다          ← "못 했다"
        NOT_BUILDABLE   약정을 못 만들었다     ← 전이 앞에서 끝났다
        ```

        ★★ **이 줄이 없어서 「승인 15건 RECORDED」 를 보고 원장에 닿은 줄 알았다**
          (실측 2026-09-11). `approval_outcomes` 는 *"승인을 적었나"* 까지만 답한다 —
          그 승인이 **장부에 닿았나**는 축이 하나 더 뒤다.

        🔴 **`approval_outcomes` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다 —
          담으면 *"적었다"* 와 *"닿았다"* 가 한 표에 섞여 어느 쪽 수가 는 것인지를
          못 읽는다 (`end_codes` 와 `sales_end_codes` 를 가른 것과 같은 이유).

        ★ **이름의 주인은 `pending_transition.py` 다.** 여기서 새 이름을 안 붙이고
          세기만 한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            if day.pending_transition is not None:
                total.update(day.pending_transition.outcomes)
        return total

    @property
    def maintenance_statuses(self) -> Mapping[str, int]:
        """물류 유지보수 **단계** 분포 (2026-09-11). `approval_statuses` 와 같은 자리다.

        🔴 **Lot 축 한 줄로는 「안 켰다」와 「켰는데 0 Lot」이 안 갈린다.** 둘 다 `{}`
           로 나오고, 그러면 성적표를 보는 사람이 *"폐기할 것이 없었구나"* 로 읽는다 —
           실제로는 안 켠 것일 수 있다.

        ★★ 구현이 이 구멍을 보고했고 값은 이미 `DayRunOutcome.maintenance_status` 에
          안 접힌 채 있었다. **재는 줄만 없었다.** 승인이 `승인`·`승인어휘` 두 줄인
          것과 같은 이유로 여기도 둘이다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            total[day.maintenance_status] += 1
        return total

    @property
    def maintenance_outcomes(self) -> Mapping[str, int]:
        """물류 유지보수의 **Lot 별** 결과 분포 (2026-09-11). 🔴 **넷을 접지 않는다.**

        ```text
        DISPOSED                 전량 폐기했다 · 자리도 돌려줬다
        SKIPPED_HELD_ALLOCATION  살아있는 할당이 있어 **손대지 않았다** ← 사람 몫이다
        PALLETS_EMPTIED          잔량이 이미 0 이라 자리만 돌려줬다
        FAILED                   도메인이 거절했다                     ← "못 했다"
        ```

        ★★ **`SKIPPED_HELD_ALLOCATION` 이 안 보이면 자동화가 왜 덜 했는지를 성적표가
          못 답한다.** 물류가 *"자동 부분 폐기를 하지 않는다 — 남은 판단은 사람
          몫이다"* 로 일부러 남긴 줄이고, 창고가 안 비는 날 **거기부터 봐야** 한다.

        ★ **이름의 주인은 `logistics/auto_maintenance.py` 다.** 여기서 새 이름을
          안 붙이고 세기만 한다 (`transition_outcomes` 와 같은 모양).

        🔴 **`approval_outcomes` 와 한 칸에 담지 않는다.** 어휘가 다르고 축이 다르다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            if day.maintenance is not None:
                total.update(day.maintenance.outcomes)
        return total

    @property
    def approval_outcomes(self) -> Mapping[str, int]:
        """승인 **행별** 어휘 분포 (2026-09-11). 🔴 **여덟을 접지 않는다.**

        ```text
        RECORDED · ALREADY_DECIDED · NOT_APPROVABLE · NO_RULE_FOR_CYCLE
        LABEL_NOT_OFFERED · AMBIGUOUS_TYPE · BLOCKED_BY_BOUNDARY · FAILED
        ```

        ★ **이름의 주인은 `backfill.py` 다.** 여기서 새 이름을 안 붙이고 세기만
          한다 — `end_codes` 가 `scheduler` 의 값을 그대로 세는 것과 같은 모양이다.

        🔴 **`approval_statuses` 와 축이 다르다.** 저쪽은 하루의 단계이고 이쪽은
          실행 이력 한 행이다 — 묶으면 *"승인이 왜 0건인가"* 를 성적표가 못 답한다.
        """
        total: Counter[str] = Counter()
        for day in self.days:
            for approval in (day.procurement_approval, day.sales_approval):
                if approval is not None:
                    total.update(approval.outcomes)
        return total


def walk(
    *,
    sim_run_id: str,
    start: date,
    end: date,
    now: datetime,
    calendar: Callable[[], MarketCalendar] = get_market_calendar,
    readiness: Callable[[date], DayForecastReadiness] = lambda as_of: day_forecast_readiness(
        as_of=as_of
    ),
    run_day_fn: Callable[..., DayRunOutcome] = run_scheduled_day,
    policy_version: str = DAILY_POLICY_VERSION,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ticks: Callable[[], float] = time.monotonic,
    auto_approve: bool = False,
    rules_of: Callable[[str], BackfillRules] = read_run_rules,
    terms_of: Callable[[str], SalesTermsRule | None] = read_run_sales_terms,
    auto_maintain: bool = False,
) -> WalkResult:
    """`start` 부터 `end` 까지 하루씩 걷는다. **개장일마다 하루 실행을 부른다.**

    🔴 **`run_scheduled_day` 를 부른다.** `run_procurement` 을 직접 부르지 않는다 —
      그러면 개장 · 입고 · 채권 · 수금 · 장부 관문을 통째로 건너뛰고, 그 위에서 나온
      *"사고 0건"* 은 아무것도 증명하지 않는다.

    :param sim_run_id: 어느 실행에 이 걸음을 쌓는가. 🔴 **기본값이 없다 — 안 주면
        터진다.** 179일을 어느 실행에 쌓는지가 곧 그 곡선의 정체이고, 기본값을 두면
        **말 안 하고 번인에 쌓을 수 있다.** 그러면 사람이 심어 둔 30일 위에 179일이
        겹쳐 앉고, 어느 행이 번인이고 어느 행이 걷기인지 되가를 방법이 없다.

        ★ **여기서 실행을 만들지 않는다.** 행을 세우는 것은 `sim_run.create_sim_run`
          이고, 이 파일은 **받은 축을 나르기만** 한다 — 걷기가 실행을 만들면 같은
          범위를 두 번 걸을 때마다 실행이 하나씩 늘어난다.

    :param now: 걷는 동안 쓸 시각. 🔴 **인자다 — 이 파일은 시계를 안 읽는다.**
        날짜는 안 쓰고 **시각만** 떼어 걷는 날마다 붙인다 (모듈 docstring).
    :param calendar: 개장 축. `is_market_open` 하나만 부른다.
    :param readiness: 그날 예측 게이트. `wake_up` 과 같은 모양으로 받는다.
    :param run_day_fn: 하루 실행. 🔴 **기본값이 `run_scheduled_day` 자체다** —
        `None` 을 안 받는다 (`clock.py` · `verifier.py` 와 같은 규율).
    :param ticks: 소요 시간을 재는 단조 시계. 🔴 **벽시계가 아니다** — 날짜도
        시간대도 안 만들고 *"얼마나 걸렸나"* 만 답한다. 검사가 고정값을 꽂는다.
    :param auto_approve: 🔴 **기본이 거짓이다. 안 주면 승인 함수가 이름조차 안
        불린다** (2026-09-11). 켜면 각 판단 **바로 뒤**에 승인이 선다.

        ★★ **설정에 규칙이 있다고 켜지지 않는다.** *"있으니까 한다"* 는 암묵
          스위치이고, 그러면 설정을 실험하려고 넣은 사람이 **승인까지 하게 된다.**
          켜는 것은 명시로만이고, 그 명시가 이 인자 하나다.
    :param rules_of: 그 실행이 정한 **승인** 규칙을 읽는 자리. 🔴 **`auto_approve` 가
        거짓이면 한 번도 안 불린다** — 안 켠 걷기가 승인 규칙을 물을 이유가 없다.
    :param terms_of: 그 실행이 정한 **판매 상업 조건**을 읽는 자리 (2026-09-11).

        🔴 **`rules_of` 와 한 인자로 묶지 않는다.** 축이 다르다 — 저쪽을 부르는
          것은 *"안 켜고 걸으면 막는다"* 는 관문이라 **안 켜면 안 부르는 것이
          잠겨 있다.** 이쪽은 관문이 아니라 요청에 실릴 값이고, 승인을 안 켜도
          조건은 실려야 한다. 하나로 묶으면 *"승인을 켜야 조건이 실린다"* 가 되고
          그 사실은 아무 데도 안 적혀 있다.

        ★ **실행당 한 번 부른다.** 규칙은 실행에 속하지 날에 속하지 않는다.
    :param auto_maintain: 🔴 **기본이 거짓이다. 안 주면 유지보수 함수가 이름조차
        안 불린다** (2026-09-11). 켜면 **개장 바로 뒤**에 그날 자리를 비운다.

        ★★ **승인보다 더 조심할 자리다.** 폐기는 되돌릴 경로가 없다 —
          `ADJUST_IN` 도 실사도 없다고 물류가 못박았다.

        ⚠️ **`auto_approve` 처럼 걷기 전에 막는 관문이 없다.** 확인할 규칙 파일이
          없기 때문이다 — 버릴 것이 없는 날은 사고가 아니라 `NOTHING_DUE` 다.
    :raises ValueError: 범위가 거꾸로거나 `now` 에 시간대가 없거나 `sim_run_id` 가
        빈 문자열일 때. **막고 사유를 낸다** — 조용히 바로잡지 않는다.

        🔴 **`auto_approve` 인데 그 실행이 규칙을 안 들었을 때도 막는다.** 조용히
          걸으면 179일 뒤에 승인 0건이 나오고, 사람은 그것을 *"승인이 돌았는데
          해당이 없었구나"* 로 읽는다 — 그때는 하루도 되돌릴 수 없다.
    """
    if not sim_run_id.strip():
        # 🔴 **상수로 메우지 않는다.** 조용히 번인으로 떨어지면 재무 채무와 매입
        #    원장이 서로 다른 실행에 앉고, 그때는 아무 오류도 안 난다.
        raise ValueError(
            "sim_run_id 없이는 걸을 수 없다 — 어느 실행에 쌓는지가 곡선의 정체다."
            " 실행을 먼저 만들고(`sim_run.create_sim_run`) 그 이름을 넘겨라"
        )
    if start > end:
        raise ValueError(
            f"걷기 범위가 거꾸로다: {start.isoformat()} ~ {end.isoformat()}"
            " — 어느 쪽이 시작인지를 여기서 정하지 않는다"
        )
    if now.tzinfo is None:
        raise ValueError(
            "시간대 없는 시각으로는 마감을 못 잰다 — 어느 지역의 10:30 인지가 없다."
            " walk 는 시계를 안 읽으므로 부르는 쪽이 시간대를 붙여 줘야 한다"
        )
    if max_consecutive_failures < 1:
        raise ValueError(
            f"연속 사고 상한이 {max_consecutive_failures} 다 — 1 보다 작으면 한 날도 못 걷는다"
        )

    # ── 🔴 **켰으면 걷기 전에 규칙을 확인한다** (2026-09-11) ────────────
    #
    # ⚠️ **조용히 아무것도 안 하면 사람이 「승인이 돌았는데 0건이구나」 로 읽는다.**
    #    그래서 첫날을 걷기도 전에 막는다 — 179일을 다 걷고 나서 알면 늦다.
    #
    # 🔴 **여기서 규칙을 지어내지 않는다.** 기본 규칙은 곧 업무 규칙이고, 그러면
    #    아무도 안 정한 규칙으로 곡선이 선다 (`backfill.py` 의 그 규율 그대로).
    if auto_approve:
        try:
            rules_of(sim_run_id)
        except BackfillRuleMissing as exc:
            raise ValueError(
                f"--auto-approve 인데 실행 {sim_run_id!r} 이 백필 규칙을 안 들었다: {exc}"
                " — 규칙을 실은 실행을 먼저 열어라(`sim_run_runner --backfill-rules`)."
                " 조용히 0건으로 걷지 않는다"
            ) from exc

    # 🔴 **상업 조건은 걷기 전에 한 번 읽는다** (2026-09-11). 규칙은 실행에 속하지
    #    날에 속하지 않는다 — 날마다 읽으면 같은 설정을 179번 다시 읽고, 그러다
    #    하루만 다른 조건으로 도는 날이 오면 **왜 그런지를 설정만 보고는 못 읽는다.**
    #
    # ⚠️ **여기서 막지 않는다.** 조건이 없는 것은 사고가 아니라 종전 동작이다 —
    #    `auto_approve` 관문과 축이 다르다 (`terms_of` 설명).
    sales_terms = terms_of(sim_run_id)

    market = calendar()
    started_ticks = ticks()

    days: list[DayRunOutcome] = []
    skipped: list[date] = []
    incidents: list[WalkIncident] = []
    stopped_at: date | None = None
    stopped_reason: str | None = None
    in_a_row = 0

    day = start
    while day <= end:
        # ── ① 달력 ──────────────────────────────────────────────────
        try:
            is_open = market.is_market_open(day)
        except CalendarNotCovered as exc:
            # 🔴 **건너뛰지 않는다.** 못 읽은 날을 안 선 날로 바꾸면 분모가 줄어든다.
            stopped_at = day
            stopped_reason = f"개장 달력을 못 읽어서 멈춘다: {exc}"
            break
        if not is_open:
            skipped.append(day)
            day += timedelta(days=1)
            continue

        # ── ② 하루 ──────────────────────────────────────────────────
        action = plan_next_action(
            now=_moment_on(day, now),
            as_of=day,
            calendar=market,
            gate_result=readiness(day),
        )
        try:
            # 🔴 **받은 축을 그대로 넘긴다.** 여기서 상수를 다시 읽거나 이름을
            #    고쳐 짓지 않는다 — 그러면 걷기가 부른 하루와 걷기가 말한 실행이
            #    갈리고, 성적표가 자기가 무엇을 쟀는지 모르게 된다.
            outcome = run_day_fn(
                action,
                policy_version=policy_version,
                sim_run_id=sim_run_id,
                # 🔴 **받은 스위치를 그대로 넘긴다.** 여기서 규칙의 유무를 보고
                #    다시 정하지 않는다 — 그러면 스위치가 둘이 된다.
                auto_approve=auto_approve,
                # 🔴 **읽은 조건을 그대로 넘긴다.** 하루가 제 손으로 다시 읽지
                #    않는다 — 그러면 같은 설정의 주인이 둘이 되고, 하루를 부르는
                #    모든 검사가 조용히 실 DB 를 친다.
                sales_terms=sales_terms,
                # 🔴 **받은 스위치를 그대로 넘긴다.** 여기서 다시 정하지 않는다 —
                #    그러면 폐기를 켜고 끄는 자리가 둘이 된다.
                auto_maintain=auto_maintain,
            )
        except Exception as exc:  # noqa: BLE001 - 하루가 터져도 다음 날은 걷는다.
            # ★ **터진 날도 사고로 남고 걷기는 이어진다.** 여기서 raise 하면 나머지
            #   날을 통째로 못 본다.
            incidents.append(
                WalkIncident(as_of=day, reason=f"하루 실행이 터졌다: {type(exc).__name__}: {exc}")
            )
            in_a_row += 1
        else:
            days.append(outcome)
            reason = _incident_reason(outcome, ran=action.should_run)
            if reason is None:
                in_a_row = 0
            else:
                incidents.append(WalkIncident(as_of=day, reason=reason))
                in_a_row += 1

        # ── ③ 연속 사고 상한 ────────────────────────────────────────
        if in_a_row >= max_consecutive_failures:
            stopped_at = day
            stopped_reason = (
                f"사고가 {in_a_row}일 연속이라 멈춘다 (상한 {max_consecutive_failures})"
                " — 이 뒤를 더 걸어도 같은 사실이 줄 수만 늘어난다"
            )
            break

        day += timedelta(days=1)

    return WalkResult(
        start=start,
        end=end,
        days=tuple(days),
        skipped_days=tuple(skipped),
        incidents=tuple(incidents),
        stopped_at=stopped_at,
        stopped_reason=stopped_reason,
        elapsed_seconds=ticks() - started_ticks,
    )


def _moment_on(day: date, now: datetime) -> datetime:
    """받은 시각의 **시각 부분**을 그날에 붙인다.

    🔴 **왜 필요한가.** `plan_next_action` 은 `now` 와 그날 10:30 을 비교한다. 받은
      `now` 를 179일에 그대로 쓰면 첫날 말고는 전부 마감이 한참 지난 것으로 읽히고,
      `NONE_READY` 인 날이 전부 `RUN_AND_RECORD` 가 된다.

    ★ **시간대를 새로 만들지 않는다.** `timetz()` 가 받은 값의 것을 그대로 나른다.
    """
    return datetime.combine(day, now.timetz())


def _incident_reason(outcome: DayRunOutcome, *, ran: bool) -> str | None:
    """이 날이 사고인가. 사고면 사유, 아니면 `None`.

    🔴 **`scheduler` 가 낸 값만 읽는다.** 여기서 상태 목록을 다시 세지 않는다 —
      세면 `_LEDGER_GAP_STATUSES` 가 두 곳에 생기고, 한쪽만 바뀌는 날이 온다.

    ```text
    action == BLOCKED                       달력이든 게이트든 못 읽었다
    돌기로 했는데 procurement_status 가
      NOT_ATTEMPTED                         개장이 막혔거나 장부 관문이 돌아섰다
    failed_items 가 비지 않았다              품목이 터졌다 (나머지는 돌았다)
    outbound_status == FAILED               나가려다 못 나갔다
    ```

    ⚠️ **`WAIT` 은 사고가 아니다.** *"아직"* 이지 *"못"* 이 아니다. 그 구분이
      `scheduler` 가 다섯 어휘를 가른 이유이고, 여기서 접으면 그게 무의미해진다.

    ⚠️ **`NOT_A_MARKET_DAY` 도 사고가 아니다.** 달력 검사가 먼저 걸러서 여기까지
      오지도 않지만, 온다 해도 *"안 서는 날"* 은 정상이다.

    ⚠️ **출고 `NOTHING_DUE` 도 같은 결로 사고가 아니다.** *"나갈 것이 없다"* 는
      *"못 나갔다"* 가 아니다 — 예약이 아직 0행인 지금 그것을 사고로 세면 **매일이
      사고**가 되고, 사고 목록이 아무것도 안 가리킨다.

    🔴 **출고 `NOT_ATTEMPTED` 를 여기서 다시 세지 않는다.** 거기까지 못 간 날은
      판단 단계를 안 탄 날이고, 그것은 **위 줄이 이미 사고로 잡았다** — 겹쳐 적으면
      한 사실이 사고 둘로 세진다.
    """
    if outcome.action == "BLOCKED":
        return f"BLOCKED — {outcome.reason}"
    if ran and outcome.procurement_status == "NOT_ATTEMPTED":
        # ★ 개장 실패와 장부 관문을 한 값이 이미 가른다 — 둘 다 판단 단계를 안 탄다.
        return (
            f"판단 단계를 안 탔다 (개장: {outcome.day_open_status} ·"
            f" 입고: {outcome.inbound_status} · 채권: {outcome.receivable_status}"
            f" · 수금: {outcome.collection_status})"
            + (f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
        )
    if outcome.failed_items:
        return f"품목이 터졌다: {', '.join(outcome.failed_items)}"
    if outcome.outbound_status == "FAILED":
        # ★ **사유를 여기서 짓지 않는다.** 무엇이 못 나갔는지는 `OutboundOut.reason`
        #   이 알고, `_stage` 가 그것을 note 로 실어 보냈다 — 그 값을 그대로 나른다.
        return "출고가 못 나갔다" + (f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
    return None


# ── 진입점 — 인자만 받는다 ─────────────────────────────────────────────
#
# 🔴 **로직이 여기 없다.** `walk()` 가 다 안다. 이 아래는 문자열을 날짜로 바꾸고
#    결과를 사람이 읽게 찍는 것뿐이다.


def _parser() -> argparse.ArgumentParser:
    """인자 정의. 🔴 **날짜에 기본값이 없다.**

    ⚠️ 기본 범위를 두면 **그 범위가 곧 업무 규칙이 된다** — 아무도 그것을 정한 적이
      없는데 성적표마다 그 범위가 찍히고, 나중에 *"왜 2월 7일부터인가"* 에 답할
      사람이 없다. 안 주면 막고 사유를 낸다.

    ⚠️ `--now` 도 기본값이 없다. 여기서 시계를 읽으면 **돌린 시각에 따라 성적이
      갈리고**, 그 사실이 성적표 어디에도 안 남는다.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.master.backtest_runner",
        description="범위를 하루씩 걸으며 개장일마다 하루 실행(run_scheduled_day)을 부른다",
    )
    parser.add_argument(
        "--sim-run-id",
        required=True,
        help="어느 실행에 쌓는가 (예: SIM-WALK-202601) · 🔴 기본값 없음 — 안 주면 막는다",
    )
    parser.add_argument("--start", required=True, help="걷기 시작일 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="걷기 종료일 (YYYY-MM-DD · 포함)")
    parser.add_argument(
        "--now",
        required=True,
        help="걷는 동안 쓸 시각 (ISO 8601 · 시간대 필수 · 예: 2026-09-07T10:35+09:00)",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=MAX_CONSECUTIVE_FAILURES,
        help=f"연속 사고 상한 (기본 {MAX_CONSECUTIVE_FAILURES})",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help=(
            "🔴 각 판단 바로 뒤에 규칙대로 승인한다 · master_decisions 에 행이 쓰인다"
            " · 안 주면 한 건도 승인하지 않는다"
        ),
    )
    parser.add_argument(
        "--auto-maintain",
        action="store_true",
        default=False,
        help=(
            "🔴 개장 바로 뒤에 그날 창고 자리를 비운다 · 폐기대기 Lot 이 없어진다"
            " (되돌릴 경로 없음) · 안 주면 한 Lot 도 건드리지 않는다"
        ),
    )
    return parser


def format_summary(result: WalkResult) -> str:
    """걷기 결과를 사람이 읽을 줄로. **값을 새로 만들지 않는다.**"""
    lines = [
        f"범위      {result.start.isoformat()} ~ {result.end.isoformat()}",
        f"돈 날     {len(result.days)}일 · 휴장 {len(result.skipped_days)}일",
        f"판단      {dict(sorted(result.actions.items()))}",
        f"종료코드  {dict(sorted(result.end_codes.items()))}",
        f"채권      {dict(sorted(result.receivable_statuses.items()))}",
        f"판매      {dict(sorted(result.sales_statuses.items()))}",
        f"판매코드  {dict(sorted(result.sales_end_codes.items()))}",
        f"출고      {dict(sorted(result.outbound_statuses.items()))}",
        # 🔴 **승인 줄을 접지 않는다** (2026-09-11). 단계와 어휘가 축이 다르므로
        #    두 줄이다 — 한 줄로 묶으면 *"안 켰다"* 와 *"켰는데 0건"* 이 같아 보인다.
        f"승인      {dict(sorted(result.approval_statuses.items()))}",
        f"승인어휘  {dict(sorted(result.approval_outcomes.items()))}",
        # 🔴 **전이 줄을 접지 않는다** (2026-09-11). *"승인을 적었다"* 와 *"그 승인이
        #    원장에 닿았다"* 는 축이 다르다 — 이 줄이 없어서 `RECORDED 15` 를 보고
        #    원장에 닿은 줄 알았고, `purchases` 는 0행이었다.
        f"전이      {dict(sorted(result.transition_outcomes.items()))}",
        # 🔴 **유지보수 줄을 접지 않는다** (2026-09-11). 몇 Lot 이 없어졌고 몇이
        #    **사람 몫으로 남았는지**가 보여야 한다 — 창고가 안 비는 날 봐야 할
        #    자리가 `SKIPPED_HELD_ALLOCATION` 이고, 접으면 그 줄이 사라진다.
        f"유지보수  {dict(sorted(result.maintenance_statuses.items()))}",
        f"유지어휘  {dict(sorted(result.maintenance_outcomes.items()))}",
        f"사고      {len(result.incidents)}건",
        f"소요      {result.elapsed_seconds:.1f}초",
    ]
    lines += [f"  사고 {one.as_of.isoformat()}  {one.reason}" for one in result.incidents]
    if result.stopped_reason is not None:
        lines.append(f"멈춤      {result.stopped_at} — {result.stopped_reason}")
    return "\n".join(lines)


def _use_utf8_output() -> None:
    """요약을 찍다 죽지 않게 출력 스트림을 UTF-8 로 맞춘다.

    🔴 **걷기를 다 마치고 `print` 에서 죽었다** (2026-09-09 실측).

    ```text
    UnicodeEncodeError: 'cp949' codec can't encode character '\\u2014'
      File "app/master/backtest_runner.py", line 401, in main
        print(format_summary(result))
    ```

      `format_summary` 는 한국어와 `—` 로 적는다. 윈도우 기본 인코딩이 cp949 라
      그 한 글자에서 터졌고, **결과는 다 계산해 놓고 성적표만 잃었다** — 다시
      보려면 걷기를 통째로 또 돌려야 한다.

    ★ **요약 문장을 ASCII 로 낮추지 않는다.** 사람이 읽으라고 쓴 한국어이고,
      바꿔야 할 것은 문장이 아니라 그 문장을 내보내는 통로다.

    ⚠️ **여기서만 바꾼다 — 라이브러리 코드가 아니라 진입점이다.** `walk()` 나
      `format_summary()` 가 프로세스 전역 스트림을 건드리면, 그것을 부르는 쪽의
      출력 설정까지 이 모듈이 정하는 셈이 된다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # ★ 감싸인 스트림(파이프 대역·캡처)이면 그쪽 규칙을 따른다. 여기서
            #   억지로 바꾸려다 진입점이 터지면 고치려던 것과 같은 일이 난다.
            continue
        reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: Sequence[str]) -> int:
    """진입점. **인자만 받아 `walk()` 에 넘긴다.**

    :returns: 끝까지 걸었고 사고가 없으면 0. 아니면 1 — **조용히 0 을 내지 않는다.**
    """
    args = _parser().parse_args(argv)
    _use_utf8_output()
    # 🔴 **걷기 전에 등록소를 채운다.** 이 진입점은 `app/main.py` 를 안 거치므로,
    #    이 줄이 없으면 등록소가 전부 빈 채로 걷는다 — 걷는 날마다 *"하루 넘김
    #    미등록: finance, logistics"* 로 돌아서고 5일이 5일 다 사고였다 (2026-09-09).
    #
    # ★ **여기서 무엇을 등록할지 정하지 않는다.** 목록의 주인은 `bootstrap` 하나이고
    #   FastAPI 진입점도 같은 함수를 부른다. 두 진입점이 다른 세상을 보면 CLI 로 낸
    #   성적이 앱의 성적이 아니다.
    #
    # ⚠️ **`walk()` 안이 아니라 여기다.** `walk` 는 검사가 대역을 꽂아 부르는 함수이고,
    #   거기서 전역 등록소를 채우면 검사가 만든 세상을 조립 뿌리가 덮어쓴다.
    wire_registries()
    result = walk(
        sim_run_id=args.sim_run_id,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        now=datetime.fromisoformat(args.now),
        max_consecutive_failures=args.max_consecutive_failures,
        auto_approve=args.auto_approve,
        auto_maintain=args.auto_maintain,
    )
    print(format_summary(result))
    return 0 if result.completed and not result.incidents else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
