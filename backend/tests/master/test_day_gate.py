"""개장 관문 — **그날이 열렸는가** (계약 2026-09-04 · 구현 2026-09-06).

🔴 **두 Gate 는 다른 물음이다.**

```text
요청 진입
  → open_day Gate       "그날 장부가 열렸는가"     ← 이 파일
  → execution day Gate  "그날 판단을 도는가"
  → Purchase Flow
```

★ **토요일은 첫 관문을 통과하고 둘째에서 막힌다.** 개장은 달력일 전부이고 매입 판단은
  평일만이다.

⚠️ **계약을 내고 그보다 작게 만든 자리였다.** 재무가 `day_gate.gate` 어휘로 회신해
  와서 드러났다 — 구현에 관문 자체가 없었다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.master import day_open
from app.master.day_gate import SPLIT_THRESHOLD_DAYS, check_day_gate

AS_OF = date(2026, 1, 7)
토요일 = date(2026, 1, 10)


class _가짜커넥션:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _파트:
    """`opened_through` 날까지 열려 있는 파트."""

    def __init__(self, opened_through: date | None) -> None:
        self._through = opened_through
        self.asked: list[date] = []

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        self.asked.append(as_of)
        return self._through is not None and as_of <= self._through

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:  # pragma: no cover
        raise AssertionError("관문은 열지 않는다 — 물어보기만 한다")


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(day_open.registered())
    day_open.reset()
    yield
    day_open.reset()
    for part, impl in before.items():
        day_open.register_day_opening(part, impl)


def _등록(finance: date | None, logistics: date | None) -> tuple[_파트, _파트]:
    f, lg = _파트(finance), _파트(logistics)
    day_open.register_day_opening("finance", f)
    day_open.register_day_opening("logistics", lg)
    return f, lg


# ── ① 통과 ───────────────────────────────────────────────────────────────


def test_둘_다_열려_있으면_통과한다():
    _등록(AS_OF, AS_OF)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.gate == "PASS"
    assert gate.result == "ALREADY_OPENED"
    assert gate.next_action is None, "PASS 면 next_action 은 null 이다"


def test_토요일도_통과한다():
    """🔴 **개장은 달력일 전부다.** 매입 판단이 평일만인 것과 **다른 관문**이다."""
    assert 토요일.weekday() == 5
    _등록(토요일, 토요일)

    gate = check_day_gate(토요일, connect=lambda: _가짜커넥션())

    assert gate.gate == "PASS"


def test_등록이_0건이면_통과한다():
    """⚠️ **없는 구현에 대고 *"안 열렸다"* 고 말하지 않는다.**

    이것이 없으면 개장 구현이 붙기 전까지 **모든 판단이 막힌다.**
    """
    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.gate == "PASS"


def test_관문은_열지_않는다():
    """★ 여는 것은 `open_day` 이고 별도 진입점이다.

    *"조회했을 뿐인데 행이 생겼다"* 가 되면 안 된다 — `_파트.open_day` 가 부르면 터진다.
    """
    _등록(AS_OF, AS_OF)

    check_day_gate(AS_OF, connect=lambda: _가짜커넥션())  # open_day 를 부르면 AssertionError


def test_커넥션을_닫는다():
    conn = _가짜커넥션()
    _등록(AS_OF, AS_OF)

    check_day_gate(AS_OF, connect=lambda: conn)

    assert conn.closed == 1


# ── ② 막힘 — gate 와 result 를 따로 낸다 ──────────────────────────────────


def test_한_파트만_안_열려도_막는다():
    """★ **등록된 파트 전부가 열려 있어야 통과다.** 하나라도 안 열렸으면 그 날 장부는
    온전하지 않고, 그 위에서 판단하면 **없는 상태를 읽거나 남의 날 상태를 읽는다.**"""
    _등록(AS_OF - timedelta(days=1), AS_OF)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.gate == "BLOCKED"
    assert gate.result == "NOT_OPENED"
    assert [p.part for p in gate.failed_parts] == ["finance"]


def test_gate_만_보고_막을_수_있다():
    """🔴 **`result` 다섯 중 어느 것이 통과인지를 화면이 알아야 한다면 그건 값 파싱과
    같다** (판매 요청)."""
    _등록(AS_OF - timedelta(days=3), AS_OF - timedelta(days=3))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.gate == "BLOCKED"
    assert gate.result in {"NOT_OPENED", "REJECTED_GAP", "NEVER_OPENED"}


def test_막히면_next_action_이_반드시_찬다():
    """🔴 **`BLOCKED` 인데 `next_action` 이 비는 경우는 없다.** 막았으면 다음에 무엇을
    할지도 같이 말한다."""
    _등록(AS_OF - timedelta(days=2), AS_OF - timedelta(days=2))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.next_action is not None


# ── ③ next_action 판정 — 계약 §2 그대로 ───────────────────────────────────


def test_한_번도_안_열렸으면_OPEN_DAY_REQUIRED():
    _등록(None, None)

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.result == "NEVER_OPENED"
    assert gate.next_action == "OPEN_DAY_REQUIRED"
    assert gate.gap_days is None
    assert gate.last_opened_date is None


def test_상한_안이면_RETRY_OPEN_DAY():
    _등록(AS_OF - timedelta(days=5), AS_OF - timedelta(days=5))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.result == "NOT_OPENED"
    assert gate.next_action == "RETRY_OPEN_DAY"
    assert gate.gap_days == 5
    assert gate.last_opened_date == AS_OF - timedelta(days=5)


def test_31일을_넘으면_ADMIN_FORCE_OPEN_REQUIRED():
    """🔴 **`NOT_OPENED` 로 접으면 화면이 재시도를 권하고, 재시도로는 안 풀린다.**"""
    _등록(AS_OF - timedelta(days=40), AS_OF - timedelta(days=40))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.result == "REJECTED_GAP"
    assert gate.next_action == "ADMIN_FORCE_OPEN_REQUIRED"
    assert gate.gap_days == 40


def test_366일을_넘으면_SPLIT_FORCE_OPEN_REQUIRED():
    """★ 관리자가 강제 개장을 눌러도 안 열린다 — 합의된 절대 상한이라 마스터가 거절한다.

    `ADMIN_FORCE_OPEN_REQUIRED` 로 보내면 **관리자가 눌렀는데 안 열리고 화면은 왜인지
    못 말한다.**
    """
    _등록(AS_OF - timedelta(days=400), AS_OF - timedelta(days=400))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.result == "REJECTED_GAP"
    assert gate.next_action == "SPLIT_FORCE_OPEN_REQUIRED"
    assert gate.gap_days == 400
    assert gate.gap_days > SPLIT_THRESHOLD_DAYS


def test_횟수를_아직_안_센다는_것을_사유에_적는다():
    """🟡 계약은 실패 1회째와 2회 이상을 **횟수로** 가르는데
    `master_day_openings.attempt_count` 가 아직 없다. **조용히 근사하지 않는다.**"""
    _등록(AS_OF - timedelta(days=2), AS_OF - timedelta(days=2))

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert "시도 횟수를 아직 안 세" in gate.reason


# ── ④ 못 물어본 것과 안 열린 것은 다르다 ──────────────────────────────────


def test_조회가_터지면_CONTACT_OPERATOR():
    """⚠️ **관문이 500 을 내면 판단이 아예 안 돈다.** 예외를 값으로 바꾼다."""

    class _터지는파트:
        def is_open(self, conn: Any, *, as_of: date) -> bool:
            raise RuntimeError("연결 없음")

    day_open.register_day_opening("finance", _터지는파트())

    gate = check_day_gate(AS_OF, connect=lambda: _가짜커넥션())

    assert gate.gate == "BLOCKED"
    assert gate.next_action == "CONTACT_OPERATOR"
    assert "못 읽었다" in gate.reason


# ── ⑤ 두 관문이 실행 경로에서 갈린다 ──────────────────────────────────────


def test_run_procurement_이_개장을_먼저_본다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **개장이 실행일보다 먼저다.** 그 날 장부가 안 열렸으면 실행일이어도 읽을
    상태가 없다."""
    from app.master.day_gate import DayGate
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    막힘 = DayGate(
        as_of=AS_OF,
        gate="BLOCKED",
        result="NOT_OPENED",
        reason="재무가 안 열렸다",
        next_action="RETRY_OPEN_DAY",
    )
    monkeypatch.setattr("app.master.service.check_day_gate", lambda as_of, **kw: 막힘)
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: "RUN-1")

    평일 = date(2026, 1, 7)
    assert 평일.weekday() < 5, "실행일인데도 막히는 것을 재는 검사다"

    out = run_procurement(
        ProcurementRunRequest(as_of=평일, policy_version="v1.3", item="배추", request_id="REQ-G-1"),
        verifier=None,
    )

    assert out.end_code == "E4_NOT_STARTED"
    assert out.day_gate is not None
    assert out.day_gate.gate == "BLOCKED"
    assert out.day_gate.next_action == "RETRY_OPEN_DAY"


def test_토요일은_개장을_통과하고_실행일에서_막힌다(monkeypatch: pytest.MonkeyPatch):
    """★ **`E4_NOT_STARTED` 하나로는 그 둘이 같아 보인다.** `day_gate` 가 가른다."""
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: "RUN-2")

    out = run_procurement(
        ProcurementRunRequest(
            as_of=토요일, policy_version="v1.3", item="배추", request_id="REQ-G-2"
        ),
        verifier=None,
    )

    assert out.end_code == "E4_NOT_STARTED"
    assert out.day_gate is not None
    assert out.day_gate.gate == "PASS", "개장은 통과해야 한다 — 막은 것은 실행일 관문이다"
    assert "주말" in (out.reason or "")
