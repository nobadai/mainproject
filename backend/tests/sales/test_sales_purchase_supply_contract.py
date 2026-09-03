"""Sales 가 Purchase 추가공급 회신을 받을 때의 소비 경계.

★ 이 파일이 지키는 것은 **조용히 정상 처리하지 않는 것**이다.
    · 출처만 맞고 capability 가 다르면 공급 결과로 읽지 않는다
    · 키가 없는 것과 명시적 null 이 같아지지 않는다
    · 0kg 은 사실이고 미실행은 모름이다
    · `risks` 칸이 없는 회신을 "위험 0건" 으로 읽지 않는다
    · Purchase 회신이 붙어 있다는 사실 자체를 누수로 오판하지 않는다
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.sales.proposal import run_proposal
from app.sales.schemas import PurchaseAdditionalSupplyResult, SalesProposalInput


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


_LOGISTICS = {
    "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
    "sellable_supply": {
        "status": "READY",
        "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
        "supply_capacity_by_date": [
            {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
        ],
    },
    "delivery_feasibility": {
        "status": "READY",
        "daily_outbound_capacity_kg": 5000,
        "reason_codes": [],
    },
}


def _reply(
    *,
    capability="ADDITIONAL_SUPPLY_CONTEXT",
    runtime="READY",
    business="ok",
    payload=None,
    ref="PUR-1",
):
    return {
        "source_agent": "purchase",
        "capability": capability,
        "reply_ref": ref,
        "runtime_status": runtime,
        "business_status": business,
        "payload": {"procurable_quantity_kg": 2000, "risks": []}
        if payload is None
        else payload,
    }


def _request(replies, scenario_id="SALES-001-C"):
    return SalesProposalInput.model_validate(
        {
            "business_mode": "SPOT_SALES",
            "is_refeed": True,
            "feedback_attempt": 1,
            "user_request": {
                "item": "배추",
                "requested_quantity_kg": 5000,
                "preferred_unit_price_krw": 2000,
                "preferred_delivery_date": "2026-09-10",
            },
            "logistics_context": _LOGISTICS,
            "feedback": {
                "attempt": 1,
                "domain_replies": replies,
                "scenario_feedback": [
                    {
                        "scenario_id": scenario_id,
                        "reply_refs": [item["reply_ref"] for item in replies],
                    }
                ],
            },
        }
    )


def _aggressive(reply):
    return reply.scenarios[2]


# ---------------------------------------------------------------------------
# typed 수신 모델 — 칸은 필수, 값은 nullable
# ---------------------------------------------------------------------------


def test_explicit_null_quantity_is_valid():
    parsed = PurchaseAdditionalSupplyResult.model_validate(
        {"procurable_quantity_kg": None, "risks": []}
    )

    assert parsed.procurable_quantity_kg is None
    assert parsed.risks == []


def test_missing_quantity_key_is_invalid():
    """🔴 키 없음과 명시적 null 은 다른 사실이다."""
    with pytest.raises(ValidationError):
        PurchaseAdditionalSupplyResult.model_validate({"risks": []})


def test_missing_risks_key_is_invalid():
    """`risks` 를 안 보낸 것을 '위험 0건' 으로 읽지 않는다."""
    with pytest.raises(ValidationError):
        PurchaseAdditionalSupplyResult.model_validate({"procurable_quantity_kg": 0})


def test_zero_quantity_is_a_fact():
    parsed = PurchaseAdditionalSupplyResult.model_validate(
        {"procurable_quantity_kg": 0, "risks": []}
    )

    assert parsed.procurable_quantity_kg == Decimal(0)


def test_boolean_quantity_is_rejected():
    with pytest.raises(ValidationError):
        PurchaseAdditionalSupplyResult.model_validate(
            {"procurable_quantity_kg": True, "risks": []}
        )


def test_extra_purchase_fields_are_tolerated():
    """Purchase 가 더 실어 보내도 Sales 가 쓰는 칸만 있으면 읽는다."""
    parsed = PurchaseAdditionalSupplyResult.model_validate(
        {
            "procurable_quantity_kg": 25,
            "risks": ["창고 점유 검사 보류"],
            "fulfillable": True,
            "expected_arrival_date": None,
        }
    )

    assert parsed.procurable_quantity_kg == Decimal(25)
    assert parsed.risks == ["창고 점유 검사 보류"]


# ---------------------------------------------------------------------------
# 정상 소비
# ---------------------------------------------------------------------------


def test_positive_quantity_becomes_conditional_supply_with_lineage():
    reply = run_proposal(
        _request([_reply(payload={"procurable_quantity_kg": 25, "risks": ["R1"]})])
    )
    scenario = _aggressive(reply)

    assert scenario.supply.conditional_quantity_kg == Decimal(25)
    assert scenario.supply.dependency_ref == "PUR-1"
    assert scenario.conditional_purchase is True
    assert "R1" in scenario.risks


def test_zero_quantity_is_preserved_and_not_conditional():
    reply = run_proposal(
        _request([_reply(payload={"procurable_quantity_kg": 0, "risks": []})])
    )
    scenario = _aggressive(reply)

    assert scenario.supply.conditional_quantity_kg == Decimal(0)
    assert scenario.conditional_purchase is False


def test_runtime_not_ready_keeps_the_quantity_unknown():
    reply = run_proposal(
        _request(
            [
                _reply(
                    runtime="RUNTIME_NOT_READY",
                    payload={"procurable_quantity_kg": 2000, "risks": []},
                )
            ]
        )
    )
    scenario = _aggressive(reply)

    # 못 돈 회신의 숫자를 사실로 삼지 않고, 0 으로도 바꾸지 않는다.
    assert scenario.supply.conditional_quantity_kg is None
    assert scenario.conditional_purchase is False


def test_explicit_null_quantity_stays_unknown():
    reply = run_proposal(
        _request([_reply(payload={"procurable_quantity_kg": None, "risks": []})])
    )

    assert _aggressive(reply).supply.conditional_quantity_kg is None


# ---------------------------------------------------------------------------
# scenario 분배 / reference leak
# ---------------------------------------------------------------------------


def test_replies_do_not_bleed_between_scenarios():
    request = SalesProposalInput.model_validate(
        {
            "business_mode": "SPOT_SALES",
            "is_refeed": True,
            "feedback_attempt": 1,
            "user_request": {
                "item": "배추",
                "requested_quantity_kg": 5000,
                "preferred_unit_price_krw": 2000,
                "preferred_delivery_date": "2026-09-10",
            },
            "logistics_context": _LOGISTICS,
            "feedback": {
                "attempt": 1,
                "domain_replies": [
                    _reply(ref="PUR-C", payload={"procurable_quantity_kg": 25, "risks": []})
                ],
                "scenario_feedback": [
                    {"scenario_id": "SALES-001-C", "reply_refs": ["PUR-C"]}
                ],
            },
        }
    )

    reply = run_proposal(request)

    # C 안에만 붙는다.
    assert _aggressive(reply).supply.dependency_ref == "PUR-C"
    for scenario in reply.scenarios[:2]:
        assert scenario.supply.dependency_ref is None
        assert not [r for r in scenario.domain_replies if r.source_agent == "purchase"]


# ---------------------------------------------------------------------------
# 미래 Purchase 필드 — 수신 부분집합 계약
# ---------------------------------------------------------------------------


def test_future_purchase_fields_do_not_invalidate_the_reply():
    """🔴 매입 DTO 의 정본은 매입이 소유한다.

    Sales 가 안 쓰는 칸이 더 왔다고 회신 전체를 무효로 만들면, 남의 계약을 Sales 가
    소유하는 셈이 되어 매입이 칸을 늘릴 때마다 판매가 깨진다.
    """
    reply = run_proposal(
        _request(
            [
                _reply(
                    payload={
                        "procurable_quantity_kg": 25,
                        "risks": ["R1"],
                        "expected_arrival_date": "2026-09-20",
                        "binding_constraint": "WAREHOUSE",
                        "fulfillable": True,
                        "future_purchase_field": "opaque",
                    }
                )
            ]
        )
    )
    scenario = _aggressive(reply)

    assert scenario.supply.conditional_quantity_kg == Decimal(25)
    assert scenario.conditional_purchase is True
    assert "R1" in scenario.risks


def test_required_keys_are_still_enforced_alongside_extra_fields():
    with pytest.raises(ValidationError):
        PurchaseAdditionalSupplyResult.model_validate(
            {"risks": [], "future_purchase_field": "opaque"}
        )


def test_wrong_type_on_a_required_key_is_invalid():
    with pytest.raises(ValidationError):
        PurchaseAdditionalSupplyResult.model_validate(
            {"procurable_quantity_kg": 25, "risks": "not-a-list"}
        )


def test_original_payload_is_preserved_on_the_reply():
    """부분집합만 검증해도 원본은 잃지 않는다."""
    reply = run_proposal(
        _request(
            [_reply(payload={"procurable_quantity_kg": 25, "risks": [], "extra": "x"})]
        )
    )
    carried = next(
        item
        for item in _aggressive(reply).domain_replies
        if item.source_agent == "purchase"
    )

    assert carried.payload["extra"] == "x"


# ---------------------------------------------------------------------------
# risks 는 개수를 고정하지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 7])
def test_risk_count_never_drives_the_conditional_verdict(count):
    risks = [f"R{index}" for index in range(count)]

    reply = run_proposal(
        _request([_reply(payload={"procurable_quantity_kg": 25, "risks": risks})])
    )
    scenario = _aggressive(reply)

    # 조건부 여부는 수량이 정한다 — 위험 개수가 아니다.
    assert scenario.conditional_purchase is True
    for risk in risks:
        assert risk in scenario.risks


def test_risks_are_carried_verbatim_without_reinterpretation():
    raw = "입고일 기준 창고 점유 검사 보류 — 물류 입고 소요일이 미확정입니다"

    reply = run_proposal(
        _request([_reply(payload={"procurable_quantity_kg": 25, "risks": [raw]})])
    )

    assert raw in _aggressive(reply).risks
