"""채권 발행 경계 — **판매 확정이 채권을 만드는 자리** (2026-09-09 · 판매·재무 결정).

🔴 **부르는 곳이 0건이었다** (실측 2026-09-09).

```text
app/finance/receivables.py :46   confirm_receivable(conn, ReceivableCreateInput(...))
production 호출자 0건 — 검사 파일이 전부다

haetdeul.sales   2026-01-05 · 01-07 · 01-09  CONFIRMED→DELIVERED  총 297,000원
haetdeul.receivables 에 그 셋의 채권   **0행**   ← 배선이 없어서
```

★ 판매·재무 결정 2026-09-09.

```text
판매 확정 → receivables_krw += 원금   (issued_date = sale_date)
수금      → current_cash_krw += · receivables_krw −=

"Sales 에서 Finance 를 직접 호출하지 않는다.
 Master 가 확정 트랜잭션을 잇는 위치에서 Finance boundary 를 호출하는 것이 맞다."
```

★ 이 파일이 잠그는 것은 여덟이다.

```text
① 앞의 다섯 등록소와 섞이지 않는다   한 사전이면 "수금은 되는데 채권은 안 섬"을 못 적는다
② 미등록과 "확정 판매 없음" 이 다르다  뭉치면 재무 구현체가 빠진 날 조용히 성공한다
③ 트랜잭션을 마스터가 쥔다            실패하면 아무것도 안 바뀐다
④ 예외를 밖으로 안 낸다               발행 실패가 그날을 통째로 세우면 안 된다
⑤ 개장 Gate 를 코드가 본다            안 열린 날은 묻지도 않는다
⑥ 다섯 어휘가 각각 나오고 안 섞인다    ISSUED · NOTHING_DUE · BLOCKED · NOT_OPENED · FAILED
⑦ 장부 관문이 채권도 본다             receivables_krw 는 매입 cap 이 보는 값이다
⑧ 성적표가 채권을 적는다              값이 있는데 화면이 안 읽으면 없는 것과 같다
```

⚠️ **여기 재무는 가짜다.** 이 파일이 재는 것은 *"등록되면 어떻게 도는가"* 이지 재무
  구현이 아니다. 실물을 끌어오면 이 여덟 규율이 재무 사정에 따라 깨지고, 그때
  **무엇이 틀렸는지 이 파일이 못 말한다.** 실 배선은
  `tests/master/test_receivable_registration.py` 가 잰다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.master import receivable
from app.master.day_gate import DayGate
from app.master.receivable import ReceivablePartOut, issue_receivables

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
    def __init__(self, *, out: ReceivablePartOut | None = None, raises: Exception | None = None):
        self.calls: list[date] = []
        self._out = out or ReceivablePartOut(part="finance", status="NOTHING_DUE")
        self._raises = raises

    def issue(self, conn: Any, *, as_of: date) -> ReceivablePartOut:
        self.calls.append(as_of)
        if self._raises is not None:
            raise self._raises
        return self._out


def _세움(*ids: str, created: int | None = None) -> ReceivablePartOut:
    return ReceivablePartOut(
        part="finance",
        status="ISSUED",
        issued=list(ids),
        created=len(ids) if created is None else created,
    )


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
    """🔴 **이 파일은 채권 경계를 잰다 — 개장 상태를 재지 않는다.**

    `issue_receivables` 가 앞에 개장 Gate 를 두면서 이 파일의 검사가 **실 DB 의
    `master_day_openings` 에 매이게** 됐다. `test_collection.py` · `test_inbound.py`
    가 같은 이유로 같은 전제를 둔다.

    ⚠️ **Gate 자체는 아래 `⑤` 절이 잰다.** 거기서는 이 fixture 를 다시 덮어쓴다.
    """
    monkeypatch.setattr(
        receivable,
        "check_day_gate",
        lambda as_of, connect=None: DayGate(as_of=as_of, gate="PASS", result="ALREADY_OPENED"),
    )


@pytest.fixture(autouse=True)
def _등록소를_되돌린다() -> Any:
    """검사가 등록한 대역이 다음 검사로 새지 않게 한다."""
    before = dict(receivable.registered())
    receivable.reset()
    yield
    receivable.reset()
    for part, impl in before.items():
        receivable.register_receivable(part, impl)


# ── ① 앞의 다섯 등록소와 섞이지 않는다 ────────────────────────────────────


def test_여섯째_등록소는_앞의_다섯과_따로다():
    """🔴 **한 사전에 섞으면 *"수금은 되는데 채권은 안 서는"* 상태를 못 적는다.**

    ★ **지금이 정확히 그 상태다** — 수금 배선은 `#427` 로 섰고 채권 배선은 없었다.
    """
    from app.master import cancellation, collection, day_open, inbound, transition

    receivable.register_receivable("finance", _재무())

    assert "finance" in receivable.registered()
    assert receivable.missing() == ()
    # 앞의 등록소들은 이 등록에 영향받지 않는다
    assert set(day_open.PARTS) == {"finance", "logistics"}
    assert set(inbound.PARTS) == {"logistics"}
    assert set(collection.PARTS) == {"finance"}
    assert set(receivable.PARTS) == {"finance"}
    assert "finance" not in inbound.registered(), "입고 등록소에 재무가 새어 들어갔다"
    # 🔴 **여섯이 여섯 개의 다른 사전이다.** 채권을 등록해도 앞의 다섯에는 안 뜬다 —
    #    한 사전이면 여기 등록한 대역이 저쪽 `missing()` 을 조용히 채운다.
    앞의_다섯 = {
        "transition": transition.registered(),
        "day_open": day_open.registered(),
        "cancellation": cancellation.registered_cancellations(),
        "inbound": inbound.registered(),
        "collection": collection.registered(),
    }
    대역 = receivable.registered()["finance"]
    for 이름, 등록 in 앞의_다섯.items():
        assert 대역 not in 등록.values(), f"{이름} 등록소에 채권 대역이 새어 들어갔다"


def test_채권_발행_파트는_재무_하나다():
    """★ 판매는 확정 사실의 원천이지 채권 원장을 갖지 않는다."""
    with pytest.raises(ValueError, match="채권 발행 파트가 아니다"):
        receivable.register_receivable("sales", _재무())  # type: ignore[arg-type]


# ── ② 미등록과 "확정 판매 없음" 은 다른 사실이다 ──────────────────────────


def test_미등록이면_사유가_남는다():
    """⚠️ 뭉치면 배선이 빠진 날 매일 *"오늘은 판 게 없었다"* 로 보인다."""
    conn = _가짜커넥션()

    out = issue_receivables(AS_OF, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == ["finance"]
    assert "미등록" in out.reason
    assert conn.committed == 0, "커넥션을 열지도 말아야 한다"


def test_확정_판매가_없는_것은_미등록이_아니다():
    """★ 둘 다 `NOTHING_DUE` 지만 `missing` 과 `reason` 이 가른다."""
    receivable.register_receivable("finance", _재무())
    conn = _가짜커넥션()

    out = issue_receivables(AS_OF, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == [], "등록은 돼 있다"
    assert out.reason == ""
    assert out.parts[0].status == "NOTHING_DUE"
    assert conn.committed == 1, "물어보기는 했다"


# ── ③ 트랜잭션 ────────────────────────────────────────────────────────────


def test_채권을_세우면_한_번_커밋한다():
    receivable.register_receivable("finance", _재무(out=_세움("AR-SALE-1")))
    conn = _가짜커넥션()

    out = issue_receivables(AS_OF, connect=lambda: conn)

    assert out.status == "ISSUED"
    assert out.parts[0].issued == ["AR-SALE-1"]
    assert conn.committed == 1
    assert conn.rolled_back == 0
    assert conn.closed == 1


def test_터지면_통째로_롤백한다():
    """🔴 반쯤 서면 **채권은 늘었는데 `finance_states` 는 그대로인** 장부가 된다."""
    receivable.register_receivable("finance", _재무(raises=RuntimeError("축이 안 맞는다")))
    conn = _가짜커넥션()

    out = issue_receivables(AS_OF, connect=lambda: conn)

    assert out.status == "FAILED"
    assert "축이 안 맞는다" in out.reason
    assert conn.committed == 0
    assert conn.rolled_back == 1
    assert conn.closed == 1


# ── ④ 예외를 밖으로 안 낸다 ───────────────────────────────────────────────


def test_실패해도_예외가_안_오른다():
    """⚠️ *"예외를 안 올린다"* 가 *"그러니 판단을 계속한다"* 는 아니다 — `⑦` 이 그 답이다."""
    receivable.register_receivable("finance", _재무(raises=RuntimeError("boom")))

    out = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "FAILED"
    assert out.parts == [], "실패했으면 파트 결과를 내지 않는다"


# ── ⑤ 개장 Gate 를 코드가 본다 ────────────────────────────────────────────


def test_안_열린_날은_채권을_세우지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(receivable, "check_day_gate", _막힌_Gate)
    재무 = _재무()
    receivable.register_receivable("finance", 재무)

    out = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "NOT_OPENED"
    assert 재무.calls == [], "장부가 안 열렸는데 파트를 불렀다"


def test_안_열린_것을_BLOCKED_로_접지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **고칠 곳이 완전히 다른데 화면은 같아 보인다.**

    ```text
    BLOCKED      세울 대상이 있는데 **그 건이** 발행 불가 — sale_id 가 나온다
    NOT_OPENED   **아직 아무것도 안 봤다** — 장부가 없어 물어보지도 못했다
    ```
    """
    monkeypatch.setattr(receivable, "check_day_gate", _막힌_Gate)
    receivable.register_receivable("finance", _재무())

    out = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status != "NOTHING_DUE"
    assert out.status != "BLOCKED", "안 열린 것을 BLOCKED 로 접었다"
    assert out.status == "NOT_OPENED"
    assert out.next_action == "OPEN_DAY_REQUIRED", "다음에 할 일을 안 실었다"
    assert out.reason == "한 번도 열린 적이 없다", "개장이 낸 사유를 다시 지었다"


