"""**범위 걷기 — 어느 날을 걷고, 어느 날을 건너뛰고, 어디서 멈추는가.**

🔴 **DB 를 안 탄다.** 달력 · 게이트 · 하루 실행을 전부 대역으로 준다. 그래서 이
검사는 *"걷기가 어떻게 도는가"* 만 재고, 하루 실행이 무엇을 하는지는
`test_scheduler.py` 가 잰다.

⚠️ **실제로 걷지 않는다.** 이 판은 도구를 세우는 것까지다 — 179 영업일 성적은
  도구가 선 다음 판이고, 여기서 그것을 흉내 내면 대역이 낸 숫자가 성적처럼 보인다.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.master import backtest_runner
from app.master.backtest_runner import WalkResult, format_summary, walk
from app.master.clock import SEOUL
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.scheduler import DayRunOutcome, ItemRunOutcome

ITEMS = ("무", "배추", "양파")

#: 걷기가 쓸 시각. **마감(10:30) 뒤로 둔다** — 예측이 안 온 날을 `RUN_AND_RECORD`
#: 로 만들어, 걷기가 `WAIT` 에 걸려 아무것도 안 도는 상태를 피한다.
AFTER_DEADLINE = datetime(2026, 2, 7, 10, 35, tzinfo=SEOUL)


# ── 대역 ────────────────────────────────────────────────────────────────


class _Calendar:
    """개장 축 대역. **날짜별로 답을 정해 준다.**

    ```text
    True   장이 선다
    False  안 선다
    None   못 읽었다 → CalendarNotCovered
    ```
    """

    def __init__(self, answers: dict[date, bool | None], *, default: bool | None = True) -> None:
        self._answers = answers
        self._default = default
        self.calls: list[date] = []

    def is_market_open(self, day: date) -> bool:
        self.calls.append(day)
        answer = self._answers.get(day, self._default)
        if answer is None:
            raise CalendarNotCovered(f"{day} 이 달력에 없다")
        return answer


def _gate(as_of: date) -> DayForecastReadiness:
    """`ALL_READY`. 게이트 자체는 `test_forecast_gate.py` 가 잰다."""
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",
        items=tuple(
            ItemForecastGate(item=i, as_of=as_of, readiness="READY", grade="MEASURED")
            for i in ITEMS
        ),
    )


#: 출고가 터진 날의 사유. **`ship_due_sales` 가 실제로 내는 문장이다.**
OUTBOUND_FAILED_REASON = "나갈 것을 못 읽었다: connection refused"


def _ran(
    as_of: date,
    *,
    end_code: str = "E1_OK",
    outbound: str = "NOTHING_DUE",
) -> DayRunOutcome:
    """정상적으로 끝까지 돈 하루.

    ⚠️ **출고 기본이 `NOTHING_DUE` 다.** 판단이 돈 날은 출고 단계까지 갔다는 뜻이고
      (`run_scheduled_day` 의 순서), 그런 날에 `NOT_ATTEMPTED` 를 두면 실제로는 못
      나오는 짝을 대역이 만들어 낸다.
    """
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="예측이 왔다",
        day_open_status="OPENED",
        inbound_status="NOTHING_DUE",
        collection_status="NOTHING_DUE",
        procurement_status="RAN",
        outbound_status=outbound,
        items=tuple(
            ItemRunOutcome(item=i, request_id=f"REQ-{i}", status="RAN", end_code=end_code)
            for i in ITEMS
        ),
        # ★ `_stage` 가 `OutboundOut.reason` 을 실어 note 로 넘기는 그 모양 그대로.
        notes=(f"출고: {outbound} {OUTBOUND_FAILED_REASON}".strip(),)
        if outbound == "FAILED"
        else (f"출고: {outbound}",),
    )


def _ledger_gap(as_of: date) -> DayRunOutcome:
    """장부 관문이 돌아선 하루. **판단 단계를 안 탔다.**

    ★ 그날은 출고 단계에 **오지도 않는다** — `outbound_status` 는 기본값
      `NOT_ATTEMPTED` 그대로다.
    """
    return DayRunOutcome(
        as_of=as_of,
        action="RUN_NOW",
        reason="예측이 왔다",
        day_open_status="OPENED",
        inbound_status="BLOCKED",
        collection_status="NOTHING_DUE",
        notes=("장부가 안 서서 판단을 안 돌린다 (입고: BLOCKED · 수금: NOTHING_DUE)",),
    )


class _RunDay:
    """하루 실행 대역. **부른 날을 세고, 정해 준 날에는 터진다.**"""

    def __init__(
        self,
        *,
        boom_on: frozenset[date] = frozenset(),
        gap_on: frozenset[date] = frozenset(),
        outbound_on: Mapping[date, str] | None = None,
    ) -> None:
        self.boom_on = boom_on
        self.gap_on = gap_on
        self.outbound_on = {} if outbound_on is None else dict(outbound_on)
        self.calls: list[date] = []

    def __call__(self, action, **kwargs) -> DayRunOutcome:
        self.calls.append(action.as_of)
        if action.as_of in self.boom_on:
            raise RuntimeError("장부가 깨졌다")
        if action.as_of in self.gap_on:
            return _ledger_gap(action.as_of)
        if action.as_of in self.outbound_on:
            return _ran(action.as_of, outbound=self.outbound_on[action.as_of])
        return _ran(action.as_of)


def _walk(
    *,
    start: date,
    end: date,
    calendar: _Calendar | None = None,
    run_day: _RunDay | None = None,
    now: datetime = AFTER_DEADLINE,
    max_consecutive_failures: int = 5,
) -> tuple[WalkResult, _RunDay]:
    runner = _RunDay() if run_day is None else run_day
    result = walk(
        start=start,
        end=end,
        now=now,
        calendar=lambda: _Calendar({}) if calendar is None else calendar,
        readiness=_gate,
        run_day_fn=runner,
        max_consecutive_failures=max_consecutive_failures,
        ticks=_Ticks(),
    )
    return result, runner


class _Ticks:
    """단조 시계 대역. **부를 때마다 1초씩 흐른다** — 고정값이라 검사가 흔들리지 않는다."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


