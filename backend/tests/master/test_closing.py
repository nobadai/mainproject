"""하루 마감 경계 — **하루가 닫히는 자리** (2026-09-10 · 일곱째 등록소).

🔴 **`daily_closings` 에 INSERT 하는 코드가 저장소 전체에 0곳이었다** (실측 2026-09-10).

```text
INSERT 하는 코드              0곳
읽는 곳                       master/ledger_repository · finance/dashboard · api/_agent_docs
행 30 · 2025-12-02 ~ 12-31    번인 시드 (sim_run_id = SIM-BURNIN-202512)
걷기 구간(2026-01~)           **0행**
```

★★ **걷기가 손익을 한 줄도 안 남겼다.** 하루를 닫는 자리가 없어서다.

★ 이 파일이 잠그는 것은 여덟이다.

```text
① 앞의 여섯 등록소와 섞이지 않는다   한 사전이면 "채권은 서는데 마감은 안 됨"을 못 적는다
② 미등록과 "닫을 것 없음" 이 다르다   뭉치면 재무 구현체가 빠진 날 조용히 성공한다
③ 트랜잭션을 마스터가 쥔다            실패하면 아무것도 안 바뀐다
④ 마감이 터져도 그날 걷기 결과가 그대로다
⑤ 개장 Gate 를 코드가 본다            안 열린 날은 묻지도 않는다
⑥ 다섯 어휘가 각각 나오고 안 섞인다    CLOSED · NOTHING_DUE · BLOCKED · NOT_OPENED · FAILED
⑦ 장부 관문이 막은 날은 BLOCKED       "마감이 없다" 와 "마감이 막혔다" 는 다르다
⑧ 마스터가 숫자를 계산하지 않는다      어댑터가 낸 값을 그대로 적는다
```

⚠️ **여기 재무는 가짜다.** 이 파일이 재는 것은 *"등록되면 어떻게 도는가"* 이지 재무
  구현이 아니다 — **재무 마감 구현은 아직 없다.** 실물을 끌어오면 이 여덟 규율이
  재무 사정에 따라 깨지고, 그때 **무엇이 틀렸는지 이 파일이 못 말한다.**

🔴 **이 판이 끝나도 손익 곡선은 여전히 빈다.** 재무 어댑터가 붙어야 찬다. 이 파일이
  잠그는 것은 **자리**이지 숫자가 아니다.
"""

from __future__ import annotations

import ast
import inspect as _inspect
import pathlib
import re
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.finance import closing_adapter as finance_closing_adapter
from app.finance.closing import FinanceDayClosingResult
from app.finance.closing_adapter import FinanceClosingAdapter
from app.master import closing
from app.master.clock import SEOUL
from app.master.closing import ClosingPartOut, close_day
from app.master.day_gate import DayGate

AS_OF = date(2026, 1, 7)
토요일 = date(2026, 1, 10)
축 = "SIM-TEST-CLOSING"


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
    """마감 대역. **`daily_closings` 의 PK 를 그대로 흉내 낸다.**

    🔴 **`ledger` 의 키가 `(sim_run_id, close_date)` 다** — 실 DDL 의 PK 그대로다.
      마스터가 여기에 지어낸 id 를 더하면 같은 날이 두 벌 쌓이고, 그것을 이 dict 가
      잡는다.
    """

    def __init__(
        self,
        *,
        out: ClosingPartOut | None = None,
        raises: Exception | None = None,
        ledger: dict[tuple[str, date], int] | None = None,
    ) -> None:
        self.calls: list[tuple[date, str]] = []
        self._out = out
        self._raises = raises
        self.ledger: dict[tuple[str, date], int] = {} if ledger is None else ledger

    def close(self, conn: Any, *, as_of: date, sim_run_id: str) -> ClosingPartOut:
        self.calls.append((as_of, sim_run_id))
        if self._raises is not None:
            raise self._raises
        if self._out is not None:
            return self._out
        # 🔴 **멱등이다.** 이미 있으면 새로 안 적는다 — 그래도 마감은 서 있다.
        키 = (sim_run_id, as_of)
        새로적음 = 0 if 키 in self.ledger else 1
        self.ledger[키] = self.ledger.get(키, 0) + 새로적음
        return ClosingPartOut(
            part="finance",
            status="CLOSED",
            closed=[f"{sim_run_id}:{as_of.isoformat()}"],
            created=새로적음,
        )


def _닫음(*keys: str, created: int | None = None) -> ClosingPartOut:
    return ClosingPartOut(
        part="finance",
        status="CLOSED",
        closed=list(keys),
        created=len(keys) if created is None else created,
    )


