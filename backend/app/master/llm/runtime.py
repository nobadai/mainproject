"""의도 분류 — 프로바이더 · 검증 · 재시도 · fallback.

팀 규약(finance·logistics·orchestrator·critic·purchase 5벌)과 같은 배치다.
프로바이더는 3종이고 `LLM_PROVIDER` 로 고른다. 에이전트 접두사는 `MASTER_`.

★ **검증 체인은 프로바이더 밖에 있다.** 프로바이더는 "문자열을 받아온다"까지만 하고,
  닫힌 열거 대조·숫자 출처 검사·재시도는 `IntentService` 가 소유한다.

★ **API 키는 `.env` 에서만 읽는다.** `LLMSettings` 에 싣지 않는다 — 설정 객체는 로그·
  예외에 실릴 수 있다. 키가 없으면 예외를 던지고 **fallback 으로 간다.**

⚠️ **이것이 팀의 6번째 LLM 런타임 복제다.**
  기존 5벌과 규약(env 이름 · status 4값 · Provider 프로토콜 · 검증 체인 분리)을 그대로
  따랐다. 신규 공용 층을 만들어 전 파트를 갈아엎는 것보다 이번 범위에 맞다고 판단했지만,
  **공용 `app/llm/` 추출은 팀 안건으로 열려 있다**(소유 파트 미정). 이 파일은 프로바이더가
  도메인 타입을 모르는 형태(`generate(system, user, schema) -> str`)라 추출 시 그대로
  들어낼 수 있게 해 뒀다 — 매입 런타임은 도메인 컨텍스트에 묶여 있어 그렇지 않다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import ValidationError

from app.master.envelope import agent_allowed_modes
from app.master.llm.schemas import Intent, IntentResult, LLMStatus

_ENV_FILES = (
    Path(__file__).resolve().parents[3] / ".env",
    Path(__file__).resolve().parents[4] / ".env",
)
_ENV_PREFIX = "MASTER_"

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "ollama": "gemma3:4b",
}

#: 발화문에 없던 숫자를 조건에 지어넣는 것을 막는다. 매입 ⑤의 "숫자 금지"와 다르다 —
#: 여기서는 **사용자가 말한 숫자는 허용**하고, 출처 없는 숫자만 거부한다.
_DIGITS = re.compile(r"\d")

SYSTEM_PROMPT = """당신은 햇들농산 매입 의사결정 시스템의 요청 해석 레이어다.
사용자의 한국어 발화문을 정해진 종류 중 하나로 분류하는 것이 전부다.

절대 규칙:
- 실행하지 않는다. 분류만 한다.
- 지정된 JSON Schema 에 맞는 JSON 만 출력한다. 설명 문장을 덧붙이지 않는다.
- 목록에 없는 값을 만들지 않는다.
- 확실하지 않으면 action 을 UNKNOWN 으로, confidence 를 LOW 로 둔다.
  **모르겠다고 답하는 것이 틀리게 분류하는 것보다 낫다.**

action 종류:
- PROCUREMENT_RUN       오늘 매입안을 만들어 달라 (예: "오늘 배추 얼마나 사야 해?")
- STATUS_QUERY          특정 부서 상태만 조회 (예: "지금 자금 상황 알려줘")
- RERUN_WITH_CONDITION  조건을 붙여 다시 (예: "예산 2천만원으로 낮춰서 다시")
- SELECT_SCENARIO       제시된 안을 고름 (예: "기본안으로 진행해")
- UNKNOWN               위 어디에도 확실히 속하지 않음

필드 규칙:
- agents 는 STATUS_QUERY 일 때만 채운다. finance(재무·자금), inventory(재고·물류),
  purchase(매입) 중에서 고른다. 그 외 action 에서는 빈 배열이다.
- item 은 배추·무·양파·피마늘 중 발화문이 가리키는 것. 알 수 없으면 비운다.
  **추측해서 채우지 않는다.**
- scenario_label 은 SELECT_SCENARIO 일 때만. 사용자가 부른 이름을 그대로 옮긴다.
- condition 은 RERUN_WITH_CONDITION 일 때만. **사용자의 말 그대로** 옮긴다.
  발화문에 없는 숫자를 만들지 않는다.
