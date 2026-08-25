from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.finance.repository import get_active_finance_policy


def _rows() -> list[dict[str, object]]:
    values = {
        "purchase_payment_days": ("NUMERIC", Decimal("7")),
        "payroll_date": ("NUMERIC", Decimal("25")),
        "monthly_labor_cost_krw": ("NUMERIC", Decimal("12941280")),
        "minimum_cash_balance_krw": ("NUMERIC", Decimal("12941280")),
        "cashflow_projection_days": ("NUMERIC", Decimal("30")),
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


def _load(rows: list[dict[str, object]]):
    with (
        patch("app.finance.repository.get_db_schema", return_value="configured_schema"),
        patch("app.finance.repository.fetch_all", return_value=rows) as fetch,
    ):
        policy = get_active_finance_policy()
    assert fetch.call_args.args[1] == ["finance", "v1.3-PROVISIONAL", "AGENT_MVP_DEMO"]
    return policy


def test_get_active_finance_policy_parses_typed_values_and_metadata():
    policy = _load(_rows())

    assert policy.purchase_payment_days == 7
    assert policy.payroll_date == 25
    assert policy.monthly_labor_cost_krw == Decimal("12941280")
    assert policy.minimum_cash_balance_krw == Decimal("12941280")
    assert policy.cashflow_projection_days == 30
    assert policy.cash_priority_reference == "minimum_cash_balance_krw"
    assert policy.cash_priority_high_ratio == Decimal("1.0")
    assert policy.cash_priority_medium_ratio == Decimal("1.5")
    assert policy.policy_version == "v1.3-PROVISIONAL"
    assert policy.usage_scope == "AGENT_MVP_DEMO"
    assert policy.source_refs["purchase_payment_days"] == "policy:purchase_payment_days"


def test_zero_numeric_policy_is_not_treated_as_missing():
    rows = _rows()
    rows[0]["value_numeric"] = Decimal("0")
    assert _load(rows).purchase_payment_days == 0


@pytest.mark.parametrize("field", ["policy_version", "usage_scope"])
def test_policy_metadata_mismatch_fails_closed(field):
    rows = _rows()
    rows[0][field] = "wrong"
    with pytest.raises(ValueError, match="mismatch"):
        _load(rows)


def test_missing_or_inactive_required_policy_fails_closed():
    with pytest.raises(LookupError, match="purchase_payment_days"):
        _load(_rows()[1:])


@pytest.mark.parametrize(
    ("key", "mutation", "error"),
    [
        ("purchase_payment_days", {"value_kind": "TEXT"}, ValueError),
        ("purchase_payment_days", {"value_numeric": None}, ValueError),
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
