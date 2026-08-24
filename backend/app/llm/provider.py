"""Provider boundary and Ollama structured-output adapter."""

import json
import urllib.error
import urllib.request
from typing import Protocol

from app.llm.config import LLMSettings
from app.llm.schemas import AgentInterpretation, SanitizedLLMContext

SYSTEM_PROMPT = """당신은 Finance/Logistics Agent의 해석 레이어다.
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


class LLMProvider(Protocol):
    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        """Return one raw structured-output candidate."""


class OllamaProvider:
    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        user_payload: dict[str, object] = {
            "context": context.model_dump(mode="json"),
        }
        if retry_guidance:
            user_payload["correction"] = retry_guidance
        payload = {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "format": AgentInterpretation.model_json_schema(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
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
            with urllib.request.urlopen(
                request,
                timeout=self.settings.timeout_seconds,
            ) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError("Local LLM request failed") from error
        content = (document.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise TypeError("Local LLM response did not contain message content")
        return content


class UnavailableProvider:
    """Configured provider placeholder that always triggers deterministic fallback."""

    def generate(
        self,
        context: SanitizedLLMContext,
        *,
        retry_guidance: list[str] | None = None,
    ) -> str:
        del context, retry_guidance
        raise RuntimeError("Configured LLM provider is not supported")


def create_provider(settings: LLMSettings) -> LLMProvider:
    if settings.provider == "ollama":
        return OllamaProvider(settings)
    return UnavailableProvider()
