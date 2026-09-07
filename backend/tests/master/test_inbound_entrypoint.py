"""입고에 **자기 진입점**이 있는가 — 그리고 순서를 **코드가** 지키는가.

🔴 **물류 물음 2026-09-07** — *"`receive_arrivals(as_of)` 의 production 호출자는
   어디입니까?"* 실측해 보니 **0건**이었다. 경계도 구현도 검수도 배선도 다 섰는데
   부르는 자리가 없어, 새 승인이 정상 `purchase_id` 를 실어도 도착일에 아무 일도
   안 일어날 상태였다.

★ **`register_inbound` 호출이 0건이던 것(`#337`)과 다른 문제다.** 그때는 *"무엇으로
  받을지"* 가 미등록이었고, 이번은 *"언제 받을지"* 가 없다. 둘을 한 문장으로 접으면
  고칠 곳이 사라진다.

---

🔴 **왜 `run_procurement` 안이 아닌가.**

`router.py` 의 `master_open_day` 가 개장에 대해 적어 둔 그대로다 — *"명시적 호출이다.
실행의 부작용이 아니다. 하루가 넘어가는 것은 **사건**이고, 사건에는 자기 자리가 있다."*
**입고도 사건이다.** 판단 안에 넣으면 판단 한 번이 재고를 늘리고 *"같은 `as_of` 로
백번 돌려도 같은 답"* 이 깨진다.

🔴 **왜 상위 `run_day` 하나로 묶지 않는가.** 물류가 그 안(`B`)을 주셨는데, 묶으면
   **실패 조합을 한 응답으로 못 낸다** — *"개장 성공 · 입고 BLOCKED · 판단 성공"* 을
   한 `status` 로 적을 수 없다. `#316` 에서 `BLOCKED` 를 `NOTHING_DUE` 로 접었다가
   물류가 잡아 준 것과 같은 병이다.

★ 대신 **순서를 문장이 아니라 Gate 가 지킨다.** 전에는 docstring 에 *"`open_day`
  다음이다"* 라고만 적혀 있었고 코드는 아무것도 안 봤다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.master import inbound
from app.master.day_gate import DayGate
from app.master.inbound import InboundPartOut, receive_arrivals

AS_OF = date(2026, 1, 7)
토요일 = date(2026, 1, 10)


class _물류:
    def __init__(self, out: InboundPartOut | None = None) -> None:
        self.calls: list[date] = []
        self._out = out or InboundPartOut(part="logistics", status="NOTHING_DUE")

    def receive(self, conn: Any, *, as_of: date) -> InboundPartOut:
        self.calls.append(as_of)
        return self._out


class _가짜커넥션:
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(inbound.registered())
    inbound.reset()
    yield
    inbound.reset()
    for part, impl in before.items():
        inbound.register_inbound(part, impl)


def _막힌_Gate(as_of: date, *, connect: Any = None) -> DayGate:
    return DayGate(
        as_of=as_of,
        gate="BLOCKED",
        result="NEVER_OPENED",
        reason="한 번도 열린 적이 없다",
        next_action="OPEN_DAY_REQUIRED",
    )


# ---------------------------------------------------------------------------
# 1. 진입점이 있는가
# ---------------------------------------------------------------------------


def test_도착분을_받는_엔드포인트가_있다() -> None:
    """🔴 **이것이 없어서 물류가 물었다.** 없으면 도착일에 아무도 안 부른다."""
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/receive" in paths, (
        f"입고 진입점이 없다. 있는 경로: {sorted(p for p in paths if 'days' in p)}"
    )


def test_개장과_입고가_다른_엔드포인트다() -> None:
    """★ **사건 둘은 자리 둘이다.** 하나로 묶으면 실패 조합을 못 낸다."""
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/open" in paths
    assert "/master/days/{as_of}/receive" in paths


def test_판단_경로가_입고를_부작용으로_돌리지_않는다() -> None:
    """🔴 **`run_procurement` 이 입고를 부르면 판단이 재고를 늘린다.**

    *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다 — 개장을 판단 밖에 둔 이유와
    같다. 원문을 읽어 잠근다.
    """
    import ast
    import inspect as _inspect

    from app.master import service

    tree = ast.parse(_inspect.getsource(service))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "receive_arrivals" not in called, (
        "판단 경로가 입고를 부른다 — 입고는 사건이라 자기 자리가 있어야 한다"
    )


# ---------------------------------------------------------------------------
# 2. 순서를 **코드가** 지키는가
# ---------------------------------------------------------------------------


def test_안_열린_날은_받지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 전에는 docstring 에 *"open_day 다음이다"* 라고만 적혀 있었다."""
    monkeypatch.setattr(inbound, "check_day_gate", _막힌_Gate)
    물류 = _물류()
    inbound.register_inbound("logistics", 물류)

    out = receive_arrivals(AS_OF, connect=_가짜커넥션)

    assert out.status == "NOT_OPENED"
    assert 물류.calls == [], "장부가 안 열렸는데 파트를 불렀다"


