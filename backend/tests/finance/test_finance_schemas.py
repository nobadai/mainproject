import pytest
from pydantic import ValidationError

from app.finance.schemas import ChannelTerm
from app.purchase_agent.schemas import PurchaseProposal


def test_purchase_agent_v04_contract_uses_int_kg_contract(purchase_payload):
    request = PurchaseProposal.model_validate(purchase_payload)
    scenario = request.scenarios[0]

    assert scenario.total_amount_krw == 7_125_000
    assert type(scenario.total_amount_krw) is int
    assert scenario.sourcing_plan[0].qty_kg == 3000
    assert type(scenario.sourcing_plan[0].qty_kg) is int


def test_purchase_agent_contract_rejects_legacy_ton_fields(purchase_payload):
    scenario = purchase_payload["scenarios"][0]
    scenario["total_quantity_ton"] = scenario.pop("total_qty_kg")

    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(purchase_payload)


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
        PurchaseProposal.model_validate(purchase_payload)


def test_purchase_sourcing_rejects_boolean_and_zero_price(purchase_payload):
    line = purchase_payload["scenarios"][0]["sourcing_plan"][0]
    line["grade_unit_price"] = True
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(purchase_payload)

    line["grade_unit_price"] = 0
    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(purchase_payload)


def test_purchase_scenario_rejects_quantity_total_mismatch(purchase_payload):
    purchase_payload["scenarios"][0]["sourcing_plan"][1]["qty_kg"] = 1000

    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(purchase_payload)


def test_purchase_agent_contract_rejects_duplicate_kg_names(purchase_payload):
    scenario = purchase_payload["scenarios"][0]
    scenario["total_quantity_kg"] = scenario.pop("total_qty_kg")
    scenario["split_plan"][0]["quantity_kg"] = scenario["split_plan"][0].pop("qty_kg")

    with pytest.raises(ValidationError):
        PurchaseProposal.model_validate(purchase_payload)


def test_channel_term_rejects_boolean_settlement_days():
    with pytest.raises(ValidationError):
        ChannelTerm(
            channel_type="DIRECT_B2B",
            partner_id="KIMCHI_FACTORY_001",
            settlement_days=True,
        )
