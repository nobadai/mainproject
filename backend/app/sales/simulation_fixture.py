"""Deterministic 2026 confirmed Sales fixture for simulation walks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.sales.db import get_connection
from app.sales.persistence import SaleWriteResult, confirm_sale
from app.sales.schemas import SalesConfirmationInput

SIM_RUN_ID = "SIM-BURNIN-202512"
CUSTOMER_PARTNER_ID = "KIMCHI_FACTORY_001"
ITEM_NAME = "배추"
UNIT_PRICE_KRW = Decimal("2200")
MARGIN_RATE = Decimal("0.20")


@dataclass(frozen=True)
class ConfirmedSaleFixture:
    sale_date: date
    quantity_kg: Decimal
    identity_suffix: str | None = None


FIXTURE_SALES: tuple[ConfirmedSaleFixture, ...] = (
    ConfirmedSaleFixture(date(2026, 1, 5), Decimal("60")),
    ConfirmedSaleFixture(date(2026, 1, 7), Decimal("45")),
    ConfirmedSaleFixture(date(2026, 1, 9), Decimal("30")),
)

CAPACITY_RELIEF_SALES: tuple[ConfirmedSaleFixture, ...] = (
    ConfirmedSaleFixture(date(2026, 1, 5), Decimal("700"), "CAPACITY-01"),
    ConfirmedSaleFixture(date(2026, 1, 6), Decimal("650"), "CAPACITY-02"),
    ConfirmedSaleFixture(date(2026, 1, 7), Decimal("650"), "CAPACITY-03"),
    ConfirmedSaleFixture(date(2026, 1, 8), Decimal("600"), "CAPACITY-04"),
    ConfirmedSaleFixture(date(2026, 1, 9), Decimal("600"), "CAPACITY-05"),
)

ALL_CONFIRMED_SALES_FIXTURES: tuple[ConfirmedSaleFixture, ...] = (
    *FIXTURE_SALES,
    *CAPACITY_RELIEF_SALES,
)


def confirmed_sales_fixture_requests() -> list[SalesConfirmationInput]:
    return [_request_for(row) for row in ALL_CONFIRMED_SALES_FIXTURES]


def apply_confirmed_sales_fixture(conn: Any) -> list[SaleWriteResult]:
    return [confirm_sale(conn, request) for request in confirmed_sales_fixture_requests()]


def main() -> None:
    with get_connection() as conn:
        results = apply_confirmed_sales_fixture(conn)
    written = sum(result.sales_written for result in results)
    print(f"confirmed sales fixture applied: {written}/{len(results)} new sales")


def _request_for(row: ConfirmedSaleFixture) -> SalesConfirmationInput:
    identity = row.sale_date.isoformat()
    if row.identity_suffix is not None:
        identity = f"{row.identity_suffix}-{identity}"
    scenario_id = f"SIM-SALES-{identity}"
    run_id = f"SALES-FIXTURE-{identity}"
    amount = row.quantity_kg * UNIT_PRICE_KRW
    profit = amount * MARGIN_RATE
    return SalesConfirmationInput.model_validate(
        {
            "execution_identity": {
                "request_id": f"REQ-{run_id}",
                "run_id": run_id,
                "as_of": row.sale_date.isoformat(),
                "policy_version": "fixture-2026-sales",
                "feedback_attempt": 0,
            },
            "selected_scenario_id": scenario_id,
            "selected_scenario": {
                "scenario_id": scenario_id,
                "scenario_type": "CONSERVATIVE",
                "objective": "RISK_DEFENSE",
                "business_mode": "CONTRACT_FULFILLMENT",
                "item": ITEM_NAME,
                "partner_id": CUSTOMER_PARTNER_ID,
                "quantity_kg": str(row.quantity_kg),
                "unit_price_krw": str(UNIT_PRICE_KRW),
                "sales_amount_krw": str(amount),
                "delivery_date": row.sale_date.isoformat(),
                "payment_days": 30,
                "payment_terms_type": "SINGLE",
                "contract_term_days": 90,
                "source_ref": "SALES_SIMULATION_FIXTURE_2026",
                "supply": {
                    "confirmed_quantity_kg": str(row.quantity_kg),
                    "required_additional_quantity_kg": "0",
                    "additional_supply_required": False,
                },
                "sales_decision_axes": ["FIXTURE"],
                "required_validations": [],
                "evidence_refs": ["SALES_SIMULATION_FIXTURE_2026"],
                "rationale": ["2026 Master walk 관통 검증용 확정 판매 fixture"],
                "risks": [],
                "uncertainties": [],
                "conditional_purchase": False,
                "variant_collapsed": False,
                "variant_collapsed_reason": None,
                "domain_replies": [],
                "status": "EXECUTABLE",
                "execution_dependencies": [],
                "unmet_quantity_kg": "0",
                "finance_verdict": "PASS",
                "contribution_margin_krw": str(profit),
                "contribution_margin_rate": str(MARGIN_RATE),
                "sell_priority": "LOW",
            },
            "sim_run_id": SIM_RUN_ID,
            "sale_date": row.sale_date.isoformat(),
            "order_date": row.sale_date.isoformat(),
            "source_order_id": f"SIM-ORDER-{identity}",
            "note": "2026 confirmed sales simulation fixture",
            "line": {
                "item_name": ITEM_NAME,
                "quantity_kg": str(row.quantity_kg),
                "unit_price_krw_per_kg": str(UNIT_PRICE_KRW),
                "grade": None,
                "contribution_profit_krw": str(profit),
                "contribution_margin_rate": str(MARGIN_RATE),
            },
        }
    )


if __name__ == "__main__":
    main()
