"""매입대금이 현금곡선에 **한 번** 실린다는 계약.

🔴 이 파일이 지키는 것은 둘이다.

```text
안 실린 채무를 버리지 않는다   기일이 지나도 아직 안 실었으면 오늘 싣는다
두 번 싣지 않는다              같은 채무는 한 실행에서 한 번뿐이다
```

⚠️ 둘 중 하나만 지키는 고침이 둘 다 그럴듯하다. `== as_of` 는 앞을 어기고,
  `<= as_of` 로 여는 것은 뒤를 어긴다. 그래서 두 축을 같은 파일에서 잰다.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.finance.closing import FinanceDayClosing

SIM_RUN_ID = "SIM-CONSOLE-A"
OTHER_RUN = "SIM-CONSOLE-B"
AS_OF = date(2026, 1, 5)
PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


def _state(**over) -> dict:
    row = {
        "financing_mode": "LOAN_BASELINE",
        "current_cash_krw": Decimal(10_000),
        "receivables_krw": Decimal(0),
        "current_debt_krw": Decimal(0),
        "state_date": AS_OF,
    }
    row.update(over)
    return row


class _Cursor:
    """실제 질의의 **뜻대로** 답하는 대역.

    ★ `NOT EXISTS` 와 `ON CONFLICT` 를 흉내 내는 것이 요점이다. 그 둘을 무시하면
      «두 번 실었다» 를 잡는 검사가 아무것도 못 잡는다.
    """

    def __init__(self, conn):
        self.conn = conn
        self.rows: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = str(query)
        self.rows = []
        self.rowcount = 0
        self.conn.executed.append((text, params))
        if "sim_runs" in text:
            self.rows = [(PERIOD_START, PERIOD_END, "LOAN_BASELINE", None)]
        elif ".finance_states" in text and "state_date =" in text:
            self.rows = [_state()]
        elif ".finance_states" in text and "state_date <" in text:
            self.rows = []
        elif "SUM(original_amount_krw)" in text or "SUM(outstanding_amount_krw)" in text:
            self.rows = [{"amount": Decimal(0)}]
        elif ".payables" in text:
            run, _issued, due_ceiling = params[0], params[1], params[2]
            self.rows = [
                row
                for row in self.conn.payables
                if row["sim_run_id"] == run
                and row["due_date"] <= due_ceiling
                and row["status"] in {"OPEN", "PARTIAL"}
                and (run, row["payable_id"]) not in self.conn.recognized
            ]
        elif "INSERT INTO" in text and "finance_payable_closing_events" in text:
            key = (params[0], params[1])
            if key in self.conn.recognized:
                #  🔴 PK 가 막는다. 애플리케이션 판단이 아니라 DB 가 최종 방어선이다.
                self.rowcount = 0
            else:
                self.conn.recognized[key] = {
                    "recognized_date": params[2],
                    "recognized_amount_krw": params[3],
                    "due_date": params[4],
                }
                self.rowcount = 1
        elif "recognized_amount_krw" in text:
            run, recognized_date = params[0], params[1]
            self.rows = [
                {"recognized_amount_krw": event["recognized_amount_krw"]}
                for (event_run, _), event in self.conn.recognized.items()
                if event_run == run and event["recognized_date"] == recognized_date
            ]
        elif ".expenses" in text:
            self.rows = []
        elif "SUM(total_amount_krw)" in text:
            self.rows = [{"amount": Decimal(0)}]
        elif "INSERT INTO" in text and "daily_closings" in text:
            key = (params["sim_run_id"], params["close_date"])
            if key not in self.conn.closings:
                self.conn.closings[key] = dict(params)
                self.rowcount = 1
        elif "UPDATE" in text and "daily_closings" in text:
            key = (params["sim_run_id"], params["close_date"])
            self.conn.closings[key].update(params)
            self.rowcount = 1
        elif "inventory_lots" in text or "inventory" in text:
            self.rows = [{"quantity_kg": Decimal(0), "inventory_book_value_krw": Decimal(0)}]
        else:
            raise AssertionError(text)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, payables: list[dict]):
        self.payables = payables
        self.recognized: dict[tuple[str, str], dict] = {}
        self.closings: dict[tuple[str, date], dict] = {}
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _Cursor(self)


def _payable(
    payable_id: str,
    *,
    due: date,
    outstanding: str,
    run: str = SIM_RUN_ID,
    status: str = "OPEN",
) -> dict:
    return {
        "payable_id": payable_id,
        "sim_run_id": run,
        "due_date": due,
        "outstanding_amount_krw": Decimal(outstanding),
        "status": status,
    }


def _close(conn, *, as_of: date = AS_OF, sim_run_id: str = SIM_RUN_ID):
    return FinanceDayClosing().close(conn, as_of=as_of, sim_run_id=sim_run_id)


def _cash_out(conn, *, as_of: date = AS_OF, sim_run_id: str = SIM_RUN_ID) -> Decimal:
    return conn.closings[(sim_run_id, as_of)]["purchase_cash_out_krw"]


@pytest.fixture(autouse=True)
def _stub_inventory(monkeypatch):
    """재고 스냅샷은 이 파일의 관심이 아니다 — 마감이 돌 만큼만 세운다."""

    class _Inventory:
        quantity_kg = Decimal(0)
        inventory_book_value_krw = Decimal(0)

    monkeypatch.setattr(
        "app.finance.closing.load_inventory_snapshot_as_of",
        lambda *_a, **_k: _Inventory(),
    )
    monkeypatch.setattr("app.finance.closing.get_db_schema", lambda: "haetdeul")


# ---------------------------------------------------------------------------
# Case 1 · 늦게 생긴 payable
# ---------------------------------------------------------------------------


def test_a_payable_created_after_its_due_date_still_lands():
    """🔴 승인 D일에는 행이 없고 D+1 에 `due_date = D` 로 생긴다. 그래도 실려야 한다."""
    due = AS_OF - timedelta(days=1)
    conn = _Connection([_payable("PAY-1", due=due, outstanding="1294002")])

    _close(conn)

    assert _cash_out(conn) == Decimal(1_294_002)
    assert len(conn.recognized) == 1
    event = conn.recognized[(SIM_RUN_ID, "PAY-1")]
    assert event["recognized_date"] == AS_OF
    #  ★ 계약 기일은 고쳐 쓰지 않는다 — 실은 날과 나란히 남는다.
    assert event["due_date"] == due


# ---------------------------------------------------------------------------
# Case 2 · 같은 날 재마감
# ---------------------------------------------------------------------------


def test_reclosing_the_same_day_keeps_the_same_cash_out():
    """🔴 **새로 적힌 것만 더하면 재마감이 그 칸을 0 으로 덮는다.**"""
    conn = _Connection([_payable("PAY-1", due=AS_OF, outstanding="1294002")])

    _close(conn)
    first = _cash_out(conn)
    _close(conn)
    second = _cash_out(conn)
    _close(conn)
    third = _cash_out(conn)

    assert first == Decimal(1_294_002)
    assert second == first
    assert third == first
    assert len(conn.recognized) == 1


# ---------------------------------------------------------------------------
# Case 3 · 다음 날 중복 금지
# ---------------------------------------------------------------------------


def test_the_next_day_does_not_recount_the_same_payable():
    """🔴 `<= as_of` 로 열기만 하면 여기서 무너진다 — 갚을 때까지 날마다 실린다."""
    conn = _Connection([_payable("PAY-1", due=AS_OF, outstanding="1294002")])

    _close(conn)
    _close(conn, as_of=AS_OF + timedelta(days=1))

    assert _cash_out(conn) == Decimal(1_294_002)
    assert _cash_out(conn, as_of=AS_OF + timedelta(days=1)) == Decimal(0)
    assert len(conn.recognized) == 1


# ---------------------------------------------------------------------------
# Case 4 · 정상 당일 payable
# ---------------------------------------------------------------------------


def test_a_payable_due_today_lands_once_and_not_tomorrow():
    conn = _Connection([_payable("PAY-1", due=AS_OF, outstanding="500")])

    _close(conn)
    _close(conn, as_of=AS_OF + timedelta(days=1))

    assert _cash_out(conn) == Decimal(500)
    assert _cash_out(conn, as_of=AS_OF + timedelta(days=1)) == Decimal(0)


# ---------------------------------------------------------------------------
# Case 5 · 복수 payable
# ---------------------------------------------------------------------------


def test_multiple_payables_sum_and_get_one_event_each():
    conn = _Connection(
        [
            _payable("PAY-1", due=AS_OF, outstanding="300"),
            _payable("PAY-2", due=AS_OF - timedelta(days=2), outstanding="700"),
        ]
    )

    _close(conn)

    assert _cash_out(conn) == Decimal(1_000)
    assert len(conn.recognized) == 2
    assert {key[1] for key in conn.recognized} == {"PAY-1", "PAY-2"}


# ---------------------------------------------------------------------------
# Case 6 · PARTIAL
# ---------------------------------------------------------------------------


def test_a_partial_payable_recognizes_only_what_is_still_outstanding():
    """🔴 원금이 아니라 **남은 미지급액**이다. 원금을 실으면 이미 나간 몫을 다시 센다."""
    conn = _Connection(
        [_payable("PAY-1", due=AS_OF, outstanding="700", status="PARTIAL")]
    )

    _close(conn)

    assert _cash_out(conn) == Decimal(700)
    assert conn.recognized[(SIM_RUN_ID, "PAY-1")]["recognized_amount_krw"] == Decimal(700)


# ---------------------------------------------------------------------------
# Case 7 · 실행 격리
# ---------------------------------------------------------------------------


def test_another_runs_payable_is_never_recognized_here():
    conn = _Connection(
        [
            _payable("PAY-1", due=AS_OF, outstanding="300"),
            _payable("PAY-2", due=AS_OF, outstanding="900", run=OTHER_RUN),
        ]
    )

    _close(conn)

    assert _cash_out(conn) == Decimal(300)
    assert list(conn.recognized) == [(SIM_RUN_ID, "PAY-1")]


def test_the_recognition_queries_carry_the_run():
    conn = _Connection([_payable("PAY-1", due=AS_OF, outstanding="300")])

    _close(conn)

    insert = next(
        (text, params)
        for text, params in conn.executed
        if "INSERT INTO" in text and "finance_payable_closing_events" in text
    )
    total = next(
        (text, params)
        for text, params in conn.executed
        if "recognized_amount_krw" in text and "SELECT" in text
    )
    assert insert[1][0] == SIM_RUN_ID
    assert "ON CONFLICT (sim_run_id, payable_id) DO NOTHING" in insert[0]
    assert "sim_run_id = %s" in total[0]
    assert total[1][0] == SIM_RUN_ID


# ---------------------------------------------------------------------------
# 주말 이월 · 상태
# ---------------------------------------------------------------------------


def test_a_weekend_due_date_waits_for_the_monday_cash_date():
    """기일이 토요일이면 현금은 월요일에 나간다 — 토요일 마감은 아직 안 싣는다."""
    saturday = date(2026, 1, 3)
    monday = date(2026, 1, 5)
    conn = _Connection([_payable("PAY-1", due=saturday, outstanding="400")])

    _close(conn, as_of=saturday)
    assert _cash_out(conn, as_of=saturday) == Decimal(0)
    assert conn.recognized == {}

    _close(conn, as_of=monday)
    assert _cash_out(conn, as_of=monday) == Decimal(400)


def test_a_settled_payable_is_not_recognized():
    """SETTLED 는 조회 자체에서 빠진다 — 이미 나간 돈을 다시 싣지 않는다."""
    conn = _Connection(
        [_payable("PAY-1", due=AS_OF, outstanding="0", status="SETTLED")]
    )

    _close(conn)

    assert _cash_out(conn) == Decimal(0)
    assert conn.recognized == {}


def test_the_payable_ledger_is_never_written_by_closing():
    """🔴 이번 판은 **귀속만** 적는다. 지급 처리(paid·status)는 건드리지 않는다."""
    conn = _Connection([_payable("PAY-1", due=AS_OF, outstanding="300")])

    _close(conn)

    writes = [
        text
        for text, _ in conn.executed
        if ("UPDATE" in text or "INSERT" in text) and ".payables" in text
    ]
    assert writes == []
    assert conn.payables[0]["status"] == "OPEN"
    assert conn.payables[0]["outstanding_amount_krw"] == Decimal(300)
