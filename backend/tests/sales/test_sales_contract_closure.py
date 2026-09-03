"""2026-09-03 계약 확정 — 결제방식 · source_ref · 조건부 공급 소유권.

★ 이 파일이 지키는 것.
    · `payment_terms_type` 은 사용자/계약이 말해 준 사실이지 추론값이 아니다
    · `source_ref` 는 상업조건의 **직접 출발점**이고 evidence_refs 와 역할이 다르다
    · 갱신에서 사용자가 조건을 바꾸면 계약 ref 를 그 변경안의 출처로 쓰지 않는다
    · Purchase 가 확인해 준 조건부 수량만 조건부 칸에 산다 (0 은 사실, 미확인은 None)
    · 조건부 수량을 확정 공급이나 Scenario 수량에 합산하지 않는다
"""

from decimal import Decimal

import pytest

from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput


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

_CONTRACT = {
    "contract_id": "C-1",
    "partner_id": "P-1",
    "item": "배추",
    "contract_quantity_kg": 4000,
    "contract_unit_price_krw": 1800,
    "contract_delivery_date": "2026-09-10",
    "contract_payment_days": 20,
    "contract_payment_terms_type": "INSTALLMENT",
    "contract_term_days": 90,
    "source_ref": "CONTRACT:C-1",
}


def _user(**over):
    """Scenario 를 만들 수 있는 최소 상업 사실 — 여기에 시험 대상만 얹는다."""
    base = {
        "item": "배추",
        "requested_quantity_kg": 3000,
        "preferred_unit_price_krw": 2000,
        "preferred_delivery_date": "2026-09-10",
    }
    base.update(over)
    return base


def _request(mode="SPOT_SALES", user=None, contract=None, **over):
    data = {
        "business_mode": mode,
        "user_request": {"item": "배추", **(user or {})},
        "logistics_context": _LOGISTICS,
    }
    if contract is not None:
        data["contract_context"] = contract
    data.update(over)
    return SalesProposalInput.model_validate(data)


def _first(reply):
    return reply.scenarios[0]


# ---------------------------------------------------------------------------
# payment_terms_type
# ---------------------------------------------------------------------------


def test_fulfillment_carries_the_contract_payment_terms_type():
    reply = run_proposal(_request("CONTRACT_FULFILLMENT", contract=_CONTRACT))

    assert _first(reply).payment_terms_type == "INSTALLMENT"


def test_renewal_user_type_overrides_the_contract():
    reply = run_proposal(
        _request(
            "CONTRACT_PROPOSAL_RENEWAL",
            user={"preferred_payment_terms_type": "SINGLE"},
            contract=_CONTRACT,
        )
    )

    assert _first(reply).payment_terms_type == "SINGLE"


def test_renewal_falls_back_to_the_contract_type_when_user_is_silent():
    reply = run_proposal(
        _request(
            "CONTRACT_PROPOSAL_RENEWAL",
            user={"requested_quantity_kg": 4000},
            contract=_CONTRACT,
        )
    )

    assert _first(reply).payment_terms_type == "INSTALLMENT"


@pytest.mark.parametrize("mode", ["CONTRACT_PROPOSAL_NEW", "SPOT_SALES"])
def test_new_and_spot_keep_the_user_type(mode):
    reply = run_proposal(
        _request(mode, user=_user(preferred_payment_terms_type="INSTALLMENT"))
    )

    assert _first(reply).payment_terms_type == "INSTALLMENT"


@pytest.mark.parametrize("mode", ["CONTRACT_PROPOSAL_NEW", "SPOT_SALES"])
def test_no_user_type_stays_none(mode):
    reply = run_proposal(_request(mode, user=_user()))

    assert _first(reply).payment_terms_type is None


def test_payment_days_alone_never_creates_single():
    """🔴 결제일수는 *언제* 받는지이고 결제방식은 *몇 번에* 받는지다."""
    reply = run_proposal(_request("SPOT_SALES", user=_user(preferred_payment_days=30)))

    scenario = _first(reply)
    assert scenario.payment_days == 30
    assert scenario.payment_terms_type is None


# ---------------------------------------------------------------------------
# source_ref
# ---------------------------------------------------------------------------


def test_fulfillment_source_ref_is_the_contract():
    reply = run_proposal(_request("CONTRACT_FULFILLMENT", contract=_CONTRACT))

    assert _first(reply).source_ref == "CONTRACT:C-1"


def test_renewal_without_override_keeps_the_contract_source():
    reply = run_proposal(_request("CONTRACT_PROPOSAL_RENEWAL", contract=_CONTRACT))

    assert _first(reply).source_ref == "CONTRACT:C-1"


def test_renewal_override_with_user_source_uses_the_user_ref():
    reply = run_proposal(
        _request(
            "CONTRACT_PROPOSAL_RENEWAL",
            user={"preferred_unit_price_krw": 2500, "source_ref": "USER-REQ:U-9"},
            contract=_CONTRACT,
        )
    )

    assert _first(reply).source_ref == "USER-REQ:U-9"


def test_renewal_override_without_user_source_is_none_not_the_contract():
    """🔴 바꾼 사람은 사용자인데 계약을 근거로 달면 누가 정했는지 뒤바뀐다."""
    reply = run_proposal(
        _request(
            "CONTRACT_PROPOSAL_RENEWAL",
            user={"preferred_unit_price_krw": 2500},
            contract=_CONTRACT,
        )
    )

    assert _first(reply).source_ref is None


