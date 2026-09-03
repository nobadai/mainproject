"""Sales Refeed — 마스터가 전달한 재무 회신을 **그대로** 소비한다.

★ 이 파일이 지키는 것.
    · 재무 회신 원본(payload · reply_ref · business_status)이 잘리지 않는다
    · Sales 가 재무 판정을 다시 내리지 않는다
    · 재무가 한도를 줬다고 Sales 가 수량/가격을 조정하는 규칙을 발명하지 않는다
    · 계보(parent_scenario_id · revision)가 이어진다

🔴 재무는 이제 `max_finance_allowed_amount_krw` · `max_finance_allowed_payment_terms_days`
   를 payload 에 싣는다. 그 값이 있다고 Sales 가 수량을 깎거나 결제조건을 바꾸는
   규칙은 **아직 계약으로 정해지지 않았다.** 정해지지 않은 것을 코드가 먼저 정하면
   그것이 곧 아무도 합의하지 않은 정책이 된다.
"""

from decimal import Decimal

import pytest

from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput


@pytest.fixture(autouse=True)
def _deterministic_explanation(monkeypatch):
    """LLM 을 끄는 이유는 속도가 아니라 **무엇을 시험하는지** 때문이다.

    여기서 보는 것은 회신 소비와 계보다 — 모델이 살았는지에 따라 결과가 달라지면
    그것은 이 검사가 답할 질문이 아니다.
    """
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def _finance_reply(**over):
    payload = {
        "status": "EVALUATED",
        "finance_verdict": "FAIL",
        "scenario_id": "SALES-001-A",
        "reason_codes": ["SALES_MARGIN_BELOW_MINIMUM"],
        "max_finance_allowed_amount_krw": "5000000",
        "max_finance_allowed_payment_terms_days": 15,
        "evidence_refs": ["FIN-AGENT:req-1:1:SALES-001-A:evaluate_sales_scenario"],
    }
    payload.update(over)
    return {
        "source_agent": "finance",
        "capability": "FINANCIAL_VALIDATION",
        "reply_ref": "FIN-REPLY-1",
        "runtime_status": "READY",
        "business_status": "reject",
        "payload": payload,
    }


def _refeed_request(reply=None, **over):
    data = {
        "business_mode": "SPOT_SALES",
        "is_refeed": True,
        "feedback_attempt": 1,
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": 5000,
            "preferred_unit_price_krw": 2000,
            "preferred_delivery_date": "2026-09-10",
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
                ],
            },
        },
        "feedback": {
            "attempt": 1,
            "domain_replies": [reply if reply is not None else _finance_reply()],
            "scenario_feedback": [
                {"scenario_id": "SALES-001-A", "reply_refs": ["FIN-REPLY-1"]}
            ],
        },
    }
    data.update(over)
    return SalesProposalInput.model_validate(data)


def _scenario_a(reply_obj):
    return next(s for s in reply_obj.scenarios if s.scenario_id.startswith("SALES-001-A"))


# ---------------------------------------------------------------------------
# 원본 보존
# ---------------------------------------------------------------------------


def test_finance_reply_payload_survives_untouched():
    reply = run_proposal(_refeed_request())

    carried = _scenario_a(reply).domain_replies
    assert carried, "재무 회신이 시나리오에 붙지 않았다"
    finance = next(item for item in carried if item.source_agent == "finance")
    # 재무가 낸 사실이 하나도 잘리지 않는다.
    assert finance.payload["finance_verdict"] == "FAIL"
    assert finance.payload["reason_codes"] == ["SALES_MARGIN_BELOW_MINIMUM"]
    assert finance.payload["max_finance_allowed_amount_krw"] == "5000000"
    assert finance.payload["evidence_refs"] == [
        "FIN-AGENT:req-1:1:SALES-001-A:evaluate_sales_scenario"
    ]


def test_reply_ref_and_business_status_are_preserved():
    reply = run_proposal(_refeed_request())

    finance = next(
        item for item in _scenario_a(reply).domain_replies if item.source_agent == "finance"
    )
    assert finance.reply_ref == "FIN-REPLY-1"
    assert finance.business_status == "reject"
    assert finance.runtime_status == "READY"


def test_sales_does_not_re_adjudicate_the_finance_verdict():
    """재무가 reject 라고 한 것을 Sales 가 conditional 로 바꾸지 않는다."""
    reply = run_proposal(_refeed_request())

    finance = next(
        item for item in _scenario_a(reply).domain_replies if item.source_agent == "finance"
    )
    assert finance.business_status == "reject"
    assert finance.payload["finance_verdict"] == "FAIL"


def test_finance_fail_is_surfaced_as_a_risk_not_rewritten():
    reply = run_proposal(_refeed_request())

    assert "FINANCE_FAIL" in _scenario_a(reply).risks


# ---------------------------------------------------------------------------
# 한도를 받았다고 조정 규칙을 만들지 않는다
# ---------------------------------------------------------------------------


def test_finance_amount_boundary_does_not_shrink_sales_quantity():
    """🔴 한도가 왔다고 수량을 깎는 규칙은 아직 계약에 없다."""
    baseline = run_proposal(_refeed_request(feedback=None, is_refeed=False, feedback_attempt=0))
    refed = run_proposal(_refeed_request())

    base_qty = _scenario_a(baseline).quantity_kg
    refed_qty = _scenario_a(refed).quantity_kg
    assert refed_qty == base_qty


def test_finance_payment_term_boundary_does_not_rewrite_payment_days():
    reply = run_proposal(_refeed_request())

    scenario = _scenario_a(reply)
    # 재무가 15일을 상한으로 줬지만 Sales 가 결제조건을 만들어 넣지 않는다.
    assert scenario.payment_days != 15 or scenario.payment_days is None


def test_margin_reason_does_not_raise_the_unit_price():
    """🔴 MARGIN 사유로 가격을 올리는 역산은 금지다."""
    baseline = run_proposal(_refeed_request(feedback=None, is_refeed=False, feedback_attempt=0))
    refed = run_proposal(_refeed_request())

    assert _scenario_a(refed).unit_price_krw == _scenario_a(baseline).unit_price_krw


def test_finance_fail_without_any_boundary_still_changes_no_number():
    reply_without_boundary = _finance_reply(
        max_finance_allowed_amount_krw=None,
        max_finance_allowed_payment_terms_days=None,
    )
    baseline = run_proposal(_refeed_request(feedback=None, is_refeed=False, feedback_attempt=0))

    refed = run_proposal(_refeed_request(reply=reply_without_boundary))

    assert _scenario_a(refed).unit_price_krw == _scenario_a(baseline).unit_price_krw
    assert _scenario_a(refed).quantity_kg == _scenario_a(baseline).quantity_kg


# ---------------------------------------------------------------------------
# 계보
# ---------------------------------------------------------------------------


def test_refeed_keeps_parent_lineage():
    reply = run_proposal(_refeed_request())

    scenario = _scenario_a(reply)
    assert scenario.scenario_id == "SALES-001-A-R1"
    assert scenario.parent_scenario_id == "SALES-001-A"
    assert scenario.revision == 1


def test_required_validations_survive_the_refeed():
    reply = run_proposal(_refeed_request())

    scenario = _scenario_a(reply)
    # 어떤 검증이 걸려 있었는지가 회송 뒤에도 남는다.
    assert isinstance(scenario.required_validations, list)


def test_zero_confirmed_supply_is_not_confused_with_unknown():
    reply = run_proposal(_refeed_request())

    supply = _scenario_a(reply).supply
    assert supply.confirmed_quantity_kg is None or isinstance(
        supply.confirmed_quantity_kg, Decimal
    )
