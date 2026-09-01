from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest

from app.finance.agent import (
    DEFAULT_MAX_REPLANS,
    DEFAULT_MAX_TOOL_CALLS,
    PRE_PURCHASE_TOOLS,
    SCENARIO_VALIDATION_TOOLS,
    FinanceAgentController,
    FinanceToolRegistry,
    ToolAction,
    validate_finance_scenario_output,
)
from app.finance.repository import (
    FinanceDataNotReady,
    PostgresFinanceAsOfDataPort,
)
from app.finance.run_repository import get_finance_execution, save_finance_execution
from app.finance.schemas import CashEvent, FinancePolicy
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
    validate_reply,
)


class Port:
    debt = Decimal(0)
    payroll = Decimal(100)

    def load_finance_position(self, as_of):
        assert as_of == date(2025, 1, 1)
        return {
            "finance_state_id": "FIN-STATE-TEST",
            "current_cash_krw": Decimal(1000),
            "current_debt_krw": self.debt,
        }

    def load_policy(self, as_of, policy_version):
        assert as_of == date(2025, 1, 1)
        assert policy_version == "v1.3-PROVISIONAL"
        return FinancePolicy(
            purchase_payment_days=1,
            payroll_date=10,
            monthly_labor_cost_krw=Decimal(100),
            minimum_cash_balance_krw=Decimal(100),
            cashflow_projection_days=30,
            cash_priority_reference="minimum_cash_balance_krw",
            cash_priority_high_ratio=Decimal(1),
            cash_priority_medium_ratio=Decimal(2),
            policy_version="v1.3-PROVISIONAL",
            usage_scope="AGENT_MVP_DEMO",
            source_refs={
                "payroll_date": "POL-PAYROLL-DATE",
                "monthly_labor_cost_krw": "FACT-PAYROLL-AMOUNT",
                "purchase_payment_days": "policy:purchase-days",
                "minimum_cash_balance_krw": "policy:min-cash",
                "cash_priority_reference": "policy:pressure",
                "cash_priority_high_ratio": "policy:pressure-high",
                "cash_priority_medium_ratio": "policy:pressure-medium",
            },
        )

    def load_payroll(self, as_of, horizon):
        del as_of, horizon
        return self.payroll

    def load_obligations(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_receivables(self, as_of, horizon):
        del as_of, horizon
        return []

    def load_debt_schedule(self, as_of, horizon):
        del as_of, horizon
        raise FinanceDataNotReady("debt_policy")


class Planner:
    model = "test-planner"
    attempts = 0

    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, **kwargs):
        del kwargs
        self.attempts += 1
        return next(self.actions)


class FailingFinalizer:
    model = "failing-finalizer"
    attempts = 0

    def finalize(self, **kwargs):
        del kwargs
        self.attempts += 1
        raise TimeoutError("finalization timeout")


class ReceivablePort(Port):
    def load_receivables(self, as_of, horizon):
        del as_of, horizon
        return [
            CashEvent(
                event_date=date(2025, 1, 5),
                event_type="RECEIVABLE",
                amount_krw=Decimal(500),
                direction="INFLOW",
                ref_id="REC-TEST-1",
            )
        ]


class CountingPort(Port):
    policy_reads = 0

    def load_policy(self, as_of, policy_version):
        self.policy_reads += 1
        return super().load_policy(as_of, policy_version)


class MissingN5Port(Port):
    def load_policy(self, as_of, policy_version):
        return super().load_policy(as_of, policy_version).model_copy(
            update={"purchase_payment_days": None}
        )


class MarginPolicyPort(Port):
    def load_policy(self, as_of, policy_version):
        policy = super().load_policy(as_of, policy_version)
        return policy.model_copy(
            update={
                "margin_defense_floor_rate": Decimal("0.12"),
                "source_refs": {
                    **policy.source_refs,
                    "margin_defense_floor_rate": "policy:margin-floor",
                },
            }
        )


class BaseViolationPort(Port):
    def load_obligations(self, as_of, horizon):
        del as_of, horizon
        return [
            CashEvent(
                event_date=date(2025, 1, 2),
                event_type="COMMITTED_OUTFLOW",
                amount_krw=Decimal(1000),
                direction="OUTFLOW",
                ref_id="BASE-OUTFLOW-1",
            )
        ]


