from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.finance.capabilities.scenario import _scenario_schedule
from app.finance.db import _build_finance_policy
from app.finance.execution import _indexed_verdict_evidence
from app.finance.rules import classify_base_stress
from app.finance.schemas import CashEvent
from app.finance.tools import build_payroll_schedule, derive_critical_payment_dates


def policy_rows(*, payroll_date: Decimal | None = Decimal(10)) -> list[dict[str, object]]:
    values = {
        "purchase_payment_days": ("NUMERIC", Decimal(7)),
        "payroll_date": ("NUMERIC", payroll_date),
        "monthly_labor_cost_krw": ("NUMERIC", Decimal(12941280)),
        "minimum_cash_balance_krw": ("NUMERIC", Decimal(12941280)),
        "cashflow_projection_days": ("NUMERIC", Decimal(30)),
        "cash_priority_reference": ("TEXT", "minimum_cash_balance_krw"),
        "cash_priority_high_ratio": ("NUMERIC", Decimal(1)),
        "cash_priority_medium_ratio": ("NUMERIC", Decimal("1.5")),
    }
    return [
        {
            "policy_key": key,
            "value_kind": kind,
            "value_numeric": value if kind == "NUMERIC" else None,
            "value_text": value if kind == "TEXT" else None,
            "value_json": None,
            "source_ref": f"db-policy:{key}",
            "policy_version": "v1.3-PROVISIONAL",
            "usage_scope": "AGENT_MVP_DEMO",
        }
        for key, (kind, value) in values.items()
    ]


def test_payroll_date_is_required_db_policy_with_source_and_drives_event():
    policy = _build_finance_policy(policy_rows(payroll_date=Decimal(17)))
    assert policy.payroll_date == 17
    assert policy.source_refs["payroll_date"] == "db-policy:payroll_date"
    event = build_payroll_schedule(
        as_of=date(2026, 8, 27), horizon_end=date(2026, 9, 26), policy=policy
    )[0]
    assert event.event_date == date(2026, 9, 17)
    assert event.source_ref == "db-policy:monthly_labor_cost_krw"
    assert event.schedule_source_ref == "db-policy:payroll_date"


@pytest.mark.parametrize("missing_key", ["monthly_labor_cost_krw", "payroll_date"])
def test_payroll_lineage_source_ref가_없으면_실패한다(missing_key):
    policy = _build_finance_policy(policy_rows())
    source_refs = dict(policy.source_refs)
    source_refs.pop(missing_key)
    policy = policy.model_copy(update={"source_refs": source_refs})
    with pytest.raises(ValueError, match=missing_key):
        build_payroll_schedule(
            as_of=date(2026, 8, 27), horizon_end=date(2026, 9, 26), policy=policy
        )


def test_missing_or_non_integer_payroll_date_fails_closed():
    with pytest.raises((LookupError, ValueError, ValidationError)):
        _build_finance_policy(
            [row for row in policy_rows() if row["policy_key"] != "payroll_date"]
        )
    with pytest.raises(ValueError, match="integer"):
        _build_finance_policy(policy_rows(payroll_date=Decimal("10.5")))


def outflow(day: int, amount: int, *, direction: str = "OUTFLOW") -> CashEvent:
    return CashEvent(
        event_date=date(2026, 9, day), event_type="COMMITTED_OUTFLOW",
        amount_krw=amount, direction=direction, ref_id=f"event-{day}-{amount}-{direction}",
    )


def test_critical_dates_use_outflow_max_ties_and_minimum_cash_violations_only():
    dates = derive_critical_payment_dates(
        current_cash_krw=Decimal(100), minimum_cash_balance_krw=Decimal(50),
        cash_events=[
            outflow(3, 30), outflow(4, 30), outflow(5, 5, direction="INFLOW"), outflow(6, 10),
        ],
    )
    assert dates == (date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 6))
    assert date(2026, 9, 5) not in dates