def test_안_열린_것과_받을_게_없는_것을_가른다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **접으면 *"어제 개장을 안 돌렸다"* 가 *"오늘은 올 게 없었다"* 로 나간다.**"""
    monkeypatch.setattr(inbound, "check_day_gate", _막힌_Gate)
    inbound.register_inbound("logistics", _물류())

    out = receive_arrivals(AS_OF, connect=_가짜커넥션)

    assert out.status != "NOTHING_DUE"
    assert out.status != "BLOCKED", (
        "안 열린 것을 BLOCKED 로 접었다 — BLOCKED 는 '그 건이' 처리 불가라는 뜻이다"
    )


def test_다음에_할_일을_해석하지_않고_옮긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 무엇을 해야 하는지는 **개장이 아는 사실**이다. 입고가 다시 판정하면 주인이 둘이 된다."""
    monkeypatch.setattr(inbound, "check_day_gate", _막힌_Gate)
    inbound.register_inbound("logistics", _물류())

    out = receive_arrivals(AS_OF, connect=_가짜커넥션)

    assert out.next_action == "OPEN_DAY_REQUIRED"
    assert out.reason == "한 번도 열린 적이 없다"


def test_열린_날은_평소대로_받는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ Gate 가 통과를 막으면 안 된다 — 미등록도 PASS 다 (`day_gate` 계약)."""
    monkeypatch.setattr(
        inbound,
        "check_day_gate",
        lambda as_of, connect=None: DayGate(as_of=as_of, gate="PASS", result="ALREADY_OPENED"),
    )
    물류 = _물류(InboundPartOut(part="logistics", status="RECEIVED", received=["INB-A-1"]))
    inbound.register_inbound("logistics", 물류)

    out = receive_arrivals(AS_OF, connect=_가짜커넥션)

    assert out.status == "RECEIVED"
    assert 물류.calls == [AS_OF]
    assert out.next_action is None, "막히지 않았는데 다음 할 일이 실렸다"


def test_입고가_하루를_열지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **여기서 `open_day` 를 부르면 입고가 개장의 부작용이 된다.**

    `check_day_gate` 는 묻기만 한다. 원문으로 잠근다.
    """
    import ast
    import inspect as _inspect

    src = _inspect.getsource(inbound.receive_arrivals)
    tree = ast.parse(src.lstrip())
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "open_day" not in called, "입고가 하루를 연다 — 개장은 명시적 사건이어야 한다"
    assert "check_day_gate" in called, "Gate 를 안 본다 — 순서가 다시 문장으로만 남는다"


# ---------------------------------------------------------------------------
# 3. 달력일이다
# ---------------------------------------------------------------------------


def test_토요일에도_받는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **창고는 토요일에도 받는다.** 실행일 판정을 여기에 끼우면 안 된다.

    ```text
    토요일   open_day 실행 · receive 실행 · run_procurement 은 실행일이 아니라 SKIP
    ```
    """
    monkeypatch.setattr(
        inbound,
        "check_day_gate",
        lambda as_of, connect=None: DayGate(as_of=as_of, gate="PASS", result="OPENED"),
    )
    물류 = _물류(InboundPartOut(part="logistics", status="RECEIVED", received=["INB-SAT-1"]))
    inbound.register_inbound("logistics", 물류)

    out = receive_arrivals(토요일, connect=_가짜커넥션)

    assert out.status == "RECEIVED"
    assert 물류.calls == [토요일]


def test_엔드포인트가_실패도_200_으로_낸다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `/days/{as_of}/open` 과 같은 태도 — 막힌 것은 오류가 아니라 **그날의 사실**이다."""
    monkeypatch.setattr(inbound, "check_day_gate", _막힌_Gate)
    inbound.register_inbound("logistics", _물류())

    import app.main

    client = TestClient(app.main.app)
    resp = client.post(f"/master/days/{AS_OF.isoformat()}/receive")

    assert resp.status_code == 200
    assert resp.json()["status"] == "NOT_OPENED"
    assert resp.json()["next_action"] == "OPEN_DAY_REQUIRED"
