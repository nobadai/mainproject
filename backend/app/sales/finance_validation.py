"""Sales Scenario -> Finance validation payload projection."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, NamedTuple

from app.sales.schemas import SalesScenario

FINANCE_REQUIRED_FIELDS: tuple[str, ...] = (
    "scenario_id",
    "partner_id",
    "item",
    "quantity_kg",
    "unit_price_krw",
    "reported_sales_amount_krw",
    "payment_terms_type",
    "source_ref",
)
OWNED_BUT_OPTIONAL_FINANCE_FIELDS: tuple[str, ...] = ("payment_terms_type", "source_ref")


class FinanceValidationProjection(NamedTuple):
    payload: dict[str, Any]
    unresolved: tuple[str, ...]


def build_financial_validation_request(
    scenario: SalesScenario,
) -> FinanceValidationProjection:
    payload: dict[str, Any] = {"item": scenario.item, "scenario_id": scenario.scenario_id}
    unresolved: list[str] = []

    if scenario.partner_id is not None:
        payload["partner_id"] = scenario.partner_id
    else:
        unresolved.append("partner_id")

    for source, target in (
        ("quantity_kg", "quantity_kg"),
        ("unit_price_krw", "unit_price_krw"),
        ("sales_amount_krw", "reported_sales_amount_krw"),
    ):
        value = getattr(scenario, source)
        if value is None:
            unresolved.append(target)
        else:
            payload[target] = _plain(value)

    if scenario.payment_days is not None:
        payload["payment_days"] = scenario.payment_days

    for field in OWNED_BUT_OPTIONAL_FINANCE_FIELDS:
        value = getattr(scenario, field)
        if value is None:
            unresolved.append(field)
        else:
            payload[field] = value

    supply = _supply(scenario)
    if supply is not None:
        payload["supply"] = supply

    return FinanceValidationProjection(payload, tuple(unresolved))


def build_financial_validation_batch(
    scenarios: list[SalesScenario],
) -> FinanceValidationProjection:
    projections = [build_financial_validation_request(scenario) for scenario in scenarios]
    unresolved: list[str] = []
    for projection in projections:
        for field in projection.unresolved:
            if field not in unresolved:
                unresolved.append(field)
    return FinanceValidationProjection(
        {"scenarios": [projection.payload for projection in projections]},
        tuple(unresolved),
    )


def _supply(scenario: SalesScenario) -> dict[str, Any] | None:
    confirmed = scenario.supply.confirmed_quantity_kg
    if confirmed is None:
        return None
    supply: dict[str, Any] = {"confirmed_quantity_kg": _plain(confirmed)}
    conditional = scenario.supply.conditional_quantity_kg
    if conditional is not None:
        supply["conditional_quantity_kg"] = _plain(conditional)
        if scenario.supply.dependency_ref is not None:
            supply["dependency_ref"] = scenario.supply.dependency_ref
    return supply


def _plain(value: Decimal) -> str:
    return str(value)
