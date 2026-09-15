from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

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
        "판매안을 만들기 위해 필요한 정보가 부족합니다. 부족한 항목을 확인해 주세요."
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


def _history_run(
    run_id: UUID,
    *,
    sim_run_id: str = "SIM-SALES-ADAPTER",
    scenarios: list[dict[str, Any]] | None = None,
):
    """저장된 판매 실행 하나. **봉투 안에 실행 축이 들어 있다.**"""

    class Run:
        def __init__(self) -> None:
            self.run_id = run_id
            self.as_of = date(2026, 1, 7)
            self.runtime_status = "READY"
            self.request_payload = {"context": {"sim_run_id": sim_run_id}}
            self.response_payload = {
                "request_id": "REQ-SALES-ADAPTER",
                "payload": {
                    "status": "SCENARIOS_GENERATED",
                    "scenarios": scenarios
                    if scenarios is not None
                    else [{"item": "배추", "quantity_kg": "1000.0"}],
                    "missing_capabilities": ["FINANCIAL_VALIDATION"],
                },
            }

    return Run()


def _proposal(item: str, scenario_type: str, **over: Any):
    from app.sales.console_proposals import ConsoleSalesProposal

    data: dict[str, Any] = {
        "request_id": f"REQ-{item}",
        "history_run_id": "RUN-1",
        "scenario_id": f"SALES-001-{scenario_type[0]}",
        "scenario_type": scenario_type,
        "objective": None,
        "item": item,
        "partner_id": "KIMCHI_FACTORY_001",
        "quantity_kg": "1000",
        "unit_price_krw": "1500",
        "reported_sales_amount_krw": "1500000",
        "payment_days": 7,
        "delivery_date": date(2026, 1, 8),
        "status": "UNRESOLVED",
        "rationale": [],
        "risks": [],
        "uncertainties": [],
        "finance_verdict": "PASS",
        "finance_status": "EVALUATED",
        "finance_reason_codes": [],
        "contribution_margin_krw": None,
        "contribution_margin_rate": None,
        "current_partner_ar_krw": None,
        "available_credit_krw": None,
        "projected_partner_ar_krw": None,
        "credit_limit_krw": None,
        "required_collection_before_sale_krw": None,
        "credit_utilization_rate": None,
        "expected_credit_recovery_date": None,
        "missing_capabilities": ["FINANCIAL_VALIDATION"],
        "evidence_refs": [],
        "source_ref": None,
        "cost_basis_amount_krw": None,
        "cost_basis_quantity_kg": None,
        "cost_basis_method": None,
        "cost_basis_refs": [],
        "confirmed_quantity_kg": None,
        "conditional_quantity_kg": None,
        "additional_supply_required": None,
        "ml_support_used": None,
        "recommended": False,
    }
    data.update(over)
    return ConsoleSalesProposal.model_validate(data)


def _proposals(rows, *, request_count: int | None = None, hidden: int = 0):
    from app.sales.console_proposals import ConsoleSalesProposalsResponse

    return ConsoleSalesProposalsResponse(
        sim_run_id="SIM-SALES-ADAPTER",
        as_of=date(2026, 1, 7),
        request_count=len({row.request_id for row in rows})
        if request_count is None
        else request_count,
        hidden_zero_quantity=hidden,
        rows=rows,
    )


#: 사용자 말풍선에 나오면 안 되는 기계용 글자.
_RAW = (
    "request_id",
    "runtime_status",
    "READY",
    "SCENARIOS_GENERATED",
    "proposal_count",
    "no_stock_count",
    "pending_validations",
    "FINANCIAL_VALIDATION",
    "sim_run_id",
    "recent_runs",
    "REQ-",
    "SIM-",
    "PASS",
    "REVIEW_REQUIRED",
    "FAIL",
    "CONSERVATIVE",
    "BALANCED",
    "AGGRESSIVE",
)


def _status(monkeypatch, proposals, runs=None):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    monkeypatch.setattr(
        adapter,
        "list_sales_runs",
        lambda **_kwargs: (
            runs
            if runs is not None
            else [_history_run(UUID("11111111-1111-1111-1111-111111111111"))]
        ),
    )
    monkeypatch.setattr(adapter, "get_console_sales_proposals", lambda **_kwargs: proposals)
    return adapter.sales_port(_request(mode="STATUS_QUERY", payload={}))


def test_status_query_answers_in_words_a_user_reads(monkeypatch):
    """🔴 마스터는 부서 payload 의 키를 **이름 그대로** 편다. 그래서 키와 값이 곧 화면 글자다."""
    reply, metadata = _status(
        monkeypatch,
        _proposals(
            [
                _proposal("양파", "CONSERVATIVE"),
                _proposal("양파", "BALANCED", finance_verdict="REVIEW_REQUIRED"),
                _proposal(
                    "배추",
                    "CONSERVATIVE",
                    finance_verdict="FAIL",
                    required_collection_before_sale_krw="1158615",
                ),
                _proposal("배추", "BALANCED", finance_verdict=None),
            ]
        ),
    )

    assert reply.runtime_status == "READY"
    assert reply.run_id == "11111111-1111-1111-1111-111111111111"
    assert reply.payload == {
        "as_of": "2026-01-07",
        "오늘 검토 중인 판매": "양파 판매안 2개 · 배추 판매안 2개",
        "재무 검토": "재무 검토가 필요한 판매안이 있습니다 (진행 가능한 판매안 1개)",
        "선회수 필요": (
            "1개 안은 기존 미수금을 먼저 회수해야 현재 여신한도 안에서 판매할 수 있습니다"
        ),
        "확정된 판매": "아직 없습니다",
    }
    assert metadata.used_tools == ("list_sales_runs", "get_console_sales_proposals")


