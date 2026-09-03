"""A/B/C 의미 · 세 수량 · 계보 · 근거 — 조용히 섞이지 않는가.

★ 이 파일이 지키는 것.
    · 세 수량은 서로 다른 사실이다 (확정 / 부족 / 조건부 확보 가능)
    · 확정 0kg 은 사실이고 확정 미수신은 모름이다
    · 권위 있는 중간값이 없으면 만들지 않고 collapse 로 남긴다
    · 계보는 Sales 가 소유한다 — 외부 문자열에서 만들지 않는다
    · source_ref 와 evidence_refs 는 역할이 다르다

각 검사에는 **뒤집으면 깨지는 역검사**를 함께 둔다 — 임의 비율·평균·0 대체가
들어오면 여기서 걸린다.
"""

from decimal import Decimal

import pytest

from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def _logistics(confirmed, *, date="2026-09-10", item="배추"):
    """confirmed 가 None 이면 그 날짜의 권위 수량 자체를 주지 않는다."""
    capacity = (
        [] if confirmed is None else [{"date": date, "confirmed_sellable_quantity_kg": confirmed}]
    )
    return {
        "query_scope": {"item": item, "max_confirmed_sellable_quantity_kg": 9999},
        "sellable_supply": {
            "status": "READY",
            "inventory_by_item": [{"item": item, "available_qty_kg": 9999}],
            "supply_capacity_by_date": capacity,
        },
        "delivery_feasibility": {
            "status": "READY",
            "daily_outbound_capacity_kg": 9999,
            "reason_codes": [],
        },
    }


def _request(requested, confirmed, **over):
    data = {
        "business_mode": "SPOT_SALES",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": requested,
            "preferred_unit_price_krw": 2000,
            "preferred_delivery_date": "2026-09-10",
        },
        "logistics_context": _logistics(confirmed),
    }
    data.update(over)
    return SalesProposalInput.model_validate(data)


def _types(reply):
    return {scenario.scenario_type: scenario for scenario in reply.scenarios}


# ---------------------------------------------------------------------------
# 세 수량 — 공급 매트릭스
# ---------------------------------------------------------------------------


def test_confirmed_above_request_needs_no_additional_supply():
    scenarios = _types(run_proposal(_request(1000, 3000)))

    for scenario in scenarios.values():
        assert scenario.supply.required_additional_quantity_kg == Decimal(0)
        assert scenario.supply.additional_supply_required is False


def test_confirmed_equal_to_request_needs_no_additional_supply():
    scenarios = _types(run_proposal(_request(3000, 3000)))

    aggressive = scenarios["AGGRESSIVE"]
    assert aggressive.supply.confirmed_quantity_kg == Decimal(3000)
    assert aggressive.supply.required_additional_quantity_kg == Decimal(0)


def test_confirmed_below_request_computes_the_shortfall():
    aggressive = _types(run_proposal(_request(5000, 3000)))["AGGRESSIVE"]

    assert aggressive.supply.confirmed_quantity_kg == Decimal(3000)
    assert aggressive.supply.required_additional_quantity_kg == Decimal(2000)
    assert aggressive.supply.additional_supply_required is True


def test_zero_confirmed_supply_is_a_fact_not_a_missing_context():
    """🔴 0kg 확인과 확정 수량 미수신은 다른 사실이다."""
    aggressive = _types(run_proposal(_request(100, 0)))["AGGRESSIVE"]

    assert aggressive.supply.confirmed_quantity_kg == Decimal(0)
    assert aggressive.supply.required_additional_quantity_kg == Decimal(100)


def test_absent_confirmed_supply_stays_unknown_and_asks_for_context():
    aggressive = _types(run_proposal(_request(100, None)))["AGGRESSIVE"]

    assert aggressive.supply.confirmed_quantity_kg is None
    # 모르는 것을 0 으로 바꾸면 부족량이 100 으로 확정돼 버린다.
    assert aggressive.supply.required_additional_quantity_kg is None
    assert "SELLABLE_SUPPLY_CONTEXT" in aggressive.required_validations


def test_shortfall_is_never_copied_into_conditional_quantity():
    """🔴 '더 필요한 양' 과 '확보 가능하다고 확인된 양' 은 다른 사실이다."""
    aggressive = _types(run_proposal(_request(5000, 3000)))["AGGRESSIVE"]

    assert aggressive.supply.required_additional_quantity_kg == Decimal(2000)
    # 매입이 확인해 주기 전까지 조건부는 모름이다.
    assert aggressive.supply.conditional_quantity_kg is None
    assert aggressive.supply.dependency_ref is None


