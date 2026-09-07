"""P6 facts 구조화 — formatter·인용 화이트리스트·상한·기록 (LLM 정책 결정서 v1.3 §5)."""

import json
from datetime import date
from decimal import Decimal

import pytest

from app.logistics import interpretation
from app.logistics.interpretation import (
    MASTER_LLM_ENV,
    _assemble_facts,
    _build_signal_facts,
    build_logistics_context,
    build_sanitized_context,
    format_count,
    format_measured_percent,
    format_policy_percent,
    format_ratio_with_threshold,
    master_interpretation_service,
    master_llm_enabled,
    translate_missing_data,
    uncalled_interpretation,
)
from app.logistics.llm.runtime import (
    InterpretationService,
    InterpretationValidationError,
    LLMSettings,
    UnavailableProvider,
    ValidationIssue,
    build_template_interpretation,
    validate_interpretation,
)
from app.logistics.llm.schemas import ContextFact, SanitizedLLMContext
from app.logistics.schemas import (
    InboundConstraints,
    LogisticsBand,
    LogisticsProcurementResponse,
    LogisticsSalesResponse,
)

# ---------------------------------------------------------------------------
# formatter — 표기 스펙 (2026-08-31 확정)
# ---------------------------------------------------------------------------


def test_measured_percent_is_one_decimal_half_up():
    assert format_measured_percent(Decimal("0.9165")) == "91.7%"
    # ROUND_HALF_UP — float round() 의 banker's rounding 이라면 91.6% 가 된다.
    assert format_measured_percent(Decimal("0.9165")) == format_measured_percent(Decimal("0.9165"))
    # 소수 1자리 고정 — 정수로 떨어져도 표기는 같은 규칙이다.
    assert format_measured_percent(Decimal("0.92")) == "92.0%"


def test_policy_percent_preserves_precision_without_trailing_zeros():
    # 정수 강제가 아니다 — 실측 후 임계가 소수가 되어도 표기가 의미를 잃지 않는다.
    assert format_policy_percent(Decimal("0.90")) == "90%"
    assert format_policy_percent(Decimal("0.30")) == "30%"
    assert format_policy_percent(Decimal("0.925")) == "92.5%"
    # 유효 정밀도 보존 — 자릿수는 정책값이 소유하고 formatter 는 반올림하지 않는다.
    assert format_policy_percent(Decimal("0.9012345678")) == "90.12345678%"


def test_ratio_with_threshold_bundles_measured_and_policy():
    assert format_ratio_with_threshold(Decimal("0.9165"), Decimal("0.90")) == "91.7% (임계 90%)"


def test_count_attaches_unit_to_every_number():
    assert format_count(3, "개") == "3개"
    assert format_count(2, "건") == "2건"


# ---------------------------------------------------------------------------
# fact 조립 — signal별 목록과 상한
# ---------------------------------------------------------------------------


def _sales_response(**overrides) -> LogisticsSalesResponse:
    fields = {
        "snapshot_id": None,
        "approval_id": "H1",
        "runtime_status": "READY",
        "verdict": "PASS",
        "daily_outbound_capacity_kg": None,
        "lot_constraints": [],
        "hard_constraints": [],
        "soft_warnings": ["FRESHNESS_QUALITY_RISK"],
        "missing_data": [],
        "preferred_adjustment": "우선 출고 대상으로 검토합니다.",
    }
    fields.update(overrides)
    return LogisticsSalesResponse(**fields)


_FRESHNESS_MEASUREMENTS = {
    "freshness_risk_lot_count": 3,
    "freshness_min_remaining_ratio": Decimal("0.25"),
    "freshness_pressure_ratio": Decimal("0.30"),
}


def test_capacity_fact_bundles_usage_and_threshold():
    facts = _build_signal_facts(
        "CAPACITY_TIGHT",
        {
            "capacity_window_usage": Decimal("0.9165"),
            "capacity_tight_ratio": Decimal("0.90"),
        },
    )

    assert [(f.fact_id, f.display_value) for f in facts] == [
        ("capacity_window_usage", "91.7% (임계 90%)")
    ]