def test_채권_발행이_하루를_열지_않는다() -> None:
    """🔴 **여기서 `open_day` 를 부르면 발행이 개장의 부작용이 된다.** 원문으로 잠근다."""
    import ast
    import inspect as _inspect

    src = _inspect.getsource(receivable.issue_receivables)
    tree = ast.parse(src.lstrip())
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "open_day" not in called, "채권 발행이 하루를 연다"
    assert "check_day_gate" in called, "Gate 를 안 본다 — 순서가 다시 문장으로만 남는다"


def test_판단_경로가_채권_발행을_부작용으로_돌리지_않는다() -> None:
    """🔴 **`run_procurement` 이 이것을 부르면 판단 한 번이 채권 잔액을 움직인다.**"""
    import ast
    import inspect as _inspect

    from app.master import service

    tree = ast.parse(_inspect.getsource(service))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "issue_receivables" not in called, "판단 경로가 채권 발행을 부른다"


def test_실행일_달력을_안_쓴다() -> None:
    """🔴 **원문을 잠근다.** `sales.sale_date` 가 정본이고 마스터가 그 날짜를 밀지 않는다."""
    import pathlib

    원문 = pathlib.Path(receivable.__file__).read_text(encoding="utf-8")
    코드 = "\n".join(
        line for line in 원문.splitlines() if not line.strip().startswith(("#", "*", "```"))
    )

    assert "next_execution_day" not in 코드
    assert "is_execution_day" not in 코드


