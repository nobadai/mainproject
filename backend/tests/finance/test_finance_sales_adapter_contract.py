"""Finance Sales Core Phase 8 — 판매 재무 판정의 봉투 매핑 계약.

★ 이 파일이 지키는 것은 **도메인 판정이 봉투를 지나며 사라지지 않는 것**이다.
    · PASS→ok · REVIEW_REQUIRED→conditional · FAIL→reject 는 Finance 가 옮긴다
    · 옮긴 뒤에도 payload.finance_verdict 에 원본이 남는다
    · 판정이 없는 세 경우는 전부 skipped 이고 reject 가 아니다
    · 결제일수 상한은 payload 필드이지 SuggestedAdjustment 축이 아니다
    · Refeed 가 잘라먹을 것이 없도록 payload 가 자기 완결적이다
"""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.adapter import (
    SALES_VERDICT_TO_BUSINESS_STATUS,
    build_sales_validation_payload,
    map_sales_finance_verdict,
)
from app.finance.sales_models import (
    SalesFinancialSummary,
    SalesValidationResult,
)


def _summary(**overrides):
    values = {
        "recalculated_sales_amount_krw": Decimal(1_000_000),
        "reported_sales_amount_krw": Decimal(1_000_000),
        "amount_difference_krw": Decimal(0),
        "amount_match": True,
        "sales_cost_basis_krw": Decimal(700_000),
        "contribution_margin_krw": Decimal(300_000),
        "contribution_margin_rate": Decimal("0.3"),
        "collection_date": date(2026, 4, 9),
    }
    values.update(overrides)
    return SalesFinancialSummary(**values)


def _result(**overrides):
    values = {
        "scenario_id": "SC-001",
        "runtime_status": "READY",
        "status": "EVALUATED",
        "finance_verdict": "PASS",
        "financial_summary": _summary(),
        "rule_results": (),
        "reason_codes": ("SALES_AMOUNT_MATCH",),
        "missing_fields": (),
        "missing_data": (),
        "evidence_refs": ("SALES-REPLY:R-9",),
    }
    values.update(overrides)
    return SalesValidationResult(**values)


# ---------------------------------------------------------------------------
# 판정 매핑
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("PASS", "ok"), ("REVIEW_REQUIRED", "conditional"), ("FAIL", "reject")],
)
def test_finance_verdict_maps_to_the_envelope_word(verdict, expected):
    runtime_status, business_status = map_sales_finance_verdict(
        _result(finance_verdict=verdict)
    )

    assert runtime_status == "READY"
    assert business_status == expected


def test_the_mapping_table_is_exactly_the_three_domain_verdicts():
    assert SALES_VERDICT_TO_BUSINESS_STATUS == {
        "PASS": "ok",
        "REVIEW_REQUIRED": "conditional",
        "FAIL": "reject",
    }


@pytest.mark.parametrize("verdict", ["PASS", "REVIEW_REQUIRED", "FAIL"])
def test_the_original_domain_verdict_survives_the_mapping(verdict):
    payload = build_sales_validation_payload(_result(finance_verdict=verdict))

    # conditional 만 남으면 왜 conditional 인지 되돌릴 수 없다.
    assert payload["finance_verdict"] == verdict


# ---------------------------------------------------------------------------
# 판정이 없는 경우 — 전부 skipped 다
# ---------------------------------------------------------------------------


def test_input_incomplete_is_skipped_and_finance_stays_ready():
    runtime_status, business_status = map_sales_finance_verdict(
        _result(
            status="INPUT_INCOMPLETE",
            finance_verdict=None,
            financial_summary=None,
            missing_fields=("unit_price_krw",),
        )
    )

    # 제안이 미완성인 것은 Finance 고장이 아니다.
    assert runtime_status == "READY"
    assert business_status == "skipped"


def test_runtime_not_ready_is_skipped_not_reject():
    runtime_status, business_status = map_sales_finance_verdict(
        _result(
            runtime_status="RUNTIME_NOT_READY",
            status="RUNTIME_NOT_READY",
            finance_verdict=None,
            missing_data=("finance_minimum_margin_rate",),
        )
    )

    assert runtime_status == "RUNTIME_NOT_READY"
    assert business_status == "skipped"


