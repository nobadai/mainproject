from datetime import UTC, date, datetime, timedelta
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
            "preferred_delivery_date": "2026-09-10",
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
                ],
            },
            "delivery_feasibility": {
                "status": "UNRESOLVED",
                "daily_outbound_capacity_kg": 5000,
                "reason_codes": ["OUTBOUND_EVIDENCE_UNRESOLVED"],
            },
        },
    }
    data.update(overrides)
    return SalesProposalInput.model_validate(data)


def _forecast(item="배추"):
    as_of = date(2026, 9, 1)
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "target_kind": "AUC",
        "unit": "원/kg",
        "current_price": 9999,
        "horizon_days": 1,
        "model_version": "test",
        "generated_at": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
        "daily": [
            {
                "date": (as_of + timedelta(days=1)).isoformat(),
                "predicted": 9999,
                "lower": 9000,
                "upper": 11000,
            }
        ],
    }


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
        ml_context=_forecast(),
    )
    reply = run_proposal(request)

    assert all(s.unit_price_krw is None and s.sales_amount_krw is None for s in reply.scenarios)
    assert all("PRICE_CONTEXT_REQUIRED" in s.uncertainties for s in reply.scenarios)


def test_refeed_creates_new_lineage_and_keeps_purchase_reference_conditional(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        is_refeed=True,
        feedback_attempt=1,
        feedback={
            "attempt": 1,
            "domain_replies": [
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    "reply_ref": "PUR-1",
                    "runtime_status": "READY",
                    "payload": {"procurable_quantity_kg": 2000},
                }
            ],
            "scenario_feedback": [{"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}],
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


def test_second_refeed_attempt_creates_r2_lineage(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(is_refeed=True, feedback_attempt=2))
    scenario = reply.scenarios[0]
    assert scenario.scenario_id == "SALES-001-A-R2"
    assert scenario.parent_scenario_id == "SALES-001-A"
    assert scenario.revision == 2


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
                "scenario_feedback": [
                    {"scenario_id": "SALES-001-A", "reply_refs": ["FIN-1"]},
                    {"scenario_id": "SALES-001-B", "reply_refs": ["FIN-1"]},
                    {"scenario_id": "SALES-001-C", "reply_refs": ["FIN-1"]},
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
            "sellable_supply": {
                "status": "READY",
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 0}
                ],
            },
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
        # 원계약은 공격안에 남기고, 공급 부족 보수안은 별도 조정안으로 표현한다.
        assert reply.scenarios[2].quantity_kg == Decimal(5000)
        assert reply.scenarios[0].quantity_kg == Decimal(3000)
        assert reply.scenarios[0].sales_decision_axes == ["QUANTITY"]


def test_delivery_date_uses_exact_logistics_vector_not_query_scope_max(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        user_request={
            "item": "배추",
            "requested_quantity_kg": 6000,
            "preferred_delivery_date": "2026-09-10",
        },
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 7000},
            "sellable_supply": {
                "status": "READY",
                "supply_capacity_by_date": [
                    {
                        "date": "2026-09-10",
                        "confirmed_sellable_quantity_kg": 4200,
                        "freshness_unresolved_inbound_quantity_kg": 1800,
                        "uncertainties": ["CONFIRMED_INBOUND_FRESHNESS_UNRESOLVED"],
                    }
                ],
            },
            "delivery_feasibility": {"status": "UNRESOLVED", "daily_outbound_capacity_kg": 5000},
            "evidence_refs": ["LOG-1"],
        },
    )
    scenario = run_proposal(request).scenarios[2]
    assert scenario.supply.confirmed_quantity_kg == Decimal(4200)
    assert scenario.supply.required_additional_quantity_kg == Decimal(1800)
    assert "CONFIRMED_INBOUND_FRESHNESS_UNRESOLVED" in scenario.uncertainties
    assert "LOG-1" in scenario.evidence_refs


def test_missing_delivery_vector_does_not_fall_back_to_scope_max(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 7000},
            "sellable_supply": {"status": "READY", "supply_capacity_by_date": []},
            "delivery_feasibility": {"status": "UNRESOLVED"},
        }
    )
    scenario = run_proposal(request).scenarios[2]
    assert scenario.supply.confirmed_quantity_kg is None
    assert scenario.supply.required_additional_quantity_kg is None
    assert "SELLABLE_SUPPLY_CONTEXT" in scenario.required_validations


def test_no_date_uses_current_inventory_view_without_summing_lots(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request(
        user_request={"item": "배추", "requested_quantity_kg": 8000},
        logistics_context={
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 9999},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 7000}],
                "lot_constraints": [
                    {"lot_id": "A", "item": "배추", "available_qty_kg": 4000},
                    {"lot_id": "B", "item": "배추", "available_qty_kg": 4000},
                ],
            },
            "delivery_feasibility": {"status": "UNRESOLVED"},
        },
    )
    scenario = run_proposal(request).scenarios[2]
    assert scenario.supply.confirmed_quantity_kg == Decimal(7000)
    assert scenario.supply.required_additional_quantity_kg == Decimal(1000)


@pytest.mark.parametrize(
    ("business_mode", "extra", "reason"),
    [
        ("CONTRACT_FULFILLMENT", {}, "CONTRACT_CONTEXT_REQUIRED"),
        ("CONTRACT_PROPOSAL_RENEWAL", {}, "PREVIOUS_CONTRACT_CONTEXT_REQUIRED"),
    ],
)
def test_contract_modes_without_authoritative_contract_are_input_incomplete(
    monkeypatch, business_mode, extra, reason
):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(business_mode=business_mode, **extra))
    assert reply.status == "INPUT_INCOMPLETE"
    assert reply.scenarios == []
    assert reason in reply.missing_data


@pytest.mark.parametrize(
    ("context_key", "context", "reason"),
    [
        ("logistics_context", {"query_scope": {"item": "무"}}, "LOGISTICS_ITEM_MISMATCH"),
        ("ml_context", _forecast("양파"), "ML_ITEM_MISMATCH"),
    ],
)
def test_item_mismatch_is_input_incomplete(monkeypatch, context_key, context, reason):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(**{context_key: context}))
    assert reply.status == "INPUT_INCOMPLETE"
    assert reason in reply.missing_data


def test_feedback_distribution_uses_reply_refs_and_rejects_unknown_ref(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    valid = _request(
        feedback={
            "domain_replies": [
                {
                    "source_agent": "finance",
                    "capability": "FINANCIAL_VALIDATION",
                    "reply_ref": "FIN-A",
                    "runtime_status": "READY",
                    "business_status": "reject",
                }
            ],
            "scenario_feedback": [{"scenario_id": "SALES-001-A", "reply_refs": ["FIN-A"]}],
        }
    )
    reply = run_proposal(valid)
    assert "FINANCE_FAIL" in reply.scenarios[0].risks
    assert "FINANCE_FAIL" not in reply.scenarios[1].risks

    invalid = _request(
        feedback={"scenario_feedback": [{"scenario_id": "SALES-001-A", "reply_refs": ["UNKNOWN"]}]}
    )
    invalid_reply = run_proposal(invalid)
    assert invalid_reply.status == "INPUT_INCOMPLETE"
    assert "SCENARIO_FEEDBACK_UNKNOWN_REPLY_REF" in invalid_reply.missing_data


def test_proposal_reply_exposes_state_and_llm_alias(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    reply = run_proposal(_request(is_refeed=True, feedback_attempt=2))
    assert reply.status == "SCENARIOS_GENERATED"
    assert reply.is_refeed is True
    assert reply.feedback_attempt == 2
    assert reply.llm == reply.recommendation
