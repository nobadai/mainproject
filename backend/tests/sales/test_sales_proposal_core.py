from decimal import Decimal

import pytest

from app.sales.proposal import run_proposal, self_check_scenarios
from app.sales.schemas import SalesProposalInput


def _request(**overrides):
    data = {
        "business_mode": "CONTRACT_PROPOSAL_NEW",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": 5000,
            "preferred_unit_price_krw": 2000,
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
            "sellable_supply_status": "READY",
            "delivery_feasibility": {
                "status": "UNRESOLVED",
                "daily_outbound_capacity_kg": 5000,
                "reason_codes": ["OUTBOUND_EVIDENCE_UNRESOLVED"],
            },
        },
    }
    data.update(overrides)
    return SalesProposalInput.model_validate(data)


def test_final_core_generates_three_typed_scenarios_without_fake_intermediate(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request())

    assert [s.scenario_type for s in reply.scenarios] == ["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
    assert [s.objective for s in reply.scenarios] == [
        "RISK_DEFENSE",
        "BALANCE",
        "SALES_OPPORTUNITY",
    ]
    assert reply.scenarios[0].quantity_kg == Decimal(3000)
    assert reply.scenarios[0].supply.required_additional_quantity_kg == Decimal(0)
    assert reply.scenarios[2].supply.required_additional_quantity_kg == Decimal(2000)
    assert "ADDITIONAL_SUPPLY_CONTEXT" in reply.scenarios[2].required_validations
    assert reply.scenarios[1].variant_collapsed is True
    assert reply.scenarios[1].variant_collapsed_reason
    assert "DELIVERY_FEASIBILITY_CONTEXT" in reply.missing_capabilities


def test_price_is_null_and_amount_does_not_use_market_or_invented_value(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        user_request={"item": "배추", "requested_quantity_kg": 5000},
        ml_context={"current_price": 9999, "daily": [{"price": 8888}]},
    )
    reply = run_proposal(request)

    assert all(s.unit_price_krw is None and s.sales_amount_krw is None for s in reply.scenarios)
    assert all("PRICE_CONTEXT_REQUIRED" in s.uncertainties for s in reply.scenarios)


def test_refeed_creates_new_lineage_and_keeps_purchase_reference_conditional(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        is_refeed=True,
        feedback={
            "attempt": 1,
            "domain_replies": [
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    "reply_ref": "PUR-1",
                    "runtime_status": "READY",
                    "scenario_id": "SALES-001-C",
                    "payload": {"procurable_quantity_kg": 2000},
                }
            ],
        },
    )
    reply = run_proposal(request)
    confirmed, aggressive = reply.scenarios[0], reply.scenarios[2]

    assert aggressive.scenario_id == "SALES-001-C-R1"
    assert aggressive.parent_scenario_id == "SALES-001-C"
    assert aggressive.revision == 1
    assert aggressive.conditional_purchase is True
    assert "PUR-1" in aggressive.evidence_refs
    assert "PUR-1" not in confirmed.evidence_refs
    assert confirmed.conditional_purchase is False


def test_finance_fail_remains_visible_but_is_not_recommended(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(
        _request(
            feedback={
                "attempt": 1,
                "domain_replies": [
                    {
                        "source_agent": "finance",
                        "capability": "FINANCIAL_VALIDATION",
                        "reply_ref": "FIN-1",
                        "runtime_status": "READY",
                        "business_status": "reject",
                    }
                ],
            }
        )
    )

    assert all("FINANCE_FAIL" in scenario.risks for scenario in reply.scenarios)
    assert reply.recommended_scenario_id is None
    assert reply.recommendation.recommended_candidate_id is None


def test_self_check_rejects_amount_mutation():
    scenario = run_proposal(_request()).scenarios[0]
    scenario.sales_amount_krw = Decimal(1)
    check = self_check_scenarios([scenario])
    assert check.passed is False
    assert "SALES_AMOUNT_INCONSISTENT" in check.issue_codes


def test_zero_confirmed_supply_is_not_missing_value(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 0},
            "delivery_feasibility": {"status": "UNRESOLVED"},
        }
    )
    reply = run_proposal(request)
    assert reply.scenarios[0].supply.confirmed_quantity_kg == Decimal(0)
    assert reply.scenarios[0].supply.required_additional_quantity_kg == Decimal(0)
    assert reply.scenarios[2].supply.required_additional_quantity_kg == Decimal(5000)


@pytest.mark.parametrize(
    "business_mode",
    [
        "CONTRACT_FULFILLMENT",
        "CONTRACT_PROPOSAL_NEW",
        "CONTRACT_PROPOSAL_RENEWAL",
        "SPOT_SALES",
    ],
)
def test_all_sales_business_modes_are_represented_without_implicit_policy(
    monkeypatch, business_mode
):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    extra = {
        "contract_context": {
            "contract_quantity_kg": 5000,
            "contract_unit_price_krw": 2000,
        }
    }
    reply = run_proposal(_request(business_mode=business_mode, **extra))

    assert all(scenario.business_mode == business_mode for scenario in reply.scenarios)
    if business_mode == "CONTRACT_FULFILLMENT":
        assert all(scenario.quantity_kg == Decimal(5000) for scenario in reply.scenarios)
