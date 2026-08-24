"""Fail-closed validation for Local LLM interpretations."""

import re
from enum import StrEnum

from pydantic import ValidationError

from app.llm.schemas import AgentInterpretation, SanitizedLLMContext

_NUMERIC_PATTERN = re.compile(r"\d")
_SENTENCE_SPLIT = re.compile(r"[.!?。]+")
_MAX_SUMMARY_CHARACTERS = 240


class ValidationIssue(StrEnum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    NUMERIC_OUTPUT_FORBIDDEN = "NUMERIC_OUTPUT_FORBIDDEN"
    SIGNAL_MISSING = "SIGNAL_MISSING"
    UNSUPPORTED_RISK = "UNSUPPORTED_RISK"
    DUPLICATE_RISK = "DUPLICATE_RISK"
    SUMMARY_TOO_LONG = "SUMMARY_TOO_LONG"
    TOO_MANY_SENTENCES = "TOO_MANY_SENTENCES"
    REPETITIVE_OUTPUT = "REPETITIVE_OUTPUT"
    UNSUPPORTED_ADJUSTMENT = "UNSUPPORTED_ADJUSTMENT"


class InterpretationValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        super().__init__(", ".join(issues))
        self.issues = issues


def validate_interpretation(
    raw_output: str,
    context: SanitizedLLMContext,
) -> AgentInterpretation:
    try:
        interpretation = AgentInterpretation.model_validate_json(raw_output)
    except ValidationError as error:
        raise InterpretationValidationError([ValidationIssue.INVALID_SCHEMA]) from error

    issues: list[ValidationIssue] = []
    output_text = " ".join(
        [
            interpretation.summary,
            *interpretation.risks,
            interpretation.suggested_adjustment or "",
        ]
    )
    if _NUMERIC_PATTERN.search(output_text):
        issues.append(ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN)

    expected_signals = set(context.signals)
    actual_signals = set(interpretation.risks)
    if expected_signals - actual_signals:
        issues.append(ValidationIssue.SIGNAL_MISSING)
    if actual_signals - expected_signals:
        issues.append(ValidationIssue.UNSUPPORTED_RISK)
    if len(interpretation.risks) != len(actual_signals):
        issues.append(ValidationIssue.DUPLICATE_RISK)
    if len(interpretation.summary) > _MAX_SUMMARY_CHARACTERS:
        issues.append(ValidationIssue.SUMMARY_TOO_LONG)

    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(interpretation.summary)
        if sentence.strip()
    ]
    if len(sentences) > 2:
        issues.append(ValidationIssue.TOO_MANY_SENTENCES)
    normalized_sentences = [" ".join(sentence.split()) for sentence in sentences]
    if len(normalized_sentences) != len(set(normalized_sentences)):
        issues.append(ValidationIssue.REPETITIVE_OUTPUT)

    adjustment = interpretation.suggested_adjustment
    if adjustment is not None and adjustment not in context.allowed_adjustments:
        issues.append(ValidationIssue.UNSUPPORTED_ADJUSTMENT)
    if (
        not context.allowed_adjustments
        and adjustment is not None
        and ValidationIssue.UNSUPPORTED_ADJUSTMENT not in issues
    ):
        issues.append(ValidationIssue.UNSUPPORTED_ADJUSTMENT)

    if issues:
        raise InterpretationValidationError(issues)
    return interpretation


def retry_guidance(issues: list[ValidationIssue]) -> list[str]:
    """Translate internal validation issues into non-domain natural-language rules."""
    guidance = []
    if ValidationIssue.INVALID_SCHEMA in issues:
        guidance.append("지정된 세 필드만 포함한 유효한 JSON을 작성하세요.")
    if ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in issues:
        guidance.append("숫자와 날짜를 사용하지 마세요.")
    if ValidationIssue.SIGNAL_MISSING in issues:
        guidance.append("제공된 모든 signal을 risks에 정확히 한 번 포함하세요.")
    if ValidationIssue.UNSUPPORTED_RISK in issues:
        guidance.append("제공된 signal에 없는 위험을 추가하지 마세요.")
    if ValidationIssue.DUPLICATE_RISK in issues:
        guidance.append("같은 risk를 반복하지 마세요.")
    if ValidationIssue.SUMMARY_TOO_LONG in issues or ValidationIssue.TOO_MANY_SENTENCES in issues:
        guidance.append("summary를 짧은 두 문장 이내로 작성하세요.")
    if ValidationIssue.REPETITIVE_OUTPUT in issues:
        guidance.append("같은 내용을 반복하지 마세요.")
    if ValidationIssue.UNSUPPORTED_ADJUSTMENT in issues:
        guidance.append(
            "suggested_adjustment는 허용 목록에서만 선택하고 목록이 비어 있으면 null로 두세요."
        )
    return guidance
