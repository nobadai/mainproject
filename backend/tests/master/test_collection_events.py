"""수금 사건을 **표에서 읽어 오는 자리**가 제대로 서 있는가.

🔴 **없던 것은 사건이 아니라 「수금일」이었다** (실측 2026-09-08 · `DB_SCHEMA=haetdeul`).

```text
haetdeul.receivables   15행
  COLLECTED  6   PARTIAL  2   OPEN  7

🟢 **얼마** 들어왔나   received_amount_krw 에 있다
🔴 **언제** 들어왔나   **어디에도 없었다** — receivables 에 수금일 칸이 없다
🔴 수금 사건 표        **없었다** (%collect% · %cash% · %payment% 전수 0건)
```

★ `database/master_collection_events.sql` 이 그 자리를 만들었고, 여기서 잠그는 것은
  **그 자리를 읽는 방법**이다.

---

이 파일이 잠그는 것 넷.

```text
① 축 필터        (sim_run_id, financing_mode) 것만 나온다 — 다른 축이 섞이면 실패
② 못 읽음 ≠ 없음  조회 실패는 예외로 올라오고, 부르는 쪽이 BLOCKED 로 접는다
③ 빈 표          NOTHING_DUE 이지 BLOCKED 가 아니다
④ 호출 시점      배선 시점이 아니라 collect() 마다 읽는다
```

🔴 **③ 이 지금 매일 나오는 답이다.** 표가 비어 있다 (2026-09-08 실측 0행). **이 판은
  자리를 만들 뿐 사건을 만들지 않는다** — 무엇을 사실로 둘지는 팀 결정이고, 재무가
  *"due_date 경과를 수금으로 읽지 않는다"* 로 그은 선이 그 이유다. **낸 것과 도는
  것은 다르다.**
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Self

import pytest

from app.finance.collection import CollectionEvent
from app.finance.db import FinanceDataNotReady, FinanceRuntimeAxis, get_db_schema
from app.master.collection_events import read_collection_events
from app.master.finance_collection import FinanceCollectionAdapter
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

AS_OF = date(2026, 1, 10)
"""토요일이다. **입금은 토요일에도 찍힌다** — 수금은 달력일이다."""

축_모드 = "LOAN_BASELINE"
남의_모드 = "BASE_NO_LOAN"
남의_실행 = "SIM-SOMEONE-ELSE"


# ---------------------------------------------------------------------------
# 대역
# ---------------------------------------------------------------------------


class _가짜커서:
    def __init__(self, 상자: _가짜커넥션) -> None:
        self._상자 = 상자

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        if self._상자.조회_예외 is not None:
            raise self._상자.조회_예외
        self._상자.executed.append((query, params))

    def fetchall(self) -> list[Any]:
        return list(self._상자.rows)


class _가짜커넥션:
    """SQL 문장과 인자를 잡아 두는 커넥션. **닫혔는지도 센다.**"""

    def __init__(self, rows: list[Any] | None = None, 조회_예외: Exception | None = None) -> None:
        self.rows = rows or []
        self.조회_예외 = 조회_예외
        self.executed: list[tuple[Any, Any]] = []
        self.closed = 0

    def cursor(self) -> _가짜커서:
        return _가짜커서(self)

    def close(self) -> None:
        self.closed += 1


def _행(
    *,
    sim_run_id: str = BURN_IN_SIM_RUN_ID,
    financing_mode: str = 축_모드,
    collection_date: date = AS_OF,
    receivable_id: str = "RCV-0001",
    target: Decimal = Decimal("1234567.000000"),
) -> dict[str, Any]:
    return {
        "sim_run_id": sim_run_id,
        "financing_mode": financing_mode,
        "collection_date": collection_date,
        "receivable_id": receivable_id,
        "target_received_total_krw": target,
    }


def _축(*, sim_run_id: str = BURN_IN_SIM_RUN_ID, financing_mode: str = 축_모드) -> Any:
    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        return FinanceRuntimeAxis(sim_run_id=sim_run_id, financing_mode=financing_mode)

    return read_axis


class _사건조회기록:
    def __init__(self, events: tuple[CollectionEvent, ...] = (), 예외: Exception | None = None):
        self.calls: list[dict[str, str]] = []
        self.events = events
        self.예외 = 예외

    def __call__(self, *, sim_run_id: str, financing_mode: str) -> tuple[CollectionEvent, ...]:
        self.calls.append({"sim_run_id": sim_run_id, "financing_mode": financing_mode})
        if self.예외 is not None:
            raise self.예외
        return self.events


# ---------------------------------------------------------------------------
# 1. 축 필터 — 다른 축 행이 섞이면 안 된다
# ---------------------------------------------------------------------------


def test_축_둘로_거른다() -> None:
    """🔴 **`financing_mode` 를 안 걸면 무차입 장부의 수금이 대출 장부에 섞인다.**

    ⚠️ 실측으로 `finance_states` 에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이
      **공존한다.** 섞여도 에러가 안 나고 숫자만 바뀐다 — 그래서 문장으로 잠근다.
    """
    conn = _가짜커넥션(rows=[_행()])

    read_collection_events(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        financing_mode=축_모드,
        connect=lambda: conn,
    )

    assert len(conn.executed) == 1
    query, params = conn.executed[0]
    문장 = query.as_string(None)
    assert "sim_run_id = %s" in 문장, f"sim_run_id 로 안 거른다: {문장}"
    assert "financing_mode = %s" in 문장, f"financing_mode 로 안 거른다: {문장}"
    assert params == (BURN_IN_SIM_RUN_ID, 축_모드), (
        f"축 둘이 인자로 안 실렸다: {params!r} — 안 실리면 표 전체가 딸려 온다"
    )


def test_표_이름이_정본_스키마에_붙는다() -> None:
    """★ 스키마를 안 붙이면 `search_path` 에 따라 **다른 표를 읽는다.**"""
    conn = _가짜커넥션()

    read_collection_events(
        sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode=축_모드, connect=lambda: conn
    )

    문장 = conn.executed[0][0].as_string(None)
    assert f'"{get_db_schema()}"."master_collection_events"' in 문장, 문장


def test_행을_재무_사건으로_옮긴다() -> None:
    """🔴 **`CollectionEvent` 는 재무 것이다.** 마스터가 같은 모양을 새로 만들지 않는다.

    ⚠️ **금액을 손대지 않는다.** 누적 target 은 재무가 delta 로 접어 쓰는 값이라,
      여기서 반올림하면 `receivables` 항등식이 어긋난 이유를 재무 쪽에서 찾게 된다.
    """
    금액 = Decimal("2948483.378450")
    conn = _가짜커넥션(rows=[_행(receivable_id="RCV-0007", target=금액)])

    사건들 = read_collection_events(
        sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode=축_모드, connect=lambda: conn
    )

    assert len(사건들) == 1
    사건 = 사건들[0]
    assert isinstance(사건, CollectionEvent)
    assert type(사건).__module__ == "app.finance.collection", (
        f"수금 사건 모양이 재무 것이 아니다: {type(사건).__module__}"
    )
    assert 사건.sim_run_id == BURN_IN_SIM_RUN_ID
    assert 사건.financing_mode == 축_모드
    assert 사건.collection_date == AS_OF
    assert 사건.receivable_id == "RCV-0007"
    assert 사건.target_received_total_krw == 금액


def test_표가_비면_빈_튜플이다() -> None:
    """★ **빈 튜플은 "사건이 없다" 이고 그것은 정상이다.** 지금 표가 그 상태다."""
    conn = _가짜커넥션(rows=[])

    assert (
        read_collection_events(
            sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode=축_모드, connect=lambda: conn
        )
        == ()
    )


# ---------------------------------------------------------------------------
# 2. **못 읽은 것과 없는 것은 다르다**
# ---------------------------------------------------------------------------


def test_조회_실패를_빈_튜플로_접지_않는다() -> None:
    """🔴 **`()` 로 접으면 표가 안 서 있는 날이 *"오늘은 들어올 게 없었다"* 로 읽힌다.**

    ★ `opened_days_after` 가 `None`(못 읽음)과 `()`(없음)을 가른 것과 같은 규율이다.
      여기서는 예외를 그대로 올린다 — 접는 자리는 부르는 쪽이다.
    """
    conn = _가짜커넥션(조회_예외=RuntimeError("relation does not exist"))

    with pytest.raises(RuntimeError, match="relation does not exist"):
        read_collection_events(
            sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode=축_모드, connect=lambda: conn
        )


def test_조회가_터져도_커넥션을_닫는다() -> None:
    """⚠️ 예외를 올리는 것과 커넥션을 흘리는 것은 다른 문제다."""
    conn = _가짜커넥션(조회_예외=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        read_collection_events(
            sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode=축_모드, connect=lambda: conn
        )

    assert conn.closed == 1, "조회가 터진 뒤 커넥션이 안 닫혔다"


def test_커넥션_열기가_터지면_그대로_올라온다() -> None:
    """★ 연결 자체가 안 되는 것도 **못 읽은 것**이다."""

    def 못_연다() -> Any:
        raise FinanceDataNotReady("connection refused")

    with pytest.raises(FinanceDataNotReady, match="connection refused"):
        read_collection_events(
            sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode=축_모드, connect=못_연다
        )


# ---------------------------------------------------------------------------
# 3. 어댑터 — 못 읽으면 **BLOCKED**, 비면 **NOTHING_DUE**
# ---------------------------------------------------------------------------


def test_사건을_못_읽으면_막는다() -> None:
    """🔴 **`NOTHING_DUE` 로 접으면 들어왔어야 할 현금이 없는 채로 매입 판단이 돈다.**"""
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(),
        load_events=_사건조회기록(예외=RuntimeError('relation "master_collection_events" ...')),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED", f"사건을 못 읽었는데 {out.status} 로 지나갔다"


def test_못_읽은_이유가_사유에_남는다() -> None:
    """⚠️ **접기만 하고 사유를 버리면 화면에 *"막혔다"* 만 남고 고칠 곳이 사라진다.**"""
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(),
        load_events=_사건조회기록(예외=RuntimeError("표가 없다: master_collection_events")),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert "master_collection_events" in out.reason, (
        f"무엇이 실패했는지가 사유에서 사라졌다: {out.reason!r}"
    )


def test_표가_비면_NOTHING_DUE_다() -> None:
    """🔴 **`BLOCKED` 로 접으면 뒤의 orchestration 이 매일 사람을 부른다.**

    ★ 표가 비어 있으므로 **이것이 지금 매일 나오는 답**이다. *"확인했고 낼 것이
      없다"* 이지 *"막혔다"* 가 아니다.
    """
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(),
        load_events=_사건조회기록(events=()),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "NOTHING_DUE", f"표가 비었는데 {out.status} 다"
    assert out.status != "BLOCKED"
    assert out.collected == []


def test_그날_것이_아니면_NOTHING_DUE_다() -> None:
    """★ **그날 것을 고르는 일은 재무가 한다.** 로더는 축의 사건을 통째로 넘긴다.

    ⚠️ 마스터가 날짜까지 고르면 *"오늘 무엇이 수금됐나"* 의 주인이 둘이 된다.
    """
    다른날 = CollectionEvent(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        financing_mode=축_모드,
        collection_date=date(2026, 1, 11),
        receivable_id="RCV-0001",
        target_received_total_krw=Decimal(1000000),
    )
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(),
        load_events=_사건조회기록(events=(다른날,)),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "NOTHING_DUE", f"그날 사건이 아닌데 {out.status} 다"


# ---------------------------------------------------------------------------
# 4. **호출 시점**에 읽는다 — 배선 시점이 아니다
# ---------------------------------------------------------------------------


def test_사건_조회를_임포트_시점에_하지_않는다() -> None:
    """🔴 **배선 시점에 고정하면 표에 한 줄 넣어도 앱을 다시 띄우기 전까지 안 돈다.**

    ★ 축 조회를 임포트 시점에 안 하는 것과 같은 규율이다 — 배선이 DB 를 요구하기
      시작하면 앱이 뜨는 조건이 조용히 늘어난다.
    """
    원천 = _사건조회기록()
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(),
        load_events=원천,
    )

    assert 원천.calls == [], "생성만 했는데 사건을 읽었다"

    adapter.collect(conn=None, as_of=AS_OF)
    adapter.collect(conn=None, as_of=AS_OF)

    assert len(원천.calls) == 2, (
        "호출마다 표를 다시 읽어야 한다 — 사건 목록은 마스터가 캐시할 값이 아니다"
    )


def test_축이_막히면_사건을_읽으러_가지_않는다() -> None:
    """★ 막을 것을 먼저 막는다. 축을 모르는데 표를 읽으면 **어느 축을 읽는지 모른다.**"""
    원천 = _사건조회기록()
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(sim_run_id=남의_실행),
        load_events=원천,
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert 원천.calls == [], "축이 다른데 사건을 읽으러 갔다"


# ---------------------------------------------------------------------------
# 5. 🔴 실제 표로 잰다 — `-m db`
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_다른_축_행이_섞이지_않는다() -> None:
    """🔴 **가짜 커넥션은 WHERE 를 실제로 못 건다.** 여기서만 그것이 보인다.

    ★ **커밋하지 않는다.** 넣고 읽고 되돌린다 — 공유 DB 에 시연용 사건을 남기면
      그것이 나중에 사실로 읽힌다.
    """
    from psycopg import sql

    from app.finance.db import get_connection

    conn = get_connection()
    표 = sql.SQL("{}.{}").format(
        sql.Identifier(get_db_schema()), sql.Identifier("master_collection_events")
    )

    class _안닫는커넥션:
        """로더가 트랜잭션을 닫지 못하게 감싼다 — 안 그러면 되돌릴 수 없다."""

        def cursor(self) -> Any:
            return conn.cursor()

        def close(self) -> None:
            return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s,%s,%s)").format(표),
                (BURN_IN_SIM_RUN_ID, 축_모드, AS_OF, "RCV-TEST-1", Decimal(100), "검사"),
            )
            cur.execute(
                sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s,%s,%s)").format(표),
                (BURN_IN_SIM_RUN_ID, 남의_모드, AS_OF, "RCV-TEST-2", Decimal(200), "검사"),
            )
            cur.execute(
                sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s,%s,%s)").format(표),
                (남의_실행, 축_모드, AS_OF, "RCV-TEST-3", Decimal(300), "검사"),
            )

        사건들 = read_collection_events(
            sim_run_id=BURN_IN_SIM_RUN_ID,
            financing_mode=축_모드,
            connect=_안닫는커넥션,
        )
    finally:
        conn.rollback()
        conn.close()

    나온것 = {사건.receivable_id for 사건 in 사건들}
    assert "RCV-TEST-1" in 나온것, "그 축의 사건이 안 나왔다"
    assert "RCV-TEST-2" not in 나온것, "financing_mode 가 다른 행이 섞였다"
    assert "RCV-TEST-3" not in 나온것, "sim_run_id 가 다른 행이 섞였다"
