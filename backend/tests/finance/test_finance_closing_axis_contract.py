"""하루 넘김과 일 마감이 **같은 실행축을 말한다.**

★ 마스터 결정 ㄷ (확정) — 마감은 **실행축에서 실제로 일어난 사실만** 기록한다.
  무차입/차입 A/B 비교는 `sim_run` 을 하나 더 실제로 걸어서 분리한다.

  ```text
  FinanceDayOpening   실행축 하나를 전진시킨다
  FinanceDayClosing   같은 실행축 하나로 닫는다
  ```

🔴 **예전 계약은 여기서 뒤집혔다.** 마감이 같은 날짜의 `BASE_NO_LOAN` 을 따로 요구해,
   정상적으로 연 하루가 `base_finance_state` 로 막혔다 (실측 2025-12-02~12-30).
   이제 그 조회는 없다 — 요구하지도, 만들지도 않는다.

★ 두 현금 칸의 뜻.

  ```text
  loan_cash_balance_krw = C          실행축 현금 그대로        (대출 포함 곡선)
  base_cash_balance_krw = C - D      남은 원금만큼을 뺀 값      (대출 제외 곡선)
  ```

  `D` 는 **남은 원금 잔액**(`current_debt_krw`)이지 누적 실행액이 아니다. 누적 실행액을
  빼면 원금을 갚을수록 이 값이 낮아진다 — 갚은 돈은 이미 `C` 에서 나갔으므로 두 번
  빼는 셈이다. 이 파일의 원금 상환 검사가 그것을 잡는다.

⚠️ `base_cash_balance_krw` 는 *"대출이 없었다면 있었을 현금"* 이 **아니다.** 그건 별도
  `sim_run` 이 답할 질문이다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance import closing
from app.finance.day_open import FinanceDayOpening
from app.finance.db import FinanceDataNotReady, InventorySnapshot

CARRY_FROM = date(2026, 1, 5)
AS_OF = date(2026, 1, 6)
SIM_RUN_ID = "SIM-BURNIN-202512"
LOAN_MODE = "LOAN_BASELINE"
BASE_MODE = "BASE_NO_LOAN"

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


def _state(
    state_date: date,
    mode: str,
    *,
    cash: Decimal,
    debt: Decimal,
    receivables: Decimal = Decimal(700),
    state_id: str | None = None,
) -> dict:
    return {
        "finance_state_id": state_id or f"FIN-{mode}-{state_date:%Y%m%d}",
        "sim_run_id": SIM_RUN_ID,
        "state_date": state_date,
        "state_type": "DAY",
        "financing_mode": mode,
        "current_cash_krw": cash,
        "minimum_operating_cash_krw": Decimal(15_902_640),
        "committed_outflows_krw": Decimal(0),
        "unsettled_purchase_payables_krw": Decimal(0),
        "receivables_krw": receivables,
        "inventory_book_value_krw": Decimal(3_100_000),
        "operational_inventory_value_krw": Decimal(2_900_000),
        "current_debt_krw": debt,
        "recommended_loan_amount_krw": Decimal(0),
        "note": "fixture",
    }


class _Cursor:
    """하루 넘김과 마감이 **같은 가짜 원장**을 본다.

    ★ 하루 넘김은 이름 있는 인자를, 마감은 자리 인자를 쓴다 — 둘을 그대로 흉내낸다.
    """

    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.conn.executed.append((text, params))
        self.rows = []
        self.rowcount = 0
        stripped = text.lstrip()

        if "v_current_finance_state" in text:
            self.rows = list(self.conn.axes)
        elif "sim_runs" in text:
            self.rows = [
                (PERIOD_START, PERIOD_END, self.conn.run_mode, self.conn.config_json)
            ]
        elif ".finance_states" in text and "finance_state_id = %s" in text:
            # baseline 은 **PK 한 개**로만 찾는다 — 날짜도 state_type 도 쓰지 않는다.
            wanted = params[0]
            self.rows = [
                {
                    "sim_run_id": row["sim_run_id"],
                    "financing_mode": row["financing_mode"],
                    "current_cash_krw": row["current_cash_krw"],
                    "receivables_krw": row["receivables_krw"],
                    "current_debt_krw": row["current_debt_krw"],
                }
                for row in self.conn.baseline_states
                if row["finance_state_id"] == wanted
            ]
        elif stripped.startswith("SELECT finance_state_id"):
            # 하루 넘김의 존재 확인.
            self.rows = [
                (row["finance_state_id"],)
                for row in self.conn.states
                if row["sim_run_id"] == params["sim_run_id"]
                and row["financing_mode"] == params["financing_mode"]
                and row["state_date"] == params["state_date"]
            ][:2]
        elif "INSERT INTO" in text and "finance_states" in text:
            source = next(
                (
                    row
                    for row in self.conn.states
                    if row["sim_run_id"] == params["sim_run_id"]
                    and row["financing_mode"] == params["financing_mode"]
                    and row["state_date"] == params["carry_from"]
                ),
                None,
            )
            already = any(
                row["finance_state_id"] == params["finance_state_id"]
                for row in self.conn.states
            )
            if source is not None and not already:
                carried = deepcopy(source)
                carried.update(
                    finance_state_id=params["finance_state_id"],
                    state_date=params["as_of"],
                    state_type=params["state_type"],
                    note=params["note"],
                )
                self.conn.states.append(carried)
                self.rowcount = 1
        elif ".finance_states" in text and "state_date = %s" in text:
            sim_run_id, mode, state_date = params
            self.rows = [
                self._state_row(row)
                for row in self.conn.states
                if row["sim_run_id"] == sim_run_id
                and row["financing_mode"] == mode
                and row["state_date"] == state_date
            ][:2]
        elif ".finance_states" in text and "state_date < %s" in text:
            sim_run_id, mode, state_date = params
            matching = [
                row
                for row in self.conn.states
                if row["sim_run_id"] == sim_run_id
                and row["financing_mode"] == mode
                and row["state_date"] < state_date
            ]
            matching.sort(key=lambda row: row["state_date"], reverse=True)
            self.rows = [self._prior_row(row) for row in matching[:2]]
        elif "SUM(original_amount_krw)" in text:
            self.rows = [{"amount": self.conn.issued_receivables}]
        elif "SUM(outstanding_amount_krw)" in text:
            self.rows = [{"amount": self.conn.outstanding_receivables}]
        elif ".payables" in text:
            self.rows = list(self.conn.payables)
        elif "recognized_amount_krw" in text:
            #  이 파일의 대역에는 채무가 없다 (payables=()) — 귀속도 없다.
            self.rows = []
        elif ".expenses" in text:
            self.rows = list(self.conn.expenses)
        elif "SUM(total_amount_krw)" in text:
            self.rows = [{"amount": self.conn.sales_recognized}]
        elif "INSERT INTO" in text and "daily_closings" in text:
            key = (params["sim_run_id"], params["close_date"])
            if key not in self.conn.closings:
                self.conn.closings[key] = dict(params, closed=True)
                self.rowcount = 1
        elif "UPDATE" in text and "daily_closings" in text:
            key = (params["sim_run_id"], params["close_date"])
            self.conn.closings[key].update(params, closed=True)
            self.rowcount = 1
        else:
            raise AssertionError(text)

    @staticmethod
    def _state_row(row):
        return {
            "financing_mode": row["financing_mode"],
            "current_cash_krw": row["current_cash_krw"],
            "receivables_krw": row["receivables_krw"],
            "current_debt_krw": row["current_debt_krw"],
        }

    @staticmethod
    def _prior_row(row):
        return {
            "state_date": row["state_date"],
            "financing_mode": row["financing_mode"],
            "current_cash_krw": row["current_cash_krw"],
            "receivables_krw": row["receivables_krw"],
            "current_debt_krw": row["current_debt_krw"],
        }

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(
        self,
        states=(),
        *,
        run_mode=LOAN_MODE,
        axes=None,
        config_json=None,
        baseline_states=(),
        payables=(),
        expenses=(),
        issued_receivables=Decimal(0),
        outstanding_receivables=Decimal(700),
        sales_recognized=Decimal(0),
    ):
        self.states = [deepcopy(row) for row in states]
        self.run_mode = run_mode
        self.config_json = {} if config_json is None else config_json
        self.baseline_states = [deepcopy(row) for row in baseline_states]
        self.axes = list(axes if axes is not None else [(SIM_RUN_ID, run_mode)])
        self.payables = payables
        self.expenses = expenses
        self.issued_receivables = issued_receivables
        self.outstanding_receivables = outstanding_receivables
        self.sales_recognized = sales_recognized
        self.closings = {}
        self.executed = []

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _schema():
    snapshot = InventorySnapshot(Decimal(123), Decimal(456), Decimal(456))
    with (
        patch("app.finance.day_open.get_db_schema", return_value="haetdeul"),
        patch("app.finance.closing.get_db_schema", return_value="haetdeul"),
        patch("app.finance.day_open.load_inventory_snapshot_as_of", return_value=snapshot),
        patch("app.finance.closing.load_inventory_snapshot_as_of", return_value=snapshot),
    ):
        yield


def _open_and_close(conn, *, as_of=AS_OF, carry_from=CARRY_FROM):
    FinanceDayOpening().open_day(conn, as_of=as_of, carry_from=carry_from)
    return closing.FinanceDayClosing().close(conn, as_of=as_of, sim_run_id=SIM_RUN_ID)


def _row(conn, *, as_of=AS_OF):
    return conn.closings[(SIM_RUN_ID, as_of)]


def _modes(conn, state_date):
    return sorted(
        row["financing_mode"] for row in conn.states if row["state_date"] == state_date
    )


# ---------------------------------------------------------------------------
# 연 하루가 그날 닫힌다
# ---------------------------------------------------------------------------


def test_opened_day_closes_on_the_same_execution_axis():
    """🔴 예전에는 여기서 `base_finance_state` 로 막혔다."""
    conn = _Conn([_state(CARRY_FROM, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))])

    result = _open_and_close(conn)

    assert result.status == "CLOSED"
    assert list(conn.closings) == [(SIM_RUN_ID, AS_OF)]


def test_closing_never_asks_for_the_base_no_loan_axis():
    """★ 요구하지도, 만들지도 않는다."""
    conn = _Conn([_state(CARRY_FROM, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))])

    _open_and_close(conn)

    assert _modes(conn, AS_OF) == [LOAN_MODE]
    state_queries = [
        params
        for text, params in conn.executed
        if ".finance_states" in text and isinstance(params, list)
    ]
    assert state_queries, "마감이 상태를 읽지 않았다"
    assert all(BASE_MODE not in params for params in state_queries)


def test_cash_columns_split_the_remaining_principal():
    """`C = 12,000` · `D = 3,000` → 대출 포함 12,000 · 대출 제외 9,000."""
    conn = _Conn([_state(CARRY_FROM, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))])

    _open_and_close(conn)

    row = _row(conn)
    assert row["loan_cash_balance_krw"] == Decimal(12_000)
    assert row["base_cash_balance_krw"] == Decimal(9_000)


def test_execution_axis_comes_from_the_run_not_from_the_current_view():
    """★ 축은 **건네받은 `sim_run_id`** 가 정한다 — "지금" 축이 아니다.

    과거 실행을 다시 닫을 때 `v_current_finance_state` 를 보면 남의 실행 축 위에서
    닫게 된다.
    """
    conn = _Conn([_state(CARRY_FROM, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))])

    _open_and_close(conn)

    sim_run_queries = [text for text, _ in conn.executed if "sim_runs" in text]
    assert sim_run_queries, "실행 축을 sim_runs 에서 읽지 않았다"
    assert "financing_mode" in sim_run_queries[0]


# ---------------------------------------------------------------------------
# 원금 상환 — 이 파일에서 가장 중요한 검사
# ---------------------------------------------------------------------------


def test_principal_repayment_does_not_lower_the_debt_free_curve():
    """🔴 **누적 실행액을 빼는 구현을 여기서 잡는다.**

    ```text
    Day1  C 12,000  D 3,000   → loan 12,000 · base 9,000
    Day2  C 11,000  D 2,000   → loan 11,000 · base 9,000
    ```

    원금 1,000 을 갚으면 현금도 1,000 줄어든다. 남은 원금도 1,000 줄었으므로 대출
    제외 곡선은 **그대로 9,000** 이다. 누적 실행액 3,000 을 계속 빼는 구현이라면
    8,000 이 나온다 — 갚은 돈을 두 번 빼는 것이다.
    """
    day1 = date(2026, 1, 6)
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(CARRY_FROM, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(day1, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(day2, LOAN_MODE, cash=Decimal(11_000), debt=Decimal(2_000)),
        ]
    )

    closing.FinanceDayClosing().close(conn, as_of=day1, sim_run_id=SIM_RUN_ID)
    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    before = _row(conn, as_of=day1)
    after = _row(conn, as_of=day2)

    assert (before["loan_cash_balance_krw"], before["base_cash_balance_krw"]) == (
        Decimal(12_000),
        Decimal(9_000),
    )
    assert after["loan_cash_balance_krw"] == Decimal(11_000)
    assert after["base_cash_balance_krw"] == Decimal(9_000)
    # 🔴 누적 실행액을 빼는 구현이면 여기가 8,000 이 된다.
    assert after["base_cash_balance_krw"] != Decimal(8_000)


def test_repayment_day_has_no_new_borrowing():
    """상환은 **음수 차입이 아니다** — 그날 새로 빌린 돈이 없을 뿐이다."""
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(day2, LOAN_MODE, cash=Decimal(11_000), debt=Decimal(2_000)),
        ]
    )

    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    assert _row(conn, as_of=day2)["loan_execution_krw"] == Decimal(0)


def test_additional_borrowing_is_the_daily_increase_only():
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(day2, LOAN_MODE, cash=Decimal(17_000), debt=Decimal(8_000)),
        ]
    )

    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    row = _row(conn, as_of=day2)
    assert row["loan_execution_krw"] == Decimal(5_000)
    assert row["base_cash_balance_krw"] == Decimal(9_000)


def test_unchanged_debt_reports_no_execution_and_a_moving_debt_free_curve():
    """부채가 그대로면 대출 제외 곡선은 **영업 현금 흐름만큼** 움직인다."""
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(day2, LOAN_MODE, cash=Decimal(12_500), debt=Decimal(3_000)),
        ]
    )

    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    row = _row(conn, as_of=day2)
    assert row["loan_execution_krw"] == Decimal(0)
    assert row["base_cash_balance_krw"] == Decimal(9_500)


def test_full_repayment_makes_both_curves_equal():
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(day2, LOAN_MODE, cash=Decimal(9_000), debt=Decimal(0)),
        ]
    )

    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    row = _row(conn, as_of=day2)
    assert row["base_cash_balance_krw"] == row["loan_cash_balance_krw"] == Decimal(9_000)
    assert row["loan_execution_krw"] == Decimal(0)


def test_first_day_without_a_prior_state_counts_the_whole_debt_as_execution():
    # 직전 상태가 없는 날은 그날 발행한 채권이 곧 잔액이다 — 수금 0.
    conn = _Conn(
        [_state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))],
        issued_receivables=Decimal(700),
    )

    closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    row = _row(conn)
    assert row["loan_execution_krw"] == Decimal(3_000)
    assert row["base_cash_balance_krw"] == Decimal(9_000)


# ---------------------------------------------------------------------------
# 실행축이 BASE_NO_LOAN 인 실행 — 별도 sim_run 으로 A/B 를 걷는 길
# ---------------------------------------------------------------------------


def test_a_base_no_loan_run_closes_too():
    """★ 마감이 `LOAN_BASELINE` 을 가정하면 A/B 를 별도 실행으로 거는 길이 막힌다."""
    conn = _Conn(
        [_state(CARRY_FROM, BASE_MODE, cash=Decimal(10_000), debt=Decimal(0))],
        run_mode=BASE_MODE,
    )

    result = _open_and_close(conn)

    assert result.status == "CLOSED"
    row = _row(conn)
    assert row["loan_cash_balance_krw"] == Decimal(10_000)
    assert row["base_cash_balance_krw"] == Decimal(10_000)
    assert row["loan_execution_krw"] == Decimal(0)
    assert _modes(conn, AS_OF) == [BASE_MODE]


# ---------------------------------------------------------------------------
# fail-closed 는 그대로
# ---------------------------------------------------------------------------


def test_missing_execution_state_blocks_the_close():
    conn = _Conn([_state(CARRY_FROM, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))])

    with pytest.raises(FinanceDataNotReady) as raised:
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert raised.value.key == "finance_state"
    assert not conn.closings


def test_two_states_on_the_execution_axis_are_ambiguous():
    conn = _Conn(
        [
            _state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000)),
            _state(
                AS_OF, LOAN_MODE, cash=Decimal(9_999), debt=Decimal(1), state_id="FIN-DUP"
            ),
        ]
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert raised.value.key == "finance_state_ambiguous"


def test_debt_larger_than_cash_is_a_fact_not_a_blocked_close():
    """🔴 **음수 대출제외 현금은 자료 미준비가 아니다.**

    남은 원금이 보유 현금보다 크면 대출 제외 곡선은 음수다 — *"대출을 빼고 보면
    이만큼 모자란다"* 는 재무 사실이다. 막으면 **가장 위험한 날의 마감이 통째로
    사라진다**: 위험을 기록하지 않는 것과 위험이 없는 것은 다르다.

    ★ 여기서 *"현금은 0 이상"* 정책을 새로 만들지 않는다.
    """
    conn = _Conn(
        [_state(AS_OF, LOAN_MODE, cash=Decimal(10_000), debt=Decimal(12_000))],
        issued_receivables=Decimal(700),
    )

    result = closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert result.status == "CLOSED"
    row = _row(conn)
    assert row["loan_cash_balance_krw"] == Decimal(10_000)
    assert row["base_cash_balance_krw"] == Decimal(-2_000)


def test_negative_debt_free_cash_does_not_raise_not_ready():
    """제거된 가드가 되살아나면 여기서 걸린다."""
    conn = _Conn(
        [_state(AS_OF, LOAN_MODE, cash=Decimal(10_000), debt=Decimal(12_000))],
        issued_receivables=Decimal(700),
    )

    try:
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)
    except FinanceDataNotReady as exc:  # pragma: no cover - 회귀 시에만 도달한다
        raise AssertionError(f"음수 대출제외 현금이 막혔다: {exc.key}") from exc

    assert _row(conn)["base_cash_balance_krw"] < Decimal(0)


def test_a_negative_ledger_amount_still_blocks_on_the_existing_guard():
    """★ 원장 값 자체의 음수 방어는 **따로 있고, 이번 작업에서 손대지 않았다.**

    `current_cash_krw` 가 음수인 상태는 `_daily_closing_amount` 가 `daily_closing_ledger`
    로 막는다 — 이 계약은 이번 브랜치 이전부터 있었고(현재 `dev` 에도 있다) 여기서
    풀지 않는다. 대출제외 현금의 음수와는 **다른 칸의 이야기**다.
    """
    conn = _Conn(
        [_state(AS_OF, LOAN_MODE, cash=Decimal(-1_000), debt=Decimal(0))],
        issued_receivables=Decimal(700),
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert raised.value.key == "daily_closing_ledger"


def test_a_run_without_a_financing_mode_blocks():
    conn = _Conn(
        [_state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))],
        run_mode="   ",
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert raised.value.key == "sim_run_financing_mode"


# ---------------------------------------------------------------------------
# 새 실행 첫날 — **전날 행이 없다 ≠ 전날 값이 0이다**
#
# 🔴 부채를 물려받은 새 실행에서 직전을 0 으로 접으면 `max(D - 0, 0)` 이 되어
#    **있지도 않은 첫날 신규 차입**이 기록된다. 실측 baseline 부채가 45,272,104원이라
#    그 하루가 통째로 거짓 차입 사건이 된다.
#
# ★ 정본 포인터는 `finance_state_id` 하나다. `state_type` 은 그 행을 고른 이유이지
#   조회 키가 아니다 — 실측으로 `DAY30` 행은 둘(`FIN-DAY30-BASE`·`FIN-DAY30-LOAN`)이다.
# ---------------------------------------------------------------------------

BASELINE_STATE_ID = "FIN-DAY30-LOAN"
BASELINE_RUN_ID = "SIM-BURNIN-202512"


def _baseline_config(
    *,
    finance_state_id: str | None = BASELINE_STATE_ID,
    from_sim_run_id: str | None = BASELINE_RUN_ID,
):
    section: dict = {}
    if finance_state_id is not None:
        section["finance_state_id"] = finance_state_id
    if from_sim_run_id is not None:
        section["from_sim_run_id"] = from_sim_run_id
    return {"baseline": section}


def _baseline_row(
    *,
    debt: Decimal,
    receivables: Decimal = Decimal(10_000),
    cash: Decimal = Decimal(50_000),
    sim_run_id: str = BASELINE_RUN_ID,
    finance_state_id: str = BASELINE_STATE_ID,
):
    return {
        "finance_state_id": finance_state_id,
        "sim_run_id": sim_run_id,
        "financing_mode": LOAN_MODE,
        "current_cash_krw": cash,
        "receivables_krw": receivables,
        "current_debt_krw": debt,
    }


def _first_day_conn(
    *,
    current_debt: Decimal,
    baseline_debt: Decimal,
    in_run_outstanding: Decimal = Decimal(700),
    baseline_receivables: Decimal = Decimal(10_000),
    issued: Decimal = Decimal(0),
    config=None,
    baseline_states=None,
):
    """같은 실행에 이전 상태가 **하나도 없는** 첫날.

    ★ 상태의 채권은 **물려받은 출발분 + 이 실행에서 발행되어 남은 분**이다 (재무 확정
      기준 ④). 그래서 `receivables` 표에는 뒷항만 있고, 앞항은 baseline 행에만 있다.

    🔴 예전 이 대역은 `outstanding_receivables=current_receivables` 로, 표 하나가
      **잔액 전체**를 들고 있다고 세웠다. 시작을 물려받은 실행에서는 그런 표가 없다 —
      실측으로 `receivables` 는 0행이고 물려받은 잔액은 상태에만 있다.
    """
    return _Conn(
        [
            _state(
                AS_OF,
                LOAN_MODE,
                cash=Decimal(60_000),
                debt=current_debt,
                receivables=baseline_receivables + in_run_outstanding,
            )
        ],
        config_json=_baseline_config() if config is None else config,
        baseline_states=(
            [_baseline_row(debt=baseline_debt, receivables=baseline_receivables)]
            if baseline_states is None
            else baseline_states
        ),
        issued_receivables=issued,
        outstanding_receivables=in_run_outstanding,
    )


def _close_new_run(conn, *, as_of=AS_OF):
    return closing.FinanceDayClosing().close(conn, as_of=as_of, sim_run_id=SIM_RUN_ID)


# A ─ 물려받은 부채가 그대로면 첫날 신규 차입은 없다
def test_inherited_debt_unchanged_reports_no_first_day_borrowing():
    """🔴 **이 파일에서 가장 중요한 검사.**

    물려받은 부채 45,272,104 를 그대로 들고 시작한 첫날은 새로 빌린 돈이 0 이다.
    직전을 0 으로 접는 구현이면 여기서 45,272,104 가 나온다.
    """
    inherited = Decimal("45272104.184486")
    conn = _first_day_conn(
        current_debt=inherited, baseline_debt=inherited, issued=Decimal(700)
    )

    _close_new_run(conn)

    row = _row(conn)
    assert row["loan_execution_krw"] == Decimal(0)
    assert row["loan_execution_krw"] != inherited


# B ─ 물려받은 뒤 실제로 더 빌렸으면 증가분만
def test_borrowing_on_top_of_the_baseline_is_only_the_increase():
    conn = _first_day_conn(
        current_debt=Decimal(50_000), baseline_debt=Decimal(45_000), issued=Decimal(700)
    )

    _close_new_run(conn)

    assert _row(conn)["loan_execution_krw"] == Decimal(5_000)


# C ─ 물려받은 뒤 갚았으면 0 (음수 차입이 아니다)
def test_repaying_below_the_baseline_is_not_negative_borrowing():
    conn = _first_day_conn(
        current_debt=Decimal(45_000), baseline_debt=Decimal(50_000), issued=Decimal(700)
    )

    _close_new_run(conn)

    assert _row(conn)["loan_execution_krw"] == Decimal(0)


# D ─ 첫날 수금도 물려받은 채권을 직전으로 쓴다
def test_first_day_collection_uses_the_inherited_receivables():
    """`10,000 + 2,000 - 11,500 = 500`.

    직전을 0 으로 접으면 `0 + 2,000 - 11,500` 이 음수가 되어 마감이 통째로 막힌다 —
    수금이 있었던 날이 사라진다.

    ★ 수금은 **이 실행에서 발행한 2,000 중 500** 이다. 물려받은 10,000 은 개별 수금
      가능한 채권으로 풀리지 않으므로(재무 확정 기준 ⑥) 그대로 남고, 상태의 채권은
      `10,000 + 1,500 = 11,500` 이다.
    """
    conn = _first_day_conn(
        current_debt=Decimal(45_000),
        baseline_debt=Decimal(45_000),
        baseline_receivables=Decimal(10_000),
        in_run_outstanding=Decimal(1_500),
        issued=Decimal(2_000),
    )

    _close_new_run(conn)

    row = _row(conn)
    assert row["collection_cash_in_krw"] == Decimal(500)
    assert row["receivables_balance_krw"] == Decimal(11_500)


# E ─ 선언이 깨졌으면 0 으로 접지 않는다
@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"baseline": {}}, id="empty-section"),
        pytest.param({"baseline": {"finance_state_id": ""}}, id="blank-id"),
        pytest.param({"baseline": {"from_sim_run_id": BASELINE_RUN_ID}}, id="no-id"),
        pytest.param(
            {"baseline": {"finance_state_id": BASELINE_STATE_ID}}, id="no-lineage"
        ),
        pytest.param({"baseline": "FIN-DAY30-LOAN"}, id="not-a-mapping"),
    ],
)
def test_a_broken_baseline_declaration_blocks_instead_of_starting_from_zero(config):
    conn = _first_day_conn(
        current_debt=Decimal(45_000), baseline_debt=Decimal(45_000), config=config
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close_new_run(conn)

    assert raised.value.key == "baseline_finance_state_invalid"
    assert not conn.closings


def test_a_baseline_pointer_to_a_missing_row_blocks():
    conn = _first_day_conn(
        current_debt=Decimal(45_000), baseline_debt=Decimal(45_000), baseline_states=[]
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close_new_run(conn)

    assert raised.value.key == "baseline_finance_state"


# F ─ 계보가 어긋나면 막는다
def test_a_baseline_pointing_at_another_run_blocks():
    """포인터가 남의 실행을 가리키면 **에러 없이 숫자만 바뀐다** — 그래서 대조한다."""
    conn = _first_day_conn(
        current_debt=Decimal(45_000),
        baseline_debt=Decimal(45_000),
        baseline_states=[
            _baseline_row(debt=Decimal(45_000), sim_run_id="SIM-SOMEONE-ELSE")
        ],
    )

    with pytest.raises(FinanceDataNotReady) as raised:
        _close_new_run(conn)

    assert raised.value.key == "baseline_finance_state_invalid"


# G ─ 같은 실행의 어제가 baseline 보다 먼저다
def test_same_run_prior_takes_precedence_over_the_baseline():
    """★ 순서를 뒤집으면 **둘째 날부터 매일이 첫날처럼** 보인다."""
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(
                AS_OF,
                LOAN_MODE,
                cash=Decimal(60_000),
                debt=Decimal(45_000),
                receivables=Decimal(10_700),
            ),
            _state(
                day2,
                LOAN_MODE,
                cash=Decimal(70_000),
                debt=Decimal(46_000),
                receivables=Decimal(10_700),
            ),
        ],
        config_json=_baseline_config(),
        baseline_states=[_baseline_row(debt=Decimal(10))],
    )

    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    # 어제 부채 45,000 대비 1,000 만 신규 차입이다. baseline(10) 을 썼다면 45,990 이 된다.
    assert _row(conn, as_of=day2)["loan_execution_krw"] == Decimal(1_000)


def test_baseline_is_not_used_as_the_prior_when_the_same_run_has_a_prior_day():
    """★ baseline 행은 **Opening AR Carry 때문에 매일 읽는다.** 읽는 것과 직전으로
      쓰는 것은 다르다 — 여기서 잠그는 것은 *"직전으로 쓰지 않는다"* 쪽이다.

    🔴 예전 이 검사는 *"조회 자체가 나가지 않는다"* 를 잠갔다. 그 기계적 잠금은 이제
      너무 세다 — 출발 채권을 분리해 대조하려면(재무 확정 기준 ④) 둘째 날에도 그 행이
      필요하다. 그래서 **조회 유무가 아니라 숫자로** 잠근다.
    """
    day2 = date(2026, 1, 7)
    conn = _Conn(
        [
            _state(
                AS_OF,
                LOAN_MODE,
                cash=Decimal(60_000),
                debt=Decimal(45_000),
                receivables=Decimal(10_700),
            ),
            _state(
                day2,
                LOAN_MODE,
                cash=Decimal(70_000),
                debt=Decimal(46_000),
                receivables=Decimal(10_700),
            ),
        ],
        config_json=_baseline_config(),
        baseline_states=[_baseline_row(debt=Decimal(10), receivables=Decimal(10_000))],
    )

    closing.FinanceDayClosing().close(conn, as_of=day2, sim_run_id=SIM_RUN_ID)

    row = _row(conn, as_of=day2)
    # 직전이 baseline(부채 10) 이었다면 45,990 이 된다. 어제(45,000)를 썼으므로 1,000.
    assert row["loan_execution_krw"] == Decimal(1_000)
    # 직전이 baseline(채권 10,000) 이었다면 수금이 0 이 아니라 -700 이 되어 막혔다.
    assert row["collection_cash_in_krw"] == Decimal(0)


# ─ 선언이 아예 없는 실행은 기존 계약 그대로다
def test_a_run_without_a_baseline_declaration_keeps_the_existing_first_day_contract():
    """⚠️ 모든 실행이 baseline 을 가져야 한다고 일반화하지 않는다.

    시작을 물려받아야 하는 실행인지 아닌지를 가를 정본은 `config_json.baseline`
    선언뿐이고, `run_type` 으로 추측하는 규칙을 여기서 새로 만들지 않는다.
    """
    conn = _Conn(
        [_state(AS_OF, LOAN_MODE, cash=Decimal(12_000), debt=Decimal(3_000))],
        issued_receivables=Decimal(700),
    )

    closing.FinanceDayClosing().close(conn, as_of=AS_OF, sim_run_id=SIM_RUN_ID)

    assert _row(conn)["loan_execution_krw"] == Decimal(3_000)


def test_baseline_numbers_are_never_read_from_config_json():
    """★ 숫자의 Source of Truth 는 언제나 `finance_states` 원본 행이다.

    `config_json` 에 숫자가 섞여 있어도 마감은 그것을 쓰지 않는다 — 복제된 숫자는
    원본이 바뀌는 날 조용히 갈린다.
    """
    conn = _first_day_conn(
        current_debt=Decimal(45_000),
        baseline_debt=Decimal(45_000),
        issued=Decimal(700),
        config={
            "baseline": {
                "finance_state_id": BASELINE_STATE_ID,
                "from_sim_run_id": BASELINE_RUN_ID,
                # 있어도 쓰지 않는다.
                "current_debt_krw": 999_999_999,
                "receivables_krw": 888_888_888,
            }
        },
    )

    _close_new_run(conn)

    row = _row(conn)
    assert row["loan_execution_krw"] == Decimal(0)
    # 물려받은 10,000 + 이 실행에서 남은 700. config_json 의 888,888,888 이 아니다.
    assert row["receivables_balance_krw"] == Decimal(10_700)