def test_실행일_달력으로_as_of_를_보정하지_않는다():
    """★ 토요일에 확정된 판매의 채권이 월요일 장부에 서면 안 된다."""
    assert 토요일.weekday() == 5
    재무 = _재무(out=_세움("AR-SAT-1"))
    receivable.register_receivable("finance", 재무)

    out = issue_receivables(토요일, connect=lambda: _가짜커넥션())

    assert out.status == "ISSUED"
    assert 재무.calls == [토요일], "실행일 달력으로 밀었다"


# ── ⑥ 다섯 어휘가 각각 나오고 안 섞인다 ───────────────────────────────────


def test_다섯_어휘가_각각_나오는_길이_있다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **자기 생존 검사다.** 어휘를 선언만 해 두고 아무 길도 안 내면, 그 어휘는
    타입 힌트로만 존재하고 **화면에는 영원히 안 나온다.**

    ⚠️ 이 검사는 0건을 세고 초록이 될 수 없다 — 다섯을 **다 모아** 비교한다.
    """
    본_것: set[str] = set()

    # ① NOT_OPENED — 장부가 안 열렸다
    monkeypatch.setattr(receivable, "check_day_gate", _막힌_Gate)
    receivable.register_receivable("finance", _재무())
    본_것.add(issue_receivables(AS_OF, connect=lambda: _가짜커넥션()).status)

    monkeypatch.setattr(
        receivable,
        "check_day_gate",
        lambda as_of, connect=None: DayGate(as_of=as_of, gate="PASS", result="OPENED"),
    )
    # ② NOTHING_DUE — 그날 확정된 판매가 없다
    receivable.register_receivable("finance", _재무())
    본_것.add(issue_receivables(AS_OF, connect=lambda: _가짜커넥션()).status)

    # ③ ISSUED — 대상이 있었고 채권이 서 있다
    receivable.register_receivable("finance", _재무(out=_세움("AR-SALE-1")))
    본_것.add(issue_receivables(AS_OF, connect=lambda: _가짜커넥션()).status)

    # ④ BLOCKED — 대상은 있는데 못 세운다
    receivable.register_receivable(
        "finance",
        _재무(out=ReceivablePartOut(part="finance", status="BLOCKED", reason="기일이 없다")),
    )
    본_것.add(issue_receivables(AS_OF, connect=lambda: _가짜커넥션()).status)

    # ⑤ FAILED — 세워 보다 터졌다
    receivable.register_receivable("finance", _재무(raises=RuntimeError("boom")))
    본_것.add(issue_receivables(AS_OF, connect=lambda: _가짜커넥션()).status)

    assert 본_것 == {"ISSUED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED"}, (
        f"어휘 다섯 중 길이 없는 것이 있다: 나온 것 {sorted(본_것)}"
    )


def test_BLOCKED_를_NOTHING_DUE_로_접지_않는다():
    """🔴 **`inbound.py` 의 그 규칙 그대로다** (물류 지적 2026-09-06).

    ```text
    NOTHING_DUE   실제로 그날 확정된 판매가 없음
    BLOCKED       확정 판매는 존재하지만 채권을 못 세움
    ```

    ⚠️ 접으면 **`receivables_krw` 가 적게 잡힌 채로 매입 판단이 돈다.**
    """
    막힘 = ReceivablePartOut(
        part="finance", status="BLOCKED", reason="SALE-1: collection_due_date 가 비어 있다"
    )
    receivable.register_receivable("finance", _재무(out=막힘))

    out = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "BLOCKED"
    assert out.status != "NOTHING_DUE", "세울 게 있는데 없다고 말했다"
    assert "막혔다" in out.reason
    assert "collection_due_date" in out.parts[0].reason, "무엇이 막았는지가 사라졌다"


def test_세운_것이_있어도_막힌_것이_있으면_BLOCKED_다():
    """★ *"세울 게 있었는데 못 세웠다"* 가 *"세웠다"* 보다 **먼저 알려야 하는 사실**이다."""
    둘 = ReceivablePartOut(
        part="finance", status="BLOCKED", issued=["AR-SALE-1"], created=1, reason="한 건이 막혔다"
    )
    receivable.register_receivable("finance", _재무(out=둘))

    out = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "BLOCKED"
    assert out.parts[0].issued == ["AR-SALE-1"], "선 것까지 지우지는 않는다"


def test_ISSUED_인데_새로_만든_건수가_0_일_수_있다():
    """🔴 **멱등이라 이미 있으면 만든 건수가 0 이다. 그래도 채권은 서 있다.**

    ⚠️ **`issued` 와 `created` 를 한 값으로 접으면 안 된다.** 접으면
      *"이미 있어서 안 만들었다"* 와 *"대상이 없었다"* 가 같아 보이고, 두 번째 걸음이
      매일 *"아무것도 안 했다"* 로 읽힌다.
    """
    두번째_걸음 = _세움("AR-SALE-1", "AR-SALE-2", created=0)
    receivable.register_receivable("finance", _재무(out=두번째_걸음))

    out = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())

    assert out.status == "ISSUED", "이미 서 있는 것을 NOTHING_DUE 로 접었다"
    assert out.parts[0].created == 0
    assert len(out.parts[0].issued) == 2, "서 있는 채권을 안 실었다"
    # 🔴 대상이 없던 날과 **다른 값**이어야 한다.
    receivable.register_receivable("finance", _재무())
    없던_날 = issue_receivables(AS_OF, connect=lambda: _가짜커넥션())
    assert 없던_날.status != out.status, "두 사실이 같은 status 로 나간다"


# ── 진입점 ────────────────────────────────────────────────────────────────


def test_채권_발행_엔드포인트가_있다() -> None:
    """🔴 **없으면 판매 확정일에 아무도 안 부른다** — `confirm_receivable` 이 그랬다."""
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/issue-receivables" in paths, (
        f"채권 진입점이 없다. 있는 경로: {sorted(p for p in paths if 'days' in p)}"
    )


def test_장부를_바꾸는_사건_넷이_다_다른_엔드포인트다() -> None:
    """★ **사건 넷은 자리 넷이다.** 하나로 묶으면 실패 조합을 한 응답으로 못 낸다.

    ```text
    개장 성공 · 입고 BLOCKED · 채권 ISSUED · 수금 NOTHING_DUE ← 한 status 로 어떻게 적나
    ```
    """
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/open" in paths
    assert "/master/days/{as_of}/receive" in paths
    assert "/master/days/{as_of}/issue-receivables" in paths
    assert "/master/days/{as_of}/collect" in paths


def test_엔드포인트가_부르는_함수가_하루_실행이_부르는_함수와_같다() -> None:
    """🔴 **둘이 갈리면 손으로 부른 결과와 걷기 결과가 다른 코드를 지난다.**"""
    import inspect as _inspect

    from app.master import router as router_module
    from app.master import scheduler

    사람_경로 = _inspect.getsource(router_module.master_issue_receivables)
    assert "run_issue_receivables(as_of)" in 사람_경로

    assert router_module.run_issue_receivables is receivable.issue_receivables
    assert (
        _inspect.signature(scheduler.run_scheduled_day).parameters["issue_fn"].default
        is receivable.issue_receivables
    ), "하루 실행이 다른 함수를 부른다"


def test_엔드포인트가_실패도_200_으로_낸다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 막힌 것은 오류가 아니라 **그날의 사실**이다."""
    monkeypatch.setattr(receivable, "check_day_gate", _막힌_Gate)
    receivable.register_receivable("finance", _재무())

    import app.main

    client = TestClient(app.main.app)
    resp = client.post(f"/master/days/{AS_OF.isoformat()}/issue-receivables")

    assert resp.status_code == 200
    assert resp.json()["status"] == "NOT_OPENED"
    assert resp.json()["next_action"] == "OPEN_DAY_REQUIRED"


