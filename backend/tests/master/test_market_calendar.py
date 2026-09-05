"""개장 축 — **가락이 서는가** (`#303` 후속 · 2026-09-05).

🔴 **`holiday_calendar` 와 같은 표를 읽지만 다른 사실을 읽는다.**

```text
holiday_calendar   dt · holiday_nm   "공휴일인가"    마스터 문 앞 (#282 §A)
market_calendar    dt · is_open      "장이 서는가"   매입 회차일 봉투
```

★ 두 물음이 실제로 다른 날을 가리킨다 — 2026년 공휴일 22건 중 **14건이 `is_open=t`**
  (설날·추석 포함)이고, 공휴일이 아닌데 장이 안 서는 날이 **3일**이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import market_calendar
from app.master.execution_day import CalendarNotCovered
from app.master.market_calendar import MlMarketDays

_설날 = date(2026, 2, 16)  # 월요일 · 공휴일인데 장이 선다
_평일휴장 = date(2026, 1, 2)  # 금요일 · 공휴일이 아닌데 장이 안 선다
_토요일 = date(2026, 1, 3)  # 토요일 · 장이 선다


def _시장(rows: list[dict[str, Any]]) -> MlMarketDays:
    return MlMarketDays(read=lambda: rows)


def _행(day: date, is_open: bool) -> dict[str, Any]:
    return {"dt": day, "is_open": is_open}


# ── ① 판정 — `is_open` 그대로 ─────────────────────────────────────────────


def test_공휴일이라도_is_open_이면_장이_선다():
    """🔴 **설날·추석에도 가락이 선다.** 공휴일 이름으로 판정하면 여기서 틀린다."""
    assert _시장([_행(_설날, True)]).is_market_open(_설날) is True


def test_공휴일이_아니어도_is_open_이_거짓이면_안_선다():
    """🔴 `holiday_nm` 으로는 **원리상 못 잡는** 날이다 (`2026-01-02` · `02-19` · `07-08`)."""
    assert _시장([_행(_평일휴장, False)]).is_market_open(_평일휴장) is False


def test_토요일도_값이_정한다_요일이_아니다():
    """실 표에서 토요일은 3년간 개장 94 · 휴장 11 이다."""
    시장 = _시장([_행(_토요일, True), _행(date(2026, 3, 7), False)])

    assert 시장.is_market_open(_토요일) is True
    assert 시장.is_market_open(date(2026, 3, 7)) is False


def test_표에_없는_날은_답하지_않는다():
    """🔴 **없음과 안 함을 구분한다.** 안 덮는 날을 *"장이 선다"* 로 만들지 않는다."""
    시장 = _시장([_행(_설날, True)])

    with pytest.raises(CalendarNotCovered) as exc:
        시장.is_market_open(date(2026, 5, 5))
    assert "2026-05-05" in str(exc.value)
    assert "2026-02-16" in str(exc.value), "덮는 범위를 사유에 적어야 한다"


# ── ② 못 읽었을 때 ────────────────────────────────────────────────────────


def test_표를_못_읽으면_장이_선다고_하지_않는다():
    """🔴 못 읽은 것을 개장으로 만들면 **휴장일에 회차를 세우고 아무도 모른다.**"""

    def 터진다() -> list[dict[str, Any]]:
        raise RuntimeError("연결 없음")

    with pytest.raises(CalendarNotCovered) as exc:
        MlMarketDays(read=터진다).is_market_open(_설날)
    assert "개장 축이 없다" in str(exc.value)


def test_표가_비어_있으면_장이_선다고_하지_않는다():
    """★ 빈 표는 *"휴장이 하나도 없다"* 가 아니라 **달력이 안 심겼다**는 사실이다."""
    with pytest.raises(CalendarNotCovered):
        _시장([]).is_market_open(_설날)


def test_못_읽은_것은_캐시하지_않는다():
    """★ 실패까지 캐시하면 DB 가 잠깐 끊긴 뒤 **프로세스를 다시 띄울 때까지** 죽는다."""
    시도: list[int] = []

    def 처음만_터진다() -> list[dict[str, Any]]:
        시도.append(1)
        if len(시도) == 1:
            raise RuntimeError("연결 없음")
        return [_행(_설날, True)]

    시장 = MlMarketDays(read=처음만_터진다)
    with pytest.raises(CalendarNotCovered):
        시장.is_market_open(_설날)

    assert 시장.is_market_open(_설날) is True
    assert len(시도) == 2


def test_표를_한_번만_읽는다():
    """문 앞이 매 요청 부르는 자리라 캐시가 없으면 커넥션이 하나씩 더 열린다."""
    시도: list[int] = []

    def 센다() -> list[dict[str, Any]]:
        시도.append(1)
        return [_행(_설날, True), _행(_토요일, True)]

    시장 = MlMarketDays(read=센다)
    시장.is_market_open(_설날)
    시장.is_market_open(_토요일)

    assert len(시도) == 1


# ── ③ 조회 원문 — 안 쓰는 칸을 안 가져온다 ────────────────────────────────


def _잡은_질의(monkeypatch: pytest.MonkeyPatch) -> str:
    """조회를 가로채 **원문만** 본다. DB 는 안 부른다."""
    잡은질의: list[Any] = []

    monkeypatch.setattr(market_calendar, "get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr(
        market_calendar,
        "fetch_all",
        lambda query: (잡은질의.append(query), [])[1],
    )

    with pytest.raises(CalendarNotCovered):  # 빈 결과 — 질의만 보면 된다
        MlMarketDays().is_market_open(_설날)

    return str(잡은질의[0])


def test_조회가_뷰가_아니라_표를_가리킨다(monkeypatch: pytest.MonkeyPatch):
    """뷰는 `WHERE c.dt <= CURRENT_DATE` 로 잘려 있어 미래를 못 답한다 (`#298`)."""
    질의 = _잡은_질의(monkeypatch)

    assert "ml_calendar_days" in 질의, f"표를 안 읽는다: {질의}"
    assert "v_ml_batch_days" not in 질의, f"뷰로 되돌아갔다: {질의}"


def test_조회가_판정에_안_쓰는_칸을_가져오지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **`SELECT *` 를 안 쓴다.** 안 가져오면 나중에 누가 그 칸으로 판정하지 못한다.

    ⚠️ **`holiday_nm` 도 금지다.** 가져오면 *"공휴일이면 밀자"* 가 언젠가 여기로
      돌아오고, 그러면 첫 판이 틀린 그 자리로 되돌아간다.
    """
    질의 = _잡은_질의(monkeypatch)

    assert "is_open" in 질의, f"개장 여부를 안 가져온다: {질의}"
    for 금지 in ("holiday_nm", "is_survey", "status", "has_batch", "*"):
        assert 금지 not in 질의, f"판정에 안 쓰는 칸을 가져온다: {금지} — {질의}"


def test_두_모듈이_같은_표_이름을_쓴다():
    """★ ML 이 표 이름을 바꾸면 **한 자리만** 고치면 된다."""
    from app.master import holiday_calendar

    assert market_calendar.TABLE is holiday_calendar.TABLE


# ── ④ 프로세스 하나에 달력 하나 ───────────────────────────────────────────


def test_get_market_calendar_는_같은_것을_돌려준다():
    market_calendar.reset()
    try:
        assert market_calendar.get_market_calendar() is market_calendar.get_market_calendar()
    finally:
        market_calendar.reset()


def test_만드는_것은_안_터진다():
    """★ 표를 읽는 것은 첫 `is_market_open` 때다 — 문 앞에서 터지면 판단이 멈춘다."""
    market_calendar.reset()
    try:
        market_calendar.get_market_calendar()  # 예외가 안 나야 한다
    finally:
        market_calendar.reset()
