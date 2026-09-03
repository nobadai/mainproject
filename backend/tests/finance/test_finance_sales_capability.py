"""Finance Sales Core Phase 6 — 판매 재무 내부 검증 Capability.

★ 이 파일이 지키는 것은 **못 한 일을 성공처럼 보이지 않게 하는 것**이다.
    · 제안에 사실이 빠지면 INPUT_INCOMPLETE 이고 Finance 는 멀쩡하다
    · Finance 정책이 없으면 RUNTIME_NOT_READY 이고 FAIL 이 아니다
    · 조건부 물량을 확정 재고원가로 덮지 않는다
    · 못 구한 값은 0이 아니라 None 으로 남는다
  임계값 숫자는 전부 시험 픽스처다 — 저장소의 권위 있는 정책이 아니다.
"""

from datetime import date
from decimal import Decimal

from app.finance.capabilities.sales import (
    evaluate_sales_scenario,
    parse_sales_validation_input,
)
from app.finance.sales_models import PartnerReceivable
from app.finance.tools import (
    build_proposed_sales_collection_event,
    project_sales_scenario_cashflow,
    summarize_partner_receivables,
)

AS_OF = date(2026, 3, 1)
HORIZON = date(2026, 6, 1)

# 시험 픽스처 — 저장소의 권위 있는 정책이 아니다.
MIN_RATE = Decimal("0.10")
WARN_RATE = Decimal("0.20")
MAX_DAYS = 45
MIN_CASH = Decimal(1_000_000)
CREDIT_LIMIT = Decimal(50_000_000)


def _payload(**overrides):
    payload = {
        "scenario_id": "SC-001",
        "partner_id": "P-100",
        "item": "red_pepper",
        "quantity_kg": Decimal(100),
        "unit_price_krw": Decimal(10_000),
        "reported_sales_amount_krw": Decimal(1_000_000),
        "payment_terms_type": "SINGLE",
        "payment_days": 30,
        "collection_reference_date": date(2026, 3, 10),
        "source_ref": "SALES-REPLY:R-9",
        "inventory_cost_basis": {
            "amount_krw": Decimal(700_000),
            "cost_method": "ACTUAL",
            "source_ref": "INV-LOT:L-1",
            "evidence_grade": "OFFICIAL",
        },
        # 전부 확정된 제안이다. 조건부 0 은 **명시된 사실**이라 마진을 낼 수 있다 —
        # 공급을 아예 안 보내는 경우(모름)와 다르다.
        "supply": {
            "confirmed_quantity_kg": Decimal(100),
            "conditional_quantity_kg": Decimal(0),
        },
        # Sales 가 Finance 와 무관한 키를 더 실어 보내도 통과해야 한다.
        "objective": "MAXIMIZE_MARGIN",
        "adjustment_axes": ["channel_mix"],
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _ABSENT}


class _Absent:
    pass


_ABSENT = _Absent()


def _receivables(*items):
    return summarize_partner_receivables(
        partner_id="P-100", as_of=AS_OF, receivables=list(items)
    )


def _receivable(ref="R-1", amount="2000000", due=date(2026, 4, 1), status="OPEN"):
    return PartnerReceivable(
        receivable_id=ref,
        due_date=due,
        outstanding_amount_krw=Decimal(amount),
        status=status,
        source_ref=f"RECEIVABLE:{ref}",
    )


def _cashflow(collection=date(2026, 4, 9), amount="1000000"):
    return project_sales_scenario_cashflow(
        as_of=AS_OF,
        current_cash_krw=Decimal(10_000_000),
        horizon_end=HORIZON,
        base_cash_events=[],
        proposed_collection=build_proposed_sales_collection_event(
            proposal_ref="SC-001",
            collection_date=collection,
            sales_amount_krw=Decimal(amount),
            source_ref="SALES-REPLY:R-9",
        ),
    )


def _rule_of(result, rule_id):
    return next(item for item in result.rule_results if item["rule_id"] == rule_id)


def _evaluate(payload=None, **overrides):
    kwargs = {
        "finance_minimum_margin_rate": MIN_RATE,
        "finance_warning_margin_rate": WARN_RATE,
        "max_finance_allowed_payment_terms_days": MAX_DAYS,
        "minimum_cash_balance_krw": MIN_CASH,
        "credit_limit_krw": CREDIT_LIMIT,
        "receivable_facts": _receivables(_receivable()),
        "scenario_cashflow": _cashflow(),
        "collection_risk_mode": None,
    }
    kwargs.update(overrides)
    return evaluate_sales_scenario(payload if payload is not None else _payload(), **kwargs)


