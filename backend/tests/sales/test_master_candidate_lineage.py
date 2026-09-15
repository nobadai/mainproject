"""사용자 판매 후보가 Master 이력과 Today Proposals 승인 키로 이어지는지 잠근다."""

from datetime import date
from pathlib import Path
from uuid import UUID

from app.master import decision_service, persistence, wiring
from app.master.day_gate import DayGate
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.schemas import SalesRunRequest
from app.master.service import run_sales
from app.sales.console_proposals import get_console_sales_proposals
from tests.master.logistics_pre_sales import PRE_SALES_PAYLOAD

AS_OF = date(2026, 9, 16)
SIM_RUN = "SIM-USER-SALES-LINEAGE"
RUN_A = "11111111-1111-1111-1111-111111111111"
RUN_B = "22222222-2222-2222-2222-222222222222"


def _port(payload):
    def port(request: AgentRequest):
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            payload=payload,
        )
        return reply, ExecutionMetadata(
            run_id=reply.run_id,
            request_id=reply.request_id,
            agent=request.agent,
        )

    return port


def test_user_candidate_keeps_master_run_through_today_proposals_and_approval(monkeypatch):
    """RUN-B가 뒤에 생겨도 화면이 본 RUN-A의 id와 안 번호를 승인 입력으로 유지한다."""
    wiring.reset()
    monkeypatch.setattr(
        "app.master.service.check_day_gate",
        lambda as_of, **_kwargs: DayGate(
            as_of=as_of,
            gate="PASS",
            result="ALREADY_OPENED",
            last_opened_date=as_of,
        ),
    )
    wiring.register("inventory", _port(PRE_SALES_PAYLOAD))
    wiring.register(
        "sales",
        _port(
            {
                "scenarios": [
                    {
                        "scenario_id": "SCN-A-1",
                        "scenario_type": "BALANCED",
                        "item": "배추",
                        "partner_id": "KIMCHI_FACTORY_001",
                        "quantity_kg": "100",
                        "unit_price_krw": "1200",
                        "reported_sales_amount_krw": "120000",
                        "delivery_date": "2026-09-17",
                        "payment_days": 30,
                        "required_validations": ["FINANCIAL_VALIDATION"],
                    }
                ],
                "situation": "판매 가능",
            }
        ),
    )
    wiring.register("finance", _port({"verdict": "PASS"}))
    monkeypatch.setattr(persistence, "try_save_run", lambda **_kwargs: RUN_A)

    created = run_sales(
        SalesRunRequest(
            as_of=AS_OF,
            sim_run_id=SIM_RUN,
            policy_version="v1.3",
            business_mode="SPOT_SALES",
            partner_id="KIMCHI_FACTORY_001",
            item="배추",
            requested_quantity_kg="100",
            preferred_unit_price_krw="1200",
            preferred_delivery_date=date(2026, 9, 17),
            preferred_payment_days=30,
            preferred_payment_terms_type="SINGLE",
            source_ref="CONSOLE-SALES-LINEAGE-TEST",
        )
    )
    assert created.candidates, created.model_dump(mode="json")
    scenario = dict(created.candidates[0].scenario)

    def read_proposals(statement, params=None):
        if "source_order_id" in str(statement):
            return []
        return [
            {
                "request_id": created.request_id,
                "history_run_id": created.history_run_id,
                "payload": {"recommended_scenario_id": "SCN-A-1", "scenarios": [scenario]},
                "scenario": scenario,
                "finance_verdict": "PASS",
                "finance_status": "EVALUATED",
                "rule_results": [],
                "financial_summary": {},
            }
        ]

    monkeypatch.setattr("app.sales.console_proposals.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.sales.console_proposals.fetch_all", read_proposals)
    today = get_console_sales_proposals(sim_run_id=SIM_RUN, as_of=AS_OF)
    selected = today.rows[0]

    latest_was_used = False

    def latest(*_args, **_kwargs):
        nonlocal latest_was_used
        latest_was_used = True
        return {"run_id": UUID(RUN_B), "request_id": created.request_id}

    monkeypatch.setattr(decision_service, "get_run_by_request_id", latest)
    monkeypatch.setattr(
        decision_service,
        "get_run",
        lambda run_id: {
            "run_id": run_id,
            "request_id": created.request_id,
            "response_payload": created.model_dump(mode="json"),
        },
    )
    approval_run = decision_service._run_for(created.request_id, selected.history_run_id)

    assert created.history_run_id == RUN_A
    assert selected.history_run_id == RUN_A
    assert selected.scenario_id == "SCN-A-1"
    assert approval_run["run_id"] == UUID(RUN_A)
    assert latest_was_used is False


def test_user_sales_form_uses_only_the_master_endpoint():
    source = (
        Path(__file__).parents[3]
        / "frontend"
        / "src"
        / "components"
        / "console"
        / "SalesCandidatePanel.tsx"
    ).read_text(encoding="utf-8")
    api_source = (Path(__file__).parents[3] / "frontend" / "src" / "lib" / "api.ts").read_text(
        encoding="utf-8"
    )

    assert "salesRun({" in source
    assert "console-proposal" not in source
    assert '"/master/sales/run"' in api_source