def test_finance_closing_adapter_passes_the_exact_master_axis(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def _finance_close_day(*, as_of: date, sim_run_id: str, conn: Any) -> FinanceDayClosingResult:
        seen.update(as_of=as_of, sim_run_id=sim_run_id, conn=conn)
        return FinanceDayClosingResult(
            part="finance",
            status="CLOSED",
            closed=[f"{sim_run_id}:{as_of.isoformat()}"],
            created=1,
        )

    monkeypatch.setattr(finance_closing_adapter, "close_day", _finance_close_day)
    conn = _가짜커넥션()

    out = FinanceClosingAdapter().close(conn, as_of=AS_OF, sim_run_id=축)

    assert seen == {"as_of": AS_OF, "sim_run_id": 축, "conn": conn}
    assert out == ClosingPartOut(
        part="finance", status="CLOSED", closed=[f"{축}:{AS_OF.isoformat()}"], created=1
    )


def test_master_closing_registry_calls_finance_adapter(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[date, str]] = []

    def _finance_close_day(*, as_of: date, sim_run_id: str, conn: Any) -> FinanceDayClosingResult:
        calls.append((as_of, sim_run_id))
        return FinanceDayClosingResult(part="finance", status="CLOSED", closed=["row"], created=1)

    monkeypatch.setattr(finance_closing_adapter, "close_day", _finance_close_day)
    closing.register_closing("finance", FinanceClosingAdapter())
    conn = _가짜커넥션()

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: conn)

    assert out.status == "CLOSED"
    assert calls == [(AS_OF, 축)]
    assert (conn.committed, conn.rolled_back, conn.closed) == (1, 0, 1)


def _코드만() -> str:
    """`closing.py` 에서 **주석과 문서만 걷어낸 코드**.

    🔴 **줄 앞머리로 거르면 안 된다.** 이 모듈은 docstring 안에 `INSERT` ·
      `daily_closings` · `BURN_IN_SIM_RUN_ID` 를 **일부러 적어 뒀다** — 왜 안 쓰는지를
      적은 문장이라 그것까지 걸리면 검사가 *"설명을 지우라"* 는 말이 된다.

    ★ `ast.unparse` 는 주석을 아예 안 싣는다. docstring 만 손으로 걷어낸다.
    """
    tree = ast.parse(pathlib.Path(closing.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _막힌_Gate(as_of: date, *, connect: Any = None, sim_run_id: str = "") -> DayGate:
    return DayGate(
        as_of=as_of,
        gate="BLOCKED",
        result="NEVER_OPENED",
        reason="한 번도 열린 적이 없다",
        next_action="OPEN_DAY_REQUIRED",
    )


@pytest.fixture(autouse=True)
def _열린_날로_둔다(monkeypatch: pytest.MonkeyPatch) -> Any:
    """🔴 **이 파일은 마감 경계를 잰다 — 개장 상태를 재지 않는다.**

    `close_day` 가 앞에 개장 Gate 를 두므로, 두지 않으면 이 파일의 검사가 **실 DB 의
    `master_day_openings` 에 매인다.** `test_receivable.py` · `test_collection.py` 가
    같은 이유로 같은 전제를 둔다.

    ⚠️ **Gate 자체는 아래 `⑤` 절이 잰다.** 거기서는 이 fixture 를 다시 덮어쓴다.
    """
    monkeypatch.setattr(
        closing,
        "check_day_gate",
        lambda as_of, connect=None, sim_run_id="": DayGate(
            as_of=as_of, gate="PASS", result="ALREADY_OPENED"
        ),
    )


@pytest.fixture(autouse=True)
def _등록소를_되돌린다() -> Any:
    """검사가 등록한 대역이 다음 검사로 새지 않게 한다."""
    before = dict(closing.registered())
    closing.reset()
    yield
    closing.reset()
    for part, impl in before.items():
        closing.register_closing(part, impl)


# ── ① 앞의 여섯 등록소와 섞이지 않는다 ────────────────────────────────────


def test_일곱째_등록소는_앞의_여섯과_따로다():
    """🔴 **한 사전에 섞으면 *"채권은 서는데 하루는 안 닫히는"* 상태를 못 적는다.**

    ★ **지금이 정확히 그 상태다** — 채권 배선은 `#489` 로 섰고 마감은 자리조차 없었다.
    """
    from app.master import cancellation, collection, day_open, inbound, receivable, transition

    closing.register_closing("finance", _재무())

    assert "finance" in closing.registered()
    assert closing.missing() == ()
    assert set(closing.PARTS) == {"finance"}
    assert "finance" not in inbound.registered(), "입고 등록소에 재무가 새어 들어갔다"
    # 🔴 **일곱이 일곱 개의 다른 사전이다.** 마감을 등록해도 앞의 여섯에는 안 뜬다 —
    #    한 사전이면 여기 등록한 대역이 저쪽 `missing()` 을 조용히 채운다.
    앞의_여섯 = {
        "transition": transition.registered(),
        "day_open": day_open.registered(),
        "cancellation": cancellation.registered_cancellations(),
        "inbound": inbound.registered(),
        "collection": collection.registered(),
        "receivable": receivable.registered(),
    }
    대역 = closing.registered()["finance"]
    for 이름, 등록 in 앞의_여섯.items():
        assert 대역 not in 등록.values(), f"{이름} 등록소에 마감 대역이 새어 들어갔다"


def test_마감_파트는_재무_하나다():
    """★ 물류가 재고를 알고 매입이 현금유출을 알지만, 셋을 한 줄로 만드는 것은 재무다."""
    with pytest.raises(ValueError, match="마감 파트가 아니다"):
        closing.register_closing("logistics", _재무())  # type: ignore[arg-type]


# ── ② 미등록과 "닫을 것 없음" 은 다른 사실이다 ────────────────────────────


def test_미등록이면_사유가_남는다():
    """🔴 **오늘이 이 길이다.** 재무 마감 어댑터가 아직 없다.

    ⚠️ 뭉치면 구현이 안 붙은 날 매일 *"오늘은 닫을 게 없었다"* 로 보인다.
    """
    conn = _가짜커넥션()

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == ["finance"]
    assert "미등록" in out.reason
    assert conn.committed == 0, "커넥션을 열지도 말아야 한다"


def test_미등록이어도_터지지_않는다():
    """🔴 **어댑터가 없으면 미등록으로 남는다 — 예외가 아니다.**"""
    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status == "NOTHING_DUE", "미등록이 예외나 FAILED 로 나갔다"


def test_닫을_움직임이_없는_것은_미등록이_아니다():
    """★ 둘 다 `NOTHING_DUE` 지만 `missing` 과 `reason` 이 가른다."""
    closing.register_closing(
        "finance", _재무(out=ClosingPartOut(part="finance", status="NOTHING_DUE"))
    )
    conn = _가짜커넥션()

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: conn)

    assert out.status == "NOTHING_DUE"
    assert out.missing == [], "등록은 돼 있다"
    assert out.reason == ""
    assert out.parts[0].status == "NOTHING_DUE"
    assert conn.committed == 1, "물어보기는 했다"


# ── ③ 트랜잭션 ────────────────────────────────────────────────────────────


def test_닫으면_한_번_커밋한다():
    closing.register_closing("finance", _재무(out=_닫음("SIM-1:2026-01-07")))
    conn = _가짜커넥션()

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: conn)

    assert out.status == "CLOSED"
    assert out.parts[0].closed == ["SIM-1:2026-01-07"]
    assert conn.committed == 1
    assert conn.rolled_back == 0
    assert conn.closed == 1


