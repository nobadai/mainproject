from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest

from app.finance.adapter import finance_port
from app.finance.schemas import FinanceDebtPolicy, FinancePolicy, FinanceRuntimeContext, FinanceSnapshot
from app.master.envelope import AgentRequest, ExecutionContext
from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput


def _sales_request(**overrides):
    data = {
        "business_mode": "CONTRACT_PROPOSAL_NEW",
        "user_request": {
            "item": "배추",
            "partner_id": "P-100",
            "requested_quantity_kg": 5000,
            "preferred_unit_price_krw": 2000,
            "preferred_delivery_date": "2026-09-10",
            "preferred_payment_days": 30,
            "allow_additional_sourcing": True,
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
                "status": "READY",
                "daily_outbound_capacity_kg": 5000,
                "reason_codes": [],
            },
        },
    }
    data.update(overrides)
    return SalesProposalInput.model_validate(data)


@pytest.fixture
def finance_context():
    return FinanceRuntimeContext(
        snapshot=FinanceSnapshot(
            snapshot_id=None,
            finance_state_id="FIN-DAY30-LOAN",
            sim_run_id="SIM-BURNIN-202512",
            state_date=date(2025, 12, 31),
            state_type="DAY30",
            financing_mode="LOAN_BASELINE",
            current_cash_krw=Decimal("31993913.770000"),
            minimum_operating_cash_krw=Decimal("15902640.000000"),
            committed_outflows_krw=Decimal("0.000000"),
            unsettled_purchase_payables_krw=Decimal("0.000000"),
            financial_limit_krw=Decimal("16091273.770000"),
        ),
        policy=FinancePolicy(
            purchase_payment_days=7,
            payroll_date=25,
            monthly_labor_cost_krw=Decimal(12941280),
            minimum_cash_balance_krw=Decimal(12941280),
            cashflow_projection_days=30,
            cash_priority_reference="minimum_cash_balance_krw",
            cash_priority_high_ratio=Decimal("1.0"),
            cash_priority_medium_ratio=Decimal("1.5"),
            policy_version="v1.3-PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            source_refs={
                "payroll_date": "policy:payroll_date",
                "monthly_labor_cost_krw": "policy:monthly_labor_cost_krw",
                "fixture": "test",
            },
        ),
        debt_policy=FinanceDebtPolicy(
            debt_runtime_status="SIM_FIXED_EXECUTED",
            debt_principal_krw=Decimal("45272104.184486"),
            debt_execution_date=date(2025, 12, 2),
            debt_annual_rate=Decimal("0.025"),
            debt_term_months=72,
            debt_grace_months=36,
            debt_grace_payment_mode="INTEREST_ONLY",
            debt_repayment_method="EQUAL_PRINCIPAL_AFTER_GRACE",
            debt_payment_frequency="MONTHLY",
            debt_payment_day_rule="MONTH_END",
            debt_first_payment_rule="EXECUTION_MONTH_END",
            debt_interest_method="OUTSTANDING_PRINCIPAL_ANNUAL_RATE_DIV_12",
            policy_version="v1.3-PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            source_refs={"fixture": "test"},
        ),
        cash_events=(),
    )