# ── ⑦ 장부 관문이 채권도 본다 ─────────────────────────────────────────────
#
# 🔴 **채권이 안 서면 `receivables_krw` 가 적게 잡히고, 그것은 매입 cap 이 보는
#    값이다.** `_ledger_gap` 주석의 *"어느 쪽이 틀려도 에러 없이 틀린 답이 나온다"*
#    가 그대로 세 번째 축에도 적용된다.


class _Out:
    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason


class _Spy:
    def __init__(self, out: Any = None, *, boom: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self.out = out
        self.boom = boom

    def __call__(self, arg, *args, **kwargs):
        self.calls.append(arg)
        if self.boom is not None:
            raise self.boom
        return self.out


class _Procure:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request, verifier=None):
        self.requests.append(request)
        return _Out("RAN")


def _하루(**kwargs) -> Any:
    from app.master.scheduler import ScheduledAction, run_scheduled_day

    defaults: dict[str, Any] = {
        "open_day_fn": _Spy(_Out("OPENED")),
        "receive_fn": _Spy(_Out("RECEIVED")),
        "issue_fn": _Spy(_Out("ISSUED")),
        "collect_fn": _Spy(_Out("COLLECTED")),
        "procure_fn": _Procure(),
        "outbound_fn": _Spy(_Out("NOTHING_DUE")),
        # ⚠️ **계약이 아는 품목이어야 한다.** 아무 문자열이나 주면
        #   `ProcurementRunRequest` 가 거절하고, 그 거절이 `FAILED` 로 잡혀
        #   *"판단이 안 돌았다"* 와 구별이 안 된다.
        "items": ("배추",),
    }
    defaults.update(kwargs)
    from datetime import datetime

    from app.master.clock import SEOUL

    action = ScheduledAction(
        as_of=AS_OF,
        now=datetime(2026, 1, 7, 9, 30, tzinfo=SEOUL),
        action="RUN_AND_RECORD",
        reason="검사",
        deadline=datetime(2026, 1, 7, 10, 30, tzinfo=SEOUL),
    )
    assert action.should_run, "전제가 깨졌다 — 이 검사는 도는 날을 재려던 것이다"
    return run_scheduled_day(action, **defaults), defaults["procure_fn"]