# ---------------------------------------------------------------------------
# 1. 완전 계산 가능
# ---------------------------------------------------------------------------


def test_fully_calculable_case_produces_every_fact():
    result = _evaluate()
    summary = result.financial_summary

    assert summary is not None
    assert summary.recalculated_sales_amount_krw == Decimal(1_000_000)
    assert summary.amount_match is True
    assert summary.sales_cost_basis_krw == Decimal(700_000)
    assert summary.contribution_margin_krw == Decimal(300_000)
    assert summary.contribution_margin_rate == Decimal("0.3")
    assert summary.collection_date == date(2026, 4, 9)
    assert summary.projected_partner_ar_krw == Decimal(3_000_000)
    assert summary.available_credit_krw == Decimal(48_000_000)


def test_collection_risk_policy_absence_keeps_the_aggregate_verdict_open():
    # 회수위험 정책이 없으므로 종합 판정은 아직 닫힌다 — 이것이 오늘의 사실이다.
    result = _evaluate()

    assert result.runtime_status == "RUNTIME_NOT_READY"
    assert result.finance_verdict is None
    assert "sales_collection_risk_policy" in result.missing_data


# ---------------------------------------------------------------------------
# 2. 금액 불일치
# ---------------------------------------------------------------------------


def test_amount_mismatch_is_recorded_with_exact_difference():
    result = _evaluate(_payload(reported_sales_amount_krw=Decimal(900_000)))
    summary = result.financial_summary

    assert summary is not None
    assert summary.amount_match is False
    assert summary.amount_difference_krw == Decimal(100_000)
    assert "SALES_AMOUNT_MISMATCH" in result.reason_codes
    amount_rule = next(r for r in result.rule_results if r["rule_id"] == "FIN-SALES-AMOUNT")
    assert amount_rule["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# 3. 역마진은 사실이다
# ---------------------------------------------------------------------------


def test_negative_margin_is_kept_as_a_calculated_fact():
    result = _evaluate(
        _payload(
            inventory_cost_basis={
                "amount_krw": Decimal(1_500_000),
                "cost_method": "ACTUAL",
                "source_ref": "INV-LOT:L-1",
                "evidence_grade": "OFFICIAL",
            }
        )
    )
    summary = result.financial_summary

    assert summary is not None
    assert summary.contribution_margin_krw == Decimal(-500_000)
    assert summary.contribution_margin_rate == Decimal("-0.5")
    margin_rule = next(r for r in result.rule_results if r["rule_id"] == "FIN-SALES-MARGIN")
    assert margin_rule["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# 4~5. 없는 원가 · 없는 정책
# ---------------------------------------------------------------------------


def test_missing_cost_basis_leaves_margin_none_not_zero():
    result = _evaluate(_payload(inventory_cost_basis=_ABSENT))
    summary = result.financial_summary

    assert summary is not None
    assert summary.sales_cost_basis_krw is None
    assert summary.contribution_margin_krw is None
    assert summary.contribution_margin_rate is None
    assert "authoritative_inventory_cost_basis" in result.missing_data
    assert result.finance_verdict is None


def test_missing_margin_policy_is_runtime_not_ready_not_fail():
    result = _evaluate(finance_warning_margin_rate=None)

    assert result.runtime_status == "RUNTIME_NOT_READY"
    assert result.finance_verdict is None
    assert "finance_warning_margin_rate" in result.missing_data


def test_missing_payment_policy_is_runtime_not_ready_not_fail():
    result = _evaluate(max_finance_allowed_payment_terms_days=None)

    assert result.runtime_status == "RUNTIME_NOT_READY"
    assert result.finance_verdict is None
    assert "max_finance_allowed_payment_terms_days" in result.missing_data


# ---------------------------------------------------------------------------
# 6. 조건부 공급
# ---------------------------------------------------------------------------


def test_conditional_supply_is_not_covered_by_confirmed_inventory_cost():
    result = _evaluate(
        _payload(
            supply={
                "confirmed_quantity_kg": Decimal(60),
                "conditional_quantity_kg": Decimal(40),
                "dependency_ref": "PURCHASE-APPROVAL:A-1",
            }
        )
    )
    summary = result.financial_summary

    assert summary is not None
    # 확정 재고원가로 제안 전체 마진을 계산하지 않는다.
    assert summary.contribution_margin_krw is None
    assert summary.sales_cost_basis_krw is None
    assert "sales_cost_basis_for_conditional_supply" in result.missing_data


def test_fully_confirmed_supply_still_computes_margin():
    result = _evaluate(
        _payload(
            supply={
                "confirmed_quantity_kg": Decimal(100),
                "conditional_quantity_kg": Decimal(0),
            }
        )
    )
    summary = result.financial_summary

    assert summary is not None
    assert summary.contribution_margin_krw == Decimal(300_000)


# ---------------------------------------------------------------------------
# 7. 결제조건 · 현금흐름
# ---------------------------------------------------------------------------


def test_payment_term_over_authoritative_max_fails():
    result = _evaluate(_payload(payment_days=90), collection_risk_mode=None)
    payment_rule = next(
        r for r in result.rule_results if r["rule_id"] == "FIN-SALES-PAYMENT-TERM"
    )

    assert payment_rule["verdict"] == "FAIL"
    assert result.max_finance_allowed_payment_terms_days == MAX_DAYS


def test_scenario_cashflow_breach_is_a_fail_rule():
    breached = project_sales_scenario_cashflow(
        as_of=AS_OF,
        current_cash_krw=Decimal(500_000),
        horizon_end=HORIZON,
        base_cash_events=[],
        proposed_collection=build_proposed_sales_collection_event(
            proposal_ref="SC-001",
            collection_date=date(2026, 4, 9),
            sales_amount_krw=Decimal(1_000_000),
            source_ref="SALES-REPLY:R-9",
        ),
    )
    result = _evaluate(scenario_cashflow=breached)
    cash_rule = next(r for r in result.rule_results if r["rule_id"] == "FIN-SALES-CASHFLOW")

    assert cash_rule["verdict"] == "FAIL"
    assert "BASE_MINIMUM_CASH_VIOLATED" in cash_rule["reason_codes"]


def test_missing_scenario_cashflow_is_runtime_not_ready():
    result = _evaluate(scenario_cashflow=None)

    assert result.runtime_status == "RUNTIME_NOT_READY"
    assert "sales_scenario_cashflow" in result.missing_data


# ---------------------------------------------------------------------------
# 8~9. 여신 · 신규 거래처
# ---------------------------------------------------------------------------


def test_missing_credit_policy_is_runtime_not_ready_not_fail():
    result = _evaluate(credit_limit_krw=None)

    assert result.runtime_status == "RUNTIME_NOT_READY"
    assert result.finance_verdict is None
    assert "partner_credit_limit_krw" in result.missing_data
    summary = result.financial_summary
    assert summary is not None
    assert summary.credit_limit_krw is None
    assert summary.available_credit_krw is None


def test_new_partner_with_no_receivables_does_not_fail_on_credit():
    result = _evaluate(receivable_facts=_receivables())
    credit_rule = next(r for r in result.rule_results if r["rule_id"] == "FIN-SALES-CREDIT")
    summary = result.financial_summary

    assert credit_rule["verdict"] == "PASS"
    assert summary is not None
    assert summary.current_partner_ar_krw == Decimal(0)
    assert summary.projected_partner_ar_krw == Decimal(1_000_000)


def test_overdue_facts_are_surfaced_even_without_a_risk_policy():
    result = _evaluate(
        receivable_facts=_receivables(_receivable(due=date(2026, 1, 1), amount="2000000"))
    )
    summary = result.financial_summary

    assert summary is not None
    assert summary.overdue_ar_krw == Decimal(2_000_000)
    risk_rule = next(
        r for r in result.rule_results if r["rule_id"] == "FIN-SALES-COLLECTION-RISK"
    )
    assert risk_rule["verdict"] is None


# ---------------------------------------------------------------------------
# 10. 입력 미비 — Finance 고장이 아니다
# ---------------------------------------------------------------------------


def test_missing_sales_origin_field_is_input_incomplete_not_error():
    result = _evaluate(_payload(unit_price_krw=_ABSENT))

    assert result.status == "INPUT_INCOMPLETE"
    assert result.runtime_status == "READY"
    assert result.finance_verdict is None
    assert result.missing_fields == ("unit_price_krw",)


def test_every_missing_required_field_is_named_at_once():
    result = _evaluate(_payload(quantity_kg=_ABSENT, partner_id=_ABSENT))

    assert set(result.missing_fields) == {"partner_id", "quantity_kg"}


def test_blank_string_counts_as_missing():
    result = _evaluate(_payload(scenario_id="   "))

    assert result.status == "INPUT_INCOMPLETE"
    assert "scenario_id" in result.missing_fields


def test_unparseable_payload_does_not_become_a_verdict():
    result = _evaluate(_payload(quantity_kg="not-a-number"))

    assert result.status == "INPUT_INCOMPLETE"
    assert result.finance_verdict is None
    assert result.missing_fields == ("sales_payload_not_parseable",)


def test_float_business_numbers_are_refused_rather_than_silently_accepted():
    result = _evaluate(_payload(unit_price_krw=10000.5))

    assert result.status == "INPUT_INCOMPLETE"
    assert result.missing_fields == ("sales_payload_not_parseable",)


def test_extra_sales_keys_do_not_break_finance_parsing():
    parsed, missing = parse_sales_validation_input(
        _payload(scenario_type="WHAT_IF", risks=["weather"])
    )

    assert missing == ()
    assert parsed is not None
    assert parsed.scenario_id == "SC-001"


# ---------------------------------------------------------------------------
# 11~12. 근거 계보 · null 과 0
# ---------------------------------------------------------------------------


def test_evidence_lineage_is_preserved_for_surfaced_numbers():
    result = _evaluate()

    assert "SALES-REPLY:R-9" in result.evidence_refs
    assert "INV-LOT:L-1" in result.evidence_refs
    assert "RECEIVABLE:R-1" in result.evidence_refs


def test_null_payment_days_is_not_treated_as_same_day_collection():
    result = _evaluate(_payload(payment_days=_ABSENT))
    summary = result.financial_summary

    assert summary is not None
    # 회수일을 지어내지 않는다.
    assert summary.collection_date is None
    payment_rule = next(
        r for r in result.rule_results if r["rule_id"] == "FIN-SALES-PAYMENT-TERM"
    )
    assert payment_rule["verdict"] is None
    assert payment_rule["reason_codes"] == ("SALES_PAYMENT_DAYS_ABSENT",)


def test_zero_payment_days_is_a_real_value_and_is_judged():
    result = _evaluate(_payload(payment_days=0))
    summary = result.financial_summary

    assert summary is not None
    assert summary.collection_date == date(2026, 3, 10)
    payment_rule = next(
        r for r in result.rule_results if r["rule_id"] == "FIN-SALES-PAYMENT-TERM"
    )
    assert payment_rule["verdict"] == "PASS"


def test_zero_cost_basis_is_honoured_while_missing_cost_basis_is_not():
    zero_cost = _evaluate(
        _payload(
            inventory_cost_basis={
                "amount_krw": Decimal(0),
                "cost_method": "ACTUAL",
                "source_ref": "INV-LOT:L-1",
                "evidence_grade": "OFFICIAL",
            }
        )
    )
    summary = zero_cost.financial_summary

    assert summary is not None
    assert summary.sales_cost_basis_krw == Decimal(0)
    assert summary.contribution_margin_krw == Decimal(1_000_000)


def test_result_is_self_contained_enough_to_survive_a_refeed():
    result = _evaluate()
    payload = result.model_dump()

    for key in (
        "finance_verdict",
        "rule_results",
        "reason_codes",
        "missing_data",
        "missing_fields",
        "financial_summary",
        "evidence_refs",
        "max_finance_allowed_payment_terms_days",
    ):
        assert key in payload
    assert len(payload["rule_results"]) == 6


def test_installment_is_not_silently_treated_as_single():
    result = _evaluate(_payload(payment_terms_type="INSTALLMENT"))
    payment_rule = next(
        r for r in result.rule_results if r["rule_id"] == "FIN-SALES-PAYMENT-TERM"
    )

    assert payment_rule["verdict"] is None
    assert payment_rule["missing_policy"] == ("sales_installment_payment_policy",)


# ---------------------------------------------------------------------------
# 13. 사실 없음 ≠ 0 — 자리 메우기 금지
# ---------------------------------------------------------------------------


def test_absent_receivable_facts_do_not_become_zero_ar():
    result = _evaluate(receivable_facts=None)
    summary = result.financial_summary

    assert summary is not None
    # 0원 채권(사실)과 채권 자료 없음을 섞지 않는다.
    assert summary.current_partner_ar_krw is None
    assert summary.projected_partner_ar_krw is None
    assert summary.overdue_ar_krw is None
    assert "partner_receivable_facts" in result.missing_data


def test_credit_rule_stays_closed_when_receivable_facts_are_absent():
    # 한도가 있어도 채권 사실이 없으면 판정하지 않는다.
    result = _evaluate(receivable_facts=None, credit_limit_krw=CREDIT_LIMIT)
    credit_rule = _rule_of(result, "FIN-SALES-CREDIT")

    assert credit_rule["runtime_status"] == "RUNTIME_NOT_READY"
    assert credit_rule["verdict"] is None


def test_collection_risk_stays_closed_when_overdue_is_unknown():
    result = _evaluate(receivable_facts=None)
    risk_rule = _rule_of(result, "FIN-SALES-COLLECTION-RISK")

    assert risk_rule["runtime_status"] == "RUNTIME_NOT_READY"
    assert risk_rule["verdict"] is None
    assert "partner_receivable_facts" in risk_rule["missing_policy"]


def test_zero_overdue_is_a_fact_distinct_from_unknown_overdue():
    known_zero = _evaluate(receivable_facts=_receivables())
    unknown = _evaluate(receivable_facts=None)

    assert known_zero.financial_summary is not None
    assert unknown.financial_summary is not None
    assert known_zero.financial_summary.overdue_ar_krw == Decimal(0)
    assert unknown.financial_summary.overdue_ar_krw is None


def test_unknown_conditional_supply_does_not_cover_the_whole_proposal():
    """🔴 조건부 칸이 비어 있으면 '조건부 0' 이 아니라 '모름' 이다.

    모르는 채로 확정 재고원가를 제안 전체에 씌우면 역마진이 마진처럼 보인다.
    확정 물량은 그대로 받되(정보를 잃지 않는다), 마진은 fail closed 한다.
    """
    result = _evaluate(_payload(supply={"confirmed_quantity_kg": Decimal(60)}))
    summary = result.financial_summary

    assert summary is not None
    assert summary.contribution_margin_krw is None
    assert summary.sales_cost_basis_krw is None
    assert "sales_supply_conditional_quantity" in result.missing_data


def test_explicit_zero_conditional_supply_still_computes_margin():
    """0 은 사실이다 — '조건부 물량 없음' 을 명시하면 마진을 낸다."""
    result = _evaluate(
        _payload(
            supply={
                "confirmed_quantity_kg": Decimal(100),
                "conditional_quantity_kg": Decimal(0),
            }
        )
    )
    summary = result.financial_summary

    assert summary is not None
    assert summary.contribution_margin_krw == Decimal(300_000)
    assert "sales_supply_conditional_quantity" not in result.missing_data


def test_absent_supply_is_unknown_not_zero_conditional():
    """🔴 공급을 못 받은 것은 **모름**이지 '조건부 공급 없음' 이 아니다.

    예전에는 여기서 조건부 0 으로 읽어, 확정 재고원가가 제안 전체를 덮는 것을 막는
    방어가 통째로 풀렸다. 모르면 그 판단을 할 수 없다 — fail closed.
    """
    result = _evaluate(_payload(supply=_ABSENT))
    summary = result.financial_summary

    assert summary is not None
    assert summary.contribution_margin_krw is None
    assert summary.sales_cost_basis_krw is None
    assert "sales_supply_context" in result.missing_data


def test_absent_supply_does_not_block_rules_that_do_not_need_it():
    """공급이 없다고 공급과 무관한 사실까지 못 내지는 않는다."""
    result = _evaluate(_payload(supply=_ABSENT))
    summary = result.financial_summary

    assert summary is not None
    # 금액 정합성은 공급과 무관하게 계산된다.
    assert summary.recalculated_sales_amount_krw == Decimal(1_000_000)
    assert summary.amount_match is True
