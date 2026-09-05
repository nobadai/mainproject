"""실행일 봉투 — **매입에 달력을 값으로 싣는다** (2026-09-05).

★ 이 파일이 지키는 것은 셋이다.

  ```text
  ① 목록에 주말이 들어간다        매입이 weekday() 를 다시 갖지 않게
  ② 지평이 목록과 한 덩어리다      "없다" 와 "모른다" 가 갈리게
  ③ 못 덮으면 통째로 안 싣는다     반쪽 달력이 지평을 거짓말하지 않게
  ```
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.master.calendar_walk import MAX_WALK_DAYS
from app.master.execution_calendar import build_execution_calendar
from app.master.execution_day import CalendarNotCovered

# 2026-01-05 는 월요일이다 (관통에 쓴 날).
_월요일 = date(2026, 1, 5)


class _공휴일이_없는_달력:
    def is_holiday(self, day: date) -> bool:
        return False


class _설날이_있는_달력:
    """2026-02-17(화) 하나만 공휴일. 지평 안(월요일 + 43일... 이 아니라 43일 밖)이 아니라
    **지평 안**에 오게 고른 날이다 — 1/5 + 31 = 2/5 이므로 1월 안의 날을 쓴다."""

    HOLIDAY = date(2026, 1, 20)  # 화요일

    def is_holiday(self, day: date) -> bool:
        return day == self.HOLIDAY


class _중간에_끊기는_달력:
    """`CUT` 부터는 표에 없다. **거짓을 안 돌려주고 못 봤다고 말한다.**"""

    CUT = date(2026, 1, 15)

    def is_holiday(self, day: date) -> bool:
        if day >= self.CUT:
            raise CalendarNotCovered(f"{day.isoformat()} 이 달력에 없다")
        return False


def test_지평은_MAX_WALK_DAYS_다_새_상수를_안_만든다() -> None:
    """★ `18(coverage_days.max) + 5(최대 연휴) = 23 < 31` 이라 최악도 지평 안이다."""
    envelope = build_execution_calendar(_월요일, calendar=_공휴일이_없는_달력())

    assert envelope.horizon_end == _월요일 + timedelta(days=MAX_WALK_DAYS)


def test_주말이_목록에_들어간다() -> None:
    """🔴 **주말을 빼면 매입이 weekday() 를 다시 갖는다** — 판정이 두 곳이 된다."""
    envelope = build_execution_calendar(_월요일, calendar=_공휴일이_없는_달력())

    첫_토요일 = date(2026, 1, 10)
    첫_일요일 = date(2026, 1, 11)
    assert 첫_토요일 in envelope.non_execution_days
    assert 첫_일요일 in envelope.non_execution_days

    # ★ 지평(1/5 ~ 2/5) 안의 토·일이 **하나도 안 빠진다.**
    주말_전부 = {
        _월요일 + timedelta(days=n)
        for n in range(MAX_WALK_DAYS + 1)
        if (_월요일 + timedelta(days=n)).weekday() >= 5
    }
    assert 주말_전부 <= set(envelope.non_execution_days)
    # 월요일 시작 32일(양 끝 포함) = 4주 + 4일 → 토·일 여덟 (2/7·2/8 은 지평 밖)
    assert len(주말_전부) == 8


def test_공휴일도_같은_목록에_들어간다() -> None:
    """평일인데 안 도는 날은 공휴일뿐이다. **주말과 한 목록**이라 받는 쪽이 안 가른다."""
    envelope = build_execution_calendar(_월요일, calendar=_설날이_있는_달력())

    assert _설날이_있는_달력.HOLIDAY in envelope.non_execution_days
    assert _설날이_있는_달력.HOLIDAY.weekday() < 5  # 평일인 것이 요점이다


def test_달력이_없으면_주말만_걸린다() -> None:
    """`is_execution_day` 의 기본값 그대로다 — 오늘까지의 동작."""
    envelope = build_execution_calendar(_월요일)

    assert all(day.weekday() >= 5 for day in envelope.non_execution_days)


def test_목록이_오름차순이고_중복이_없다() -> None:
    """받는 쪽이 정렬을 다시 하지 않아도 된다."""
    days = build_execution_calendar(_월요일, calendar=_설날이_있는_달력()).non_execution_days

    assert list(days) == sorted(days)
    assert len(set(days)) == len(days)


def test_as_of_자신도_판정한다() -> None:
    """★ 1회차 offset 이 0 이라 `as_of` 가 회차일이 된다. 빼 두면 그 날만 판정이 두 곳."""
    토요일 = date(2026, 1, 10)
    envelope = build_execution_calendar(토요일, calendar=_공휴일이_없는_달력())

    assert 토요일 in envelope.non_execution_days


def test_지평_끝날도_포함한다() -> None:
    """`horizon_end` 는 **포함**이다. 하루가 조용히 빠지면 그 날만 안 밀린다."""
    # 지평 끝이 주말이 되도록 as_of 를 고른다: 1/9(금) + 31 = 2/9(월) 이라
    # 끝이 평일이다. 하루 당겨 1/8(목) + 31 = 2/8(일).
    목요일 = date(2026, 1, 8)
    envelope = build_execution_calendar(목요일, calendar=_공휴일이_없는_달력())

    assert envelope.horizon_end == date(2026, 2, 8)
    assert envelope.horizon_end.weekday() == 6  # 일요일
    assert envelope.horizon_end in envelope.non_execution_days


def test_지평_밖은_covers_가_거짓이다() -> None:
    """🔴 *"목록에 없다"* 가 두 뜻이라, 가르는 칸이 봉투 안에 있어야 한다."""
    envelope = build_execution_calendar(_월요일, calendar=_공휴일이_없는_달력())

    assert envelope.covers(envelope.horizon_end)
    assert not envelope.covers(envelope.horizon_end + timedelta(days=1))


def test_달력이_끊기면_예외를_그대로_올린다() -> None:
    """⚠️ **여기서 잡지 않는다.** 잡아서 영업일로 넘기면 *"달력이 끊긴 것"* 과
    *"영업일인 것"* 이 같아진다 — 부르는 쪽이 정한다."""
    with pytest.raises(CalendarNotCovered):
        build_execution_calendar(_월요일, calendar=_중간에_끊기는_달력())


def test_payload_는_날짜를_문자열로_편다() -> None:
    """봉투는 JSON 으로 오간다 — `cap_by_date` 키를 문자열로 강제하는 것과 같은 규율."""
    payload = build_execution_calendar(_월요일, calendar=_설날이_있는_달력()).as_payload()

    assert payload["horizon_end"] == "2026-02-05"
    assert "2026-01-20" in payload["non_execution_days"]
    assert all(isinstance(day, str) for day in payload["non_execution_days"])
    # ★ 키가 둘뿐이다. 목록만 실리는 일도, 지평만 실리는 일도 없다.
    assert set(payload) == {"non_execution_days", "horizon_end"}
