"""**UTC 서버에서도 KST 날짜가 나온다.**

🔴 **고정 시각을 오늘과 다른 날로 잡는다.** 처음에 `2026-09-08`(검사를 쓴 날)을
넣었더니 `date.today()` 로 갈아 끼운 변이가 **그날만 초록**이었다 — 주입한 값을
통째로 무시해도 우연히 같은 답이 나왔기 때문이다. 지나간 날로 두면 그 우연이 없다.

🔴 09:30 KST 는 **00:30 UTC** 다. `date.today()` 로 읽으면 서버가 UTC 일 때 날짜가
하루 밀리고, 개장 · 실행일 · 도착일이 전부 달력일로 도는 표에서 그날은 *"안 열린 날"*
이 된다.

★ **DB 도 실제 시계도 안 탄다.** `today_in_seoul` 의 `now` 자리에 고정 시각을 넣는다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.master.clock import SEOUL, seoul_now, today_in_seoul

_UTC = UTC


def _at(moment: datetime):
    """그 순간을 주는 함수. `now` 자리에 넣는다."""
    return lambda: moment


# ── UTC 로 도는 서버 ─────────────────────────────────────────────────────


def test_UTC_자정_직후여도_KST_날짜가_나온다():
    """🔴 **ML 배치 시각 그대로다.** 09:30 KST = 전날이 아니라 그날 00:30 UTC."""
    got = today_in_seoul(_at(datetime(2025, 3, 14, 0, 30, tzinfo=_UTC)))

    assert got == date(2025, 3, 14), "UTC 자정 직후를 전날로 읽었다"


def test_UTC_전날_밤이_KST_로는_오늘이다():
    """**여기서 갈린다.** 2026-09-07 23:00Z 는 서울에서 이미 2026-09-08 08:00 이다.

    ★ `date.today()` 를 쓰면 UTC 서버에서 `2026-09-07` 이 나온다 — 하루 밀린다.
    """
    got = today_in_seoul(_at(datetime(2025, 3, 13, 23, 0, tzinfo=_UTC)))

    assert got == date(2025, 3, 14), "UTC 밤 시각을 서울 날짜로 안 옮겼다"


def test_KST_자정_직전은_아직_그날이다():
    """반대쪽 끝도 본다 — 한쪽만 맞으면 오프셋을 반대로 더해도 통과한다."""
    got = today_in_seoul(_at(datetime(2025, 3, 14, 23, 59, tzinfo=SEOUL)))

    assert got == date(2025, 3, 14)


def test_KST_자정_직후는_다음_날이다():
    got = today_in_seoul(_at(datetime(2025, 3, 15, 0, 1, tzinfo=SEOUL)))

    assert got == date(2025, 3, 15)


# ── 기본값 ───────────────────────────────────────────────────────────────


def test_기본값이_None_이_아니라_실제_시계다():
    """⚠️ 이 저장소는 `None` 을 *"안 돌렸다"* 로 쓴다.

    `None` 을 기본값으로 두면 *"시계를 안 읽었다"* 와 *"기본 시계를 읽었다"* 가
    같은 값이 된다 (`verifier.py` 의 `critic=None` 이력).
    """
    import inspect

    default = inspect.signature(today_in_seoul).parameters["now"].default

    assert default is not None, "기본값이 None 이면 '안 돌렸다' 와 구분이 안 된다"
    assert default is seoul_now


def test_인자를_안_주면_실제_시계를_읽는다():
    """기본 경로가 안 막혔는지 본다 — 이게 없으면 위 검사들은 대역만 재고 끝난다."""
    got = today_in_seoul()
    now = seoul_now()

    assert got == now.date()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=9)


# ── 시간대 없는 시각 ─────────────────────────────────────────────────────


def test_시간대_없는_시각은_거절한다():
    """🔴 **naive datetime 을 서울로 가정하지 않는다.**

    가정하면 UTC 서버가 준 naive 시각이 서울 시각으로 읽혀 **9시간이 통째로 사라진다.**
    그것이 바로 이 파일이 막으려는 하루 밀림이다.
    """
    with pytest.raises(ValueError):
        today_in_seoul(_at(datetime(2025, 3, 14, 0, 30)))  # noqa: DTZ001 — 일부러 naive 다