def test_scenario_fact_uses_agreed_count_format():
    # "조건부 N건 (전체 M건)" 문형은 표기 스펙(2026-08-31)으로 확정된 형식이다 —
    # 슬래시("2/3건")로 회귀하면 토큰 추출이 깨진다.
    facts = _build_signal_facts(
        "SCENARIO_ADJUSTMENT_REQUIRED",
        {"scenario_conditional_count": 2, "scenario_total_count": 3},
    )

    assert [(f.fact_id, f.display_value) for f in facts] == [
        ("scenario_conditional_count", "조건부 2건 (전체 3건)")
    ]


def test_context_facts_carry_judged_values_only():
    context, overflow = build_logistics_context(_sales_response(), _FRESHNESS_MEASUREMENTS)

    assert overflow is False
    assert [fact.display_value for fact in context.facts] == ["3개", "25.0% (임계 30%)"]
    # fact_id 는 무숫자 명명이다 — 검증기의 숫자 검사와 충돌하지 않는다.
    assert all(not any(ch.isdigit() for ch in fact.fact_id) for fact in context.facts)


def test_signal_without_measurements_fails_closed():
    # signal 은 섰는데 판정 수치가 전달되지 않으면 배선 버그다 — 확인된 fact 없이
    # 해석시키지 않고 LLM 을 건너뛴다 (facts_incomplete=True).
    context, incomplete = build_logistics_context(_sales_response(), None)

    assert incomplete is True
    assert context.facts == []


def test_fact_overflow_returns_empty_and_flag(monkeypatch):
    # 총 상한(8) 초과 — 조용한 절단이 아니라 빈 목록 + overflow 플래그다.
    # 현행 조립기의 실측 최대는 4개라 이 가드는 휴면이다(확장 자리) — 실제 조립기가
    # 만들 수 없는 fact 수를 monkeypatch 로 만들어 가드 자체의 동작만 고정한다.
    def two_facts(signal, measurements):
        del measurements
        return [
            ContextFact(fact_id=f"{signal}_left", label="검증용", display_value="3개"),
            ContextFact(fact_id=f"{signal}_right", label="검증용", display_value="3개"),
        ]

    monkeypatch.setattr("app.logistics.interpretation._build_signal_facts", two_facts)
    signals = [f"SIGNAL_{name}" for name in ["A", "B", "C", "D", "E"]]
    facts, overflow = _assemble_facts(signals, {})

    assert overflow is True
    assert facts == []


def test_duplicate_fact_ids_across_signals_are_deduped():
    # 신선도 두 signal(매입·판매)은 같은 fact_id 를 낸다 — 사이클 분리가 무너져
    # 공존해도 같은 fact 가 두 번 나가지 않는다 (첫 것 유지 + 로그).
    signals = ["INVENTORY_FRESHNESS_PRESSURE", "FRESHNESS_QUALITY_RISK"]
    facts, overflow = _assemble_facts(signals, _FRESHNESS_MEASUREMENTS)

    assert overflow is False
    assert [fact.fact_id for fact in facts] == [
        "freshness_risk_lot_count",
        "freshness_min_remaining_ratio",
    ]


# ---------------------------------------------------------------------------
# 인용 화이트리스트 — fail-closed (부분 일치 금지)
# ---------------------------------------------------------------------------


def _quote_context() -> SanitizedLLMContext:
    return SanitizedLLMContext(
        signals=["INVENTORY_FRESHNESS_PRESSURE"],
        facts=[
            ContextFact(
                fact_id="freshness_risk_lot_count",
                label="신선도 임박 가용 Lot 수",
                display_value="3개",
            ),
            ContextFact(
                fact_id="freshness_min_remaining_ratio",
                label="최소 신선도 잔여 비율",
                display_value="25.0% (임계 30%)",
            ),
        ],
        allowed_adjustments=["quantity", "timing"],
        preferred_adjustment=None,
        missing_data=[],
    )


def _raw(summary: str) -> str:
    return json.dumps(
        {
            "summary": summary,
            "risks": ["INVENTORY_FRESHNESS_PRESSURE"],
            "suggested_adjustment": None,
        },
        ensure_ascii=False,
    )


def test_display_value_tokens_are_quotable():
    interpretation = validate_interpretation(
        _raw("신선도 임박 Lot이 3개이며 최소 잔여 비율은 25.0%입니다."), _quote_context()
    )

    assert "3개" in interpretation.summary