def test_the_master_speech_bubble_carries_no_machine_words(monkeypatch):
    """마스터가 실제로 렌더링한 문자열로 확인한다 — payload 만 보면 펴는 방식이 바뀐 날 놓친다."""
    from app.master.answer import facts_from_status, render_answer
    from app.master.status_flow import StatusOutcome

    reply, _ = _status(
        monkeypatch,
        _proposals(
            [
                _proposal("무", "CONSERVATIVE", recommended=True, sale_status="CONFIRMED"),
                _proposal("무", "AGGRESSIVE", finance_verdict="FAIL"),
            ]
        ),
    )
    outcome = StatusOutcome(
        status_code="S1_ANSWERED",
        reason="sales 상태를 조회했다.",
        plan=None,  # 사실 줄을 펴는 데는 계획을 쓰지 않는다
        answers={"sales": dict(reply.payload)},
    )
    text = render_answer(facts_from_status(outcome))

    for raw in _RAW:
        assert raw not in text, (raw, text)
    assert "추천 판매안 무 안정 우선" in text
    assert "확정된 판매 무 안정 우선" in text


def test_finance_review_state_comes_from_finance_not_from_the_first_sales_reply(monkeypatch):
    """1차 판매 회신은 늘 «재무 검토 미완» 이다. 재무가 이미 판정했으면 그 판정을 말한다."""
    reply, _ = _status(monkeypatch, _proposals([_proposal("배추", "CONSERVATIVE")]))

    assert reply.payload["재무 검토"] == "현재 조건에서 진행 가능한 판매안이 준비되어 있습니다"


def test_status_query_says_so_when_this_run_has_no_history(monkeypatch):
    reply, _ = _status(monkeypatch, _proposals([], request_count=0), runs=[])

    assert reply.payload["오늘 판매안"] == "이 날짜에는 판매가 돌지 않았습니다."
    assert "없습니다" in reply.reasoning


def test_status_query_names_an_empty_day_with_nothing_to_sell(monkeypatch):
    """⚠️ «물량이 없어 안이 서지 않은 것» 과 «안을 안 낸 것» 은 다른 사실이다."""
    reply, _ = _status(monkeypatch, _proposals([], request_count=3, hidden=3))

    assert reply.payload["오늘 판매안"] == "팔 수 있는 물량이 없어 판매안이 서지 않았습니다."


def test_unreadable_proposals_are_not_reported_as_no_sales(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    monkeypatch.setattr(adapter, "list_sales_runs", lambda **_kwargs: [])

    def broken(**_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(adapter, "get_console_sales_proposals", broken)
    reply, _ = adapter.sales_port(_request(mode="STATUS_QUERY", payload={}))

    assert reply.payload["오늘 판매안"].startswith("판매안 정보를 읽지 못했습니다")


def test_status_query_answers_only_for_this_run(monkeypatch):
    """🔴 실행 축이 다른 이력을 «우리 판매 진행 상황» 으로 내지 않는다."""
    mine = _history_run(UUID("11111111-1111-1111-1111-111111111111"))
    theirs = _history_run(
        UUID("22222222-2222-2222-2222-222222222222"), sim_run_id="SIM-SOMEONE-ELSE"
    )
    seen: dict[str, Any] = {}

    def proposals(**kwargs):
        seen.update(kwargs)
        return _proposals([_proposal("배추", "CONSERVATIVE")])

    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    monkeypatch.setattr(adapter, "list_sales_runs", lambda **_kwargs: [theirs, mine])
    monkeypatch.setattr(adapter, "get_console_sales_proposals", proposals)
    reply, _ = adapter.sales_port(_request(mode="STATUS_QUERY", payload={}))

    assert reply.run_id == "11111111-1111-1111-1111-111111111111"
    assert seen == {"sim_run_id": "SIM-SALES-ADAPTER", "as_of": date(2026, 1, 7)}


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

    monkeypatch.setattr(
        adapter, "list_sales_runs", lambda **_kwargs: [_history_run(saved["run_id"])]
    )

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


def test_additional_supply_context_routes_to_purchase_boundary_query():
    """Master opened this route on 2026-09-10 after Purchase shipped the mode in #485.

    The expectation flipped, not the meaning: Sales still owns the capability name and
    still does not call Purchase itself. What is asserted here is that the name Sales
    emits reaches Purchase as a *boundary* question (``SUPPLY_CAPACITY_QUERY``) and not
    as ``GENERATE_SCENARIOS`` -- the latter would build a procurement plan inside the
    sales cycle, silently and without error.
    """
    from app.master.envelope import CAPABILITY_ROUTING

    assert CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"] == (
        "purchase",
        "SUPPLY_CAPACITY_QUERY",
    )


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


@pytest.mark.parametrize(
    ("verdicts", "sentence"),
    [
        (["FAIL", "FAIL", "FAIL"], "현재 조건으로 바로 진행하기 어려운 판매안이 3개 있습니다"),
        (["PASS", "PASS"], "현재 조건에서 진행 가능한 판매안이 준비되어 있습니다"),
        (
            ["PASS", "REVIEW_REQUIRED"],
            "재무 검토가 필요한 판매안이 있습니다 (진행 가능한 판매안 1개)",
        ),
        ([None, "FAIL"], "재무 검토가 필요한 판매안이 있습니다"),
        (["UNKNOWN_CODE"], "재무 검토가 필요한 판매안이 있습니다"),
        (
            ["PASS", "FAIL", "FAIL"],
            "진행 가능한 판매안 1개와 현재 조건으로 진행하기 어려운 판매안 2개가 있습니다",
        ),
    ],
)
def test_the_review_sentence_is_chosen_from_the_finance_verdicts(verdicts, sentence):
    assert adapter.review_sentence(verdicts) == sentence
