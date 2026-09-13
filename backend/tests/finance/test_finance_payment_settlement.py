"""매입대금 **실제 지급** 계약 (#637).

🔴 이 파일이 지키는 것은 하나다 — **다섯 사실이 같이 움직인다.**

```text
payables.paid_amount_krw          늘어난다
payables.outstanding_amount_krw   줄어든다
payables.status                   OPEN/PARTIAL → PARTIAL/SETTLED
finance_states.current_cash_krw                  줄어든다
finance_states.unsettled_purchase_payables_krw   줄어든다
```

⚠️ 하나만 움직이면 장부가 서로 다른 말을 한다. #637 이전이 정확히 그 상태였다 —
  곡선에는 매입유출이 실렸는데 채무는 `OPEN` 이고 현금은 그대로였다.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.finance.db import FinanceDataNotReady
from app.finance.settlement import settle_recognized_payables

SIM_RUN_ID = "SIM-CONSOLE-A"
OTHER_RUN = "SIM-CONSOLE-B"
AS_OF = date(2026, 1, 5)


class _Cursor:
    """실제 질의의 뜻대로 답하고, UPDATE 는 대역 장부에 **실제로 반영**한다.

    ★ 반영하지 않는 대역이면 «줄었다» 를 잴 수 없고, 그러면 이 파일은 아무것도 안
      지킨다.
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
        self.conn.executed.append((text, list(params or [])))
        if "e.recognized_amount_krw" in text:
            run, recognized_date = params[0], params[1]
            self.rows = [
                {
                    "payable_id": row["payable_id"],
                    "paid_amount_krw": row["paid_amount_krw"],
                    "outstanding_amount_krw": row["outstanding_amount_krw"],
                    "original_amount_krw": row["original_amount_krw"],
                    "cancelled_amount_krw": row["cancelled_amount_krw"],
                    "recognized_amount_krw": event["recognized_amount_krw"],
                }
                for row in self.conn.payables
                for (event_run, event_payable), event in self.conn.recognized.items()
                if event_run == run
                and event_payable == row["payable_id"]
                and row["sim_run_id"] == run
                and event["recognized_date"] == recognized_date
                and row["status"] in {"OPEN", "PARTIAL"}
                and row["outstanding_amount_krw"] > 0
            ]
        elif "UPDATE" in text and ".payables" in text:
            paid, outstanding, status, settled_date, payable_id = params
            row = next(r for r in self.conn.payables if r["payable_id"] == payable_id)
            row.update(
                paid_amount_krw=paid,
                outstanding_amount_krw=outstanding,
                status=status,
                settled_date=settled_date,
            )
            self.rowcount = 1
        elif "finance_states" in text and "FOR UPDATE" in text:
            run, as_of_param = params[0], params[1]
            self.rows = [
                dict(row)
                for row in self.conn.states
                if row["sim_run_id"] == run and row["state_date"] == as_of_param
            ]
        elif "UPDATE" in text and "finance_states" in text:
            cash, unsettled, state_id = params
            row = next(
                r for r in self.conn.states if r["finance_state_id"] == state_id
            )
            row.update(current_cash_krw=cash, unsettled_purchase_payables_krw=unsettled)
            self.rowcount = 1
        else:
            raise AssertionError(text)

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, payables, *, recognized=None, states=None):
        self.payables = payables
        self.recognized = recognized if recognized is not None else {}
        self.states = states if states is not None else [_state()]
        self.executed: list[tuple[str, list]] = []

    def cursor(self):
        return _Cursor(self)

    def payable(self, payable_id: str) -> dict:
        return next(row for row in self.payables if row["payable_id"] == payable_id)

    @property
    def state(self) -> dict:
        return self.states[0]


def _state(
    *,
    run: str = SIM_RUN_ID,
    state_date: date = AS_OF,
    cash: str = "10000000",
    unsettled: str = "5000000",
    state_id: str | None = None,
) -> dict:
    return {
        "finance_state_id": state_id or f"FIN-{run}-{state_date.isoformat()}",
        "sim_run_id": run,
        "state_date": state_date,
        "current_cash_krw": Decimal(cash),
        "unsettled_purchase_payables_krw": Decimal(unsettled),
    }


def _payable(
    payable_id: str,
    *,
    outstanding: str,
    paid: str = "0",
    original: str | None = None,
    status: str = "OPEN",
    run: str = SIM_RUN_ID,
) -> dict:
    return {
        "payable_id": payable_id,
        "sim_run_id": run,
        "paid_amount_krw": Decimal(paid),
        "outstanding_amount_krw": Decimal(outstanding),
        "original_amount_krw": Decimal(original or outstanding),
        "cancelled_amount_krw": Decimal(0),
        "status": status,
        "settled_date": None,
    }


