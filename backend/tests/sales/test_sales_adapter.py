from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from app.master.envelope import AgentRequest, ExecutionContext
from app.sales import adapter
from app.sales.schemas import SalesProposalReply


def _context() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-SALES-ADAPTER",
        as_of=date(2026, 1, 7),
        trigger="USER_REQUEST",
        policy_version="POLICY-V1",
        sim_run_id="SIM-SALES-ADAPTER",
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "business_mode": "CONTRACT_PROPOSAL_NEW",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": "5000",
            "preferred_unit_price_krw": "2000",
            "preferred_delivery_date": "2026-01-15",
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": "5000"},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": "5000"}],
                "supply_capacity_by_date": [
                    {"date": "2026-01-15", "confirmed_sellable_quantity_kg": "5000"}
                ],
            },
            "delivery_feasibility": {"status": "READY", "daily_outbound_capacity_kg": "5000"},
        },
    }
    data.update(overrides)
    return data


def _request(
    payload: dict[str, Any] | None = None,
    *,
    mode: str = "GENERATE_SALES_PROPOSAL",
    call_seq: int = 1,
) -> AgentRequest:
    return AgentRequest(
        context=_context(),
        agent="sales",
        mode=mode,  # type: ignore[arg-type]
        call_seq=call_seq,
        payload=payload if payload is not None else _payload(),
    )


