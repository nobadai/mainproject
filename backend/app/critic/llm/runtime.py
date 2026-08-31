"""Critic-owned Ollama provider, policy, validator, retry and fallback runtime.

L5_SYSTEM_PROMPT 는 `selector_llm.py`(설계 원본)의 L4 판정 프롬프트에서 옮겨왔다.
런타임 골격(설정·재시도·상태 결정 순서)은 Finance / Logistics / Orchestrator 와 동일하다.

★ temperature 는 항상 0 이고 프롬프트는 생성 측과 완전히 분리된다 (설계서 §6.4).
  같은 모델·같은 프롬프트를 쓰면 자기가 만든 논리를 자기가 승인한다.
"""

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

from app.critic.llm.schemas import (
    InterpretationResult,
    JudgeInterpretation,
    LLMStatus,
    SanitizedLLMContext,
)

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"
# 에이전트 전용 설정 접두사 — `CRITIC_LLM_MODEL` 로 판정 모델을 생성 모델과 분리한다 (§6.4).
_ENV_PREFIX = "CRITIC_"
_NUMERIC_PATTERN = re.compile(r"\d")
_SENTENCE_SPLIT = re.compile(r"[.!?。]+")
_MAX_SUMMARY_CHARACTERS = 240
_MAX_NOTE_CHARACTERS = 400

#: Provider 별 기본 모델. stable 을 pin 한다 — `latest`·`preview` 같은 자동 갱신
#: 별칭은 출력 성향이 예고 없이 바뀌고, 그러면 **판정 성적이 근거가 못 된다.**
#:
#: 🔴 **§6.4 — 판정 모델은 생성 모델과 달라야 한다.** 같은 모델·같은 논리면 자기가
#:   만든 설명을 자기가 승인한다. 그래서 ollama 에서 `qwen2.5:7b`(생성 측 gemma3 과
#:   다른 계열)를 골랐고, Gemini 에서는 `flash-lite`(마스터·물류가 쓰는 것)가 아니라
#:   **한 단계 위인 `flash`** 를 기본으로 둔다.
#:
#:   ⚠️ 지금 설명문을 만드는 쪽은 매입이고 매입은 아직 ollama 다. **매입이 Gemini 로
#:   오는 날 이 값이 매입 것과 같아지지 않는지 다시 봐야 한다.**
_DEFAULT_MODELS = {
    "ollama": "gemma3:4b",
    "gemini": "gemini-3.5-flash",
}

#: Gemini 는 자체 엔드포인트를 쓴다. `LLM_BASE_URL` 기본값은 Ollama 라 거기서 읽으면
#: provider 만 바꾼 사람이 로컬 11434 로 쏘고 연결 실패로만 본다.
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Gemini `responseSchema`. JSON Schema 를 그대로 못 먹어서 **직접 적는다** —
#: 판정 출력이 칸 셋뿐이라 변환기를 두는 것보다 이쪽이 읽기 쉽다 (물류와 같은 방식).
#: `JudgeInterpretation` 이 바뀌면 여기도 바꿔야 한다 — 검사가 둘을 대조한다.
_GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "note": {"type": "string"},
    },
    "required": ["summary", "verdict", "note"],
}

