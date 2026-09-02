"""영업 Agent API 테스트.

입력·출력은 팀 공통 I/O 계약(캐논) 구조를 따른다. 다만 미구현 결과는 정상 업무 결과처럼
채우지 않고 null·빈 목록·명시적 미구현 상태로 낸다.
실행이력 저장(save_sales_agent_run)은 monkeypatch로 대체해 PostgreSQL 없이
POST 계산·응답과 저장 호출 인자를 검증한다.
"""

from datetime import UTC, date, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _fake_run(**overrides) -> dict:
    row = {
        "run_id": uuid4(),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": "T0-20260821-01",
        "runtime_status": "READY",
        "request_payload": {"item": "배추"},
        "response_payload": {"agent": "sales"},
        "created_at": datetime(2026, 8, 21, 9, 0, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _snapshot_a(**overrides) -> dict:
    snapshot = {
        "snapshot_id": "T0-20260821-01",
        "as_of": "2026-08-21",
        "item": "배추",
        "policy_version": None,
        "inbound_lead_days": 4,
        "confirmed_orders": [
            {"order_id": "ORD-1", "delivery_date": "2026-08-23", "qty_kg": 200},
            {"order_id": "ORD-2", "delivery_date": "2026-08-25", "qty_kg": 1500},
        ],
        "inventory": {
            "on_hand": [
                {"lot_id": "LOT-A", "qty_kg": 600, "freshness_days_left": 8},
                {"lot_id": "LOT-B", "qty_kg": 100, "freshness_days_left": 2},
            ],
            "in_transit": [],
        },
    }
    snapshot.update(overrides)
    return {"cycle": "PROCUREMENT", "as_of": "2026-08-21", "sales_snapshot": snapshot}


def _snapshot_b(**overrides) -> dict:
    snapshot = {
        "snapshot_id": "T0-20260821-01",
        "as_of": "2026-08-21",
        "item": "배추",
        "policy_version": None,
        "cost_basis": None,
        "confirmed_orders": [
            {"order_id": "ORD-1", "delivery_date": "2026-08-23", "qty_kg": 200},
        ],
        "sales_opportunities": None,
        "inventory": {
            "on_hand": [
                {
                    "lot_id": "LOT-A",
                    "qty_kg": 600,
                    "freshness_days_left": 8,
                    "reserved_for_confirmed_kg": 200,
                },
                {"lot_id": "LOT-B", "qty_kg": 100, "freshness_days_left": 2},
            ],
            "in_transit": [],
        },
    }
    snapshot.update(overrides)
    return {
        "cycle": "SALES",
        "as_of": "2026-08-21",
        "approved_purchase": {"approval_id": "H1-20260821-001"},
        "sales_snapshot": snapshot,
    }


def test_procurement_computes_floor_and_saves(monkeypatch):
    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return kwargs

    monkeypatch.setattr("app.sales.service.save_sales_agent_run", fake_save)

    response = client.post("/sales/procurement", json=_snapshot_a())

    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "sales"
    assert body["band"]["today_floor_kg"] == "1100"
    assert body["band"]["binding_delivery_date"] == "2026-08-25"
    assert body["suggested_adjustment"]["min_qty_kg"] == "1100"
    # 자기 회신 상태는 아직 평가하지 못하므로 verdict는 null이다
    assert body["verdict"] is None
    # 하드 제약 3종은 모두 미구현으로 표시된다(가짜 통과 없음)
    assert {c["code"] for c in body["hard_constraints"]} == {
        "CONFIRMED_DEMAND_TOTAL",
        "DELIVERY_DEADLINE",
        "DAILY_OUTBOUND_CAPACITY",
    }
    assert all(c["passed"] is None for c in body["hard_constraints"])
    assert all("NOT_IMPLEMENTED" in c["skip_reason"] for c in body["hard_constraints"])
    # 소프트 경고 판정이 미구현이라 빈 목록(가짜 경고 없음)
    assert body["soft_warnings"] == []
    assert saved["cycle"] == "PROCUREMENT"
    assert saved["snapshot_id"] == "T0-20260821-01"
    assert saved["runtime_status"] == "READY"


def test_procurement_not_ready_keeps_null_not_zero(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "app.sales.service.save_sales_agent_run",
        lambda **kwargs: saved.update(kwargs) or kwargs,
    )

    payload = _snapshot_a()
    del payload["sales_snapshot"]["inbound_lead_days"]

    response = client.post("/sales/procurement", json=payload)

    assert response.status_code == 200
    body = response.json()
    # 미결은 0이 아니라 null로 나타난다(0 != null 원칙)
    assert body["band"]["today_floor_kg"] is None
    assert body["band"]["binding_delivery_date"] is None
    assert body["suggested_adjustment"]["min_qty_kg"] is None
    # runtime_status는 실행이력 DB 컬럼용으로만 내부 계산된다
    assert saved["runtime_status"] == "RUNTIME_NOT_READY"


def test_allocation_reports_facts_without_fake_candidates(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "app.sales.service.save_sales_agent_run",
        lambda **kwargs: saved.update(kwargs) or kwargs,
    )

    response = client.post("/sales/allocation", json=_snapshot_b())

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "S1"
    assert body["meta"]["item"] == "배추"
    assert body["meta"]["approval_id"] == "H1-20260821-001"
    # 확정 의무량은 실제 집계값, 후보와 충당량은 미구현이라 빈 목록·null·명시적 사유
    assert body["confirmed_obligation_kg"] == "200"
    assert body["candidates"] == []
    assert body["coverable_kg"] is None
    assert body["no_feasible_reason"] == "CANDIDATE_EVIDENCE_MISSING"
    assert saved["cycle"] == "SALES"
    # 후보 근거가 없어 실행이력도 READY로 과장하지 않는다
    assert saved["runtime_status"] == "RUNTIME_NOT_READY"


def test_allocation_builds_deduplicated_evidenced_candidates_and_accepts_context(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(
        sales_opportunities=[
            {"opportunity_id": "OP-1", "channel": "B2B", "qty_kg": 100, "unit_price": 2000,
             "delivery_date": "2026-08-24", "payment_days": 30, "evidence_ref": "CON-1"},
            {"opportunity_id": "OP-dup", "channel": "B2B", "qty_kg": 100, "unit_price": 2000,
             "delivery_date": "2026-08-24", "payment_days": 30, "evidence_ref": "CON-2"},
        ]
    )
    payload["initial_context"] = {
        "finance": {"reference": "FIN-1", "cash_status": "WATCH"},
        "logistics": {"reference": "LOG-1", "sellable_status": "KNOWN"},
    }
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["allocation"][0]["unit_price"] == "2000"
    assert len(body["missing_capabilities"]) == 2


def test_allocation_refeed_applies_authoritative_constraints(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(sales_opportunities=[
        {"opportunity_id": "OP-1", "channel": "B2B", "qty_kg": 10000, "unit_price": 2000,
         "delivery_date": "2026-08-24", "payment_days": 30, "evidence_ref": "CON-1"},
    ])
    payload["refeed_results"] = [
        {"candidate_id": "OP-1", "source": "LOGISTICS", "verdict": "PASS", "max_qty_kg": 8000,
         "ref_id": "LOG-RESULT"},
        {"candidate_id": "OP-1", "source": "FINANCE", "verdict": "PASS", "max_payment_days": 15,
         "ref_id": "FIN-RESULT"},
        {"candidate_id": "OP-1", "source": "PURCHASE", "verdict": "PASS", "conditional": True,
         "ref_id": "PUR-RESULT"},
    ]
    body = client.post("/sales/allocation", json=payload).json()
    candidate = body["candidates"][0]
    assert candidate["allocation"][0]["qty_kg"] == "8000"
    assert candidate["payment_days"] == 15
    assert candidate["adjustment_axis"] == "MIX"
    assert candidate["conditional"] is True


def test_finance_fail_is_not_promoted_to_pass(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(sales_opportunities=[
        {"opportunity_id": "OP-1", "channel": "B2B", "qty_kg": 1, "unit_price": 2,
         "delivery_date": "2026-08-24", "evidence_ref": "CON-1"},
    ])
    payload["refeed_results"] = [{"candidate_id": "OP-1", "source": "FINANCE", "verdict": "FAIL"}]
    candidate = client.post("/sales/allocation", json=payload).json()["candidates"][0]
    assert "FINANCE_FAIL" in candidate["risks"]


def test_allocation_does_not_invent_missing_commercial_numbers(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(sales_opportunities=[
        {"opportunity_id": "NO-PRICE", "channel": "B2B", "qty_kg": 0,
         "delivery_date": "2026-08-24", "evidence_ref": "LEAD-1"},
        {"opportunity_id": "NO-QTY", "channel": "B2B", "unit_price": 2,
         "delivery_date": "2026-08-24", "evidence_ref": "LEAD-2"},
    ])
    body = client.post("/sales/allocation", json=payload).json()
    assert body["candidates"] == []
    assert body["no_feasible_reason"] == "PRICE_EVIDENCE_MISSING"
    assert body["no_feasible_message"] == (
        "현재 정보만으로 판매가격을 확정하기 어렵습니다. "
        "계약가, 기존 거래가격 또는 시장가격 정보가 필요합니다."
    )


def test_allocation_limits_candidates_to_three_and_self_checks(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(sales_opportunities=[
        {"opportunity_id": f"OP-{number}", "channel": f"B2B-{number}", "qty_kg": number,
         "unit_price": number, "delivery_date": "2026-08-24", "evidence_ref": f"CON-{number}"}
        for number in range(1, 5)
    ])
    body = client.post("/sales/allocation", json=payload).json()
    assert len(body["candidates"]) == 3
    assert body["self_check"] == {"passed": True, "issue_codes": [], "messages": []}


def test_situation_is_represented_without_activating_spot_sales(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(sales_opportunities=[
        {"opportunity_id": "OP-1", "channel": "B2B", "qty_kg": 1, "unit_price": 2,
         "delivery_date": "2026-08-24", "evidence_ref": "CON-1"},
    ])
    body = client.post("/sales/allocation", json=payload).json()
    assert body["business_mode"] is None
    assert body["situation"] == "CONTRACT_FULFILLMENT"


def test_conditional_purchase_creates_separate_delivery_scenario(monkeypatch):
    monkeypatch.setattr("app.sales.service.save_sales_agent_run", lambda **kwargs: kwargs)
    payload = _snapshot_b(sales_opportunities=[
        {"opportunity_id": "OP-1", "channel": "B2B", "qty_kg": 10000, "unit_price": 2300,
         "delivery_date": "2026-09-10", "payment_days": 30, "evidence_ref": "USER-1"},
    ])
    payload["refeed_results"] = [
        {"candidate_id": "OP-1", "source": "LOGISTICS", "verdict": "PASS", "max_qty_kg": 8000},
        {"candidate_id": "OP-1", "source": "FINANCE", "verdict": "PASS", "max_payment_days": 15},
        {"candidate_id": "OP-1", "source": "PURCHASE", "verdict": "PASS", "conditional": True,
         "additional_qty_kg": 2000, "available_date": "2026-09-13"},
    ]
    candidates = client.post("/sales/allocation", json=payload).json()["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["adjustment_axis"] == "MIX"
    assert candidates[0]["allocation"][0]["qty_kg"] == "8000"
    assert candidates[1]["conditional"] is True
    assert candidates[1]["outbound_by_date"][0]["date"] == "2026-09-13"




def test_as_of_must_match_snapshot():
    payload = _snapshot_a()
    payload["as_of"] = "2026-08-22"  # 바깥 as_of와 스냅샷 as_of 불일치
    response = client.post("/sales/procurement", json=payload)
    assert response.status_code == 422


def test_allocation_requires_canon_key():
    payload = _snapshot_b()
    del payload["sales_snapshot"]["cost_basis"]  # 캐논 필수 키 누락
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_procurement_rejects_unknown_field():
    payload = _snapshot_a()
    payload["unexpected"] = "x"
    response = client.post("/sales/procurement", json=payload)
    assert response.status_code == 422


def test_in_transit_requires_arrival_date():
    payload = _snapshot_b(
        inventory={
            "on_hand": [{"lot_id": "A", "qty_kg": 600, "freshness_days_left": 8}],
            "in_transit": [{"lot_id": "C", "qty_kg": 300}],
        }
    )
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_in_transit_key_is_required():
    payload = _snapshot_b(
        inventory={"on_hand": [{"lot_id": "A", "qty_kg": 600, "freshness_days_left": 8}]}
    )
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_reserved_over_quantity_is_rejected():
    payload = _snapshot_b(
        inventory={
            "on_hand": [
                {
                    "lot_id": "A",
                    "qty_kg": 100,
                    "freshness_days_left": 8,
                    "reserved_for_confirmed_kg": 200,
                }
            ],
            "in_transit": [],
        }
    )
    response = client.post("/sales/allocation", json=payload)
    assert response.status_code == 422


def test_list_runs_applies_filter(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return [_fake_run()]

    monkeypatch.setattr("app.sales.service.list_sales_agent_runs", fake_list)

    response = client.get("/sales/runs", params={"cycle": "PROCUREMENT", "limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["cycle"] == "PROCUREMENT"
    assert captured["cycle"] == "PROCUREMENT"
    assert captured["limit"] == 10


def test_get_run_single(monkeypatch):
    run = _fake_run()
    monkeypatch.setattr("app.sales.service.get_sales_agent_run", lambda run_id: run)

    response = client.get(f"/sales/runs/{run['run_id']}")

    assert response.status_code == 200
    assert response.json()["run_id"] == str(run["run_id"])


def test_get_run_missing_is_404(monkeypatch):
    def fake_get(run_id):
        raise LookupError("not found")

    monkeypatch.setattr("app.sales.service.get_sales_agent_run", fake_get)

    response = client.get(f"/sales/runs/{uuid4()}")
    assert response.status_code == 404


def test_list_runs_limit_out_of_range_is_422():
    response = client.get("/sales/runs", params={"limit": 999})
    assert response.status_code == 422
