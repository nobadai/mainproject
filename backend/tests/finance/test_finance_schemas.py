from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.finance.schemas import FinanceSalesRequest, PurchaseAgentOutput


def test_purchase_agent_v04_contract_keeps_decimal(purchase_payload):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    assert request.scenarios[0].total_amount_krw == Decimal(7125000)
    assert request.scenarios[0].sourcing_plan[0].qty_kg == 3000


def test_purchase_agent_contract_rejects_legacy_ton_fields(purchase_payload):
    scenario = purchase_payload["scenarios"][0]
    scenario["total_quantity_ton"] = scenario.pop("total_qty_kg")

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(purchase_payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_amount_krw", True),
        ("coverage_days", True),
    ],
)
def test_purchase_scenario_rejects_boolean_numbers(purchase_payload, field, value):
    purchase_payload["scenarios"][0][field] = value

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(purchase_payload)


def test_purchase_sourcing_rejects_boolean_and_zero_price(purchase_payload):
    line = purchase_payload["scenarios"][0]["sourcing_plan"][0]
    line["grade_unit_price"] = True
    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(purchase_payload)

    line["grade_unit_price"] = 0
    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(purchase_payload)


def test_purchase_scenario_rejects_quantity_total_mismatch(purchase_payload):
    purchase_payload["scenarios"][0]["sourcing_plan"][1]["qty_kg"] = 1000

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(purchase_payload)


def test_purchase_agent_contract_rejects_duplicate_kg_names(purchase_payload):
    scenario = purchase_payload["scenarios"][0]
    scenario["total_quantity_kg"] = scenario.pop("total_qty_kg")
    scenario["split_plan"][0]["quantity_kg"] = scenario["split_plan"][0].pop("qty_kg")

    with pytest.raises(ValidationError):
        PurchaseAgentOutput.model_validate(purchase_payload)


def test_sales_contract_rejects_boolean_settlement_days(sales_payload):
    sales_payload["channel_terms"][0]["settlement_days"] = True

    with pytest.raises(ValidationError):
        FinanceSalesRequest.model_validate(sales_payload)


def test_finance_sales_contract_does_not_accept_sales_candidates(sales_payload):
    sales_payload["candidates"] = [{"channel": "KIMCHI_FACTORY"}]

    with pytest.raises(ValidationError):
        FinanceSalesRequest.model_validate(sales_payload)
