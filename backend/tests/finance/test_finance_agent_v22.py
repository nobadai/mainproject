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
from app.finance.run_repository import get_finance_v22_run
from app.finance.schemas import FinancePolicy
from app.master.envelope import AgentRequest, ExecutionContext


class Port:
    debt = Decimal(0)
    payroll = Decimal(100)

    def load_finance_position(self, as_of):
        assert as_of == date(2025, 1, 1)
        return {"current_cash_krw": Decimal(1000), "current_debt_krw": self.debt}

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
