"""
backtest_runner.py — **범위를 하루씩 걸으며 하루 실행을 부른다.**

```text
walk(start=..., end=..., now=...)   start..end 를 하루씩 걷는다
                                    개장일마다 run_scheduled_day 를 부른다
```

🔴 **여기에 판단이 없다. 개장도 입고도 수금도 출고도 여기서 다시 짜지 않는다.**

  그 순서는 `scheduler.run_scheduled_day` 가 이미 안다. 이 파일은 **날짜 축**만
  진다 — 어느 날을 걷고 어느 날을 건너뛰고 어디서 멈추는가.

★ **왜 저장소에 세우나.** 179 영업일 걷기를 임시 스크립트로 돌려 성적을 냈는데,
  그 스크립트가 저장소 밖이라 다른 사람이 같은 걸음을 못 걷는다. 성적만 남고
  **그 성적을 낸 걸음이 안 남는 것**이 문제다.

⚠️ **그 임시 스크립트는 `run_procurement` 을 직접 불렀다.** 그래서 개장 · 입고 ·
  수금 · 장부 관문이 한 번도 안 돌았고, *"사고 0건"* 은 **그 네 단계를 안 탄 채로**
  나온 숫자였다. 이 파일이 고치는 것이 정확히 그것이다.

```text
❌ run_procurement(ProcurementRunRequest(as_of=d, ...))   개장·입고·수금·장부게이트를 건너뛴다
🟢 run_scheduled_day(action, ...)                          오늘 선 순서 전부를 안다
```

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
  `NOT_ATTEMPTED` · `failed_items`). 이 파일이 새로 만든 말은 걷기 자체에 관한 것
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

from app.master.bootstrap import wire_registries
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, day_forecast_readiness
from app.master.market_calendar import MarketCalendar, get_market_calendar
from app.master.scheduler import (
    DAILY_POLICY_VERSION,
    DayRunOutcome,
    ScheduledAction,
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


def walk(
    *,
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
) -> WalkResult:
    """`start` 부터 `end` 까지 하루씩 걷는다. **개장일마다 하루 실행을 부른다.**

    🔴 **`run_scheduled_day` 를 부른다.** `run_procurement` 을 직접 부르지 않는다 —
      그러면 개장 · 입고 · 수금 · 장부 관문을 통째로 건너뛰고, 그 위에서 나온
      *"사고 0건"* 은 아무것도 증명하지 않는다.

    :param now: 걷는 동안 쓸 시각. 🔴 **인자다 — 이 파일은 시계를 안 읽는다.**
        날짜는 안 쓰고 **시각만** 떼어 걷는 날마다 붙인다 (모듈 docstring).
    :param calendar: 개장 축. `is_market_open` 하나만 부른다.
    :param readiness: 그날 예측 게이트. `wake_up` 과 같은 모양으로 받는다.
    :param run_day_fn: 하루 실행. 🔴 **기본값이 `run_scheduled_day` 자체다** —
        `None` 을 안 받는다 (`clock.py` · `verifier.py` 와 같은 규율).
    :param ticks: 소요 시간을 재는 단조 시계. 🔴 **벽시계가 아니다** — 날짜도
        시간대도 안 만들고 *"얼마나 걸렸나"* 만 답한다. 검사가 고정값을 꽂는다.
    :raises ValueError: 범위가 거꾸로거나 `now` 에 시간대가 없을 때.
        **막고 사유를 낸다** — 조용히 바로잡지 않는다.
    """
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
            outcome = run_day_fn(action, policy_version=policy_version)
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
    ```

    ⚠️ **`WAIT` 은 사고가 아니다.** *"아직"* 이지 *"못"* 이 아니다. 그 구분이
      `scheduler` 가 다섯 어휘를 가른 이유이고, 여기서 접으면 그게 무의미해진다.

    ⚠️ **`NOT_A_MARKET_DAY` 도 사고가 아니다.** 달력 검사가 먼저 걸러서 여기까지
      오지도 않지만, 온다 해도 *"안 서는 날"* 은 정상이다.
    """
    if outcome.action == "BLOCKED":
        return f"BLOCKED — {outcome.reason}"
    if ran and outcome.procurement_status == "NOT_ATTEMPTED":
        # ★ 개장 실패와 장부 관문을 한 값이 이미 가른다 — 둘 다 판단 단계를 안 탄다.
        return (
            f"판단 단계를 안 탔다 (개장: {outcome.day_open_status} ·"
            f" 입고: {outcome.inbound_status} · 수금: {outcome.collection_status})"
            + (f" — {'; '.join(outcome.notes)}" if outcome.notes else "")
        )
    if outcome.failed_items:
        return f"품목이 터졌다: {', '.join(outcome.failed_items)}"
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
    return parser


def format_summary(result: WalkResult) -> str:
    """걷기 결과를 사람이 읽을 줄로. **값을 새로 만들지 않는다.**"""
    lines = [
        f"범위      {result.start.isoformat()} ~ {result.end.isoformat()}",
        f"돈 날     {len(result.days)}일 · 휴장 {len(result.skipped_days)}일",
        f"판단      {dict(sorted(result.actions.items()))}",
        f"종료코드  {dict(sorted(result.end_codes.items()))}",
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
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        now=datetime.fromisoformat(args.now),
        max_consecutive_failures=args.max_consecutive_failures,
    )
    print(format_summary(result))
    return 0 if result.completed and not result.incidents else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