def split_scenario(**updates: object) -> dict[str, object]:
    scenario: dict[str, object] = {
        "scenario_id": "S1", "total_qty_kg": 3, "total_amount_krw": 300,
        "max_price": 120,
        "split_plan": [
            {"seq": 1, "date": "2026-08-27", "qty_kg": 1},
            {"seq": 2, "date": "2026-08-28", "qty_kg": 2},
        ],
        "payment_schedule": [
            {"seq": 1, "purchase_date": "2026-08-27", "payment_date": "2026-09-03",
             "qty_kg": 1, "amount_krw": 100, "amount_max_krw": 120, "basis": "spot"},
            {"seq": 2, "purchase_date": "2026-08-28", "payment_date": "2026-09-04",
             "qty_kg": 2, "amount_krw": 200, "amount_max_krw": 240, "basis": "spot"},
        ],
    }
    scenario.update(updates)
    return scenario


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s: s.update(total_qty_kg=4), "qty sum"),
        (lambda s: s.update(total_amount_krw=301), "amount sum"),
        (lambda s: s["payment_schedule"][0].update(purchase_date="2026-08-26"), "purchase_date"),
        (lambda s: s["payment_schedule"][0].update(qty_kg=2), "qty_kg"),
        (lambda s: s["payment_schedule"][0].update(payment_date="2026-09-02"), "policy days"),
        (lambda s: s["payment_schedule"][0].update(amount_max_krw=121), "max_price"),
        (lambda s: s["payment_schedule"][0].update(basis=""), "basis"),
    ],
)
def test_split_payment_schedule_contract_rejects_mismatches(mutation, message):
    scenario = split_scenario()
    mutation(scenario)
    with pytest.raises(ValueError, match=message):
        _scenario_schedule(
            scenario=scenario, as_of=date(2026, 8, 27), horizon=date(2026, 9, 30),
            default_payment_days=7,
        )


def test_h1_authoritative_payment_date_and_amount_are_preserved():
    scenario = split_scenario(authoritative_h1_payment_data=True)
    scenario["payment_schedule"][0].update(payment_date="2026-09-08", amount_max_krw=111)
    schedule = _scenario_schedule(
        scenario=scenario, as_of=date(2026, 8, 27), horizon=date(2026, 9, 30),
        default_payment_days=7,
    )
    assert schedule[0].payment_date == date(2026, 9, 8)
    assert schedule[0].amount_max_krw == 111


@pytest.mark.parametrize(
    ("base_safe", "stress_safe", "expected"),
    [(True, True, "ok"), (True, False, "conditional"), (False, False, "reject")],
)
def test_base_stress_verdict_matrix(base_safe, stress_safe, expected):
    assert classify_base_stress(base_safe=base_safe, stress_safe=stress_safe) == expected


def test_base_unsafe_stress_safe_is_validation_error():
    with pytest.raises(ValueError, match="invalid"):
        classify_base_stress(base_safe=False, stress_safe=True)


def evidence(claim: str, value: float | None) -> dict[str, object]:
    return {
        "claim": claim, "source": "tool_calc", "ref_ids": ["FIN:S1"], "value": value,
        "unit": "krw", "evidence_grade": "OFFICIAL", "evidence_detail": None,
    }


@pytest.mark.parametrize("results", [[], [{"scenario_id": "S1", "evidences": []}]])
def test_indexed_verdict_evidence_supports_zero_and_one_item(results):
    assert _indexed_verdict_evidence(results) == []


def test_indexed_verdict_evidence_uses_paths_and_skips_none_without_orphans():
    results = [
        {"finance_cap_amount_krw": 0, "scenario_projected_cash_min": None,
         "evidences": [evidence("finance_cap_amount_krw", 0)]},
        {"candidate_amount_krw": 25, "evidences": [evidence("candidate_amount_krw", 25)]},
    ]
    claims = [item.claim for item in _indexed_verdict_evidence(results)]
    assert claims == ["verdicts[0].finance_cap_amount_krw", "verdicts[1].candidate_amount_krw"]
