"""개장 정본 — **실패도 남아야 센다** (2026-09-06 · 계약 §2 이행).

🔴 이 파일이 잠그는 것은 넷이다.

```text
① 파트 트랜잭션 밖이다        롤백되면 "실패했다" 가 사라진다
② 적재 실패가 개장을 안 죽인다  이력이 없는 것보다 하루를 못 여는 것이 나쁘다
③ 성공이 연속 실패를 0 으로     "어제 성공, 오늘 첫 실패" 가 첫 실패로 세어진다
④ 정본 키는 (as_of, sim_run_id) 파트 고유 축을 마스터가 안 가진다
```
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

# ⚠️ **직접 바인딩한다.** conftest 가 모듈 속성을 가짜로 꽂는데(다른 검사가 실 DB 를
#   안 치게), 이 파일은 **그 함수 자체를 재므로** 임포트 시점의 진짜를 들고 있어야 한다.
#   가짜 커넥션을 직접 주므로 DB 로 안 나간다.
from app.master.day_opening_repository import (
    DayOpeningRecord,
    read_day_opening,
    record_day_opening,
)

AS_OF = date(2026, 1, 8)
SIM = "SIM-BURNIN-202512"


class _커서:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        self.executed.append((str(query), params))

    def fetchone(self) -> Any:
        return self.row


class _커넥션:
    def __init__(self, row: Any = None, *, raises: Exception | None = None) -> None:
        self.cur = _커서(row)
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self._raises = raises

    def cursor(self) -> Any:
        if self._raises is not None:
            raise self._raises
        return self.cur

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


# ── ① 적재 ────────────────────────────────────────────────────────────────


def test_적고_커밋한다():
    conn = _커넥션()

    ok = record_day_opening(
        as_of=AS_OF, sim_run_id=SIM, result="OPENED", connect=lambda: conn
    )

    assert ok is True
    assert conn.committed == 1
    assert conn.closed == 1


def test_정본_키가_as_of_와_sim_run_id_다():
    """🔴 **파트 고유 축(`financing_mode` · `usage_scope`)을 마스터가 안 가진다.**

    가지기 시작하면 파트가 늘 때마다 정본 키가 바뀐다 (재무·물류 2026-09-06 합의).
    """
    conn = _커넥션()

    record_day_opening(as_of=AS_OF, sim_run_id=SIM, result="OPENED", connect=lambda: conn)

    query, params = conn.cur.executed[0]
    assert "ON CONFLICT (as_of, sim_run_id)" in query
    assert "financing_mode" not in query
    assert "usage_scope" not in query


def test_성공이면_연속_실패를_0_으로_보낸다():
    """🔴 **어제 성공하고 오늘 처음 실패한 것이 첫 실패로 세어져야 한다.**

    안 그러면 `day_gate` 가 **재시도 한 번 없이 사람을 부른다.**
    """
    conn = _커넥션()

    record_day_opening(as_of=AS_OF, sim_run_id=SIM, result="ALREADY_OPENED", connect=lambda: conn)

    query, params = conn.cur.executed[0]
    assert "CASE WHEN %s THEN 0 ELSE" in query
    assert params[3] == 0, "처음 적을 때 실패 수가 0 이어야 한다"
    assert params[-1] is True, "성공 플래그가 참이어야 0 으로 리셋된다"


def test_실패면_연속_실패를_올린다():
    conn = _커넥션()

    record_day_opening(as_of=AS_OF, sim_run_id=SIM, result="NOT_OPENED", connect=lambda: conn)

    _, params = conn.cur.executed[0]
    assert params[3] == 1, "처음 적는 실패는 1 이다"
    assert params[-1] is False


def test_REJECTED_GAP_도_실패로_센다():
    conn = _커넥션()

    record_day_opening(as_of=AS_OF, sim_run_id=SIM, result="REJECTED_GAP", connect=lambda: conn)

    _, params = conn.cur.executed[0]
    assert params[-1] is False


# ── ② 적재 실패가 개장을 안 죽인다 ────────────────────────────────────────


def test_적재가_터져도_예외가_안_오른다():
    """🔴 **이력이 없는 것보다 하루를 못 여는 것이 나쁘다** (`try_save_run` 과 같은 판단).

    ⚠️ 다만 **조용히 넘어가지 않는다** — 로그에 남는다.
    """
    conn = _커넥션(raises=RuntimeError("연결 끊김"))

    ok = record_day_opening(
        as_of=AS_OF, sim_run_id=SIM, result="OPENED", connect=lambda: conn
    )

    assert ok is False
    assert conn.rolled_back == 1
    assert conn.closed == 1


def test_커넥션을_못_열어도_예외가_안_오른다():
    def 못연다() -> Any:
        raise RuntimeError("DB 없음")

    assert record_day_opening(as_of=AS_OF, sim_run_id=SIM, result="OPENED", connect=못연다) is False


# ── ③ 조회 ────────────────────────────────────────────────────────────────


def test_없으면_None_이다():
    """★ **없는 것과 못 읽은 것이 같은 `None` 이다.** 관문이 그때 근사하고, **근사라고
    사유에 적는다** — 판단을 멈추지 않는다."""
    conn = _커넥션(row=None)

    assert read_day_opening(as_of=AS_OF, sim_run_id=SIM, connect=lambda: conn) is None


def test_읽으면_연속_실패를_들고_온다():
    row = {
        "as_of": AS_OF,
        "sim_run_id": SIM,
        "result": "NOT_OPENED",
        "attempt_count": 17,
        "failure_count": 2,
        "reason": "재무가 안 열렸다",
    }
    conn = _커넥션(row=row)

    record = read_day_opening(as_of=AS_OF, sim_run_id=SIM, connect=lambda: conn)

    assert isinstance(record, DayOpeningRecord)
    assert record.failure_count == 2
    assert record.attempt_count == 17, "둘은 다른 수다"


def test_조회가_터져도_None_이다():
    conn = _커넥션(raises=RuntimeError("연결 끊김"))

    assert read_day_opening(as_of=AS_OF, sim_run_id=SIM, connect=lambda: conn) is None


# ── ④ open_day 가 정본을 남긴다 ───────────────────────────────────────────


def test_성공한_개장도_정본에_남긴다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **변이가 구멍을 찾은 자리다** (2026-09-06).

    `_record(out)` 를 통째로 지웠는데 검사가 하나도 안 울었다 — 미등록 경로만 재고
    **정상 경로를 안 쟀기 때문**이다. 그러면 *"매일 도는 성공"* 이 정본에 안 남고,
    `failure_count` 가 영영 0 으로 안 돌아간다.
    """
    from app.master import day_open

    남긴것: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.master.day_open.record_day_opening",
        lambda **kw: (남긴것.append(kw), True)[1],
        raising=False,
    )

    class _이미열린파트:
        def is_open(self, conn: Any, *, as_of: date) -> bool:
            return True

        def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:
            raise AssertionError("이미 열려 있으면 안 부른다")

    day_open.reset()
    day_open.register_day_opening("finance", _이미열린파트())
    day_open.register_day_opening("logistics", _이미열린파트())
    try:
        out = day_open.open_day(AS_OF, connect=lambda: _커넥션())
    finally:
        day_open.reset()

    assert out.status == "ALREADY_OPENED"
    assert 남긴것, "🔴 성공한 개장이 정본에 안 남았다 — failure_count 가 영영 안 리셋된다"
    assert 남긴것[0]["result"] == "ALREADY_OPENED"
    assert [p.part for p in 남긴것[0]["parts"]] == ["finance", "logistics"]


def test_open_day_가_파트_트랜잭션_밖에서_남긴다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **파트 트랜잭션 안에 넣으면 롤백될 때 *"실패했다"* 까지 사라진다.**

    그러면 `day_gate` 가 재시도와 사람을 영영 못 가른다.
    """
    from app.master import day_open

    남긴것: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.master.day_open.record_day_opening",
        lambda **kw: (남긴것.append(kw), True)[1],
        raising=False,
    )
    day_open.reset()

    out = day_open.open_day(AS_OF, connect=lambda: _커넥션())

    assert out.status == "NOT_OPENED", "미등록이라 안 열린다"
    assert 남긴것, "🔴 미등록으로 돌아설 때도 남겨야 한다 — 그것도 사실이다"
    assert 남긴것[0]["result"] == "NOT_OPENED"
    assert 남긴것[0]["as_of"] == AS_OF