def request(mode="PRE_PURCHASE", payload=None):
    return AgentRequest(
        context=ExecutionContext(
            request_id="req-1",
            as_of=date(2025, 1, 1),
            trigger="USER_REQUEST",
            policy_version="v1.3-PROVISIONAL",
        ),
        agent="finance",
        mode=mode,
        payload=payload or {},
    )


def test_registry_exposes_exactly_six_business_tools():
    registry = FinanceToolRegistry(Port())
    assert registry.names_for("PRE_PURCHASE") == PRE_PURCHASE_TOOLS
    assert registry.names_for("SCENARIO_VALIDATION") == SCENARIO_VALIDATION_TOOLS
    assert not hasattr(registry, "load_finance_position")
    assert DEFAULT_MAX_TOOL_CALLS == 8
    assert DEFAULT_MAX_REPLANS == 2


@patch("app.finance.agent.save_finance_execution")
def test_pre_purchase_dynamic_order_and_envelope(save_run):
    order = [
        "analyze_payment_pressure",
        "assess_finance_position",
        "calculate_purchase_finance_cap",
        "project_cashflow",
    ]
    planner = Planner([*(ToolAction(name) for name in order), ToolAction(finalize=True)])
    reply, metadata = FinanceAgentController(Port(), planner).run(request())
    assert reply.request_id == "req-1"
    assert reply.as_of == date(2025, 1, 1)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert metadata.used_tools == tuple(order)
    assert "budget_remaining" not in vars(request())
    assert reply.payload["available_cash"] == 1000
    assert reply.payload["purchase_payment_days"] == 1
    assert reply.payload["margin_defense_floor_rate"] is None
    assert {e.claim for e in reply.evidences} >= {
        "available_cash",
        "finance_cap_amount_krw",
        "base_projected_cash_min",
        "payment_pressure",
    }
    save_run.assert_called_once()


@patch("app.finance.agent.save_finance_execution")
def test_pre_purchase_returns_margin_policy_with_evidence(save_run):
    planner = Planner(
        [
            ToolAction("assess_finance_position"),
            ToolAction("calculate_purchase_finance_cap"),
            ToolAction("analyze_payment_pressure"),
            ToolAction(finalize=True),
        ]
    )
    reply, _ = FinanceAgentController(MarginPolicyPort(), planner).run(request())
    assert reply.payload["margin_defense_floor_rate"] == 0.12
    evidence = {item.claim: item for item in reply.evidences}
    assert evidence["margin_defense_floor_rate"].ref_ids == ("policy:margin-floor",)


@patch("app.finance.agent.save_finance_execution")
def test_missing_n5_is_not_ready_and_never_returns_finance_cap(save_run):
    reply, _ = FinanceAgentController(
        MissingN5Port(), Planner([ToolAction("assess_finance_position")])
    ).run(request())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("purchase_payment_days",)
    assert "finance_cap_amount_krw" not in reply.payload


@patch("app.finance.agent.save_finance_execution")
def test_capability_completion_does_not_require_every_pre_purchase_tool(save_run):
    planner = Planner(
        [
            ToolAction("analyze_payment_pressure", reason="Check payment concentration."),
            ToolAction("assess_finance_position", reason="Read the Finance position."),
            ToolAction("calculate_purchase_finance_cap", reason="Derive the purchase boundary."),
            ToolAction(finalize=True),
        ]
    )
    req = request()
    reply, metadata = FinanceAgentController(Port(), planner).run(req)
    assert reply.runtime_status == "READY"
    assert metadata.used_tools == (
        "analyze_payment_pressure",
        "assess_finance_position",
        "calculate_purchase_finance_cap",
    )
    assert "project_cashflow" not in metadata.used_tools
    assert validate_reply(req, reply, metadata) == ()
    assert "Check payment concentration." in metadata.observations[0]
    evidence = {item.claim: item for item in reply.evidences}
    assert evidence["available_cash"].source == "finance"
    assert evidence["payment_pressure"].source == "tool_calc"
    assert evidence["payroll_payment_day"].source == "finance"
    assert evidence["payroll_payment_day"].evidence_grade == "SIM_FIXED"
    assert evidence["payroll_payment_day"].ref_ids == ("POL-PAYROLL-DATE",)
    assert metadata.llm_fallback_used is False
    assert reply.payload["policy_version_used"] == "v1.3-PROVISIONAL"
    assert evidence["policy_version_used"].unit == "version"
    assert evidence["critical_payment_dates"].value == 100
    assert evidence["critical_payment_dates"].unit == "KRW"


