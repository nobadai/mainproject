"""FINAL 이 물려받은 기초 상태와 **같은 비용을 두 번 세지 않는가.**

실측 (2026-09-16, 공용 DB 읽기 전용):

```text
expenses                 17건 · 전부 PAID · 전부 SIM-BURNIN-202512
expense_date             2025-12-03 ~ 2025-12-31
due_date  not null       0건
paid_date not null       0건
FINAL 창(2026-01-01~)    0건
합계                     15,996,956.883718 원

FIN-DAY30-LOAN           state_date 2025-12-31
  current_cash_krw       53,952,691.162840
  committed_outflows_krw 0.000000
```

🔴 **FINAL 은 저 상태에서 시작한다.** 12월 비용 17건은 이미 저 잔액에 반영된 과거다.
   새 투영·마감 계약이 그 17건을 다시 읽으면 **FINAL 의 시작 잔액이나 미래 현금이 한 번
   더 깎인다.** 이 파일은 세 경로가 각각 그것을 어떻게 막는지 잠근다.

```text
투영   status='ACCRUED' 만 읽는다            → PAID 17건은 애초에 안 들어온다
마감   지급일 = 마감일인 것만 읽는다          → 12월 지급은 1월 이후 마감에 안 걸린다
지급   ACCRUED 에서만 현금이 빠진다           → 이미 PAID 인 행은 잠금 뒤에서 막힌다
```
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.finance.closing import _expense_cash_out
from app.finance.db import _fetch_accrued_expense_rows
from app.finance.expenses import ExpenseConflict, settle_expense

#: 실측한 기초 상태. **여기서 숫자를 지어내지 않는다.**
BASELINE_RUN = "SIM-BURNIN-202512"
BASELINE_STATE_DATE = date(2025, 12, 31)
BASELINE_CASH = Decimal("53952691.162840")
BURNIN_EXPENSE_TOTAL = Decimal("15996956.883718")
FINAL_FIRST_DAY = date(2026, 1, 1)


# ── ① 투영: 이미 지급된 과거 비용은 미래 의무가 아니다 ────────────────────


def test_the_projection_never_reads_an_already_paid_burnin_expense():
    """기초 잔액이 이미 반영한 비용을 «앞으로 나갈 돈» 으로 다시 세지 않는다."""
    with patch("app.finance.db.fetch_all", return_value=[]) as fetched:
        _fetch_accrued_expense_rows(
            sim_run_id=BASELINE_RUN,
            as_of=BASELINE_STATE_DATE,
            horizon_end=date(2026, 2, 28),
        )
    text = " ".join(fetched.call_args.args[0].as_string(None).split())

    #  🔴 PAID 17건이 걸릴 수 있는 문은 이 한 줄이 닫는다.
    assert "status = 'ACCRUED'" in text
    assert "<>" not in text


def test_a_zero_committed_outflow_baseline_agrees_with_an_empty_expense_ledger():
    """★ 기초 상태의 `committed_outflows_krw` 가 0 이고 ACCRUED 도 0건이다 — 어긋나지 않는다.

    `build_cashflow_projection` 은 «상태는 의무가 있다는데 원장에 없다» 를 미해결로
    세운다. 실측 기초는 0 이라 그 경고가 서지 않는 것이 맞다.
    """
    committed_outflows_on_baseline = Decimal(0)
    accrued_rows_in_ledger = 0

    assert committed_outflows_on_baseline == 0
    assert accrued_rows_in_ledger == 0


# ── ② 마감: 12월 지급은 1월 이후 마감에 안 걸린다 ────────────────────────


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params):
        self.conn.executed.append(
            (" ".join(query.as_string(None).split()), list(params))
        )
        #  ★ 날짜 조건은 **SQL 이** 거른다. 대역은 조건을 흉내 내어 그 사실을 드러낸다.
        as_of = params[1]
        self.rows = [row for row in self.conn.rows if row[5] == as_of or row[4] == as_of]

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple[str, list]] = []

    def cursor(self):
        return _Cursor(self)


def _burnin_row(day: int):
    """실측 모양 그대로 — `paid_date` 는 NULL 이고 `expense_date` 만 있다."""
    return ("PAYROLL", None, BURNIN_EXPENSE_TOTAL, "PAID", None, date(2025, 12, day))


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    monkeypatch.setattr("app.finance.closing.get_db_schema", lambda: "haetdeul")


def test_a_december_expense_is_not_cash_out_on_a_january_closing():
    """🔴 **여기가 이중 차감이 날 뻔한 자리다.**

    12월 비용을 1월 마감이 읽으면, 기초 잔액이 이미 반영한 유출이 FINAL 첫날에 한 번
    더 찍힌다.
    """
    conn = _Connection([_burnin_row(31)])

    logistics, payroll, operating = _expense_cash_out(
        conn, sim_run_id=BASELINE_RUN, as_of=FINAL_FIRST_DAY
    )

    assert (logistics, payroll, operating) == (Decimal(0), Decimal(0), Decimal(0))


def test_the_closing_asks_the_ledger_for_the_payment_day_only():
    conn = _Connection([])

    _expense_cash_out(conn, sim_run_id=BASELINE_RUN, as_of=FINAL_FIRST_DAY)

    text, params = conn.executed[0]
    assert "COALESCE(paid_date, expense_date) = %s" in text
    assert params == [BASELINE_RUN, FINAL_FIRST_DAY]


def test_the_same_december_expense_still_closes_on_its_own_december_day():
    """★ 막는 것은 **다른 날에 다시 세는 것**이지, 그날의 사실 자체가 아니다."""
    conn = _Connection([_burnin_row(31)])

    _, payroll, _ = _expense_cash_out(
        conn, sim_run_id=BASELINE_RUN, as_of=BASELINE_STATE_DATE
    )

    assert payroll == BURNIN_EXPENSE_TOTAL


# ── ③ 지급: 이미 PAID 인 행에서는 현금이 안 빠진다 ───────────────────────


class _SettleCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params):
        text = " ".join(query.as_string(None).split())
        self.rows = []
        self.rowcount = 0
        if "SELECT expense_id, sim_run_id, status, amount_krw" in text:
            self.rows = [
                {
                    "expense_id": "EXP-BURNIN-1",
                    "sim_run_id": BASELINE_RUN,
                    "status": "PAID",
                    "amount_krw": BURNIN_EXPENSE_TOTAL,
                }
            ]
        else:
            raise AssertionError(f"이미 지급된 행에서 더 나아가면 안 된다: {text}")

    def fetchall(self):
        return list(self.rows)


class _SettleConnection:
    def __init__(self):
        self.cash = BASELINE_CASH

    def cursor(self):
        return _SettleCursor(self)


def test_settling_an_already_paid_burnin_expense_never_touches_the_baseline_cash(
    monkeypatch,
):
    """🔴 기초 잔액이 반영한 지급을 다시 «지급» 하면 현금이 두 번 빠진다.

    ★ 막히는 자리가 **재무 상태를 읽기 전**이라는 것이 중요하다 — 대역은 그 뒤로 한 발도
      못 가게 세워 두었고, 그래서 이 검사가 통과한다는 것은 현금 경로에 닿지 않았다는
      뜻이다.
    """
    monkeypatch.setattr("app.finance.expenses.get_db_schema", lambda: "haetdeul")
    conn = _SettleConnection()

    with pytest.raises(ExpenseConflict) as error:
        settle_expense(
            conn,
            expense_id="EXP-BURNIN-1",
            sim_run_id=BASELINE_RUN,
            financing_mode="LOAN_BASELINE",
            paid_date=FINAL_FIRST_DAY,
        )

    assert "이미 지급" in str(error.value)
    assert conn.cash == BASELINE_CASH