def test_generate_sales_proposal_returns_ready_ok_with_payload(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    saved = {}

    def fake_save_sales_agent_run(**kwargs):
        saved.update(kwargs)
        return {**kwargs, "run_id": kwargs["run_id"]}

    monkeypatch.setattr(adapter, "save_sales_agent_run", fake_save_sales_agent_run)

    reply, metadata = adapter.sales_port(_request())

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert reply.payload["status"] == "SCENARIOS_GENERATED"
    assert reply.payload["scenarios"]
    assert "recommended_scenario_id" in reply.payload
    assert metadata.run_id == reply.run_id
    assert metadata.used_tools == ("run_proposal",)
    assert saved["run_id"] == UUID(reply.run_id)
    assert saved["runtime_status"] == "READY"
    assert saved["response_payload"]["payload"]["status"] == "SCENARIOS_GENERATED"
    assert metadata.llm_status == "SKIPPED_TEMPLATE"


def test_request_context_becomes_sales_execution_identity(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        adapter,
        "save_sales_agent_run",
        lambda **kwargs: {**kwargs, "run_id": kwargs["run_id"]},
    )

    def fake_run(request):
        captured.update(request.model_dump(mode="json"))
        return _reply(status="INPUT_INCOMPLETE", missing_data=["x"])

    monkeypatch.setattr(adapter, "run_proposal", fake_run)

    reply, _ = adapter.sales_port(_request())

    identity = captured["execution_identity"]
    assert identity["request_id"] == "REQ-SALES-ADAPTER"
    assert identity["run_id"] == reply.run_id
    assert identity["as_of"] == "2026-01-07"
    assert identity["policy_version"] == "POLICY-V1"


def test_optional_key_absence_is_not_filled(monkeypatch):
    captured = {}

    def fake_run(request):
        captured["fields"] = request.model_fields_set
        return _reply()

    monkeypatch.setattr(adapter, "run_proposal", fake_run)
    monkeypatch.setattr(
        adapter,
        "save_sales_agent_run",
        lambda **kwargs: {**kwargs, "run_id": kwargs["run_id"]},
    )

    adapter.sales_port(_request(_payload()))

    assert "contract_context" not in captured["fields"]
    assert "ml_context" not in captured["fields"]
    assert "feedback" not in captured["fields"]


def test_explicit_none_is_preserved(monkeypatch):
    captured = {}

    def fake_run(request):
        captured["fields"] = request.model_fields_set
        captured["contract_context"] = request.contract_context
        return _reply()

    monkeypatch.setattr(adapter, "run_proposal", fake_run)
    monkeypatch.setattr(
        adapter,
        "save_sales_agent_run",
        lambda **kwargs: {**kwargs, "run_id": kwargs["run_id"]},
    )

    adapter.sales_port(_request(_payload(contract_context=None)))

    assert "contract_context" in captured["fields"]
    assert captured["contract_context"] is None


def test_feedback_attempt_comes_from_payload_not_call_seq(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        adapter,
        "save_sales_agent_run",
        lambda **kwargs: {**kwargs, "run_id": kwargs["run_id"]},
    )

    def fake_run(request):
        captured["feedback_attempt"] = request.feedback_attempt
        captured["identity_attempt"] = request.execution_identity.feedback_attempt
        captured["is_refeed"] = request.is_refeed
        return _reply()

    monkeypatch.setattr(adapter, "run_proposal", fake_run)

    adapter.sales_port(_request(_payload(feedback_attempt=2), call_seq=9))

    assert captured == {"feedback_attempt": 2, "identity_attempt": 2, "is_refeed": True}


def test_all_infeasible_candidates_are_still_ready_ok(monkeypatch):
    proposal = _reply()
    dumped = proposal.model_dump()
    dumped["scenarios"][0]["status"] = "INFEASIBLE"
    dumped["scenarios"][0]["required_validations"] = []
    dumped["recommended_scenario_id"] = None
    fake = SalesProposalReply.model_validate(dumped)
    monkeypatch.setattr(adapter, "run_proposal", lambda _request: fake)
    monkeypatch.setattr(
        adapter,
        "save_sales_agent_run",
        lambda **kwargs: {**kwargs, "run_id": kwargs["run_id"]},
    )

    reply, _ = adapter.sales_port(_request())

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert reply.payload["scenarios"][0]["status"] == "INFEASIBLE"


def test_input_incomplete_maps_to_not_ready_and_carries_missing(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "run_proposal",
        lambda _request: _reply(
            status="INPUT_INCOMPLETE",
            scenarios=[],
            missing_data=["PROPOSAL_QUANTITY_REQUIRED"],
            missing_capabilities=["FINANCIAL_VALIDATION"],
        ),
    )
    monkeypatch.setattr(
        adapter,
        "save_sales_agent_run",
        lambda **kwargs: {**kwargs, "run_id": kwargs["run_id"]},
    )

    reply, _ = adapter.sales_port(_request())

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("PROPOSAL_QUANTITY_REQUIRED",)
    assert reply.missing_capability == ("FINANCIAL_VALIDATION",)
    assert reply.additional_validation_required is True
    assert reply.reasoning == (
        "판매안을 만들기 위해 필요한 정보가 부족합니다. "
        "부족한 항목을 확인해 주세요."
    )


def test_generated_without_scenarios_is_contract_error(monkeypatch):
    monkeypatch.setattr(adapter, "run_proposal", lambda _request: _reply(scenarios=[]))

    reply, _ = adapter.sales_port(_request())

    assert reply.runtime_status == "ERROR"
    assert reply.business_status == "skipped"
    assert reply.payload["validation_errors"] == ["scenarios"]


def test_unsupported_mode_uses_adapter_not_implemented_convention():
    request = AgentRequest(
        context=_context(),
        agent="sales",
        mode="STATUS_QUERY",
        payload={},
    )

    reply, _ = adapter._not_implemented(request)

    assert reply.runtime_status == "RUNTIME_NOT_READY"
    assert reply.business_status == "skipped"
    assert reply.missing_data == ("STATUS_QUERY_translation",)
    assert reply.missing_capability == ("STATUS_QUERY translation",)


def test_not_implemented_uses_disabled_llm_metadata(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")

    request = AgentRequest(
        context=_context(),
        agent="sales",
        mode="STATUS_QUERY",
        payload={},
    )

    reply, metadata = adapter._not_implemented(request)

    assert metadata.llm_status == "DISABLED"
    assert metadata.llm_model
    assert reply.reasoning == "요청하신 판매 기능은 아직 연결되지 않았습니다."


def test_status_query_uses_sales_run_history(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")

    class Run:
        run_id = UUID("11111111-1111-1111-1111-111111111111")

        def model_dump(self, mode: str = "json") -> dict[str, str]:
            return {"run_id": str(self.run_id), "as_of": "2026-01-07"}

    runs = [Run()]
    monkeypatch.setattr(adapter, "list_sales_runs", lambda **_kwargs: runs)

    reply, metadata = adapter.sales_port(_request(mode="STATUS_QUERY", payload={}))

    assert reply.runtime_status == "READY"
    assert reply.business_status == "ok"
    assert reply.run_id == "11111111-1111-1111-1111-111111111111"
    assert reply.payload == {
        "as_of": "2026-01-07",
        "recent_runs": [runs[0].model_dump(mode="json")],
    }
    assert metadata.used_tools == ("list_sales_runs",)
    assert metadata.run_id == reply.run_id
    assert metadata.llm_status == "DISABLED"


def test_generate_persists_actual_llm_metadata(monkeypatch):
    saved = {}

    def fake_save_sales_agent_run(**kwargs):
        saved.update(kwargs)
        return {**kwargs, "run_id": kwargs["run_id"]}

    proposal = _reply()
    proposal_dump = proposal.model_dump()
    proposal_dump["llm"] = {
        "status": "SUCCESS",
        "recommended_candidate_id": "SALES-001-A",
        "summary": "summary",
        "recommendation_reason": "reason",
        "risk_explanation": "risk",
        "user_message": "message",
        "llm_provider": "openai",
        "llm_model": "gpt-5",
        "llm_attempts": 2,
        "llm_fallback_used": True,
    }
    proposal_dump["recommendation"] = proposal_dump["llm"]
    monkeypatch.setattr(
        adapter,
        "run_proposal",
        lambda _request: SalesProposalReply.model_validate(proposal_dump),
    )
    monkeypatch.setattr(adapter, "save_sales_agent_run", fake_save_sales_agent_run)

    reply, metadata = adapter.sales_port(_request())

    assert saved["runtime_status"] == "READY"
    assert metadata.llm_status == "SUCCESS"
    assert metadata.llm_model == "gpt-5"
    assert metadata.llm_attempts == 2
    assert metadata.llm_fallback_used is True
    assert reply.run_id == str(saved["run_id"])


def test_generate_then_status_query_uses_same_run_id(monkeypatch):
    saved = {}

    def fake_save_sales_agent_run(**kwargs):
        saved.update(kwargs)
        return {**kwargs, "run_id": kwargs["run_id"]}

    monkeypatch.setattr(adapter, "save_sales_agent_run", fake_save_sales_agent_run)

    generated, _ = adapter.sales_port(_request())

    class Run:
        def __init__(self, run_id: UUID) -> None:
            self.run_id = run_id

        def model_dump(self, mode: str = "json") -> dict[str, str]:
            return {"run_id": str(self.run_id), "as_of": "2026-01-07"}

    monkeypatch.setattr(adapter, "list_sales_runs", lambda **_kwargs: [Run(saved["run_id"])])

    queried, metadata = adapter.sales_port(_request(mode="STATUS_QUERY", payload={}))

    assert generated.run_id == str(saved["run_id"])
    assert queried.run_id == str(saved["run_id"])
    assert metadata.run_id == queried.run_id


def test_sales_adapter_does_not_import_other_domain_agents():
    tree = ast.parse(Path("app/sales/adapter.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not {name for name in imported if name.startswith("app.finance")}
    assert not {name for name in imported if name.startswith("app.logistics")}
    assert not {name for name in imported if name.startswith("app.purchase_agent")}


def test_additional_supply_context_remains_unroutable():
    from app.master.envelope import CAPABILITY_ROUTING

    assert CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"] is None


def _reply(
    *,
    status: str = "SCENARIOS_GENERATED",
    scenarios: list[dict[str, Any]] | None = None,
    missing_data: list[str] | None = None,
    missing_capabilities: list[str] | None = None,
) -> SalesProposalReply:
    scenario_rows = scenarios
    if scenario_rows is None:
        scenario_rows = [
            {
                "scenario_id": "SALES-001-A",
                "scenario_type": "CONSERVATIVE",
                "objective": "RISK_DEFENSE",
                "business_mode": "CONTRACT_PROPOSAL_NEW",
                "item": "배추",
                "quantity_kg": "5000",
                "unit_price_krw": "2000",
                "sales_amount_krw": "10000000",
                "delivery_date": "2026-01-15",
                "supply": {"confirmed_quantity_kg": "5000"},
                "status": "EXECUTABLE",
                "required_validations": ["FINANCIAL_VALIDATION"],
            }
        ]
    return SalesProposalReply.model_validate(
        {
            "status": status,
            "business_mode": "CONTRACT_PROPOSAL_NEW",
            "is_refeed": False,
            "feedback_attempt": 0,
            "scenarios": scenario_rows,
            "missing_data": missing_data or [],
            "missing_capabilities": missing_capabilities or [],
            "recommended_scenario_id": "SALES-001-A" if scenario_rows else None,
            "llm": _recommendation("SALES-001-A" if scenario_rows else None),
            "recommendation": _recommendation("SALES-001-A" if scenario_rows else None),
            "self_check": {"passed": True},
            "decision_trace": [],
        }
    )


def _recommendation(candidate_id: str | None) -> dict[str, Any]:
    return {
        "status": "SKIPPED_TEMPLATE",
        "recommended_candidate_id": candidate_id,
        "summary": "summary",
        "recommendation_reason": "reason",
        "risk_explanation": "risk",
        "user_message": "message",
    }
