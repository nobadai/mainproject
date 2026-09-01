from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.finance.repository import get_active_finance_debt_policy, get_active_finance_policy

#: `patch()` 대상 모듈 경로 — 소유 모듈을 직접 가리킨다.
_STATE_REPO = "app.finance.infrastructure.finance_state_repository"


def _rows() -> list[dict[str, object]]:
    values = {
        "purchase_payment_days": ("NUMERIC", Decimal(7)),
        "payroll_date": ("NUMERIC", Decimal(25)),
        "monthly_labor_cost_krw": ("NUMERIC", Decimal(12941280)),
        "minimum_cash_balance_krw": ("NUMERIC", Decimal(12941280)),
        "cashflow_projection_days": ("NUMERIC", Decimal(30)),
        "cash_priority_reference": ("TEXT", "minimum_cash_balance_krw"),
        "cash_priority_high_ratio": ("NUMERIC", Decimal("1.0")),
        "cash_priority_medium_ratio": ("NUMERIC", Decimal("1.5")),
    }
    rows = []
    for key, (kind, value) in values.items():
        rows.append(
            {
                "policy_key": key,
                "value_kind": kind,
                "value_numeric": value if kind == "NUMERIC" else None,
                "value_text": value if kind == "TEXT" else None,
                "value_json": None,
                "source_ref": f"policy:{key}",
                "policy_version": "v1.3-PROVISIONAL",
                "usage_scope": "AGENT_MVP_DEMO",
            }
        )
    return rows


def _debt_rows() -> list[dict[str, object]]:
    values = {
        "debt_runtime_status": ("TEXT", "SIM_FIXED_EXECUTED"),
        "debt_principal_krw": ("NUMERIC", Decimal("45272104.184486")),
        "debt_execution_date": ("TEXT", "2025-12-02"),
        "debt_annual_rate": ("NUMERIC", Decimal("0.025")),
        "debt_term_months": ("NUMERIC", Decimal(72)),
        "debt_grace_months": ("NUMERIC", Decimal(36)),
        "debt_grace_payment_mode": ("TEXT", "INTEREST_ONLY"),
        "debt_repayment_method": ("TEXT", "EQUAL_PRINCIPAL_AFTER_GRACE"),
        "debt_payment_frequency": ("TEXT", "MONTHLY"),
        "debt_payment_day_rule": ("TEXT", "MONTH_END"),
        "debt_first_payment_rule": ("TEXT", "EXECUTION_MONTH_END"),
        "debt_interest_method": (
            "TEXT",
            "OUTSTANDING_PRINCIPAL_ANNUAL_RATE_DIV_12",
        ),
    }
    return [
        {
            "policy_key": key,
            "value_kind": kind,
            "value_numeric": value if kind == "NUMERIC" else None,
            "value_text": value if kind == "TEXT" else None,
            "value_json": None,
            "evidence_grade": "SIM_FIXED",
            "source_ref": "MVP-DECISION-20260825:N9-DEMO",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
        for key, (kind, value) in values.items()
    ]


def _load_debt(rows):
    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_all", return_value=rows),
    ):
        return get_active_finance_debt_policy()


def _load(rows: list[dict[str, object]]):
    with (
        patch(f"{_STATE_REPO}.get_db_schema", return_value="configured_schema"),
        patch(f"{_STATE_REPO}.fetch_all", return_value=rows) as fetch,
    ):
        policy = get_active_finance_policy()
    assert fetch.call_args.args[1] == ["finance", "v1.3-PROVISIONAL", "AGENT_MVP_DEMO"]
    return policy


def test_get_active_finance_policy_parses_typed_values_and_metadata():
    policy = _load(_rows())

    assert policy.purchase_payment_days == 7
    assert policy.payroll_date == 25
    assert policy.source_refs["payroll_date"] == "policy:payroll_date"
    assert policy.margin_defense_floor_rate is None
    assert policy.monthly_labor_cost_krw == Decimal(12941280)
    assert policy.minimum_cash_balance_krw == Decimal(12941280)
    assert policy.cashflow_projection_days == 30
    assert policy.cash_priority_reference == "minimum_cash_balance_krw"
    assert policy.cash_priority_high_ratio == Decimal("1.0")
    assert policy.cash_priority_medium_ratio == Decimal("1.5")
    assert policy.policy_version == "v1.3-PROVISIONAL"
    assert policy.usage_scope == "AGENT_MVP_DEMO"
    assert policy.source_refs["purchase_payment_days"] == "policy:purchase_payment_days"


def test_zero_numeric_policy_is_not_treated_as_missing():
    rows = _rows()
    rows[0]["value_numeric"] = Decimal(0)
    assert _load(rows).purchase_payment_days == 0


