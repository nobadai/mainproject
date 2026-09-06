"""Finance Sales P0 v1.7 회귀 — TEST FIXTURE only."""

from decimal import Decimal

import pytest

from app.finance.capabilities.sales import evaluate_sales_scenario, parse_sales_validation_input
from app.finance.collection import (
    FinanceCollectionConflict,
    apply_cumulative_collection,
    build_collection_transition,
)
from app.finance.sales_models import (
    ConditionalSupplyCostBasis,
    InventoryCostBasis,
    VerifiedDirectCost,
)
from app.finance.tools import compose_sales_cost_basis


def _payload(*, conditional_cost=...):
    payload = {
        "scenario_id": "TEST-SC-1",
        "partner_id": "TEST-P-1",
        "item": "배추",
        "quantity_kg": "8500",
        "unit_price_krw": "2300",
        "reported_sales_amount_krw": "19550000",
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": "2026-09-10",
        "supply": {
            "confirmed_quantity_kg": "7000",
            "conditional_quantity_kg": "1500",
            "dependency_ref": "TEST:PUR-1",
        },
        "inventory_cost_basis": {
            "amount_krw": "12600000",
            "cost_method": "ACTUAL",
            "source_ref": "TEST:INV-1",
            "evidence_grade": "OFFICIAL",
            "included_components": ["confirmed_handling"],
        },
        "direct_costs": [
            {
                "component": "outbound_transport",
                "amount_krw": "100000",
                "cost_method": "ACTUAL",
                "source_ref": "TEST:LOG-1",
                "evidence_grade": "OFFICIAL",
            }
        ],
        "source_ref": "TEST:SALES-1",
    }
    if conditional_cost is not ...:
        payload["conditional_supply_cost_basis"] = conditional_cost
    return payload


def _evaluate(payload):
    return evaluate_sales_scenario(
        payload,
        finance_minimum_margin_rate=Decimal("0.01"),
        finance_warning_margin_rate=Decimal("0.02"),
        max_finance_allowed_payment_terms_days=45,
    )


def test_f03_conditional_supply_without_authoritative_cost_fails_closed():
    result = _evaluate(_payload())
    assert result.financial_summary is not None
    assert result.financial_summary.contribution_margin_krw is None
    assert "sales_cost_basis_for_conditional_supply" in result.missing_data


def test_f04_conditional_authoritative_cost_composes_margin_and_lineage():
    result = _evaluate(
        _payload(
            conditional_cost={
                "amount_krw": "3000000",
                "cost_method": "ACTUAL",
                "source_ref": "TEST:PUR-COST-1",
                "evidence_grade": "OFFICIAL",
                "included_components": ["conditional_handling"],
            }
        )
    )
    assert result.financial_summary is not None
    assert result.financial_summary.sales_cost_basis_krw == Decimal(15700000)
    assert result.financial_summary.contribution_margin_krw == Decimal(3850000)
    assert "TEST:INV-1" in result.evidence_refs
    assert "TEST:PUR-COST-1" in result.evidence_refs
    assert "TEST:LOG-1" in result.evidence_refs


def test_finance_parser_accepts_conditional_cost_and_ignores_unowned_extra_fields():
    payload = _payload(
        conditional_cost={
            "amount_krw": "3000000",
            "cost_method": "ACTUAL",
            "source_ref": "TEST:PUR-COST-1",
            "evidence_grade": "OFFICIAL",
        }
    )
    payload["future_sales_field"] = {"untouched": True}
    parsed, missing = parse_sales_validation_input(payload)
    assert missing == ()
    assert parsed is not None
    assert parsed.conditional_supply_cost_basis is not None
    assert parsed.conditional_supply_cost_basis.amount_krw == Decimal(3000000)


def test_f05_direct_cost_in_either_basis_is_not_counted_twice():
    inventory = InventoryCostBasis(
        amount_krw=Decimal(100),
        cost_method="ACTUAL",
        source_ref="TEST:INV",
        evidence_grade="OFFICIAL",
    )
    conditional = ConditionalSupplyCostBasis(
        amount_krw=Decimal(40),
        cost_method="ACTUAL",
        included_components=("outbound_transport",),
        source_ref="TEST:PUR",
        evidence_grade="OFFICIAL",
    )
    direct = VerifiedDirectCost(
        component="outbound_transport",
        amount_krw=Decimal(9),
        cost_method="ACTUAL",
        source_ref="TEST:LOG",
        evidence_grade="OFFICIAL",
    )
    result = compose_sales_cost_basis(
        inventory_cost_basis=inventory,
        conditional_supply_cost_basis=conditional,
        direct_costs=[direct],
    )
    assert result is not None
    assert result.amount_krw == Decimal(140)
    assert result.already_included_components == ("outbound_transport",)