def test_터지면_통째로_롤백한다():
    """🔴 반쯤 닫히면 **손익 곡선에 절반짜리 점이 확정값으로 앉는다.**"""
    closing.register_closing("finance", _재무(raises=RuntimeError("잔액을 못 읽는다")))
    conn = _가짜커넥션()

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: conn)

    assert out.status == "FAILED"
    assert "잔액을 못 읽는다" in out.reason
    assert conn.committed == 0
    assert conn.rolled_back == 1
    assert conn.closed == 1


# ── ③-b 같은 날을 두 번 걸어도 두 벌이 안 쌓인다 ──────────────────────────


def test_daily_closings_의_키가_sim_run_id_와_close_date_다():
    """🔴 **키를 확인하고 적었다** — `master_agent_runs` 에서 겪은 그것 때문이다.

    ⚠️ 그 표는 UNIQUE 가 있는데도 `run_id` 가 PK 라 같은 `request_id` 를 두 번 넣어도
      안 막혔다. **제약이 있다는 말과 그 제약이 내가 넣는 키를 막는다는 말은 다르다.**

    ★ 그래서 이 검사가 DDL 을 다시 읽는다 — 이 축이 바뀌는 날 마스터가 어댑터에게
      주는 두 값도 같이 바뀌어야 한다.
    """
    repo = pathlib.Path(closing.__file__).parent.parent.parent.parent
    ddl = (repo / "database" / "10_domain_schema.sql").read_text(encoding="utf-8")

    match = re.search(
        r"ADD CONSTRAINT daily_closings_pkey PRIMARY KEY \(([^)]*)\)", ddl, re.IGNORECASE
    )
    assert match is not None, "10_domain_schema.sql 에 daily_closings 의 PK 가 없다"
    축들 = tuple(c.strip() for c in match.group(1).split(","))
    assert 축들 == ("sim_run_id", "close_date"), f"마감 키가 바뀌었다: {축들}"


def test_마스터가_어댑터에게_주는_것이_그_키_둘이다():
    """🔴 **마스터가 `closing_id` 같은 것을 지어내면 같은 날이 두 벌 쌓인다.**"""
    파라미터 = _inspect.signature(closing.ClosingPort.close).parameters

    assert set(파라미터) == {"self", "conn", "as_of", "sim_run_id"}, (
        f"마감 Port 가 키 밖의 것을 받는다: {sorted(파라미터)}"
    )