L5_SYSTEM_PROMPT = """당신은 검증자다.
아래 결정의 설명문(rationale)이 데이터와 모순되는지만 본다.

검사 대상:
- 설명문이 인용한 근거가 facts와 binding_constraints에 실제로 있는가
- 설명문의 인과가 binding_constraints와 반대 방향은 아닌가
- signals에서 언급된 위험을 설명문이 누락했는가

검사 대상이 아닌 것:
- 수량이 적절한가 — 이미 결정론 Core가 검증했다
- 더 나은 대안이 있는가 — 당신의 역할이 아니다

규칙:
- 수량을 바꾸라고 제안하지 않는다. 문장의 문제만 지적한다.
- 숫자, 금액, 날짜, 비율을 출력하지 않는다.
- verdict는 PASS 또는 FAIL이다.
- FAIL이면 어느 문장의 어느 부분이 무엇과 모순되는지 note에 적는다.
- PASS이면 note는 짧게 근거만 적는다.
- summary는 최대 두 문장으로 작성한다.
- 모든 문장은 한국어로 작성한다.
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
            "format": JudgeInterpretation.model_json_schema(),
            "messages": [
                {"role": "system", "content": L5_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            # ★ temperature 0 고정 (§6.4). 판정은 흔들리면 안 된다.
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
            raise RuntimeError("Critic Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Critic Local LLM response did not contain message content")
        return content


class GeminiProvider:
    """Gemini REST 호출. 표준 라이브러리만 쓴다 — Ollama 경로와 같은 규율이다.

    ★ **API 키는 호출 시점에 환경에서 읽는다.** `LLMSettings` 에 담지 않는다 —
      설정 객체는 로그·예외에 통째로 실릴 수 있고 키가 거기 끼면 지울 수 없다.
    ★ 자체 재시도는 없다 — 재시도는 `JudgeService` 가 소유한다.
    ★ temperature 0 고정 (§6.4). **판정은 흔들리면 안 된다.**
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
            raise RuntimeError("GEMINI_API_KEY is not set")
        user_payload: dict[str, object] = {"context": context.model_dump(mode="json")}
        if retry_guidance:
            user_payload["correction"] = retry_guidance
        payload = {
            "system_instruction": {"parts": [{"text": L5_SYSTEM_PROMPT}]},
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
        base_url = (
            os.getenv(f"{_ENV_PREFIX}GEMINI_BASE_URL")
            or os.getenv("GEMINI_BASE_URL")
            or _GEMINI_BASE_URL
        ).rstrip("/")
        request = urllib.request.Request(
            f"{base_url}/models/{self.settings.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            # 🔴 **감싸지 않는다.** `URLError` 의 하위라 아래 except 가 같이 먹는데,
            #    감싸면 **상태 코드가 사라진다.** 마스터에서 429(quota)가
            #    RuntimeError 로 덮여 한도 초과와 서버 다운이 같아 보였다.
            raise
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError("Critic Gemini request failed") from error
        # 🔴 **`parts[0]` 이 아니다 — 사고 조각이 앞에 오는 모델이 있다.**
        #    마스터에서 이것 때문에 호출이 성공했는데 FALLBACK 으로 떨어졌다
        #    (`SELECT_SCENARIO` 12번 중 11번). 판정에서 같은 일이 나면
        #    **검증이 조용히 안 돈다** — 그게 이 프로젝트에서 가장 나쁜 실패다.
        candidates = document.get("candidates") or []
        parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
        for part in parts:
            if part.get("thought"):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        raise TypeError("Critic Gemini response did not contain text content")


class UnavailableProvider:
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        del context, retry_guidance
        raise RuntimeError("Configured Critic LLM provider is not supported")


class ValidationIssue(StrEnum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    NUMERIC_OUTPUT_FORBIDDEN = "NUMERIC_OUTPUT_FORBIDDEN"
    NOTE_REQUIRED_ON_FAIL = "NOTE_REQUIRED_ON_FAIL"
    NOTE_TOO_LONG = "NOTE_TOO_LONG"
    SUMMARY_TOO_LONG = "SUMMARY_TOO_LONG"
    TOO_MANY_SENTENCES = "TOO_MANY_SENTENCES"
    REPETITIVE_OUTPUT = "REPETITIVE_OUTPUT"


class JudgeValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        super().__init__(", ".join(issues))
        self.issues = issues


class JudgeService:
    def __init__(self, settings: LLMSettings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def judge(
        self,
        context: SanitizedLLMContext,
        *,
        runtime_ready: bool,
        end_stage_reached: bool,
    ) -> InterpretationResult:
        """상태 결정 순서는 Finance / Logistics 런타임과 동일하다.

        DISABLED → SKIPPED_TEMPLATE → SUCCESS → FALLBACK.
        어느 경로로 끝나든 L0~L4 결정론 검증 결과는 그대로 살아 있다.
        """
        template = build_template_judgement(context)
        if not self.settings.enabled:
            return self._result(template, status="DISABLED", attempts=0, fallback=False)
        if not needs_llm(
            context,
            runtime_ready=runtime_ready,
            end_stage_reached=end_stage_reached,
        ):
            return self._result(template, status="SKIPPED_TEMPLATE", attempts=0, fallback=False)

        guidance = None
        attempts = 0
        for _ in range(self.settings.max_retries + 1):
            attempts += 1
            try:
                raw_output = self.provider.generate(context, retry_guidance=guidance)
                interpretation = validate_judgement(raw_output)
                return self._result(
                    interpretation,
                    status="SUCCESS",
                    attempts=attempts,
                    fallback=False,
                )
            except JudgeValidationError as error:
                guidance = retry_guidance(error.issues)
            except Exception:  # noqa: BLE001 - optional LLM cannot fail Critic Core.
                guidance = ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]
        return self._result(template, status="FALLBACK", attempts=attempts, fallback=True)

    def _result(
        self,
        interpretation: JudgeInterpretation,
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


#: 미지원 값은 조용히 무시하지 않고 `UnavailableProvider` 로 보내 **터뜨린다** —
#: 오타 하나로 판정이 조용히 안 도는 것이 가장 나쁘다.
_PROVIDERS: dict[str, type] = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
}


def get_llm_settings() -> LLMSettings:
    """에이전트 전용 설정 → 공통 설정 → 기본값 순으로 읽는다.

    ★ `CRITIC_LLM_MODEL` 을 selector 와 **다른 모델**로 두는 것이 §6.4 의 요구다 —
      같은 모델·같은 논리면 자기가 만든 설명을 자기가 승인한다. 프롬프트 분리만으로는 부족하다.
    """
    load_dotenv(_ENV_FILE)
    scoped_provider = os.getenv(f"{_ENV_PREFIX}LLM_PROVIDER")
    global_provider = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
    provider = (scoped_provider or global_provider).strip().lower()
    # 🔴 **모델은 프로바이더에 종속된 값이다.** Critic 이 전역과 다른 프로바이더를
    #    쓸 때 전역 `LLM_MODEL`(재무·오케가 같이 보는 `gemma3:4b`)을 상속하면
    #    Gemini 에 없는 모델을 요청해 404 가 난다. 그 경우에만 전역 모델을 건너뛴다.
    #    프로바이더가 같으면 전역 모델은 정당한 상속이라 사슬을 그대로 따른다.
    #    물류(#95)·마스터가 같은 규칙을 쓴다 — 세 파트가 다르게 풀면 `.env` 를 읽는
    #    사람이 규칙을 세 번 배워야 한다.
    #
    #    ⚠️ `CRITIC_LLM_MODEL` 이 **직접 지정돼 있으면 그것이 이긴다.** provider 를
    #    바꾸면서 그 줄을 안 고치면 (예: `qwen2.5:7b` 를 둔 채 gemini 로) 404 가
    #    난다 — 지정을 무시하는 것이 더 나쁘므로 여기서 덮지 않는다.
    if provider != global_provider and not os.getenv(f"{_ENV_PREFIX}LLM_MODEL"):
        model = _DEFAULT_MODELS.get(provider, "")
    else:
        model = _env("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=provider,
        model=model.strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=max(0.1, float(_env("LLM_TIMEOUT_SECONDS", "30"))),
        max_retries=min(1, max(0, int(_env("LLM_MAX_RETRIES", "1")))),
    )


def get_judge_service() -> JudgeService:
    settings = get_llm_settings()
    factory = _PROVIDERS.get(settings.provider)
    provider: LLMProvider = factory(settings) if factory else UnavailableProvider()
    return JudgeService(settings, provider)


def needs_llm(
    context: SanitizedLLMContext,
    *,
    runtime_ready: bool,
    end_stage_reached: bool,
) -> bool:
    """검증할 설명문이 실제로 있을 때만 부른다.

    앞 레이어가 FAIL 로 끊겼으면(`end_stage_reached`) L5 는 돌지 않는다 — 설계서 §8 의
    "앞 계층 FAIL 이면 뒤 레이어 생략" 규칙이다.
    """
    if not runtime_ready or end_stage_reached:
        return False
    return bool(context.rationale.strip())


def validate_judgement(raw_output: str) -> JudgeInterpretation:
    try:
        interpretation = JudgeInterpretation.model_validate_json(raw_output)
    except ValidationError as error:
        raise JudgeValidationError([ValidationIssue.INVALID_SCHEMA]) from error
    issues = _validation_issues(interpretation)
    if issues:
        raise JudgeValidationError(issues)
    return interpretation


def _validation_issues(interpretation: JudgeInterpretation) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if _NUMERIC_PATTERN.search(f"{interpretation.summary} {interpretation.note}"):
        issues.append(ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN)
    if interpretation.verdict == "FAIL" and not interpretation.note.strip():
        issues.append(ValidationIssue.NOTE_REQUIRED_ON_FAIL)
    if len(interpretation.note) > _MAX_NOTE_CHARACTERS:
        issues.append(ValidationIssue.NOTE_TOO_LONG)
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
    return issues


def retry_guidance(issues: list[ValidationIssue]) -> list[str]:
    guidance = []
    if ValidationIssue.INVALID_SCHEMA in issues:
        guidance.append("지정된 세 필드만 포함한 유효한 JSON을 작성하세요.")
    if ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in issues:
        guidance.append("숫자와 날짜를 사용하지 마세요.")
    if ValidationIssue.NOTE_REQUIRED_ON_FAIL in issues:
        guidance.append("FAIL이면 어느 부분이 무엇과 모순되는지 note에 적으세요.")
    if ValidationIssue.NOTE_TOO_LONG in issues:
        guidance.append("note를 짧게 작성하세요.")
    if ValidationIssue.SUMMARY_TOO_LONG in issues or ValidationIssue.TOO_MANY_SENTENCES in issues:
        guidance.append("summary를 짧은 두 문장 이내로 작성하세요.")
    if ValidationIssue.REPETITIVE_OUTPUT in issues:
        guidance.append("같은 내용을 반복하지 마세요.")
    return guidance


def build_template_judgement(context: SanitizedLLMContext) -> JudgeInterpretation:
    """LLM 을 못 쓸 때의 기본값.

    ★ 반드시 PASS 다. 판정하지 못한 것을 FAIL 로 적으면 검증하지 않은 것을 검증했다고
      말하는 셈이 된다. 대신 '수행되지 않았다'를 note 에 남기고, 호출부가 이를
      `skipped` 로 올려 coverage 에 드러낸다 (설계서 §8).
    """
    summary = (
        " ".join(context.facts[:2])
        if context.facts
        else "결정론적 검증 결과 외에 추가로 확인된 논리 문제가 없습니다."
    )
    return JudgeInterpretation(
        summary=summary,
        verdict="PASS",
        note="L5 논리 일관성 검증이 수행되지 않았습니다.",
    )


def _env(key: str, default: str) -> str:
    """`CRITIC_<KEY>` → `<KEY>` → default. 빈 문자열은 미설정으로 본다."""
    return os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key) or default


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