# ── ① 달력 ──────────────────────────────────────────────────────────────


def test_안_서는_날은_건너뛴다():
    """🔴 **휴장일에는 하루 실행을 안 부른다.**

    ⚠️ 부르면 `NOT_A_MARKET_DAY` 가 나와 아무 일도 안 나지만, 성적표에서 그 날이
      *"돈 날"* 로 섞인다 — 179일 중 몇 날을 실제로 돌았는지가 안 세진다.
    """
    쉬는날 = date(2026, 2, 8)
    result, runner = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 9),
        calendar=_Calendar({쉬는날: False}),
    )

    assert result.skipped_days == (쉬는날,), f"휴장일이 안 남았다: {result.skipped_days}"
    assert 쉬는날 not in runner.calls, "휴장일에 하루 실행을 불렀다"
    assert runner.calls == [date(2026, 2, 7), date(2026, 2, 9)]


def test_달력을_못_읽으면_멈추고_사유를_낸다():
    """🔴 **건너뛰지 않는다.** 모르는 날을 안 서는 날로 바꾸면 분모가 조용히 준다.

    ★ `CalendarNotCovered` 는 *"장이 서는지 아닌지를 이 표로는 말할 수 없다"* 는
      뜻이다. 그 날을 건너뛰면 그 문장을 *"안 선다"* 로 읽는 것이다.
    """
    모르는날 = date(2026, 2, 9)
    result, runner = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 12),
        calendar=_Calendar({모르는날: None}),
    )

    assert result.stopped_at == 모르는날, f"멈춘 날이 다르다: {result.stopped_at}"
    assert result.stopped_reason is not None and "달력" in result.stopped_reason
    assert not result.completed
    assert 모르는날 not in result.skipped_days, "못 읽은 날을 휴장일로 셌다"
    assert runner.calls == [date(2026, 2, 7), date(2026, 2, 8)], (
        f"멈춘 뒤로도 걸었다: {runner.calls}"
    )


# ── ② 실패 규율 ─────────────────────────────────────────────────────────