@pytest.mark.parametrize("막힌상태", ["BLOCKED", "FAILED"])
def test_채권이_막히면_판단을_안_돌린다(막힌상태: str) -> None:
    """🔴 `receivables_krw` 가 실제보다 적게 반영된 채로 매입 cap 이 계산된다."""
    out, procure = _하루(issue_fn=_Spy(_Out(막힌상태, "세울 게 있는데 막혔다")))

    assert out.receivable_status == 막힌상태
    assert procure.requests == [], "장부가 안 섰는데 판단이 돌았다"
    assert out.procurement_status == "NOT_ATTEMPTED"


def test_채권이_터져도_판단을_안_돌린다() -> None:
    """★ `_stage` 가 예외를 `FAILED` 로 옮기고, 그 `FAILED` 도 막는 축이다."""
    out, procure = _하루(issue_fn=_Spy(boom=RuntimeError("재무가 죽었다")))

    assert out.receivable_status == "FAILED"
    assert procure.requests == []


def test_채권_NOTHING_DUE_는_판단을_안_막는다() -> None:
    """🔴 **막으면 대부분의 날이 멈춘다.** *"확인했고 세울 것이 없다"* 는 정상이다."""
    out, procure = _하루(issue_fn=_Spy(_Out("NOTHING_DUE", "오늘 확정된 판매가 없다")))

    assert out.receivable_status == "NOTHING_DUE"
    assert len(procure.requests) == 1, "정상인데 판단을 안 돌렸다"
    assert out.procurement_status == "RAN"


