"""세 판매 전략의 수량·가격·상태·근거를 한 계약으로 고정한다."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.finance.sales_policy import FINANCE_SALES_MVP_POLICY_REF
from app.sales.proposal import _generate_scenarios, run_proposal, self_check_scenarios
from app.sales.schemas import SalesProposalInput

DELIVERY = date(2026, 9, 18)
ML_ROW_REF = (
    "v_ml_price_forecast(item=배추,target_kind=WHSL,base_dt:2026-09-16,"
    "forecast_date=2026-09-18,model_version=GOLDEN-WHSL)"
)


def _finance(ref: str) -> dict[str, object]:
    return {
        "source_agent": "finance",
        "capability": "FINANCIAL_VALIDATION",
        "reply_ref": ref,
        "runtime_status": "READY",
        "business_status": "ok",
        "payload": {"finance_verdict": "PASS"},
    }


def _logistics(ref: str) -> dict[str, object]:
    return {
        "source_agent": "logistics",
        "capability": "DELIVERY_FEASIBILITY_CONTEXT",
        "reply_ref": ref,
        "runtime_status": "READY",
        "business_status": "ok",
        "payload": {
            "sell_priority": "HIGH",
            "inventory_risk_severity": "SEVERE",
        },
    }


def _request() -> SalesProposalInput:
    replies = [
        _finance("FIN-A"),
        _finance("FIN-B"),
        _finance("FIN-C"),
        _logistics("LOG-A"),
        _logistics("LOG-B"),
        _logistics("LOG-C"),
        {
            "source_agent": "purchase",
            "capability": "ADDITIONAL_SUPPLY_CONTEXT",
            "reply_ref": "PUR-C",
            "runtime_status": "READY",
            "business_status": "ok",
            "payload": {
                "procurable_quantity_kg": 2000,
                "risks": [],
                "basis": "warehouse",
            },
        },
    ]
    return SalesProposalInput.model_validate(
        {
            "business_mode": "SPOT_SALES",
            "user_request": {
                "item": "배추",
                "partner_id": "CUST-1",
                "requested_quantity_kg": 10000,
                "preferred_unit_price_krw": 1400,
                "preferred_delivery_date": DELIVERY,
                "source_ref": "sim_runs/GOLDEN#sales_terms/ML_CURRENT_PRICE",
                "allow_additional_sourcing": True,
            },
            "ml_context": {
                "as_of": "2026-09-16",
                "item": "배추",
                "target_kind": "WHSL",
                "unit": "원/kg",
                "current_price": 1400,
                "horizon_days": 2,
                "model_version": "GOLDEN-WHSL",
                "generated_at": datetime(2026, 9, 16, tzinfo=UTC),
                "use_recommended": True,
                "daily": [
                    {
                        "date": "2026-09-17",
                        "lower": 1340,
                        "predicted": 1440,
                        "upper": 1590,
                    },
                    {
                        "date": DELIVERY,
                        "lower": 1350,
                        "predicted": 1450,
                        "upper": 1600,
                    },
                ],
            },
            "logistics_context": {
                "query_scope": {"item": "배추"},
                "sellable_supply": {
                    "status": "READY",
                    "inventory_by_item": [{"item": "배추", "available_qty_kg": 7000}],
                    "supply_capacity_by_date": [
                        {
                            "date": DELIVERY,
                            "confirmed_sellable_quantity_kg": 7000,
                        }
                    ],
                    "inventory_cost_basis": {
                        "item": "배추",
                        "quantity_kg": 7000,
                        "amount_krw": 7000000,
                        "allocation_method": "FEFO",
                        "cost_method": "ACTUAL",
                        "source_ref": "LOT-1",
                        "source_refs": ["LOT-1"],
                        "evidence_grade": "OFFICIAL",
                    },
                },
                "delivery_feasibility": {
                    "status": "READY",
                    "earliest_delivery_date": DELIVERY,
                },
                "evidence_refs": ["LOG-SUPPLY"],
            },
            "feedback": {
                "attempt": 1,
                "domain_replies": replies,
                "scenario_feedback": [
                    {"scenario_id": "SALES-001-A", "reply_refs": ["FIN-A", "LOG-A"]},
                    {"scenario_id": "SALES-001-B", "reply_refs": ["FIN-B", "LOG-B"]},
                    {
                        "scenario_id": "SALES-001-C",
                        "reply_refs": ["FIN-C", "LOG-C", "PUR-C"],
                    },
                ],
            },
        }
    )


def test_sales_three_strategy_golden_contract(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request()
    scenarios = _generate_scenarios(request)
    reply = run_proposal(request)
    by_type = {scenario.scenario_type: scenario for scenario in scenarios}

    conservative = by_type["CONSERVATIVE"]
    balanced = by_type["BALANCED"]
    aggressive = by_type["AGGRESSIVE"]

    assert conservative.objective == "RISK_DEFENSE"
    assert conservative.quantity_kg == Decimal(7000)
    assert conservative.unit_price_krw == Decimal(1600)
    assert conservative.sales_amount_krw == Decimal(11200000)
    assert conservative.status == "EXECUTABLE"
    assert conservative.supply.conditional_quantity_kg == Decimal(0)

    assert balanced.objective == "BALANCE"
    assert balanced.quantity_kg == Decimal(10000)
    assert balanced.unit_price_krw == Decimal(1450)
    assert balanced.sales_amount_krw == Decimal(14500000)
    assert balanced.variant_collapsed is True
    assert balanced.variant_collapsed_reason == "AUTHORITATIVE_INTERMEDIATE_OPTION_UNAVAILABLE"
    assert balanced.status == "INFEASIBLE"

    assert aggressive.objective == "SALES_OPPORTUNITY"
    assert aggressive.quantity_kg == Decimal(9000)
    assert aggressive.unit_price_krw == Decimal(1360)
    assert aggressive.sales_amount_krw == Decimal(12240000)
    assert aggressive.supply.confirmed_quantity_kg == Decimal(7000)
    assert aggressive.supply.conditional_quantity_kg == Decimal(2000)
    assert aggressive.unmet_quantity_kg == Decimal(1000)
    assert aggressive.status == "CONDITIONAL"
    assert "PURCHASE_COMMITMENT_REQUIRED" in aggressive.execution_dependencies

    assert all("PRICE" in scenario.sales_decision_axes for scenario in by_type.values())
    assert reply.recommended_scenario_id == conservative.scenario_id
    assert self_check_scenarios(scenarios).passed is True


def test_sales_three_strategy_evidence_lineage(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _request()
    scenarios = _generate_scenarios(request)
    reply = run_proposal(request)
    by_type = {scenario.scenario_type: scenario for scenario in scenarios}
    conservative = by_type["CONSERVATIVE"]
    balanced = by_type["BALANCED"]
    aggressive = by_type["AGGRESSIVE"]

    for scenario, own_finance in (
        (conservative, "FIN-A"),
        (balanced, "FIN-B"),
        (aggressive, "FIN-C"),
    ):
        assert "LOG-SUPPLY" in scenario.evidence_refs
        assert own_finance in scenario.evidence_refs
        assert FINANCE_SALES_MVP_POLICY_REF in scenario.evidence_refs
        assert ML_ROW_REF in scenario.evidence_refs
        assert scenario.ml_support_used is True
        other_finance = {"FIN-A", "FIN-B", "FIN-C"} - {own_finance}
        assert other_finance.isdisjoint(scenario.evidence_refs)

    assert "PUR-C" not in conservative.evidence_refs
    assert "PUR-C" not in balanced.evidence_refs
    assert "PUR-C" in aggressive.evidence_refs
    assert aggressive.supply.dependency_ref == "PUR-C"

    traces = {trace.candidate_id: trace for trace in reply.decision_trace}
    assert traces[conservative.scenario_id].policy_model_refs == ["GOLDEN-WHSL"]
    assert traces[balanced.scenario_id].policy_model_refs == ["GOLDEN-WHSL"]
    assert traces[aggressive.scenario_id].policy_model_refs == ["GOLDEN-WHSL"]