def _recognition(
    payable_id: str, *, amount: str, run: str = SIM_RUN_ID, on: date = AS_OF
) -> dict:
    return {(run, payable_id): {"recognized_date": on, "recognized_amount_krw": Decimal(amount)}}


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    monkeypatch.setattr("app.finance.settlement.get_db_schema", lambda: "haetdeul")


# ---------------------------------------------------------------------------
# 1 · 전액 지급 — 다섯 사실이 같이 움직인다
# ---------------------------------------------------------------------------


def test_an_open_payable_is_paid_in_full():
    conn = _Connection(
        [_payable("PAY-1", outstanding="1294002")],
        recognized=_recognition("PAY-1", amount="1294002"),
    )

    result = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    row = conn.payable("PAY-1")
    assert result.total_paid_krw == Decimal(1_294_002)
    assert row["paid_amount_krw"] == Decimal(1_294_002)
    assert row["outstanding_amount_krw"] == Decimal(0)
    assert row["status"] == "SETTLED"
    assert row["settled_date"] == AS_OF
    assert conn.state["current_cash_krw"] == Decimal(10_000_000) - Decimal(1_294_002)
    assert conn.state["unsettled_purchase_payables_krw"] == Decimal(5_000_000) - Decimal(
        1_294_002
    )


# ---------------------------------------------------------------------------
# 2 · PARTIAL 채무의 추가 지급
# ---------------------------------------------------------------------------


def test_a_partial_payable_pays_only_what_is_left():
    """원금 1,000 중 300 이 이미 나갔으면 이번에 나가는 것은 남은 700 이다."""
    conn = _Connection(
        [_payable("PAY-1", outstanding="700", paid="300", original="1000", status="PARTIAL")],
        recognized=_recognition("PAY-1", amount="700"),
    )

    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    row = conn.payable("PAY-1")
    assert row["paid_amount_krw"] == Decimal(1_000)
    assert row["outstanding_amount_krw"] == Decimal(0)
    assert row["status"] == "SETTLED"
    assert conn.state["current_cash_krw"] == Decimal(10_000_000) - Decimal(700)


def test_paying_less_than_the_balance_keeps_it_partial():
    """🔴 인식액이 잔액보다 작으면 **덜 낸 것**이고, 그때는 아직 끝난 날이 아니다."""
    conn = _Connection(
        [_payable("PAY-1", outstanding="1000")],
        recognized=_recognition("PAY-1", amount="400"),
    )

    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    row = conn.payable("PAY-1")
    assert row["paid_amount_krw"] == Decimal(400)
    assert row["outstanding_amount_krw"] == Decimal(600)
    assert row["status"] == "PARTIAL"
    #  ★ 다 갚은 날만 적는다.
    assert row["settled_date"] is None


# ---------------------------------------------------------------------------
# 3 · 멱등 — 다시 돌려도 두 번 나가지 않는다
# ---------------------------------------------------------------------------


def test_running_settlement_twice_pays_once():
    conn = _Connection(
        [_payable("PAY-1", outstanding="1294002")],
        recognized=_recognition("PAY-1", amount="1294002"),
    )

    first = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)
    second = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)
    third = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert first.total_paid_krw == Decimal(1_294_002)
    #  🔴 두 번째부터는 낼 돈이 없다 — 장부 자신이 멱등을 말한다.
    assert second.total_paid_krw == Decimal(0)
    assert third.total_paid_krw == Decimal(0)
    assert conn.state["current_cash_krw"] == Decimal(10_000_000) - Decimal(1_294_002)


def test_a_second_run_does_not_touch_the_state_at_all():
    conn = _Connection(
        [_payable("PAY-1", outstanding="500")],
        recognized=_recognition("PAY-1", amount="500"),
    )
    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)
    conn.executed.clear()

    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    #  ⚠️ `FOR UPDATE` 도 "UPDATE" 를 담는다 — 쓰기는 `SET` 으로 가른다.
    writes = [text for text, _ in conn.executed if "SET " in text]
    assert writes == []


# ---------------------------------------------------------------------------
# 4 · 실행 격리
# ---------------------------------------------------------------------------


def test_another_runs_payable_is_never_paid_here():
    conn = _Connection(
        [
            _payable("PAY-1", outstanding="300"),
            _payable("PAY-2", outstanding="900", run=OTHER_RUN),
        ],
        recognized={
            **_recognition("PAY-1", amount="300"),
            **_recognition("PAY-2", amount="900", run=OTHER_RUN),
        },
    )

    result = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert result.total_paid_krw == Decimal(300)
    assert conn.payable("PAY-2")["status"] == "OPEN"
    assert conn.payable("PAY-2")["outstanding_amount_krw"] == Decimal(900)


def test_every_settlement_query_carries_the_run():
    conn = _Connection(
        [_payable("PAY-1", outstanding="300")],
        recognized=_recognition("PAY-1", amount="300"),
    )

    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    for text, params in conn.executed:
        if "UPDATE" in text:
            continue
        assert "sim_run_id = %s" in text, text
        assert params[0] == SIM_RUN_ID, text