def test_같은_날을_두_번_걸어도_두_벌이_안_쌓인다():
    """🔴 **마감 행은 하나다. 두 번째는 `CLOSED` 인데 새로 적은 건수가 0 이다.**"""
    재무 = _재무()
    closing.register_closing("finance", 재무)

    첫째 = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())
    둘째 = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert 첫째.status == "CLOSED"
    assert 둘째.status == "CLOSED", "이미 닫힌 것을 NOTHING_DUE 로 접었다"
    assert len(재무.ledger) == 1, f"같은 날이 두 벌 쌓였다: {재무.ledger}"
    assert 첫째.parts[0].created == 1
    assert 둘째.parts[0].created == 0, "두 번째 걸음이 새 행을 적은 것처럼 보인다"
    assert 둘째.parts[0].closed == [f"{축}:{AS_OF.isoformat()}"], "서 있는 마감을 안 실었다"


def test_한_번_부르고_어댑터의_멱등에_기대지_않는다():
    """🔴 **두 번 부르고 `ON CONFLICT` 가 받아 주기를 기대하는 것이 그 기대다.**

    ⚠️ `master_agent_runs` 가 정확히 그렇게 틀렸다 — 제약을 믿고 두 번 넣었는데
      그 제약이 내가 넣는 키를 안 막았다.
    """
    재무 = _재무()
    closing.register_closing("finance", 재무)

    close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert 재무.calls == [(AS_OF, 축)], f"파트를 한 번보다 많이 불렀다: {재무.calls}"


# ── ④ 마감이 터져도 그날 걷기 결과가 그대로다 ─────────────────────────────


def test_실패해도_예외가_안_오른다():
    """★ 이력 때문에 운영이 멈추면 안 된다 — `try_save_run` 이 `try_` 인 이유와 같다."""
    closing.register_closing("finance", _재무(raises=RuntimeError("boom")))

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status == "FAILED"
    assert out.parts == [], "실패했으면 파트 결과를 내지 않는다"


# ── ⑤ 개장 Gate 를 코드가 본다 ────────────────────────────────────────────


def test_안_열린_날은_닫지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(closing, "check_day_gate", _막힌_Gate)
    재무 = _재무()
    closing.register_closing("finance", 재무)

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status == "NOT_OPENED"
    assert 재무.calls == [], "장부가 안 열렸는데 파트를 불렀다"


def test_안_열린_것을_BLOCKED_로_접지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **고칠 곳이 완전히 다른데 화면은 같아 보인다.**

    ```text
    BLOCKED      **장부가 안 섰다** — 입고·채권·수금 중 무엇이 막았는지가 사유에 있다
    NOT_OPENED   **아직 아무것도 안 봤다** — 하루가 안 열려 물어보지도 못했다
    ```
    """
    monkeypatch.setattr(closing, "check_day_gate", _막힌_Gate)
    closing.register_closing("finance", _재무())

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status != "NOTHING_DUE"
    assert out.status != "BLOCKED", "안 열린 것을 BLOCKED 로 접었다"
    assert out.status == "NOT_OPENED"
    assert out.next_action == "OPEN_DAY_REQUIRED", "다음에 할 일을 안 실었다"
    assert out.reason == "한 번도 열린 적이 없다", "개장이 낸 사유를 다시 지었다"


def test_안_열린_날은_관문_사유보다_앞이다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **판정 순서가 계약이다.** 하루가 안 열린 날은 장부 관문에 오지도 않는다."""
    monkeypatch.setattr(closing, "check_day_gate", _막힌_Gate)
    closing.register_closing("finance", _재무())

    out = close_day(
        AS_OF, sim_run_id=축, ledger_gap="장부가 안 서서", connect=lambda: _가짜커넥션()
    )

    assert out.status == "NOT_OPENED", "안 열린 날이 BLOCKED 로 접혔다"


def test_마감이_하루를_열지_않는다() -> None:
    """🔴 **여기서 `open_day` 를 부르면 마감이 개장의 부작용이 된다.** 원문으로 잠근다."""
    src = _inspect.getsource(closing.close_day)
    tree = ast.parse(src.lstrip())
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "open_day" not in called, "마감이 하루를 연다"
    assert "check_day_gate" in called, "Gate 를 안 본다 — 순서가 다시 문장으로만 남는다"


def test_판단_경로가_마감을_부작용으로_돌리지_않는다() -> None:
    """🔴 **`run_procurement` 이 이것을 부르면 판단 한 번이 그날을 닫는다.**"""
    from app.master import service

    tree = ast.parse(_inspect.getsource(service))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "close_day" not in called, "판단 경로가 마감을 부른다"


def test_실행일_달력을_안_쓴다() -> None:
    """🔴 **원문을 잠근다.** 마감은 달력일이다 — 토요일도 닫힌다."""
    코드 = _코드만()

    assert "next_execution_day" not in 코드
    assert "is_execution_day" not in 코드