def test_conditional_supply_is_never_summed_into_confirmed():
    reply = run_proposal(
        _request(
            5000,
            3000,
            is_refeed=True,
            feedback_attempt=1,
            feedback={
                "attempt": 1,
                "domain_replies": [
                    {
                        "source_agent": "purchase",
                        "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                        "reply_ref": "PUR-1",
                        "runtime_status": "READY",
                        "business_status": "ok",
                        "payload": {"procurable_quantity_kg": 2000, "risks": []},
                    }
                ],
                "scenario_feedback": [
                    {"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}
                ],
            },
        )
    )
    supply = _types(reply)["AGGRESSIVE"].supply

    assert supply.confirmed_quantity_kg == Decimal(3000)
    assert supply.conditional_quantity_kg == Decimal(2000)
    # 합쳤다면 5000 이 됐을 것이다.
    assert supply.confirmed_quantity_kg != Decimal(5000)


# ---------------------------------------------------------------------------
# A/B/C 의미
# ---------------------------------------------------------------------------


def test_conservative_uses_the_authoritative_confirmed_quantity():
    """보수안은 확정 공급량을 쓴다 — 임의 비율을 만들지 않는다."""
    scenarios = _types(run_proposal(_request(5000, 3000)))
    conservative = scenarios["CONSERVATIVE"]

    assert conservative.quantity_kg == Decimal(3000)
    assert "QUANTITY" in conservative.sales_decision_axes


@pytest.mark.parametrize("ratio", ["0.8", "0.9", "0.95"])
def test_conservative_never_uses_an_invented_ratio(ratio):
    conservative = _types(run_proposal(_request(5000, 3000)))["CONSERVATIVE"]
    invented = Decimal(5000) * Decimal(ratio)

    assert conservative.quantity_kg != invented


def test_balanced_collapses_instead_of_inventing_a_middle_quantity():
    """🔴 확정과 요청 사이의 중간값을 만들 권위 있는 근거가 없다."""
    balanced = _types(run_proposal(_request(5000, 3000)))["BALANCED"]

    assert balanced.variant_collapsed is True
    assert balanced.variant_collapsed_reason == "AUTHORITATIVE_INTERMEDIATE_OPTION_UNAVAILABLE"
    # 평균(4000)도, 임의 비율도 아니다.
    assert balanced.quantity_kg == Decimal(5000)
    assert balanced.quantity_kg != Decimal(4000)


def test_collapse_always_carries_a_reason():
    reply = run_proposal(_request(5000, 3000))

    for scenario in reply.scenarios:
        if scenario.variant_collapsed:
            assert scenario.variant_collapsed_reason


def test_scenario_types_come_from_the_closed_vocabulary():
    """유형 **어휘**는 계약이다 (`ScenarioType` 이 닫힌 Literal).

    ★ 반면 "항상 정확히 세 개" 는 스키마가 강제하지 않는다 —
      `SalesProposalReply.scenarios` 에 길이 제약이 없다. 그래서 개수를 계약처럼
      잠그지 않고, 어휘와 유형 유일성만 고정한다. 개수 정책이 확정되면 그때 넣는다.
    """
    from typing import get_args

    from app.sales.schemas import ScenarioType

    reply = run_proposal(_request(1000, 3000))
    types = [scenario.scenario_type for scenario in reply.scenarios]

    assert types, "안이 하나도 없으면 이 검사는 아무것도 지키지 못한다"
    assert set(types) <= set(get_args(ScenarioType))
    # 같은 유형이 두 번 나오면 어느 쪽이 그 유형인지 알 수 없다.
    assert len(types) == len(set(types))


def test_scenario_count_is_not_locked_by_the_schema():
    """개수를 계약으로 승격하지 않았다는 사실 자체를 남긴다."""
    from app.sales.schemas import SalesProposalReply

    metadata = SalesProposalReply.model_fields["scenarios"].metadata

    assert not metadata, f"길이 제약이 생겼다면 개수 계약을 다시 판단한다: {metadata}"


def test_converged_scenarios_do_not_pretend_to_be_different_numbers():
    """🔴 값이 같아졌으면 같다고 말한다 — 다른 안인 척 숫자를 벌리지 않는다."""
    reply = run_proposal(_request(1000, 3000))
    quantities = {s.scenario_type: s.quantity_kg for s in reply.scenarios}

    # 확정 공급이 요청보다 많아 어느 유형도 수량을 좁힐 근거가 없다.
    assert set(quantities.values()) == {Decimal(1000)}
    for scenario in reply.scenarios:
        if scenario.quantity_kg == Decimal(1000) and scenario.scenario_type != "AGGRESSIVE":
            # 같은 숫자를 내놓는 안은 그 사실을 collapse 로 밝힌다.
            assert scenario.variant_collapsed is True


def test_aggressive_keeps_the_requested_quantity_without_clipping():
    aggressive = _types(run_proposal(_request(5000, 3000)))["AGGRESSIVE"]

    assert aggressive.quantity_kg == Decimal(5000)
    assert "ADDITIONAL_SUPPLY_CONTEXT" in aggressive.required_validations


# ---------------------------------------------------------------------------
# 계보 — Sales 가 소유한다
# ---------------------------------------------------------------------------


def _refeed(attempt, replies=(), feedback_scenarios=()):
    return _request(
        5000,
        3000,
        is_refeed=True,
        feedback_attempt=attempt,
        feedback={
            "attempt": attempt,
            "domain_replies": list(replies),
            "scenario_feedback": list(feedback_scenarios),
        },
    )


def test_initial_run_has_no_parent_and_revision_zero():
    reply = run_proposal(_request(5000, 3000))

    for scenario in reply.scenarios:
        assert scenario.revision == 0
        assert scenario.parent_scenario_id is None
        assert "-R" not in scenario.scenario_id


@pytest.mark.parametrize("attempt", [1, 2])
def test_refeed_lineage_is_generated_by_sales(attempt):
    reply = run_proposal(_refeed(attempt))

    for scenario in reply.scenarios:
        assert scenario.revision == attempt
        assert scenario.scenario_id.endswith(f"-R{attempt}")
        assert scenario.parent_scenario_id == scenario.scenario_id.rsplit("-R", 1)[0]
        assert scenario.parent_scenario_id.startswith("SALES-001-")


def test_lineage_is_not_derived_from_an_external_reply_string():
    """외부 회신의 ref 문자열이 계보를 만들지 않는다."""
    reply = run_proposal(
        _refeed(
            1,
            replies=[
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    # 계보처럼 생긴 문자열을 일부러 넣는다.
                    "reply_ref": "SALES-001-C-R9",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {"procurable_quantity_kg": 100, "risks": []},
                }
            ],
            feedback_scenarios=[
                {"scenario_id": "SALES-001-C", "reply_refs": ["SALES-001-C-R9"]}
            ],
        )
    )
    aggressive = _types(reply)["AGGRESSIVE"]

    # 회신 ref 를 파싱해 R9 을 만들지 않는다 — 회차는 Sales 의 attempt 가 정한다.
    assert aggressive.revision == 1
    assert aggressive.scenario_id == "SALES-001-C-R1"
    assert aggressive.supply.dependency_ref == "SALES-001-C-R9"