@patch("app.finance.agent.save_finance_execution")
def test_multi_scenario_results_are_isolated_and_common_contract_valid(save_run):
    planner = Planner(
        [
            ToolAction("evaluate_purchase_scenario", reason="Validate S1."),
            ToolAction(finalize=True),
            ToolAction("evaluate_purchase_scenario", reason="Validate S2."),
            ToolAction("validate_amount_adjustment", reason="Validate S2 amount."),
            ToolAction(finalize=True),
            ToolAction("evaluate_purchase_scenario", reason="Validate S3."),
            ToolAction(finalize=True),
        ]
    )
    req = request(
        "SCENARIO_VALIDATION",
        {
            "scenarios": [
                {"scenario_id": "S1", "total_amount_krw": 700},
                {"scenario_id": "S2", "total_amount_krw": 900},
                {"scenario_id": "S3", "total_amount_krw": 600},
            ]
        },
    )
    reply, metadata = FinanceAgentController(Port(), planner).run(req)
    assert reply.runtime_status == "READY"
    assert reply.business_status == "reject"
    results = reply.payload["verdicts"]
    assert [result["scenario_id"] for result in results] == ["S1", "S2", "S3"]
    assert [result["verdict"] for result in results] == ["ok", "reject", "ok"]
    assert results[1]["adjustability"] == "ADJUSTABLE"
    assert results[1]["verdict"] == "reject"
    assert len(reply.suggested_adjustments) == 1
    assert "S2" in reply.suggested_adjustments[0].ref_ids[0]
    for result in results:
        branch = result["scenario_id"]
        derived_refs = {
            ref
            for evidence in result["evidences"]
            for ref in evidence["ref_ids"]
            if ref.startswith("FIN-AGENT:") or ":cashflow" in ref or ":FIN-CAP" in ref
        }
        assert derived_refs
        assert all(branch in ref for ref in derived_refs)
    assert validate_reply(req, reply, metadata) == ()
    assert validate_finance_scenario_output(reply) == ()


@patch("app.finance.agent.save_finance_execution")
def test_base_cashflow_failure_is_not_presented_as_repairable(save_run):
    planner = Planner([ToolAction("evaluate_purchase_scenario"), ToolAction(finalize=True)])
    reply, _ = FinanceAgentController(BaseViolationPort(), planner).run(
        request(
            "SCENARIO_VALIDATION",
            {"scenario_id": "BASE-FAIL", "total_amount_krw": 10},
        )
    )
    assert reply.runtime_status == "READY"
    assert reply.business_status == "reject"
    assert reply.payload["verdict"] == "reject"
    assert reply.payload["adjustability"] == "NOT_ADJUSTABLE"
    assert reply.payload["finance_cap_amount_krw"] == 0
    assert reply.suggested_adjustments == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"scenarios": []},
        {"scenarios": [{"scenario_id": "S", "total_amount_krw": 1}] * 2},
        {"scenario_id": "", "total_amount_krw": 1},
        {"scenario_id": "S", "total_amount_krw": 0},
        {"scenario_id": "S", "total_amount_krw": True},
    ],
)
def test_finance_owned_payload_validation_rejects_invalid_scenarios(payload):
    with patch("app.finance.agent.save_finance_execution"):
        reply, _ = FinanceAgentController(Port(), Planner([])).run(
            request("SCENARIO_VALIDATION", payload)
        )
    assert reply.runtime_status == "ERROR"


