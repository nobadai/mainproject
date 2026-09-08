"""수금 실행 경계 — **채권이 돈으로 들어오는 자리** (2026-09-07 · 재무 결정).

🔴 **부르는 곳이 0건이다** (실측).

```text
app/finance/collection.py :196   apply_explicit_collection(conn, CollectionEvent(...))
production 호출자 0건 — 검사 파일 하나가 전부다
```

★ 재무 결정 2026-09-07.

```text
Finance   실제 수금 사건의 정본 의미 소유
Master    날짜에 해당하는 사건을 **운반하고 호출**한다.
          수금 대상이나 수금액을 **스스로 결정하지 않는다**
Sales     계약 결제조건·채권 발생의 상업적 원천
```

★ 이 파일이 잠그는 것은 여섯이다.

```text
① 네 등록소와 섞이지 않는다        한 사전이면 "입고는 되는데 수금은 안 됨"을 못 적는다
② 미등록과 "들어올 것 없음" 이 다르다  뭉치면 재무 구현체가 빠진 날 조용히 성공한다
③ 트랜잭션을 마스터가 쥔다          실패하면 아무것도 안 바뀐다
④ 예외를 밖으로 안 낸다             수금 실패가 그날을 통째로 세우면 안 된다
⑤ 개장 Gate 를 코드가 본다          안 열린 날은 묻지도 않는다
⑥ 판단이 현금을 움직이지 않는다      run_procurement 안에 넣으면 멱등이 깨진다
```

🟢 **재무 구현체가 섰다** (`#404` · `app/finance/collection_source.py`). 배선은
  `app/master/finance_collection.py` 가 하고 `main.py` 가 등록한다.

⚠️ **그래도 이 파일의 가짜 재무는 그대로 둔다.** 여기가 재는 것은 *"등록되면 어떻게
  도는가"* 이지 재무 구현이 아니다. 실물을 끌어오면 이 여섯 규율이 재무 사정에 따라
  깨지고, 그때 **무엇이 틀렸는지 이 파일이 못 말한다.**

⚠️ **사건 원천은 아직 비어 있다** (`DeterministicCollectionFixtureSource.events = ()`).
  배선은 경로를 세울 뿐 수금을 만들지 않는다 — `NOTHING_DUE` 와 「미등록」이 다른
  사실이라는 것이 `②` 이고, 지금은 그 둘 중 앞쪽이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.master import collection
from app.master.collection import CollectionPartOut, collect_receipts
from app.master.day_gate import DayGate

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


class _재무:
    def __init__(self, *, out: CollectionPartOut | None = None, raises: Exception | None = None):
        self.calls: list[date] = []
        self._out = out or CollectionPartOut(part="finance", status="NOTHING_DUE")
        self._raises = raises

    def collect(self, conn: Any, *, as_of: date) -> CollectionPartOut:
        self.calls.append(as_of)
        if self._raises is not None:
            raise self._raises
        return self._out


def _수금(*ids: str) -> CollectionPartOut:
    return CollectionPartOut(part="finance", status="COLLECTED", collected=list(ids))


def _막힌_Gate(as_of: date, *, connect: Any = None) -> DayGate:
    return DayGate(
        as_of=as_of,
        gate="BLOCKED",
        result="NEVER_OPENED",
        reason="한 번도 열린 적이 없다",
        next_action="OPEN_DAY_REQUIRED",
    )


@pytest.fixture(autouse=True)
def _열린_날로_둔다(monkeypatch: pytest.MonkeyPatch) -> Any:
    """🔴 **이 파일은 수금 경계를 잰다 — 개장 상태를 재지 않는다.**

    `collect_receipts` 가 앞에 개장 Gate 를 두면서 이 파일의 검사가 **실 DB 의
    `master_day_openings` 에 매이게** 됐다. 앞선 검사가 그 표에 무엇을 남기면 여기가
    통째로 `NOT_OPENED` 로 바뀐다 — `test_inbound.py` 가 같은 이유로 같은 전제를 둔다.

    ★ **숨기지 않고 전제로 적는다.** 가려 두면 *"수금 경계가 깨졌다"* 와 *"어제 개장이
      실패해 있었다"* 가 같은 빨간불로 나오고, 그건 이 파일이 잡으려는 것이 아니다.

    ⚠️ **Gate 자체는 아래 `⑤` 절이 잰다.** 거기서는 이 fixture 를 다시 덮어쓴다 —
      통과로 두는 것이 그 검사를 무르지 않는다.
    """
    monkeypatch.setattr(
        collection,
        "check_day_gate",
        lambda as_of, connect=None: DayGate(as_of=as_of, gate="PASS", result="ALREADY_OPENED"),
    )


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(collection.registered())
    collection.reset()
    yield
    collection.reset()
    for part, impl in before.items():
        collection.register_collection(part, impl)


# ── ① 네 등록소와 섞이지 않는다 ───────────────────────────────────────────


def test_다섯째_등록소는_앞의_넷과_따로다():
    """🔴 **한 사전에 섞으면 *"입고는 되는데 수금은 안 되는"* 상태를 못 적는다.**

    ★ `day_open` 은 모든 파트에 대해 부르는 공통 진입점이라 거기에 재무 전용 수금을
      넣으면 **물류가 열릴 때도 현금이 움직인다.**
    """
    from app.master import day_open, inbound

    collection.register_collection("finance", _재무())

    assert "finance" in collection.registered()
    assert collection.missing() == ()
    # 앞의 등록소들은 이 등록에 영향받지 않는다
    assert set(day_open.PARTS) == {"finance", "logistics"}
    assert set(inbound.PARTS) == {"logistics"}
    assert set(collection.PARTS) == {"finance"}
    assert "finance" not in inbound.registered(), "입고 등록소에 재무가 새어 들어갔다"


def test_수금_파트는_재무_하나다():
    """★ 판매는 채권 발생의 상업적 원천이지 *"오늘 얼마 입금됐다"* 를 만들지 않는다.

    물류·매입은 현금 흐름에 손대지 않는다.
    """
    with pytest.raises(ValueError, match="수금 실행 파트가 아니다"):
        collection.register_collection("logistics", _재무())  # type: ignore[arg-type]


# ── ② 미등록과 "들어올 것 없음" 은 다른 사실이다 ──────────────────────────


def test_미등록이면_사유가_남는다():
    """🔴 **재무 구현체가 아직 없다 — 이것이 지금의 정상 상태다.**

    ⚠️ 오류가 아니다. 뭉치면 재무 어댑터가 붙기 전까지 매일 *"오늘은 들어올 게
      없었다"* 로 보이고, 붙는 것을 아무도 안 챙긴다.
    """
    conn = _가짜커넥션()

    out = collect_receipts(AS_OF, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == ["finance"]
    assert "미등록" in out.reason
    assert conn.committed == 0, "커넥션을 열지도 말아야 한다"


def test_들어올_것이_없는_것은_미등록이_아니다():
    """★ 둘 다 `NOTHING_DUE` 지만 `missing` 과 `reason` 이 가른다."""
    collection.register_collection("finance", _재무())
    conn = _가짜커넥션()

    out = collect_receipts(AS_OF, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == [], "등록은 돼 있다"
    assert out.reason == ""
    assert out.parts[0].status == "NOTHING_DUE"
    assert conn.committed == 1, "물어보기는 했다"


# ── ③ 트랜잭션 ────────────────────────────────────────────────────────────


def test_수금하면_한_번_커밋한다():
    collection.register_collection("finance", _재무(out=_수금("RCV-A-1")))
    conn = _가짜커넥션()

    out = collect_receipts(AS_OF, connect=lambda: conn)

    assert out.status == "COLLECTED"
    assert out.parts[0].collected == ["RCV-A-1"]
    assert conn.committed == 1
    assert conn.rolled_back == 0
    assert conn.closed == 1


def test_터지면_통째로_롤백한다():
    """🔴 수금이 반쯤 되면 **현금은 늘었는데 채권 잔액은 그대로인** 장부가 된다."""
    collection.register_collection("finance", _재무(raises=RuntimeError("축이 안 맞는다")))
    conn = _가짜커넥션()

    out = collect_receipts(AS_OF, connect=lambda: conn)

    assert out.status == "FAILED"
    assert "축이 안 맞는다" in out.reason
    assert conn.committed == 0
    assert conn.rolled_back == 1
    assert conn.closed == 1


# ── ④ 예외를 밖으로 안 낸다 ───────────────────────────────────────────────


def test_실패해도_예외가_안_오른다():
    """★ **수금 실패가 그날을 통째로 세우면 안 된다** — `receive_arrivals` 와 같은 태도.

    ⚠️ *"예외를 안 올린다"* 가 *"그러니 판단을 계속한다"* 는 아니다. 부르는 쪽이 값을
      보고 정한다.
    """
    collection.register_collection("finance", _재무(raises=RuntimeError("boom")))

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "FAILED"
    assert out.parts == [], "실패했으면 파트 결과를 내지 않는다"


# ── ⑤ 개장 Gate 를 코드가 본다 ────────────────────────────────────────────


def test_안_열린_날은_수금하지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 순서를 docstring 문장으로만 두면 코드가 아무것도 안 본다."""
    monkeypatch.setattr(collection, "check_day_gate", _막힌_Gate)
    재무 = _재무()
    collection.register_collection("finance", 재무)

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "NOT_OPENED"
    assert 재무.calls == [], "장부가 안 열렸는데 파트를 불렀다"