- confidence 는 분류가 얼마나 확실한지다. 발화문이 모호하면 낮춘다."""


@dataclass(frozen=True)
class LLMSettings:
    """설정. **API 키를 담지 않는다.**"""

    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int
    max_output_tokens: int
    effort: str | None


class TextProvider(Protocol):
    """문자열을 받아오는 것까지가 프로바이더의 일이다.

    ★ **도메인 타입을 모른다.** 매입 런타임의 프로바이더는 `SanitizedLLMContext` 를
      받는데, 그러면 공용 층으로 들어낼 수 없다. 여기는 문자열 셋만 받는다.
    """

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str: ...


def _env(key: str, default: str) -> str:
    return os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key) or default


def _read_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(f"{_ENV_PREFIX}{key}") or os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: str, *, minimum: int) -> int:
    """파싱 실패는 기본값으로 되돌린다 — `.env` 오타 하나로 앱이 죽으면 안 된다."""
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
    provider = _env("LLM_PROVIDER", "anthropic").strip().lower()
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=provider,
        model=_env("LLM_MODEL", _DEFAULT_MODELS.get(provider, "")).strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", "30", minimum=0.1),
        max_retries=min(1, _int_env("LLM_MAX_RETRIES", "1", minimum=0)),
        max_output_tokens=_int_env("LLM_MAX_OUTPUT_TOKENS", "1024", minimum=256),
        effort=(_env("LLM_EFFORT", "").strip() or None),
    )


def _intent_schema() -> dict[str, Any]:
    return Intent.model_json_schema()


def _require_model(settings: LLMSettings) -> None:
    if not settings.model:
        raise RuntimeError(f"LLM_MODEL is not set for provider {settings.provider!r}")


class AnthropicProvider:
    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str:
        import anthropic  # 지연 임포트 — 키 없는 환경에서 import 비용을 안 낸다

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _require_model(self.settings)
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=self.settings.timeout_seconds,
            max_retries=0,  # 재시도는 서비스가 소유한다 — 두 층이 세면 상한이 곱해진다
        )
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.settings.effort:
            output_config["effort"] = self.settings.effort
        message = client.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_output_tokens,
            system=system,
            output_config=output_config,
            messages=[{"role": "user", "content": user}],
        )
        # content[0] 이 아니다 — 사고 블록이 앞에 오는 모델이 있다.
        for block in message.content:
            if getattr(block, "type", None) == "text":
                return block.text
        raise TypeError("Anthropic response contained no text block")


class OpenAIProvider:
    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str:
        import openai

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _require_model(self.settings)
        client = openai.OpenAI(
            api_key=api_key,
            timeout=self.settings.timeout_seconds,
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_completion_tokens=self.settings.max_output_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "intent", "strict": True, "schema": schema},
            },
        )
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise TypeError("OpenAI response did not contain message content")
        return content


class OllamaProvider:
    """표준 라이브러리만 쓴다 (SDK 없음) — 팀 기존 경로와 같다."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str:
        import urllib.error
        import urllib.request

        payload = {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "format": schema,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": 0,
                "num_ctx": 4096,
                "num_predict": self.settings.max_output_tokens,
            },
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
            raise RuntimeError("Master Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Master Local LLM response did not contain message content")
        return content


class UnavailableProvider:
    """미지원 `LLM_PROVIDER` 값. 조용히 무시하지 않고 **터뜨려 fallback 으로 보낸다**."""

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str:
        del system, user, schema
        raise RuntimeError("Configured master LLM provider is not supported")


_PROVIDERS: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}


# ── 검증 ────────────────────────────────────────────────────────────────


class IntentIssue(StrEnum):
    NOT_JSON = "NOT_JSON"
    SCHEMA = "SCHEMA"
    AGENTS_ON_NON_QUERY = "AGENTS_ON_NON_QUERY"
    AGENTS_MISSING = "AGENTS_MISSING"
    AGENT_CANNOT_QUERY = "AGENT_CANNOT_QUERY"
    LABEL_ON_NON_SELECT = "LABEL_ON_NON_SELECT"
    LABEL_MISSING = "LABEL_MISSING"
    CONDITION_ON_NON_RERUN = "CONDITION_ON_NON_RERUN"
    CONDITION_MISSING = "CONDITION_MISSING"
    CONDITION_INVENTED_NUMBER = "CONDITION_INVENTED_NUMBER"
    UNKNOWN_NOT_EMPTY = "UNKNOWN_NOT_EMPTY"


