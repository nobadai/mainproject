from datetime import date
from decimal import Decimal

from app.finance import dashboard_service
from app.finance.schemas import (
    FinanceCashflowSummary,
    FinanceDashboardMeta,
    FinancePayableSummary,
    FinanceReceivableSummary,
)

AS_OF = date(2025, 12, 31)


def test_finance_dashboard_keeps_base_and_loan_states_separate(monkeypatch):
    _patch_common(monkeypatch)

    response = dashboard_service.get_finance_dashboard(
        sim_run_id="SIM-BURNIN-202512", as_of=AS_OF
    )

    assert [state.financing_mode for state in response.states] == [
        "BASE_NO_LOAN",
        "LOAN_BASELINE",
    ]
    base, loan = response.states
    assert base.current_cash_krw == Decimal(8680587)
    assert loan.current_cash_krw == Decimal(53952691)
    assert base.operating_cash_buffer_krw == Decimal(-1319413)
    assert loan.operating_cash_buffer_krw == Decimal(43952691)
    assert response.ledger_summary["receivables"].outstanding_amount_krw == Decimal(21922555)
    assert response.ledger_summary["payables"].outstanding_amount_krw == Decimal(0)
    assert response.expenses[0].fixed_amount_krw == Decimal(1000)
    assert [row.close_date for row in response.recent_closings] == [
        date(2025, 12, 31),
        date(2025, 12, 30),
    ]


def test_finance_cashflow_is_ascending_and_has_buffers(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_cashflow",
        lambda **_: [_closing(date(2025, 12, 30), 29), _closing(date(2025, 12, 31), 30)],
    )

    response = dashboard_service.get_finance_cashflow(
        sim_run_id="SIM-BURNIN-202512", as_of=AS_OF, days=30
    )

    assert [row.close_date for row in response.cashflow] == [
        date(2025, 12, 30),
        date(2025, 12, 31),
    ]
    assert response.cashflow[-1].base_cash_balance_krw == Decimal(8680587)
    assert response.cashflow[-1].loan_cash_balance_krw == Decimal(53952691)
    assert response.cashflow[-1].minimum_operating_cash_krw == Decimal(10000000)
    assert response.cashflow[-1].base_operating_buffer_krw == Decimal(-1319413)
    assert response.cashflow[-1].loan_operating_buffer_krw == Decimal(43952691)
    assert response.cashflow[-1].receivables_balance_krw == Decimal(21922555)


def _patch_common(monkeypatch):
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_finance_dashboard_meta",
        lambda **_: {"sim_run_id": "SIM-BURNIN-202512", "as_of": AS_OF, "data_type": "SIMULATION"},
    )
    monkeypatch.setattr(dashboard_service.repository, "load_finance_states", lambda **_: _states())
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_cashflow_summary",
        lambda **_: {
            "purchase_cash_out_krw": Decimal(1),
            "logistics_cash_out_krw": Decimal(2),
            "payroll_interest_cash_out_krw": Decimal(3),
            "sales_recognized_krw": Decimal(4),
            "collection_cash_in_krw": Decimal(5),
            "base_net_cash_krw": Decimal(6),
            "loan_execution_krw": Decimal(7),
        },
    )
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_receivable_summary",
        lambda **_: {
            "count": 15,
            "collected_count": 6,
            "partial_count": 2,
            "open_count": 7,
            "original_amount_krw": Decimal(43881332),
            "received_amount_krw": Decimal(21958777),
            "outstanding_amount_krw": Decimal(21922555),
            "overdue_amount_krw": Decimal(0),
        },
    )
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_payable_summary",
        lambda **_: {
            "count": 16,
            "original_amount_krw": Decimal(1000),
            "paid_amount_krw": Decimal(1000),
            "outstanding_amount_krw": Decimal(0),
            "overdue_amount_krw": Decimal(0),
        },
    )
    monkeypatch.setattr(dashboard_service.repository, "load_receivables", lambda **_: [])
    monkeypatch.setattr(dashboard_service.repository, "load_payables", lambda **_: [])
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_expense_summary",
        lambda **_: [
            {
                "expense_category": "PAYROLL",
                "status": "PAID",
                "expense_count": 1,
                "total_amount_krw": Decimal(1000),
                "fixed_amount_krw": Decimal(1000),
                "variable_amount_krw": Decimal(0),
            }
        ],
    )
    monkeypatch.setattr(
        dashboard_service.repository,
        "load_recent_closings",
        lambda **_: [_closing(date(2025, 12, 31), 30), _closing(date(2025, 12, 30), 29)],
    )


def _states() -> list[dict[str, object]]:
    return [
        _state("FIN-BASE", "BASE_NO_LOAN", Decimal(8680587)),
        _state("FIN-LOAN", "LOAN_BASELINE", Decimal(53952691)),
    ]


def _state(finance_state_id: str, financing_mode: str, cash: Decimal) -> dict[str, object]:
    return {
        "finance_state_id": finance_state_id,
        "state_date": AS_OF,
        "state_type": "DAY30",
        "financing_mode": financing_mode,
        "current_cash_krw": cash,
        "minimum_operating_cash_krw": Decimal(10000000),
        "committed_outflows_krw": Decimal(0),
        "unsettled_purchase_payables_krw": Decimal(0),
        "receivables_krw": Decimal(21922555),
        "inventory_book_value_krw": Decimal(0),
        "operational_inventory_value_krw": Decimal(0),
        "current_debt_krw": Decimal(0),
        "financial_limit_krw": cash - Decimal(10000000),
        "recommended_loan_amount_krw": Decimal(0),
        "note": None,
    }


def _closing(close_date: date, day_no: int) -> dict[str, object]:
    return {
        "sim_run_id": "SIM-BURNIN-202512",
        "close_date": close_date,
        "day_no": day_no,
        "purchase_cash_out_krw": Decimal(0),
        "logistics_cash_out_krw": Decimal(0),
        "payroll_interest_cash_out_krw": Decimal(0),
        "sales_recognized_krw": Decimal(0),
        "collection_cash_in_krw": Decimal(0),
        "base_net_cash_krw": Decimal(0),
        "base_cash_balance_krw": Decimal(8680587),
        "loan_execution_krw": Decimal(0),
        "loan_cash_balance_krw": Decimal(53952691),
        "receivables_balance_krw": Decimal(21922555),
        "inventory_qty_kg": Decimal(0),
        "accounting_inventory_cost_krw": Decimal(0),
        "closed": True,
    }