def test_실행일_달력으로_as_of_를_보정하지_않는다():
    """★ 토요일 마감이 월요일 행에 앉으면 안 된다."""
    assert 토요일.weekday() == 5
    재무 = _재무()
    closing.register_closing("finance", 재무)

    out = close_day(토요일, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status == "CLOSED"
    assert 재무.calls == [(토요일, 축)], "실행일 달력으로 밀었다"


# ── ⑤-b sim_run_id 를 인자로 받아 흘린다 ──────────────────────────────────


def test_sim_run_id_를_인자로_받아_어댑터까지_흘린다():
    """🔴 **상수로 박지 않는다.** 박으면 실행이 둘이 되는 날 이 파일을 고쳐야 한다."""
    재무 = _재무()
    closing.register_closing("finance", 재무)

    close_day(AS_OF, sim_run_id="SIM-다른실행", connect=lambda: _가짜커넥션())

    assert 재무.calls == [(AS_OF, "SIM-다른실행")], f"축이 안 흘렀다: {재무.calls}"


def test_빈_축을_조용히_전체로_바꾸지_않는다():
    """⚠️ 빈 축은 그날의 사실이 아니라 **배선 사고**다.

    ★ 그래도 걷기는 안 멈춘다 — `scheduler._stage` 가 이것을 `FAILED` 로 옮긴다.
      (`⑦` 절의 `test_마감이_터져도_그날_걷기_결과가_그대로다` 가 그 길을 잰다.)
    """
    closing.register_closing("finance", _재무())

    with pytest.raises(ValueError, match="sim_run_id 없이"):
        close_day(AS_OF, sim_run_id="  ", connect=lambda: _가짜커넥션())


def test_마감_원문에_실행_축_상수가_박혀_있지_않다():
    """🔴 **값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID` 하나다.**"""
    코드 = _코드만()

    assert "BURN_IN_SIM_RUN_ID" not in 코드, "마감이 실행 축 상수를 스스로 든다"
    assert "SIM-BURNIN" not in 코드, "마감이 실행 축 문자열을 박아 뒀다"


# ── ⑥ 다섯 어휘가 각각 나오고 안 섞인다 ───────────────────────────────────


def test_다섯_어휘가_각각_나오는_길이_있다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **자기 생존 검사다.** 어휘를 선언만 해 두고 아무 길도 안 내면, 그 어휘는
    타입 힌트로만 존재하고 **화면에는 영원히 안 나온다.**

    ⚠️ 이 검사는 0건을 세고 초록이 될 수 없다 — 다섯을 **다 모아** 비교한다.
    """
    본_것: set[str] = set()

    # ① NOT_OPENED — 하루가 안 열렸다
    monkeypatch.setattr(closing, "check_day_gate", _막힌_Gate)
    closing.register_closing("finance", _재무())
    본_것.add(close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션()).status)

    monkeypatch.setattr(
        closing,
        "check_day_gate",
        lambda as_of, connect=None, sim_run_id="": DayGate(
            as_of=as_of, gate="PASS", result="OPENED"
        ),
    )
    # ② NOTHING_DUE — 그날 닫을 움직임이 없다
    closing.register_closing(
        "finance", _재무(out=ClosingPartOut(part="finance", status="NOTHING_DUE"))
    )
    본_것.add(close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션()).status)

    # ③ CLOSED — 닫았다
    closing.register_closing("finance", _재무(out=_닫음("SIM-1:2026-01-07")))
    본_것.add(close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션()).status)

    # ④ BLOCKED — 장부가 안 서서 못 닫는다
    본_것.add(
        close_day(
            AS_OF, sim_run_id=축, ledger_gap="장부가 안 서서", connect=lambda: _가짜커넥션()
        ).status
    )

    # ⑤ FAILED — 닫아 보다 터졌다
    closing.register_closing("finance", _재무(raises=RuntimeError("boom")))
    본_것.add(close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션()).status)

    assert 본_것 == {"CLOSED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED"}, (
        f"어휘 다섯 중 길이 없는 것이 있다: 나온 것 {sorted(본_것)}"
    )


def test_어휘_다섯을_채권에서_가져왔고_새로_만들지_않았다() -> None:
    """🔴 **새 어휘를 지어내지 않았다.** 채권 등록소가 쓰는 그 다섯이다."""
    from app.master.receivable import ReceivableOut

    def _값들(model: Any, field: str) -> set[str]:
        return set(model.model_fields[field].annotation.__args__)  # type: ignore[union-attr]

    채권 = _값들(ReceivableOut, "status")
    마감 = _값들(closing.ClosingOut, "status")

    # 🔴 **`ISSUED` ↔ `CLOSED` 한 낱말만 다르다.** 그 하나는 동사가 다르기 때문이고,
    #    나머지 넷은 **글자 그대로** 채권 것이다.
    assert 마감 - {"CLOSED"} == 채권 - {"ISSUED"}, (
        f"어휘가 갈렸다 — 마감 {sorted(마감)} · 채권 {sorted(채권)}"
    )


def test_BLOCKED_를_NOTHING_DUE_로_접지_않는다():
    """🔴 **«마감이 없다» 와 «마감이 막혔다» 는 다르다.**

    ⚠️ 접으면 손익 곡선의 빈 칸 두 종류가 화면에서 같아 보인다 — 앞은 고칠 것이
      있고 뒤는 없다.
    """
    closing.register_closing("finance", _재무())

    막힘 = close_day(
        AS_OF,
        sim_run_id=축,
        ledger_gap="장부가 안 서서 (입고: BLOCKED)",
        connect=lambda: _가짜커넥션(),
    )
    없음 = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert 막힘.status == "BLOCKED"
    assert 막힘.status != 없음.status, "두 사실이 같은 status 로 나간다"
    assert "입고: BLOCKED" in 막힘.reason, "무엇이 막았는지가 사라졌다"


def test_파트가_BLOCKED_면_전체도_BLOCKED_다():
    """★ *"닫을 게 있었는데 못 닫았다"* 가 *"닫았다"* 보다 먼저 알려야 하는 사실이다."""
    막힘 = ClosingPartOut(
        part="finance",
        status="BLOCKED",
        closed=["SIM-1:2026-01-07"],
        created=0,
        reason="한쪽이 막혔다",
    )
    closing.register_closing("finance", _재무(out=막힘))

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status == "BLOCKED"
    assert out.parts[0].closed == ["SIM-1:2026-01-07"], "선 것까지 지우지는 않는다"


def test_CLOSED_인데_새로_적은_건수가_0_일_수_있다():
    """🔴 **멱등이라 이미 닫혔으면 적은 건수가 0 이다. 그래도 마감은 서 있다.**

    ⚠️ **`closed` 와 `created` 를 한 값으로 접으면 안 된다.** 접으면
      *"이미 닫혀 있어서 안 적었다"* 와 *"닫을 것이 없었다"* 가 같아 보이고,
      두 번째 걸음이 매일 *"아무것도 안 했다"* 로 읽힌다.
    """
    두번째_걸음 = _닫음("SIM-1:2026-01-07", created=0)
    closing.register_closing("finance", _재무(out=두번째_걸음))

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.status == "CLOSED", "이미 닫힌 것을 NOTHING_DUE 로 접었다"
    assert out.parts[0].created == 0, "어댑터가 낸 값 대신 마스터가 다시 셌다"
    assert out.parts[0].closed == ["SIM-1:2026-01-07"], "서 있는 마감을 안 실었다"
    # 🔴 닫을 것이 없던 날과 **다른 값**이어야 한다.
    closing.register_closing(
        "finance", _재무(out=ClosingPartOut(part="finance", status="NOTHING_DUE"))
    )
    없던_날 = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())
    assert 없던_날.status != out.status, "두 사실이 같은 status 로 나간다"


# ── 진입점 ────────────────────────────────────────────────────────────────


def test_마감_엔드포인트가_있다() -> None:
    """🔴 **없으면 하루 끝에 아무도 안 부른다** — `daily_closings` 가 그랬다."""
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/close" in paths, (
        f"마감 진입점이 없다. 있는 경로: {sorted(p for p in paths if 'days' in p)}"
    )


def test_장부를_바꾸는_사건_다섯이_다_다른_엔드포인트다() -> None:
    """★ **사건 다섯은 자리 다섯이다.** 하나로 묶으면 실패 조합을 한 응답으로 못 낸다.

    ```text
    개장 성공 · 입고 BLOCKED · 채권 ISSUED · 수금 NOTHING_DUE · 마감 BLOCKED
      ← 한 status 로 어떻게 적나
    ```
    """
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/master/days/{as_of}/open" in paths
    assert "/master/days/{as_of}/receive" in paths
    assert "/master/days/{as_of}/issue-receivables" in paths
    assert "/master/days/{as_of}/collect" in paths
    assert "/master/days/{as_of}/close" in paths


def test_엔드포인트가_부르는_함수가_하루_실행이_부르는_함수와_같다() -> None:
    """🔴 **둘이 갈리면 손으로 부른 결과와 걷기 결과가 다른 코드를 지난다.**"""
    from app.master import router as router_module
    from app.master import scheduler

    사람_경로 = _inspect.getsource(router_module.master_close_day)
    assert "run_close_day(as_of, sim_run_id=BURN_IN_SIM_RUN_ID)" in 사람_경로

    assert router_module.run_close_day is closing.close_day
    assert (
        _inspect.signature(scheduler.run_scheduled_day).parameters["close_fn"].default
        is closing.close_day
    ), "하루 실행이 다른 함수를 부른다"


def test_엔드포인트가_실패도_200_으로_낸다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 막힌 것은 오류가 아니라 **그날의 사실**이다."""
    monkeypatch.setattr(closing, "check_day_gate", _막힌_Gate)
    closing.register_closing("finance", _재무())

    import app.main

    client = TestClient(app.main.app)
    resp = client.post(f"/master/days/{AS_OF.isoformat()}/close")

    assert resp.status_code == 200
    assert resp.json()["status"] == "NOT_OPENED"
    assert resp.json()["next_action"] == "OPEN_DAY_REQUIRED"


# ── ⑦ 하루 순서 · 장부 관문 ───────────────────────────────────────────────


class _Out:
    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason


class _Spy:
    def __init__(self, out: Any = None, *, boom: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []
        self.out = out
        self.boom = boom

    def __call__(self, arg, *args, **kwargs):
        self.calls.append(arg)
        self.kwargs.append(kwargs)
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
        "close_fn": _Spy(_Out("CLOSED")),
        # ⚠️ **계약이 아는 품목이어야 한다.** 아무 문자열이나 주면
        #   `ProcurementRunRequest` 가 거절하고, 그 거절이 `FAILED` 로 잡혀
        #   *"판단이 안 돌았다"* 와 구별이 안 된다.
        "items": ("배추",),
    }
    defaults.update(kwargs)

    action = ScheduledAction(
        as_of=AS_OF,
        now=datetime(2026, 1, 7, 9, 30, tzinfo=SEOUL),
        action="RUN_AND_RECORD",
        reason="검사",
        deadline=datetime(2026, 1, 7, 10, 30, tzinfo=SEOUL),
    )
    assert action.should_run, "전제가 깨졌다 — 이 검사는 도는 날을 재려던 것이다"
    return run_scheduled_day(action, **defaults), defaults


def test_하루_끝에_마감을_부른다() -> None:
    """🔴 **안 부르면 걷기가 손익을 한 줄도 안 남긴다.** 그것이 이 판의 실측이었다."""
    out, given = _하루()

    assert given["close_fn"].calls == [AS_OF], "하루 끝에 마감을 안 불렀다"
    assert out.closing_status == "CLOSED"


def test_마감이_출고_뒤이고_하루의_맨_끝이다() -> None:
    """🔴 **출고가 재고를 움직인다.** 앞에서 닫으면 그날 재고가 마감 뒤에 바뀐다."""
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
        outbound_fn=기록("출고", "NOTHING_DUE"),
        close_fn=기록("마감", "CLOSED"),
    )

    assert 순서 == ["입고", "채권", "수금", "출고", "마감"], f"하루 순서가 계약과 다르다: {순서}"