def test_안_열린_것을_BLOCKED_로_접지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **고칠 곳이 완전히 다른데 화면은 같아 보인다.**

    ```text
    BLOCKED      수금할 대상이 있는데 **그 건이** 반영 불가 — receivable_id 가 나온다
    NOT_OPENED   **아직 아무것도 안 봤다** — 장부가 없어 물어보지도 못했다
    ```
    """
    monkeypatch.setattr(collection, "check_day_gate", _막힌_Gate)
    collection.register_collection("finance", _재무())

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status != "NOTHING_DUE"
    assert out.status != "BLOCKED", (
        "안 열린 것을 BLOCKED 로 접었다 — BLOCKED 는 '그 건이' 반영 불가라는 뜻이다"
    )
    assert out.status == "NOT_OPENED"


def test_다음에_할_일을_해석하지_않고_옮긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 무엇을 해야 하는지는 **개장이 아는 사실**이다. 수금이 다시 판정하면 주인이 둘이 된다."""
    monkeypatch.setattr(collection, "check_day_gate", _막힌_Gate)
    collection.register_collection("finance", _재무())

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.next_action == "OPEN_DAY_REQUIRED"
    assert out.reason == "한 번도 열린 적이 없다"


def test_열린_날은_평소대로_수금한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ Gate 가 통과를 막으면 안 된다 — 미등록도 PASS 다 (`day_gate` 계약)."""
    monkeypatch.setattr(
        collection,
        "check_day_gate",
        lambda as_of, connect=None: DayGate(as_of=as_of, gate="PASS", result="OPENED"),
    )
    재무 = _재무(out=_수금("RCV-A-1"))
    collection.register_collection("finance", 재무)

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "COLLECTED"
    assert 재무.calls == [AS_OF]
    assert out.next_action is None, "막히지 않았는데 다음 할 일이 실렸다"


# ── ⑥ BLOCKED 를 NOTHING_DUE 로 접지 않는다 ───────────────────────────────


def test_파트가_BLOCKED_면_전체도_BLOCKED_다():
    """🔴 **접으면 뒤의 orchestration 이 정상으로 오해한다.**

    ```text
    NOTHING_DUE   실제로 수금할 대상이 없음
    BLOCKED       **수금할 대상은 존재하지만 반영할 수 없음**
    ```

    ★ 축 불일치나 깨진 원장으로 막힌 날이 *"오늘은 들어올 게 없었다"* 로 보이면,
      **들어왔어야 할 현금이 장부에 없는 채로 다음 판단이 돈다.**
    """
    막힘 = CollectionPartOut(
        part="finance", status="BLOCKED", reason="receivable 과 finance_state 축이 안 맞는다"
    )
    collection.register_collection("finance", _재무(out=막힘))

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "BLOCKED"
    assert out.status != "NOTHING_DUE", "들어올 게 있는데 없다고 말했다"
    assert "막혔다" in out.reason
    assert out.parts[0].reason == "receivable 과 finance_state 축이 안 맞는다"


def test_수금한_것이_있어도_막힌_것이_있으면_BLOCKED_다():
    """★ *"들어올 게 있었는데 못 받았다"* 가 *"받았다"* 보다 **먼저 알려야 하는 사실**이다."""

    class _둘을_내는_재무:
        def collect(self, conn: Any, *, as_of: date) -> CollectionPartOut:
            return CollectionPartOut(part="finance", status="BLOCKED", collected=["RCV-A-1"])

    collection.register_collection("finance", _둘을_내는_재무())

    out = collect_receipts(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "BLOCKED"


# ── ⑦ 달력일이다 ──────────────────────────────────────────────────────────


def test_실행일_달력으로_as_of_를_보정하지_않는다():
    """🔴 **입금은 토요일에도 찍힌다.**

    `next_execution_day` 로 밀면 토요일 수금이 월요일 장부에 적히고, 그건 **마스터가
    재무 사실의 날짜를 바꾸는 것**이다.
    """
    assert 토요일.weekday() == 5
    재무 = _재무(out=_수금("RCV-SAT-1"))
    collection.register_collection("finance", 재무)

    out = collect_receipts(토요일, connect=lambda: _가짜커넥션())

    assert out.status == "COLLECTED"
    assert 재무.calls == [토요일], "실행일 달력으로 밀었다"


# ── ⑧ 진입점 ──────────────────────────────────────────────────────────────


def test_수금_엔드포인트가_있다() -> None:
    """🔴 **없으면 수금일에 아무도 안 부른다** — `apply_explicit_collection` 이 지금 그렇다."""
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/collect" in paths, (
        f"수금 진입점이 없다. 있는 경로: {sorted(p for p in paths if 'days' in p)}"
    )


def test_개장_입고_수금이_다_다른_엔드포인트다() -> None:
    """★ **사건 셋은 자리 셋이다.** 하나로 묶으면 실패 조합을 한 응답으로 못 낸다.

    ```text
    개장 성공 · 입고 BLOCKED · 수금 NOTHING_DUE   ← 이것을 한 status 로 어떻게 적나
    ```
    """
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/open" in paths
    assert "/master/days/{as_of}/receive" in paths
    assert "/master/days/{as_of}/collect" in paths


def test_엔드포인트가_실패도_200_으로_낸다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `/days/{as_of}/receive` 와 같은 태도 — 막힌 것은 오류가 아니라 **그날의 사실**이다."""
    monkeypatch.setattr(collection, "check_day_gate", _막힌_Gate)
    collection.register_collection("finance", _재무())

    import app.main

    client = TestClient(app.main.app)
    resp = client.post(f"/master/days/{AS_OF.isoformat()}/collect")

    assert resp.status_code == 200
    assert resp.json()["status"] == "NOT_OPENED"
    assert resp.json()["next_action"] == "OPEN_DAY_REQUIRED"