@patch("app.finance.agent.save_finance_execution")
def test_scenario_payment_dates_change_projected_cashflow(save_run):
    planner = Planner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction("validate_amount_adjustment"),
            ToolAction(finalize=True),
            ToolAction("evaluate_purchase_scenario"),
            ToolAction(finalize=True),
        ]
    )
    req = request(
        "SCENARIO_VALIDATION",
        {
            "scenarios": [
                    {
                        "scenario_id": "EARLY",
                        "total_qty_kg": 1,
                        "total_amount_krw": 1000,
                        "max_price": 1000,
                        "split_plan": [{"seq": 1, "date": "2025-01-01", "qty_kg": 1}],
                        "payment_schedule": [
                            {
                                "seq": 1, "purchase_date": "2025-01-01",
                                "payment_date": "2025-01-02", "qty_kg": 1,
                                "amount_krw": 1000, "amount_max_krw": 1000,
                                "basis": "as_of_unit_price",
                            }
                        ],
                    },
                    {
                        "scenario_id": "LATE",
                        "total_qty_kg": 1,
                        "total_amount_krw": 1000,
                        "max_price": 1000,
                        "split_plan": [{"seq": 1, "date": "2025-01-05", "qty_kg": 1}],
                        "payment_schedule": [
                            {
                                "seq": 1, "purchase_date": "2025-01-05",
                                "payment_date": "2025-01-06", "qty_kg": 1,
                                "amount_krw": 1000, "amount_max_krw": 1000,
                                "basis": "as_of_unit_price",
                            }
                        ],
                },
            ]
        },
    )
    reply, _ = FinanceAgentController(ReceivablePort(), planner).run(req)
    early, late = reply.payload["verdicts"]
    assert early["scenario_projected_cash_min"] < late["scenario_projected_cash_min"]
    assert early["verdict"] == "reject"
    assert late["verdict"] == "ok"


@patch("app.finance.agent.save_finance_execution")
def test_default_payment_date_is_reconstructed_from_approved_policy(save_run):
    planner = Planner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction("validate_amount_adjustment"),
            ToolAction(finalize=True),
        ]
    )
    reply, _ = FinanceAgentController(Port(), planner).run(
        request(
            "SCENARIO_VALIDATION",
            {"scenario_id": "DEFAULT", "total_amount_krw": 900, "payment_schedule": None},
        )
    )
    assert reply.runtime_status == "READY"
    assert reply.payload["payment_schedule"] == [
        {"payment_date": "2025-01-02", "amount_krw": 900}
    ]


@patch("app.finance.agent.save_finance_execution")
def test_multi_scenario_reuses_one_policy_context(save_run):
    port = CountingPort()
    planner = Planner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction(finalize=True),
            ToolAction("evaluate_purchase_scenario"),
            ToolAction(finalize=True),
        ]
    )
    reply, _ = FinanceAgentController(port, planner).run(
        request(
            "SCENARIO_VALIDATION",
            {
                "scenarios": [
                    {"scenario_id": "S1", "total_amount_krw": 700},
                    {"scenario_id": "S2", "total_amount_krw": 600},
                ]
            },
        )
    )
    assert reply.runtime_status == "READY"
    assert port.policy_reads == 1


def test_run_history_persistence_failure_becomes_agent_error():
    planner = Planner(
        [
            ToolAction("analyze_payment_pressure"),
            ToolAction("assess_finance_position"),
            ToolAction("calculate_purchase_finance_cap"),
            ToolAction(finalize=True),
        ]
    )
    with patch(
        "app.finance.agent.save_finance_execution", side_effect=RuntimeError("database down")
    ):
        reply, _ = FinanceAgentController(Port(), planner).run(request())
    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.reasoning == "재무 실행이력을 저장하지 못했습니다."


@patch("app.finance.agent.save_finance_execution")
def test_finalization_failure_uses_deterministic_fallback_after_evidence(save_run):
    planner = Planner(
        [
            ToolAction("assess_finance_position"),
            ToolAction("calculate_purchase_finance_cap"),
            ToolAction("analyze_payment_pressure"),
            ToolAction(finalize=True),
        ]
    )
    reply, metadata = FinanceAgentController(
        Port(), planner, finalizer=FailingFinalizer()
    ).run(request())
    assert reply.runtime_status == "READY"
    assert reply.payload["finance_cap_amount_krw"] == 800
    assert metadata.llm_status == "FALLBACK"
    assert metadata.llm_fallback_used is True
    assert not any(character.isdigit() for character in reply.reasoning)