@pytest.mark.parametrize("field", ["policy_version", "usage_scope"])
def test_policy_metadata_mismatch_fails_closed(field):
    rows = _rows()
    rows[0][field] = "wrong"
    with pytest.raises(ValueError, match="mismatch"):
        _load(rows)


def test_missing_n5_is_preserved_as_null_for_runtime_readiness():
    policy = _load(_rows()[1:])
    assert policy.purchase_payment_days is None
    assert "purchase_payment_days" not in policy.source_refs


def test_explicit_null_n5_is_preserved():
    rows = _rows()
    rows[0]["value_numeric"] = None
    assert _load(rows).purchase_payment_days is None


def test_missing_payroll_amount_is_preserved_as_null():
    rows = [row for row in _rows() if row["policy_key"] != "monthly_labor_cost_krw"]
    assert _load(rows).monthly_labor_cost_krw is None


def test_optional_margin_defense_floor_rate_is_source_owned():
    rows = _rows()
    rows.append(
        {
            "policy_key": "margin_defense_floor_rate",
            "value_kind": "NUMERIC",
            "value_numeric": Decimal("0.12"),
            "value_text": None,
            "value_json": None,
            "source_ref": "policy:margin_defense_floor_rate",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
    )
    policy = _load(rows)
    assert policy.margin_defense_floor_rate == Decimal("0.12")
    assert policy.source_refs["margin_defense_floor_rate"] == (
        "policy:margin_defense_floor_rate"
    )


@pytest.mark.parametrize(
    ("key", "mutation", "error"),
    [
        ("purchase_payment_days", {"value_kind": "TEXT"}, ValueError),
        ("purchase_payment_days", {"value_numeric": "7"}, TypeError),
        ("cash_priority_reference", {"value_text": 7}, TypeError),
        ("cash_priority_reference", {"value_json": {}}, ValueError),
    ],
)
def test_invalid_value_kind_columns_or_python_value_fails_closed(key, mutation, error):
    rows = _rows()
    row = next(item for item in rows if item["policy_key"] == key)
    row.update(mutation)
    with pytest.raises(error):
        _load(rows)


def test_unsupported_cash_priority_reference_fails_closed():
    rows = _rows()
    row = next(item for item in rows if item["policy_key"] == "cash_priority_reference")
    row["value_text"] = "minimum_operating_cash_krw"
    with pytest.raises(ValidationError):
        _load(rows)


def test_debt_policy_parses_all_twelve_fields_and_keeps_base_policy_compatible():
    debt = _load_debt(_debt_rows())
    assert debt.debt_principal_krw == Decimal("45272104.184486")
    assert debt.debt_execution_date == date(2025, 12, 2)
    assert debt.debt_term_months == 72
    assert debt.debt_grace_months == 36
    assert len(debt.source_refs) == 12
    assert _load(_rows()).purchase_payment_days == 7


def test_missing_or_inactive_debt_key_fails_closed():
    with pytest.raises(LookupError, match="debt_runtime_status"):
        _load_debt(_debt_rows()[1:])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("debt_runtime_status", "REAL_EXECUTED"),
        ("debt_grace_payment_mode", "NONE"),
        ("debt_repayment_method", "ANNUITY"),
        ("debt_payment_frequency", "WEEKLY"),
        ("debt_payment_day_rule", "DAY_25"),
        ("debt_first_payment_rule", "NEXT_MONTH"),
        ("debt_interest_method", "DAILY_ACCRUAL"),
    ],
)
def test_invalid_debt_text_enum_fails_closed(key, value):
    rows = _debt_rows()
    next(row for row in rows if row["policy_key"] == key)["value_text"] = value
    with pytest.raises(ValidationError):
        _load_debt(rows)


def test_invalid_debt_execution_date_fails_closed():
    rows = _debt_rows()
    next(row for row in rows if row["policy_key"] == "debt_execution_date")["value_text"] = (
        "not-a-date"
    )
    with pytest.raises(ValidationError):
        _load_debt(rows)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("debt_principal_krw", Decimal(0)),
        ("debt_principal_krw", Decimal(-1)),
        ("debt_term_months", Decimal(0)),
        ("debt_term_months", Decimal(-1)),
    ],
)
def test_invalid_debt_numeric_contract_fails_closed(key, value):
    rows = _debt_rows()
    next(row for row in rows if row["policy_key"] == key)["value_numeric"] = value
    with pytest.raises(ValidationError):
        _load_debt(rows)


def test_debt_grace_cannot_equal_or_exceed_term():
    rows = _debt_rows()
    next(row for row in rows if row["policy_key"] == "debt_grace_months")["value_numeric"] = (
        Decimal(73)
    )
    with pytest.raises(ValidationError, match="less than"):
        _load_debt(rows)