class IntentValidationError(ValueError):
    def __init__(self, issues: list[IntentIssue]) -> None:
        super().__init__(", ".join(issues))
        self.issues = issues


_GUIDANCE: dict[IntentIssue, str] = {
    IntentIssue.NOT_JSON: "JSON 만 출력한다. 설명 문장을 붙이지 않는다.",
    IntentIssue.SCHEMA: "지정된 JSON Schema 의 필드와 허용값만 쓴다.",
    IntentIssue.AGENTS_ON_NON_QUERY: "agents 는 STATUS_QUERY 일 때만 채운다.",
    IntentIssue.AGENTS_MISSING: "STATUS_QUERY 면 agents 에 최소 하나를 넣는다.",
    IntentIssue.AGENT_CANNOT_QUERY: "그 에이전트는 상태 조회를 받지 않는다.",
    IntentIssue.LABEL_ON_NON_SELECT: "scenario_label 은 SELECT_SCENARIO 일 때만 쓴다.",
    IntentIssue.LABEL_MISSING: "SELECT_SCENARIO 면 사용자가 부른 안 이름을 넣는다.",
    IntentIssue.CONDITION_ON_NON_RERUN: "condition 은 RERUN_WITH_CONDITION 일 때만 쓴다.",
    IntentIssue.CONDITION_MISSING: "RERUN_WITH_CONDITION 이면 조건을 그대로 옮긴다.",
    IntentIssue.CONDITION_INVENTED_NUMBER: (
        "condition 에 발화문에 없는 숫자를 넣지 않는다. 사용자의 말 그대로 옮긴다."
    ),
    IntentIssue.UNKNOWN_NOT_EMPTY: "UNKNOWN 이면 나머지 필드를 전부 비운다.",
}


def retry_guidance(issues: list[IntentIssue]) -> list[str]:
    return [_GUIDANCE[issue] for issue in issues]


def validate_intent(raw_output: str, utterance: str) -> Intent:
    """LLM 출력을 검사한다. **닫힌 열거가 대부분을 막고, 나머지를 여기서 막는다.**"""
    try:
        intent = Intent.model_validate_json(raw_output)
    except ValidationError as error:
        issue = IntentIssue.NOT_JSON if "json_invalid" in str(error) else IntentIssue.SCHEMA
        raise IntentValidationError([issue]) from error

    issues = _issues(intent, utterance)
    if issues:
        raise IntentValidationError(issues)
    return intent


def _issues(intent: Intent, utterance: str) -> list[IntentIssue]:
    out: list[IntentIssue] = []
    action = intent.action

    if intent.agents and action != "STATUS_QUERY":
        out.append(IntentIssue.AGENTS_ON_NON_QUERY)
    if action == "STATUS_QUERY":
        if not intent.agents:
            out.append(IntentIssue.AGENTS_MISSING)
        for agent in intent.agents:
            if "STATUS_QUERY" not in agent_allowed_modes(agent):
                out.append(IntentIssue.AGENT_CANNOT_QUERY)
                break

    label = (intent.scenario_label or "").strip()
    if label and action != "SELECT_SCENARIO":
        out.append(IntentIssue.LABEL_ON_NON_SELECT)
    if action == "SELECT_SCENARIO" and not label:
        out.append(IntentIssue.LABEL_MISSING)

    condition = (intent.condition or "").strip()
    if condition and action != "RERUN_WITH_CONDITION":
        out.append(IntentIssue.CONDITION_ON_NON_RERUN)
    if action == "RERUN_WITH_CONDITION":
        if not condition:
            out.append(IntentIssue.CONDITION_MISSING)
        elif _invents_digits(condition, utterance):
            out.append(IntentIssue.CONDITION_INVENTED_NUMBER)

    if action == "UNKNOWN" and (intent.agents or intent.item or label or condition):
        out.append(IntentIssue.UNKNOWN_NOT_EMPTY)
    return out


