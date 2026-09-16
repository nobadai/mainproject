"""일반 운영비 생명주기 — **발생 · 지급 · 취소가 각자 다른 사실인가.**

이 파일이 지키는 것은 넷이다.

```text
적는다     ACCRUED 로만 적히고 현금은 안 변한다
지급한다   현금이 정확히 한 번, 정확한 축의 상태에서 준다
취소한다   현금은 안 변하고, 이미 나간 돈은 취소 못 한다
투영한다   ACCRUED 만, 지급 예정일에
```

🔴 **가짜 연결을 쓴다.** 실 DB 를 건드리면 이 검사가 남의 실행 현금을 줄인다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance.db import FinanceDataNotReady
from app.finance.expenses import (
    KNOWN_EXPENSE_CATEGORIES,
    OPERATING_EXPENSE_CATEGORIES,
    PAYROLL_INTEREST_CATEGORIES,
    ExpenseConflict,
    cancel_expense,
    create_expense,
    effective_paid_date,
    settle_expense,
)

SIM_RUN = "SIM-EXP-1"
MODE = "LOAN_BASELINE"
AROSE = date(2026, 9, 16)
DUE = date(2026, 9, 20)


class _Cursor:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn
        self.rows: list[dict[str, object]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params):
        text = " ".join(
            (query.as_string(None) if hasattr(query, "as_string") else str(query)).split()
        )
        self.conn.executed.append((text, list(params)))
        self.rows = []
        self.rowcount = 0
        if "INSERT INTO" in text and "expenses" in text:
            self.conn.inserted.append(list(params))
            self.rowcount = 1
        elif "SELECT expense_id, sim_run_id, status" in text:
            #  ★ 지급(금액까지)과 취소(상태까지)가 같은 행을 잠그고 읽는다.
            self.rows = [dict(self.conn.expense)] if self.conn.expense else []
        elif "SELECT finance_state_id, current_cash_krw" in text:
            self.rows = [dict(row) for row in self.conn.states]
        elif "UPDATE" in text and "expenses" in text and "'PAID'" in text:
            #  ★ 원장의 잠금을 흉내 낸다 — `status = 'ACCRUED'` 조건이 실제로 걸려야
            #    두 번째 지급이 여기서 0행으로 떨어진다.
            if self.conn.expense and self.conn.expense["status"] == "ACCRUED":
                self.conn.expense["status"] = "PAID"
                self.conn.expense["paid_date"] = params[0]
                self.rowcount = 1
        elif "UPDATE" in text and "expenses" in text and "'CANCELLED'" in text:
            if self.conn.expense and self.conn.expense["status"] == "ACCRUED":
                self.conn.expense["status"] = "CANCELLED"
                self.rowcount = 1
        elif "UPDATE" in text and "finance_states" in text:
            self.conn.states[0]["current_cash_krw"] = params[0]
            self.rowcount = 1
        else:
            raise AssertionError(text)

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(
        self,
        *,
        status: str = "ACCRUED",
        amount: Decimal = Decimal(40_000),
        cash: Decimal = Decimal(1_000_000),
        states: int = 1,
        sim_run_id: str = SIM_RUN,
        expense: bool = True,
    ) -> None:
        self.expense: dict[str, object] | None = (
            {
                "expense_id": "EXP-1",
                "sim_run_id": sim_run_id,
                "status": status,
                "amount_krw": amount,
                "paid_date": None,
            }
            if expense
            else None
        )
        self.states = [
            {"finance_state_id": f"FIN-{index}", "current_cash_krw": cash}
            for index in range(states)
        ]
        self.executed: list[tuple[str, list[object]]] = []
        self.inserted: list[list[object]] = []

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    monkeypatch.setattr("app.finance.expenses.get_db_schema", lambda: "haetdeul")


def _create(conn, **overrides):
    payload: dict[str, object] = {
        "sim_run_id": SIM_RUN,
        "expense_date": AROSE,
        "due_date": DUE,
        "expense_category": "RENT",
        "amount_krw": Decimal(40_000),
        "evidence_id": "EVID-RENT-202609",
    }
    payload.update(overrides)
    return create_expense(conn, **payload)  # type: ignore[arg-type]


# ── 적는다 ────────────────────────────────────────────────────────────────


def test_a_new_expense_is_accrued_and_does_not_move_cash():
    """🔴 발생 시점에 현금을 빼지 않는다 — 그러면 지급 때 같은 돈이 두 번 빠진다."""
    conn = _Connection()

    expense_id = _create(conn)

    assert expense_id.startswith("EXP-")
    assert len(conn.inserted) == 1
    insert_text = next(text for text, _ in conn.executed if "INSERT INTO" in text)
    assert "'ACCRUED'" in insert_text
    assert not any("finance_states" in text for text, _ in conn.executed)


def test_a_new_expense_records_both_dates_and_leaves_the_payment_day_empty():
    """발생일과 지급 예정일은 다른 칸이다. 실제 지급일은 아직 없다."""
    conn = _Connection()

    _create(conn)

    params = conn.inserted[0]
    assert params[2] == AROSE
    assert params[3] == DUE
    insert_text = next(text for text, _ in conn.executed if "INSERT INTO" in text)
    assert "paid_date" in insert_text and "NULL" in insert_text


def test_the_evidence_id_is_the_expense_ledgers_own_contract():
    """★ 근거의 정본은 `evidence_id` 다 — 다른 원장의 `source_ref` 를 베끼지 않는다."""
    conn = _Connection()

    _create(conn)

    insert_text = next(text for text, _ in conn.executed if "INSERT INTO" in text)
    assert "evidence_id" in insert_text
    assert "source_ref" not in insert_text
    assert "recorded_by" not in insert_text


def test_an_expense_without_evidence_is_refused():
    with pytest.raises(ExpenseConflict):
        _create(_Connection(), evidence_id="  ")


def test_an_unknown_category_is_refused_instead_of_becoming_other():
    """🔴 모르는 분류를 «기타» 로 바꾸면 마감이 조용히 다른 칸에 센다."""
    with pytest.raises(ExpenseConflict) as error:
        _create(_Connection(), expense_category="MARKETING")
    assert "MARKETING" in str(error.value)


def test_a_zero_amount_is_not_an_expense():
    """⚠️ 0원은 «비용이 없다» 다. 0원짜리 비용이 있다는 뜻이 아니다."""
    with pytest.raises(ExpenseConflict):
        _create(_Connection(), amount_krw=Decimal(0))


def test_a_float_amount_is_refused():
    """금액은 Decimal 이다 — 부동소수로 받으면 원 단위가 조용히 어긋난다."""
    with pytest.raises(ExpenseConflict):
        _create(_Connection(), amount_krw=40_000.5)


def test_a_due_date_before_the_expense_date_is_refused():
    with pytest.raises(ExpenseConflict):
        _create(_Connection(), due_date=AROSE.replace(day=15))


def test_an_expense_without_a_run_axis_is_refused():
    with pytest.raises(ExpenseConflict):
        _create(_Connection(), sim_run_id=" ")


# ── 지급한다 ──────────────────────────────────────────────────────────────


def _settle(conn, **overrides):
    payload: dict[str, object] = {
        "expense_id": "EXP-1",
        "sim_run_id": SIM_RUN,
        "financing_mode": MODE,
        "paid_date": DUE,
    }
    payload.update(overrides)
    return settle_expense(conn, **payload)  # type: ignore[arg-type]


def test_settling_an_accrued_expense_reduces_cash_by_exactly_the_amount():
    conn = _Connection(amount=Decimal(40_000), cash=Decimal(1_000_000))

    result = _settle(conn)

    assert result.amount_krw == Decimal(40_000)
    assert result.current_cash_krw == Decimal(960_000)
    assert conn.states[0]["current_cash_krw"] == Decimal(960_000)
    assert conn.expense is not None and conn.expense["status"] == "PAID"
    assert conn.expense["paid_date"] == DUE


def test_settling_picks_the_state_by_run_mode_and_payment_day():
    """🔴 «최신 상태» 로 대신 고르지 않는다 — 남의 실행 현금이 줄어든다."""
    conn = _Connection()

    _settle(conn)

    text, params = next(
        item for item in conn.executed if "SELECT finance_state_id" in item[0]
    )
    assert "sim_run_id = %s AND financing_mode = %s AND state_date = %s" in text
    assert params == [SIM_RUN, MODE, DUE]


def test_both_the_expense_and_the_state_are_locked_before_they_are_read():
    """상태를 읽고 나서 잠그면 두 요청이 같은 `ACCRUED` 를 함께 보고 둘 다 통과한다."""
    conn = _Connection()

    _settle(conn)

    locking = [text for text, _ in conn.executed if "FOR UPDATE" in text]
    assert len(locking) == 2
    assert "expenses" in locking[0]
    assert "finance_states" in locking[1]


def test_settling_the_same_expense_twice_only_moves_cash_once():
    """🔴 같은 요청이 재시도돼도 현금은 한 번만 빠진다."""
    conn = _Connection(cash=Decimal(1_000_000))

    _settle(conn)
    with pytest.raises(ExpenseConflict) as error:
        _settle(conn)

    assert "이미 지급" in str(error.value)
    assert conn.states[0]["current_cash_krw"] == Decimal(960_000)


def test_a_cancelled_expense_cannot_be_settled():
    conn = _Connection(status="CANCELLED")

    with pytest.raises(ExpenseConflict):
        _settle(conn)

    assert conn.states[0]["current_cash_krw"] == Decimal(1_000_000)


def test_another_runs_expense_is_never_paid_from_this_runs_cash():
    conn = _Connection(sim_run_id="SIM-OTHER")

    with pytest.raises(ExpenseConflict):
        _settle(conn)

    assert conn.states[0]["current_cash_krw"] == Decimal(1_000_000)


def test_a_missing_expense_is_a_lookup_error():
    with pytest.raises(LookupError):
        _settle(_Connection(expense=False))


def test_no_state_on_the_payment_day_blocks_instead_of_guessing():
    conn = _Connection(states=0)

    with pytest.raises(FinanceDataNotReady):
        _settle(conn)


def test_two_candidate_states_block_instead_of_picking_one():
    conn = _Connection(states=2)

    with pytest.raises(FinanceDataNotReady):
        _settle(conn)


def test_cash_cannot_go_below_zero():
    conn = _Connection(amount=Decimal(40_000), cash=Decimal(1_000))

    with pytest.raises(ExpenseConflict):
        _settle(conn)

    assert conn.states[0]["current_cash_krw"] == Decimal(1_000)


# ── 취소한다 ──────────────────────────────────────────────────────────────


def test_cancelling_an_accrued_expense_leaves_cash_untouched():
    conn = _Connection()

    cancel_expense(conn, expense_id="EXP-1", sim_run_id=SIM_RUN)

    assert conn.expense is not None and conn.expense["status"] == "CANCELLED"
    assert conn.states[0]["current_cash_krw"] == Decimal(1_000_000)
    assert not any("finance_states" in text for text, _ in conn.executed)


def test_a_paid_expense_cannot_be_cancelled():
    """🔴 이미 나간 돈을 취소하면 과거의 현금유출이 장부에서 사라진다."""
    conn = _Connection(status="PAID")

    with pytest.raises(ExpenseConflict) as error:
        cancel_expense(conn, expense_id="EXP-1", sim_run_id=SIM_RUN)

    assert "환입" in str(error.value)


def test_another_runs_expense_is_not_cancelled_here():
    conn = _Connection(sim_run_id="SIM-OTHER")

    with pytest.raises(ExpenseConflict):
        cancel_expense(conn, expense_id="EXP-1", sim_run_id=SIM_RUN)


# ── 지급일을 읽는 규칙 ────────────────────────────────────────────────────


def test_a_paid_expense_reads_its_own_payment_day():
    assert effective_paid_date(status="PAID", paid_date=DUE, expense_date=AROSE) == DUE


def test_a_legacy_paid_expense_falls_back_to_the_day_it_arose():
    """★ 읽기 전용 호환이다 — 원장에 그 날짜를 적어 넣지 않는다."""
    assert effective_paid_date(status="PAID", paid_date=None, expense_date=AROSE) == AROSE


def test_an_unpaid_expense_has_no_payment_day_at_all():
    """⚠️ `ACCRUED` 에 발생일을 지급일로 돌려주면 안 나간 돈이 나간 것으로 읽힌다."""
    assert effective_paid_date(status="ACCRUED", paid_date=None, expense_date=AROSE) is None
    assert (
        effective_paid_date(status="CANCELLED", paid_date=None, expense_date=AROSE) is None
    )


# ── 분류 계약 ─────────────────────────────────────────────────────────────


def test_the_two_category_sets_never_overlap():
    """한 분류가 두 칸에 가면 같은 돈이 두 번 적힌다."""
    assert not (OPERATING_EXPENSE_CATEGORIES & PAYROLL_INTEREST_CATEGORIES)


def test_the_writable_categories_are_exactly_the_two_buckets():
    assert KNOWN_EXPENSE_CATEGORIES == (
        OPERATING_EXPENSE_CATEGORIES | PAYROLL_INTEREST_CATEGORIES
    )


def test_labor_and_logistics_names_stay_out_of_the_operating_bucket():
    """🔴 어느 칸에 가야 하는지 정한 문서가 없다 — 모르는 것을 밀어 넣지 않는다."""
    for name in ("LABOR", "LOGISTICS", "TRANSPORT", "LOGISTICS_SERVICE"):
        assert name not in OPERATING_EXPENSE_CATEGORIES
        assert name not in KNOWN_EXPENSE_CATEGORIES
