"""걷기 성적표 — **「행이 없다」가 두 가지로 갈리는가** (`Master 19.0`).

이 파일이 잠그는 것은 하나다. 표는 없는 것을 말할 수 없고, 그래서 안 돈 날은
조회 결과에서 **그냥 없는 날**이다. 없는 날은 세어지지 않으므로 아무도
*"실행일인데 안 돌았다"* 를 셀 수 없었다.

```text
그날 행   실행일인가   판정
있음      ○           WALKED
없음      ✗           SKIPPED_OFF_DAY
없음      ○           NO_ROW_ON_EXECUTION_DAY   ← 이것을 셀 수 있게 되는 것이 이 판이다
있음      ✗           ROW_ON_OFF_DAY
```

★ **DB 를 안 세운다.** 집계 SQL 은 경계(`fetch_all`)에서 가로채 *"무엇을 물으려
  했는가"* 를 보고, 판정은 집계 함수를 갈아 끼워 **스스로 만든 데이터**로 잰다.
  표가 팀 공용이라 실측값을 기대값으로 박으면 남이 한 줄 돌릴 때 빨간불이 된다.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.master import persistence, run_repository
from app.master import walk_report as walk_report_module
from app.master.execution_day import CalendarNotCovered
from app.master.router import router
from app.master.run_repository import DayRunCount, count_runs_by_day
from app.master.walk_report import (
    NO_ROW_ON_EXECUTION_DAY,
    ROW_ON_OFF_DAY,
    SKIPPED_OFF_DAY,
    UNKNOWN_CALENDAR,
    WALKED,
    WalkReport,
    walk_report,
)

_SIM = "SIM-WALK-1"

# 2026-01-19(월) ~ 01-25(일). 토·일이 범위 안에 하나씩 들어 있다.
_MON = date(2026, 1, 19)
_TUE = date(2026, 1, 20)
_WED = date(2026, 1, 21)
_THU = date(2026, 1, 22)
_FRI = date(2026, 1, 23)
_SAT = date(2026, 1, 24)
_SUN = date(2026, 1, 25)


def _row(day: date, **kw) -> DayRunCount:
    base: dict = {
        "as_of": day,
        "runs": 1,
        "end_codes": {"E1_APPROVED": 1},
        "items": ("무",),
        "gate_blocked": False,
    }
    base.update(kw)
    return DayRunCount(**base)  # type: ignore[typeddict-item]


class _Calendar:
    """공휴일 축 가짜. `못_봄` 에 든 날은 `CalendarNotCovered` 를 던진다."""

    def __init__(self, 공휴일: set[date] = frozenset(), 못_봄: set[date] = frozenset()):
        self.공휴일 = set(공휴일)
        self.못_봄 = set(못_봄)

    def is_holiday(self, day: date) -> bool:
        if day in self.못_봄:
            raise CalendarNotCovered(f"달력이 {day.isoformat()} 를 안 덮는다")
        return day in self.공휴일


def _report(monkeypatch, rows: list[DayRunCount], **kw) -> WalkReport:
    """집계를 갈아 끼우고 성적표를 만든다. **판정만 잰다.**"""
    monkeypatch.setattr(walk_report_module, "count_runs_by_day", lambda **_: list(rows))
    base = {"sim_run_id": _SIM, "start": _MON, "end": _SUN}
    base.update(kw)
    return walk_report(**base)  # type: ignore[arg-type]


def _days_with(report: WalkReport, verdict: str) -> list[date]:
    """그 판정이 붙은 날들. **비면 빈 목록이다** — 이 함수가 자기 생존 검사의 축이다."""
    return [day.as_of for day in report.days if day.verdict == verdict]


# ── ① 네 판정이 각각 나오는가 ──────────────────────────────────────────────


@pytest.fixture
def 성적표(monkeypatch) -> WalkReport:
    """월 돌았고 · 화 안 돌았고 · 토 돌았고 · 일 안 돌았다."""
    return _report(monkeypatch, [_row(_MON), _row(_SAT)])


def test_실행일에_행이_있으면_돌았다(성적표):
    assert _days_with(성적표, WALKED) == [_MON]


def test_안_도는_날에_행이_없으면_공백이_아니다(성적표):
    """🟢 주말은 안 도는 것이 정상이다. 이것을 공백으로 세면 성적표가 늘 빨갛다."""
    assert _days_with(성적표, SKIPPED_OFF_DAY) == [_SUN]


def test_실행일인데_행이_없으면_그것만_따로_센다(성적표):
    """🔴 **이 판의 값이다.** 지금까지 아무도 이 날들을 셀 수 없었다."""
    assert _days_with(성적표, NO_ROW_ON_EXECUTION_DAY) == [_TUE, _WED, _THU, _FRI]


def test_안_도는_날에_행이_있으면_이름이_따로다(성적표):
    """🟡 사고가 아니다. 실측 `01-24`·`01-31` 이 그것이고 사유를 달고 서 있다."""
    assert _days_with(성적표, ROW_ON_OFF_DAY) == [_SAT]


def test_자기_생존_판정을_세는_눈이_0건에도_초록이_아니다(monkeypatch):
    """🔴 **위 넷이 무엇을 세든 통과하는 검사면 소용이 없다.**

    같은 `_days_with` 로 **안 나올 판정**을 물어 빈 목록이 나오는지 본다. 이것이
    빈 목록을 못 내면 위 넷은 아무것도 증명하지 않는다.
    """
    전부_돌았다 = _report(monkeypatch, [_row(d) for d in (_MON, _TUE, _WED, _THU, _FRI)])

    assert _days_with(전부_돌았다, NO_ROW_ON_EXECUTION_DAY) == []
    assert _days_with(전부_돌았다, ROW_ON_OFF_DAY) == []
    assert _days_with(전부_돌았다, WALKED) == [_MON, _TUE, _WED, _THU, _FRI]


def test_요약은_행_수가_아니라_날_수다(성적표):
    """★ 하루에 여러 품목을 돈 날을 여러 날로 세면 성적표가 부풀어 오른다."""
    assert 성적표.summary == {
        WALKED: 1,
        NO_ROW_ON_EXECUTION_DAY: 4,
        ROW_ON_OFF_DAY: 1,
        SKIPPED_OFF_DAY: 1,
    }
    assert sum(성적표.summary.values()) == len(성적표.days)


# ── ② 범위 ─────────────────────────────────────────────────────────────────


def test_범위_밖_날은_결과에_없다(성적표):
    """🔴 **없는 날을 지어내지 않는다.** 범위를 받아 계산하고 범위 밖은 말하지 않는다."""
    assert [day.as_of for day in 성적표.days] == [_MON, _TUE, _WED, _THU, _FRI, _SAT, _SUN]
    assert 성적표.start == _MON
    assert 성적표.end == _SUN


def test_행_없는_날도_한_줄이다(성적표):
    """🔴 빼 버리면 `NO_ROW_ON_EXECUTION_DAY` 가 통째로 사라진다."""
    빈_날 = [day for day in 성적표.days if day.runs == 0]
    assert [day.as_of for day in 빈_날] == [_TUE, _WED, _THU, _FRI, _SUN]


def test_거꾸로_된_범위는_거부한다(monkeypatch):
    monkeypatch.setattr(walk_report_module, "count_runs_by_day", lambda **_: [])
    with pytest.raises(ValueError):
        walk_report(sim_run_id=_SIM, start=_SUN, end=_MON)


# ── ③ 행 수를 뜻으로 바꾸지 않는다 ─────────────────────────────────────────


def test_행_수와_종료코드와_실행일_여부가_따로_나온다(monkeypatch):
    """⚠️ *"행이 4건이니 개장일이다"* 가 이 판을 만든 오독이다.

    🔴 **`end_code` 로 판정하지 않는다.** 토요일에 `E4_NOT_STARTED` 4건이 서 있어도
       판정은 `ROW_ON_OFF_DAY` 이고, 종료코드는 자기 칸에 그대로 남는다.
    """
    보고서 = _report(
        monkeypatch,
        [_row(_SAT, runs=4, end_codes={"E4_NOT_STARTED": 4}, items=())],
    )
    토요일 = next(day for day in 보고서.days if day.as_of == _SAT)

    assert 토요일.runs == 4
    assert 토요일.end_codes == {"E4_NOT_STARTED": 4}
    assert 토요일.is_execution_day is False
    assert 토요일.verdict == ROW_ON_OFF_DAY


def test_장부_관문_행이_잡힌다(monkeypatch):
    """🟢 `#465` 가 남기는 행 — 품목이 없고 `E4_NOT_STARTED` 다."""
    관문코드 = run_repository.LEDGER_GAP_END_CODE
    보고서 = _report(
        monkeypatch,
        [_row(_MON, end_codes={관문코드: 1}, items=(), gate_blocked=True)],
    )
    월요일 = next(day for day in 보고서.days if day.as_of == _MON)

    assert 월요일.gate_blocked is True
    assert 월요일.verdict == WALKED, "관문이 막은 날도 행은 있었다 — 안 돈 날이 아니다"
    assert [day.gate_blocked for day in 보고서.days if day.as_of != _MON] == [False] * 6


# ── ④ 달력 ─────────────────────────────────────────────────────────────────


def test_달력을_안_주면_모른다고_적는다(성적표):
    """🔴 안 적으면 설·추석이 `NO_ROW_ON_EXECUTION_DAY` 로 나와 **없는 공백**이 된다."""
    assert 성적표.holiday_calendar_used is False


def test_달력을_주면_공휴일이_안_도는_날이_된다(monkeypatch):
    보고서 = _report(monkeypatch, [_row(_MON)], calendar=_Calendar(공휴일={_WED}))

    assert 보고서.holiday_calendar_used is True
    assert _WED in _days_with(보고서, SKIPPED_OFF_DAY)
    assert _WED not in _days_with(보고서, NO_ROW_ON_EXECUTION_DAY)


def test_못_덮은_날만_모른다로_두고_나머지는_계속_낸다(monkeypatch):
    """⚠️ 하루 때문에 성적표 전체를 죽이지 않는다 — `service.py` 의 태도 그대로다.

    🔴 못 덮은 날을 평일로 단정하면 **달력이 끊긴 것과 실행일인 것이 같아진다.**
    """
    보고서 = _report(monkeypatch, [_row(_MON)], calendar=_Calendar(못_봄={_THU}))

    assert _days_with(보고서, UNKNOWN_CALENDAR) == [_THU]
    assert next(day for day in 보고서.days if day.as_of == _THU).is_execution_day is None
    assert _days_with(보고서, WALKED) == [_MON]
    assert _days_with(보고서, NO_ROW_ON_EXECUTION_DAY) == [_TUE, _WED, _FRI]


# ── ⑤ 축이 필수다 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("없음", ["", None])
def test_축_없이_성적표를_못_만든다(monkeypatch, 없음):
    """🔴 **`list_runs` 와 반대다.** 좁히지 않으면 손 호출 1,206행이 섞여 들어온다."""
    monkeypatch.setattr(run_repository, "fetch_all", lambda *a: [])
    with pytest.raises(ValueError):
        count_runs_by_day(sim_run_id=없음, start=_MON, end=_SUN)


def test_두_함수가_서로를_가리킨다():
    """★ 태도가 반대인 두 함수가 **왜 반대인지**를 서로 옆에 두고 적는다."""
    assert "count_runs_by_day" in (run_repository.list_runs.__doc__ or "")
    assert "list_runs" in (count_runs_by_day.__doc__ or "")


# ── ⑥ 집계 SQL ─────────────────────────────────────────────────────────────


def _asked(monkeypatch, **kw) -> tuple[str, tuple]:
    잡힘: dict = {}

    def _fake(query, params):
        잡힘["query"] = query.as_string(None)
        잡힘["params"] = params
        return []

    monkeypatch.setattr(run_repository, "fetch_all", _fake)
    base = {"sim_run_id": _SIM, "start": _MON, "end": _SUN}
    base.update(kw)
    count_runs_by_day(**base)
    return 잡힘["query"], 잡힘["params"]


def test_축으로_좁힌다(monkeypatch):
    query, params = _asked(monkeypatch)

    assert "sim_run_id = %s" in query, f"좁히는 조건이 없다: {query}"
    assert _SIM in params


def test_축이_널인_행은_안_섞인다(monkeypatch):
    """🔴 `= %s` 는 NULL 을 안 집는다. **그것이 사실이고 감추는 것이 아니다.**

    ⚠️ 실측 1,206행이 그 자리다. `IS NOT DISTINCT FROM` 이나 `COALESCE` 로 바꾸면
      손 호출과 옛 실험이 조용히 걷기 성적으로 세어진다.
    """
    query, _ = _asked(monkeypatch)

    assert "IS NOT DISTINCT FROM" not in query.upper()
    assert "COALESCE(SIM_RUN_ID" not in query.upper().replace(" ", "")


def test_범위를_SQL_이_건다(monkeypatch):
    query, params = _asked(monkeypatch)

    assert "as_of >= %s" in query and "as_of <= %s" in query
    assert _MON in params and _SUN in params


def test_상한을_두지_않는다(monkeypatch):
    """⚠️ `list_runs` 의 기본 50 으로는 200일 걷기를 못 읽는다. 여기는 집계다."""
    query, _ = _asked(monkeypatch)

    assert "LIMIT" not in query.upper()


def test_파이썬이_아니라_SQL_이_센다(monkeypatch):
    """⚠️ 행을 끌어와 세면 *"몇 행을 읽었나"* 와 *"몇 행이 있나"* 가 갈린다."""
    query, _ = _asked(monkeypatch)

    assert "COUNT(*)" in query.upper()
    assert "GROUP BY" in query.upper()


def test_관문_행을_업무_키로_되찾는다(monkeypatch):
    """🔴 **모양으로 되찾지 않는다** (2026-09-09).

    옛 판정은 `item IS NULL AND end_code = 'E4_NOT_STARTED'` 였는데, 그 모양은 관문
    행만의 것이 아니다 — 품목을 정하기 전에 죽은 옛 매입 실행 14행이 실측으로 같은
    모양이다. 축이 막고 있었을 뿐이다.

    ★ 적는 쪽과 되찾는 쪽이 같은 꼬리를 본다 — 두 벌이면 한쪽만 바뀌는 날이 온다.
    """
    query, params = _asked(monkeypatch)

    assert "request_id LIKE %s" in query, f"업무 키로 안 묻는다: {query}"
    assert run_repository.LEDGER_GAP_REQUEST_LIKE in params
    assert "item IS NULL AND end_code" not in query, "옛 모양 판정이 남아 있다"


def test_종료코드는_적는_쪽에서_주인이_하나다():
    """★ 판정에서는 빠졌어도 **적을 때 쓰는 값**의 주인은 여전히 하나다."""
    assert persistence._LEDGER_GAP_END_CODE is run_repository.LEDGER_GAP_END_CODE


def test_집계_결과의_모양(monkeypatch):
    """★ 세는 것은 SQL 이 하고 여기서는 모양만 바꾼다."""

    def _fake(query, params):
        return [
            {
                "as_of": _MON,
                "runs": 3,
                "items": ["무", "배추"],
                "gate_blocked": False,
                "end_codes": {"E1_APPROVED": 3},
            }
        ]

    monkeypatch.setattr(run_repository, "fetch_all", _fake)
    [row] = count_runs_by_day(sim_run_id=_SIM, start=_MON, end=_SUN)

    assert row == {
        "as_of": _MON,
        "runs": 3,
        "end_codes": {"E1_APPROVED": 3},
        "items": ("무", "배추"),
        "gate_blocked": False,
    }


# ── ⑦ 진입점 ───────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _no_db(monkeypatch, rows: list[DayRunCount]) -> None:
    """DB 와 달력을 둘 다 끊는다 — 진입점 모양만 잰다."""
    monkeypatch.setattr(walk_report_module, "count_runs_by_day", lambda **_: list(rows))
    monkeypatch.setattr("app.master.router.get_calendar", lambda: _Calendar())


def test_행이_하나도_없어도_200_이고_날이_판정으로_찬다(client, monkeypatch):
    """🔴 **404 로 내면 *"안 걸었다"* 와 *"그런 걷기가 없다"* 가 같아진다.**"""
    _no_db(monkeypatch, [])

    response = client.get(f"/master/walks/{_SIM}/report?start={_MON}&end={_SUN}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["days"]) == 7
    assert body["summary"] == {NO_ROW_ON_EXECUTION_DAY: 5, SKIPPED_OFF_DAY: 2}
    assert body["holiday_calendar_used"] is True


def test_거꾸로_된_범위는_400(client, monkeypatch):
    _no_db(monkeypatch, [])

    response = client.get(f"/master/walks/{_SIM}/report?start={_SUN}&end={_MON}")

    assert response.status_code == 400


def test_범위를_안_주면_전체가_되지_않는다(client, monkeypatch):
    """🔴 기본값으로 "전체" 를 만들면 §2 가 성립하지 않는다 — 빈 날을 못 센다."""
    _no_db(monkeypatch, [])

    assert client.get(f"/master/walks/{_SIM}/report").status_code == 422
    assert client.get(f"/master/walks/{_SIM}/report?start={_MON}").status_code == 422
