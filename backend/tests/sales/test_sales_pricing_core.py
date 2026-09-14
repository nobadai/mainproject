"""Deterministic price strategies consume only Finance policy and delivered facts."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.finance.sales_policy import (
    FINANCE_SALES_MVP_POLICY_REF,
    load_finance_sales_mvp_policy,
)
from app.sales.proposal import _generate_scenarios
from app.sales.schemas import SalesProposalInput

AS_OF = date(2026, 1, 1)
DELIVERY = date(2026, 1, 2)


def _request(
    *,
    source_ref="sim_runs/SIM-1#sales_terms/ML_CURRENT_PRICE",
    preferred_price="1400",
    business_mode="SPOT_SALES",
    contract=None,
    basis=True,
    recommended=True,
    lower=1350,
    predicted=1450,
    upper=1600,
    priority="HIGH",
    severity="SEVERE",
    feedback=True,
):
    payload = {
        "business_mode": business_mode,
        "user_request": {
            "item": "배추",
            "partner_id": "P-1",
            "requested_quantity_kg": 100,
            "preferred_unit_price_krw": preferred_price,
            "preferred_delivery_date": DELIVERY,
            "source_ref": source_ref,
        },
        "ml_context": {
            "as_of": AS_OF,
            "item": "배추",
            "target_kind": "WHSL",
            "unit": "원/kg",
            "current_price": 1400,
            "horizon_days": 1,
            "model_version": "TEST-WHSL",
            "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
            "use_recommended": recommended,
            "daily": [{"date": DELIVERY, "lower": lower, "predicted": predicted, "upper": upper}],
        },
        "logistics_context": {
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 100}],
                "supply_capacity_by_date": [
                    {"date": DELIVERY, "confirmed_sellable_quantity_kg": 100}
                ],
                **(
                    {
                        "inventory_cost_basis": {
                            "item": "배추",
                            "quantity_kg": 100,
                            "amount_krw": 100000,
                            "allocation_method": "FEFO",
                            "cost_method": "ACTUAL",
                            "source_ref": "LOT-1",
                            "source_refs": ["LOT-1"],
                            "evidence_grade": "SIM_FIXED",
                        }
                    }
                    if basis
                    else {}
                ),
            },
            "delivery_feasibility": {"status": "READY", "earliest_delivery_date": DELIVERY},
        },
    }
    if feedback:
        payload["feedback"] = {
            "domain_replies": [
                {
                    "source_agent": "logistics",
                    "capability": "DELIVERY_FEASIBILITY_CONTEXT",
                    "reply_ref": "LOG-1",
                    "runtime_status": "READY",
                    "business_status": "ok",
                    "payload": {
                        "sell_priority": priority,
                        "inventory_risk_severity": severity,
                    },
                }
            ],
            "scenario_feedback": [
                {"scenario_id": f"SALES-001-{suffix}", "reply_refs": ["LOG-1"]}
                for suffix in ("A", "B", "C")
            ],
        }
    if contract is not None:
        payload["contract_context"] = contract
    return SalesProposalInput.model_validate(payload)


def _prices(request):
    return {scenario.scenario_type: scenario for scenario in _generate_scenarios(request)}


def test_market_margin_and_depletion_make_a_b_c_prices_distinct():
    scenarios = _prices(_request())

    assert scenarios["CONSERVATIVE"].unit_price_krw == Decimal(1600)
    assert scenarios["BALANCED"].unit_price_krw == Decimal(1450)
    assert scenarios["AGGRESSIVE"].unit_price_krw == Decimal(1360)
    assert scenarios["CONSERVATIVE"].sales_amount_krw == Decimal(160000)
    assert scenarios["BALANCED"].sales_amount_krw == Decimal(145000)
    assert scenarios["AGGRESSIVE"].sales_amount_krw == Decimal(136000)
    assert all("PRICE" in scenario.sales_decision_axes for scenario in scenarios.values())
    assert FINANCE_SALES_MVP_POLICY_REF in scenarios["AGGRESSIVE"].evidence_refs


def test_no_depletion_collapses_aggressive_price_to_balanced():
    scenarios = _prices(_request(priority="LOW", severity="NORMAL"))

    assert scenarios["AGGRESSIVE"].unit_price_krw == scenarios["BALANCED"].unit_price_krw
    assert scenarios["AGGRESSIVE"].unit_price_krw == Decimal(1450)


@pytest.mark.parametrize(
    ("kind", "kwargs", "expected"),
    [
        ("aggressive", {"lower": 1200}, Decimal(1360)),
        ("balanced", {"lower": 1200, "predicted": 1300}, Decimal(1360)),
        ("conservative", {"lower": 1200, "predicted": 1300, "upper": 1400}, Decimal(1429)),
    ],
)
def test_margin_guard_never_rounds_below_the_finance_floor(kind, kwargs, expected):
    scenarios = _prices(_request(**kwargs))
    key = {"aggressive": "AGGRESSIVE", "balanced": "BALANCED", "conservative": "CONSERVATIVE"}[kind]

    assert scenarios[key].unit_price_krw == expected


@pytest.mark.parametrize(
    ("source_ref", "mode", "contract"),
    [
        ("USER-REQ:1", "SPOT_SALES", None),
        ("sim_runs/SIM-1#sales_terms/FIXED", "SPOT_SALES", None),
        (
            "CONTRACT:C-1",
            "CONTRACT_FULFILLMENT",
            {
                "item": "배추",
                "partner_id": "P-1",
                "contract_quantity_kg": 100,
                "contract_unit_price_krw": 2100,
                "contract_delivery_date": DELIVERY,
                "source_ref": "CONTRACT:C-1",
            },
        ),
    ],
)
def test_contract_user_and_fixed_prices_are_locked(source_ref, mode, contract):
    scenarios = _prices(_request(source_ref=source_ref, business_mode=mode, contract=contract))
    expected = Decimal(2100) if contract else Decimal(1400)

    assert {scenario.unit_price_krw for scenario in scenarios.values()} == {expected}
    assert all("PRICE" not in scenario.sales_decision_axes for scenario in scenarios.values())


def test_no_cost_uses_market_only_and_unrecommended_band_is_ignored():
    market_only = _prices(_request(basis=False))
    unavailable = _prices(_request(recommended=False))

    strategies = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")
    assert [market_only[key].unit_price_krw for key in strategies] == [
        Decimal(1600),
        Decimal(1450),
        Decimal(1350),
    ]
    assert [unavailable[key].unit_price_krw for key in strategies] == [
        Decimal(1429),
        Decimal(1400),
        Decimal(1360),
    ]


def test_cost_only_and_no_authority_both_preserve_their_defined_fallbacks():
    cost_only_request = _request()
    cost_only = _prices(cost_only_request.model_copy(update={"ml_context": None}))
    no_authority_request = _request(basis=False)
    no_authority = _prices(no_authority_request.model_copy(update={"ml_context": None}))
    strategies = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")

    assert [cost_only[key].unit_price_krw for key in strategies] == [
        Decimal(1429),
        Decimal(1400),
        Decimal(1360),
    ]
    assert {no_authority[key].unit_price_krw for key in strategies} == {Decimal(1400)}


def test_non_whsl_forecast_is_not_a_sales_market_corridor():
    request = _request()
    forecast = request.ml_context.model_copy(update={"target_kind": "AUC"})
    scenarios = _prices(request.model_copy(update={"ml_context": forecast}))

    assert scenarios["CONSERVATIVE"].unit_price_krw == Decimal(1429)
    assert scenarios["BALANCED"].unit_price_krw == Decimal(1400)
    assert scenarios["AGGRESSIVE"].unit_price_krw == Decimal(1360)


def test_sales_and_finance_use_the_same_immutable_policy_loader():
    first = load_finance_sales_mvp_policy()
    second = load_finance_sales_mvp_policy()

    assert first is second
    assert first.finance_minimum_margin_rate == Decimal("0.2642")
    assert first.finance_warning_margin_rate == Decimal("0.30")


def _renewal_contract():
    return {
        "item": "배추",
        "partner_id": "P-1",
        "contract_quantity_kg": 100,
        "contract_unit_price_krw": 2100,
        "contract_delivery_date": DELIVERY,
        "contract_payment_days": 30,
        "source_ref": "CONTRACT:C-1",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"requested_quantity_kg": 80},
        {"preferred_delivery_date": date(2026, 1, 3)},
        {"preferred_payment_days": 45},
    ],
)
def test_renewal_non_price_override_keeps_the_inherited_contract_price_locked(overrides):
    request = _request(
        business_mode="CONTRACT_PROPOSAL_RENEWAL",
        preferred_price=None,
        source_ref=None,
        contract=_renewal_contract(),
    )
    user = request.user_request.model_dump()
    user.update(overrides)
    payload = request.model_dump()
    payload["user_request"] = user
    scenarios = _prices(SalesProposalInput.model_validate(payload))

    assert {scenario.unit_price_krw for scenario in scenarios.values()} == {Decimal(2100)}
    assert all("PRICE" not in scenario.sales_decision_axes for scenario in scenarios.values())


def test_renewal_explicit_user_price_is_locked():
    scenarios = _prices(
        _request(
            business_mode="CONTRACT_PROPOSAL_RENEWAL",
            preferred_price=2300,
            source_ref="USER-REQ:renewal-1",
            contract=_renewal_contract(),
        )
    )

    assert {scenario.unit_price_krw for scenario in scenarios.values()} == {Decimal(2300)}


def test_renewal_explicit_market_price_keeps_existing_market_variant_semantics():
    scenarios = _prices(
        _request(
            business_mode="CONTRACT_PROPOSAL_RENEWAL",
            preferred_price=1400,
            source_ref="sim_runs/SIM-1#sales_terms/ML_CURRENT_PRICE",
            contract=_renewal_contract(),
        )
    )

    assert [
        scenarios[key].unit_price_krw for key in ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")
    ] == [Decimal(1600), Decimal(1450), Decimal(1360)]


def test_first_pass_pre_sales_context_has_no_depletion_authority_to_discount_aggressive_price():
    """PRE_SALES payload는 feedback reply가 아니라 SalesLogisticsContext 그대로다."""
    scenarios = _prices(_request(feedback=False))

    assert scenarios["AGGRESSIVE"].unit_price_krw == scenarios["BALANCED"].unit_price_krw
    assert scenarios["AGGRESSIVE"].unit_price_krw == Decimal(1450)
    assert all(scenario.sell_priority is None for scenario in scenarios.values())
