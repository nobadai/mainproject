from datetime import date
from decimal import Decimal

import pytest

from app.finance.schemas import FinanceSnapshot


@pytest.fixture
def finance_state() -> dict[str, object]:
    return {
        "finance_state_id": "FIN-DAY30-LOAN",
        "sim_run_id": "SIM-BURNIN-202512",
        "state_date": date(2025, 12, 31),
        "state_type": "DAY30",
        "financing_mode": "LOAN_BASELINE",
        "current_cash_krw": Decimal("31993913.770000"),
        "minimum_operating_cash_krw": Decimal("15902640.000000"),
        "committed_outflows_krw": Decimal("0.000000"),
        "unsettled_purchase_payables_krw": Decimal("0.000000"),
        "financial_limit_krw": Decimal("16091273.770000"),
    }


@pytest.fixture
def finance_snapshot(finance_state) -> FinanceSnapshot:
    return FinanceSnapshot(snapshot_id=None, **finance_state)


@pytest.fixture
def purchase_payload() -> dict[str, object]:
    return {
        "meta": {
            "as_of": "2025-12-31",
            "item": "배추",
            "agent_version": "v0.4",
            "is_refeed": False,
            "feedback_attempt": 0,
        },
        "scenarios": [
            {
                "label": "기본",
                "strategy_type": "quantity",
                "coverage_days": 5,
                "total_quantity_ton": 4.5,
                "total_amount_krw": 10318995,
                "split_plan": [{"seq": 1, "date": "2025-12-31", "quantity_ton": 4.5}],
                "sourcing_plan": [
                    {
                        "market": "가락",
                        "grade": "상",
                        "quantity_ton": 3.0,
                        "grade_unit_price": 1650,
                    },
                    {
                        "market": "가락",
                        "grade": "중",
                        "quantity_ton": 1.5,
                        "grade_unit_price": 1450,
                    },
                ],
            }
        ],
    }


@pytest.fixture
def sales_payload() -> dict[str, object]:
    return {
        "cycle": "SALES",
        "as_of": "2025-12-31",
        "approved_purchase": {
            "approval_id": "H1-20260821-001",
            "total_amount_krw": 10318995,
            "payment_date": "2026-08-28",
        },
        "channel_terms": [
            {
                "channel_type": "DIRECT_B2B",
                "partner_id": "KIMCHI_FACTORY_001",
                "settlement_days": 30,
            }
        ],
    }
