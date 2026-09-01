from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app.finance.schemas import FinanceSalesResponse
from app.main import app
from app.master.envelope import AgentReply, ExecutionMetadata
from app.master.wiring import registry


def test_finance_agent_api_goes_through_finance_port():
    """`/finance/agent` 는 Controller 를 직접 만들지 않고 Master 와 같은 Port 를 탄다."""
    request_payload = {
        "context": {
            "request_id": "REQ-FINANCE-API-001",
            "as_of": "2025-12-31",
            "trigger": "USER_REQUEST",
            "policy_version": "v1.3-PROVISIONAL",
        },
        "agent": "finance",
        "mode": "PRE_PURCHASE",
        "call_seq": 1,
        "payload": {},
    }
    reply = AgentReply(
        request_id="REQ-FINANCE-API-001",
        as_of=date(2025, 12, 31),
        agent="finance",
        mode="PRE_PURCHASE",
        run_id="00000000-0000-0000-0000-000000000009",
        runtime_status="READY",
        business_status="ok",
        payload={"finance_cap_amount_krw": 1000},
        reasoning="검증된 재무 근거가 보고된 매입 가능 경계를 뒷받침합니다.",
    )
    metadata = ExecutionMetadata(
        run_id=reply.run_id,
        request_id="REQ-FINANCE-API-001",
        agent="finance",
    )
    with patch("app.finance.router.finance_port", return_value=(reply, metadata)) as port:
        response = TestClient(app).post("/finance/agent", json=request_payload)

    assert response.status_code == 200
    assert port.call_count == 1
    assert port.call_args.args[0].mode == "PRE_PURCHASE"
    assert response.json()["payload"] == {"finance_cap_amount_krw": 1000}


def test_master_registry_uses_the_same_finance_port():
    """Master Registry 에 등록된 재무 Port 가 Adapter 정본과 동일한 객체인지 읽어서 검증한다.

    Master 코드는 이번 작업에서 수정하지 않는다 — 등록 상태만 확인한다.
    """
    from app.finance.adapter import finance_port

    assert registry().get("finance") is finance_port


def test_finance_sales_api(sales_payload):
    result = FinanceSalesResponse(
        snapshot_id="FIN-DAY30-LOAN",
        approval_id="H1-20260821-001",
        runtime_status="RUNTIME_NOT_READY",
        verdict=None,
        base_cash_priority=None,
        sales_cash_priority=None,
        collection_preferences=[
            {
                "channel_type": "DIRECT_B2B",
                "partner_id": "KIMCHI_FACTORY_001",
                "settlement_days": 30,
                "liquidity_rank": 1,
            }
        ],
        hard_constraints=[],
        soft_warnings=["CASH_PRIORITY_POLICY_UNRESOLVED"],
    )
    with patch("app.finance.router.run_finance_sales", return_value=result):
        response = TestClient(app).post("/finance/sales", json=sales_payload)

    assert response.status_code == 200
    assert response.json()["sales_cash_priority"] is None
    assert response.json()["interpretation"]["summary"]
    assert response.json()["llm_status"] == "DISABLED"


def test_finance_openapi_keeps_sales_and_drops_replaced_legacy_endpoints():
    """`/finance/sales` 는 Finance B 호환 경로로 남고, Agent 가 대체한 경로는 사라진다."""
    schema = TestClient(app).get("/openapi.json").json()

    assert "/finance/sales" in schema["paths"]
    assert "/finance/agent" in schema["paths"]
    assert "/finance/runs" in schema["paths"]
    assert "/finance/procurement" not in schema["paths"]
    assert "/finance/core-review" not in schema["paths"]


def test_finance_runs_api_forwards_filters():
    run = {
        "run_id": UUID("00000000-0000-0000-0000-000000000001"),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": "FIN-DAY30-LOAN",
        "runtime_status": "RUNTIME_NOT_READY",
        "verdict": None,
        "request_payload": {"meta": {"as_of": "2026-08-21"}},
        "response_payload": {"runtime_status": "RUNTIME_NOT_READY", "verdict": None},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    with patch("app.finance.router.list_finance_runs", return_value=[run]) as list_runs:
        response = TestClient(app).get(
            "/finance/runs",
            params={
                "cycle": "PROCUREMENT",
                "as_of": "2026-08-21",
                "runtime_status": "RUNTIME_NOT_READY",
                "verdict": "PASS",
                "limit": 25,
            },
        )

    assert response.status_code == 200
    assert response.json()[0]["run_id"] == str(run["run_id"])
    assert list_runs.call_args.kwargs == {
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "runtime_status": "RUNTIME_NOT_READY",
        "verdict": "PASS",
        "limit": 25,
    }


def test_finance_run_detail_and_not_found():
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    run = {
        "run_id": run_id,
        "cycle": "SALES",
        "as_of": date(2026, 8, 21),
        "snapshot_id": None,
        "runtime_status": "RUNTIME_NOT_READY",
        "verdict": None,
        "request_payload": {},
        "response_payload": {},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    with patch("app.finance.router.get_finance_run", return_value=run):
        response = TestClient(app).get(f"/finance/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["verdict"] is None

    with patch("app.finance.router.get_finance_run", side_effect=LookupError):
        response = TestClient(app).get("/finance/runs/00000000-0000-0000-0000-000000000002")
    assert response.status_code == 404


def test_finance_runs_api_rejects_invalid_cycle():
    response = TestClient(app).get("/finance/runs", params={"cycle": "INVALID"})

    assert response.status_code == 422


def test_finance_runs_api_rejects_invalid_verdict():
    response = TestClient(app).get("/finance/runs", params={"verdict": "UNKNOWN"})

    assert response.status_code == 422
