"""실행일 봉투 — **축은 `is_open` 이다** (`#303` 후속 · 2026-09-05).

🔴 **첫 판이 틀렸다.** 주말 + `holiday_nm` 으로 냈는데 2026년 전수에서 이렇게 나왔다.

```text
                      주말+holiday_nm      is_open
실제 못 사는 날 67일   맞힘 64               맞힘 67
살 수 있는데 민다      56일 🔴              0일
못 사는데 안 민다       3일 🔴              0일
```

★ 이 파일이 지키는 것은 넷이다.

  ```text
  ① 요일을 안 본다              토요일은 대부분 개장이다
  ② 공휴일 이름을 안 본다        설날·추석에도 가락이 선다
  ③ 지평이 목록과 한 덩어리다     "없다" 와 "모른다" 가 갈리게
  ④ 못 덮으면 예외를 올린다      조용히 개장으로 넘기지 않게
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


class _다_여는_시장:
    """365일 장이 서는 가짜. **요일을 안 본다는 것**을 재는 데 쓴다."""

    def is_market_open(self, day: date) -> bool:
        return True


class _닫는_날을_지정하는_시장:
    def __init__(self, *closed: date) -> None:
        self._closed = set(closed)

    def is_market_open(self, day: date) -> bool:
        return day not in self._closed


class _중간에_끊기는_시장:
    """`CUT` 부터는 표에 없다. **거짓을 안 돌려주고 못 봤다고 말한다.**"""

    CUT = date(2026, 1, 15)

    def is_market_open(self, day: date) -> bool:
        if day >= self.CUT:
            raise CalendarNotCovered(f"{day.isoformat()} 이 달력에 없다")
        return True


# ── ① 축 — 요일도 공휴일 이름도 안 본다 ────────────────────────────────────


def test_장이_다_서면_목록이_빈다_주말도_안_민다():
    """🔴 **이 파일의 주장이다.** 첫 판은 여기서 주말 여덟을 실었다.

    ★ 토요일은 2026년에 45일 개장한다 — 요일로 밀면 **살 수 있는 날에 못 산다고**
      계획한다.
    """
    envelope = build_execution_calendar(_월요일, market=_다_여는_시장())

    assert envelope.non_execution_days == ()


def test_토요일이_열려_있으면_안_민다():
    """실 표에서 토요일은 대부분 개장이다 (3년 개장 94 · 휴장 11)."""
    첫_토요일 = date(2026, 1, 10)
    envelope = build_execution_calendar(_월요일, market=_다_여는_시장())

    assert 첫_토요일 not in envelope.non_execution_days


def test_토요일이_닫혀_있으면_민다():
    """⚠️ **요일이 아니라 값이 정한다.** 2026년에 토요일 휴장이 7일 있다."""
    토요일_휴장 = date(2026, 3, 7)
    as_of = date(2026, 3, 2)  # 월요일
    envelope = build_execution_calendar(as_of, market=_닫는_날을_지정하는_시장(토요일_휴장))

    assert 토요일_휴장 in envelope.non_execution_days


def test_공휴일이라도_장이_서면_안_민다():
    """🔴 **설날·추석에도 가락이 선다.** 2026년 공휴일 22건 중 14건이 `is_open=t` 다."""
    설날 = date(2026, 2, 16)  # 월요일
    envelope = build_execution_calendar(date(2026, 2, 9), market=_다_여는_시장())

    assert 설날 not in envelope.non_execution_days


def test_공휴일이_아니어도_장이_안_서면_민다():
    """🔴 **`holiday_nm` 으로는 원리상 못 잡는 날이다.**

    `2026-01-02`(금) · `02-19`(목) · `07-08`(수) — 셋 다 `is_survey=t` 인데
    `is_open=f` 다. 첫 판이 놓친 3일이 이것이다.
    """
    평일_휴장 = date(2026, 1, 2)
    envelope = build_execution_calendar(date(2025, 12, 29), market=_닫는_날을_지정하는_시장(평일_휴장))

    assert 평일_휴장 in envelope.non_execution_days
    assert 평일_휴장.weekday() < 5, "평일인 것이 요점이다"


# ── ② 지평 ────────────────────────────────────────────────────────────────


def test_지평은_MAX_WALK_DAYS_다_새_상수를_안_만든다():
    """★ `18(coverage_days.max) + 5(최대 연휴) = 23 < 31` 이라 최악도 지평 안이다."""
    envelope = build_execution_calendar(_월요일, market=_다_여는_시장())

    assert envelope.horizon_end == _월요일 + timedelta(days=MAX_WALK_DAYS)


def test_지평_끝날도_포함한다():
    """`horizon_end` 는 **포함**이다. 하루가 조용히 빠지면 그 날만 안 밀린다."""
    끝날 = _월요일 + timedelta(days=MAX_WALK_DAYS)
    envelope = build_execution_calendar(_월요일, market=_닫는_날을_지정하는_시장(끝날))

    assert envelope.horizon_end == 끝날
    assert 끝날 in envelope.non_execution_days


def test_지평_밖은_묻지도_않는다():
    """지평 밖 하루를 닫아 놔도 목록에 안 들어간다 — **거기까지 안 걷는다.**"""
    지평_밖 = _월요일 + timedelta(days=MAX_WALK_DAYS + 1)
    envelope = build_execution_calendar(_월요일, market=_닫는_날을_지정하는_시장(지평_밖))

    assert 지평_밖 not in envelope.non_execution_days


def test_지평_밖은_covers_가_거짓이다():
    """🔴 *"목록에 없다"* 가 두 뜻이라, 가르는 칸이 봉투 안에 있어야 한다."""
    envelope = build_execution_calendar(_월요일, market=_다_여는_시장())

    assert envelope.covers(envelope.horizon_end)
    assert not envelope.covers(envelope.horizon_end + timedelta(days=1))


# ── ③ 모양 · 실패 ─────────────────────────────────────────────────────────


def test_as_of_자신도_판정한다():
    """★ 1회차 offset 이 0 이라 `as_of` 가 회차일이 된다.

    ⚠️ **마스터가 도는 날이라도 장이 안 설 수 있다** — `2026-01-02`(금)이 그 예다.
    """
    envelope = build_execution_calendar(_월요일, market=_닫는_날을_지정하는_시장(_월요일))

    assert _월요일 in envelope.non_execution_days


def test_목록이_오름차순이고_중복이_없다():
    """받는 쪽이 정렬을 다시 하지 않아도 된다."""
    시장 = _닫는_날을_지정하는_시장(date(2026, 1, 20), date(2026, 1, 10), date(2026, 2, 3))
    days = build_execution_calendar(_월요일, market=시장).non_execution_days

    assert list(days) == sorted(days)
    assert len(set(days)) == len(days)


def test_달력이_끊기면_예외를_그대로_올린다():
    """⚠️ **여기서 잡지 않는다.** 잡아서 개장으로 넘기면 *"달력이 끊긴 것"* 과
    *"장이 서는 것"* 이 같아진다 — 부르는 쪽이 정한다."""
    with pytest.raises(CalendarNotCovered):
        build_execution_calendar(_월요일, market=_중간에_끊기는_시장())


def test_market_은_기본값이_없다():
    """🔴 기본값이 있으면 *"장이 다 선다"* 로 조용히 돈다 — 못 사는 날에 회차를 세운다."""
    with pytest.raises(TypeError):
        build_execution_calendar(_월요일)  # type: ignore[call-arg]


def test_payload_는_날짜를_문자열로_편다():
    """봉투는 JSON 으로 오간다 — `cap_by_date` 키를 문자열로 강제하는 것과 같은 규율."""
    시장 = _닫는_날을_지정하는_시장(date(2026, 1, 20))
    payload = build_execution_calendar(_월요일, market=시장).as_payload()

    assert payload["horizon_end"] == "2026-02-05"
    assert payload["non_execution_days"] == ["2026-01-20"]
    # ★ 키가 둘뿐이다. 목록만 실리는 일도, 지평만 실리는 일도 없다.
    assert set(payload) == {"non_execution_days", "horizon_end"}