@patch("app.finance.agent.save_finance_execution")
def test_zero_debt_does_not_require_debt_policy(save_run):
    planner = Planner(
        [
            *(ToolAction(name) for name in PRE_PURCHASE_TOOLS),
            ToolAction(finalize=True),
        ]
    )
    reply, _ = FinanceAgentController(Port(), planner).run(request())
    assert reply.runtime_status == "READY"


@patch("app.finance.agent.save_finance_execution")
def test_positive_debt_without_policy_is_not_ready(save_run):
    port = Port()
    port.debt = Decimal(1)
    planner = Planner([ToolAction("assess_finance_position")])
    reply, _ = FinanceAgentController(port, planner).run(request())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("debt_policy",)


@patch("app.finance.agent.save_finance_execution")
def test_missing_payroll_is_not_ready_and_never_zero_filled(save_run):
    port = Port()
    port.payroll = None
    planner = Planner([ToolAction("project_cashflow")])
    reply, _ = FinanceAgentController(port, planner).run(request())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("payroll_schedule",)
    assert "base_projected_cash_min" not in reply.payload
    assert reply.needs_followup is True


@patch("app.finance.agent.save_finance_execution")
def test_scenario_reject_stays_reject_with_verified_amount_adjustment(save_run):
    planner = Planner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction(
                "validate_amount_adjustment",
                {"axis": "amount", "candidate_amount_krw": 800},
            ),
            ToolAction(finalize=True),
        ]
    )
    reply, _ = FinanceAgentController(Port(), planner).run(
        request(
            "SCENARIO_VALIDATION",
            {"scenario_id": "S1", "total_amount_krw": 1000, "payment_schedule": None},
        )
    )
    assert reply.business_status == "reject"
    assert reply.payload["verdict"] == "reject"
    assert reply.payload["adjustability"] == "ADJUSTABLE"
    assert reply.suggested_adjustments[0].axis == "amount"
    assert reply.needs_followup is True
    assert reply.additional_validation_required is False


def test_payment_schedule_sum_mismatch_is_error():
    planner = Planner([ToolAction("evaluate_purchase_scenario")])
    with patch("app.finance.agent.save_finance_execution"):
        reply, _ = FinanceAgentController(Port(), planner).run(
            request(
                "SCENARIO_VALIDATION",
                {
                    "scenario_id": "S1",
                    "total_amount_krw": 10,
                    "payment_schedule": [{"payment_date": "2025-01-02", "amount_krw": 9}],
                },
            )
        )
    assert reply.runtime_status == "ERROR"


@patch("app.finance.agent.save_finance_execution")
def test_purchase_pr62_shape_uses_label_identity_and_validates_base_stress(save_run):
    del save_run
    scenario = {
        "label": "기본",
        "strategy_type": "timing",
        "coverage_days": 3,
        "total_qty_kg": 3,
        "total_amount_krw": 300,
        "max_price": 120,
        "margin_warning": False,
        "split_plan": [
            {"seq": 1, "date": "2025-01-01", "qty_kg": 1},
            {"seq": 2, "date": "2025-01-02", "qty_kg": 2},
        ],
        "sourcing_plan": [
            {"market": "가락", "grade": "상", "qty_kg": 3, "grade_unit_price": 100}
        ],
        "payment_schedule": [
            {
                "seq": 1,
                "purchase_date": "2025-01-01",
                "payment_date": "2025-01-02",
                "qty_kg": 1,
                "amount_krw": 100,
                "amount_max_krw": 120,
                "basis": "as_of_unit_price",
            },
            {
                "seq": 2,
                "purchase_date": "2025-01-02",
                "payment_date": "2025-01-03",
                "qty_kg": 2,
                "amount_krw": 200,
                "amount_max_krw": 240,
                "basis": "as_of_unit_price",
            },
        ],
        "expected_margin_rate": 0.3,
        "rationale": [
            {
                "source": "시세관측",
                "claim": "현재 단가",
                "ref_id": "PRICE-1",
                "evidence_grade": "OFFICIAL",
                "evidence_detail": "가락시장 기준",
            }
        ],
        "risks": [],
    }
    reply, _ = FinanceAgentController(
        Port(), Planner([ToolAction("evaluate_purchase_scenario"), ToolAction(finalize=True)])
    ).run(request("SCENARIO_VALIDATION", {"scenarios": [scenario]}))

    assert reply.runtime_status == "READY"
    assert reply.payload["verdicts"][0]["scenario_id"] == "기본"
    assert reply.payload["verdicts"][0]["verdict"] == "ok"
    assert reply.payload["verdicts"][0]["stress_projected_cash_min"] < reply.payload[
        "verdicts"
    ][0]["scenario_projected_cash_min"]