def test_하루가_터져도_다음_날을_계속_걷는다():
    """🔴 **한 날이 걷기를 세우지 않는다.** 터진 날은 사고로 남고 걸음은 이어진다."""
    터진날 = date(2026, 2, 8)
    result, runner = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(boom_on=frozenset({터진날})),
    )

    assert runner.calls == [date(2026, 2, d) for d in (7, 8, 9, 10)], (
        f"터진 날에서 멈췄다: {runner.calls}"
    )
    assert result.completed, f"끝까지 안 걸었다: {result.stopped_reason}"
    assert [one.as_of for one in result.incidents] == [터진날]
    assert "RuntimeError" in result.incidents[0].reason


def test_연속_사고가_상한에_닿으면_멈춘다():
    """⚠️ **상한이 없으면 사고 목록만 길어지고 아무도 안 읽는다.**

    ★ 장부가 깨진 채로 걸으면 179줄이 나오는데 그것은 **한 가지 사실**이다 —
      첫날 깨진 것이 안 고쳐졌다는 것.
    """
    깨진날들 = frozenset(date(2026, 2, d) for d in range(8, 20))
    result, runner = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 28),
        run_day=_RunDay(boom_on=깨진날들),
        max_consecutive_failures=3,
    )

    assert result.stopped_at == date(2026, 2, 10), f"상한에서 안 멈췄다: {result.stopped_at}"
    assert result.stopped_reason is not None and "연속" in result.stopped_reason
    assert len(result.incidents) == 3
    assert runner.calls[-1] == date(2026, 2, 10), f"멈춘 뒤로도 걸었다: {runner.calls[-1]}"


def test_정상인_날이_끼면_연속이_끊긴다():
    """★ **연속 사고 상한은 연속을 센다.** 흩어진 사고는 걷기를 안 세운다."""
    result, runner = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 16),
        run_day=_RunDay(boom_on=frozenset(date(2026, 2, d) for d in (8, 10, 12, 14))),
        max_consecutive_failures=3,
    )

    assert result.completed, f"흩어진 사고에 멈췄다: {result.stopped_reason}"
    assert len(result.incidents) == 4
    assert len(runner.calls) == 10


def test_판단_단계를_안_탄_날은_사고다():
    """🔴 **장부 관문이 돌아선 날은 조용히 지나가면 안 된다.**

    ⚠️ 그 날은 예외도 안 나고 품목도 안 터진다 — `procurement_status` 하나만
      *"안 탔다"* 고 말한다. 그것을 안 세면 **아무것도 안 산 날이 정상으로 보인다.**
    """
    막힌날 = date(2026, 2, 9)
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(gap_on=frozenset({막힌날})),
    )

    assert [one.as_of for one in result.incidents] == [막힌날]
    assert "BLOCKED" in result.incidents[0].reason, result.incidents[0].reason
    assert result.completed


# ── ③ 무엇을 부르는가 ───────────────────────────────────────────────────


def test_하루_실행을_부른다():
    """🔴 **개장일마다 `run_scheduled_day` 를 부른다.**

    ⚠️ 저장소 밖 임시 스크립트는 `run_procurement` 을 직접 불렀다. 그래서 개장 ·
      입고 · 수금 · 장부 관문이 한 번도 안 돌았고, 거기서 나온 *"사고 0건"* 은
      **그 네 단계를 안 탄 채로** 나온 숫자였다.
    """
    result, runner = _walk(start=date(2026, 2, 7), end=date(2026, 2, 9))

    assert runner.calls == [date(2026, 2, d) for d in (7, 8, 9)]
    assert len(result.days) == 3
    assert dict(result.actions) == {"RUN_NOW": 3}


def test_기본값이_run_scheduled_day_자체다():
    """★ **`None` 이 아니다.** `None` 을 허용하면 *"안 줬다"* 와 *"기본을 줬다"* 가
    같은 값이 된다 (`clock.py` · `verifier.py` 와 같은 규율)."""
    from app.master.scheduler import run_scheduled_day

    default = inspect.signature(walk).parameters["run_day_fn"].default

    assert default is run_scheduled_day, f"기본 하루 실행이 다르다: {default}"