def test_error_is_skipped_not_reject():
    runtime_status, business_status = map_sales_finance_verdict(
        _result(runtime_status="ERROR", status="ERROR", finance_verdict=None)
    )

    assert runtime_status == "ERROR"
    assert business_status == "skipped"


def test_evaluated_without_a_verdict_is_skipped_rather_than_passed():
    _, business_status = map_sales_finance_verdict(
        _result(finance_verdict=None, reason_codes=("SALES_MARGIN_RATE_UNCOMPUTABLE",))
    )

    assert business_status == "skipped"


# ---------------------------------------------------------------------------
# Refeed 를 견디는 payload
# ---------------------------------------------------------------------------


def test_payload_carries_everything_master_must_not_drop():
    payload = build_sales_validation_payload(
        _result(
            rule_results=(
                {
                    "rule_id": "FIN-SALES-MARGIN",
                    "runtime_status": "READY",
                    "verdict": "REVIEW_REQUIRED",
                    "reason_codes": ("SALES_MARGIN_BELOW_WARNING",),
                    "missing_policy": (),
                },
            )
        )
    )

    for key in (
        "finance_verdict",
        "rule_results",
        "reason_codes",
        "missing_fields",
        "missing_data",
        "data_quality",
        "evidence_refs",
        "financial_summary",
        "max_finance_allowed_payment_terms_days",
    ):
        assert key in payload, key
    assert payload["rule_results"][0]["rule_id"] == "FIN-SALES-MARGIN"


def test_evidence_and_reason_codes_are_not_summarised_away():
    payload = build_sales_validation_payload(
        _result(
            reason_codes=("SALES_AMOUNT_MISMATCH", "SALES_MARGIN_BELOW_MINIMUM"),
            evidence_refs=("SALES-REPLY:R-9", "INV-LOT:L-1"),
        )
    )

    assert payload["reason_codes"] == ["SALES_AMOUNT_MISMATCH", "SALES_MARGIN_BELOW_MINIMUM"]
    assert payload["evidence_refs"] == ["SALES-REPLY:R-9", "INV-LOT:L-1"]


def test_data_quality_reflects_missing_facts():
    complete = build_sales_validation_payload(_result())
    incomplete = build_sales_validation_payload(
        _result(missing_data=("partner_credit_limit_krw",))
    )

    assert complete["data_quality"] == "COMPLETE"
    assert incomplete["data_quality"] == "INCOMPLETE"


def test_unavailable_numbers_stay_null_rather_than_zero():
    payload = build_sales_validation_payload(
        _result(
            financial_summary=_summary(
                sales_cost_basis_krw=None,
                contribution_margin_krw=None,
                contribution_margin_rate=None,
            )
        )
    )
    summary = payload["financial_summary"]

    assert summary["sales_cost_basis_krw"] is None
    assert summary["contribution_margin_krw"] is None
    assert summary["contribution_margin_rate"] is None


# ---------------------------------------------------------------------------
# SuggestedAdjustment 는 금액 축 하나뿐이다
# ---------------------------------------------------------------------------


def test_payment_term_limit_is_a_payload_field_not_an_adjustment_axis():
    payload = build_sales_validation_payload(
        _result(max_finance_allowed_payment_terms_days=45)
    )

    assert payload["max_finance_allowed_payment_terms_days"] == 45
    # 상한은 조정이 아니라 경계다 — 공통 조정 축을 늘리지 않는다.
    assert "suggested_adjustments" not in payload


def test_the_payload_declares_no_adjustment_axis_at_all():
    payload = build_sales_validation_payload(
        _result(max_finance_allowed_payment_terms_days=45)
    )

    # 이 Phase 는 조정을 제안하지 않는다 — 축을 실을 자리 자체가 없다.
    assert "axis" not in payload
    assert not any(key.endswith("_adjustment") for key in payload)
    assert not any(key.endswith("_adjustments") for key in payload)
    # 그리고 결제일수는 오직 "상한" 이름으로만 등장한다.
    payment_keys = [key for key in payload if "payment" in key]
    assert payment_keys == ["max_finance_allowed_payment_terms_days"]


def test_finance_adjustment_axis_vocabulary_is_untouched_by_sales():
    from app.contracts.core import SuggestedAdjustment

    # 판매 작업이 공통 조정 계약을 넓히지 않았다.
    assert "payment_terms" not in str(SuggestedAdjustment.__annotations__)