# ── ⑨ 원문 규율 ───────────────────────────────────────────────────────────


def test_판단_경로가_수금을_부작용으로_돌리지_않는다() -> None:
    """🔴 **`run_procurement` 이 수금을 부르면 판단 한 번이 현금을 움직인다.**

    *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다 — 개장·입고를 판단 밖에 둔
    이유와 같고, 현금이라 더 그렇다. 원문을 읽어 잠근다.
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
    assert "collect_receipts" not in called, (
        "판단 경로가 수금을 부른다 — 수금은 사건이라 자기 자리가 있어야 한다"
    )


def test_수금이_하루를_열지_않는다() -> None:
    """🔴 **여기서 `open_day` 를 부르면 수금이 개장의 부작용이 된다.**

    `check_day_gate` 는 묻기만 한다. 원문으로 잠근다.
    """
    import ast
    import inspect as _inspect

    src = _inspect.getsource(collection.collect_receipts)
    tree = ast.parse(src.lstrip())
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "open_day" not in called, "수금이 하루를 연다 — 개장은 명시적 사건이어야 한다"
    assert "check_day_gate" in called, "Gate 를 안 본다 — 순서가 다시 문장으로만 남는다"


def test_실행일_달력을_안_쓴다() -> None:
    """🔴 **원문을 잠근다.** `next_execution_day` 를 부르는 순간 토요일 수금이 월요일로
    밀리고, 그건 조용히 틀린다."""
    import pathlib

    원문 = pathlib.Path(collection.__file__).read_text(encoding="utf-8")
    코드 = "\n".join(
        line for line in 원문.splitlines() if not line.strip().startswith(("#", "*", "```"))
    )

    assert "next_execution_day" not in 코드
    assert "is_execution_day" not in 코드
