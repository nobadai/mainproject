"""Critic API 테스트 - /critic/procurement · /critic/sales.

psycopg 의존을 피하려 app.main 대신 critic 라우터만 격리해 올린다
(finance/logistics 라우터가 DB 드라이버를 끌어오기 때문).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.critic.router import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)


def _ev(claim, ref, value, unit="kg"):
    return {
        "claim": claim,
        "ref_ids": [ref],
        "value": value,
        "unit": unit,
        "evidence_grade": "OFFICIAL",
    }


def _base_replies():
    return [
        {
            "dept": "sales",
            "checks": [
                {
                    "check_id": "floor-b",
                    "floor_kg": {"배추": 100.0},
                    "evidences": [_ev("배추 하한", "SRC-S-1", 100.0)],
                }
            ],
        },
        {
            "dept": "inventory",
            "checks": [
                {
                    "check_id": "cap-inv",
                    "cap_kg": {"배추": 5000.0, "무": 3000.0},
                    "cap_total_kg": 7000.0,
                    "evidences": [_ev("창고 상한", "SRC-I-1", 7000.0)],
                }
            ],
        },
        {
            "dept": "finance",
            "checks": [
                {
                    "check_id": "cap-fin",
                    "cap_amount_krw": 20_000_000.0,
                    "evidences": [_ev("가용자금", "SRC-F-1", 20_000_000.0, "krw")],
                }
            ],
        },
    ]


def _scenarios():
    return [
        {
            "scenario_id": "SCN-1",
            "qty_kg": {"배추": 2000.0, "무": 1000.0},
            "unit_price_krw_per_kg": {"배추": 2000.0, "무": 1500.0},
        }
    ]


def _clean_request():
    return {
        "as_of": "2026-08-24",
        "items": ["배추", "무"],
        "scenarios": _scenarios(),
        "replies": _base_replies(),
        "dept_meta": {
            "finance": {
                "inputs_used": {"cap-fin": ["cash_balance"]},
                "produced_fields": ["cap_amount_krw"],
            }
        },
    }


def test_procurement_clean_no_findings():
    """근거를 갖춘 정상 회신은 발견(findings)이 없다. L0~L4 가 실행된다."""
    r = client.post("/critic/procurement", json=_clean_request())
    assert r.status_code == 200
    body = r.json()
    assert body["findings"] == []
    assert body["status"] in ("PASS", "CONCERN")  # 단일 후보면 붕괴 CONCERN
    assert body["coverage"]["L0"] == [6, 6]
    assert body["runtime_status"] == "READY"


def test_procurement_grade_leak_fail():
    """재무 cap 산출에 등급 개입(grade_unit_price) → E-GRADE-LEAK FAIL."""
    req = _clean_request()
    req["dept_meta"] = {
        "finance": {
            "inputs_used": {"cap-fin": ["cash_balance", "grade_unit_price"]},
            "produced_fields": ["cap_amount_krw"],
        }
    }
    body = client.post("/critic/procurement", json=req).json()
    assert body["status"] == "FAIL"
    assert any(f["check_id"] == "E-GRADE-LEAK" for f in body["findings"])
    assert body["end_stage"] == "CRITIC_A"


def test_procurement_authority_fail():
    """영업이 has_unmet_obligation 을 산출 → E-AUTHORITY FAIL (S3 전속 침범)."""
    req = _clean_request()
    req["dept_meta"] = {"sales": {"produced_fields": ["floor_kg", "has_unmet_obligation"]}}
    body = client.post("/critic/procurement", json=req).json()
    assert body["status"] == "FAIL"
    assert any(f["check_id"] == "E-AUTHORITY" for f in body["findings"])


def test_procurement_contract_violation_422():
    """영업이 cap_kg 를 채우면 계약이 막고 422."""
    bad = {
        "as_of": "2026-08-24",
        "items": ["배추"],
        "scenarios": _scenarios(),
        "replies": [
            {
                "dept": "sales",
                "checks": [
                    {"check_id": "bad", "cap_kg": {"배추": 1.0}, "evidences": [_ev("x", "R", 1.0)]}
                ],
            }
        ],
    }
    assert client.post("/critic/procurement", json=bad).status_code == 422


def test_procurement_bad_strategy_type_422():
    """strategy_type 은 Literal - 오타는 pydantic 422."""
    req = _clean_request()
    req["scenarios"][0]["strategy_type"] = "price"
    assert client.post("/critic/procurement", json=req).status_code == 422


def _sales_request(**over):
    base = {
        "as_of": "2026-08-24",
        "items": ["배추"],
        "replies": [
            {
                "dept": "inventory",
                "checks": [
                    {"check_id": "s-cap", "cap_kg": {"배추": 3000.0}, "cap_total_kg": 5000.0}
                ],
            }
        ],
        "allocations": [
            {
                "allocation_id": "ALLOC-1",
                "legs": [
                    {
                        "channel": "KIMCHI_FACTORY",
                        "item": "배추",
                        "qty_kg": 2000.0,
                        "unit_price_krw_per_kg": 3000.0,
                        "lot_ids": ["L1"],
                        "due_date": "2026-08-30",
                    }
                ],
            }
        ],
        "lot_constraints": [
            {
                "lot_id": "L1",
                "item": "배추",
                "available_qty_kg": 3000.0,
                "remaining_freshness_days": 30,
            }
        ],
    }
    base.update(over)
    return base


def test_sales_clean_pass():
    """근거를 갖춘 정상 배분 - L4-8/9/10 통과. 약정 없으면 L4-7 만 skipped."""
    body = client.post("/critic/sales", json=_sales_request()).json()
    assert body["cycle"] == "B"
    assert body["status"] == "PASS"
    assert body["coverage"]["L4_B"] == [3, 4]  # L4-7 은 약정 없어 skipped
    assert any("L4-7" in s for s in body["skipped"])


def test_sales_onhand_exceed_fail():
    """L4-9 - 출고 2000kg > 가용 로트 1000kg → on_hand 초과 FAIL."""
    req = _sales_request(
        lot_constraints=[
            {
                "lot_id": "L1",
                "item": "배추",
                "available_qty_kg": 1000.0,
                "remaining_freshness_days": 30,
            }
        ]
    )
    body = client.post("/critic/sales", json=req).json()
    assert body["status"] == "FAIL"
    assert any("onhand" in f["check_id"] for f in body["findings"])
    assert body["end_stage"] == "CRITIC_B"


def test_sales_freshness_fail():
    """L4-10 - 로트 잔여 3일 < 납기 6일 → 신선도 FAIL."""
    req = _sales_request(
        lot_constraints=[
            {
                "lot_id": "L1",
                "item": "배추",
                "available_qty_kg": 3000.0,
                "remaining_freshness_days": 3,
            }
        ]
    )
    body = client.post("/critic/sales", json=req).json()
    assert body["status"] == "FAIL"
    assert any("freshness" in f["check_id"] for f in body["findings"])


def test_sales_overlay_cap_by_date_fail():
    """L4-7 - H1 승인분 overlay 후 특정 날짜 창고 점유 초과 → FAIL. 4개 검사 전부 실행."""
    req = _sales_request(
        warehouse_free_kg=1000.0,
        confirmed_occupancy_by_date={"2026-08-26": 5000.0},
        commitment={
            "approval_id": "H1-1",
            "arrival_schedule": [{"date": "2026-08-26", "qty_kg": 3000.0, "split_index": 1}],
        },
    )
    body = client.post("/critic/sales", json=req).json()
    assert body["status"] == "FAIL"
    assert any("overlay" in f["check_id"] for f in body["findings"])
    assert body["coverage"]["L4_B"] == [4, 4]  # 약정·점유 제공 → L4-7 도 실행


def test_sales_authority_fail():
    """재무가 has_unmet_obligation 을 산출 → E-AUTHORITY (S3 전속 침범)."""
    req = _sales_request(
        dept_meta={"finance": {"produced_fields": ["cap_amount_krw", "has_unmet_obligation"]}}
    )
    body = client.post("/critic/sales", json=req).json()
    assert body["status"] == "FAIL"
    assert any(f["check_id"] == "E-AUTHORITY" for f in body["findings"])