def test_bundled_fact_allows_both_tokens():
    # "25.0% (임계 30%)" 한 fact 의 두 토큰이 모두 인용 가능하다.
    interpretation = validate_interpretation(
        _raw("최소 잔여 비율 25.0%가 임계 30% 이하입니다."), _quote_context()
    )

    assert interpretation.risks == ["INVENTORY_FRESHNESS_PRESSURE"]


@pytest.mark.parametrize(
    "summary",
    [
        "잔여 비율이 0.25입니다.",  # 단위 없는 원값 — 환산 금지
        "잔여 비율이 25%입니다.",  # 표기 축약("25.0%" → "25%")도 완전 일치 위반이다
        "잔여 비율이 26.0%입니다.",  # 새 숫자 생성
        "차이가 5.0%p입니다.",  # 파생 계산
        "신선도 임박 Lot이 3일 남았습니다.",  # 단위 바꿔치기("3개" → "3일")
        "변동이 -30%입니다.",  # 부호 결합 — "30%" 부분 일치로 통과 금지
        "변동이 +30%입니다.",
        "잔여 비율이 30%%입니다.",  # 단위 연장 — "30%" 부분 일치로 통과 금지
        "보관이 3개월 남았습니다.",  # 한글 단위 연장("개" → "개월")
    ],
)
def test_non_whitelisted_numeric_tokens_are_rejected(summary):
    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(_raw(summary), _quote_context())

    assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in error.value.issues


@pytest.mark.parametrize(
    "summary",
    [
        "신선도 임박 Lot이 3개이며 검토가 필요합니다.",  # 단위 + 조사
        "신선도 임박 Lot은 3개입니다.",
    ],
)
def test_korean_particle_after_unit_is_quotable(summary):
    interpretation = validate_interpretation(_raw(summary), _quote_context())

    assert interpretation.risks == ["INVENTORY_FRESHNESS_PRESSURE"]


def test_decimal_point_is_not_a_sentence_boundary():
    # 소수점을 문장 구분자로 세면 표기 스펙의 소수 표기가 TOO_MANY_SENTENCES 로
    # 오거부된다 — 한 문장에 소수 토큰 둘이 있어도 한 문장이다.
    interpretation = validate_interpretation(
        _raw("최소 잔여 비율은 25.0%이며 임박 Lot은 3개입니다."), _quote_context()
    )

    assert interpretation.risks == ["INVENTORY_FRESHNESS_PRESSURE"]


def test_template_stays_numberless_even_with_numeric_facts():
    template = build_template_interpretation(_quote_context())

    assert not any(ch.isdigit() for ch in template.summary)


# ---------------------------------------------------------------------------
# llm_context_facts 기록 — 기준은 수신이 아니라 호출 확정
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, context, *, retry_guidance=None):
        del context, retry_guidance
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _service(provider, *, enabled=True):
    return InterpretationService(
        LLMSettings(
            enabled=enabled,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=0,
        ),
        provider,
    )


def _success_output() -> str:
    return json.dumps(
        {
            "summary": "신선도 임박 Lot이 3개 확인되었습니다.",
            "risks": ["INVENTORY_FRESHNESS_PRESSURE"],
            "suggested_adjustment": None,
        },
        ensure_ascii=False,
    )