def _invents_digits(condition: str, utterance: str) -> bool:
    """조건의 숫자가 발화문에 없는 숫자인가.

    ★ 매입 ⑤의 "숫자 금지"와 다르다. 여기서는 **사용자가 말한 숫자는 그대로 옮겨야**
      하고, 출처 없는 숫자만 거부한다. 자릿수 단위로 비교하면 "2000"과 "2천"을 구분
      못 하므로 **등장한 숫자 문자의 집합**으로 본다 — 느슨하지만 지어낸 금액은 잡는다.
    """
    return not set(_DIGITS.findall(condition)) <= set(_DIGITS.findall(utterance))


# ── 서비스 ──────────────────────────────────────────────────────────────

#: 확인 없이 바로 실행해도 되는 종류. `PROCUREMENT_RUN` 은 예산 12회와 매입 LLM 을
#: 태우므로 빠져 있다 — 오분류 비용이 비대칭이다.
_NO_CONFIRM_ACTIONS = frozenset({"STATUS_QUERY"})

_UNKNOWN = Intent(action="UNKNOWN", confidence="LOW")


class IntentService:
    """검증·재시도·fallback 을 소유한다. **프로바이더가 바뀌어도 이 층은 그대로다.**"""

    def __init__(self, settings: LLMSettings, provider: TextProvider) -> None:
        self.settings = settings
        self.provider = provider

    def classify(self, utterance: str) -> IntentResult:
        """발화문 하나를 분류한다. **실패하면 UNKNOWN 으로 되묻는다.**

        ★ 실패를 "가장 그럴듯한 것"으로 메우지 않는다. 잘못 분류한 실행은 예산을 태우고,
          사용자는 자기가 안 시킨 일이 도는 것을 본다.
        """
        text = utterance.strip()
        if not self.settings.enabled:
            return self._result(_UNKNOWN, status="DISABLED", attempts=0, fallback=False)
        if not text:
            return self._result(_UNKNOWN, status="SKIPPED_TEMPLATE", attempts=0, fallback=False)

        guidance: list[str] | None = None
        attempts = 0
        for _ in range(self.settings.max_retries + 1):
            attempts += 1
            try:
                raw = self.provider.generate(
                    SYSTEM_PROMPT, _user_payload(text, guidance), _intent_schema()
                )
                return self._result(
                    validate_intent(raw, text),
                    status="SUCCESS",
                    attempts=attempts,
                    fallback=False,
                )
            except IntentValidationError as error:
                guidance = retry_guidance(error.issues)
            except Exception:  # noqa: BLE001 — 분류 실패가 API 를 죽이면 안 된다
                break
        return self._result(_UNKNOWN, status="FALLBACK", attempts=attempts, fallback=True)

    def _result(
        self, intent: Intent, *, status: LLMStatus, attempts: int, fallback: bool
    ) -> IntentResult:
        confirm = _needs_confirmation(intent)
        return IntentResult(
            intent=intent,
            llm_status=status,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model or None,
            llm_attempts=attempts,
            llm_fallback_used=fallback,
            needs_confirmation=confirm,
            clarification=_clarification(intent) if confirm else None,
        )


def _needs_confirmation(intent: Intent) -> bool:
    if intent.action == "UNKNOWN":
        return True
    if intent.confidence != "HIGH":
        return True
    return intent.action not in _NO_CONFIRM_ACTIONS


def _clarification(intent: Intent) -> str:
    """되물을 말. **규칙이 만든다** — LLM 이 쓰면 사용자 응답 생성(⑥)이 되고, 그건 아직 없다."""
    if intent.action == "UNKNOWN":
        return (
            "무엇을 해 드릴지 알아듣지 못했습니다. "
            "매입안 생성 · 부서 상태 조회 · 조건 변경 재요청 · 안 선택 중 하나로 말씀해 주세요."
        )
    if intent.action == "PROCUREMENT_RUN":
        item = intent.item or "품목"
        return f"{item} 매입안을 새로 만들까요? (부서 호출이 일어납니다)"
    return "이렇게 이해했습니다. 진행할까요?"


def _user_payload(utterance: str, guidance: list[str] | None) -> str:
    payload: dict[str, Any] = {"utterance": utterance}
    if guidance:
        payload["correction"] = guidance
    return json.dumps(payload, ensure_ascii=False)


def get_intent_service() -> IntentService:
    settings = get_llm_settings()
    factory = _PROVIDERS.get(settings.provider)
    provider: TextProvider = factory(settings) if factory else UnavailableProvider()
    return IntentService(settings, provider)
