"""운영비 의무가 **미래 현금유출 투영에 어떻게 실리는가.**

```text
ACCRUED    실린다   지급 예정일(due_date)에
PAID       안 실린다  이미 나갔다
CANCELLED  안 실린다  나가지 않기로 했다
```

🔴 **예전 조회는 `status <> 'PAID'` 였다.** 그러면 **취소한 비용이 의무로 들어온다** —
   나가지 않기로 한 돈을 나갈 돈으로 세면 화면의 현금 여력이 실제보다 적어지고, 그
   숫자로 판매가 막힌다. `PURCHASE_PAYABLE` 과 섞이지도 않아야 한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.finance.db import _fetch_accrued_expense_rows, _rows_to_events

_DB = "app.finance.db"
AS_OF = date(2026, 9, 16)
HORIZON = date(2026, 10, 16)


def _query():
    with patch(f"{_DB}.fetch_all", return_value=[]) as fetched:
        rows = _fetch_accrued_expense_rows(
            sim_run_id="SIM-1", as_of=AS_OF, horizon_end=HORIZON
        )
    assert rows == []
    query, params = fetched.call_args.args
    return " ".join(query.as_string(None).split()), params


def test_only_accrued_expenses_are_future_obligations():
    """🔴 `status <> 'PAID'` 는 취소까지 데려온다 — 상태를 직접 적는다."""
    text, _ = _query()

    assert "status = 'ACCRUED'" in text
    assert "<>" not in text


def test_the_projection_date_is_the_day_it_is_due():
    """지급 예정일이 기준이다. 발생일로 세면 돈이 실제보다 일찍 나가는 것으로 읽힌다."""
    text, params = _query()

    assert "COALESCE(due_date, expense_date) > %s" in text
    assert "COALESCE(due_date, expense_date) <= %s" in text
    assert params == ["SIM-1", AS_OF, HORIZON]


def test_a_legacy_accrued_row_without_a_due_date_is_not_dropped():
    """★ 이 칸이 생기기 전 `ACCRUED` 행의 의무가 조용히 사라지면 안 된다.

    🔴 읽기 전용 호환이다 — 원장에 `due_date` 를 채워 넣지 않는다.
    """
    text, _ = _query()

    assert "COALESCE(due_date, expense_date) AS effective_due_date" in text


def test_the_expense_query_is_scoped_to_one_run():
    text, params = _query()

    assert "sim_run_id = %s" in text
    assert params[0] == "SIM-1"


def test_rows_become_committed_outflow_events_on_their_due_date():
    """★ 이름이 `COMMITTED_OUTFLOW` 다 — `PURCHASE_PAYABLE` 과 합치지 않는다."""
    events = _rows_to_events(
        [
            {
                "expense_id": "EXP-1",
                "effective_due_date": date(2026, 9, 20),
                "amount_krw": Decimal(40_000),
            }
        ],
        id_column="expense_id",
        date_column="effective_due_date",
        amount_column="amount_krw",
        event_type="COMMITTED_OUTFLOW",
        direction="OUTFLOW",
    )

    assert len(events) == 1
    assert events[0].event_date == date(2026, 9, 20)
    assert events[0].event_type == "COMMITTED_OUTFLOW"
    assert events[0].direction == "OUTFLOW"
    assert events[0].amount_krw == Decimal(40_000)


def test_an_expense_arising_today_but_due_later_lands_on_the_later_day():
    """발생 9/16 · 지급 예정 9/20 이면 현금 Event 는 **9/20** 이다."""
    events = _rows_to_events(
        [
            {
                "expense_id": "EXP-RENT",
                "effective_due_date": date(2026, 9, 20),
                "amount_krw": Decimal(1),
            }
        ],
        id_column="expense_id",
        date_column="effective_due_date",
        amount_column="amount_krw",
        event_type="COMMITTED_OUTFLOW",
        direction="OUTFLOW",
    )

    assert events[0].event_date != date(2026, 9, 16)
    assert events[0].event_date == date(2026, 9, 20)


# ── 판매가 보는 사실까지 ──────────────────────────────────────────────────


def test_an_accrued_expense_reaches_sales_as_a_committed_outflow_not_a_payable():
    """§38 — 운영비 의무와 매입 미지급금을 **합치지 않는다.**

    둘 다 유출이지만 매입 대금과 운영비는 다른 사실이다. 판매가 합친 값 하나만 받으면
    어느 쪽이 큰지 되짚을 수 없고, 되짚을 수 없으면 무엇을 줄여야 할지도 모른다.
    """
    from app.finance.capabilities.pre_sales import build_obligation_facts

    expense_events = _rows_to_events(
        [
            {
                "expense_id": "EXP-RENT",
                "effective_due_date": date(2026, 9, 20),
                "amount_krw": Decimal(40_000),
            }
        ],
        id_column="expense_id",
        date_column="effective_due_date",
        amount_column="amount_krw",
        event_type="COMMITTED_OUTFLOW",
        direction="OUTFLOW",
    )

    facts = build_obligation_facts(
        as_of=AS_OF, obligations=expense_events, receivables=[]
    )

    #  🔴 운영비는 미지급금 합계에 한 원도 들어가지 않는다.
    assert facts['payables_total_krw'] == Decimal(0)
    assert facts['payables_due_7d_krw'] == Decimal(0)
    assert facts['payables_due_30d_krw'] == Decimal(0)
