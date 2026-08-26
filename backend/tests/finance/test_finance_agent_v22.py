from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from app.finance.agent_v22 import (
    DEFAULT_MAX_REPLANS,
    DEFAULT_MAX_TOOL_CALLS,
    PRE_PURCHASE_TOOLS,
    SCENARIO_VALIDATION_TOOLS,
    FinanceAgentController,
    FinanceToolRegistry,
    ToolAction,
)
from app.finance.repository import (
    FinanceDataNotReady,
    PostgresFinanceAsOfDataPort,
)
from app.finance.run_repository import get_finance_v22_run, save_finance_v22_run
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
                "minimum_cash_balance_krw": "policy:min-cash",
                "cash_priority_reference": "policy:pressure",
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


@patch("app.finance.agent_v22.save_finance_v22_run")
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
    assert {e.claim for e in reply.evidences} >= {
        "available_cash",
        "finance_cap_amount_krw",
        "projected_cash_min",
        "payment_pressure",
    }
    save_run.assert_called_once()


@patch("app.finance.agent_v22.save_finance_v22_run")
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
    assert evidence["payroll_payment_day"].source == "persona"
    assert evidence["payroll_payment_day"].evidence_grade == "SIM_FIXED"
    assert metadata.llm_fallback_used is True


@patch("app.finance.agent_v22.save_finance_v22_run")
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
    results = reply.payload["scenarios"]
    assert [result["scenario_id"] for result in results] == ["S1", "S2", "S3"]
    assert [result["verdict"] for result in results] == ["ok", "reject", "ok"]
    assert results[1]["adjustability"] == "ADJUSTABLE"
    assert results[1]["verdict"] == "reject"
    for result in results:
        branch = result["scenario_id"]
        derived_refs = {
            ref
            for evidence in result["evidences"]
            for ref in evidence["ref_ids"]
            if ref.startswith("FIN-V22:") or ":cashflow" in ref or ":FIN-CAP" in ref
        }
        assert derived_refs
        assert all(branch in ref for ref in derived_refs)
    assert validate_reply(req, reply, metadata) == ()


@patch("app.finance.agent_v22.save_finance_v22_run")
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
                    "total_amount_krw": 1000,
                    "payment_schedule": [
                        {"payment_date": "2025-01-02", "amount_krw": 1000}
                    ],
                },
                {
                    "scenario_id": "LATE",
                    "total_amount_krw": 1000,
                    "payment_schedule": [
                        {"payment_date": "2025-01-06", "amount_krw": 1000}
                    ],
                },
            ]
        },
    )
    reply, _ = FinanceAgentController(ReceivablePort(), planner).run(req)
    early, late = reply.payload["scenarios"]
    assert early["projected_cash_min"] < late["projected_cash_min"]
    assert early["verdict"] == "reject"
    assert late["verdict"] == "ok"


@patch("app.finance.agent_v22.save_finance_v22_run")
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


@patch("app.finance.agent_v22.save_finance_v22_run")
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
        "app.finance.agent_v22.save_finance_v22_run", side_effect=RuntimeError("database down")
    ):
        reply, _ = FinanceAgentController(Port(), planner).run(request())
    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.reasoning == "Finance run history persistence failed."


@patch("app.finance.agent_v22.save_finance_v22_run")
def test_zero_debt_does_not_require_debt_policy(save_run):
    planner = Planner(
        [
            *(ToolAction(name) for name in PRE_PURCHASE_TOOLS),
            ToolAction(finalize=True),
        ]
    )
    reply, _ = FinanceAgentController(Port(), planner).run(request())
    assert reply.runtime_status == "READY"


@patch("app.finance.agent_v22.save_finance_v22_run")
def test_positive_debt_without_policy_is_not_ready(save_run):
    port = Port()
    port.debt = Decimal(1)
    planner = Planner([ToolAction("assess_finance_position")])
    reply, _ = FinanceAgentController(port, planner).run(request())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("debt_policy",)


@patch("app.finance.agent_v22.save_finance_v22_run")
def test_missing_payroll_is_not_ready_and_never_zero_filled(save_run):
    port = Port()
    port.payroll = None
    planner = Planner([ToolAction("project_cashflow")])
    reply, _ = FinanceAgentController(port, planner).run(request())
    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("payroll_amount",)
    assert "projected_cash_min" not in reply.payload
    assert reply.needs_followup is True


@patch("app.finance.agent_v22.save_finance_v22_run")
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


def test_payment_schedule_sum_mismatch_is_error():
    planner = Planner([ToolAction("evaluate_purchase_scenario")])
    with patch("app.finance.agent_v22.save_finance_v22_run"):
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


def test_duplicate_tool_call_is_blocked():
    planner = Planner(
        [ToolAction("assess_finance_position"), ToolAction("assess_finance_position")]
    )
    with patch("app.finance.agent_v22.save_finance_v22_run"):
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
    with patch("app.finance.agent_v22.save_finance_v22_run"):
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


def test_v22_run_id_resolves_finance_history():
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
        assert get_finance_v22_run(run_id) == row
    assert fetch.call_args.args[1] == (run_id,)


def test_v22_run_history_persists_reproducibility_fields():
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
        save_finance_v22_run(request=req, reply=reply, metadata=metadata)
    params = execute.call_args.args[1]
    assert params[5:8] == ("v1.3-PROVISIONAL", "USER_REQUEST", 1)
