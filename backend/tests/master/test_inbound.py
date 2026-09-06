"""입고 실행 경계 — **도착분을 실제로 받는다** (2026-09-06 · 물류 요청).

🔴 **지금 아프고 있는 자리다** (실측).

```text
INB-H1-THRU-20260105-BAECHU-1-1   expected_arrival_date = 2026-01-07
그런데 2026-02-06 까지 32행 내내 in_transit 에 그대로 있다
→ 창고 점유를 30일 내내 먹는다
```

★ 이 파일이 잠그는 것은 다섯이다.

```text
① day_open 이 아니다              공통 진입점에 물류 전용 실행을 넣지 않는다
② 미등록과 "받을 것 없음" 이 다르다  뭉치면 어댑터가 빠진 날 조용히 성공한다
③ 트랜잭션을 마스터가 쥔다          실패하면 아무것도 안 바뀐다
④ 예외를 밖으로 안 낸다             입고 실패가 그날을 통째로 세우면 안 된다
⑤ 달력일이다                       창고는 토요일에도 받는다
```
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import inbound
from app.master.inbound import InboundPartOut, receive_arrivals

AS_OF = date(2026, 1, 7)
토요일 = date(2026, 1, 10)


class _가짜커넥션:
    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


class _물류:
    def __init__(self, *, out: InboundPartOut | None = None, raises: Exception | None = None):
        self.calls: list[date] = []
        self._out = out or InboundPartOut(part="logistics", status="NOTHING_DUE")
        self._raises = raises

    def receive(self, conn: Any, *, as_of: date) -> InboundPartOut:
        self.calls.append(as_of)
        if self._raises is not None:
            raise self._raises
        return self._out


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(inbound.registered())
    inbound.reset()
    yield
    inbound.reset()
    for part, impl in before.items():
        inbound.register_inbound(part, impl)


def _받음(*ids: str) -> InboundPartOut:
    return InboundPartOut(part="logistics", status="RECEIVED", received=list(ids))


# ── ① day_open 과 다른 등록소다 ───────────────────────────────────────────


def test_하루_넘김_등록소와_따로다():
    """🔴 **`day_open` 은 모든 파트에 대해 부르는 공통 진입점이다.**

    거기에 물류 전용 실행을 넣으면 **재무가 열릴 때도 입고가 돈다.**
    """
    from app.master import day_open

    inbound.register_inbound("logistics", _물류())

    assert "logistics" in inbound.registered()
    assert inbound.missing() == ()
    # 하루 넘김 등록소는 이 등록에 영향받지 않는다
    assert set(day_open.PARTS) == {"finance", "logistics"}
    assert set(inbound.PARTS) == {"logistics"}


def test_입고_파트는_물류_하나다():
    """★ 재무·매입은 도착 자체를 실행하지 않는다 — 재무는 지급일에 움직이고 매입은
    승인에서 끝난다."""
    with pytest.raises(ValueError, match="입고 실행 파트가 아니다"):
        inbound.register_inbound("finance", _물류())  # type: ignore[arg-type]


# ── ② 미등록과 "받을 것 없음" 은 다른 사실이다 ────────────────────────────


def test_미등록이면_사유가_남는다():
    """🔴 **뭉치면 물류 어댑터가 빠진 날 조용히 아무 일도 안 일어난다.**"""
    conn = _가짜커넥션()

    out = receive_arrivals(AS_OF, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == ["logistics"]
    assert "미등록" in out.reason
    assert conn.committed == 0, "커넥션을 열지도 말아야 한다"


def test_받을_것이_없는_것은_미등록이_아니다():
    """★ 둘 다 `NOTHING_DUE` 지만 `missing` 과 `reason` 이 가른다."""
    inbound.register_inbound("logistics", _물류())
    conn = _가짜커넥션()

    out = receive_arrivals(AS_OF, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == [], "등록은 돼 있다"
    assert out.reason == ""
    assert out.parts[0].status == "NOTHING_DUE"
    assert conn.committed == 1, "물어보기는 했다"


# ── ③ 트랜잭션 ────────────────────────────────────────────────────────────


def test_받으면_한_번_커밋한다():
    inbound.register_inbound("logistics", _물류(out=_받음("INB-A-1")))
    conn = _가짜커넥션()

    out = receive_arrivals(AS_OF, connect=lambda: conn)

    assert out.status == "RECEIVED"
    assert out.parts[0].received == ["INB-A-1"]
    assert conn.committed == 1
    assert conn.rolled_back == 0
    assert conn.closed == 1


def test_터지면_통째로_롤백한다():
    """🔴 입고가 반쯤 되면 **로트는 생겼는데 in_transit 은 남은** 장부가 된다."""
    inbound.register_inbound("logistics", _물류(raises=RuntimeError("검수에서 막혔다")))
    conn = _가짜커넥션()

    out = receive_arrivals(AS_OF, connect=lambda: conn)

    assert out.status == "FAILED"
    assert "검수에서 막혔다" in out.reason
    assert conn.committed == 0
    assert conn.rolled_back == 1
    assert conn.closed == 1


# ── ④ 예외를 밖으로 안 낸다 ───────────────────────────────────────────────


def test_실패해도_예외가_안_오른다():
    """★ **입고 실패가 그날을 통째로 세우면 안 된다** — 그건 입고 하나보다 크다.

    `apply_approval` · `undo_approval` 과 같은 태도다.
    """
    inbound.register_inbound("logistics", _물류(raises=RuntimeError("boom")))

    out = receive_arrivals(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "FAILED"
    assert out.parts == [], "실패했으면 파트 결과를 내지 않는다"


# ── ⑤ 달력일이다 ─────────────────────────────────────────────────────────


def test_토요일에도_받는다():
    """🔴 **창고는 토요일에도 물건을 받는다.**

    `is_open` 은 *"시장이 서는가"* 이지 창고가 여는가가 아니다 — `open_day` 와 같은
    결이고 `#240` 의 *"실행일은 평일만, 경과일수는 달력일"* 과 같다.
    """
    assert 토요일.weekday() == 5
    물류 = _물류(out=_받음("INB-SAT-1"))
    inbound.register_inbound("logistics", 물류)

    out = receive_arrivals(토요일, connect=lambda: _가짜커넥션())

    assert out.status == "RECEIVED"
    assert 물류.calls == [토요일], "실행일 달력으로 밀었다"


def test_받는_날을_그대로_넘긴다():
    물류 = _물류()
    inbound.register_inbound("logistics", 물류)

    receive_arrivals(AS_OF, connect=lambda: _가짜커넥션())

    assert 물류.calls == [AS_OF]


# ── ⑥ 모듈 규율 ───────────────────────────────────────────────────────────


def test_실행일_달력을_안_쓴다():
    """🔴 **원문을 잠근다.** `next_execution_day` 를 부르는 순간 토요일 입고가 월요일로
    밀리고, 그건 조용히 틀린다."""
    import pathlib

    원문 = pathlib.Path(inbound.__file__).read_text(encoding="utf-8")
    코드 = "\n".join(
        line for line in 원문.splitlines() if not line.strip().startswith(("#", "*", "```"))
    )

    assert "next_execution_day" not in 코드
    assert "is_execution_day" not in 코드
