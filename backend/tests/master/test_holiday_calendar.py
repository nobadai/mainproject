"""공휴일도 안 도는 날이다 — **달력을 주면 거른다. 안 주면 오늘 그대로다.**

2026-09-05 (`#282`).

🔴 `execution_day` 가 주말만 걸렀다. 그래서 이런 답이 나왔다.

```text
next_execution_day(2025-12-31) = 2026-01-01   신정이다
```

★ **이제 달력이 저장소에 있다.** `#264` 로 ML 이 `database/ml_calendar_days.sql` 을
  넣었고 실 DB 에 표와 뷰가 있다.

🔴 **읽는 것은 표다** (`#298`). 처음에는 `v_ml_batch_days` 를 읽었는데 그 뷰가
  `WHERE c.dt <= CURRENT_DATE` 로 잘려 **공휴일의 절반을 가렸다.** 실측(2026-09-05):

```text
haetdeul.ml_calendar_days   2025-09-04 ~ 2027-09-05   732행   공휴일 44일
haetdeul.v_ml_batch_days    2025-09-04 ~ 2026-09-05   367행   공휴일 22일
2026-01-01  holiday_nm = '1월1일'
```

★ **뷰가 틀린 것이 아니다.** `CURRENT_DATE` 컷은 배치 상태의 제약이고 공휴일 축이
  그것을 물려받았을 뿐이다. 공휴일의 주인은 표다.

🔴 **판정 근거는 `holiday_nm` 하나다.** `is_open` · `is_survey` · `status` ·
`has_batch` 는 안 본다.

```text
is_open      뜻이 흔들린다 — 실측 2026-01-02 는 is_open=False 인데 holiday_nm 이
             없고 배치는 있다. 2026-01-03(토)은 is_open=True 다
is_survey    조사일 축이다 — 예측 기준일이 거기서 나오지 공휴일이 거기서 나오지 않는다
has_batch    #242 가 이미 따로 본다 — 같은 사실을 두 곳에서 판정하지 않는다
```

⚠️ **이 파일은 DB 를 안 부른다.** 가짜 달력을 꽂아 돈다 — `execution_day` 를 순수하게
  유지한 이유가 그것이다.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.master import day_open, execution_day, holiday_calendar
from app.master.calendar_walk import MAX_WALK_DAYS
from app.master.execution_day import (
    CalendarNotCovered,
    ExecutionDayNotFound,
    is_execution_day,
    next_execution_day,
)
from app.master.holiday_calendar import MlCalendarDays

# ── 실측값 ─────────────────────────────────────────────────────────────────
#
# 아래 날짜와 이름은 전부 `haetdeul.ml_calendar_days` 실측이다 (2026-09-05 조회).
# 지어낸 달력이 아니라 **저 표가 실제로 들고 있는 값**이어야, 이 검사가 통과하는 것과
# 운영에서 도는 것이 같아진다.

_신정 = date(2026, 1, 1)  # 목 · holiday_nm = '1월1일'
_신정_전날 = date(2025, 12, 31)  # 수
_신정_다음_평일 = date(2026, 1, 2)  # 금 · holiday_nm 없음 (is_open=False 인데도)

#: 추석 연휴 실측. 목요일(10-02) 다음 실행일이 **여드레 뒤** 금요일(10-10)이다.
_추석_연휴 = {
    date(2025, 10, 3): "개천절",
    date(2025, 10, 5): "추석",
    date(2025, 10, 6): "추석",
    date(2025, 10, 7): "추석",
    date(2025, 10, 8): "대체공휴일",
    date(2025, 10, 9): "한글날",
}
_연휴_전날 = date(2025, 10, 2)  # 목
_연휴_다음_실행일 = date(2025, 10, 10)  # 금

#: 🔴 **뷰였으면 못 답하던 날들이다.** 표에는 있고 뷰에는 없다 (실측 2026-09-05).
_뷰_밖의_공휴일 = {
    date(2026, 9, 24): "추석",
    date(2026, 12, 25): "기독탄신일",
    date(2027, 1, 1): "1월1일",
}
#: 표에는 있는데 공휴일이 아닌 미래 날. 뷰였으면 이 날도 `CalendarNotCovered` 였다.
_뷰_밖의_평일 = date(2026, 9, 7)  # 월 · holiday_nm 없음
#: 표도 못 덮는 날. **범위가 넓어진 것이지 무한해진 것이 아니다.**
_표_밖의_날 = date(2028, 1, 3)

# 2026-09-07(월) ~ 2026-09-13(일). `test_execution_day.py` 와 같은 주다.
_MONDAY = date(2026, 9, 7)
_WEEK = [_MONDAY + timedelta(days=n) for n in range(7)]
_THURSDAY = date(2026, 9, 10)
_SATURDAY = date(2026, 9, 12)
_SUNDAY = date(2026, 9, 13)
_NEXT_MONDAY = date(2026, 9, 14)


class 가짜달력:
    """덮는 범위와 공휴일을 **따로** 든다. 범위 밖은 답하지 않는다.

    🔴 **없음과 안 함을 구분하려면 둘이 따로여야 한다.** 공휴일 집합 하나만 들면
      *"공휴일이 아니다"* 와 *"달력에 없다"* 가 같은 모양이 된다 — 이 파일이
      막으려는 바로 그 혼동이다.
    """

    def __init__(self, holidays: set[date], covers: tuple[date, date]) -> None:
        self.holidays = holidays
        self.covers = covers
        self.asked: list[date] = []

    def is_holiday(self, day: date) -> bool:
        self.asked.append(day)
        first, last = self.covers
        if day < first or day > last:
            raise CalendarNotCovered(f"{day.isoformat()} 이 달력에 없다 (가짜)")
        return day in self.holidays


class 죽은달력:
    """표를 못 읽는 달력. **어느 날을 물어도 못 봤다고 한다.**"""

    def is_holiday(self, day: date) -> bool:
        raise CalendarNotCovered("ml_calendar_days 를 못 읽었다 (가짜)")


def _신정을_아는_달력() -> 가짜달력:
    return 가짜달력({_신정}, (date(2025, 9, 4), date(2026, 9, 4)))


# ── ① calendar 를 안 주면 오늘과 똑같다 ────────────────────────────────────


def test_달력을_안_주면_주말만_거른다():
    """🔴 **기본값이 오늘 동작이다.** 이 판으로 깨지는 기존 호출이 하나도 없어야 한다."""
    verdict = {day: is_execution_day(day) for day in _WEEK}

    assert sum(verdict.values()) == 5
    assert not verdict[_SATURDAY] and not verdict[_SUNDAY]


def test_달력을_안_주면_신정도_실행일이다():
    """★ **공휴일 축이 없으면 없는 대로 답한다** — 이것이 `#282` 이전의 동작이다."""
    assert is_execution_day(_신정) is True, "달력을 안 줬는데 공휴일을 알아냈다"
    assert next_execution_day(_신정_전날) == _신정, (
        "달력을 안 줬는데 신정을 건너뛰었다 — 기본값이 오늘 동작이 아니다"
    )


def test_달력을_안_주면_달력에_묻지도_않는다():
    """★ 기본값이 *"항상 있는 달력"* 으로 바뀌면 여기가 먼저 운다 (변이 2)."""
    calendar = _신정을_아는_달력()

    is_execution_day(_신정)
    next_execution_day(_신정_전날)

    assert calendar.asked == [], f"달력을 안 줬는데 물어봤다: {calendar.asked}"


# ── ② calendar 를 주면 공휴일이 실행일이 아니다 ────────────────────────────


def test_달력을_주면_신정은_실행일이_아니다():
    calendar = _신정을_아는_달력()

    assert is_execution_day(_신정, calendar=calendar) is False, "신정에 판단을 돌린다"


def test_달력을_줘도_평일은_그대로_실행일이다():
    """★ 공휴일을 막느라 평일까지 막으면 아무 날도 안 돈다."""
    calendar = _신정을_아는_달력()

    assert is_execution_day(_신정_다음_평일, calendar=calendar) is True
    assert is_execution_day(_신정_전날, calendar=calendar) is True


def test_주말은_달력에_묻지_않고_거른다():
    """★ 답이 안 바뀌는데 묻기만 하면, 달력이 안 덮는 주말에 *"토요일인지도 모르겠다"*
    가 된다. 순수 함수로 답할 수 있는 것은 순수 함수가 답한다.
    """
    calendar = 가짜달력(set(), (_신정, _신정))  # 주말은 하나도 안 덮는다

    assert is_execution_day(_SATURDAY, calendar=calendar) is False
    assert calendar.asked == [], f"주말인데 달력에 물었다: {calendar.asked}"


# ── ③ 신정을 건너뛴다 ──────────────────────────────────────────────────────


def test_신정을_건너뛴다():
    """🔴 **이 파일의 첫 주장이다.** 전에는 2026-01-01 이 나왔다."""
    calendar = _신정을_아는_달력()

    following = next_execution_day(_신정_전날, calendar=calendar)

    assert following == _신정_다음_평일, f"신정을 실행일로 골랐다: {following}"


def test_신정_다음_평일을_is_open_때문에_거르지_않는다():
    """🔴 **판정 근거는 `holiday_nm` 하나다** (§A).

    실측에서 `2026-01-02` 는 `is_open=False` 인데 `holiday_nm` 이 없고 배치는 있다.
    `is_open` 으로 판정했다면 이 날이 실행일에서 빠졌을 것이다.
    """
    calendar = _신정을_아는_달력()

    assert is_execution_day(_신정_다음_평일, calendar=calendar) is True


# ── ④ 연휴가 이어져도 상한 안에서 멈춘다 ───────────────────────────────────


def test_추석_연휴_이레를_건너뛴다():
    """★ 실측 연휴다 — 개천절·주말·추석 사흘·대체공휴일·한글날이 이어진다."""
    calendar = 가짜달력(set(_추석_연휴), (date(2025, 9, 4), date(2026, 9, 4)))

    following = next_execution_day(_연휴_전날, calendar=calendar)

    assert following == _연휴_다음_실행일, f"연휴 안의 날을 골랐다: {following}"
    assert len(calendar.asked) <= MAX_WALK_DAYS, (
        f"상한보다 많이 걸었다: {len(calendar.asked)}걸음"
    )


def test_상한만큼만_걷는다():
    """🔴 **막는 것만으로는 부족하다 — 걸음 자체가 상한 안에서 끝나야 한다.**"""
    끝없는_연휴 = {_신정 + timedelta(days=n) for n in range(365)}
    calendar = 가짜달력(끝없는_연휴, (date(2025, 9, 4), date(2027, 9, 4)))

    with pytest.raises(ExecutionDayNotFound):
        next_execution_day(_신정_전날, calendar=calendar)

    assert len(calendar.asked) <= MAX_WALK_DAYS, (
        f"상한을 넘겨 걸었다: {len(calendar.asked)}걸음 (상한 {MAX_WALK_DAYS})"
    )


# ── ⑤ 상한을 넘으면 막고 사유를 낸다 ───────────────────────────────────────


def test_상한을_넘으면_사유를_낸다():
    끝없는_연휴 = {_신정 + timedelta(days=n) for n in range(365)}
    calendar = 가짜달력(끝없는_연휴, (date(2025, 9, 4), date(2027, 9, 4)))

    with pytest.raises(ExecutionDayNotFound) as caught:
        next_execution_day(_신정_전날, calendar=calendar)

    사유 = str(caught.value)
    assert str(MAX_WALK_DAYS) in 사유, f"상한을 사유에 안 적었다: {사유}"
    assert "달력" in 사유, f"왜 막혔는지 안 말한다: {사유}"


def test_상한은_하루_넘김과_같은_상수다():
    """🔴 **같은 수를 두 곳에 적으면 언젠가 한쪽만 바뀐다.**

    달력을 하루씩 걷는 자리가 둘이고(하루 넘김은 뒤로, 실행일은 앞으로) 멈추는
    이유가 같다. 이유와 수는 `calendar_walk.py` 에 한 번만 있다.
    """
    assert day_open.MAX_CARRY_DAYS == MAX_WALK_DAYS
    assert MAX_WALK_DAYS == 31, "상한이 바뀌었다면 두 자리의 사유 문장도 같이 봐야 한다"


# ── ⑥ 🔴 표에 없는 날짜를 조용히 평일로 넘기지 않는다 ──────────────────────


def test_달력_밖의_평일은_답하지_않는다():
    """🔴 **이 판의 핵심이다.**

    달력이 안 덮는 날을 *"평일"* 로 단정하면 **달력이 끊긴 것과 평일인 것이
    같아진다.** 그때부터 아무도 둘을 구분할 수 없다.
    """
    calendar = 가짜달력(set(), (_신정, _신정))  # 신정 하루만 덮는다
    달력_밖의_평일 = date(2026, 3, 4)  # 수요일

    with pytest.raises(CalendarNotCovered):
        is_execution_day(달력_밖의_평일, calendar=calendar)


def test_걷다가_달력_밖으로_나가면_답하지_않는다():
    calendar = 가짜달력({_신정}, (_신정_전날, _신정))  # 01-02 를 안 덮는다

    with pytest.raises(CalendarNotCovered):
        next_execution_day(_신정_전날, calendar=calendar)


def test_표가_안_덮는_날을_구현체도_평일로_안_넘긴다():
    """★ 표가 넓어져도 끝은 있다 — 범위 밖은 **덮는 범위를 사유에 적고** 답하지 않는다."""
    calendar = MlCalendarDays(
        read=lambda: [
            {"dt": _신정, "holiday_nm": "1월1일"},
            {"dt": _신정_다음_평일, "holiday_nm": None},
        ]
    )

    with pytest.raises(CalendarNotCovered) as caught:
        calendar.is_holiday(date(2026, 3, 4))

    사유 = str(caught.value)
    assert "2026-01-01~2026-01-02" in 사유, f"덮는 범위를 사유에 안 적었다: {사유}"


def test_표를_못_읽으면_공휴일이_없다고_하지_않는다():
    def 터진다() -> list[dict[str, Any]]:
        raise RuntimeError("connection refused")

    calendar = MlCalendarDays(read=터진다)

    with pytest.raises(CalendarNotCovered) as caught:
        calendar.is_holiday(_신정)

    assert "connection refused" in str(caught.value), "무엇 때문인지 안 말한다"


def test_표가_비어_있으면_공휴일이_없다고_하지_않는다():
    """★ 빈 표는 *"공휴일이 하나도 없다"* 가 아니라 **달력이 안 심겼다**는 사실이다."""
    calendar = MlCalendarDays(read=list)

    with pytest.raises(CalendarNotCovered):
        calendar.is_holiday(_신정)


# ── 구현체 — 무엇을 어떻게 읽는가 ──────────────────────────────────────────


def _표를_흉내낸_달력() -> MlCalendarDays:
    """실측 표의 모양 그대로다 — **오늘 이후가 들어 있다.** 뷰에는 없던 행들이다."""
    return MlCalendarDays(
        read=lambda: [
            {"dt": _신정, "holiday_nm": "1월1일"},
            {"dt": _신정_다음_평일, "holiday_nm": None},
            {"dt": _뷰_밖의_평일, "holiday_nm": None},
            *({"dt": d, "holiday_nm": nm} for d, nm in sorted(_뷰_밖의_공휴일.items())),
        ]
    )


def _잡은_질의(monkeypatch: pytest.MonkeyPatch) -> str:
    """조회를 가로채 **원문만** 본다. DB 는 안 부른다."""
    잡은질의: list[Any] = []

    monkeypatch.setattr(holiday_calendar, "get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr(
        holiday_calendar,
        "fetch_all",
        lambda query: (잡은질의.append(query), [])[1],
    )

    with pytest.raises(CalendarNotCovered):  # 빈 결과 — 질의만 보면 된다
        MlCalendarDays().is_holiday(_신정)

    return str(잡은질의[0])


def test_조회가_뷰가_아니라_표를_가리킨다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **이 판의 핵심이다** (`#298`).

    뷰는 `WHERE c.dt <= CURRENT_DATE` 로 잘려 있어 실측 공휴일 44일 중 22일만 보였다.
    그 컷은 **배치 상태**의 제약이지 달력의 제약이 아니다 — 공휴일의 주인은 표다.
    """
    질의 = _잡은_질의(monkeypatch)

    assert "ml_calendar_days" in 질의, f"표를 안 읽는다: {질의}"
    assert "v_ml_batch_days" not in 질의, f"뷰로 되돌아갔다 — 미래가 다시 잘린다: {질의}"