def _receivable(received="0"):
    received_value = Decimal(received)
    return {
        "receivable_id": "TEST-AR-1",
        "sim_run_id": "TEST-SIM",
        "original_amount_krw": Decimal(10000000),
        "received_amount_krw": received_value,
        "outstanding_amount_krw": Decimal(10000000) - received_value,
        "status": "OPEN" if received_value == 0 else "PARTIAL",
    }


def _state(cash="20000000", receivables="10000000"):
    return {
        "finance_state_id": "TEST-FIN-1",
        "sim_run_id": "TEST-SIM",
        "current_cash_krw": Decimal(cash),
        "receivables_krw": Decimal(receivables),
    }


def test_f10_partial_collection_updates_ar_and_cash_without_rounding():
    plan = build_collection_transition(
        _receivable(), _state(), target_received_total_krw=Decimal("4000000.002760")
    )
    assert plan.delta_received_krw == Decimal("4000000.002760")
    assert plan.next_outstanding_amount_krw == Decimal("5999999.997240")
    assert plan.next_current_cash_krw == Decimal("24000000.002760")
    assert plan.next_receivables_krw == Decimal("5999999.997240")
    assert plan.next_status == "PARTIAL"


def test_f12_full_collection_and_f13_f14_invalid_targets():
    full = build_collection_transition(
        _receivable("4000000"),
        _state(cash="24000000", receivables="6000000"),
        target_received_total_krw="10000000",
    )
    assert full.next_status == "COLLECTED"
    assert full.next_outstanding_amount_krw == 0
    with pytest.raises(FinanceCollectionConflict):
        build_collection_transition(_receivable(), _state(), target_received_total_krw="10000001")
    with pytest.raises(FinanceCollectionConflict):
        build_collection_transition(
            _receivable("4000000"), _state(), target_received_total_krw="3999999"
        )
    with pytest.raises(FinanceCollectionConflict):
        build_collection_transition(_receivable(), _state(), target_received_total_krw="-1")


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.current = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        text = str(query)
        self.rowcount = 0
        if "SELECT * FROM" in text and "finance_states" in text:
            self.current = self.conn.state
        elif "SELECT * FROM" in text and "receivables" in text:
            self.current = self.conn.receivable
        elif "UPDATE" in text and "finance_states" in text:
            cash, receivables, _identifier = params
            self.conn.state.update(current_cash_krw=cash, receivables_krw=receivables)
            self.rowcount = 1
            self.conn.update_count += 1
        elif "UPDATE" in text and "receivables" in text:
            target, outstanding, status, _identifier = params
            self.conn.receivable.update(
                received_amount_krw=target,
                outstanding_amount_krw=outstanding,
                status=status,
            )
            self.rowcount = 1
            self.conn.update_count += 1
        else:  # pragma: no cover - 새로운 SQL은 fake 계약도 함께 갱신해야 한다.
            raise AssertionError(text)

    def fetchone(self):
        return self.current


class _Connection:
    def __init__(self):
        self.receivable = _receivable()
        self.state = _state()
        self.update_count = 0

    def cursor(self):
        return _Cursor(self)


def test_f11_cumulative_collection_is_idempotent_in_caller_transaction(monkeypatch):
    monkeypatch.setattr("app.finance.collection.get_db_schema", lambda: "test_schema")
    conn = _Connection()
    first = apply_cumulative_collection(
        conn,
        receivable_id="TEST-AR-1",
        finance_state_id="TEST-FIN-1",
        target_received_total_krw="4000000",
    )
    second = apply_cumulative_collection(
        conn,
        receivable_id="TEST-AR-1",
        finance_state_id="TEST-FIN-1",
        target_received_total_krw="4000000",
    )
    assert first.delta_received_krw == Decimal(4000000)
    assert second.delta_received_krw == 0
    assert conn.state["current_cash_krw"] == Decimal(24000000)
    assert conn.update_count == 2
