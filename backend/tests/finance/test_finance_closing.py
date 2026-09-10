"""일 마감이 **실 원장을 그대로 닫을 수 있는가.**

🔴 이 파일의 가짜 연결은 한동안 실 원장보다 **너그러웠다.** 직전 상태를 물으면 늘
   한 건만 돌려줬는데, 실제 축(`LOAN_BASELINE`)에는 일별 상태가 252건 쌓여 있다.
   그래서 *"직전 상태가 둘 이상이면 모호하다"* 는 잘못된 판단이 여기서는 한 번도
   드러나지 않았고, 실 DB 에서는 셋째 날부터 모든 마감이 막혔다.

★ 그래서 가짜는 **실 원장의 모양을 흉내낸다** — 날짜별로 상태가 쌓여 있고, 질의는
  `ORDER BY state_date DESC LIMIT 2` 의 뜻대로 잘라 준다. 가짜가 실물보다 너그러우면
  통과한 검사가 아무것도 증명하지 못한다.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance import closing
from app.finance.db import FinanceDataNotReady, InventorySnapshot

SIM_RUN_ID = "SIM-WALK-202601"
AS_OF = date(2026, 1, 5)

#: 실 원장의 비용 분류. **이름을 지어내지 않는다** — 실측 `expenses.expense_category` 다.
LOGISTICS_CATEGORY = "LOGISTICS_SERVICE"
LOAN_INTEREST_CATEGORY = "LOAN_INTEREST"


def _state(state_date, mode, *, cash, receivables=Decimal(1_200), debt=Decimal(0)):
    return {
        "state_date": state_date,
        "financing_mode": mode,
        "current_cash_krw": cash,
        "receivables_krw": receivables,
        "current_debt_krw": debt,
    }


def _default_prior_states():
    """직전 상태가 **여러 날 쌓인** 정상 실행. 실 원장이 이 모양이다."""
    return [
        _state(date(2026, 1, 1), "BASE_NO_LOAN", cash=Decimal(6_000)),
        _state(date(2026, 1, 2), "BASE_NO_LOAN", cash=Decimal(7_000)),
        _state(date(2026, 1, 4), "BASE_NO_LOAN", cash=Decimal(8_000)),
        _state(date(2026, 1, 1), "LOAN_BASELINE", cash=Decimal(8_000), debt=Decimal(1_000)),
        _state(date(2026, 1, 2), "LOAN_BASELINE", cash=Decimal(9_000), debt=Decimal(1_500)),
        _state(date(2026, 1, 4), "LOAN_BASELINE", cash=Decimal(10_000), debt=Decimal(2_000)),
    ]


def _default_states():
    return [
        {
            "financing_mode": "BASE_NO_LOAN",
            "current_cash_krw": Decimal(9_000),
            "receivables_krw": Decimal(700),
            "current_debt_krw": Decimal(0),
        },
        {
            "financing_mode": "LOAN_BASELINE",
            "current_cash_krw": Decimal(12_000),
            "receivables_krw": Decimal(700),
            "current_debt_krw": Decimal(3_000),
        },
    ]


def _states_with_receivables(value):
    """마감 당일 두 축의 채권 잔액을 함께 세운다.

    ★ 상태와 원장이 어긋나면 마감은 `receivables_balance_mismatch` 로 막는다 — 그게
      계약이므로, 수금을 재는 검사는 **둘을 같이** 움직여야 실제 경로를 지난다.
    """
    return [dict(row, receivables_krw=value) for row in _default_states()]


def _default_expenses():
    return [
        ("PAYROLL", None, Decimal(100)),
        (LOGISTICS_CATEGORY, "DELIVERY-1", Decimal(30)),
    ]


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = str(query)
        self.conn.executed.append((text, params))
        self.rows = []
        self.row = None
        self.rowcount = 0
        if "sim_runs" in text:
            self.rows = [(self.conn.period_start, self.conn.period_end)]
        elif ".finance_states" in text and "state_date =" in text:
            self.rows = list(self.conn.states)
        elif ".finance_states" in text and "state_date <" in text:
            # ★ 실 질의의 뜻대로 자른다 — 같은 mode, as_of 이전, 최신순 두 건.
            mode, as_of = params[1], params[2]
            matching = [
                row
                for row in self.conn.prior_states
                if row["financing_mode"] == mode and row["state_date"] < as_of
            ]
            matching.sort(key=lambda row: row["state_date"], reverse=True)
            self.rows = matching[:2]
        elif "SUM(original_amount_krw)" in text:
            self.row = {"amount": self.conn.issued_receivables}
        elif "SUM(outstanding_amount_krw)" in text:
            self.row = {"amount": self.conn.outstanding_receivables}
        elif ".payables" in text:
            self.rows = self.conn.payables
        elif ".expenses" in text:
            self.rows = self.conn.expenses
        elif "SUM(total_amount_krw)" in text:
            self.row = {"amount": self.conn.sales_recognized}
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

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(
        self,
        payables,
        *,
        states=None,
        prior_states=None,
        expenses=None,
        issued_receivables=Decimal(500),
        outstanding_receivables=Decimal(700),
        sales_recognized=Decimal(1_000),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
    ):
        self.payables = payables
        self.states = _default_states() if states is None else states
        self.prior_states = _default_prior_states() if prior_states is None else prior_states
        self.expenses = _default_expenses() if expenses is None else expenses
        self.issued_receivables = issued_receivables
        self.outstanding_receivables = outstanding_receivables
        self.sales_recognized = sales_recognized
        self.period_start = period_start
        self.period_end = period_end
        self.closings = {}
        self.executed = []
        self.cursor_value = _Cursor(self)

    def cursor(self):
        return self.cursor_value


@pytest.fixture(autouse=True)
def _schema_and_inventory(monkeypatch):
    monkeypatch.setattr(closing, "get_db_schema", lambda: "test_schema")
    calls = []

    def inventory_snapshot(*args, **kwargs):
        calls.append((args, kwargs))
        return InventorySnapshot(
            quantity_kg=Decimal(42),
            inventory_book_value_krw=Decimal(840),
            operational_inventory_value_krw=Decimal(999),
        )

    monkeypatch.setattr(
        closing,
        "load_inventory_snapshot_as_of",
        inventory_snapshot,
    )
    return calls


def _close(conn, *, as_of=AS_OF, sim_run_id=SIM_RUN_ID):
    return closing.FinanceDayClosing().close(conn, as_of=as_of, sim_run_id=sim_run_id)


def _row(conn, *, as_of=AS_OF, sim_run_id=SIM_RUN_ID):
    return conn.closings[(sim_run_id, as_of)]


# ---------------------------------------------------------------------------
# 기존 계약
# ---------------------------------------------------------------------------


def test_close_day_writes_one_closed_2026_january_row_and_is_idempotent(_schema_and_inventory):
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    first = _close(conn)
    second = _close(conn)

    assert first.status == second.status == "CLOSED"
    assert (first.created, second.created) == (1, 0)
    assert list(conn.closings) == [(SIM_RUN_ID, AS_OF)]
    row = _row(conn)
    assert row["day_no"] == 5
    assert row["closed"] is True
    assert row["purchase_cash_out_krw"] == Decimal(200)
    assert row["logistics_cash_out_krw"] == Decimal(30)
    assert row["payroll_interest_cash_out_krw"] == Decimal(100)
    assert row["sales_recognized_krw"] == Decimal(1_000)
    assert row["collection_cash_in_krw"] == Decimal(1_000)
    assert row["base_net_cash_krw"] == Decimal(670)
    assert row["base_cash_balance_krw"] == Decimal(9_000)
    assert row["loan_execution_krw"] == Decimal(1_000)
    assert row["loan_cash_balance_krw"] == Decimal(12_000)
    assert row["receivables_balance_krw"] == Decimal(700)
    assert row["inventory_qty_kg"] == Decimal(42)
    assert row["accounting_inventory_cost_krw"] == Decimal(840)
    assert _schema_and_inventory[0][1] == {"sim_run_id": SIM_RUN_ID, "as_of": AS_OF}


def test_persisted_payable_due_date_is_the_purchase_cash_authority():
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    _close(conn)

    assert _row(conn)["purchase_cash_out_krw"] == Decimal(200)
    assert not any(".purchases" in text for text, _ in conn.executed)


def test_weekend_due_date_is_not_rewritten_and_moves_cash_out_to_monday():
    sunday = date(2026, 1, 4)
    conn = _Connection(payables=[(sunday, Decimal(200))])

    _close(conn)

    assert _row(conn)["purchase_cash_out_krw"] == Decimal(200)
    assert conn.payables[0][0] == sunday


def test_sim_run_id_is_bound_to_repository_query_not_interpreted_from_its_text():
    sim_run_id = "not-a-date-or-policy-axis"
    conn = _Connection(payables=[])

    _close(conn, sim_run_id=sim_run_id)

    sim_run_query = next(text for text, _ in conn.executed if "sim_runs" in text)
    assert "period_start" in sim_run_query
    assert (sim_run_id, AS_OF) in conn.closings


# ---------------------------------------------------------------------------
# 직전 상태 — **쌓여 있는 것이 정상이다**
# ---------------------------------------------------------------------------


def test_many_prior_days_are_history_not_ambiguity():
    """🔴 실 DB 가 막히던 자리.

    `LOAN_BASELINE` 축에는 일별 상태가 252건 있다. 예전 판단(`len(rows) > 1`)은
    *"이 축에 이전 상태가 둘 이상 있다"* 를 모호함으로 읽어, **일별 상태가 쌓인
    정상 실행의 셋째 날부터** 모든 마감을 세웠다.
    """
    conn = _Connection(payables=[])

    _close(conn)

    # 직전 BASE 는 1/4(8,000), 직전 LOAN 은 1/4(부채 2,000) 이다.
    assert _row(conn)["loan_execution_krw"] == Decimal(1_000)


def test_two_states_on_the_same_latest_date_are_still_ambiguous():
    """★ 진짜 모호함은 **가장 늦은 날짜가 둘일 때**다 — 그때는 고르지 않는다."""
    prior = _default_prior_states()
    prior.append(_state(date(2026, 1, 4), "BASE_NO_LOAN", cash=Decimal(8_888)))
    conn = _Connection(payables=[], prior_states=prior)

    with pytest.raises(FinanceDataNotReady):
        _close(conn)


def test_prior_state_never_reads_a_future_day():
    """마감은 **그날까지의 사실**로만 선다."""
    prior = _default_prior_states()
    prior.append(_state(date(2026, 1, 20), "BASE_NO_LOAN", cash=Decimal(99_999)))
    conn = _Connection(payables=[], prior_states=prior)

    _close(conn)

    assert _row(conn)["loan_execution_krw"] == Decimal(1_000)


def test_first_day_without_any_prior_state_still_closes():
    conn = _Connection(
        payables=[],
        prior_states=[],
        issued_receivables=Decimal(700),
        outstanding_receivables=Decimal(700),
    )

    _close(conn)

    # 직전 부채가 없으면 현재 부채 전액이 이번 실행분이다.
    assert _row(conn)["loan_execution_krw"] == Decimal(3_000)


# ---------------------------------------------------------------------------
# BASE / LOAN 축 분리
# ---------------------------------------------------------------------------


def test_base_and_loan_axes_do_not_bleed_into_each_other():
    conn = _Connection(payables=[])

    _close(conn)

    row = _row(conn)
    assert row["base_cash_balance_krw"] == Decimal(9_000)
    assert row["loan_cash_balance_krw"] == Decimal(12_000)


def test_missing_base_state_blocks_the_close():
    """BASE 축이 없으면 **닫지 않는다** — 대출 잔액을 무차입 칸에 넣지 않는다."""
    conn = _Connection(
        payables=[],
        states=[
            row for row in _default_states() if row["financing_mode"] != "BASE_NO_LOAN"
        ],
    )

    with pytest.raises(FinanceDataNotReady):
        _close(conn)


def test_duplicate_state_for_one_mode_on_the_close_date_blocks():
    conn = _Connection(payables=[], states=[*_default_states(), _default_states()[0]])

    with pytest.raises(FinanceDataNotReady):
        _close(conn)


def test_without_loan_axis_the_base_balance_carries_the_loan_column():
    """대출 축이 없는 실행은 차입이 없다 — 실행액 0, 잔액은 무차입 잔액이다."""
    conn = _Connection(
        payables=[],
        states=[row for row in _default_states() if row["financing_mode"] != "LOAN_BASELINE"],
    )

    _close(conn)

    row = _row(conn)
    assert row["loan_execution_krw"] == Decimal(0)
    assert row["loan_cash_balance_krw"] == row["base_cash_balance_krw"] == Decimal(9_000)


# ---------------------------------------------------------------------------
# 비용 분류 — 원장이 쓰는 이름을 그대로 안다
# ---------------------------------------------------------------------------


def test_loan_interest_is_a_payroll_interest_cash_out():
    """🔴 실 DB 가 막히던 두 번째 자리.

    원장이 쓰는 이름은 `INTEREST` 가 아니라 `LOAN_INTEREST` 다. 예전 목록에 그 이름이
    없어서 **이자를 지급한 날은 마감이 통째로 막혔다** (실측 2025-12-31).
    """
    conn = _Connection(
        payables=[],
        expenses=[
            ("PAYROLL", None, Decimal(100)),
            (LOAN_INTEREST_CATEGORY, None, Decimal(7)),
            (LOGISTICS_CATEGORY, "DELIVERY-1", Decimal(30)),
        ],
    )

    _close(conn)

    row = _row(conn)
    assert row["payroll_interest_cash_out_krw"] == Decimal(107)
    assert row["logistics_cash_out_krw"] == Decimal(30)


def test_delivery_linked_expense_is_logistics_cash_out():
    conn = _Connection(
        payables=[], expenses=[(LOGISTICS_CATEGORY, "DELIVERY-9", Decimal(55))]
    )

    _close(conn)

    row = _row(conn)
    assert row["logistics_cash_out_krw"] == Decimal(55)
    assert row["payroll_interest_cash_out_krw"] == Decimal(0)


def test_unknown_expense_category_blocks_instead_of_guessing():
    """★ 모르는 분류를 어느 칸에도 넣지 않는다 — 틀린 값을 확정하느니 막는다."""
    conn = _Connection(payables=[], expenses=[("MARKETING", None, Decimal(10))])

    with pytest.raises(FinanceDataNotReady):
        _close(conn)


def test_only_paid_expenses_are_cash_out():
    """비용은 **실제로 지급된 것**만 센다 — 원장에 `PAID` 상태가 실재한다."""
    conn = _Connection(payables=[])

    _close(conn)

    expense_query = next(text for text, _ in conn.executed if ".expenses" in text)
    assert "status = 'PAID'" in expense_query
    assert "expense_date = %s" in expense_query


# ---------------------------------------------------------------------------
# 매입 현금 — 계약 만기와 현금 효과일
# ---------------------------------------------------------------------------


def test_d0_payable_is_cash_out_on_the_same_day():
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    _close(conn)

    assert _row(conn)["purchase_cash_out_krw"] == Decimal(200)


def test_payable_due_on_another_business_day_is_not_counted_today():
    """각 채무는 **자기 현금효과일 하루에만** 실린다 — 두 번 세지 않는다."""
    conn = _Connection(payables=[(date(2026, 1, 2), Decimal(200))])

    _close(conn)

    assert _row(conn)["purchase_cash_out_krw"] == Decimal(0)


def test_saturday_due_date_is_not_cash_out_on_saturday():
    saturday = date(2026, 1, 3)
    conn = _Connection(payables=[(saturday, Decimal(200))])

    _close(conn, as_of=saturday)

    assert _row(conn, as_of=saturday)["purchase_cash_out_krw"] == Decimal(0)


def test_purchase_cash_out_reads_only_unsettled_payables():
    """현재 계약은 *"아직 안 나간 돈"* 을 상태로 믿는다 (`OPEN`·`PARTIAL`)."""
    conn = _Connection(payables=[])

    _close(conn)

    payable_query = next(text for text, _ in conn.executed if ".payables" in text)
    assert "IN ('OPEN', 'PARTIAL')" in payable_query
    assert "issued_date <= %s" in payable_query


# ---------------------------------------------------------------------------
# 채권 · 수금
# ---------------------------------------------------------------------------


def test_issue_only_day_collects_nothing():
    conn = _Connection(
        payables=[],
        issued_receivables=Decimal(500),
        outstanding_receivables=Decimal(1_700),
        states=_states_with_receivables(Decimal(1_700)),
    )

    _close(conn)

    row = _row(conn)
    assert row["collection_cash_in_krw"] == Decimal(0)
    assert row["receivables_balance_krw"] == Decimal(1_700)


def test_collection_only_day_has_no_issue():
    conn = _Connection(
        payables=[],
        issued_receivables=Decimal(0),
        outstanding_receivables=Decimal(900),
        states=_states_with_receivables(Decimal(900)),
    )

    _close(conn)

    assert _row(conn)["collection_cash_in_krw"] == Decimal(300)


def test_partial_collection_is_the_difference_not_the_whole_receivable():
    conn = _Connection(
        payables=[],
        issued_receivables=Decimal(0),
        outstanding_receivables=Decimal(1_050),
        states=_states_with_receivables(Decimal(1_050)),
    )

    _close(conn)

    assert _row(conn)["collection_cash_in_krw"] == Decimal(150)


def test_issue_and_collection_on_the_same_day_net_out():
    conn = _Connection(
        payables=[],
        issued_receivables=Decimal(400),
        outstanding_receivables=Decimal(1_400),
        states=_states_with_receivables(Decimal(1_400)),
    )

    _close(conn)

    assert _row(conn)["collection_cash_in_krw"] == Decimal(200)


def test_receivable_balance_that_disagrees_with_state_blocks_the_close():
    """🔴 두 원장이 다른 말을 하면 **고르지 않는다.**"""
    conn = _Connection(payables=[], outstanding_receivables=Decimal(701))

    with pytest.raises(FinanceDataNotReady):
        _close(conn)


def test_negative_collection_blocks_instead_of_being_written():
    """수금이 음수라는 것은 원장이 어긋났다는 뜻이지 **환불이 아니다.**"""
    conn = _Connection(
        payables=[],
        issued_receivables=Decimal(0),
        outstanding_receivables=Decimal(1_500),
        states=_states_with_receivables(Decimal(1_500)),
    )

    with pytest.raises(FinanceDataNotReady):
        _close(conn)


def test_receivables_are_never_read_from_the_future():
    conn = _Connection(payables=[])

    _close(conn)

    outstanding_query = next(
        text for text, _ in conn.executed if "SUM(outstanding_amount_krw)" in text
    )
    assert "issued_date <= %s" in outstanding_query


# ---------------------------------------------------------------------------
# 매출 인식
# ---------------------------------------------------------------------------


def test_sales_recognition_is_confirmed_or_delivered_on_the_close_date():
    conn = _Connection(payables=[])

    _close(conn)

    sales_query = next(text for text, _ in conn.executed if "SUM(total_amount_krw)" in text)
    assert "sale_date = %s" in sales_query
    assert "'CONFIRMED', 'DELIVERED'" in sales_query
    assert "CANCELLED" not in sales_query


def test_sales_recognition_is_not_collection():
    """발생 매출과 현금 수금은 **다른 칸**이다 — 하나로 접으면 되돌릴 수 없다."""
    conn = _Connection(
        payables=[],
        sales_recognized=Decimal(1_000),
        issued_receivables=Decimal(0),
        outstanding_receivables=Decimal(900),
        states=_states_with_receivables(Decimal(900)),
    )

    _close(conn)

    row = _row(conn)
    assert row["sales_recognized_krw"] == Decimal(1_000)
    assert row["collection_cash_in_krw"] == Decimal(300)


# ---------------------------------------------------------------------------
# 재고 계보
# ---------------------------------------------------------------------------


def test_accounting_inventory_cost_uses_book_value_not_operational_value():
    """★ 회계 재고원가는 `inventory_book_value_krw` 다 — 운영 평가액이 아니다."""
    conn = _Connection(payables=[])

    _close(conn)

    row = _row(conn)
    assert row["accounting_inventory_cost_krw"] == Decimal(840)
    assert row["accounting_inventory_cost_krw"] != Decimal(999)


def test_inventory_snapshot_is_taken_on_the_close_date(_schema_and_inventory):
    conn = _Connection(payables=[])

    _close(conn)

    assert _schema_and_inventory[0][1] == {"sim_run_id": SIM_RUN_ID, "as_of": AS_OF}


# ---------------------------------------------------------------------------
# 실행 축 · 기간 경계
# ---------------------------------------------------------------------------


def test_close_date_outside_the_run_period_blocks():
    conn = _Connection(payables=[])

    with pytest.raises(FinanceDataNotReady):
        _close(conn, as_of=date(2026, 2, 15))


def test_day_no_counts_from_the_run_period_start():
    conn = _Connection(payables=[], period_start=date(2026, 1, 3))

    _close(conn)

    assert _row(conn)["day_no"] == 3


def test_blank_sim_run_id_is_refused():
    conn = _Connection(payables=[])

    with pytest.raises(ValueError):
        _close(conn, sim_run_id="   ")


def test_every_ledger_query_is_scoped_to_the_run():
    """★ `sim_run_id` 가 **모든 조회를 관통한다** — 하나라도 빠지면 남의 실행이 섞인다."""
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    _close(conn)

    for text, params in conn.executed:
        if "daily_closings" in text:
            assert params["sim_run_id"] == SIM_RUN_ID
            continue
        assert "sim_run_id = %s" in text, text
        assert SIM_RUN_ID in list(params or []), text


def test_reclosing_the_same_day_overwrites_with_the_same_facts():
    conn = _Connection(payables=[(AS_OF, Decimal(200))])

    _close(conn)
    before = dict(_row(conn))
    _close(conn)
    after = dict(_row(conn))

    assert before == after
