"""Logistics-owned LLM providers (Ollama·Gemini), policy, validator, retry and fallback runtime."""

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

from app.logistics.llm.schemas import (
    AgentInterpretation,
    ContextFact,
    InterpretationResult,
    LLMErrorKind,
    LLMStatus,
    SanitizedLLMContext,
)

#: backend/.env 와 저장소 루트 .env 를 순서대로 읽는다 — 마스터 _ENV_FILES 패턴.
#: 팀 환경에서 .env 가 루트에 있는 경우 backend/ 만 보면 키를 못 찾는다.
_ENV_FILES = (
    Path(__file__).resolve().parents[3] / ".env",
    Path(__file__).resolve().parents[4] / ".env",
)
#: 물류 전용 환경변수 접두어 — 마스터의 MASTER_ 패턴 복제 (결정서 §6).
#: 전역 LLM_PROVIDER 하나로 다른 Agent 까지 함께 바뀌는 것을 막는다.
_ENV_PREFIX = "LOGISTICS_"
#: Provider 별 기본 모델. Gemini 는 stable 버전을 pin 한다 — latest/preview 같은
#: 자동 갱신 별칭은 출력 성향이 예고 없이 바뀌므로 금지 (결정서 §6).
_DEFAULT_MODELS = {
    "ollama": "gemma3:4b",
    "gemini": "gemini-3.5-flash-lite",
}
#: 숫자+단위 결합 토큰 (v1.3 인용 화이트리스트). 부호까지 포함해 하나로 추출한다 —
#: "-30%"·"+30%"가 허용 토큰 "30%"의 부분 문자열로 통과하는 우회를 막는다 (교차 검증
#: 지적). "%"에는 부정 전방탐색을 걸어 "30%%"가 "30%"로 잘려 통과하지 않게 한다
#: ("%%"에서 % 단위 매치가 실패하면 무단위 토큰 "30"이 되어 거부된다 — fail-closed).
#: 단위 없는 숫자("0.92")도 토큰으로 잡혀 화이트리스트 대조에서 거부된다.
#: 한글 단위 뒤에 조사가 붙는 경우("3개이며")는 패턴이 아니라 _is_quoted_token 의
#: 조사 화이트리스트가 처리한다 — "3개월" 같은 단위 연장은 조사가 아니므로 거부된다.
_NUMERIC_TOKEN_PATTERN = re.compile(r"[+-]?\d(?:[\d.,/]*\d)?(?:%p|%(?!%)|[A-Za-z]+|[가-힣]+)?")
#: 문장 구분자 — 마침표는 숫자 사이 소수점("91.7%")을 제외한다. 소수점을 문장으로
#: 세면 표기 스펙이 공식 지원하는 소수 표기가 TOO_MANY_SENTENCES 로 오거부된다.
_SENTENCE_SPLIT = re.compile(r"(?:(?<!\d)\.(?!\d)|[!?。])+")
_MAX_SUMMARY_CHARACTERS = 240
#: 단독으로 LLM 을 호출할 수 있는 질적 업무 위험 (LLM 정책 결정서 §2).
_QUALITATIVE_SIGNALS = {
    "FRESHNESS_QUALITY_RISK",
    "INVENTORY_FRESHNESS_PRESSURE",
    "SCENARIO_ADJUSTMENT_REQUIRED",
}
#: 복합 위험 화이트리스트 — 2개 이상 겹치면 호출. 데이터 미확정 코드는 넣지 않는다.
#:
#: ★ 이 분기는 **의도적 휴면 상태다.** 현재 성립 가능한 조합에는 항상
#:   INVENTORY_FRESHNESS_PRESSURE(Qualitative)가 끼어 단독 분기가 먼저 잡는다.
#:   SCENARIO_ADJUSTMENT_REQUIRED 도 Qualitative 라 여기 넣어도 휴면이 안 풀린다 —
#:   넣지 않는다. 단독 호출 대상이 아닌 업무 위험(예: 품목 재고 부족 signal)이
#:   추가되는 날 처음으로 살아난다. 죽은 코드가 아니라 확장 자리다.
_COMPOSITE_SIGNALS = {"CAPACITY_TIGHT", "INVENTORY_FRESHNESS_PRESSURE"}
SYSTEM_PROMPT = """당신은 Inventory/Logistics Agent의 해석 레이어다.
입력 Context는 deterministic Core와 Rule 검증을 통과했다.
계산기나 결정 엔진이 아니며 질적 설명만 작성한다.

규칙:
- 숫자를 새로 만들지 않는다. facts의 display_value 표기만 그대로 인용할 수 있고,
  가능하면 label의 의미와 함께 서술한다. 환산·반올림·단위 변경도 새 숫자다.
- facts에 없는 날짜, 금액, 수량, 비율, 용량을 출력하지 않는다.
- 계산하거나 추정하지 않는다.
- risks에는 signals에 있는 코드만 사용한다.
- 모든 signal을 정확히 한 번 보존한다.
- 새로운 위험이나 원인을 생성하지 않는다.
- facts의 의미를 과장하지 않는다.
- summary는 최대 두 문장으로 작성하고 반복하지 않는다.
- summary는 업무 위험 설명을 우선하고, 공간이 남는 경우에만 missing_data를 언급한다.
- missing_data에 없는 부족 정보를 새로 만들지 않는다.
- suggested_adjustment는 allowed_adjustments 중 하나만 선택한다.
- preferred_adjustment가 있으면 suggested_adjustment는 반드시 그 값이어야 하며
  그 방향만 설명한다.
- preferred_adjustment가 null이면 suggested_adjustment도 null이다 — 조정 방향을
  스스로 고르지 않는다.
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
            raise RuntimeError("Logistics Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Logistics Local LLM response did not contain message content")
        return content


#: Gemini API 기본 엔드포인트. LOGISTICS_GEMINI_BASE_URL 로만 바꾼다 —
#: LLM_BASE_URL 은 Ollama 로컬 주소라 의미가 다르다.
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Gemini 구조화 출력 스키마. AgentInterpretation 3필드를 Gemini 의 OpenAPI 서브셋
#: 으로 평탄화한 것이다 — pydantic json_schema 의 anyOf 는 지원 범위 밖일 수 있어
#: 손으로 고정한다. 필드가 늘면 여기도 같이 는다 (지금은 늘리지 않는 게 계약이다).
_GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "suggested_adjustment": {"type": "STRING", "nullable": True},
    },
    "required": ["summary", "risks", "suggested_adjustment"],
}


class GeminiProvider:
    """Gemini REST 호출. 정책 판정은 하지 않는다 — 호출·구조화 응답·오류 전달만.

    ★ API Key 는 호출 시점에 환경변수에서 읽는다 — `LLMSettings` 에 저장하지 않고
      로그·예외 메시지에도 원문을 싣지 않는다 (결정서 §6). 키는 Auth Key 로 신규
      발급한다 (2026-09 부터 Standard 키 전면 거부).
    ★ SDK 를 쓰지 않고 표준 라이브러리만 쓴다 — 팀 Ollama 경로와 같은 규율이고,
      HTTPError 가 그대로 전파되어 classify_llm_error 가 상태 코드로 분류한다.
    ★ 자체 재시도는 없다 — 재시도는 InterpretationService 가 소유한다.
    """

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        api_key = os.getenv(f"{_ENV_PREFIX}GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ProviderAuthError("GEMINI_API_KEY is not set")
        user_payload: dict[str, object] = {"context": context.model_dump(mode="json")}
        if retry_guidance:
            user_payload["correction"] = retry_guidance
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _GEMINI_RESPONSE_SCHEMA,
            },
        }
        base_url = (os.getenv(f"{_ENV_PREFIX}GEMINI_BASE_URL") or _GEMINI_BASE_URL).rstrip("/")
        request = urllib.request.Request(
            f"{base_url}/models/{self.settings.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        # HTTPError·URLError·TimeoutError 는 그대로 전파한다 — 분류는
        # classify_llm_error 한 곳이 한다 (여기서 삼키면 재시도 정책이 눈을 잃는다).
        with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
            document = json.loads(response.read().decode("utf-8"))
        try:
            content = document["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as error:
            raise TypeError("Gemini response did not contain text content") from error
        if not isinstance(content, str):
            raise TypeError("Gemini response text content was not a string")
        return content


class ProviderConfigurationError(RuntimeError):
    """설정 문제(미지원 Provider·모델 등) — 다시 불러도 같으므로 즉시 FALLBACK."""


class ProviderAuthError(RuntimeError):
    """API Key 부재·인증 거부 — 재시도 무의미. Key 원문은 절대 싣지 않는다."""


class UnavailableProvider:
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        del context, retry_guidance
        raise ProviderConfigurationError("Configured Logistics LLM provider is not supported")


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
    PREFERRED_ADJUSTMENT_VIOLATION = "PREFERRED_ADJUSTMENT_VIOLATION"


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
        facts_overflow: bool = False,
    ) -> InterpretationResult:
        template = build_template_interpretation(context)
        if not self.settings.enabled:
            return self._result(template, status="DISABLED", attempts=0, fallback=False)
        if facts_overflow or not needs_llm(
            context,
            runtime_ready=runtime_ready,
            has_blocking_constraints=has_blocking_constraints,
        ):
            # fact 상한 초과는 조용한 절단이 아니라 LLM 미호출이다 (v1.3 §5) —
            # 결정론 결과 + 무숫자 Template 유지, overflow 자체는 조립기가 로그로 남긴다.
            return self._result(
                template,
                status="SKIPPED_TEMPLATE",
                attempts=0,
                fallback=False,
            )
        # 여기부터는 호출 확정이다 — provider.generate(context)의 입력으로 쓰이므로
        # 전송 전에 실패(AUTH_ERROR 등)해도 llm_context_facts에 기록된다 (v1.3 §5).
        context_facts = list(context.facts)

        # 전송 재시도와 검증(correction) 재시도는 **별도 예산**이다 (결정서 §6).
        # 하나의 카운터를 공유하면 첫 호출이 timeout 일 때 검증 실패의 correction
        # 기회가 사라진다 — timeout → 잘못된 출력 → 교정 출력 순서가 성립해야 한다.
        # 최악 호출 수는 1 + 전송 1 + 검증 1 = 3회로 유한하다.
        guidance = None
        attempts = 0
        error_kind: LLMErrorKind | None = None
        transport_retries_left = self.settings.max_retries
        # 검증 correction 은 정책 고정 1회다 (결정서 §6). MAX_RETRIES 는 전송 재시도의
        # 손잡이라 0 으로 꺼도 correction 경로까지 꺼지면 안 된다 — 교차 검토 지적.
        validation_retries_left = 1
        while True:
            attempts += 1
            try:
                raw_output = self.provider.generate(context, retry_guidance=guidance)
            except Exception as error:  # noqa: BLE001 - optional LLM cannot fail Logistics Core.
                # 전송 실패 — 재시도 가치가 있는 오류만 다시 해본다 (결정서 §6).
                # AUTH·QUOTA·BAD_REQUEST 는 다시 불러도 같으므로 즉시 FALLBACK.
                retryable, error_kind = classify_llm_error(error)
                if retryable and transport_retries_left > 0:
                    transport_retries_left -= 1
                    continue
                break
            try:
                interpretation = validate_interpretation(raw_output, context)
            except InterpretationValidationError as error:
                # 검증 실패 — 전송 재시도와 별개 경로. correction 을 붙여 다시 시도한다.
                error_kind = "VALIDATION_FAILED"
                if validation_retries_left > 0:
                    validation_retries_left -= 1
                    guidance = retry_guidance(error.issues)
                    continue
                break
            return self._result(
                interpretation,
                status="SUCCESS",
                attempts=attempts,
                fallback=False,
                context_facts=context_facts,
            )
        return self._result(
            template,
            status="FALLBACK",
            attempts=attempts,
            fallback=True,
            error_kind=error_kind,
            context_facts=context_facts,
        )

    def _result(
        self,
        interpretation: AgentInterpretation,
        *,
        status: LLMStatus,
        attempts: int,
        fallback: bool,
        error_kind: LLMErrorKind | None = None,
        context_facts: list[ContextFact] | None = None,
    ) -> InterpretationResult:
        return InterpretationResult(
            interpretation=interpretation,
            llm_status=status,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model,
            llm_attempts=attempts,
            llm_fallback_used=fallback,
            # 최종 상태만 기록한다 — 재시도 후 성공이면 None (중간 실패는 로그 몫).
            llm_error_kind=error_kind,
            # 호출 확정된 facts만 기록 — SKIPPED_TEMPLATE·DISABLED는 빈 목록.
            llm_context_facts=list(context_facts or []),
        )


def _env(key: str, default: str) -> str:
    return os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key) or default


def _int_env(key: str, default: str, *, minimum: int) -> int:
    """파싱 실패는 기본값으로 되돌린다 — `.env` 오타 하나로 물류가 죽으면 안 된다."""
    try:
        return max(minimum, int(_env(key, default)))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _float_env(key: str, default: str, *, minimum: float) -> float:
    try:
        return max(minimum, float(_env(key, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def get_llm_settings() -> LLMSettings:
    for env_file in _ENV_FILES:
        load_dotenv(env_file)
    scoped_provider = os.getenv(f"{_ENV_PREFIX}LLM_PROVIDER")
    global_provider = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
    provider = (scoped_provider or global_provider).strip().lower()
    # 모델은 provider 에 종속된 값이다. 물류 provider 가 전역과 **다를 때** 전역
    # LLM_MODEL(예: Ollama 의 gemma3:4b)을 상속하면 Gemini 가 존재하지 않는 모델로
    # 호출돼 400 이 난다 — 실호출 검증에서 실제로 발생한 사례다. 그 경우에만 전역
    # 모델을 건너뛴다. provider 가 같으면(예: 둘 다 ollama) 전역 모델은 유효한
    # 상속이므로 폴백 사슬(LOGISTICS_ → 전역 → 기본값)을 그대로 따른다.
    if provider != global_provider and not os.getenv(f"{_ENV_PREFIX}LLM_MODEL"):
        model = _DEFAULT_MODELS.get(provider, "")
    else:
        model = _env("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=provider,
        model=model.strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        # 기본 10초 (PROVISIONAL) — 원격 API 장애 시 최악 경로가 재시도 포함 약 20초
        # 에서 끊기도록 잡는다. AI 는 보조 기능이라 물류 응답을 오래 잡으면 안 된다.
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", "10", minimum=0.1),
        max_retries=min(1, _int_env("LLM_MAX_RETRIES", "1", minimum=0)),
    )


#: Provider registry — Ollama 는 제거하지 않는다. 선택은 물류 전용 env 가 정한다.
_PROVIDERS: dict[str, type] = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
}


def get_interpretation_service() -> InterpretationService:
    settings = get_llm_settings()
    factory = _PROVIDERS.get(settings.provider)
    provider: LLMProvider = factory(settings) if factory else UnavailableProvider()
    return InterpretationService(settings, provider)


def classify_llm_error(error: Exception) -> tuple[bool, LLMErrorKind]:
    """전송 실패를 (재시도 가능 여부, 기록 분류)로 판정한다.

    분류 로직은 이 함수 한 곳이다 — 재시도 정책과 llm_error_kind 기록이 같은
    결과를 쓰므로 둘이 어긋날 수 없다 (결정서 §6). 오류 메시지의 세부(Key 원문 등)
    는 분류값으로만 남고 그대로 실리지 않는다.
    """
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ProviderAuthError):
            return False, "AUTH_ERROR"
        if isinstance(cause, ProviderConfigurationError):
            return False, "BAD_REQUEST"
        if isinstance(cause, urllib.error.HTTPError):
            if cause.code in (401, 403):
                return False, "AUTH_ERROR"
            if cause.code == 429:
                return False, "QUOTA_EXCEEDED"
            if 400 <= cause.code < 500:
                return False, "BAD_REQUEST"
            return True, "SERVER_ERROR"
        if isinstance(cause, TimeoutError):
            return True, "TIMEOUT"
        if isinstance(cause, urllib.error.URLError):
            if isinstance(cause.reason, TimeoutError):
                return True, "TIMEOUT"
            return True, "NETWORK_ERROR"
        if isinstance(cause, json.JSONDecodeError | TypeError):
            return True, "INVALID_RESPONSE"
        cause = cause.__cause__
    # 분류할 수 없는 예외 — 일시적일 수 있으므로 기존 동작(1회 재시도)을 유지한다.
    return True, "NETWORK_ERROR"


def needs_llm(
    context: SanitizedLLMContext,
    *,
    runtime_ready: bool,
    has_blocking_constraints: bool,
) -> bool:
    if not runtime_ready or has_blocking_constraints:
        return False
    signals = set(context.signals)
    if signals & _QUALITATIVE_SIGNALS:
        return True
    return len(signals & _COMPOSITE_SIGNALS) >= 2


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
    # 숫자 검사는 v1.3부터 인용 화이트리스트다 — 출력의 모든 숫자+단위 토큰이
    # display_value 토큰 집합에 완전 일치해야 한다. 새 숫자의 생성·계산·환산
    # ("0.92"·"93%"·"2%p")만 차단하고 자연어 수식("약")은 통제하지 않는다.
    # 검사 범위는 summary·suggested_adjustment 유지. risks 는 signal 코드 보존
    # 필드라 제외한다 — 코드에 숫자가 든 signal 이 오면(방어선) 보존 규칙과
    # 숫자 규칙이 충돌해 구조적으로 매번 FALLBACK 이 되기 때문이다 (결정서 §5).
    numeric_scope = " ".join([interpretation.summary, interpretation.suggested_adjustment or ""])
    allowed_tokens = _allowed_numeric_tokens(context)
    output_tokens = _NUMERIC_TOKEN_PATTERN.findall(numeric_scope)
    if any(not _is_quoted_token(token, allowed_tokens) for token in output_tokens):
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
    # preferred 일관성 — Prompt 만 믿지 않는다 (결정서 §5). Rule 이 정한 방향과
    # 다른 추천이 나가면 화면에서 결정론 결과와 LLM 이 서로 다른 말을 하게 된다.
    if context.preferred_adjustment is not None:
        if adjustment != context.preferred_adjustment:
            issues.append(ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION)
    elif adjustment is not None:
        issues.append(ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION)
    return issues


#: 한글 단위 토큰 뒤에 이어질 수 있는 조사·어미 (fail-closed 화이트리스트).
#: "3개이며"는 "3개" 인용 + 조사 "이며"다 — 여기 없는 접미("3개월"의 "월")는 단위
#: 연장으로 간주해 거부한다. 조사를 패턴에서 탐욕 매치로 흡수하면 정당한 인용이
#: 전부 깨지고, 무제한 허용하면 단위 바꿔치기가 뚫린다 — 목록 대조가 그 사이다.
_KOREAN_PARTICLE_SUFFIXES = frozenset(
    {
        "이",
        "가",
        "은",
        "는",
        "을",
        "를",
        "와",
        "과",
        "의",
        "도",
        "만",
        "씩",
        "이며",
        "이고",
        "이라",
        "이라서",
        "라서",
        "이므로",
        "이니",
        "인",
        "임",
        "이다",
        "입니다",
        "이었습니다",
        "였습니다",
        "이었고",
        "였고",
        "이었으며",
        "였으며",
        "로",
        "으로",
        "에",
        "에서",
        "부터",
        "까지",
        "보다",
        "처럼",
        "만큼",
        "조차",
        "마저",
    }
)


def _allowed_numeric_tokens(context: SanitizedLLMContext) -> frozenset[str]:
    """display_value 표기에서 인용 가능한 숫자+단위 토큰 집합 (fail-closed의 기준).

    출력 검사와 **같은 패턴**으로 추출한다 — 추출 규칙이 두 벌이면 화이트리스트와
    검사가 어긋난다. "91.7% (임계 90%)" 한 fact의 두 토큰이 모두 인용 가능하다.
    측정 표기의 ".0"(예: "25.0%")은 trailing zero 제거형("25%")도 함께 허용한다 —
    같은 fact 안의 임계 표기("30%")가 zero 를 떼는 규칙이라 LLM 이 따라 떼기 쉽고,
    값이 같은 표기 동치라 fail-closed 를 깨지 않는다. 반올림·환산은 여전히 거부된다.
    """
    tokens: set[str] = set()
    for fact in context.facts:
        for token in _NUMERIC_TOKEN_PATTERN.findall(fact.display_value):
            tokens.add(token)
            if token.endswith(".0%"):
                tokens.add(token.removesuffix(".0%") + "%")
    return frozenset(tokens)


def _is_quoted_token(token: str, allowed: frozenset[str]) -> bool:
    """토큰이 허용 표기의 정확한 인용인가 — 부분 일치 금지, 한글 조사만 예외."""
    if token in allowed:
        return True
    # 한글 단위 토큰은 조사가 붙어 추출된다("3개이며") — 허용 토큰 + 조사 화이트리스트
    # 조합만 통과시킨다. "3개월"의 "월"처럼 목록에 없는 접미는 거부된다.
    return any(
        token.startswith(quoted) and token[len(quoted) :] in _KOREAN_PARTICLE_SUFFIXES
        for quoted in allowed
    )


def retry_guidance(issues: list[ValidationIssue]) -> list[str]:
    guidance = []
    if ValidationIssue.INVALID_SCHEMA in issues:
        guidance.append("지정된 세 필드만 포함한 유효한 JSON을 작성하세요.")
    if ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in issues:
        guidance.append(
            "숫자는 facts의 display_value 표기만 그대로 인용하세요. "
            "새 숫자를 만들거나 환산·반올림하지 마세요."
        )
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
    if ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION in issues:
        guidance.append(
            "preferred_adjustment가 있으면 suggested_adjustment는 그 값이어야 하고, "
            "없으면 null이어야 합니다."
        )
    return guidance


#: Template Fallback의 무숫자 고정 문형 — signal 코드별 사람용 의미.
#: v1.3에서 facts가 수치 표기(ContextFact)로 바뀌었지만 Template은 무숫자 문형을
#: 유지한다 — Fallback까지 인용 검증 대상으로 만들지 않는다 (결정서 §5).
_TEMPLATE_SIGNAL_PHRASES = {
    "CAPACITY_TIGHT": "확정 입출고를 반영한 미래 창고 여유가 운영 임계 수준 이하입니다.",
    "FRESHNESS_QUALITY_RISK": "재고의 우선 출고와 품질 위험 검토가 필요합니다.",
    "INVENTORY_FRESHNESS_PRESSURE": "기존 재고의 신선도 잔여가 보관한계 대비 충분하지 않습니다.",
    "SCENARIO_ADJUSTMENT_REQUIRED": "매입안이 물류 경계에 걸려 조정 검토가 필요합니다.",
}


def build_template_interpretation(context: SanitizedLLMContext) -> AgentInterpretation:
    phrases = [
        _TEMPLATE_SIGNAL_PHRASES.get(signal, "정의되지 않은 재고물류 신호가 확인되었습니다.")
        for signal in context.signals[:2]
    ]
    summary = (
        " ".join(phrases)
        if phrases
        else "결정론적 재고물류 검토 결과 별도 위험 신호가 확인되지 않았습니다."
    )
    return AgentInterpretation(
        summary=summary,
        risks=list(context.signals),
        # 템플릿도 preferred 규칙을 따른다 — Rule 이 방향을 안 정했으면 추천하지
        # 않는다. allowed[0] 자동 추천은 preferred 강제와 충돌해 폐기했다.
        suggested_adjustment=context.preferred_adjustment,
    )


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