def test_관문_사유가_셋을_다_적는다() -> None:
    """⚠️ 하나만 적으면 사람이 물류를 볼지 판매를 볼지 재무를 볼지 모른다."""
    out, _ = _하루(issue_fn=_Spy(_Out("BLOCKED", "기일이 없다")))

    관문 = [note for note in out.notes if "장부가 안 서서" in note]
    assert len(관문) == 1, f"관문 사유가 없다: {out.notes}"
    assert "입고: RECEIVED" in 관문[0]
    assert "채권: BLOCKED" in 관문[0], f"채권 상태가 사유에 없다: {관문[0]}"
    assert "수금: COLLECTED" in 관문[0]


def test_채권이_수금보다_앞이다() -> None:
    """🔴 **채권이 서야 수금할 것이 있다.** 순서가 계약이다."""
    순서: list[str] = []

    def 기록(이름: str, status: str):
        def _부른다(as_of, *args, **kwargs):
            순서.append(이름)
            return _Out(status)

        return _부른다

    _하루(
        receive_fn=기록("입고", "RECEIVED"),
        issue_fn=기록("채권", "ISSUED"),
        collect_fn=기록("수금", "COLLECTED"),
    )

    assert 순서 == ["입고", "채권", "수금"], f"하루 순서가 계약과 다르다: {순서}"


# ── ⑧ 성적표가 채권을 적는다 ──────────────────────────────────────────────
#
# ⚠️ **값이 있는데 성적표가 안 읽으면 없는 것과 같다** — `#446` 이 출고에 한 것과
#    같은 방식이다.


def test_성적표가_채권_분포를_적는다() -> None:
    """🔴 **다섯 값을 접지 않고 그대로 센다.**"""
    from app.master.backtest_runner import WalkResult, format_summary
    from app.master.scheduler import DayRunOutcome

    def _날(as_of: date, status: str) -> DayRunOutcome:
        return DayRunOutcome(
            as_of=as_of, action="RUN_AND_RECORD", reason="", receivable_status=status
        )

    result = WalkResult(
        start=date(2026, 1, 5),
        end=date(2026, 1, 8),
        days=(
            _날(date(2026, 1, 5), "ISSUED"),
            _날(date(2026, 1, 6), "NOTHING_DUE"),
            _날(date(2026, 1, 7), "BLOCKED"),
            _날(date(2026, 1, 8), "FAILED"),
        ),
    )

    assert dict(result.receivable_statuses) == {
        "ISSUED": 1,
        "NOTHING_DUE": 1,
        "BLOCKED": 1,
        "FAILED": 1,
    }, f"넷이 안 갈렸다: {dict(result.receivable_statuses)}"
    assert "채권" in format_summary(result), "성적표가 채권 줄을 안 찍는다"
    assert "ISSUED" in format_summary(result)


def test_성적표_사유가_채권_상태를_적는다() -> None:
    """★ 판단 단계를 안 탄 날, 어느 단계가 막았는지가 사유에 있어야 한다."""
    from app.master.backtest_runner import _incident_reason
    from app.master.scheduler import DayRunOutcome

    사유 = _incident_reason(
        DayRunOutcome(
            as_of=AS_OF,
            action="RUN_AND_RECORD",
            reason="",
            day_open_status="OPENED",
            inbound_status="NOTHING_DUE",
            receivable_status="BLOCKED",
            collection_status="NOTHING_DUE",
        ),
        ran=True,
    )

    assert 사유 is not None
    assert "채권: BLOCKED" in 사유, f"채권 상태가 사고 사유에서 사라졌다: {사유}"
