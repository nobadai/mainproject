import json
import urllib.error

import pytest

from app.logistics.llm.runtime import (
    InterpretationService,
    InterpretationValidationError,
    LLMSettings,
    ValidationIssue,
    build_template_interpretation,
    needs_llm,
    validate_interpretation,
)
from app.logistics.llm.schemas import ContextFact, SanitizedLLMContext


class FakeProvider:
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


def _service(provider):
    return InterpretationService(
        LLMSettings(
            enabled=True,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        provider,
    )


def _freshness_facts() -> list[ContextFact]:
    return [
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
    ]


def _context():
    return SanitizedLLMContext(
        signals=["FRESHNESS_QUALITY_RISK"],
        facts=_freshness_facts(),
        allowed_adjustments=["우선 출고 대상으로 검토합니다."],
        # Rule 이 정한 우선 조정 — 없으면 검증기가 추천을 null 로 강제한다.
        preferred_adjustment="우선 출고 대상으로 검토합니다.",
    )


def _output(*, summary="품질 위험 검토가 필요합니다."):
    return json.dumps(
        {
            "summary": summary,
            "risks": ["FRESHNESS_QUALITY_RISK"],
            "suggested_adjustment": "우선 출고 대상으로 검토합니다.",
        },
        ensure_ascii=False,
    )


def test_logistics_runtime_uses_its_own_provider_and_validator():
    provider = FakeProvider([_output()])

    result = _service(provider).interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 1
    assert provider.calls == 1


def test_logistics_numeric_output_retries_then_falls_back():
    provider = FakeProvider([_output(summary="위험 수치가 3입니다.")] * 2)

    result = _service(provider).interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "FALLBACK"
    assert result.llm_attempts == 2
    assert result.llm_fallback_used is True


def test_logistics_provider_failure_does_not_fail_interpretation():
    provider = FakeProvider([RuntimeError("unavailable")] * 2)

    result = _service(provider).interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "FALLBACK"
    assert result.interpretation.risks == ["FRESHNESS_QUALITY_RISK"]


# ---------------------------------------------------------------------------
# 호출 게이트 — LLM 정책 결정서 §2 (17-A · Qualitative · Composite 휴면)
# ---------------------------------------------------------------------------


def _gate_context(signals: list[str]) -> SanitizedLLMContext:
    # 게이트 판정은 facts 를 읽지 않는다 — signal 만으로 호출 여부를 정한다.
    return SanitizedLLMContext(
        signals=signals,
        facts=[],
        allowed_adjustments=[],
    )


@pytest.mark.parametrize(
    "signal",
    ["FRESHNESS_QUALITY_RISK", "INVENTORY_FRESHNESS_PRESSURE", "SCENARIO_ADJUSTMENT_REQUIRED"],
)
def test_qualitative_signal_triggers_alone(signal):
    assert needs_llm(_gate_context([signal]), runtime_ready=True, has_blocking_constraints=False)


def test_capacity_tight_alone_does_not_trigger():
    assert not needs_llm(
        _gate_context(["CAPACITY_TIGHT"]), runtime_ready=True, has_blocking_constraints=False
    )


def test_unresolved_only_context_does_not_trigger():
    """UNRESOLVED 는 게이트를 안 막지만(17-A), 업무 위험이 없으면 부를 이유도 없다."""
    assert not needs_llm(_gate_context([]), runtime_ready=True, has_blocking_constraints=False)


def test_fail_blocks_the_call_even_with_signals():
    assert not needs_llm(
        _gate_context(["INVENTORY_FRESHNESS_PRESSURE"]),
        runtime_ready=True,
        has_blocking_constraints=True,
    )


# ---------------------------------------------------------------------------
# 검증기 — 숫자 검사 범위 · preferred 강제 (결정서 §5)
# ---------------------------------------------------------------------------


def _validator_context(**overrides) -> SanitizedLLMContext:
    fields = {
        "signals": ["INVENTORY_FRESHNESS_PRESSURE"],
        "facts": _freshness_facts(),
        "allowed_adjustments": ["quantity", "timing"],
        "preferred_adjustment": None,
        "missing_data": [],
    }
    fields.update(overrides)
    return SanitizedLLMContext(**fields)


def _raw(summary: str, risks: list[str], suggested: str | None) -> str:
    return json.dumps(
        {"summary": summary, "risks": risks, "suggested_adjustment": suggested},
        ensure_ascii=False,
    )


def test_digit_bearing_risk_code_is_not_a_numeric_violation():
    """risks 는 signal 코드 보존 필드다 — 코드 속 숫자로 FALLBACK 이 나면 안 된다."""
    context = _validator_context(signals=["LOG-H02"], facts=[])

    interpretation = validate_interpretation(
        _raw("구역별 수용량이 확정되지 않았습니다.", ["LOG-H02"], None), context
    )

    assert interpretation.risks == ["LOG-H02"]


def test_digit_in_summary_is_still_rejected():
    context = _validator_context()

    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(
            _raw("위험 수치가 3입니다.", ["INVENTORY_FRESHNESS_PRESSURE"], None), context
        )

    assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in error.value.issues


def test_suggested_must_match_preferred():
    context = _validator_context(preferred_adjustment="quantity")

    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(
            _raw("신선도 압박이 있습니다.", ["INVENTORY_FRESHNESS_PRESSURE"], "timing"), context
        )

    assert ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION in error.value.issues


def test_no_preferred_means_no_suggestion():
    context = _validator_context(preferred_adjustment=None)

    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(
            _raw("신선도 압박이 있습니다.", ["INVENTORY_FRESHNESS_PRESSURE"], "quantity"), context
        )

    assert ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION in error.value.issues


def test_template_follows_preferred_rule():
    """Rule 이 방향을 안 정했으면 템플릿도 추천하지 않는다 — allowed[0] 자동 추천 폐기."""
    without_preferred = build_template_interpretation(_validator_context())
    with_preferred = build_template_interpretation(
        _validator_context(preferred_adjustment="quantity")
    )

    assert without_preferred.suggested_adjustment is None
    assert with_preferred.suggested_adjustment == "quantity"


# ---------------------------------------------------------------------------
# 전송 오류 분류 — 재시도 가능만 1회, 확정 오류는 즉시 FALLBACK (결정서 §6)
# ---------------------------------------------------------------------------


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://fake", code, "err", None, None)


@pytest.mark.parametrize(
    ("code", "kind"),
    [(401, "AUTH_ERROR"), (403, "AUTH_ERROR"), (429, "QUOTA_EXCEEDED"), (400, "BAD_REQUEST")],
)
def test_terminal_http_errors_skip_retry(code, kind):
    provider = FakeProvider([_http_error(code), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 1  # 재시도 없음 — Quota 를 더 태우지 않는다
    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == kind


def test_timeout_retries_once_then_records_kind():
    provider = FakeProvider([TimeoutError(), TimeoutError()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 2
    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "TIMEOUT"


def test_retry_then_success_leaves_no_error_kind():
    """최종 상태만 기록한다 — 재시도 후 성공이면 error_kind 는 null 이다."""
    provider = FakeProvider([TimeoutError(), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert result.llm_error_kind is None


def test_validation_fallback_records_validation_failed():
    provider = FakeProvider([_output(summary="수치 3 포함"), _output(summary="수치 5 포함")])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "VALIDATION_FAILED"


def test_transport_and_validation_retries_have_independent_budgets():
    """timeout → 잘못된 출력 → 교정 출력 — 전송 재시도가 correction 기회를 먹으면 안 된다."""
    provider = FakeProvider([TimeoutError(), _output(summary="수치 3 포함"), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 3
    assert result.llm_error_kind is None


# ---------------------------------------------------------------------------
# 설정 폴백 — 모델은 provider 종속 값이다 (경계를 넘는 상속 금지)
# ---------------------------------------------------------------------------


def _pin_env(monkeypatch, **values):
    """관련 키 전부를 고정한다 — 빈 문자열은 '미설정'으로 동작한다 (or 폴백 사슬).

    load_dotenv 는 이미 있는 환경변수를 덮지 않으므로, .env 파일 값이 테스트에
    새어 들어오지 않게 모든 관련 키를 명시적으로 setenv 한다.
    """
    defaults = {
        "LLM_PROVIDER": "",
        "LLM_MODEL": "",
        "LOGISTICS_LLM_PROVIDER": "",
        "LOGISTICS_LLM_MODEL": "",
    }
    defaults.update(values)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)


def test_same_provider_still_inherits_global_model(monkeypatch):
    """물류 provider 를 명시해도 전역과 같으면 전역 모델 상속(폴백 사슬)은 유지된다."""
    from app.logistics.llm.runtime import get_llm_settings

    _pin_env(
        monkeypatch,
        LLM_PROVIDER="ollama",
        LLM_MODEL="custom-team-model",
        LOGISTICS_LLM_PROVIDER="ollama",
    )

    assert get_llm_settings().model == "custom-team-model"


def test_cross_provider_global_model_is_not_inherited(monkeypatch):
    """provider 가 다르면 전역 모델(Ollama 용)을 건너뛰고 provider 기본으로 간다."""
    from app.logistics.llm.runtime import get_llm_settings

    _pin_env(
        monkeypatch,
        LLM_PROVIDER="ollama",
        LLM_MODEL="gemma3:4b",
        LOGISTICS_LLM_PROVIDER="gemini",
    )

    settings = get_llm_settings()

    assert settings.provider == "gemini"
    assert settings.model == "gemini-3.5-flash-lite"


def test_explicit_logistics_model_always_wins(monkeypatch):
    from app.logistics.llm.runtime import get_llm_settings

    _pin_env(
        monkeypatch,
        LLM_PROVIDER="ollama",
        LLM_MODEL="gemma3:4b",
        LOGISTICS_LLM_PROVIDER="gemini",
        LOGISTICS_LLM_MODEL="gemini-custom-pin",
    )

    assert get_llm_settings().model == "gemini-custom-pin"


def _service_without_transport_retry(provider):
    return InterpretationService(
        LLMSettings(
            enabled=True,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=0,
        ),
        provider,
    )


def test_validation_correction_survives_zero_transport_retries():
    """MAX_RETRIES=0 은 전송 재시도만 끈다 — correction 1회는 정책 고정이다 (결정서 §6)."""
    provider = FakeProvider([_output(summary="수치 3 포함"), _output()])

    result = _service_without_transport_retry(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert result.llm_error_kind is None


def test_zero_transport_retries_fall_back_on_first_timeout():
    provider = FakeProvider([TimeoutError(), _output()])

    result = _service_without_transport_retry(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 1
    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "TIMEOUT"
