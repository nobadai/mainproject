"""지급일이 된 운영비를 **한 번에** 지급한다 — 무엇까지 나가고 무엇이 남는가.

`settle_due_expenses()` 가 하는 일은 고르는 것뿐이다. 실제 지급은 기존
`settle_expense()` 가 하고, 이 파일은 **고르는 규칙**을 잠근다.

```text
지급일 전     안 나간다
지급일 당일   나간다
지급일 지남   그날 나간다 — paid_date 는 실제로 나간 날
두 번 호출    두 번째는 없다
다른 실행     건드리지 않는다
```

🔴 **가짜 연결을 쓴다.** 실 DB 를 건드리면 이 검사가 남의 실행 현금을 줄인다.

★ 기존 `test_finance_expense_lifecycle.py` 의 가짜는 비용 **한 건**을 다룬다. 여기서는
  여러 건 · 여러 실행 · 정렬을 봐야 하므로 같은 규율(잠금 흉내 · 상태 조건)을 지키면서
  여러 행을 드는 가짜를 따로 세운다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance.db import FinanceDataNotReady
from app.finance.expenses import (
    ExpenseConflict,
    settle_due_expenses,
)

RUN = "SIM-DUE-A"
OTHER_RUN = "SIM-DUE-B"
MODE = "BASE_NO_LOAN"
AS_OF = date(2026, 3, 10)


class _Row(dict):
    """dict 접근만 쓰는 행. 모듈의 기존 관례와 같다."""


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
        #  ★ 스키마 식별자는 `"haetdeul"` 로 인용되어 나온다. 인용부호를 벗겨서
        #    질의 갈래를 이름으로 고른다 — 인용 방식이 바뀌어도 검사가 안 흔들린다.
        text = " ".join(
            (query.as_string(None) if hasattr(query, "as_string") else str(query))
            .replace('"', "")
            .split()
        )
        self.conn.executed.append((text, list(params)))
        self.rows = []
        self.rowcount = 0

        if "FROM haetdeul.sim_runs" in text:
            run = self.conn.runs.get(str(params[0]))
            self.rows = [_Row(financing_mode=run)] if run is not None else []
            return

        if "SELECT expense_id FROM haetdeul.expenses" in text:
            #  🔴 실제 질의의 조건을 **그대로** 흉내 낸다. 여기서 느슨하게 고르면
            #     production 이 무엇을 거르는지 검사가 확인하지 못한다.
            sim_run_id, as_of = str(params[0]), params[1]
            matched = [
                row
                for row in self.conn.expenses
                if row["sim_run_id"] == sim_run_id
                and row["status"] == "ACCRUED"
                and row["due_date"] <= as_of
            ]
            matched.sort(key=lambda row: (row["due_date"], row["expense_id"]))
            self.rows = [_Row(expense_id=row["expense_id"]) for row in matched]
            return

        if "SELECT expense_id, sim_run_id, status" in text:
            found = self.conn.find(str(params[0]))
            self.rows = [_Row(**found)] if found else []
            return

        if "SELECT finance_state_id, current_cash_krw" in text:
            sim_run_id, mode, state_date = str(params[0]), str(params[1]), params[2]
            self.rows = [
                _Row(**row)
                for row in self.conn.states
                if row["sim_run_id"] == sim_run_id
                and row["financing_mode"] == mode
                and row["state_date"] == state_date
            ]
            return

        if "UPDATE haetdeul.expenses" in text and "'PAID'" in text:
            found = self.conn.find(str(params[1]))
            if found and found["status"] == "ACCRUED":
                found["status"] = "PAID"
                found["paid_date"] = params[0]
                self.rowcount = 1
            return

        if "UPDATE haetdeul.finance_states" in text:
            for row in self.conn.states:
                if row["finance_state_id"] == params[1]:
                    row["current_cash_krw"] = params[0]
                    self.rowcount = 1
            return

        raise AssertionError(text)

    def fetchall(self):
        return list(self.rows)


class _Connection:
    """비용 여러 건 · 실행 여러 개를 드는 가짜 원장."""

    def __init__(
        self,
        *,
        expenses: list[dict[str, object]] | None = None,
        runs: dict[str, object] | None = None,
        cash: Decimal = Decimal(10_000_000),
        state_dates: tuple[date, ...] = (AS_OF,),
    ) -> None:
        self.expenses = expenses if expenses is not None else []
        self.runs: dict[str, object] = runs if runs is not None else {RUN: MODE, OTHER_RUN: MODE}
        self.states = [
            {
                "finance_state_id": f"FIN-{run}-{day}",
                "sim_run_id": run,
                "financing_mode": MODE,
                "state_date": day,
                "current_cash_krw": cash,
            }
            for run in (RUN, OTHER_RUN)
            for day in state_dates
        ]
        self.executed: list[tuple[str, list[object]]] = []
        self.commits = 0

    def find(self, expense_id: str) -> dict[str, object] | None:
        for row in self.expenses:
            if row["expense_id"] == expense_id:
                return row
        return None

    def cash(self, run: str = RUN, day: date = AS_OF) -> Decimal:
        return next(
            Decimal(str(row["current_cash_krw"]))
            for row in self.states
            if row["sim_run_id"] == run and row["state_date"] == day
        )

    def cursor(self):
        return _Cursor(self)

    def commit(self):  # pragma: no cover - 불리면 검사가 실패한다
        self.commits += 1


def _expense(
    expense_id: str,
    *,
    due: date,
    amount: int = 3_855_000,
    status: str = "ACCRUED",
    run: str = RUN,
) -> dict[str, object]:
    return {
        "expense_id": expense_id,
        "sim_run_id": run,
        "status": status,
        "amount_krw": Decimal(amount),
        "due_date": due,
        "paid_date": None,
    }


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    monkeypatch.setattr("app.finance.expenses.get_db_schema", lambda: "haetdeul")


# ── B. 지급일 전 ──────────────────────────────────────────────────────────


def test_an_expense_not_yet_due_is_left_alone():
    """지급일이 오지 않은 비용은 **손대지 않는다.** 미리 내는 것은 지급이 아니다."""
    conn = _Connection(expenses=[_expense("EXP-1", due=date(2026, 3, 11))])
    before = conn.cash()

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert settled == ()
    assert conn.expenses[0]["status"] == "ACCRUED"
    assert conn.expenses[0]["paid_date"] is None
    assert conn.cash() == before
    #  ★ 지급 질의 자체가 없었다 — 고르지 않은 것을 «지급하려다 막혔다» 로 읽지 않는다.
    assert not any("'PAID'" in text for text, _ in conn.executed)


# ── C. 지급일 당일 ────────────────────────────────────────────────────────


def test_an_expense_due_today_is_paid_today():
    """지급일 당일이면 나간다. 현금은 정확히 그 금액만 줄어든다."""
    conn = _Connection(expenses=[_expense("EXP-1", due=AS_OF, amount=3_855_000)])
    before = conn.cash()

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert len(settled) == 1
    assert settled[0].expense_id == "EXP-1"
    assert settled[0].paid_date == AS_OF
    assert settled[0].amount_krw == Decimal(3_855_000)
    assert conn.expenses[0]["status"] == "PAID"
    assert conn.expenses[0]["paid_date"] == AS_OF
    assert conn.cash() == before - Decimal(3_855_000)
    assert settled[0].current_cash_krw == conn.cash()


# ── D. 지급일이 지난 것 ───────────────────────────────────────────────────


def test_an_overdue_expense_is_paid_on_the_day_it_actually_leaves():
    """🔴 **늦게 나간 돈을 제 날짜에 나간 것처럼 적지 않는다.**

    `paid_date` 는 지급일(`due_date`)이 아니라 **실제로 나간 걷기 날짜**다. 과거 날짜를
    그대로 적으면 이미 마감된 날의 현금흐름이 뒤에서 바뀐다.
    """
    overdue = date(2026, 2, 10)
    conn = _Connection(expenses=[_expense("EXP-1", due=overdue)])
    before = conn.cash()

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert len(settled) == 1
    assert settled[0].paid_date == AS_OF
    assert conn.expenses[0]["paid_date"] == AS_OF
    assert conn.expenses[0]["due_date"] == overdue  # 지급일은 사실이라 바뀌지 않는다
    #  현금은 딱 한 번 준다.
    assert conn.cash() == before - Decimal(3_855_000)


def test_several_due_expenses_are_paid_in_the_contracted_order():
    """정렬은 계약이다 — `ORDER BY due_date, expense_id`."""
    conn = _Connection(
        expenses=[
            _expense("EXP-C", due=date(2026, 2, 10)),
            _expense("EXP-A", due=date(2026, 2, 10)),
            _expense("EXP-B", due=date(2026, 1, 10)),
        ],
        #  세 건이 다 나가야 순서를 볼 수 있다 — 현금이 모자라 중간에서 막히면
        #  이 검사가 «정렬» 이 아니라 «현금 부족» 을 보게 된다.
        cash=Decimal(20_000_000),
    )

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert [item.expense_id for item in settled] == ["EXP-B", "EXP-A", "EXP-C"]
    assert all(item.paid_date == AS_OF for item in settled)


# ── E. 두 번 호출 ─────────────────────────────────────────────────────────


def test_calling_twice_on_the_same_day_pays_once():
    """같은 날 두 번 불려도 현금은 한 번만 준다."""
    conn = _Connection(expenses=[_expense("EXP-1", due=AS_OF)])
    before = conn.cash()

    first = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)
    after_first = conn.cash()
    second = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert len(first) == 1
    assert second == ()
    assert after_first == before - Decimal(3_855_000)
    assert conn.cash() == after_first


# ── F. 다른 실행 격리 ─────────────────────────────────────────────────────


def test_another_runs_expense_is_never_touched():
    """🔴 **남의 실행 비용을 이 실행의 현금에서 빼지 않는다.**"""
    conn = _Connection(
        expenses=[
            _expense("EXP-A-1", due=AS_OF, run=RUN),
            _expense("EXP-B-1", due=AS_OF, run=OTHER_RUN),
        ]
    )
    before_other = conn.cash(OTHER_RUN)

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert [item.expense_id for item in settled] == ["EXP-A-1"]
    other = conn.find("EXP-B-1")
    assert other is not None
    assert other["status"] == "ACCRUED"
    assert other["paid_date"] is None
    assert conn.cash(OTHER_RUN) == before_other


def test_a_run_with_nothing_due_is_not_an_error():
    """지급할 것이 없으면 **빈 튜플**이다. 예외가 아니다."""
    conn = _Connection(expenses=[_expense("EXP-B-1", due=AS_OF, run=OTHER_RUN)])

    assert settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF) == ()


# ── 축을 지어내지 않는다 ──────────────────────────────────────────────────


def test_an_unknown_run_is_closed_not_guessed():
    """🔴 없는 실행에 «지급할 것이 없었다» 로 답하지 않는다."""
    conn = _Connection(expenses=[], runs={})

    with pytest.raises(FinanceDataNotReady) as raised:
        settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert raised.value.key == "sim_run"


def test_a_blank_financing_mode_is_closed_not_defaulted():
    """조달 축이 비어 있으면 기본값을 고르지 않는다 — 다른 장부의 현금이 준다."""
    conn = _Connection(expenses=[_expense("EXP-1", due=AS_OF)], runs={RUN: "   "})

    with pytest.raises(FinanceDataNotReady) as raised:
        settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert raised.value.key == "sim_run_financing_mode"
    #  막혔으므로 비용은 그대로다.
    assert conn.expenses[0]["status"] == "ACCRUED"


def test_the_financing_mode_comes_from_the_run_not_the_caller():
    """축은 이 함수가 `sim_runs` 에서 읽는다. 부르는 쪽이 넘기지 않는다."""
    conn = _Connection(expenses=[_expense("EXP-1", due=AS_OF)], runs={RUN: MODE})

    settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    state_query = next(
        params
        for text, params in conn.executed
        if "SELECT finance_state_id, current_cash_krw" in text
    )
    #  재무 상태를 고른 축이 `sim_runs` 에서 읽은 값이다.
    assert state_query[1] == MODE


# ── 한 거래다 ─────────────────────────────────────────────────────────────


def test_a_failure_partway_through_is_raised_not_swallowed():
    """🔴 **앞선 지급이 성공했어도 실패를 삼키지 않는다.**

    지급이 실패했는데 그날이 정상 마감으로 서면 «현금은 줄었는데 비용은 0원» 인 기록이
    남는다. 그래서 여기서 올리고, 부르는 쪽이 그날을 막는다.
    """
    conn = _Connection(
        expenses=[
            _expense("EXP-1", due=date(2026, 1, 10), amount=3_855_000),
            #  두 번째는 현금보다 크다 — `settle_expense` 가 막는다.
            _expense("EXP-2", due=date(2026, 2, 10), amount=99_000_000),
        ],
        cash=Decimal(10_000_000),
    )

    with pytest.raises(ExpenseConflict):
        settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    #  앞선 지급은 이미 일어났다 — 되돌리는 것은 부르는 쪽의 거래다.
    assert conn.find("EXP-1")["status"] == "PAID"
    assert conn.find("EXP-2")["status"] == "ACCRUED"


def test_the_helper_never_commits():
    """🔴 **중간 커밋이 없다.** 절반만 나간 상태로 굳으면 현금과 원장이 갈린다.

    ★ 커넥션은 부르는 쪽이 소유한다. 여기서 커밋하면 위 실패 상황에서 앞선 지급을
      되돌릴 수 없다.
    """
    conn = _Connection(
        expenses=[
            _expense("EXP-1", due=date(2026, 1, 10)),
            _expense("EXP-2", due=date(2026, 2, 10)),
        ]
    )

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert len(settled) == 2
    assert conn.commits == 0


def test_a_failure_also_leaves_the_commit_untouched():
    conn = _Connection(
        expenses=[_expense("EXP-1", due=AS_OF, amount=99_000_000)],
        cash=Decimal(10_000_000),
    )

    with pytest.raises(ExpenseConflict):
        settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert conn.commits == 0


def test_paid_and_cancelled_expenses_are_not_candidates():
    """이미 끝난 비용은 후보가 아니다."""
    conn = _Connection(
        expenses=[
            _expense("EXP-PAID", due=date(2026, 1, 10), status="PAID"),
            _expense("EXP-CANCELLED", due=date(2026, 1, 10), status="CANCELLED"),
            _expense("EXP-ACCRUED", due=date(2026, 1, 10)),
        ]
    )

    settled = settle_due_expenses(conn, sim_run_id=RUN, as_of=AS_OF)

    assert [item.expense_id for item in settled] == ["EXP-ACCRUED"]
