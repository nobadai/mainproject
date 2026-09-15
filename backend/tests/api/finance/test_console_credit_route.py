"""재무 콘솔 여신 현황 경로 — **실행 축과 기준일 없이는 부르지 않는다.**"""

from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.finance import console_routes
from app.finance.console_credit import ConsoleCreditResponse, ConsolePartnerCredit


def _client(monkeypatch, captured: dict) -> TestClient:
    def fake(*, sim_run_id: str, as_of: date) -> ConsoleCreditResponse:
        captured.update(sim_run_id=sim_run_id, as_of=as_of)
        return ConsoleCreditResponse(
            sim_run_id=sim_run_id,
            as_of=as_of,
            partners=[
                ConsolePartnerCredit(
                    partner_id="KIMCHI_FACTORY_001",
                    partner_name="김치제조공장",
                    payment_days=7,
                    credit_limit_krw=Decimal(10_000_000),
                    credit_limit_evidence_grade="SIM_FIXED",
                    current_ar_krw=Decimal(6_500_000),
                    overdue_ar_krw=Decimal(0),
                    open_receivable_count=2,
                    available_credit_krw=Decimal(3_500_000),
                    credit_utilization_rate=Decimal("0.65"),
                    upcoming_collections=[],
                )
            ],
        )

    monkeypatch.setattr(console_routes, "get_console_credit", fake)
    app = FastAPI()
    app.include_router(console_routes.router)
    return TestClient(app)


def test_credit_route_passes_the_run_axis_and_day_through(monkeypatch):
    captured: dict = {}
    response = _client(monkeypatch, captured).get(
        "/console/finance/credit", params={"sim_run_id": "SIM-T", "as_of": "2026-01-08"}
    )

    assert response.status_code == 200
    assert captured == {"sim_run_id": "SIM-T", "as_of": date(2026, 1, 8)}
    body = response.json()["partners"][0]
    assert body["payment_days"] == 7
    assert Decimal(body["available_credit_krw"]) == Decimal(3_500_000)


def test_credit_route_refuses_a_missing_run_axis(monkeypatch):
    response = _client(monkeypatch, {}).get(
        "/console/finance/credit", params={"as_of": "2026-01-08"}
    )

    assert response.status_code == 422
