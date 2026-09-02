"""Sales LLM Runtime은 실제 호출 경로를 대체해 안전성만 단위 검증한다."""

from app.sales.llm.runtime import interpret_candidates
from app.sales.llm.schemas import LlmInterpretationOutput
from app.sales.schemas import AllocationLeg, SalesCandidate


def _candidate() -> SalesCandidate:
    return SalesCandidate(
        candidate_id="C-1", allocation=[AllocationLeg(channel="B2B", qty_kg=1, unit_price=1)]
    )


def test_llm_disabled_returns_deterministic_recommendation(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    result = interpret_candidates([_candidate()])
    assert result.status == "DISABLED"
    assert result.recommended_candidate_id == "C-1"


def test_llm_success_keeps_candidate_values_and_returns_korean(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("SALES_LLM_MODEL", "test-model")
    monkeypatch.setattr(
        "app.sales.llm.runtime._call_gemini",
        lambda context, settings: LlmInterpretationOutput(
            recommended_candidate_id=context[0].candidate_id,
            summary="확정 가능한 판매안을 우선 추천합니다.",
            recommendation_reason="조건부 의존이 없어 실행 가능성을 우선했습니다.",
            risk_explanation="외부 검증 결과를 함께 확인해 주세요.",
            user_message="현재 확인된 조건으로 진행할 수 있습니다.",
        ),
    )
    candidate = _candidate()
    before = candidate.model_dump()
    result = interpret_candidates([candidate])
    assert result.status == "SUCCESS"
    assert result.recommended_candidate_id == "C-1"
    assert candidate.model_dump() == before


def test_llm_unknown_candidate_and_provider_error_fall_back(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_LLM_MODEL", "test-model")
    monkeypatch.setattr(
        "app.sales.llm.runtime._call_gemini",
        lambda context, settings: LlmInterpretationOutput(
            recommended_candidate_id="UNKNOWN",
            summary="판매안을 추천합니다.",
            recommendation_reason="근거를 검토했습니다.",
            risk_explanation="조건을 확인해 주세요.",
            user_message="확인 후 진행해 주세요.",
        ),
    )
    result = interpret_candidates([_candidate()])
    assert result.status == "FALLBACK"
    assert result.llm_fallback_used is True


def test_llm_unsupported_provider_and_exception_fall_back(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("SALES_LLM_MODEL", "test-model")
    result = interpret_candidates([_candidate()])
    assert result.status == "FALLBACK"
    assert result.llm_fallback_used is True


def test_llm_numeric_or_malformed_response_falls_back(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "true")
    monkeypatch.setenv("SALES_LLM_MODEL", "test-model")
    monkeypatch.setattr(
        "app.sales.llm.runtime._call_gemini",
        lambda context, settings: LlmInterpretationOutput(
            recommended_candidate_id=context[0].candidate_id,
            summary="수량은 1입니다.",
            recommendation_reason="근거를 검토했습니다.",
            risk_explanation="조건을 확인해 주세요.",
            user_message="확인 후 진행해 주세요.",
        ),
    )
    assert interpret_candidates([_candidate()]).status == "FALLBACK"