def _finance_request(payload):
    return AgentRequest(
        context=ExecutionContext(
            request_id="REQ-SALES-VALIDATION",
            as_of=date(2025, 12, 31),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode="SALES_VALIDATION",
        payload=payload,
    )


def _capture_save(saved):
    def _inner(_query, params):
        saved["params"] = params
        return {"run_id": UUID("00000000-0000-0000-0000-000000000009")}

    return _inner


def _finance_scenario(scenario_id="SALES-001-A", **overrides):
    payload = {
        "scenario_id": scenario_id,
        "partner_id": "P-100",
        "item": "배추",
        "quantity_kg": "3000",
        "unit_price_krw": "2000",
        "reported_sales_amount_krw": "6000000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2026-09-10",
        "source_ref": f"SALES:{scenario_id}",
    }
    payload.update(overrides)
    return payload


def _finance_reply(ref="FIN-1", scenario_id="SALES-001-A", verdict="PASS"):
    return {
        "source_agent": "finance",
        "capability": "FINANCIAL_VALIDATION",
        "reply_ref": ref,
        "runtime_status": "READY",
        "business_status": "ok" if verdict == "PASS" else "reject",
        "payload": {
            "scenario_id": scenario_id,
            "finance_verdict": verdict,
            "financial_summary": {
                "contribution_margin_krw": 1000,
                "contribution_margin_rate": "0.1",
                "scenario_projected_cash_min": 500000,
                "depends_on_projected_inflow": False,
            },
            "reason_codes": [f"FINANCE_{verdict}"],
            "evidence_refs": [ref],
        },
    }


def _purchase_reply(ref="PUR-1", quantity=2000, available_date="2026-09-12"):
    return {
        "source_agent": "purchase",
        "capability": "ADDITIONAL_SUPPLY_CONTEXT",
        "reply_ref": ref,
        "runtime_status": "READY",
        "business_status": "ok" if quantity else "reject",
        "payload": {
            "procurable_quantity_kg": quantity,
            "risks": [] if quantity else ["NO_SUPPLY"],
            "available_date": available_date,
        },
    }


def test_sales_proposal_runs_graph_and_returns_ranked_recommendation(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")

    reply = run_proposal(_sales_request())

    assert reply.status == "SCENARIOS_GENERATED"
    assert reply.scenarios
    assert reply.recommended_scenario_id is None
    assert reply.recommendation.recommended_candidate_id is None
    assert reply.self_check.passed is True
    assert reply.decision_trace


def test_sales_missing_price_does_not_invent_amount_and_terminates(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _sales_request(
        user_request={
            "item": "배추",
            "partner_id": "P-100",
            "requested_quantity_kg": 5000,
            "preferred_delivery_date": "2026-09-10",
            "allow_additional_sourcing": True,
        }
    )

    reply = run_proposal(request)

    assert reply.status == "SCENARIOS_GENERATED"
    assert reply.recommended_scenario_id is None
    assert all(scenario.unit_price_krw is None for scenario in reply.scenarios)
    assert all(scenario.sales_amount_krw is None for scenario in reply.scenarios)
    assert all("PRICE_CONTEXT_REQUIRED" in scenario.uncertainties for scenario in reply.scenarios)
    assert reply.self_check.passed is True


def test_sales_validation_requests_and_purchase_need_are_candidate_scoped(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")

    reply = run_proposal(_sales_request())
    by_id = {scenario.scenario_id: scenario for scenario in reply.scenarios}

    assert "FINANCIAL_VALIDATION" in by_id["SALES-001-A"].required_validations
    assert "ADDITIONAL_SUPPLY_CONTEXT" in by_id["SALES-001-C"].required_validations
    assert by_id["SALES-001-C"].supply.required_additional_quantity_kg == Decimal(2000)
    for scenario in reply.scenarios:
        assert all(not ref.startswith("purchase:called") for ref in scenario.evidence_refs)


def test_finance_sales_validation_port_preserves_identity_and_missing_states(finance_context):
    saved = {}
    complete = _finance_scenario("SALES-001-A")
    incomplete = _finance_scenario("SALES-001-B")
    del incomplete["unit_price_krw"]

    with (
        patch("app.finance.adapter.get_current_finance_runtime_context", return_value=finance_context),
        patch("app.finance.adapter.load_partner_receivables", return_value=[]),
        patch("app.finance.llm.planner.finance_llm_enabled", return_value=False),
        patch("app.finance.adapter.finance_llm_enabled", return_value=False),
        patch("app.finance.execution.get_db_schema", return_value="haetdeul"),
        patch("app.finance.execution.execute_returning_one", side_effect=_capture_save(saved)),
    ):
        reply, metadata = finance_port(
            _finance_request({"scenarios": [complete, incomplete]})
        )

    results = {item["scenario_id"]: item for item in reply.payload["scenario_results"]}
    assert set(metadata.used_tools) == {"evaluate_sales_scenario"}
    assert saved["params"][3] == "SALES_VALIDATION"
    assert results["SALES-001-A"]["status"] == "RUNTIME_NOT_READY"
    assert results["SALES-001-A"]["finance_verdict"] is None
    assert "partner_credit_limit_krw" in results["SALES-001-A"]["missing_data"]
    assert results["SALES-001-B"]["status"] == "INPUT_INCOMPLETE"
    assert results["SALES-001-B"]["missing_fields"] == ["unit_price_krw"]


def test_finance_fail_feedback_excludes_candidate_and_keeps_lineage(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _sales_request(
        is_refeed=True,
        feedback_attempt=1,
        feedback={
            "attempt": 1,
            "domain_replies": [
                _finance_reply("FIN-A", "SALES-001-A", "FAIL"),
                _finance_reply("FIN-B", "SALES-001-B", "PASS"),
            ],
            "scenario_feedback": [
                {"scenario_id": "SALES-001-A", "reply_refs": ["FIN-A"]},
                {"scenario_id": "SALES-001-B", "reply_refs": ["FIN-B"]},
            ],
        },
    )

    reply = run_proposal(request)

    assert reply.recommended_scenario_id != "SALES-001-A-R1"
    rejected = next(trace for trace in reply.decision_trace if trace.candidate_id == "SALES-001-A-R1")
    kept = next(trace for trace in reply.decision_trace if trace.candidate_id == "SALES-001-B-R1")
    assert rejected.finance_verdict == "FAIL"
    assert rejected.recommended is False
    assert "FIN-A" in rejected.reply_refs
    assert kept.finance_verdict == "PASS"


def test_purchase_feedback_conditions_candidate_without_rewriting_delivery(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    request = _sales_request(
        is_refeed=True,
        feedback_attempt=1,
        feedback={
            "attempt": 1,
            "domain_replies": [_purchase_reply("PUR-C", quantity=2000, available_date="2026-09-12")],
            "scenario_feedback": [{"scenario_id": "SALES-001-C", "reply_refs": ["PUR-C"]}],
        },
    )

    reply = run_proposal(request)
    aggressive = next(scenario for scenario in reply.scenarios if scenario.parent_scenario_id == "SALES-001-C")

    assert aggressive.scenario_id == "SALES-001-C-R1"
    assert aggressive.conditional_purchase is True
    assert aggressive.supply.conditional_quantity_kg == Decimal(2000)
    assert aggressive.supply.dependency_ref == "PUR-C"
    assert aggressive.delivery_date.isoformat() == "2026-09-10"
    assert "PUR-C" in aggressive.evidence_refs
    assert "DELIVERY_FEASIBILITY_CONTEXT" not in aggressive.required_validations
