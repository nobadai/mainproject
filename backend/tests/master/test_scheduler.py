"""**하루 스케줄러 — 깨어났을 때 무엇을 하고, 무엇을 안 하는가.**

🔴 **이 파일은 시각을 주입해서 전 구간을 돈다.** 09:29 · 09:30 · 10:29 · 10:30 ·
10:35 를 한 스위트 안에서 지난다. `plan_next_action` 이 순수 함수라 가능하고,
잠자는 루프였으면 여기서 한 시간을 기다리거나 아무것도 못 쟀을 것이다.

⚠️ **DB 를 안 탄다.** 달력 · 게이트 · 서비스 함수를 전부 대역으로 준다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from app.master import scheduler
from app.master.clock import SEOUL
from app.master.execution_day import CalendarNotCovered
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.scheduler import (
    DayRunOutcome,
    ScheduledAction,
    plan_next_action,
    run_scheduled_day,
    wake_up,
)

AS_OF = date(2026, 9, 8)
ITEMS = ("무", "배추", "양파")


def _at(hour: int, minute: int) -> datetime:
    """그날 서울 시각."""
    return datetime(AS_OF.year, AS_OF.month, AS_OF.day, hour, minute, tzinfo=SEOUL)


# ── 대역 ────────────────────────────────────────────────────────────────


class _Calendar:
    """개장 축 대역. `is_open` 이 `None` 이면 못 읽은 것으로 던진다."""

    def __init__(self, is_open: bool | None) -> None:
        self._is_open = is_open
        self.calls: list[date] = []

    def is_market_open(self, day: date) -> bool:
        self.calls.append(day)
        if self._is_open is None:
            raise CalendarNotCovered(f"{day} 이 달력에 없다")
        return self._is_open


def _one(item: str, readiness: str, grade: str | None) -> ItemForecastGate:
    return ItemForecastGate(item=item, as_of=AS_OF, readiness=readiness, grade=grade)  # type: ignore[arg-type]


def _gate(
    *,
    ready: tuple[str, ...] = (),
    not_yet: tuple[str, ...] = (),
    unreadable: tuple[str, ...] = (),
) -> DayForecastReadiness:
    """`day_forecast_readiness` 가 내는 모양 그대로 만든다. 접는 규칙은 그쪽 것을 쓴다."""
    gates = tuple(
        [_one(i, "READY", "MEASURED") for i in ready]
        + [_one(i, "NOT_YET", "MISSING") for i in not_yet]
        + [_one(i, "UNREADABLE", None) for i in unreadable]
    )
    if unreadable:
        fold = "UNREADABLE"
    elif ready and not not_yet:
        fold = "ALL_READY"
    elif ready:
        fold = "SOME_READY"
    else:
        fold = "NONE_READY"
    return DayForecastReadiness(as_of=AS_OF, readiness=fold, items=gates)  # type: ignore[arg-type]


ALL_READY = _gate(ready=ITEMS)
SOME_READY = _gate(ready=("배추",), not_yet=("무", "양파"))
NONE_READY = _gate(not_yet=ITEMS)
UNREADABLE = _gate(not_yet=("무", "양파"), unreadable=("배추",))


def _plan(
    *, now: datetime, is_open: bool | None = True, gate: DayForecastReadiness = ALL_READY
) -> ScheduledAction:
    return plan_next_action(now=now, as_of=AS_OF, calendar=_Calendar(is_open), gate_result=gate)


@dataclass
class _Out:
    """서비스 함수가 내는 값의 최소 모양. `status` 와 `reason` 만 본다."""

    status: str
    reason: str = ""


class _Spy:
    """서비스 함수 대역. **몇 번 불렸는지**를 센다."""

    def __init__(self, out: object = None, boom: Exception | None = None) -> None:
        self.out = out
        self.boom = boom
        self.calls: list[object] = []

    def __call__(self, arg, *args, **kwargs):
        self.calls.append(arg)
        if self.boom is not None:
            raise self.boom
        return self.out


class _Procure:
    """`run_procurement` 대역. **request_id 로 행을 센다** — DB 유일 인덱스 흉내다."""

    def __init__(self, boom_on: str | None = None) -> None:
        self.rows: dict[str, int] = {}
        self.requests: list[object] = []
        self.boom_on = boom_on

    def __call__(self, request, verifier=None):
        self.requests.append(request)
        if self.boom_on is not None and request.item == self.boom_on:
            raise RuntimeError(f"{request.item} 판단이 터졌다")
        # 🔴 같은 request_id 는 한 행이다 — `master_agent_runs_run_request_unique`.
        self.rows[request.request_id] = self.rows.get(request.request_id, 0) + 1
        return _Out(status="RAN")


def _procure_response(end_code: str = "E1_APPROVED"):
    class _R:
        pass

    r = _R()
    r.end_code = end_code  # type: ignore[attr-defined]
    return r


def _run(
    action: ScheduledAction, *, procure: object | None = None, **kwargs
) -> tuple[DayRunOutcome, _Procure]:
    procure_fn = _Procure() if procure is None else procure
    defaults = {
        "open_day_fn": _Spy(_Out("OPENED")),
        "receive_fn": _Spy(_Out("RECEIVED")),
        "collect_fn": _Spy(_Out("COLLECTED")),
        "procure_fn": procure_fn,
        "items": ITEMS,
    }
    defaults.update(kwargs)
    return run_scheduled_day(action, **defaults), procure_fn  # type: ignore[arg-type]


# ── 다섯 어휘가 각각 나온다 ────────────────────────────────────────────


def test_달력이_안_선다고_하면_NOT_A_MARKET_DAY():
    """★ 게이트만 봤으면 이 날도 `NONE_READY` 라 한 시간을 헛기다렸을 것이다."""
    action = _plan(now=_at(9, 30), is_open=False, gate=NONE_READY)

    assert action.action == "NOT_A_MARKET_DAY"
    assert action.retry_after is None


def test_달력을_못_읽으면_BLOCKED():
    """🔴 fail-closed. 못 읽은 것을 *"장이 선다"* 로도 *"안 선다"* 로도 안 만든다."""
    action = _plan(now=_at(9, 30), is_open=None, gate=ALL_READY)

    assert action.action == "BLOCKED"
    assert "달력" in action.reason


def test_ALL_READY_면_RUN_NOW():
    action = _plan(now=_at(9, 30), gate=ALL_READY)

    assert action.action == "RUN_NOW"
    assert action.ready_items == ITEMS


def test_SOME_READY_도_RUN_NOW_이고_빠진_품목이_남는다():
    """⚠️ 빠진 품목은 그 자리에서 `MISSING → E4` 로 정직하게 남는다. 무엇인지는 여기 있다."""
    action = _plan(now=_at(9, 30), gate=SOME_READY)

    assert action.action == "RUN_NOW"
    assert action.ready_items == ("배추",)
    assert action.not_yet_items == ("무", "양파")


def test_NONE_READY_이고_마감_전이면_WAIT():
    action = _plan(now=_at(9, 30), gate=NONE_READY)

    assert action.action == "WAIT"
    assert action.retry_after == scheduler.SCHEDULE_INTERVAL


def test_NONE_READY_이고_마감_뒤면_RUN_AND_RECORD():
    action = _plan(now=_at(10, 35), gate=NONE_READY)

    assert action.action == "RUN_AND_RECORD"


def test_게이트를_못_읽으면_BLOCKED():
    action = _plan(now=_at(9, 30), gate=UNREADABLE)

    assert action.action == "BLOCKED"
    assert "배추" in action.reason


# ── 마감 판정 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 30, "WAIT"),
        (9, 35, "WAIT"),
        (10, 25, "WAIT"),
        (10, 29, "WAIT"),
        (10, 30, "RUN_AND_RECORD"),  # 🔴 마감 시각 **자신**이 마감 뒤다
        (10, 35, "RUN_AND_RECORD"),
        (23, 59, "RUN_AND_RECORD"),
    ],
)
def test_마감_시각으로_WAIT_와_RUN_AND_RECORD_가_갈린다(hour, minute, expected):
    """★ 전 구간을 한 검사가 지난다 — `now` 를 주입할 수 있어서다."""
    assert _plan(now=_at(hour, minute), gate=NONE_READY).action == expected


def test_상태를_안_들고도_같은_답이_나온다():
    """★ 몇 번째 깨어남인지 안 센다. 같은 시각이면 몇 번을 물어도 같은 답이다."""
    answers = {_plan(now=_at(9, 40), gate=NONE_READY).action for _ in range(5)}

    assert answers == {"WAIT"}


def test_시간대_없는_시각은_거절한다():
    with pytest.raises(ValueError, match="시간대"):
        plan_next_action(
            # 🔴 시간대 없는 시각이 검사 대상이다 — 여기서만 일부러 만든다.
            now=datetime(2026, 9, 8, 10, 0),  # noqa: DTZ001
            as_of=AS_OF,
            calendar=_Calendar(True),
            gate_result=NONE_READY,
        )


# ── 🔴 접지 않는다 ─────────────────────────────────────────────────────


def test_BLOCKED_는_WAIT_로_안_접힌다():
    """🔴 접으면 DB 가 죽은 날 영원히 재시도하고 그 사실이 아무 데도 안 남는다."""
    for is_open, gate in ((None, ALL_READY), (True, UNREADABLE)):
        action = _plan(now=_at(9, 35), is_open=is_open, gate=gate)

        assert action.action == "BLOCKED"
        assert action.action != "WAIT"
        assert action.retry_after is None, "BLOCKED 에 재시도 간격이 붙으면 그것이 곧 WAIT 다"


def test_휴장일은_BLOCKED_가_아니다():
    """★ *"안 서는 날"* 과 *"못 읽은 날"* 도 다르다."""
    assert _plan(now=_at(9, 30), is_open=False, gate=NONE_READY).action == "NOT_A_MARKET_DAY"


def test_RUN_AND_RECORD_사유에_ML_배치가_없었다는_말이_들어간다():
    """★ 1년에 여섯 날 — 달력은 열렸는데 ML 배치가 없는 날을 나중에 눈에 띄게 한다."""
    action = _plan(now=_at(10, 40), gate=NONE_READY)

    assert "달력은 열렸는데 ML 배치가 없었다" in action.reason


# ── 🔴 WAIT 중에는 판단을 안 돌린다 ───────────────────────────────────


@pytest.mark.parametrize("action_name", ["WAIT", "NOT_A_MARKET_DAY", "BLOCKED"])
def test_안_도는_답이면_서비스_함수를_하나도_안_부른다(action_name):
    """🔴 `WAIT` 에서 돌리면 09:30~10:30 사이에 `E4_NOT_STARTED` 가 열두 건 쌓인다."""
    plans = {
        "WAIT": _plan(now=_at(9, 30), gate=NONE_READY),
        "NOT_A_MARKET_DAY": _plan(now=_at(9, 30), is_open=False, gate=NONE_READY),
        "BLOCKED": _plan(now=_at(9, 30), is_open=None, gate=ALL_READY),
    }
    opened = _Spy(_Out("OPENED"))
    received = _Spy(_Out("RECEIVED"))
    collected = _Spy(_Out("COLLECTED"))
    procure = _Procure()

    out = run_scheduled_day(
        plans[action_name],
        open_day_fn=opened,
        receive_fn=received,
        collect_fn=collected,
        procure_fn=procure,
        items=ITEMS,
    )

    assert out.action == action_name
    assert procure.requests == [], "판단을 돌렸다 — E4 가 쌓인다"
    assert opened.calls == [] and received.calls == [] and collected.calls == []
    assert out.day_open_status == "NOT_ATTEMPTED"


def test_열두_번_WAIT_해도_판단은_0회다():
    """★ 09:30 부터 5분 간격으로 마감 직전까지 — 실제로 깨어나는 만큼 돌려 본다."""
    procure = _Procure()
    moment = _at(9, 30)
    waits = 0
    while moment < scheduler.deadline_at(AS_OF):
        action = _plan(now=moment, gate=NONE_READY)
        run_scheduled_day(action, procure_fn=procure, items=ITEMS)
        waits += action.action == "WAIT"
        moment += scheduler.SCHEDULE_INTERVAL

    assert waits == 12
    assert procure.requests == []


# ── 실행 순서와 실패 규율 ───────────────────────────────────────────────


def test_순서는_개장_입고_수금_판단이다():
    order: list[str] = []

    def note(name, out):
        def _call(arg, *a, **k):
            order.append(name)
            return out

        return _call

    procure = _Procure()

    def procure_noted(request, verifier=None):
        order.append(f"판단:{request.item}")
        return procure(request)

    run_scheduled_day(
        _plan(now=_at(9, 30), gate=ALL_READY),
        open_day_fn=note("개장", _Out("OPENED")),
        receive_fn=note("입고", _Out("RECEIVED")),
        collect_fn=note("수금", _Out("COLLECTED")),
        procure_fn=procure_noted,
        items=ITEMS,
    )

    assert order == ["개장", "입고", "수금", "판단:무", "판단:배추", "판단:양파"]


def test_개장이_실패하면_그_뒤를_안_한다():
    """🔴 상태 행이 없으면 입고 · 수금 · 판단이 적을 자리가 없다."""
    received, collected = _Spy(_Out("RECEIVED")), _Spy(_Out("COLLECTED"))
    out, procure = _run(
        _plan(now=_at(9, 30), gate=ALL_READY),
        open_day_fn=_Spy(_Out("NOT_OPENED", "하루 넘김 미등록")),
        receive_fn=received,
        collect_fn=collected,
    )

    assert out.day_open_status == "NOT_OPENED"
    assert received.calls == [] and collected.calls == []
    assert procure.requests == []


def test_개장이_터져도_예외를_안_내보낸다():
    out, procure = _run(
        _plan(now=_at(9, 30), gate=ALL_READY),
        open_day_fn=_Spy(boom=RuntimeError("커넥션이 없다")),
    )

    assert out.day_open_status == "FAILED"
    assert procure.requests == []


def test_ALREADY_OPENED_는_통과다():
    """★ *"할 일이 없었다"* 이지 *"못 했다"* 가 아니다 — 같은 날 두 번째 깨어남이 여기다."""
    out, procure = _run(
        _plan(now=_at(9, 35), gate=ALL_READY),
        open_day_fn=_Spy(_Out("ALREADY_OPENED")),
    )

    assert out.day_open_status == "ALREADY_OPENED"
    assert len(procure.requests) == 3


def test_판단이_실패해도_개장을_안_되돌린다():
    """🔴 하루가 열린 것은 사실이고, 판단이 실패한 것은 별개 사실이다 (`#400`)."""
    undo = _Spy(_Out("OPENED"))

    out, _ = _run(
        _plan(now=_at(9, 30), gate=ALL_READY),
        open_day_fn=undo,
        procure=_Procure(boom_on="배추"),
    )

    assert out.day_open_status == "OPENED", "개장 결과가 판단 실패로 덮였다"
    assert len(undo.calls) == 1, "되돌리려고 개장을 다시 불렀다"


def test_한_품목이_터져도_나머지는_돈다():
    out, procure = _run(_plan(now=_at(9, 30), gate=ALL_READY), procure=_Procure(boom_on="배추"))

    assert out.failed_items == ("배추",)
    assert sorted(one.item for one in out.items if one.status == "RAN") == ["무", "양파"]
    assert len(procure.requests) == 3, "터진 뒤에 나머지를 안 불렀다"


def test_터진_품목이_결과에_남는다():
    out, _ = _run(_plan(now=_at(9, 30), gate=ALL_READY), procure=_Procure(boom_on="양파"))

    터진것 = next(one for one in out.items if one.item == "양파")
    assert 터진것.status == "FAILED"
    assert "판단이 터졌다" in 터진것.reason
    assert 터진것.end_code is None, "못 돈 실행에 종료 코드를 지어내면 안 된다"


def test_입고가_터져도_판단은_돈다():
    """★ 설계 지시 — 상태는 값으로 남고, 무엇을 더 할지는 결과를 보는 쪽이 정한다."""
    out, procure = _run(
        _plan(now=_at(9, 30), gate=ALL_READY),
        receive_fn=_Spy(boom=RuntimeError("물류가 죽었다")),
    )

    assert out.inbound_status == "FAILED"
    assert len(procure.requests) == 3


# ── 멱등 ────────────────────────────────────────────────────────────────


def test_같은_날_두_번_돌아도_행이_안_는다():
    """🔴 `request_id` 가 날짜와 품목만으로 정해져서 유일 인덱스가 두 번째를 잡는다."""
    procure = _Procure()

    _run(_plan(now=_at(9, 30), gate=ALL_READY), procure=procure)
    _run(_plan(now=_at(9, 35), gate=ALL_READY), procure=procure)

    assert len(procure.requests) == 6, "두 번 부르긴 했다"
    assert len(procure.rows) == 3, "행이 늘었다 — request_id 가 실행마다 갈렸다"
    assert sorted(procure.rows) == [
        "REQ-DAILY-20260908-무",
        "REQ-DAILY-20260908-배추",
        "REQ-DAILY-20260908-양파",
    ]


def test_request_id_에_시각이_안_들어간다():
    """★ 시각이 들어가면 같은 날 두 번째가 새 행이 된다."""
    첫번째 = scheduler.daily_request_id(AS_OF, "배추")
    두번째 = scheduler.daily_request_id(AS_OF, "배추")

    assert 첫번째 == 두번째 == "REQ-DAILY-20260908-배추"


def test_품목_목록을_다시_안_센다():
    """★ `commitment.ITEM_CODES` 하나가 주인이다."""
    from app.master.commitment import ITEM_CODES

    assert set(scheduler.scheduled_items()) == set(ITEM_CODES)


# ── 진입점 ──────────────────────────────────────────────────────────────


def test_wake_up_은_시계를_한_번만_읽는다():
    """★ 두 번 읽으면 자정을 넘기는 순간 `as_of` 와 마감 비교가 다른 날을 가리킨다."""
    reads = []

    def now():
        reads.append(1)
        return _at(9, 30)

    procure = _Procure()
    out = wake_up(
        now=now,
        calendar=lambda: _Calendar(True),
        readiness=lambda as_of: ALL_READY,
        open_day_fn=_Spy(_Out("OPENED")),
        receive_fn=_Spy(_Out("RECEIVED")),
        collect_fn=_Spy(_Out("COLLECTED")),
        procure_fn=procure,
    )

    assert len(reads) == 1
    assert out.as_of == AS_OF
    assert out.action == "RUN_NOW"


def test_wake_up_은_안_잔다():
    """🔴 데몬은 다음 판이다. 이 함수는 한 번 깨어난 것을 처리하고 바로 돌아온다."""
    started = datetime.now(SEOUL)
    wake_up(
        now=lambda: _at(9, 30),
        calendar=lambda: _Calendar(True),
        readiness=lambda as_of: NONE_READY,
        procure_fn=_Procure(),
    )

    assert datetime.now(SEOUL) - started < timedelta(seconds=2)
