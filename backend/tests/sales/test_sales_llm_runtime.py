"""Sales LLM Runtime은 실제 호출 경로를 대체해 안전성만 단위 검증한다."""

from app.sales.llm.runtime import LlmInterpretationOutput, interpret_candidates, load_settings
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


def test_finance_fail_candidate_is_never_recommended(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    candidate = _candidate()
    candidate.risks.append("FINANCE_FAIL")
    result = interpret_candidates([candidate])
    assert result.recommended_candidate_id is None


def test_sales_gemini_model_does_not_inherit_common_ollama_model(monkeypatch):
    monkeypatch.setattr("app.sales.llm.runtime._load_environment", lambda: None)
    monkeypatch.delenv("SALES_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    assert load_settings().model == "gemini-3.5-flash-lite"


# ---------------------------------------------------------------------------
# Gemini responseSchema 낮추기 — 중첩 모델
# ---------------------------------------------------------------------------


def test_nested_model_schema_has_no_ref_or_defs():
    """🔴 **전략 Planner 가 이것 때문에 한 번도 안 돌았다** (2026-09-16 실측).

    Pydantic 은 모델 안에 모델이 있으면 정의를 `$defs` 로 빼고 자리에는 `$ref` 만
    남긴다. Gemini `responseSchema` 는 그 둘을 모르고 요청 자체를 거부한다.

    ```text
    HTTP 400  Unknown name "$defs" at 'generation_config.response_schema'
              Unknown name "$ref"  at '...properties[0].value.items'
    ```

    상태 칸은 정직하게 `FALLBACK` 을 말하고 있었지만 원인이 호출 밖이 아니라
    **우리 스키마**였다.
    """
    import json

    from app.sales.llm.runtime import LlmStrategyPlanOutput, _gemini_safe_schema

    raw = LlmStrategyPlanOutput.model_json_schema()
    assert "$defs" in raw, "중첩이 사라졌다면 이 검사가 무엇을 막는지 다시 본다"

    safe = _gemini_safe_schema(raw)
    wire = json.dumps(safe, ensure_ascii=False)

    assert "$defs" not in wire
    assert "$ref" not in wire


def test_nested_model_schema_keeps_the_contract():
    """펴 넣되 **계약은 그대로다** — 자세 어휘가 살아 있어야 한다."""
    from app.sales.llm.runtime import LlmStrategyPlanOutput, _gemini_safe_schema

    safe = _gemini_safe_schema(LlmStrategyPlanOutput.model_json_schema())
    item = safe["properties"]["strategies"]["items"]

    assert item["properties"]["strategy"]["enum"] == [
        "CONSERVATIVE",
        "BALANCED",
        "AGGRESSIVE",
    ]
    assert item["properties"]["price_posture"]["enum"] == [
        "MARGIN_DEFENSE",
        "MARKET_ALIGNED",
        "DEPLETION",
    ]


def test_unresolvable_reference_is_not_silently_dropped():
    """못 펴는 참조를 빈 칸으로 두면 **계약이 조용히 달라진다.**"""
    import pytest as _pytest

    from app.sales.llm.runtime import _gemini_safe_schema

    with _pytest.raises(TypeError):
        _gemini_safe_schema({"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}})


def test_flat_model_schema_still_passes_through():
    """해석 호출은 중첩이 없다 — 그 길이 안 바뀌었는지 같이 본다."""
    from app.sales.llm.runtime import LlmInterpretationOutput, _gemini_safe_schema

    safe = _gemini_safe_schema(LlmInterpretationOutput.model_json_schema())

    assert set(safe["properties"]) == {
        "recommended_candidate_id",
        "summary",
        "recommendation_reason",
        "risk_explanation",
        "user_message",
    }