def test_조회가_판정에_안_쓰는_칸을_가져오지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **`SELECT *` 를 안 쓴다.** 안 가져오면 나중에 누가 그 칸으로 판정하지 못한다."""
    질의 = _잡은_질의(monkeypatch)

    assert "holiday_nm" in 질의, f"공휴일 이름을 안 가져온다: {질의}"
    for 금지 in ("is_open", "is_survey", "status", "has_batch", "*"):
        assert 금지 not in 질의, f"판정에 안 쓰는 칸을 가져온다: {금지} — {질의}"


def test_뷰였으면_못_답하던_미래를_답한다():
    """🔴 **뷰에 묶여 있을 때 아프던 자리다.**

    `2026-09-05`(토)에 이런 문장이 남았다 — *"다음 실행일을 찾다 달력 밖으로 나갔다
    — 2026-09-07 이 달력에 없다"*. 금요일 거절 문구가 *"다음 실행일은 월요일"* 이라
    하는데 **그 월요일이 공휴일이어도 몰랐다.**
    """
    calendar = _표를_흉내낸_달력()

    assert calendar.is_holiday(_뷰_밖의_평일) is False, "월요일을 이제도 못 본다"
    for day, name in _뷰_밖의_공휴일.items():
        assert calendar.is_holiday(day) is True, f"{day} {name} 을 못 본다"


def test_미래를_답해도_표_밖은_여전히_답하지_않는다():
    """★ **덮는 범위가 넓어진 것이지 무한해진 것이 아니다.**

    이 검사가 없으면 `#298` 이 *"이제 어느 날이든 답한다"* 로 읽힌다.
    """
    calendar = _표를_흉내낸_달력()

    with pytest.raises(CalendarNotCovered):
        calendar.is_holiday(_표_밖의_날)


def test_holiday_nm_하나로_판정한다():
    """★ 실측 그대로다. `is_open` · `is_survey` · `status` 를 같이 줘도 안 본다."""
    calendar = MlCalendarDays(
        read=lambda: [
            # 공휴일인데 경매는 선다 (실측 2025-10-03 개천절 · is_open=True)
            {"dt": date(2025, 10, 3), "holiday_nm": "개천절", "is_open": True},
            # 경매가 안 서는데 공휴일은 아니다 (실측 2026-01-02 · has_batch=True)
            {"dt": _신정_다음_평일, "holiday_nm": None, "is_open": False},
        ]
    )

    assert calendar.is_holiday(date(2025, 10, 3)) is True
    assert calendar.is_holiday(_신정_다음_평일) is False


def test_표를_한_번만_읽는다():
    """🔴 `is_execution_day` 는 문 앞에서 매 요청 불린다 — 캐시가 없으면 커넥션이 는다."""
    읽음: list[int] = []

    def 읽는다() -> list[dict[str, Any]]:
        읽음.append(1)
        return [{"dt": _신정, "holiday_nm": "1월1일"}]

    calendar = MlCalendarDays(read=읽는다)
    for _ in range(5):
        calendar.is_holiday(_신정)

    assert len(읽음) == 1, f"표를 {len(읽음)}번 읽었다"


def test_못_읽은_것은_캐시하지_않는다():
    """★ 실패까지 캐시하면 DB 가 잠깐 끊긴 뒤 **프로세스를 다시 띄울 때까지** 축이 죽는다."""
    시도: list[int] = []

    def 처음만_터진다() -> list[dict[str, Any]]:
        시도.append(1)
        if len(시도) == 1:
            raise RuntimeError("잠깐 끊겼다")
        return [{"dt": _신정, "holiday_nm": "1월1일"}]

    calendar = MlCalendarDays(read=처음만_터진다)
    with pytest.raises(CalendarNotCovered):
        calendar.is_holiday(_신정)

    assert calendar.is_holiday(_신정) is True, "한 번 실패했다고 축이 죽었다"


@pytest.mark.db
def test_실_DB_의_표가_미래_공휴일을_답한다():
    """🔴 **가짜를 안 꽂고 실제로 읽는다** (`#298` §D · `uv run pytest -m db`).

    위의 `test_뷰였으면_못_답하던_미래를_답한다` 는 가짜 조회라 **읽는 대상이
    바뀌어도 안 운다.** 대상이 바뀌었다는 사실은 여기가 잡는다.

    ⚠️ 기본 스위트에서는 빠진다 — 사내망 밖에서 스위트가 전원 빨간불이 되면 아무도
      스위트를 안 믿는다 (`pyproject.toml` 의 `db` 마커 주석).
    """
    holiday_calendar.reset()
    calendar = holiday_calendar.get_calendar()

    for day, name in _뷰_밖의_공휴일.items():
        assert calendar.is_holiday(day) is True, f"{day} {name} 을 못 본다"
    assert calendar.is_holiday(_뷰_밖의_평일) is False

    holiday_calendar.reset()


# ── ⑦ 달력을 못 읽어도 주말 판정은 계속 돈다 ───────────────────────────────


def _wire_all() -> list[str]:
    from app.master import wiring
    from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

    called: list[str] = []

    def port(request: AgentRequest):
        called.append(request.agent)
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        payload = {"scenarios": [{"scenario_id": "SCN-1"}]} if request.agent == "purchase" else {}
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload,
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    wiring.reset()
    for part in ("finance", "inventory", "purchase"):
        wiring.register(part, port)
    return called


@pytest.fixture
def 적재를_막는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: "RUN-FAKE-1")


def _run(as_of: date, request_id: str):
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    return run_procurement(
        ProcurementRunRequest(
            as_of=as_of, policy_version="v1.3", item="배추", request_id=request_id
        ),
        verifier=None,
    )


@pytest.fixture
def 달력이_죽는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.master.service.get_calendar", lambda: 죽은달력())


def test_달력이_죽어도_주말은_그대로_접힌다(적재를_막는다, 달력이_죽는다):
    """🔴 **달력이 죽었다고 매입 판단이 멈추면 안 된다.** 주말 축까지는 계속 돈다."""
    _wire_all()

    response = _run(_SATURDAY, "REQ-CAL-DEAD-1")

    assert response.end_code == "E4_NOT_STARTED"
    assert "실행일이 아니다" in response.reason
    assert _NEXT_MONDAY.isoformat() in response.reason, "다음 실행일을 못 골랐다"


def test_달력이_죽어도_평일은_그대로_돈다(적재를_막는다, 달력이_죽는다):
    called = _wire_all()

    response = _run(_THURSDAY, "REQ-CAL-DEAD-2")

    assert "purchase" in called, f"달력이 죽었다고 판단을 멈췄다: {called}"
    assert "실행일이 아니다" not in response.reason


def test_못_본_공휴일_축이_응답에_남는다(적재를_막는다, 달력이_죽는다):
    """🔴 **안 한 것을 안 했다고 적는다** (`verifier.py` 의 `skipped` 와 같은 규율).

    비워 두면 *"공휴일까지 보고 실행일이라고 했다"* 로 읽힌다.
    """
    _wire_all()

    response = _run(_THURSDAY, "REQ-CAL-DEAD-3")

    남은_말 = " / ".join(response.skipped_checks)
    assert "공휴일 축" in 남은_말, f"공휴일 축을 못 본 사실이 안 남았다: {response.skipped_checks}"


def test_달력이_살아_있으면_못_본_축이_없다(적재를_막는다):
    """★ 검사가 공허하지 않다는 증명 — 달력이 돌 때는 이 문장이 안 붙어야 한다."""
    _wire_all()

    response = _run(_THURSDAY, "REQ-CAL-OK-1")

    assert not [s for s in response.skipped_checks if "공휴일 축" in s], (
        f"달력이 도는데 못 봤다고 적는다: {response.skipped_checks}"
    )


def test_공휴일에는_부서를_한_번도_안_부른다(적재를_막는다, monkeypatch: pytest.MonkeyPatch):
    """★ 한 번이라도 부르면 그 회신이 이력에 남고 *"돌긴 돌았다"* 로 읽힌다."""
    monkeypatch.setattr("app.master.service.get_calendar", _신정을_아는_달력)
    called = _wire_all()

    response = _run(_신정, "REQ-NEWYEAR-1")

    assert called == [], f"신정인데 부서를 불렀다: {called}"
    assert response.end_code == "E4_NOT_STARTED"


def test_공휴일_사유가_주말이라고_말하지_않는다(적재를_막는다, monkeypatch: pytest.MonkeyPatch):
    """★ 설날을 *"주말이라"* 로 적으면 사유가 거짓말을 한다 — 사람이 달력을 다시 본다."""
    monkeypatch.setattr("app.master.service.get_calendar", _신정을_아는_달력)
    _wire_all()

    reason = _run(_신정, "REQ-NEWYEAR-2").reason

    assert "공휴일이라" in reason, f"왜 안 돌았는지 안 말한다: {reason}"
    assert "주말" not in reason, f"목요일을 주말이라고 한다: {reason}"
    assert _신정_다음_평일.isoformat() in reason, f"다음 실행일이 신정이다: {reason}"


# ── ⑧ 순수 유지 — execution_day 에 SQL 이 없다 ─────────────────────────────


def test_실행일_모듈에_SQL_이_없다():
    """🔴 **`execution_day` 는 DB 를 안 부른다.**

    ```text
    ① 검사가 DB 를 필요로 하게 된다 — 지금은 순수해서 가짜가 필요 없다
    ② 매입이 이 함수를 인용할 예정이다 — 매입은 봉투만 받는 파트라 DB 를 못 부른다
    ```
    """
    source = Path(execution_day.__file__).read_text(encoding="utf-8")
    code = ast.get_docstring(ast.parse(source))
    본문 = source.replace(code or "", "")  # 모듈 docstring 은 뷰 이름을 말할 수 있다

    for 금지 in ("SELECT", "fetch_all", "fetch_one", "get_connection", "psycopg"):
        assert 금지 not in 본문, f"실행일 모듈이 DB 를 안다: {금지}"
    assert "app.finance.db" not in source, "실행일 모듈이 DB 모듈을 임포트한다"


def test_실행일_모듈이_달력을_직접_만들지_않는다():
    """★ 포트만 연다. 실물을 여기서 만들면 순수함이 임포트 한 줄로 무너진다."""
    tree = ast.parse(inspect.getsource(execution_day))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "app.master.holiday_calendar" not in imported, (
        "실행일 모듈이 구현체를 임포트한다 — 화살표가 거꾸로다"
    )


def test_매입_배선이_별건이라고_적혀_있다():
    """⚠️ **이 판이 연 것은 포트까지다.** 봉투에 달력을 실을지는 정하지 않았다."""
    doc = execution_day.__doc__ or ""

    assert "매입" in doc and "별건" in doc, (
        "매입에 달력을 전달하는 것이 별건이라는 사실이 안 적혀 있다 —"
        " 다음 사람이 이 판에서 다 됐다고 읽는다"
    )