def test_마감에_실행_축을_흘려_준다() -> None:
    """🔴 **`daily_closings` 의 PK 절반이다.** 안 흘리면 행이 어디에 앉을지 안 정해진다."""
    out, given = _하루(sim_run_id="SIM-걷기-2026")

    assert given["close_fn"].kwargs[0]["sim_run_id"] == "SIM-걷기-2026"
    assert out.closing_status == "CLOSED"


def test_기본_축의_주인은_ledger_repository_하나다() -> None:
    """★ 관문 행이 싣는 값과 같아야 한다 — 갈리면 축으로 훑을 때 마감만 빠진다."""
    from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
    from app.master.scheduler import run_scheduled_day

    assert (
        _inspect.signature(run_scheduled_day).parameters["sim_run_id"].default is BURN_IN_SIM_RUN_ID
    )


def test_장부_관문이_막은_날은_BLOCKED_다() -> None:
    """🔴 **안 닫는다. 그런데 «마감이 없다» 로 두지도 않는다.**

    ⚠️ `NOT_ATTEMPTED` 로 두면 휴장일·`WAIT`·개장 실패와 같은 값이 되고, 손익
      곡선에는 넷 다 빈 칸이라 가를 데가 없다.
    """
    close_fn = _Spy(_Out("BLOCKED"))
    out, _ = _하루(issue_fn=_Spy(_Out("BLOCKED", "기일이 없다")), close_fn=close_fn)

    assert out.closing_status == "BLOCKED"
    assert out.closing_status != "NOT_ATTEMPTED", "막힌 날이 «마감이 없다» 로 접혔다"
    assert close_fn.kwargs[0]["ledger_gap"], "관문 사유를 안 넘겼다"
    assert "채권: BLOCKED" in close_fn.kwargs[0]["ledger_gap"], "무엇이 막았는지가 사라졌다"


