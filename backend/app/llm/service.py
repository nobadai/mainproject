"""Optional interpretation orchestration with retry and deterministic fallback."""

from app.llm.config import LLMSettings, get_llm_settings
from app.llm.policy import needs_llm
from app.llm.provider import LLMProvider, create_provider
from app.llm.schemas import (
    AgentInterpretation,
    InterpretationResult,
    SanitizedLLMContext,
)
from app.llm.validator import (
    InterpretationValidationError,
    retry_guidance,
    validate_interpretation,
)


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
            except Exception:  # noqa: BLE001 - LLM failures cannot fail deterministic Agents.
                guidance = ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]

        return self._result(template, status="FALLBACK", attempts=attempts, fallback=True)

    def fallback(self, context: SanitizedLLMContext) -> InterpretationResult:
        return self._result(
            build_template_interpretation(context),
            status="FALLBACK",
            attempts=0,
            fallback=True,
        )

    def _result(
        self,
        interpretation: AgentInterpretation,
        *,
        status: str,
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


def build_template_interpretation(context: SanitizedLLMContext) -> AgentInterpretation:
    if context.facts:
        summary = " ".join(context.facts[:2])
    else:
        summary = "결정론적 검토 결과 별도 위험 신호가 확인되지 않았습니다."
    return AgentInterpretation(
        summary=summary,
        risks=list(context.signals),
        suggested_adjustment=(
            context.allowed_adjustments[0] if context.allowed_adjustments else None
        ),
    )


def get_interpretation_service() -> InterpretationService:
    settings = get_llm_settings()
    return InterpretationService(settings, create_provider(settings))
