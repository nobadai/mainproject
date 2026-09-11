"""Sales v1.7 P0 계약 회귀 — 모든 숫자는 TEST FIXTURE다."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.sales.llm.runtime import LlmInterpretationOutput, interpret_candidates
from app.sales.proposal import _generate_scenarios, run_proposal
from app.sales.ranking import rank_scenarios, remove_dominated_scenarios
from app.sales.schemas import LogisticsLotConstraint, SalesCandidate, SalesProposalInput


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")


def _logistics(confirmed=7000):
    return {
        "query_scope": {"item": "배추"},
        "sellable_supply": {
            "status": "READY",
            "inventory_by_item": [{"item": "배추", "available_qty_kg": confirmed}],
            "supply_capacity_by_date": [
                {"date": "2026-09-10", "confirmed_sellable_quantity_kg": confirmed}
            ],
        },
        "delivery_feasibility": {"status": "READY"},
    }


def _reply(source, capability, ref, *, business="ok", payload=None):
    return {
        "source_agent": source,
        "capability": capability,
        "reply_ref": ref,
        "runtime_status": "READY",
        "business_status": business,
        "payload": payload or {},
    }


def _request(
    *,
    mode="SPOT_SALES",
    allow=True,
    quantity=10000,
    replies=(),
    target="C",
    delivery_date="2026-09-10",
    logistics=None,
):
    return SalesProposalInput.model_validate(
        {
            "business_mode": mode,
            "user_request": {
                "item": "배추",
                "partner_id": "P-1",
                "requested_quantity_kg": quantity,
                "preferred_unit_price_krw": 2300,
                "preferred_delivery_date": delivery_date,
                "preferred_payment_days": 30,
                "preferred_payment_terms_type": "SINGLE",
                "source_ref": "TEST:USER-1",
                "allow_additional_sourcing": allow,
            },
            "logistics_context": _logistics() if logistics is None else logistics,
            "feedback": {
                "domain_replies": list(replies),
                "scenario_feedback": [
                    {
                        "scenario_id": f"SALES-001-{target}",
                        "reply_refs": [reply["reply_ref"] for reply in replies],
                    }
                ],
            }
            if replies
            else None,
        }
    )


def _finance(verdict="PASS", *, margin="1000000", extra=None):
    payload = {
        "finance_verdict": verdict,
        "financial_summary": {
            "contribution_margin_krw": margin,
            "contribution_margin_rate": "0.05",
            "scenario_projected_cash_min": "100",
            "depends_on_projected_inflow": False,
            "overdue_ar_krw": "0",
            "future_finance_field": "ignored",
        },
        "reason_codes": [],
        "missing_data": [],
        "evidence_refs": ["TEST:FIN-1"],
    }
    payload.update(extra or {})
    return _reply("finance", "FINANCIAL_VALIDATION", "FIN-1", payload=payload)


def _purchase(quantity, *, available_date=None):
    return _reply(
        "purchase",
        "ADDITIONAL_SUPPLY_CONTEXT",
        "PUR-1",
        payload={
            "procurable_quantity_kg": quantity,
            "available_date": available_date,
            "risks": [],
            "future_purchase_field": "ignored",
        },
    )


@pytest.mark.parametrize("value", [-7, -1, 0, 3])
def test_s01_remaining_freshness_preserves_signed_value(value):
    lot = LogisticsLotConstraint(lot_id="L-1", item="배추", remaining_freshness_days=value)
    assert lot.remaining_freshness_days == value


def test_s02_partial_purchase_caps_supported_candidate():
    reply = run_proposal(_request(replies=[_purchase(1500), _finance()]))
    scenario = next(item for item in reply.scenarios if item.scenario_type == "AGGRESSIVE")
    assert scenario.quantity_kg == Decimal(8500)
    assert scenario.supply.confirmed_quantity_kg == Decimal(7000)
    assert scenario.supply.conditional_quantity_kg == Decimal(1500)
    assert scenario.unmet_quantity_kg == Decimal(1500)
    assert scenario.status == "CONDITIONAL"


def test_s03_zero_and_s04_null_are_not_conflated():
    zero = _generate_scenarios(_request(replies=[_purchase(0), _finance()]))[-1]
    unknown = _generate_scenarios(_request(replies=[_purchase(None), _finance()]))[-1]
    assert zero.supply.conditional_quantity_kg == Decimal(0)
    assert zero.status == "INFEASIBLE"
    assert unknown.supply.conditional_quantity_kg is None
    assert unknown.status == "UNRESOLVED"


def test_s05_s06_spot_sourcing_is_explicit_opt_in():
    denied = run_proposal(_request(allow=False))
    allowed = run_proposal(_request(allow=True))
    denied_aggressive = next(s for s in denied.scenarios if s.scenario_type == "AGGRESSIVE")
    allowed_aggressive = next(s for s in allowed.scenarios if s.scenario_type == "AGGRESSIVE")
    assert "ADDITIONAL_SUPPLY_CONTEXT" not in denied_aggressive.required_validations
    assert "ADDITIONAL_SUPPLY_CONTEXT" in allowed_aggressive.required_validations


def test_s07_purchase_date_is_not_customer_delivery_date():
    reply = run_proposal(
        _request(replies=[_purchase(1500, available_date="2026-09-15"), _finance()])
    )
    scenario = next(item for item in reply.scenarios if item.scenario_type == "AGGRESSIVE")
    assert scenario.delivery_date == date(2026, 9, 10)
    assert "DELIVERY_REVALIDATION_REQUIRED" in scenario.execution_dependencies


def test_s08_user_price_survives_finance_pass_and_extra_fields():
    scenario = run_proposal(_request(quantity=7000, replies=[_finance()])).scenarios[-1]
    assert scenario.unit_price_krw == Decimal(2300)
    assert scenario.finance_verdict == "PASS"
    assert scenario.contribution_margin_krw == Decimal(1000000)


def test_s09_fail_does_not_invent_price_or_payment_and_s10_authority_can_adjust_payment():
    failed = _generate_scenarios(_request(quantity=7000, replies=[_finance("FAIL")]))[-1]
    assert failed.unit_price_krw == Decimal(2300)
    assert failed.payment_days == 30

    alternative = run_proposal(
        _request(
            quantity=7000,
            target="B",
            replies=[_finance("FAIL", extra={"max_finance_allowed_payment_terms_days": 20})],
        )
    )
    balanced = next(item for item in alternative.scenarios if item.scenario_type == "BALANCED")
    assert balanced.payment_days == 20
    assert "USER_PAYMENT_TERM_ACCEPTANCE_REQUIRED" in balanced.execution_dependencies
    assert "FINANCE_REVALIDATION_REQUIRED" in balanced.execution_dependencies


def _pre_sales_logistics(
    *, status, confirmed, earliest, daily_capacity=5000, capacity_by_date=True
):
    return {
        "query_scope": {"item": "배추", "as_of": "2026-09-09"},
        "sellable_supply": {
            "status": "READY",
            "inventory_by_item": [{"item": "배추", "available_qty_kg": confirmed}],
            "lot_constraints": [],
            "supply_capacity_by_date": (
                [{"date": "2026-09-10", "confirmed_sellable_quantity_kg": confirmed}]
                if capacity_by_date
                else []
            ),
        },
        "delivery_feasibility": {
            "status": status,
            "daily_outbound_capacity_kg": daily_capacity,
            "delivery_route": "FIXED_ROUTE",
            "transport_lead_time": 0,
            "earliest_delivery_date": earliest,
            "reason_codes": ["DELIVERY_BEFORE_PREP_LEAD"] if status == "FAIL" else [],
        },
    }


@pytest.mark.parametrize(
    (
        "delivery_date",
        "quantity",
        "confirmed",
        "status",
        "daily_capacity",
        "expected_confirmed",
        "expected_status",
    ),
    [
        ("2026-09-09", 3500, 3500, "FAIL", 5000, None, "INFEASIBLE"),
        ("2026-09-10", 3500, 3500, "READY", 6000, 3500, "EXECUTABLE"),
        ("2026-09-10", 5001, 5001, "FAIL", 5000, 5001, "INFEASIBLE"),
        ("2026-09-10", 3500, 3500, "READY", 5000, 3500, "EXECUTABLE"),
        ("2026-09-10", 3501, 3501, "FAIL", 5000, 3501, "INFEASIBLE"),
    ],
    ids=["as-of-fails", "as-of-plus-one-ready", "5001-exceeds", "1500-plus-3500", "1500-plus-3501"],
)
def test_pre_sales_delivery_facts_are_consumed_without_sales_recalculation(
    delivery_date, quantity, confirmed, status, daily_capacity, expected_confirmed, expected_status
):
    logistics = _pre_sales_logistics(
        status=status,
        confirmed=confirmed,
        earliest="2026-09-10",
        daily_capacity=daily_capacity,
    )
    request = _request(
        quantity=quantity,
        delivery_date=delivery_date,
        logistics=logistics,
        replies=[_finance()],
    )

    scenario = _generate_scenarios(request)[-1]

    delivery = request.logistics_context.delivery_feasibility
    assert delivery.delivery_route == "FIXED_ROUTE"
    assert delivery.transport_lead_time == 0
    assert delivery.earliest_delivery_date == date(2026, 9, 10)
    assert scenario.supply.confirmed_quantity_kg == (
        None if expected_confirmed is None else Decimal(expected_confirmed)
    )
    assert scenario.status == expected_status


def test_pre_sales_without_delivery_date_accepts_an_empty_date_capacity_vector():
    """날짜별 여력 벡터가 비어 있어도 확정 수량은 그대로 쓴다.

    ★ 사람이 납기를 안 준 자동 실행이므로 **물류가 확정한 최초 납품일**을 채택한다
      (`delivery_feasibility.status == READY`). 그 값이 회수 기준일이 된다.
    """
    request = _request(
        quantity=3500,
        delivery_date=None,
        logistics=_pre_sales_logistics(
            status="READY", confirmed=3500, earliest="2026-09-10", capacity_by_date=False
        ),
        replies=[_finance()],
    )

    scenario = _generate_scenarios(request)[-1]

    assert scenario.delivery_date == date(2026, 9, 10)
    assert scenario.collection_reference_date == scenario.delivery_date
    assert scenario.supply.confirmed_quantity_kg == Decimal(3500)
    assert scenario.status == "EXECUTABLE"


def test_an_unresolved_delivery_never_invents_a_date():
    """🔴 물류가 못 정한 날짜를 판매가 대신 정하지 않는다.

    `as_of` 나 오늘 날짜로 메우면 그 순간 없는 사실이 납기가 되고, 회수일이 거기서
    파생되어 현금흐름까지 거짓이 된다.
    """
    request = _request(
        quantity=3500,
        delivery_date=None,
        logistics=_pre_sales_logistics(
            status="UNRESOLVED", confirmed=3500, earliest="2026-09-10"
        ),
        replies=[_finance()],
    )

    scenario = _generate_scenarios(request)[-1]

    assert scenario.delivery_date is None
    assert scenario.collection_reference_date is None


def test_a_user_delivery_date_is_not_overwritten_by_logistics():
    """★ 사람이 말한 날짜가 먼저다."""
    request = _request(
        quantity=3500,
        delivery_date="2026-09-20",
        logistics=_pre_sales_logistics(
            status="READY", confirmed=3500, earliest="2026-09-10"
        ),
        replies=[_finance()],
    )

    scenario = _generate_scenarios(request)[-1]

    assert scenario.delivery_date == date(2026, 9, 20)
    assert scenario.collection_reference_date == date(2026, 9, 20)


def test_s11_missing_profit_stays_none_and_all_unresolved_has_no_recommendation():
    reply = run_proposal(_request(quantity=7000))
    assert all(item.contribution_margin_krw is None for item in reply.scenarios)
    assert reply.recommended_scenario_id is None


def test_s12_s13_exact_profit_tie_uses_stable_id_without_threshold():
    base = run_proposal(_request(quantity=7000, replies=[_finance()])).scenarios[-1]
    left = base.model_copy(update={"scenario_id": "B", "status": "EXECUTABLE"})
    right = base.model_copy(update={"scenario_id": "A", "status": "EXECUTABLE"})
    assert [item.scenario_id for item in rank_scenarios([left, right])] == ["A", "B"]
    right.contribution_margin_krw = Decimal("999999.999999")
    assert rank_scenarios([left, right])[0].scenario_id == "B"


def test_exact_profit_tie_uses_finance_soft_facts_before_sales_tiebreakers():
    base = run_proposal(_request(quantity=7000, replies=[_finance()])).scenarios[-1]
    inflow_dependent = base.model_copy(
        update={"scenario_id": "A", "status": "EXECUTABLE", "depends_on_projected_inflow": True}
    )
    self_funded = base.model_copy(
        update={"scenario_id": "B", "status": "EXECUTABLE", "depends_on_projected_inflow": False}
    )
    assert rank_scenarios([inflow_dependent, self_funded])[0].scenario_id == "B"


def test_s14_dominance_removes_only_same_terms_without_authority_advantage():
    base = run_proposal(_request(quantity=7000, replies=[_finance()])).scenarios[-1]
    better = base.model_copy(
        update={"scenario_id": "A", "status": "EXECUTABLE", "contribution_margin_krw": Decimal(10)}
    )
    worse = base.model_copy(
        update={
            "scenario_id": "B",
            "status": "CONDITIONAL",
            "contribution_margin_krw": Decimal(9),
            "execution_dependencies": ["FINANCE_REVALIDATION_REQUIRED"],
        }
    )
    kept, excluded = remove_dominated_scenarios([worse, better])
    assert [item.scenario_id for item in kept] == ["A"]
    assert excluded == {"B": ["DOMINATED_BY:A"]}


def _forecast(*, recommended=True):
    as_of = date(2026, 9, 1)
    return {
        "as_of": as_of,
        "item": "배추",
        "target_kind": "AUC",
        "unit": "원/kg",
        "current_price": 2000,
        "horizon_days": 2,
        "model_version": "TEST-ML",
        "generated_at": datetime(2026, 9, 1, tzinfo=UTC),
        "use_recommended": recommended,
        "daily": [
            {"date": as_of + timedelta(days=i), "predicted": 2100, "lower": 1900, "upper": 2200}
            for i in (1, 2)
        ],
    }


def test_s15_s16_ml_horizon_and_gate_are_non_blocking():
    outside_data = _request(quantity=7000).model_dump()
    gated_data = _request(quantity=7000).model_dump()
    outside_data["ml_context"] = _forecast()
    gated_data["ml_context"] = _forecast(recommended=False)
    outside_reply = run_proposal(SalesProposalInput.model_validate(outside_data))
    gated_reply = run_proposal(SalesProposalInput.model_validate(gated_data))
    assert all("ML_HORIZON_EXCEEDED" in item.uncertainties for item in outside_reply.scenarios)
    assert all(not item.ml_support_used for item in gated_reply.scenarios)


def test_s17_logistics_revalidation_forces_review():
    logistics = _reply(
        "logistics",
        "DELIVERY_FEASIBILITY_CONTEXT",
        "LOG-1",
        business="LOGISTICS_REVALIDATION_REQUIRED",
        payload={
            "reason_codes": ["LOGISTICS_REVALIDATION_REQUIRED"],
            "sell_priority": "HIGH",
            "inventory_risk_severity": "SEVERE",
            "remaining_freshness_days": -7,
        },
    )
    scenario = run_proposal(_request(quantity=7000, replies=[logistics, _finance()])).scenarios[-1]
    assert scenario.status == "REVIEW_REQUIRED"
    assert "DELIVERY_REVALIDATION_REQUIRED" in scenario.execution_dependencies
    assert scenario.sell_priority == "HIGH"
    assert scenario.authoritative_inventory_risk_severity == "SEVERE"
    assert scenario.remaining_freshness_days == -7


def test_s18_llm_cannot_override_deterministic_id(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setattr(
        "app.sales.llm.runtime._call_gemini",
        lambda *_args: LlmInterpretationOutput(
            recommended_candidate_id="B",
            summary="요약",
            recommendation_reason="근거",
            risk_explanation="위험",
            user_message="안내",
        ),
    )
    candidates = [
        SalesCandidate(candidate_id="A", allocation=[]),
        SalesCandidate(candidate_id="B", allocation=[]),
    ]
    result = interpret_candidates(candidates, recommended_candidate_id="A")
    assert result.recommended_candidate_id == "A"


def test_s19_conditional_can_be_recommended_and_s20_unresolved_cannot():
    conditional = run_proposal(_request(replies=[_purchase(1500), _finance()]))
    unresolved = run_proposal(_request())
    assert conditional.recommended_scenario_id is not None
    chosen = next(
        s for s in conditional.scenarios if s.scenario_id == conditional.recommended_scenario_id
    )
    assert chosen.status == "CONDITIONAL"
    assert unresolved.recommended_scenario_id is None


# ---------------------------------------------------------------------------
# 확정 물량의 재고 취득원가 — 판매는 **나르기만** 한다 (#1)
# ---------------------------------------------------------------------------


def _cost_basis(quantity="7000", amount="4547200"):
    return {
        "item": "배추",
        "quantity_kg": quantity,
        "amount_krw": amount,
        "allocation_method": "FEFO",
        "cost_method": "ACTUAL",
        "included_components": ["inventory_acquisition_cost"],
        "source_ref": "LOT-A",
        "source_refs": ["LOT-A", "LOT-B"],
        "evidence_grade": "SIM_FIXED",
    }


def _logistics_with_basis(basis, confirmed=7000):
    context = _logistics(confirmed)
    context["sellable_supply"]["inventory_cost_basis"] = basis
    return context


def test_물류가_낸_재고원가가_후보에_그대로_실린다():
    """🔴 **판매가 금액을 손대지 않는다.** 계보도 줄이지 않는다."""
    request = _request(logistics=_logistics_with_basis(_cost_basis()))

    후보 = {s.scenario_type: s for s in _generate_scenarios(request)}
    보수 = 후보["CONSERVATIVE"]

    # 확정 7,000kg 이 이 안의 물량이다 (요청 10,000 중 확정분).
    assert 보수.quantity_kg == Decimal(7000)
    assert 보수.inventory_cost_basis is not None
    assert 보수.inventory_cost_basis.amount_krw == Decimal(4547200)
    assert 보수.inventory_cost_basis.source_refs == ["LOT-A", "LOT-B"]
    assert 보수.inventory_cost_basis.cost_method == "ACTUAL"
    assert 보수.inventory_cost_basis.allocation_method == "FEFO"


def test_덮는_양이_확정_물량과_다르면_싣지_않는다():
    """🔴 모자란 원가를 실으면 재무가 그것을 «이 판매의 원가» 로 읽는다.

    ★ 비례 배분하지 않는다 — 없는 원가를 만드는 대신 **버린다.** 그러면 재무는
      원가를 못 받았다는 사실로 `RUNTIME_NOT_READY` 에서 멈춘다.
    """
    request = _request(logistics=_logistics_with_basis(_cost_basis(quantity="6999")))

    for scenario in _generate_scenarios(request):
        assert scenario.inventory_cost_basis is None


def test_품목이_다른_재고원가는_싣지_않는다():
    basis = _cost_basis()
    basis["item"] = "무"
    request = _request(logistics=_logistics_with_basis(basis))

    for scenario in _generate_scenarios(request):
        assert scenario.inventory_cost_basis is None


def test_물류가_원가를_안_내면_칸이_비어_있다():
    """없는 것을 0원으로 채우지 않는다."""
    request = _request(logistics=_logistics_with_basis(None))

    for scenario in _generate_scenarios(request):
        assert scenario.inventory_cost_basis is None


def test_재고원가는_재무_전선_이름_그대로_직렬화된다():
    """마스터는 후보를 통째로 나른다 — 재무 parser 가 읽는 이름이 그대로 있어야 한다."""
    request = _request(logistics=_logistics_with_basis(_cost_basis()))
    보수 = next(s for s in _generate_scenarios(request) if s.scenario_type == "CONSERVATIVE")

    wire = 보수.model_dump(by_alias=True, mode="json")

    assert wire["inventory_cost_basis"]["source_refs"] == ["LOT-A", "LOT-B"]
    assert wire["inventory_cost_basis"]["cost_method"] == "ACTUAL"