def test_관문이_막은_날에는_어댑터를_안_부른다() -> None:
    """🔴 **장부가 실제보다 적은 채로 닫으면 그 틀린 숫자가 확정값으로 앉는다.**

    ★ 이 검사는 대역이 아니라 **실제 `close_day`** 를 태운다 — 관문 사유를 받고도
      어댑터를 부르는지가 물음이기 때문이다.
    """
    재무 = _재무()
    closing.register_closing("finance", 재무)

    out, _ = _하루(
        issue_fn=_Spy(_Out("BLOCKED", "기일이 없다")),
        close_fn=lambda as_of, **kw: close_day(as_of, connect=lambda: _가짜커넥션(), **kw),
        sim_run_id=축,
    )

    assert out.closing_status == "BLOCKED"
    assert 재무.calls == [], "장부가 안 섰는데 그날을 닫았다"
    assert 재무.ledger == {}, "막힌 날의 마감 행이 적혔다"


def test_안_도는_날은_마감을_아예_안_탄다() -> None:
    """🔴 **`WAIT` · 휴장일은 `NOT_ATTEMPTED` 다.** 막힌 것과 다른 사실이다."""
    from app.master.scheduler import ScheduledAction, run_scheduled_day

    close_fn = _Spy(_Out("CLOSED"))
    action = ScheduledAction(
        as_of=AS_OF,
        now=datetime(2026, 1, 7, 9, 30, tzinfo=SEOUL),
        action="WAIT",
        reason="아직 예측이 안 왔다",
        deadline=datetime(2026, 1, 7, 10, 30, tzinfo=SEOUL),
    )

    out = run_scheduled_day(action, close_fn=close_fn, items=("배추",))

    assert out.closing_status == "NOT_ATTEMPTED"
    assert close_fn.calls == [], "안 도는 날에 마감을 불렀다"