def test_모듈이_run_procurement_을_안_부른다():
    """🔴 **소스로 지킨다.** 대역을 꽂는 검사만으로는 기본 경로가 바뀐 것을 못 본다.

    ★ **자기 생존 검사를 같이 둔다** — 스캐너가 `run_scheduled_day` 를 실제로
      찾는지부터 본다. 안 그러면 스캐너가 망가져 0건을 세는 날 공짜 초록이 난다.
    """
    source = Path(backtest_runner.__file__).read_text(encoding="utf-8")
    names = {
        node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)
    } | _imported_names(source)

    assert "run_scheduled_day" in names, (
        "스캐너가 run_scheduled_day 를 못 찾았다 — 스캐너가 망가졌거나"
        " 걷기가 하루 실행을 안 부른다. 어느 쪽이든 아래 단언은 공짜 초록이다"
    )
    assert "run_procurement" not in names, (
        "걷기가 run_procurement 을 직접 부른다 —"
        " 그러면 개장 · 입고 · 수금 · 장부 관문을 통째로 건너뛴다"
    )


def _imported_names(source: str) -> set[str]:
    """그 파일이 들여온 이름들. **함수 안 임포트도 잡는다.**"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            found |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            found |= {(alias.asname or alias.name).split(".")[-1] for alias in node.names}
    return found


# ── ④ 시계 ──────────────────────────────────────────────────────────────


def test_now_를_인자로_받는다():
    """🔴 **걷기가 시계를 읽으면 성적이 돌린 시각에 끌려간다.**"""
    now_param = inspect.signature(walk).parameters["now"]

    assert now_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert now_param.default is inspect.Parameter.empty, (
        "now 에 기본값이 있다 — 안 주면 막아야 한다"
    )


def test_모듈이_시계를_안_읽는다():
    """🔴 `app/master/` 에서 벽시계를 읽는 자리는 `clock.py` 하나다.

    ⚠️ `clock.seoul_now` 를 부르는 것도 답이 아니다 — 그러면 걷기가 **오늘**을
      기준으로 마감을 재기 시작하고, 백테스트가 조용히 무효가 된다.
    """
    source = Path(backtest_runner.__file__).read_text(encoding="utf-8")
    called = {
        ast.unparse(node.func).split(".")[-1]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }

    assert not called & {"now", "today", "utcnow", "seoul_now", "today_in_seoul"}, (
        "걷기가 시계를 읽는다: "
        f"{sorted(called & {'now', 'today', 'utcnow', 'seoul_now', 'today_in_seoul'})}"
    )


def test_시간대_없는_시각은_막는다():
    """★ **조용히 서울로 바꾸지 않는다.** 어느 지역의 10:30 인지가 없으면 못 잰다."""
    with pytest.raises(ValueError, match="시간대"):
        # ⚠️ **tzinfo 를 일부러 안 준다** — 시간대 없는 시각을 막는지가 이 검사다.
        #   DTZ001 은 여기서만 끈다. 검사 대상이 곧 린터가 막으려는 그 모양이다.
        _walk(
            start=date(2026, 2, 7),
            end=date(2026, 2, 7),
            now=datetime(2026, 2, 7, 10, 35),  # noqa: DTZ001
        )


def test_그날의_마감과_비교한다():
    """🔴 **받은 시각을 179일에 그대로 쓰지 않는다.**

    ★ 그러면 첫날 말고는 전부 *"마감이 한참 지난 미래"* 가 되고, 마감 전인지
      뒤인지가 첫날에만 맞는다.
    """
    before = datetime(2026, 2, 7, 9, 40, tzinfo=SEOUL)
    seen: list[datetime] = []

    class _Spy(_RunDay):
        def __call__(self, action, **kwargs):
            seen.append(action.now)
            return super().__call__(action, **kwargs)

    _walk(start=date(2026, 2, 7), end=date(2026, 2, 9), run_day=_Spy(), now=before)

    assert [one.date() for one in seen] == [date(2026, 2, d) for d in (7, 8, 9)]
    assert {one.timetz() for one in seen} == {before.timetz()}


# ── ⑤ 범위와 산출 ───────────────────────────────────────────────────────


def test_거꾸로_된_범위는_막는다():
    """★ **여기서 바로잡지 않는다.** 어느 쪽이 시작인지는 부르는 쪽이 안다."""
    with pytest.raises(ValueError, match="거꾸로"):
        _walk(start=date(2026, 2, 10), end=date(2026, 2, 7))


def test_어휘를_새로_안_만든다():
    """★ **판단 분포는 `scheduler` 가 낸 값을 센다.** 새 이름을 붙이지 않는다."""
    result, _ = _walk(start=date(2026, 2, 7), end=date(2026, 2, 8))

    assert set(result.actions) <= {
        "RUN_NOW",
        "WAIT",
        "RUN_AND_RECORD",
        "NOT_A_MARKET_DAY",
        "BLOCKED",
    }
    assert set(result.end_codes) == {"E1_OK"}


def test_소요_시간을_잰다():
    """★ 단조 시계로 잰다 — 날짜도 시간대도 안 만든다."""
    result, _ = _walk(start=date(2026, 2, 7), end=date(2026, 2, 7))

    assert result.elapsed_seconds == pytest.approx(1.0)


def test_요약에_사고와_멈춘_사유가_남는다():
    """⚠️ **화면에 안 나오는 사고는 없는 사고와 같다.**"""
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 28),
        run_day=_RunDay(boom_on=frozenset(date(2026, 2, d) for d in range(7, 20))),
        max_consecutive_failures=2,
    )

    summary = format_summary(result)

    assert "사고      2건" in summary
    assert "멈춤" in summary
    assert "2026-02-08" in summary


# ── ⑥ 진입점 ────────────────────────────────────────────────────────────


def test_날짜를_안_주면_막는다():
    """⚠️ **기본 범위를 두면 그 범위가 곧 업무 규칙이 된다** — 아무도 정한 적이 없는데."""
    with pytest.raises(SystemExit):
        backtest_runner.main([])


def test_진입점이_walk_에_그대로_넘긴다():
    """★ **진입점에 로직이 없다.** 문자열을 날짜로 바꾸는 것뿐이다."""
    seen: dict[str, object] = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return WalkResult(start=kwargs["start"], end=kwargs["end"])

    original = backtest_runner.walk
    backtest_runner.walk = _fake  # type: ignore[assignment]
    try:
        code = backtest_runner.main(
            ["--start", "2026-02-07", "--end", "2026-09-07", "--now", "2026-09-07T10:35+09:00"]
        )
    finally:
        backtest_runner.walk = original  # type: ignore[assignment]

    assert seen["start"] == date(2026, 2, 7)
    assert seen["end"] == date(2026, 9, 7)
    assert seen["now"] == datetime.fromisoformat("2026-09-07T10:35+09:00")
    assert code == 0


def test_사고가_있으면_0_이_아니다():
    """🔴 **조용히 0 을 내지 않는다.** 자동화가 그 값을 본다."""

    def _fake(**kwargs):
        return WalkResult(
            start=kwargs["start"],
            end=kwargs["end"],
            incidents=(backtest_runner.WalkIncident(as_of=kwargs["start"], reason="터졌다"),),
        )

    original = backtest_runner.walk
    backtest_runner.walk = _fake  # type: ignore[assignment]
    try:
        code = backtest_runner.main(
            ["--start", "2026-02-07", "--end", "2026-02-07", "--now", "2026-02-07T10:35+09:00"]
        )
    finally:
        backtest_runner.walk = original  # type: ignore[assignment]

    assert code == 1


def test_상한을_안_주면_모듈_상수를_쓴다():
    """★ 수의 주인은 하나다 — 진입점이 다시 세지 않는다."""
    default = inspect.signature(walk).parameters["max_consecutive_failures"].default

    assert default == backtest_runner.MAX_CONSECUTIVE_FAILURES


def test_걷는_날_수가_범위와_맞는다():
    """★ 하루씩 걷는다 — 건너뛴 날과 돈 날을 합치면 범위다."""
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 13),
        calendar=_Calendar({date(2026, 2, 8): False, date(2026, 2, 11): False}),
    )

    assert len(result.days) + len(result.skipped_days) == 7
    assert result.completed


def test_하루가_터진_날도_걸은_날로_안_센다():
    """⚠️ **`days` 는 하루 실행이 값을 낸 날만 든다.** 터진 날은 사고 목록의 것이다.

    ★ 섞으면 *"돈 날"* 이 부풀고, 성적표의 분자가 조용히 커진다.
    """
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 9),
        run_day=_RunDay(boom_on=frozenset({date(2026, 2, 8)})),
    )

    assert [one.as_of for one in result.days] == [date(2026, 2, 7), date(2026, 2, 9)]
    assert [one.as_of for one in result.incidents] == [date(2026, 2, 8)]


def test_상한이_0_이면_막는다():
    """★ 1 보다 작으면 한 날도 못 걷는다 — 조용히 걷지 않는 것보다 막는 게 낫다."""
    with pytest.raises(ValueError, match="상한"):
        _walk(start=date(2026, 2, 7), end=date(2026, 2, 7), max_consecutive_failures=0)


def test_하루_경계를_안_넘긴다():
    """★ `end` 는 포함이고 그 다음 날은 안 걷는다."""
    result, runner = _walk(start=date(2026, 2, 7), end=date(2026, 2, 7))

    assert runner.calls == [date(2026, 2, 7)]
    assert result.end + timedelta(days=1) not in runner.calls


# ── ⑦ 출고 — **넷이 다 다른 사실이다** ──────────────────────────────────
#
# ```text
# RAN            나갔다
# NOTHING_DUE    나갈 것이 없었다        ← "없다"
# FAILED         나가려다 못 나갔다      ← "못 했다"
# NOT_ATTEMPTED  거기까지 못 갔다        ← "안 했다"
# ```
#
# 🔴 **성적표가 이 넷을 못 가르면 손익 곡선이 왜 평평한지 아무도 답할 수 없다.**


def _네_가지_출고() -> WalkResult:
    """네 값이 하루씩 나오는 걷기. **네 날이 다 다른 사실이다.**

    ★ `NOT_ATTEMPTED` 는 장부 관문이 돌아선 날로 만든다 — 지어낸 짝이 아니라
      `run_scheduled_day` 가 실제로 그렇게 내는 유일한 길이다.
    """
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(
            outbound_on={
                date(2026, 2, 7): "RAN",
                date(2026, 2, 8): "NOTHING_DUE",
                date(2026, 2, 9): "FAILED",
            },
            gap_on=frozenset({date(2026, 2, 10)}),
        ),
    )
    return result


def test_출고_네_값을_따로_센다():
    """🔴 **`RAN` 만 세고 나머지를 묶으면 안 된다.**

    ⚠️ 묶는 순간 *"나갈 것이 없어서 안 나갔다"* 와 *"나가려다 못 나갔다"* 가 같은
      칸에 들어가고, 성적표가 그 둘을 영영 구별 못 한다.
    """
    result = _네_가지_출고()

    assert dict(result.outbound_statuses) == {
        "RAN": 1,
        "NOTHING_DUE": 1,
        "FAILED": 1,
        "NOT_ATTEMPTED": 1,
    }, f"넷이 안 갈렸다: {dict(result.outbound_statuses)}"


def test_요약에_출고_집계가_찍힌다():
    """🔴 **화면에 안 나오는 값은 없는 값과 같다.** 넷이 성적표에서 다 달라야 한다."""
    summary = format_summary(_네_가지_출고())

    assert "출고      " in summary, f"요약에 출고 줄이 없다:\n{summary}"
    출고줄 = next(line for line in summary.splitlines() if line.startswith("출고"))
    for 값 in ("RAN", "NOTHING_DUE", "FAILED", "NOT_ATTEMPTED"):
        assert f"'{값}': 1" in 출고줄, f"{값} 가 출고 줄에 없다: {출고줄}"


def test_집계를_비우면_요약이_빈다():
    """★ **자기 생존.** 위 검사가 집계를 실제로 읽는지부터 잰다.

    ⚠️ 요약이 집계를 안 읽고 어딘가에서 네 글자를 주워 오면 위 검사는 **고쳐서가
      아니라 우연히** 초록이 된다. 걸은 날이 없으면 출고 줄도 비어야 한다.
    """
    빈걷기 = WalkResult(start=date(2026, 2, 7), end=date(2026, 2, 7))

    assert dict(빈걷기.outbound_statuses) == {}
    assert "출고      {}" in format_summary(빈걷기)


def test_출고_집계를_format_summary_가_안_만든다():
    """🔴 **집계는 `WalkResult` 가 나르고 요약은 찍기만 한다** (그 함수 독스트링).

    ⚠️ 요약이 `days` 를 다시 훑으면 같은 사실의 주인이 둘이 되고, `end_codes` 처럼
      결과 객체만 보는 쪽에서는 그 집계를 못 읽는다.
    """
    source = inspect.getsource(format_summary)

    assert "outbound_statuses" in source, "요약이 출고 집계를 안 읽는다"
    assert "Counter" not in source, f"요약이 값을 새로 만든다:\n{source}"


def test_출고가_못_나간_날은_사고다():
    """🔴 **출고 실패가 조용하면 안 된다.**

    ⚠️ 출고는 판단 뒤 단계라, 판단이 돌면 그날은 `procurement_status == "RAN"` 이고
      품목도 안 터진다 — 출고를 안 보면 **그날이 사고 없음으로 지나간다.**
    """
    못나간날 = date(2026, 2, 9)
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(outbound_on={못나간날: "FAILED"}),
    )

    assert [one.as_of for one in result.incidents] == [못나간날], (
        f"출고 실패가 사고로 안 잡혔다: {[(i.as_of, i.reason) for i in result.incidents]}"
    )
    assert result.completed


def test_출고_사고_사유가_무엇이_못_나갔는지를_나른다():
    """⚠️ **사유를 지어내지 않는다.** `OutboundOut.reason` 이 `_stage` 의 note 로
    실려 오고, 걷기는 그 값을 그대로 옮긴다.

    ★ *"출고가 터졌다"* 만 적으면 사람이 연결을 볼지 조회를 볼지 모른 채 두 곳을
      다 뒤진다 — `_ledger_gap_note` 가 입고·수금을 둘 다 적는 것과 같은 이유다.
    """
    못나간날 = date(2026, 2, 9)
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(outbound_on={못나간날: "FAILED"}),
    )

    사유 = result.incidents[0].reason

    assert "출고" in 사유, 사유
    assert OUTBOUND_FAILED_REASON in 사유, f"못 나간 이유가 안 실렸다: {사유}"


def test_나갈_것이_없는_날은_사고가_아니다():
    """🟢 **`NOTHING_DUE` 는 정상이다.** *"없다"* 는 *"못 했다"* 가 아니다.

    🔴 예약이 아직 0행인 지금 이것을 사고로 세면 **매일이 사고**가 되고, 사고
      목록이 아무것도 안 가리킨다 (`OutboundOut` 이 `NOTHING_DUE` 를 따로 둔 이유).
    """
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(
            outbound_on={date(2026, 2, d): "NOTHING_DUE" for d in (7, 8, 9, 10)},
        ),
    )

    assert result.incidents == (), f"나갈 것이 없는 날을 사고로 셌다: {result.incidents}"
    assert dict(result.outbound_statuses) == {"NOTHING_DUE": 4}
    assert result.completed


def test_판단을_못_탄_날에_사고를_두_번_안_센다():
    """🔴 **한 사실에 사고가 둘이면 안 된다.**

    ⚠️ 장부 관문이 돌아선 날은 출고까지 못 간다 (`outbound_status` 가
      `NOT_ATTEMPTED`). 그 날을 출고로도 세면 사고 한 건이 두 건으로 부풀고, 연속
      사고 상한이 실제보다 빨리 닿아 걷기가 일찍 멈춘다.
    """
    막힌날 = date(2026, 2, 9)
    result, _ = _walk(
        start=date(2026, 2, 7),
        end=date(2026, 2, 10),
        run_day=_RunDay(gap_on=frozenset({막힌날})),
    )

    assert [one.as_of for one in result.incidents] == [막힌날], (
        f"사고가 두 번 세졌다: {[(i.as_of, i.reason) for i in result.incidents]}"
    )
    assert result.days[2].outbound_status == "NOT_ATTEMPTED", "대역이 그 짝을 안 만들었다"
    assert "판단 단계" in result.incidents[0].reason, result.incidents[0].reason
