import json

from app.logistics.llm.runtime import InterpretationService, LLMSettings
from app.logistics.llm.schemas import SanitizedLLMContext


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


def _context():
    return SanitizedLLMContext(
        signals=["FRESHNESS_QUALITY_RISK"],
        facts=["재고의 우선 출고와 품질 위험 검토가 필요합니다."],
        allowed_adjustments=["우선 출고 대상으로 검토합니다."],
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
