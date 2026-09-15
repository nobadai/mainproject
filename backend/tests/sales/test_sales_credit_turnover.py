"""판매가 **거래처 계약 결제일수**로 안을 세우고, 재무가 센 여신 사실을 그대로 나르는가.

★ 이 파일이 지키는 것:

```text
요청이 결제일수를 말하지 않으면 거래처 계약 결제일수(partners.sales_collection_days)가 실린다
사람·걷기 규칙·계약이 정한 결제일수는 거래처 계약값이 덮지 않는다
7일 결제로 선 안은 확정 뒤 만기일이 판매일 + 7일이다
재무 회신의 여신 사실(미수 · 한도 · 남은 여신 · 선회수 필요액 · 회복일)을 판매가 세지 않고 옮긴다
재무 FAIL 을 이유로 판매가 가격이나 수량을 바꾸지 않는다
금일 판매안 화면 읽기 모델이 그 사실을 0 과 NULL 을 갈라 내보낸다
```
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.sales import adapter, console_proposals
from app.sales.adapter import with_partner_payment_days
from app.sales.persistence import build_sale_confirmation_plan
from app.sales.proposal import run_proposal
from app.sales.schemas import SalesConfirmationInput, SalesProposalInput

PARTNER = "KIMCHI_FACTORY_001"


def _payload(
    *, payment_days: Any = None, mode: str = "SPOT_SALES", contract=None
) -> dict[str, Any]:
    user: dict[str, Any] = {
        "item": "배추",
        "partner_id": PARTNER,
        "requested_quantity_kg": "2000",
        "preferred_unit_price_krw": "1500",
        "preferred_delivery_date": "2026-01-15",
        "preferred_payment_terms_type": "SINGLE",
        "source_ref": "CONSOLE-SALES-REQUEST:2026-01-07:KIMCHI_FACTORY_001:배추",
    }
    if payment_days is not None:
        user["preferred_payment_days"] = payment_days
    data: dict[str, Any] = {
        "business_mode": mode,
        "user_request": user,
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": "2000"},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": "2000"}],
                "supply_capacity_by_date": [
                    {"date": "2026-01-15", "confirmed_sellable_quantity_kg": "2000"}
                ],
            },
            "delivery_feasibility": {"status": "READY", "daily_outbound_capacity_kg": "2000"},
        },
    }
    if contract is not None:
        data["contract_context"] = contract
    return data


def _seven(_partner_id: str) -> int:
    return 7


# ── 결제일수 정본 ─────────────────────────────────────────────────────────


def test_an_unspecified_payment_term_takes_the_partner_contract():
    filled = with_partner_payment_days(_payload(), lookup=_seven)

    assert filled["user_request"]["preferred_payment_days"] == 7


@pytest.mark.parametrize("stated", [30, 0])
def test_a_stated_payment_term_is_not_overridden_by_the_partner_contract(stated):
    """사람·걷기 규칙이 말한 값이 이긴다. **0일(당일 결제)도 말한 값이다.**"""
    filled = with_partner_payment_days(_payload(payment_days=stated), lookup=_seven)

    assert filled["user_request"]["preferred_payment_days"] == stated


def test_contract_fulfillment_keeps_the_contract_payment_term():
    data = _payload(
        mode="CONTRACT_FULFILLMENT", contract={"partner_id": PARTNER, "contract_payment_days": 30}
    )

    assert (
        with_partner_payment_days(data, lookup=_seven)["user_request"].get("preferred_payment_days")
        is None
    )


def test_renewal_inherits_the_previous_contract_term_before_the_partner_default():
    inherited = _payload(
        mode="CONTRACT_PROPOSAL_RENEWAL",
        contract={"partner_id": PARTNER, "contract_payment_days": 14},
    )
    open_term = _payload(mode="CONTRACT_PROPOSAL_RENEWAL", contract={"partner_id": PARTNER})

    assert (
        with_partner_payment_days(inherited, lookup=_seven)["user_request"].get(
            "preferred_payment_days"
        )
        is None
    )
    assert (
        with_partner_payment_days(open_term, lookup=_seven)["user_request"][
            "preferred_payment_days"
        ]
        == 7
    )


def test_an_unreadable_partner_contract_is_not_replaced_with_a_default():
    """못 읽은 계약을 30 이나 0 으로 메우지 않는다 — 재무가 «결제일수 없음» 으로 닫는다."""
    filled = with_partner_payment_days(_payload(), lookup=lambda _pid: None)
    anonymous = _payload()
    anonymous["user_request"].pop("partner_id")

    assert "preferred_payment_days" not in filled["user_request"]
    assert (
        "preferred_payment_days"
        not in with_partner_payment_days(anonymous, lookup=_seven)["user_request"]
    )


def test_a_failing_contract_lookup_leaves_the_term_open(monkeypatch):
    def broken(*, partner_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(adapter, "get_partner_profile", broken)

    assert adapter._partner_contract_payment_days(PARTNER) is None


# ── 7일 결제로 선 안 ──────────────────────────────────────────────────────


def _seven_day_reply():
    data = with_partner_payment_days(_payload(), lookup=_seven)
    return run_proposal(SalesProposalInput.model_validate(data))


def test_every_scenario_carries_the_seven_day_contract_term():
    reply = _seven_day_reply()

    assert reply.scenarios
    assert {scenario.payment_days for scenario in reply.scenarios} == {7}
    for scenario in reply.scenarios:
        assert scenario.collection_reference_date == scenario.delivery_date


def test_a_confirmed_seven_day_sale_is_due_seven_calendar_days_later():
    #  ★ 확정은 재무가 센 공헌이익이 있어야 선다 (없으면 확정이 막힌다). 그래서 재무
    #    검토를 받은 안으로 확정한다.
    _, reviewed = _refed("PASS", {**_CREDIT_SUMMARY, "required_collection_before_sale_krw": "0"})
    scenario = reviewed.scenarios[0]
    assert scenario.payment_days == 7
    sale_date = date(2026, 1, 15)
    plan = build_sale_confirmation_plan(
        SalesConfirmationInput.model_validate(
            {
                "execution_identity": {
                    "request_id": "REQ-7",
                    "run_id": "RUN-7",
                    "as_of": "2026-01-14",
                    "policy_version": "v1",
                    "feedback_attempt": 0,
                },
                "selected_scenario_id": scenario.scenario_id,
                "selected_scenario": scenario.model_dump(mode="json"),
                "sim_run_id": "SIM-7DAY",
                "sale_date": sale_date.isoformat(),
                "order_date": "2026-01-14",
                "source_order_id": "REQ-7",
                "line": {
                    "item_name": scenario.item,
                    "quantity_kg": str(scenario.quantity_kg),
                    "unit_price_krw_per_kg": str(scenario.unit_price_krw),
                    "grade": None,
                },
            }
        )
    )

    assert plan.collection_due_date == sale_date + timedelta(days=7) == date(2026, 1, 22)


# ── 재무 여신 사실을 옮기기만 한다 ─────────────────────────────────────────


def _credit_feedback(scenario_ids, *, verdict: str, summary: dict[str, Any]):
    return {
        "original_run_id": "RUN-7-1",
        "attempt": 1,
        "domain_replies": [
            {
                "source_agent": "finance",
                "capability": "FINANCIAL_VALIDATION",
                "reply_ref": f"FIN-{sid}",
                "runtime_status": "READY",
                "business_status": "ok" if verdict == "PASS" else "reject",
                "payload": {
                    "finance_verdict": verdict,
                    "financial_summary": summary,
                    "reason_codes": ["SALES_CREDIT_LIMIT_EXCEEDED"] if verdict == "FAIL" else [],
                    "missing_data": [],
                    "evidence_refs": [],
                },
            }
            for sid in scenario_ids
        ],
        "scenario_feedback": [
            {"scenario_id": sid, "reply_refs": [f"FIN-{sid}"]} for sid in scenario_ids
        ],
    }


_CREDIT_SUMMARY = {
    "contribution_margin_krw": "900000",
    "contribution_margin_rate": "0.30",
    "overdue_ar_krw": "0",
    "current_partner_ar_krw": "8000000",
    "projected_partner_ar_krw": "11000000",
    "credit_limit_krw": "10000000",
    "available_credit_krw": "2000000",
    "required_collection_before_sale_krw": "1000000",
    "credit_utilization_rate": "0.8",
    "expected_credit_recovery_date": "2026-01-12",
}


def _refed(verdict: str, summary: dict[str, Any]):
    first = _seven_day_reply()
    ids = [scenario.scenario_id for scenario in first.scenarios]
    data = with_partner_payment_days(_payload(), lookup=_seven)
    data.update(
        is_refeed=True,
        feedback_attempt=1,
        feedback=_credit_feedback(ids, verdict=verdict, summary=summary),
    )
    return first, run_proposal(SalesProposalInput.model_validate(data))


def test_finance_credit_facts_are_carried_not_computed():
    _, reply = _refed("PASS", _CREDIT_SUMMARY)

    scenario = reply.scenarios[0]
    assert scenario.current_partner_ar_krw == Decimal(8_000_000)
    assert scenario.credit_limit_krw == Decimal(10_000_000)
    assert scenario.available_credit_krw == Decimal(2_000_000)
    assert scenario.projected_partner_ar_krw == Decimal(11_000_000)
    assert scenario.required_collection_before_sale_krw == Decimal(1_000_000)
    assert scenario.credit_utilization_rate == Decimal("0.8")
    assert scenario.expected_credit_recovery_date == date(2026, 1, 12)


def test_without_a_finance_reply_the_credit_facts_stay_unknown():
    scenario = _seven_day_reply().scenarios[0]

    for name in (
        "current_partner_ar_krw",
        "credit_limit_krw",
        "available_credit_krw",
        "required_collection_before_sale_krw",
        "credit_utilization_rate",
        "expected_credit_recovery_date",
    ):
        assert getattr(scenario, name) is None, name


def test_a_credit_fail_does_not_make_sales_cut_price_or_quantity():
    """🔴 재무가 한도를 넘는다고 해도 판매가 가격을 내리거나 수량을 줄여 우회하지 않는다."""
    first, reply = _refed("FAIL", _CREDIT_SUMMARY)

    before = {
        s.scenario_id: (s.quantity_kg, s.unit_price_krw, s.sales_amount_krw)
        for s in first.scenarios
    }
    rejected = [trace for trace in reply.decision_trace if trace.status == "INFEASIBLE"]
    assert rejected, reply.decision_trace
    assert reply.recommended_scenario_id is None
    for scenario in reply.scenarios:
        original = scenario.parent_scenario_id or scenario.scenario_id
        assert (scenario.quantity_kg, scenario.unit_price_krw, scenario.sales_amount_krw) == before[
            original
        ]


# ── 금일 판매안 읽기 모델 ─────────────────────────────────────────────────


def _proposal_rows(summary: dict[str, Any]):
    return [
        {
            "request_id": "REQ-7",
            "history_run_id": "RUN-7",
            "payload": {"recommended_scenario_id": None, "missing_capabilities": []},
            "scenario": {
                "scenario_id": "SALES-001-A",
                "scenario_type": "CONSERVATIVE",
                "item": "배추",
                "partner_id": PARTNER,
                "quantity_kg": "2000",
                "unit_price_krw": "1500",
                "reported_sales_amount_krw": "3000000",
                "payment_days": 7,
                "delivery_date": "2026-01-15",
                "status": "UNRESOLVED",
            },
            "finance_verdict": "FAIL",
            "finance_status": "EVALUATED",
            "rule_results": [
                {
                    "rule_id": "FIN-SALES-CREDIT",
                    "verdict": "FAIL",
                    "reason_codes": ["SALES_CREDIT_LIMIT_EXCEEDED"],
                }
            ],
            "financial_summary": summary,
        }
    ]


def test_console_proposals_expose_the_collection_needed_before_the_sale(monkeypatch):
    monkeypatch.setattr(
        console_proposals, "load_proposal_rows", lambda **_: _proposal_rows(_CREDIT_SUMMARY)
    )

    row = console_proposals.get_console_sales_proposals(
        sim_run_id="SIM-7DAY", as_of=date(2026, 1, 8)
    ).rows[0]

    assert row.payment_days == 7
    assert row.current_partner_ar_krw == Decimal(8_000_000)
    assert row.required_collection_before_sale_krw == Decimal(1_000_000)
    assert row.credit_utilization_rate == Decimal("0.8")
    assert row.expected_credit_recovery_date == date(2026, 1, 12)
    assert row.finance_reason_codes == ["SALES_CREDIT_LIMIT_EXCEEDED"]


def test_console_proposals_keep_zero_collection_apart_from_unknown(monkeypatch):
    zero = {
        **_CREDIT_SUMMARY,
        "required_collection_before_sale_krw": "0",
        "expected_credit_recovery_date": None,
    }
    monkeypatch.setattr(console_proposals, "load_proposal_rows", lambda **_: _proposal_rows(zero))
    zero_row = console_proposals.get_console_sales_proposals(
        sim_run_id="S", as_of=date(2026, 1, 8)
    ).rows[0]

    monkeypatch.setattr(console_proposals, "load_proposal_rows", lambda **_: _proposal_rows({}))
    unknown_row = console_proposals.get_console_sales_proposals(
        sim_run_id="S", as_of=date(2026, 1, 8)
    ).rows[0]

    assert zero_row.required_collection_before_sale_krw == Decimal(0)
    assert zero_row.expected_credit_recovery_date is None
    assert unknown_row.required_collection_before_sale_krw is None
    assert unknown_row.current_partner_ar_krw is None
