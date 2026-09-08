"""수금 사건 생성기 — **`due_date` 당일 전액 회수를 가정으로 적는 자리**가 서 있는가.

🔴 **`SIM_FIXED` 가정이다. 실제 입금 사실이 아니다.** 재무·판매가 조건 여섯으로
  승인했고, 이 파일이 그 여섯을 하나씩 잠근다.

```text
① 실제 입금 사실로 취급 안 함   note 에 SIM_FIXED 와 근거가 들어간다
② Finance 자동수금은 계속 금지   재무는 이 표에 적힌 것만 실행한다 (여기서 안 만드는 것)
③ target 은 **누적** target      원금을 그대로 적는다
④ COLLECTED 재수금 금지          target = original 이라 delta 가 0 이다
⑤ PARTIAL 은 잔여만 · OPEN 전액   같은 한 줄이 둘 다 만든다
⑥ 신규 Receivable 에도 멱등      개장마다 다시 훑고 없는 건만 만든다
```

★★ **`target = original_amount_krw` 한 줄로 `④⑤` 가 성립한다.**

```text
OPEN       target 300 · current   0  →  delta 300
PARTIAL    target 300 · current 150  →  delta 150   ← 잔여만
COLLECTED  target 300 · current 300  →  delta   0
```

  🔴 **`④⑤` 를 여기서 다시 계산하지 않는다.** 재무 `build_collection_transition` 이
    역행·초과·항등식을 이미 막는다 — 같은 규칙을 두 곳에 앉히면 둘이 갈린다.

⚠️ **그래도 `COLLECTED` 에는 행을 안 만든다.** 낼 것이 없는 사건을 적으면 사람이
  *"그날 뭔가 들어왔다"* 로 읽는다 — *"없는 것과 안 한 것은 다르다"* 의 다른 쪽이다.

---

⚠️ **공유 DB 를 시드하지 않는다.** `master_collection_events` 는 0행으로 둔다 — 여기는
  전부 대역이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Self

import pytest

from app.finance.db import FinanceDataNotReady, FinanceRuntimeAxis
from app.master import day_open
from app.master.collection_seed import (
    CollectionSeedOutcome,
    CollectionSeedResult,
    seed_collection_events,
    seed_day,
)
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

AS_OF = date(2026, 1, 10)
"""토요일이다. **입금은 토요일에도 찍힌다** — 수금은 달력일이다."""

축_모드 = "LOAN_BASELINE"
남의_모드 = "BASE_NO_LOAN"
남의_실행 = "SIM-SOMEONE-ELSE"


class _중복행(Exception):
    """PK 충돌. 실제 DB 라면 `UniqueViolation` 이 온다."""


# ---------------------------------------------------------------------------
# 대역 — receivables 를 읽고 master_collection_events 에 쓴다
# ---------------------------------------------------------------------------


def _채권(
    *,
    receivable_id: str,
    status: str,
    original: str,
    received: str,
    sim_run_id: str = BURN_IN_SIM_RUN_ID,
    due_date: date = AS_OF,
) -> dict[str, Any]:
    """`receivables` 한 행. **항등식을 지킨다** — 원금 - 기왕수금 = 미수."""
    원금 = Decimal(original)
    기왕 = Decimal(received)
    return {
        "receivable_id": receivable_id,
        "sim_run_id": sim_run_id,
        "due_date": due_date,
        "original_amount_krw": 원금,
        "received_amount_krw": 기왕,
        "outstanding_amount_krw": 원금 - 기왕,
        "status": status,
    }


#: 실측 세 갈래를 그대로 옮긴 표본 (2026-09-08 · `receivables` 15행).
열림 = _채권(receivable_id="RCV-OPEN", status="OPEN", original="300", received="0")
일부 = _채권(receivable_id="RCV-PARTIAL", status="PARTIAL", original="300", received="150")
완료 = _채권(receivable_id="RCV-COLLECTED", status="COLLECTED", original="300", received="300")


@dataclass
class _가짜DB:
    """`receivables` 를 읽고 `master_collection_events` 에 쓰는 최소 대역.

    🔴 **SQL 문장에 실제로 적힌 조건만 적용한다.** 그래야 필터를 지우는 변이가 행동을
      바꾼다 — 문자열만 보고 통과시키면 변이가 red 가 안 된다.
    """

    receivables: list[dict[str, Any]] = field(default_factory=list)
    events: dict[tuple[Any, ...], tuple[Any, ...]] = field(default_factory=dict)
    쓰기_예외: Exception | None = None
    committed: int = 0
    rolled_back: int = 0
    closed: int = 0
    문장들: list[str] = field(default_factory=list)

    def cursor(self) -> _가짜커서:
        return _가짜커서(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


class _가짜커서:
    def __init__(self, db: _가짜DB) -> None:
        self._db = db
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        문장 = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self._db.문장들.append(문장)
        if "INSERT INTO" in 문장:
            self._insert(문장, tuple(params or ()))
            return
        self._select(문장, tuple(params or ()))

    def _select(self, 문장: str, params: tuple[Any, ...]) -> None:
        sim_run_id, as_of = params[0], params[1]
        rows = list(self._db.receivables)
        if "sim_run_id = %s" in 문장:
            rows = [row for row in rows if row["sim_run_id"] == sim_run_id]
        if "due_date = %s" in 문장:
            rows = [row for row in rows if row["due_date"] == as_of]
        if "outstanding_amount_krw > 0" in 문장:
            rows = [row for row in rows if row["outstanding_amount_krw"] > 0]
        self._rows = rows
        self.rowcount = len(rows)

    def _insert(self, 문장: str, params: tuple[Any, ...]) -> None:
        if self._db.쓰기_예외 is not None:
            raise self._db.쓰기_예외
        키 = params[:4]
        if 키 in self._db.events:
            if "ON CONFLICT" not in 문장:
                # 🔴 **멱등을 안 걸면 실제 DB 는 PK 로 터진다.**
                raise _중복행(f"duplicate key: {키!r}")
            self.rowcount = 0
            return
        self._db.events[키] = params
        self.rowcount = 1

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


def _축(*, sim_run_id: str = BURN_IN_SIM_RUN_ID, financing_mode: str = 축_모드) -> Any:
    def read_axis() -> FinanceRuntimeAxis:
        return FinanceRuntimeAxis(sim_run_id=sim_run_id, financing_mode=financing_mode)

    return read_axis


def _만든다(db: _가짜DB, **kwargs: Any) -> CollectionSeedResult:
    옵션: dict[str, Any] = {
        "sim_run_id": BURN_IN_SIM_RUN_ID,
        "financing_mode": 축_모드,
        "as_of": AS_OF,
    }
    옵션.update(kwargs)
    return seed_collection_events(db, **옵션)


def _사건(db: _가짜DB, receivable_id: str) -> tuple[Any, ...]:
    키 = (BURN_IN_SIM_RUN_ID, 축_모드, AS_OF, receivable_id)
    assert 키 in db.events, f"{receivable_id} 사건이 없다: {sorted(db.events)}"
    return db.events[키]


# ---------------------------------------------------------------------------
# 1. 세 갈래 — OPEN · PARTIAL 은 만들고 COLLECTED 는 안 만든다
# ---------------------------------------------------------------------------


def test_OPEN_과_PARTIAL_만_사건이_된다() -> None:
    """⚠️ **`COLLECTED` 는 낼 것이 없으므로 행을 안 만드는 것이 맞다.**

    delta 가 0 이라 넣어도 아무 일이 안 일어나지만, 행이 있으면 사람은 *"그날 뭔가
    들어왔다"* 로 읽는다 — 없는 것을 적지 않는다.
    """
    db = _가짜DB(receivables=[열림, 일부, 완료])

    결과 = _만든다(db)

    만들어진 = {키[3] for 키 in db.events}
    assert 만들어진 == {"RCV-OPEN", "RCV-PARTIAL"}, (
        f"세 갈래 처리가 틀렸다: {sorted(만들어진)} — COLLECTED 에 행이 생기면 안 된다"
    )
    assert 결과 == CollectionSeedResult(created=2, skipped=0)


def test_미수가_0_이면_조회에서부터_빠진다() -> None:
    """🔴 **`outstanding_amount_krw > 0` 필터가 SQL 에 있어야 한다.**

    ★ 파이썬에서 걸러도 되지 않느냐 — 안 된다. 실 DB 에서는 15건이 아니라 그날치
      전부를 끌어오게 되고, 필터가 어디 있는지가 두 곳으로 갈린다.
    """
    db = _가짜DB(receivables=[완료])

    결과 = _만든다(db)

    assert db.events == {}, f"COLLECTED 에 행이 생겼다: {sorted(db.events)}"
    assert 결과 == CollectionSeedResult(created=0, skipped=0)
    문장 = db.문장들[0]
    assert "outstanding_amount_krw > 0" in 문장, f"미수 필터가 SQL 에 없다: {문장}"


def test_그날_결제기일인_것만_고른다() -> None:
    """★ `due_date` 는 지어낸 값이 아니다 — `sale_date + payment_days` 로 선 계약일이다."""
    다른날 = _채권(
        receivable_id="RCV-LATER",
        status="OPEN",
        original="500",
        received="0",
        due_date=date(2026, 1, 11),
    )
    db = _가짜DB(receivables=[열림, 다른날])

    _만든다(db)

    assert {키[3] for 키 in db.events} == {"RCV-OPEN"}, "그날 것이 아닌 채권이 섞였다"


def test_다른_실행의_채권은_안_고른다() -> None:
    """🔴 남의 실행 장부에 수금 사건을 적으면 **에러 없이 남의 현금이 는다.**"""
    남의것 = _채권(
        receivable_id="RCV-OTHER",
        status="OPEN",
        original="500",
        received="0",
        sim_run_id=남의_실행,
    )
    db = _가짜DB(receivables=[열림, 남의것])

    _만든다(db)

    assert {키[3] for 키 in db.events} == {"RCV-OPEN"}, "다른 sim_run_id 의 채권이 섞였다"


# ---------------------------------------------------------------------------
# 2. ★★ target = original — 이 한 줄이 조건 ④⑤ 를 만든다
# ---------------------------------------------------------------------------


def test_target_은_원금이다_미수가_아니다() -> None:
    """🔴 **`target_received_total_krw` 는 누적이다.** 잔액을 적으면 두 번째 분할
    수금에서 *"cumulative collection cannot regress"* 로 거부된다.

    ★ 그리고 원금을 적는 한 줄이 `④⑤` 를 동시에 만든다 —
      `PARTIAL` 은 delta 가 잔여(150)뿐이고 `COLLECTED` 는 0 이다.
    """
    db = _가짜DB(receivables=[열림, 일부])

    _만든다(db)

    assert _사건(db, "RCV-OPEN")[4] == Decimal(300)
    assert _사건(db, "RCV-PARTIAL")[4] == Decimal(300), (
        "PARTIAL 의 target 이 미수(150)로 적혔다 — 누적 target 은 원금이다"
    )


def test_PARTIAL_은_잔여만_들어온다() -> None:
    """⑤ **잔여만.** target(300) - 기왕수금(150) = delta 150 이다.

    🔴 **delta 를 여기서 계산하지 않는다.** 재무 `build_collection_transition` 이
      한다 — 여기서는 그 계산이 잔여를 내는지만 확인한다.
    """
    from app.finance.collection import build_collection_transition

    db = _가짜DB(receivables=[일부])
    _만든다(db)
    target = _사건(db, "RCV-PARTIAL")[4]

    plan = build_collection_transition(
        {**일부, "sim_run_id": BURN_IN_SIM_RUN_ID},
        {
            "finance_state_id": "FS-1",
            "sim_run_id": BURN_IN_SIM_RUN_ID,
            "current_cash_krw": Decimal(0),
            "receivables_krw": Decimal(1000),
        },
        target_received_total_krw=target,
    )

    assert plan.delta_received_krw == Decimal(150), (
        f"PARTIAL 이 잔여만 들어오지 않았다: {plan.delta_received_krw}"
    )
    assert plan.next_status == "COLLECTED"


def test_COLLECTED_는_행이_없어_재수금이_없다() -> None:
    """④ **재수금 금지.** 행이 없으니 재무가 실행할 것도 없다."""
    db = _가짜DB(receivables=[완료])

    _만든다(db)

    assert (BURN_IN_SIM_RUN_ID, 축_모드, AS_OF, "RCV-COLLECTED") not in db.events


# ---------------------------------------------------------------------------
# 3. note — 왜 사실로 두었나
# ---------------------------------------------------------------------------


def test_note_에_SIM_FIXED_와_숫자가_들어간다() -> None:
    """★ **숫자를 넣는다.** 값만 넘기면 사람도 매입 판단도 확정으로 읽는다.

    ⚠️ 이 칸은 *"이 사건을 왜 사실로 두었나"* 를 적는 자리이고, **파생식과 성격이
      둘 다** 들어가야 한다.
    """
    db = _가짜DB(receivables=[일부])

    _만든다(db)
    note = _사건(db, "RCV-PARTIAL")[5]

    assert "SIM_FIXED" in note, f"가정임을 가르는 말머리가 없다: {note!r}"
    assert "실제 입금 사실이 아니다" in note, f"성격이 안 적혔다: {note!r}"
    assert "sale_date + payment_days" in note, f"파생식이 안 적혔다: {note!r}"
    assert str(AS_OF) in note, f"근거 due_date 가 없다: {note!r}"
    for 숫자 in ("300", "150"):
        assert 숫자 in note, f"금액이 안 적혔다 ({숫자}): {note!r}"


def test_note_의_delta_가_원금에서_기왕수금을_뺀_값이다() -> None:
    """🔴 **note 의 숫자가 실제 delta 와 달라지면 근거가 아니라 장식이다.**"""
    db = _가짜DB(receivables=[일부])

    _만든다(db)

    assert "delta 150" in _사건(db, "RCV-PARTIAL")[5]
    db2 = _가짜DB(receivables=[열림])
    _만든다(db2)
    assert "delta 300" in db2.events[(BURN_IN_SIM_RUN_ID, 축_모드, AS_OF, "RCV-OPEN")][5]


# ---------------------------------------------------------------------------
# 4. 멱등 — 조건 ⑥
# ---------------------------------------------------------------------------


def test_두_번_불러도_행이_안_는다() -> None:
    """🟢 **멱등은 PK 가 잡는다** — `ON CONFLICT DO NOTHING`.

    ⚠️ **그래도 몇 건이 들어갔는지는 센다.** 안 세면 조건 `⑥` 이 지켜지는지 밖에서
      못 본다.
    """
    db = _가짜DB(receivables=[열림, 일부, 완료])

    첫번째 = _만든다(db)
    두번째 = _만든다(db)

    assert len(db.events) == 2, f"두 번째 호출로 행이 늘었다: {sorted(db.events)}"
    assert 첫번째 == CollectionSeedResult(created=2, skipped=0)
    assert 두번째 == CollectionSeedResult(created=0, skipped=2), (
        f"이미 있던 건을 안 세거나 또 만들었다: {두번째}"
    )


def test_ON_CONFLICT_가_SQL_에_있다() -> None:
    """🔴 **파이썬에서 미리 조회해 거르는 것으로는 경합을 못 막는다.** DB 가 잡아야 한다."""
    db = _가짜DB(receivables=[열림])

    _만든다(db)

    삽입 = [문장 for 문장 in db.문장들 if "INSERT INTO" in 문장]
    assert 삽입, "INSERT 가 없다"
    assert "ON CONFLICT DO NOTHING" in 삽입[0], f"멱등이 SQL 에 없다: {삽입[0]}"


def test_새_채권이_생기면_그것만_만든다() -> None:
    """🔴 **조건 `⑥` 이다** — *"15건만 손으로 채우는 방식은 받기 어렵다"*.

    ★ 개장마다 대상 채권을 다시 훑고 **아직 event 가 없는 건만** 만든다.
    """
    db = _가짜DB(receivables=[열림, 일부])
    _만든다(db)

    새것 = _채권(receivable_id="RCV-NEW", status="OPEN", original="700", received="0")
    db.receivables.append(새것)
    결과 = _만든다(db)

    assert 결과 == CollectionSeedResult(created=1, skipped=2), (
        f"새 채권만 만들어야 한다: {결과}"
    )
    assert _사건(db, "RCV-NEW")[4] == Decimal(700)
    assert len(db.events) == 3


# ---------------------------------------------------------------------------
# 5. financing_mode 는 마스터가 안 고른다
# ---------------------------------------------------------------------------


def test_financing_mode_를_재무_축에서_받는다() -> None:
    """⚠️ **상수를 박으면 무차입 장부의 수금이 대출 장부에 조용히 들어간다.**

    실측으로 `finance_states` 에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이
    **공존한다.**
    """
    db = _가짜DB(receivables=[열림])

    결과 = seed_day(
        AS_OF,
        sim_run_id=BURN_IN_SIM_RUN_ID,
        connect=lambda: db,
        read_axis=_축(financing_mode=남의_모드),
    )

    assert 결과.status == "SEEDED"
    키 = (BURN_IN_SIM_RUN_ID, 남의_모드, AS_OF, "RCV-OPEN")
    assert 키 in db.events, f"재무가 준 모드로 안 적혔다: {sorted(db.events)}"


def test_축의_실행이_다르면_안_만든다() -> None:
    """🔴 **fail-closed.** 덮어 쓰면 남의 실행 장부에 수금 사건을 적는다."""
    db = _가짜DB(receivables=[열림])

    결과 = seed_day(
        AS_OF,
        sim_run_id=BURN_IN_SIM_RUN_ID,
        connect=lambda: db,
        read_axis=_축(sim_run_id=남의_실행),
    )

    assert 결과.status == "NOT_ATTEMPTED"
    assert db.events == {}, "축이 다른데 사건을 만들었다"
    assert 남의_실행 in 결과.reason


def test_축이_모호하면_사유를_남긴다() -> None:
    """⚠️ 접기만 하고 사유를 버리면 *"안 만들었다"* 만 남고 고칠 곳이 사라진다."""

    def 모호하다() -> FinanceRuntimeAxis:
        raise FinanceDataNotReady("finance_runtime_axis_ambiguous")

    결과 = seed_day(AS_OF, sim_run_id=BURN_IN_SIM_RUN_ID, read_axis=모호하다)

    assert 결과.status == "NOT_ATTEMPTED"
    assert "finance_runtime_axis_ambiguous" in 결과.reason


# ---------------------------------------------------------------------------
# 6. 🔴 못 했다 ≠ 낼 것이 없었다
# ---------------------------------------------------------------------------


def test_쓰기가_실패하면_UNREADABLE_이다() -> None:
    """🔴 **`0 건` 으로 접으면 *"확인했고 없었다"* 로 조용히 지나간다.**

    ⚠️ 그러면 들어왔어야 할 현금이 장부에 없는 채로 매입 판단이 돈다.
    """
    db = _가짜DB(receivables=[열림], 쓰기_예외=RuntimeError("relation does not exist"))

    결과 = seed_day(
        AS_OF, sim_run_id=BURN_IN_SIM_RUN_ID, connect=lambda: db, read_axis=_축()
    )

    assert 결과.status == "UNREADABLE", f"실패가 {결과.status} 로 접혔다"
    assert 결과.status != "NOTHING_DUE"
    assert "relation does not exist" in 결과.reason
    assert db.rolled_back == 1, "실패했는데 되돌리지 않았다"
    assert db.committed == 0


def test_낼_것이_없으면_NOTHING_DUE_다() -> None:
    """★ *"확인했고 낼 것이 없었다"* 다. 이것은 정상이고 `UNREADABLE` 이 아니다."""
    db = _가짜DB(receivables=[완료])

    결과 = seed_day(
        AS_OF, sim_run_id=BURN_IN_SIM_RUN_ID, connect=lambda: db, read_axis=_축()
    )

    assert 결과 == CollectionSeedOutcome(status="NOTHING_DUE", created=0, skipped=0)
    assert db.committed == 1


def test_커넥션을_못_열어도_UNREADABLE_이다() -> None:
    """★ 연결 자체가 안 되는 것도 **못 한 것**이다."""

    def 못_연다() -> Any:
        raise FinanceDataNotReady("connection refused")

    결과 = seed_day(
        AS_OF, sim_run_id=BURN_IN_SIM_RUN_ID, connect=못_연다, read_axis=_축()
    )

    assert 결과.status == "UNREADABLE"
    assert "connection refused" in 결과.reason


def test_성공하면_커밋하고_닫는다() -> None:
    db = _가짜DB(receivables=[열림])

    seed_day(AS_OF, sim_run_id=BURN_IN_SIM_RUN_ID, connect=lambda: db, read_axis=_축())

    assert db.committed == 1
    assert db.closed == 1


# ---------------------------------------------------------------------------
# 7. 개장 경로 — 사건 생성이 하루를 막지 않는다
# ---------------------------------------------------------------------------


class _열려있다:
    """이미 열려 있는 파트. `ALREADY_OPENED` 를 낸다."""

    def is_open(self, conn: Any, *, as_of: date) -> bool:
        return True

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:
        raise AssertionError("만들 날이 없어야 한다")


class _못연다:
    def is_open(self, conn: Any, *, as_of: date) -> bool:
        return as_of == AS_OF - timedelta(days=1)

    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None:
        raise RuntimeError("파트가 못 열었다")


class _개장커넥션:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def 등록소를_비운다() -> None:
    """다른 검사가 남긴 하루 넘김 대역을 치운다.

    ⚠️ `_OPENINGS` 는 **프로세스 전역**이다. 안 비우면 남의 물류 대역이 같이 걷고
      이 파일의 답이 실행 순서로 갈린다 (`tests/conftest.py` 가 끝나면 되돌린다).

    ★ 개장 정본 적재는 `tests/master/conftest.py` 가 이미 막는다.
    """
    day_open.reset()


def test_개장이_성공하면_사건을_만든다(등록소를_비운다: None) -> None:
    """🔴 **조건 `⑥` — 개장 시 대상 채권을 확인해 없는 건만 생성한다.**"""
    day_open.register_day_opening("finance", _열려있다())
    불린것: list[tuple[date, str]] = []

    def 대역(as_of: date, *, sim_run_id: str, **_: Any) -> CollectionSeedOutcome:
        불린것.append((as_of, sim_run_id))
        return CollectionSeedOutcome(status="SEEDED", created=3, skipped=1)

    out = day_open.open_day(
        AS_OF, connect=lambda: _개장커넥션(), seed_collection=대역
    )

    assert out.status == "ALREADY_OPENED"
    assert 불린것 == [(AS_OF, BURN_IN_SIM_RUN_ID)], f"개장 뒤에 안 불렸다: {불린것}"
    assert out.collection_seed_status == "SEEDED"
    assert out.collection_seeded == 3
    assert out.collection_seed_skipped == 1


def test_사건_생성이_터져도_하루는_열린다(등록소를_비운다: None) -> None:
    """🔴 **개장을 실패시키면 안 된다.** 사건 생성이 터져도 하루는 열려야 한다.

    ⚠️ 다만 *"못 했다"* 가 응답에 실려야 한다 — `0 건` 으로 접히면 실패다.
    """
    day_open.register_day_opening("finance", _열려있다())

    def 터진다(as_of: date, **_: Any) -> CollectionSeedOutcome:
        raise RuntimeError("수금 사건 표가 없다")

    out = day_open.open_day(
        AS_OF, connect=lambda: _개장커넥션(), seed_collection=터진다
    )

    assert out.status == "ALREADY_OPENED", f"사건 생성이 하루를 막았다: {out.status}"
    assert out.collection_seed_status == "UNREADABLE", (
        f"못 한 것이 {out.collection_seed_status} 로 접혔다"
    )
    assert out.collection_seeded == 0
    assert "수금 사건 표가 없다" in out.collection_seed_reason


def test_하루가_안_열리면_시도하지_않는다(등록소를_비운다: None) -> None:
    """🔴 **`NOT_ATTEMPTED` 를 `NOTHING_DUE` 로 접지 않는다.**

    서 있지도 않은 재무 상태 행에 대고 수금을 적으면 안 되고, *"안 했다"* 와
    *"확인했고 없었다"* 는 다른 사실이다.
    """
    day_open.register_day_opening("finance", _못연다())
    불렸나: list[Any] = []

    def 대역(as_of: date, **_: Any) -> CollectionSeedOutcome:
        불렸나.append(as_of)
        return CollectionSeedOutcome(status="SEEDED", created=1)

    out = day_open.open_day(
        AS_OF, connect=lambda: _개장커넥션(), seed_collection=대역
    )

    assert out.status == "NOT_OPENED"
    assert 불렸나 == [], "하루가 안 열렸는데 사건을 만들러 갔다"
    assert out.collection_seed_status == "NOT_ATTEMPTED"
    assert out.collection_seed_status != "NOTHING_DUE"
    assert "NOT_OPENED" in out.collection_seed_reason


def test_낼_것이_없는_날은_NOTHING_DUE_로_실린다(등록소를_비운다: None) -> None:
    """★ 표가 비어 있는 지금, 이것이 매일 나오는 답이다."""
    day_open.register_day_opening("finance", _열려있다())

    out = day_open.open_day(
        AS_OF,
        connect=lambda: _개장커넥션(),
        seed_collection=lambda as_of, **_: CollectionSeedOutcome(status="NOTHING_DUE"),
    )

    assert out.status == "ALREADY_OPENED"
    assert out.collection_seed_status == "NOTHING_DUE"