@patch("app.finance.agent.save_finance_execution")
def test_purchase_labels_must_be_unique_when_scenario_id_is_absent(save_run):
    del save_run
    reply, _ = FinanceAgentController(Port(), Planner([])).run(
        request(
            "SCENARIO_VALIDATION",
            {
                "scenarios": [
                    {"label": "기본", "total_amount_krw": 100},
                    {"label": "기본", "total_amount_krw": 200},
                ]
            },
        )
    )
    assert reply.runtime_status == "ERROR"
    assert "unique" in reply.reasoning


def test_duplicate_tool_call_is_blocked():
    planner = Planner(
        [ToolAction("assess_finance_position"), ToolAction("assess_finance_position")]
    )
    with patch("app.finance.agent.save_finance_execution"):
        reply, _ = FinanceAgentController(Port(), planner).run(request())
    assert reply.runtime_status == "ERROR"


def test_non_amount_adjustment_axis_is_rejected():
    planner = Planner(
        [
            ToolAction("evaluate_purchase_scenario"),
            ToolAction(
                "validate_amount_adjustment",
                {"axis": "quantity", "candidate_amount_krw": 800},
            ),
        ]
    )
    with patch("app.finance.agent.save_finance_execution"):
        reply, _ = FinanceAgentController(Port(), planner).run(
            request(
                "SCENARIO_VALIDATION",
                {"scenario_id": "S1", "total_amount_krw": 1000},
            )
        )
    assert reply.runtime_status == "ERROR"


def test_postgres_port_fails_closed_for_historical_as_of():
    row = {"state_date": date(2025, 1, 2)}
    with patch("app.finance.repository._get_current_finance_state_row", return_value=row):
        try:
            PostgresFinanceAsOfDataPort().load_finance_position(date(2025, 1, 1))
        except FinanceDataNotReady as error:
            assert error.key == "historical_finance_position"
        else:
            raise AssertionError("historical request silently read current state")


def test_run_id_resolves_finance_history():
    run_id = UUID("00000000-0000-0000-0000-000000000022")
    row = {
        "run_id": run_id,
        "request_id": "req-1",
        "mode": "PRE_PURCHASE",
        "as_of": date(2025, 1, 1),
        "runtime_status": "READY",
    }
    with (
        patch("app.finance.run_repository.get_db_schema", return_value="haetdeul"),
        patch("app.finance.run_repository.fetch_one", return_value=row) as fetch,
    ):
        assert get_finance_execution(run_id) == row
    assert fetch.call_args.args[1] == (run_id,)


def test_run_history_persists_reproducibility_fields():
    req = request()
    reply = AgentReply(
        request_id=req.context.request_id,
        as_of=req.context.as_of,
        agent="finance",
        mode=req.mode,
        run_id="00000000-0000-0000-0000-000000000023",
        runtime_status="READY",
        business_status="ok",
    )
    metadata = ExecutionMetadata(
        run_id=reply.run_id,
        request_id=reply.request_id,
        agent="finance",
        used_tools=("assess_finance_position",),
        tool_order=(1,),
    )
    with (
        patch("app.finance.run_repository.get_db_schema", return_value="haetdeul"),
        patch(
            "app.finance.run_repository.execute_returning_one",
            return_value={"run_id": UUID(reply.run_id)},
        ) as execute,
    ):
        save_finance_execution(request=req, reply=reply, metadata=metadata)
    params = execute.call_args.args[1]
    assert params[5:8] == ("v1.3-PROVISIONAL", "USER_REQUEST", 1)