def test_마감이_터져도_그날_걷기_결과가_그대로다() -> None:
    """🔴 **이력 때문에 운영이 멈추면 안 된다** — `try_save_run` 이 `try_` 인 이유와 같다."""
    정상 = _하루()[0]
    터짐 = _하루(close_fn=_Spy(boom=RuntimeError("재무가 죽었다")))[0]

    assert 터짐.closing_status == "FAILED"
    assert 터짐.procurement_status == 정상.procurement_status == "RAN"
    assert 터짐.outbound_status == 정상.outbound_status == "NOTHING_DUE"
    assert [one.status for one in 터짐.items] == [one.status for one in 정상.items] == ["RAN"]
    assert 터짐.inbound_status == 정상.inbound_status
    assert 터짐.receivable_status == 정상.receivable_status
    assert 터짐.collection_status == 정상.collection_status


def test_마감_NOTHING_DUE_가_그날을_사고로_만들지_않는다() -> None:
    """🔴 **오늘이 이 길이다** — 어댑터가 없어 매일 `NOTHING_DUE` 다.

    ★ *"확인했고 닫을 것이 없다"* 는 정상이고, 판단도 출고도 이미 끝난 뒤다.
    """
    out, _ = _하루(close_fn=_Spy(_Out("NOTHING_DUE", "마감 미등록: finance")))

    assert out.closing_status == "NOTHING_DUE"
    assert out.procurement_status == "RAN"
    assert out.outbound_status == "NOTHING_DUE"


# ── ⑧ 마스터가 숫자를 계산하지 않는다 ─────────────────────────────────────


def test_마스터가_daily_closings_에_직접_쓰지_않는다() -> None:
    """🔴 **마감은 재무 원장 계산이다.** 마스터가 적으면 조정자가 부서를 겸한다."""
    코드 = _코드만()

    assert "INSERT" not in 코드.upper(), "마감이 SQL 을 직접 쓴다"
    assert "daily_closings" not in 코드, "마감이 표 이름을 직접 든다"
    assert "sql." not in 코드, "마감이 SQL 을 조립한다"


def test_마감_어휘에_금액_칸이_하나도_없다() -> None:
    """🔴 **칸 하나만 열어 둬도 다음 판이 거기에 값을 채운다.**

    ⚠️ 그러면 같은 사실의 주인이 둘이 되고, 재무가 세는 값과 갈리는 날
      **에러 없이 손익만 틀린다.**
    """
    칸 = set(closing.ClosingPartOut.model_fields) | set(closing.ClosingOut.model_fields)
    금액스러운_것 = {
        이름
        for 이름 in 칸
        if any(조각 in 이름 for 조각 in ("krw", "amount", "qty", "balance", "cash", "cost"))
    }

    assert 금액스러운_것 == set(), f"마감 어휘에 금액 칸이 생겼다: {sorted(금액스러운_것)}"


def test_마감_모듈에_산술이_하나도_없다() -> None:
    """🔴 **마스터는 셈을 하지 않는다.** 어댑터가 낸 값을 그대로 적는다.

    ⚠️ `created` 를 `len(closed)` 로 다시 세는 것도 셈이다 — 세는 순간
      *"이미 닫혀 있어서 안 적었다"* 가 사라진다.
    """
    tree = ast.parse(pathlib.Path(closing.__file__).read_text(encoding="utf-8"))
    산술 = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod))
    ]

    assert 산술 == [], f"마감 모듈이 셈을 한다 (줄 {[n.lineno for n in 산술]})"


def test_어댑터가_낸_값을_그대로_옮긴다() -> None:
    """🔴 **파트 결과를 다시 만들지 않는다.** 같은 객체여야 한다."""
    파트 = _닫음("SIM-1:2026-01-07", "SIM-1:2026-01-08", created=0)
    closing.register_closing("finance", _재무(out=파트))

    out = close_day(AS_OF, sim_run_id=축, connect=lambda: _가짜커넥션())

    assert out.parts[0] is 파트, "마스터가 파트 결과를 다시 지었다"
