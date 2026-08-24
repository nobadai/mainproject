import json

import pytest

from app.llm.config import LLMSettings, get_llm_settings
from app.llm.schemas import SanitizedLLMContext
from app.llm.service import InterpretationService
from app.llm.validator import InterpretationValidationError, validate_interpretation


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.retry_guidance = []

    def generate(self, context, *, retry_guidance=None):
        del context
        self.calls += 1
        self.retry_guidance.append(retry_guidance)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _settings(*, enabled=True, retries=1):
    return LLMSettings(
        enabled=enabled,
        provider="fake",
        model="fake-model",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1,
        max_retries=retries,
    )


def _context(*, signals=None, allowed_adjustments=None):
    return SanitizedLLMContext(
        domain="LOGISTICS",
        signals=["FRESHNESS_QUALITY_RISK"] if signals is None else signals,
        facts=["재고의 우선 출고와 품질 위험 검토가 필요합니다."],
        allowed_adjustments=allowed_adjustments or [],
    )


def _output(*, summary="품질 위험 검토가 필요합니다.", risks=None, adjustment=None):
    return json.dumps(
        {
            "summary": summary,
            "risks": ["FRESHNESS_QUALITY_RISK"] if risks is None else risks,
            "suggested_adjustment": adjustment,
        },
        ensure_ascii=False,
    )


def test_policy_skip_uses_template_without_provider_call():
    provider = FakeProvider([])
    service = InterpretationService(_settings(), provider)
    context = _context(signals=["COST_MISMATCH"])

    result = service.interpret(
        context,
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert result.llm_attempts == 0
    assert result.llm_fallback_used is False
    assert result.interpretation.risks == ["COST_MISMATCH"]
    assert provider.calls == 0


def test_disabled_runtime_uses_template_without_provider_call():
    provider = FakeProvider([])
    service = InterpretationService(_settings(enabled=False), provider)

    result = service.interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "DISABLED"
    assert result.llm_attempts == 0
    assert provider.calls == 0


def test_model_can_be_replaced_by_environment(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen3:4b")

    assert get_llm_settings().model == "qwen3:4b"


def test_valid_structured_output_returns_success():
    provider = FakeProvider([_output()])
    service = InterpretationService(_settings(), provider)

    result = service.interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 1
    assert result.llm_fallback_used is False


def test_malformed_json_retries_and_succeeds_without_exposing_validator_codes():
    provider = FakeProvider(["not-json", _output()])
    service = InterpretationService(_settings(), provider)

    result = service.interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert provider.calls == 2
    correction = " ".join(provider.retry_guidance[1])
    assert "INVALID_SCHEMA" not in correction
    assert "유효한 JSON" in correction


def test_validator_failure_retries_once_then_falls_back():
    numeric = _output(summary="위험 수치가 3입니다.")
    provider = FakeProvider([numeric, numeric])
    service = InterpretationService(_settings(), provider)

    result = service.interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "FALLBACK"
    assert result.llm_attempts == 2
    assert result.llm_fallback_used is True
    assert result.interpretation.summary == "재고의 우선 출고와 품질 위험 검토가 필요합니다."


def test_provider_unavailable_falls_back_without_losing_context():
    provider = FakeProvider([RuntimeError("unavailable"), RuntimeError("unavailable")])
    service = InterpretationService(_settings(), provider)

    result = service.interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "FALLBACK"
    assert result.interpretation.risks == ["FRESHNESS_QUALITY_RISK"]
    assert result.llm_attempts == 2


@pytest.mark.parametrize(
    ("raw_output", "expected_issue"),
    [
        (_output(summary="위험 수치가 3입니다."), "NUMERIC_OUTPUT_FORBIDDEN"),
        (_output(risks=[]), "SIGNAL_MISSING"),
        (
            _output(risks=["FRESHNESS_QUALITY_RISK", "UNSUPPORTED"]),
            "UNSUPPORTED_RISK",
        ),
        (
            _output(adjustment="허용되지 않은 조정을 수행합니다."),
            "UNSUPPORTED_ADJUSTMENT",
        ),
        (
            _output(risks=["FRESHNESS_QUALITY_RISK", "FRESHNESS_QUALITY_RISK"]),
            "DUPLICATE_RISK",
        ),
        (
            _output(summary="검토가 필요합니다. 품질 위험이 있습니다. 조정이 필요합니다."),
            "TOO_MANY_SENTENCES",
        ),
        (
            _output(summary="품질 위험이 있습니다. 품질 위험이 있습니다."),
            "REPETITIVE_OUTPUT",
        ),
    ],
)
def test_validator_rejects_unsafe_output(raw_output, expected_issue):
    with pytest.raises(InterpretationValidationError) as captured:
        validate_interpretation(raw_output, _context())

    assert expected_issue in {issue.value for issue in captured.value.issues}


def test_retry_can_recover_from_missing_signal():
    provider = FakeProvider([_output(risks=[]), _output()])
    service = InterpretationService(_settings(), provider)

    result = service.interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert "SIGNAL_MISSING" not in " ".join(provider.retry_guidance[1])