# ---------------------------------------------------------------------------
# 5 · 미래 채무를 미리 내지 않는다
# ---------------------------------------------------------------------------


def test_a_payable_recognized_on_another_day_is_not_paid_today():
    """🔴 인식일이 오늘이 아니면 오늘 나갈 돈이 아니다.

    인식 자체가 이미 기일과 주말 이월을 지나온 결과라, 여기서 날짜 규칙을 다시 세우지
    않는다 — 두 벌이 되면 한쪽만 고치는 날 현금이 다른 날로 간다.
    """
    conn = _Connection(
        [_payable("PAY-1", outstanding="300")],
        recognized=_recognition("PAY-1", amount="300", on=AS_OF + timedelta(days=1)),
    )

    result = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert result.total_paid_krw == Decimal(0)
    assert conn.payable("PAY-1")["status"] == "OPEN"
    assert conn.state["current_cash_krw"] == Decimal(10_000_000)


def test_a_payable_with_no_recognition_is_never_paid():
    """인식 없이 내면 곡선에 없는 현금이 나간다."""
    conn = _Connection([_payable("PAY-1", outstanding="300")], recognized={})

    result = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert result.total_paid_krw == Decimal(0)
    assert conn.payable("PAY-1")["status"] == "OPEN"


# ---------------------------------------------------------------------------
# 6 · 복수 채무
# ---------------------------------------------------------------------------


def test_multiple_payables_are_paid_and_summed():
    conn = _Connection(
        [
            _payable("PAY-1", outstanding="300"),
            _payable("PAY-2", outstanding="700"),
        ],
        recognized={
            **_recognition("PAY-1", amount="300"),
            **_recognition("PAY-2", amount="700"),
        },
    )

    result = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert result.total_paid_krw == Decimal(1_000)
    assert {row.payable_id for row in result.settled} == {"PAY-1", "PAY-2"}
    assert conn.state["current_cash_krw"] == Decimal(10_000_000) - Decimal(1_000)
    #  ★ 상태는 **한 번** 줄어든다 — 채무마다 따로 빼면 같은 행을 여러 번 덮는다.
    state_writes = [
        text for text, _ in conn.executed if "SET " in text and "finance_states" in text
    ]
    assert len(state_writes) == 1


# ---------------------------------------------------------------------------
# 7 · 인식액이 잔액보다 클 때
# ---------------------------------------------------------------------------


def test_it_never_pays_more_than_the_ledger_still_owes():
    """🔴 인식 뒤에 다른 경로가 일부를 갚았을 수 있다. 넘겨 내면 음수 잔액이 선다."""
    conn = _Connection(
        [_payable("PAY-1", outstanding="200", paid="800", original="1000", status="PARTIAL")],
        recognized=_recognition("PAY-1", amount="1000"),
    )

    result = settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert result.total_paid_krw == Decimal(200)
    assert conn.payable("PAY-1")["outstanding_amount_krw"] == Decimal(0)
    assert conn.payable("PAY-1")["paid_amount_krw"] == Decimal(1_000)


# ---------------------------------------------------------------------------
# 8 · 상태 축
# ---------------------------------------------------------------------------


def test_cash_may_go_negative_because_that_is_a_fact():
    """🔴 돈이 모자랐다는 것은 사실이다. 막으면 가장 위험한 날의 장부가 사라진다."""
    conn = _Connection(
        [_payable("PAY-1", outstanding="9000")],
        recognized=_recognition("PAY-1", amount="9000"),
        states=[_state(cash="5000", unsettled="9000")],
    )

    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert conn.state["current_cash_krw"] == Decimal(-4_000)


def test_unsettled_payables_never_go_negative():
    """⚠️ «채무가 마이너스» 는 없는 사실이고, 그 값으로 다음 판단이 돈다."""
    conn = _Connection(
        [_payable("PAY-1", outstanding="900")],
        recognized=_recognition("PAY-1", amount="900"),
        states=[_state(unsettled="100")],
    )

    settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)

    assert conn.state["unsettled_purchase_payables_krw"] == Decimal(0)


def test_two_state_rows_on_the_same_day_stop_the_settlement():
    """🔴 축이 둘이면 어느 장부에서 돈이 나갔는지 고르지 않는다."""
    conn = _Connection(
        [_payable("PAY-1", outstanding="300")],
        recognized=_recognition("PAY-1", amount="300"),
        states=[_state(state_id="FIN-A"), _state(state_id="FIN-B")],
    )

    with pytest.raises(FinanceDataNotReady):
        settle_recognized_payables(conn, sim_run_id=SIM_RUN_ID, as_of=AS_OF)


def test_a_blank_run_is_refused():
    conn = _Connection([], recognized={})

    with pytest.raises(ValueError):
        settle_recognized_payables(conn, sim_run_id="   ", as_of=AS_OF)
