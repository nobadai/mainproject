"""Finance가 소유하는 Ollama Provider, Policy, Validator, 재시도 및 fallback Runtime."""

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv
from pydantic import ValidationError

from app.finance.llm.schemas import (
    AgentInterpretation,
    InterpretationResult,
    LLMStatus,
    SanitizedLLMContext,
)

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"
_NUMERIC_PATTERN = re.compile(r"\d")
_SENTENCE_SPLIT = re.compile(r"[.!?。]+")
_MAX_SUMMARY_CHARACTERS = 240
_COMPOSITE_SIGNALS = {
    "CASH_BUFFER_LOW",
    "COST_MISMATCH",
    "PAYABLES_DUE_SOON",
    "RECEIVABLES_CONCENTRATION",
}
SYSTEM_PROMPT = """당신은 Finance Agent의 해석 레이어다.
입력 Context는 deterministic Core와 Rule 검증을 통과했다.
계산기나 결정 엔진이 아니며 질적 설명만 작성한다.

규칙:
- 숫자, 날짜, 금액, 수량, 비율, 용량을 출력하지 않는다.
- 계산하거나 추정하지 않는다.
- risks에는 signals에 있는 코드만 사용한다.
- 모든 signal을 정확히 한 번 보존한다.
- 새로운 위험이나 원인을 생성하지 않는다.
- facts의 의미를 과장하지 않는다.
- summary는 최대 두 문장으로 작성하고 반복하지 않는다.
- suggested_adjustment는 allowed_adjustments 중 하나만 선택한다.
- allowed_adjustments가 비어 있으면 suggested_adjustment는 null이다.
- 지정된 JSON Schema에 맞는 JSON만 출력한다."""


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int


class LLMProvider(Protocol):
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str: ...


class OllamaProvider:
    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        user_payload: dict[str, object] = {"context": context.model_dump(mode="json")}
        if retry_guidance:
            user_payload["correction"] = retry_guidance
        payload = {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "format": AgentInterpretation.model_json_schema(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "options": {"temperature": 0, "num_ctx": 4096},
        }
        request = urllib.request.Request(
            f"{self.settings.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError("Finance Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Finance Local LLM response did not contain message content")
        return content


class UnavailableProvider:
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        del context, retry_guidance
        raise RuntimeError("Configured Finance LLM provider is not supported")


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


class InterpretationService:
    def __init__(self, settings: LLMSettings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def interpret(
        self,
        context: SanitizedLLMContext,
        *,
        runtime_ready: bool,
        has_blocking_constraints: bool,
    ) -> InterpretationResult:
        template = build_template_interpretation(context)
        if not self.settings.enabled:
            return self._result(template, status="DISABLED", attempts=0, fallback=False)
        if not needs_llm(
            context,
            runtime_ready=runtime_ready,
            has_blocking_constraints=has_blocking_constraints,
        ):
            return self._result(
                template,
                status="SKIPPED_TEMPLATE",
                attempts=0,
                fallback=False,
            )

        guidance = None
        attempts = 0
        for _ in range(self.settings.max_retries + 1):
            attempts += 1
            try:
                raw_output = self.provider.generate(context, retry_guidance=guidance)
                interpretation = validate_interpretation(raw_output, context)
                return self._result(
                    interpretation,
                    status="SUCCESS",
                    attempts=attempts,
                    fallback=False,
                )
            except InterpretationValidationError as error:
                guidance = retry_guidance(error.issues)
            except Exception:  # noqa: BLE001 - optional LLM cannot fail Finance Core.
                guidance = ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]
        return self._result(template, status="FALLBACK", attempts=attempts, fallback=True)

    def _result(
        self,
        interpretation: AgentInterpretation,
        *,
        status: LLMStatus,
        attempts: int,
        fallback: bool,
    ) -> InterpretationResult:
        return InterpretationResult(
            interpretation=interpretation,
            llm_status=status,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model,
            llm_attempts=attempts,
            llm_fallback_used=fallback,
        )


def get_llm_settings() -> LLMSettings:
    load_dotenv(_ENV_FILE)
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=os.getenv("LLM_PROVIDER", "ollama").strip().lower(),
        model=os.getenv("LLM_MODEL", "gemma3:4b").strip(),
        base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=max(0.1, float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))),
        max_retries=min(1, max(0, int(os.getenv("LLM_MAX_RETRIES", "1")))),
    )


def get_interpretation_service() -> InterpretationService:
    settings = get_llm_settings()
    provider: LLMProvider = (
        OllamaProvider(settings) if settings.provider == "ollama" else UnavailableProvider()
    )
    return InterpretationService(settings, provider)


def needs_llm(
    context: SanitizedLLMContext,
    *,
    runtime_ready: bool,
    has_blocking_constraints: bool,
) -> bool:
    if not runtime_ready or has_blocking_constraints:
        return False
    return len(set(context.signals) & _COMPOSITE_SIGNALS) >= 2


def validate_interpretation(
    raw_output: str,
    context: SanitizedLLMContext,
) -> AgentInterpretation:
    try:
        interpretation = AgentInterpretation.model_validate_json(raw_output)
    except ValidationError as error:
        raise InterpretationValidationError([ValidationIssue.INVALID_SCHEMA]) from error
    issues = _validation_issues(interpretation, context)
    if issues:
        raise InterpretationValidationError(issues)
    return interpretation


def _validation_issues(
    interpretation: AgentInterpretation,
    context: SanitizedLLMContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    output_text = " ".join(
        [interpretation.summary, *interpretation.risks, interpretation.suggested_adjustment or ""]
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
    normalized = [" ".join(sentence.split()) for sentence in sentences]
    if len(normalized) != len(set(normalized)):
        issues.append(ValidationIssue.REPETITIVE_OUTPUT)
    adjustment = interpretation.suggested_adjustment
    if adjustment is not None and adjustment not in context.allowed_adjustments:
        issues.append(ValidationIssue.UNSUPPORTED_ADJUSTMENT)
    return issues


def retry_guidance(issues: list[ValidationIssue]) -> list[str]:
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


def build_template_interpretation(context: SanitizedLLMContext) -> AgentInterpretation:
    summary = (
        " ".join(context.facts[:2])
        if context.facts
        else "결정론적 재무 검토 결과 별도 위험 신호가 확인되지 않았습니다."
    )
    return AgentInterpretation(
        summary=summary,
        risks=list(context.signals),
        suggested_adjustment=(
            context.allowed_adjustments[0] if context.allowed_adjustments else None
        ),
    )


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