def test_success_records_context_facts():
    result = _service(_FakeProvider([_success_output()])).interpret(
        _quote_context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert [fact.display_value for fact in result.llm_context_facts] == [
        "3개",
        "25.0% (임계 30%)",
    ]


def test_pre_send_failure_still_records_facts():
    # AUTH_ERROR 처럼 전송 전에 실패해도 호출 확정이므로 기록된다 —
    # "Gemini 가 실제 수신한 값"이라는 뜻이 아니다.
    result = _service(_FakeProvider([RuntimeError("no key")])).interpret(
        _quote_context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "FALLBACK"
    assert len(result.llm_context_facts) == 2


def test_skipped_and_disabled_record_empty_facts():
    provider = _FakeProvider([])
    skipped = _service(provider).interpret(
        _quote_context(),
        runtime_ready=True,
        has_blocking_constraints=False,
        facts_incomplete=True,
    )
    disabled = _service(provider, enabled=False).interpret(
        _quote_context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 0  # overflow 는 LLM 미호출이다
    assert skipped.llm_status == "SKIPPED_TEMPLATE"
    assert skipped.llm_context_facts == []
    assert disabled.llm_status == "DISABLED"
    assert disabled.llm_context_facts == []


# ---------------------------------------------------------------------------
# 공통 조립기 추출 (#385) — wrapper 등가 · 번역 멱등 · 무숫자 경계
# ---------------------------------------------------------------------------


def _procurement_response(**overrides) -> LogisticsProcurementResponse:
    fields = {
        "as_of": date(2026, 1, 20),
        "snapshot_id": None,
        "runtime_status": "READY",
        "verdict": "REVIEW_REQUIRED",
        "band": LogisticsBand(cap_by_date={}),
        "inbound_constraints": InboundConstraints(
            inbound_lead_days=None,
            daily_inbound_capacity_kg=None,
            inbound_transport_capacity_kg=None,
        ),
        "hard_constraints": [],
        "soft_warnings": ["SCENARIO_ADJUSTMENT_REQUIRED", "CAPACITY_TIGHT_POLICY_UNRESOLVED"],
        "missing_data": ["capacity_tight_policy"],
        "preferred_adjustment": "quantity",
        "evidences": [],
    }
    fields.update(overrides)
    return LogisticsProcurementResponse(**fields)


_SCENARIO_MEASUREMENTS = {"scenario_conditional_count": 2, "scenario_total_count": 3}


def test_sales_wrapper_equals_direct_builder_and_keeps_previous_behaviour():
    response = _sales_response(
        soft_warnings=["FRESHNESS_QUALITY_RISK", "SNAPSHOT_ID_UNRESOLVED"],
        missing_data=["snapshot_id"],
    )

    via_wrapper = build_logistics_context(response, _FRESHNESS_MEASUREMENTS)
    direct = build_sanitized_context(
        cycle="SALES",
        signals=response.soft_warnings,
        measurements=_FRESHNESS_MEASUREMENTS,
        preferred_adjustment=response.preferred_adjustment,
        missing_data=response.missing_data,
    )

    assert via_wrapper == direct
    # 추출 전 동작 그대로 — 핀
    context, incomplete = via_wrapper
    assert incomplete is False
    assert context.signals == ["FRESHNESS_QUALITY_RISK"]
    assert context.allowed_adjustments == ["우선 출고 대상으로 검토합니다."]
    assert context.preferred_adjustment == "우선 출고 대상으로 검토합니다."
    assert context.missing_data == ["snapshot_id"]
    assert [fact.display_value for fact in context.facts] == ["3개", "25.0% (임계 30%)"]


def test_procurement_wrapper_equals_direct_builder_and_keeps_previous_behaviour():
    response = _procurement_response()

    via_wrapper = build_logistics_context(response, _SCENARIO_MEASUREMENTS)
    direct = build_sanitized_context(
        cycle="PROCUREMENT",
        signals=response.soft_warnings,
        measurements=_SCENARIO_MEASUREMENTS,
        preferred_adjustment=response.preferred_adjustment,
        missing_data=response.missing_data,
    )

    assert via_wrapper == direct
    context, incomplete = via_wrapper
    assert incomplete is False
    assert context.signals == ["SCENARIO_ADJUSTMENT_REQUIRED"]
    assert context.allowed_adjustments == ["quantity", "timing"]
    assert context.preferred_adjustment == "quantity"
    assert context.missing_data == ["capacity_tight_policy"]
    assert [fact.display_value for fact in context.facts] == ["조건부 2건 (전체 3건)"]


def test_sales_without_preferred_has_no_allowed_adjustment_in_both_paths():
    response = _sales_response(preferred_adjustment=None)

    via_wrapper = build_logistics_context(response, _FRESHNESS_MEASUREMENTS)
    direct = build_sanitized_context(
        cycle="SALES",
        signals=response.soft_warnings,
        measurements=_FRESHNESS_MEASUREMENTS,
        preferred_adjustment=None,
        missing_data=[],
    )

    assert via_wrapper == direct
    assert via_wrapper[0].allowed_adjustments == []


def test_builder_translates_raw_missing_codes_and_never_carries_digits():
    # 어댑터는 raw 코드를 준다 — `LOG-H02` 의 숫자가 Context 에 실리면 무숫자 경계가 깨진다.
    context, _ = build_sanitized_context(
        cycle="PROCUREMENT",
        signals=[],
        measurements=None,
        preferred_adjustment=None,
        missing_data=["LOG-H02", "CAPACITY_TIGHT_POLICY_UNRESOLVED", "LOG-H02", "WHAT_IS_THIS_9"],
    )

    assert context.missing_data == [
        "zone_capacity",
        "capacity_tight_policy",
        "unrecognized_missing_information",
    ]
    assert not any(ch.isdigit() for name in context.missing_data for ch in name)


def test_translate_missing_data_is_idempotent_on_translated_names():
    once = translate_missing_data(["LOG-H01", "N17", "NOPE-1"])

    assert once == [
        "warehouse_capacity_policy",
        "shared_outbound_capacity",
        "unrecognized_missing_information",
    ]
    assert translate_missing_data(once) == once


def test_builder_rejects_unknown_cycle():
    with pytest.raises(ValueError):
        build_sanitized_context(
            cycle="sales",  # type: ignore[arg-type]
            signals=[],
            measurements=None,
            preferred_adjustment=None,
            missing_data=[],
        )


# ---------------------------------------------------------------------------
# Master-facing opt-in (#385) — 설정 부재 = DISABLED · Provider 클라이언트 생성 없음
# ---------------------------------------------------------------------------


def _settings(*, enabled: bool) -> LLMSettings:
    return LLMSettings(
        enabled=enabled,
        provider="ollama",
        model="gemma3:4b",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1,
        max_retries=0,
    )


def _pin_master(monkeypatch, value: str | None, *, base_enabled: bool = True) -> object:
    """opt-in 값 하나만 고정하고, 독립 경로 설정과 실 팩토리는 가짜로 막는다.

    `get_llm_settings` 를 갈아 끼우면 `.env` 는 읽히지 않는다 — 개발자 환경 값이 테스트
    결과를 흔들지 않는다. 돌려주는 sentinel 은 *"opt-in 경로가 실 팩토리에 닿았다"* 의 증거다.
    """
    if value is None:
        monkeypatch.delenv(MASTER_LLM_ENV, raising=False)
    else:
        monkeypatch.setenv(MASTER_LLM_ENV, value)
    monkeypatch.setattr(interpretation, "get_llm_settings", lambda: _settings(enabled=base_enabled))
    sentinel = object()
    monkeypatch.setattr(interpretation, "get_interpretation_service", lambda: sentinel)
    return sentinel


@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "off"])
def test_master_service_is_disabled_without_opt_in(monkeypatch, value):
    sentinel = _pin_master(monkeypatch, value)

    service = master_interpretation_service()

    assert service is not sentinel
    assert service.settings.enabled is False
    assert isinstance(service.provider, UnavailableProvider)
    assert master_llm_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_master_service_reaches_real_factory_only_with_explicit_opt_in(monkeypatch, value):
    sentinel = _pin_master(monkeypatch, value)

    assert master_llm_enabled() is True
    assert master_interpretation_service() is sentinel


def test_opt_in_does_not_revive_a_disabled_logistics_llm(monkeypatch):
    # AND 다 — 마스터 경로 손잡이는 추가로 여는 것이지, 꺼 둔 물류 LLM 을 되살리지 않는다.
    sentinel = _pin_master(monkeypatch, "true", base_enabled=False)

    service = master_interpretation_service()

    assert service is not sentinel
    assert service.settings.enabled is False


def test_disabled_master_service_never_calls_a_provider(monkeypatch):
    _pin_master(monkeypatch, None)
    service = master_interpretation_service()

    # signal 이 있고 runtime 도 READY 인 Context — 켜져 있었다면 호출됐을 조건이다.
    # UnavailableProvider 는 불리면 예외를 내 FALLBACK 이 되므로, DISABLED 가 곧 미호출의 증거다.
    result = service.interpret(_quote_context(), runtime_ready=True, has_blocking_constraints=False)

    assert result.llm_status == "DISABLED"
    assert result.llm_attempts == 0


@pytest.mark.parametrize(("enabled", "expected"), [(True, "SKIPPED_TEMPLATE"), (False, "DISABLED")])
def test_uncalled_interpretation_uses_the_service_gate_vocabulary(enabled, expected):
    provider = _FakeProvider([])

    result = uncalled_interpretation(_service(provider, enabled=enabled))

    assert result.llm_status == expected
    assert result.llm_attempts == 0
    assert result.llm_fallback_used is False
    assert provider.calls == 0
    assert result.interpretation.risks == []