def test_self_check_passes_for_a_clean_initial_run():
    reply = run_proposal(_request(5000, 3000))

    assert reply.self_check.passed is True, reply.self_check.issue_codes


# ---------------------------------------------------------------------------
# source_ref 와 evidence_refs 는 역할이 다르다
# ---------------------------------------------------------------------------

_CONTRACT = {
    "contract_id": "C-1",
    "partner_id": "P-1",
    "item": "배추",
    "contract_quantity_kg": 4000,
    "contract_unit_price_krw": 1800,
    "contract_delivery_date": "2026-09-10",
    "contract_payment_days": 20,
    "contract_term_days": 90,
    "source_ref": "CONTRACT:C-1",
}


def test_source_ref_is_not_the_first_evidence_ref():
    """🔴 배열 첫 번째를 고르는 것은 근거가 아니라 우연이다."""
    logistics = _logistics(3000)
    # 잘못 고를 수 있는 후보를 일부러 둔다.
    logistics["evidence_refs"] = ["LOG-EV-1", "LOG-EV-2"]

    reply = run_proposal(
        SalesProposalInput.model_validate(
            {
                "business_mode": "SPOT_SALES",
                "user_request": {
                    "item": "배추",
                    "requested_quantity_kg": 3000,
                    "preferred_unit_price_krw": 2000,
                    "preferred_delivery_date": "2026-09-10",
                },
                "logistics_context": logistics,
            }
        )
    )
    scenario = reply.scenarios[2]

    # 사용자 ref 를 안 줬으므로 출처는 없다. 보조 근거가 있어도 승격되지 않는다.
    assert "LOG-EV-1" in scenario.evidence_refs
    assert scenario.source_ref is None
    assert scenario.source_ref != scenario.evidence_refs[0]


def test_evidence_refs_are_broader_than_the_single_source():
    reply = run_proposal(
        SalesProposalInput.model_validate(
            {
                "business_mode": "CONTRACT_FULFILLMENT",
                "user_request": {"item": "배추"},
                "contract_context": _CONTRACT,
                "logistics_context": _logistics(4000),
            }
        )
    )
    scenario = reply.scenarios[1]

    assert scenario.source_ref == "CONTRACT:C-1"
    # 계약은 출처이자 근거지만, 근거 목록은 그보다 넓을 수 있다.
    assert "CONTRACT:C-1" in scenario.evidence_refs


def test_evidence_refs_are_deduplicated_and_carry_reply_refs():
    reply = run_proposal(
        _refeed(
            1,
            replies=[
                {
                    "source_agent": "purchase",
                    "capability": "ADDITIONAL_SUPPLY_CONTEXT",
                    "reply_ref": "PUR-1",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {"procurable_quantity_kg": 100, "risks": []},
                }
            ],
            feedback_scenarios=[{"scenario_id": "SALES-001-C", "reply_refs": ["PUR-1"]}],
        )
    )
    aggressive = _types(reply)["AGGRESSIVE"]

    assert "PUR-1" in aggressive.evidence_refs
    assert len(aggressive.evidence_refs) == len(set(aggressive.evidence_refs))