@pytest.mark.parametrize("mode", ["CONTRACT_PROPOSAL_NEW", "SPOT_SALES"])
def test_new_and_spot_use_the_user_source_ref(mode):
    reply = run_proposal(
        _request(mode, user=_user(source_ref="USER-REQ:U-1"))
    )

    assert _first(reply).source_ref == "USER-REQ:U-1"


@pytest.mark.parametrize("mode", ["CONTRACT_PROPOSAL_NEW", "SPOT_SALES"])
def test_no_user_source_ref_stays_none(mode):
    reply = run_proposal(_request(mode, user=_user()))

    assert _first(reply).source_ref is None


def test_source_ref_is_not_borrowed_from_evidence_or_scenario_id():
    reply = run_proposal(_request("SPOT_SALES", user=_user()))

    scenario = _first(reply)
    assert scenario.source_ref is None
    # 보조 근거는 따로 쌓이지만 그중 하나를 출처로 승격하지 않는다.
    assert scenario.source_ref not in scenario.evidence_refs
    assert scenario.source_ref != scenario.scenario_id


def test_source_ref_and_evidence_refs_are_different_roles():
    reply = run_proposal(_request("CONTRACT_FULFILLMENT", contract=_CONTRACT))

    scenario = _first(reply)
    # 계약이 출처이자 근거일 수는 있지만, 근거 목록은 그보다 넓다.
    assert scenario.source_ref == "CONTRACT:C-1"
    assert len(scenario.evidence_refs) >= 1


# ---------------------------------------------------------------------------
# 조건부 공급 — Purchase 가 확인해 준 것만
# ---------------------------------------------------------------------------


def _purchase_reply(status="ok", runtime="READY", quantity=2000, ref="PUR-1", omit=False):
    payload = {} if omit else {"procurable_quantity_kg": quantity}
    return {
        "source_agent": "purchase",
        "capability": "ADDITIONAL_SUPPLY_CONTEXT",
        "reply_ref": ref,
        "runtime_status": runtime,
        "business_status": status,
        "payload": payload,
    }


def _refeed(reply):
    return _request(
        "SPOT_SALES",
        user={"requested_quantity_kg": 5000, "preferred_unit_price_krw": 2000,
              "preferred_delivery_date": "2026-09-10"},
        is_refeed=True,
        feedback_attempt=1,
        feedback={
            "attempt": 1,
            "domain_replies": [reply],
            "scenario_feedback": [
                {"scenario_id": "SALES-001-C", "reply_refs": [reply["reply_ref"]]}
            ],
        },
    )


def _aggressive(reply):
    return reply.scenarios[2]


def test_initial_scenario_has_no_conditional_quantity():
    """Purchase 검증 전에는 조건부 확보량을 아무도 확인해 주지 않았다."""
    reply = run_proposal(
        _request("SPOT_SALES", user={"requested_quantity_kg": 5000,
                                     "preferred_unit_price_krw": 2000,
                                     "preferred_delivery_date": "2026-09-10"})
    )

    for scenario in reply.scenarios:
        assert scenario.supply.conditional_quantity_kg is None
        assert scenario.supply.dependency_ref is None


def test_required_additional_is_not_copied_into_conditional():
    reply = run_proposal(
        _request("SPOT_SALES", user={"requested_quantity_kg": 5000,
                                     "preferred_unit_price_krw": 2000,
                                     "preferred_delivery_date": "2026-09-10"})
    )

    scenario = _aggressive(reply)
    assert scenario.supply.required_additional_quantity_kg == Decimal(2000)
    assert scenario.supply.conditional_quantity_kg is None


def test_purchase_positive_quantity_becomes_conditional_with_lineage():
    reply = run_proposal(_refeed(_purchase_reply(quantity=1500)))

    supply = _aggressive(reply).supply
    assert supply.conditional_quantity_kg == Decimal(1500)
    assert supply.dependency_ref == "PUR-1"


def test_purchase_explicit_zero_is_preserved_as_zero():
    """0 은 '확보 가능량이 0으로 확인됨' 이라는 사실이다 — missing 이 아니다."""
    reply = run_proposal(_refeed(_purchase_reply(status="skipped", quantity=0)))

    supply = _aggressive(reply).supply
    assert supply.conditional_quantity_kg == Decimal(0)
    assert supply.dependency_ref == "PUR-1"


def test_purchase_without_quantity_stays_unknown():
    reply = run_proposal(_refeed(_purchase_reply(omit=True)))

    assert _aggressive(reply).supply.conditional_quantity_kg is None


def test_purchase_runtime_not_ready_stays_unknown():
    reply = run_proposal(_refeed(_purchase_reply(runtime="RUNTIME_NOT_READY", quantity=1500)))

    supply = _aggressive(reply).supply
    # 못 돈 회신의 숫자를 사실로 삼지 않는다.
    assert supply.conditional_quantity_kg is None
    assert supply.dependency_ref is None


def test_conditional_supply_is_never_added_to_confirmed():
    reply = run_proposal(_refeed(_purchase_reply(quantity=1500)))

    supply = _aggressive(reply).supply
    assert supply.confirmed_quantity_kg == Decimal(3000)
    assert supply.conditional_quantity_kg == Decimal(1500)
    # 합산했다면 4500 이 됐을 것이다.
    assert supply.confirmed_quantity_kg != Decimal(4500)


def test_scenario_quantity_is_not_raised_by_conditional_supply():
    baseline = run_proposal(
        _request("SPOT_SALES", user={"requested_quantity_kg": 5000,
                                     "preferred_unit_price_krw": 2000,
                                     "preferred_delivery_date": "2026-09-10"})
    )
    refed = run_proposal(_refeed(_purchase_reply(quantity=1500)))

    assert _aggressive(refed).quantity_kg == _aggressive(baseline).quantity_kg
