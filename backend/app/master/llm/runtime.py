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
    #: 🔴 stable 을 pin 한다 — `latest`·`preview` 같은 자동 갱신 별칭은 출력 성향이
    #: 예고 없이 바뀐다. 물류가 #95 에서 고른 것과 같은 모델이다 (팀 안에서 두 파트가
    #: 다른 모델을 쓰면 "모델이 달라서 그런가" 가 모든 조사에 끼어든다).
    "gemini": "gemini-3.5-flash-lite",
}

#: Gemini 는 자체 엔드포인트를 쓴다. `LLM_BASE_URL` 은 기본값이 Ollama 라
#: **거기서 읽으면 안 된다** — provider 를 바꿨는데 주소가 안 바뀌면
#: 로컬 11434 로 쏘고 연결 실패로만 보인다.
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: 발화문에 없던 숫자를 조건에 지어넣는 것을 막는다. 매입 ⑤의 "숫자 금지"와 다르다 —
#: 여기서는 **사용자가 말한 숫자는 허용**하고, 출처 없는 숫자만 거부한다.
_DIGITS = re.compile(r"\d")

SYSTEM_PROMPT = """당신은 햇들농산 매입 의사결정 시스템의 요청 해석 레이어다.
사용자의 한국어 발화문을 정해진 종류 중 하나로 분류하는 것이 전부다.

절대 규칙:
- 실행하지 않는다. 분류만 한다.
- 지정된 JSON Schema 에 맞는 JSON 만 출력한다. 설명 문장을 덧붙이지 않는다.
- 목록에 없는 값을 만들지 않는다.
- **어느 종류인지** 확실하지 않으면 UNKNOWN · LOW 로 둔다.
  모르겠다고 답하는 것이 틀리게 분류하는 것보다 낫다.

action 종류와 예시:

PROCUREMENT_RUN — 살 안을 **만들어 달라**
  "오늘 배추 얼마나 사야 해?"   "무 매입안 뽑아줘"   "오늘 뭘 사면 좋을까"
  "배추 매입 계획 만들어줘"      "얼마나 들여와야 하지?"

STATUS_QUERY — 부서의 **지금 상태만** 묻는다 (안을 만들지 않는다)
  "지금 자금 상황 알려줘"   "창고에 얼마나 남았어?"   "재고 어때?"
  "돈 얼마나 있어?"        "지금 창고 여유 있나?"

RERUN_WITH_CONDITION — **조건을 붙여 다시** 만들어 달라
  "예산 2천만원으로 낮춰서 다시"   "좀 적게 사는 걸로 다시 해줘"

SELECT_SCENARIO — **이미 나와 있는 안 중 하나를 고른다**
  "기본안으로 진행해"   "보수안 선택할게"   "두 번째 걸로 해줘"

UNKNOWN — 위 어디에도 속하지 않거나 무엇을 원하는지 알 수 없다
  "그거 있잖아 그거"   "음..."
  ★ **이 시스템에 답할 자리가 없는 것도 UNKNOWN 이다.** 가까운 부서로 돌리지 마라.
    "배추 가격 얼마야"  "시세 알려줘"  "단가 어떻게 돼"   → UNKNOWN
    품목 가격·시세를 답하는 부서는 없다. 재무는 **회사 자금**이지 품목 가격이 아니다.
    가까운 부서를 넣으면 사용자는 **물어본 것과 상관없는 숫자**를 받는다.

★ **만들어 달라**와 **고른다**를 구분하라. "안" 이라는 글자로 가르지 마라.
  "매입안 뽑아줘 · 만들어줘 · 얼마나 사야 해"  → 만들어 달라  → PROCUREMENT_RUN
  "기본안으로 · 보수안으로 · 두 번째 걸로"      → 고른다        → SELECT_SCENARIO

★ SELECT_SCENARIO 로 고르면 scenario_label 을 **반드시 채운다** — 사용자가 부른 이름 그대로.
  "기본안으로 진행해"  → scenario_label: "기본"
  "보수안 선택할게"    → scenario_label: "보수"
  "공격안으로 가자"    → scenario_label: "공격"

부서 이름 (agents) — STATUS_QUERY 일 때만 채운다:

  finance     자금 · 현금 · 잔고 · 돈 · 예산 · 지급 · 결제 · 대금 · 자금 사정
  inventory   재고 · 창고 · 보관 · 입고 · 출고 · 용량 · 여유 · 신선도 · 남은 양
  purchase    매입 진행 상황 · 지금 만들어 둔 안

★ 부서가 여럿이면 여럿을 넣는다 ("자금이랑 창고 둘 다" → finance, inventory).
★ **어느 부서인지** 애매한 것은 UNKNOWN 이 아니다 — 가까운 부서를 넣고 confidence 를
  낮춘다. UNKNOWN 은 **어느 종류인지** 모를 때만 쓴다.
★ **어느 품목인지** 모르는 것도 UNKNOWN 이 아니다. 품목은 비우고 종류는 그대로 둔다.
  "오늘 뭘 사면 좋을까"  → PROCUREMENT_RUN · item: null   (UNKNOWN 이 아니다)
  무엇을 해 달라는지가 분명하면 세부가 비어도 그 종류로 분류한다.

나머지 필드:
- item 은 배추·무·양파 중 **발화문에 나온 것을 그대로** 옮긴다.
  "오늘 배추 얼마나 사야 해?" → item: "배추"
  "무 매입안 뽑아줘"          → item: "무"
  "오늘 뭘 사면 좋을까"        → item: null   (품목이 안 나왔다)
  발화문에 없으면 null 이다. **추측해서 채우지 않는다.**
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
    scoped_provider = os.getenv(f"{_ENV_PREFIX}LLM_PROVIDER")
    global_provider = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
    provider = (scoped_provider or global_provider).strip().lower()
    # 🔴 **모델은 프로바이더에 종속된 값이다.** 마스터가 전역과 다른 프로바이더를
    #    쓸 때 전역 `LLM_MODEL`(재무·Critic·오케가 같이 보는 `gemma3:4b`)을 상속하면
    #    **Gemini 에 없는 모델을 요청해 404 가 난다.** 그 경우에만 전역 모델을
    #    건너뛴다 — 물류가 #95 에서 같은 사고를 겪고 세운 규칙이고, 두 파트가 다르게
    #    풀면 `.env` 를 읽는 사람이 규칙을 두 번 배워야 한다.
    #
    #    프로바이더가 같으면(둘 다 ollama) 전역 모델은 **정당한 상속**이므로
    #    사슬(`MASTER_LLM_MODEL` → `LLM_MODEL` → 기본값)을 그대로 따른다.
    if provider != global_provider and not os.getenv(f"{_ENV_PREFIX}LLM_MODEL"):
        model = _DEFAULT_MODELS.get(provider, "")
    else:
        model = _env("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))
    return LLMSettings(
        enabled=_read_bool("LLM_ENABLED", default=True),
        provider=provider,
        model=model.strip(),
        base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", "30", minimum=0.1),
        max_retries=min(1, _int_env("LLM_MAX_RETRIES", "1", minimum=0)),
        max_output_tokens=_int_env("LLM_MAX_OUTPUT_TOKENS", "1024", minimum=256),
        effort=(_env("LLM_EFFORT", "").strip() or None),
    )


def _intent_schema() -> dict[str, Any]:
    """구조화 출력에 넘길 JSON Schema.

    🔴 **기본값이 있는 칸을 `required` 로 올린다 — 안 그러면 모델이 그 칸을 안 쓴다.**

    파이썬 쪽 기본값(`agents=[]` · `item=None`)이 스키마의 `required` 에서 그 칸을 빼고,
    빠진 칸은 모델에게 **없는 칸처럼 보인다.**

    ```text
    "재고 어때?"              {"action":"STATUS_QUERY","confidence":"HIGH"}   agents 없음
    "오늘 배추 얼마나 사야 해?"  item 이 3/3 으로 null          발화문에 배추가 있는데도
    ```

    `agents` 는 8/28 에 올렸는데 `item` 을 빠뜨렸다. 채점표가 `action` 과 `agents` 만
    보고 있어 **드러나지 않았다** — 8/29 에 관통을 돌려 보고서야 나왔다(품목이 없으면
    마스터가 입력을 못 싣고 매입이 `E4` 로 멈춘다). 채점 항목에 `item` 을 넣었다.

    ★ **`null` 을 못 쓰게 만드는 것이 아니다.** `item` 은 `anyOf[..., null]` 이라
      required 여도 *"모르겠다"* 를 쓸 수 있다. 바뀌는 것은 **매번 판단하게 되는 것**뿐이다.
    """
    schema = Intent.model_json_schema()
    schema["required"] = sorted({*schema.get("required", ()), "agents", "item"})
    return schema


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


#: Gemini `responseSchema` 가 안 받는 칸. JSON Schema 에는 있고 저쪽에는 없다.
_GEMINI_SCHEMA_DROP = frozenset({"title", "default", "additionalProperties", "$schema", "examples"})


def _to_gemini_schema(node: Any) -> Any:
    """JSON Schema → Gemini `responseSchema`.

    ★ **Ollama 는 JSON Schema 를 그대로 먹지만 Gemini 는 못 먹는다.** 그래서 변환이
      필요하고, 변환은 **버리는 것과 바꾸는 것 둘뿐**이다.

      ```text
      버린다   title · default · additionalProperties     Gemini 가 거부한다
      바꾼다   anyOf[X, null] → X + nullable: true         저쪽의 표현 방식이다
      남긴다   description                                 Ollama 도 보고 있다
      ```

    🔴 **`description` 을 남기는 것이 중요하다.** Ollama 에는 스키마를 통째로
      넘기고 있어 모델이 클래스 docstring 을 이미 보고 있다. 여기서 빼면 프로바이더를
      바꾼 것만으로 **모델에게 보이는 지시가 달라진다** — 분류가 달라져도 그게
      모델 탓인지 프롬프트 탓인지 가릴 수 없게 된다.

    🔴 **모르는 `anyOf` 는 터뜨린다.** 조용히 흘려보내면 Gemini 가 400 을 주는데,
      그건 "스키마가 틀렸다" 가 아니라 그냥 호출 실패로 보인다. 여기서 터지면
      서비스가 fallback 으로 보내고 `llm_status` 에 남는다.
    """
    if isinstance(node, list):
        return [_to_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    if "anyOf" in node:
        branches = node["anyOf"]
        concrete = [b for b in branches if b.get("type") != "null"]
        nullable = len(concrete) != len(branches)
        if len(concrete) != 1:
            raise TypeError(
                f"Gemini 로 옮길 수 없는 anyOf 다 (분기 {len(concrete)}개): {branches!r}"
            )
        converted = _to_gemini_schema(concrete[0])
        for key, value in node.items():
            if key == "anyOf" or key in _GEMINI_SCHEMA_DROP:
                continue
            converted[key] = _to_gemini_schema(value)
        if nullable:
            converted["nullable"] = True
        return converted

    return {
        key: _to_gemini_schema(value)
        for key, value in node.items()
        if key not in _GEMINI_SCHEMA_DROP
    }


class GeminiProvider:
    """Gemini REST 호출. 표준 라이브러리만 쓴다 — Ollama 경로와 같은 규율이다.

    ★ **API 키는 호출 시점에 환경에서 읽는다.** `LLMSettings` 에 담지 않는다 —
      설정 객체는 로그·예외에 통째로 실릴 수 있고, 키가 거기 끼면 지울 수 없다.
    ★ 자체 재시도가 없다. 재시도는 `IntentService` 가 소유한다 — 두 층이 세면
      상한이 곱해진다 (Anthropic·OpenAI 프로바이더도 `max_retries=0` 이다).
    """

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str:
        import urllib.error
        import urllib.request

        api_key = os.getenv(f"{_ENV_PREFIX}GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _require_model(self.settings)
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(schema),
                "maxOutputTokens": self.settings.max_output_tokens,
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
            # 🔴 **`HTTPError` 는 감싸지 않는다.** `URLError` 의 하위라 아래 except 가
            #    같이 먹는데, 감싸면 **상태 코드가 사라진다.** 실측에서 429(quota)를
            #    `RuntimeError("Master Gemini request failed")` 로 덮어 버려, 한도에
            #    걸린 것과 서버가 죽은 것이 **로그에서 같아 보였다.**
            #
            #    지금은 `classify` 가 어떤 예외든 fallback 으로 보내므로 화면 동작은
            #    같지만, 원인을 **꺼낼 수 있게는 두어야** 한다 — 물류도 같은 이유로
            #    HTTPError 를 그대로 흘린다.
            raise
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            # 키를 메시지에 싣지 않는다. urllib 예외는 URL 을 담는데 키는 헤더라
            # 안 끼지만, 여기서 새 메시지를 만들 때도 넣지 않는다.
            raise RuntimeError("Master Gemini request failed") from error
        # 🔴 **`parts[0]` 이 아니다 — 사고 조각이 앞에 오는 모델이 있다.**
        #    `gemini-3.5-flash-lite` 는 생각을 켜고 답하며, 그때 `parts` 앞머리에
        #    `thought: true` 인 조각이 붙는다. 첫 조각만 보면 `text` 가 없어 터지고,
        #    **호출은 성공했는데 FALLBACK 으로 떨어진다** — 화면에는 "못 알아들음"
        #    으로 보여서 모델이 틀린 것처럼 읽힌다. 실측에서 `SELECT_SCENARIO` 가
        #    12번 중 11번 이렇게 죽었다 (승인 마디가 통째로 안 되는 상황이다).
        #
        #    같은 함정을 `AnthropicProvider` 가 이미 주석으로 남겨 뒀는데 여기 옮기지
        #    않았다. **프로바이더가 늘 때마다 다시 밟는 자리다.**
        candidates = document.get("candidates") or []
        parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
        for part in parts:
            if part.get("thought"):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        raise TypeError("Gemini response did not contain text content")


class UnavailableProvider:
    """미지원 `LLM_PROVIDER` 값. 조용히 무시하지 않고 **터뜨려 fallback 으로 보낸다**."""

    def generate(self, system: str, user: str, schema: dict[str, Any]) -> str:
        del system, user, schema
        raise RuntimeError("Configured master LLM provider is not supported")


_PROVIDERS: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
}


# ── 검증 ────────────────────────────────────────────────────────────────


class IntentIssue(StrEnum):
    """🔴 **행동을 바꾸는 것만 거부한다.**

    처음에는 "`agents` 는 `STATUS_QUERY` 일 때만" 처럼 **쓰이지도 않는 칸이 차 있는
    것**까지 거부했다. 실측에서 그 엄격함이 손해였다 — 모델이 `PROCUREMENT_RUN` 에
    `agents` 를 곁들이면 거부 → 재시도 → **분류를 UNKNOWN 으로 무르는** 일이 반복됐다.
    쓰지 않는 값이 붙어 있다고 답을 통째로 버리는 셈이었다.

    그래서 셋으로 나눴다.

    ```text
    안 쓰는 칸이 차 있다   →  지운다 (normalize)   — 해가 없다
    필요한 칸이 비었다     →  거부한다             — 부를 대상이 없다
    없던 내용을 지어냈다   →  거부한다             — 그대로 실행에 실린다
    ```
    """

    NOT_JSON = "NOT_JSON"
    SCHEMA = "SCHEMA"
    AGENTS_MISSING = "AGENTS_MISSING"
    AGENT_CANNOT_QUERY = "AGENT_CANNOT_QUERY"
    LABEL_MISSING = "LABEL_MISSING"
    CONDITION_MISSING = "CONDITION_MISSING"
    CONDITION_INVENTED_NUMBER = "CONDITION_INVENTED_NUMBER"


class IntentValidationError(ValueError):
    def __init__(self, issues: list[IntentIssue]) -> None:
        super().__init__(", ".join(issues))
        self.issues = issues


#: 🔴 교정 문구는 **"빠진 칸을 채워라"** 여야 한다.
#:
#: 처음엔 "STATUS_QUERY 면 agents 를 넣는다" 로만 썼는데, 모델이 그 말을 듣고
#: **분류 자체를 UNKNOWN 으로 바꿔** 회피하는 일이 실측에서 나왔다 ("재고 어때?").
#: 빈 칸을 지적받으면 그 칸을 채우는 대신 **답을 무르는 쪽이 더 쉽기 때문**이다.
#: 그래서 고칠 곳을 짚을 때 **분류를 바꾸지 말라**고 함께 못박는다.
_GUIDANCE: dict[IntentIssue, str] = {
    IntentIssue.NOT_JSON: "JSON 만 출력한다. 설명 문장을 붙이지 않는다.",
    IntentIssue.SCHEMA: "지정된 JSON Schema 의 필드와 허용값만 쓴다.",
    IntentIssue.AGENTS_MISSING: (
        "action 은 STATUS_QUERY 로 그대로 두고 agents 만 채워라. "
        "자금·현금·잔고는 finance, 재고·창고·보관은 inventory 다. "
        "UNKNOWN 으로 바꾸지 마라."
    ),
    IntentIssue.AGENT_CANNOT_QUERY: "그 에이전트는 상태 조회를 받지 않는다. 다른 부서를 골라라.",
    IntentIssue.LABEL_MISSING: (
        "action 은 SELECT_SCENARIO 로 그대로 두고 scenario_label 만 채워라 — "
        "사용자가 부른 이름 그대로(기본 · 보수 · 공격). UNKNOWN 으로 바꾸지 마라."
    ),
    IntentIssue.CONDITION_MISSING: (
        "action 은 RERUN_WITH_CONDITION 으로 그대로 두고 condition 만 채워라 — "
        "사용자의 말 그대로. UNKNOWN 으로 바꾸지 마라."
    ),
    IntentIssue.CONDITION_INVENTED_NUMBER: (
        "condition 에 발화문에 없는 숫자를 넣지 않는다. 사용자의 말 그대로 옮긴다."
    ),
}


def retry_guidance(issues: list[IntentIssue]) -> list[str]:
    return [_GUIDANCE[issue] for issue in issues]


def normalize_intent(intent: Intent) -> Intent:
    """그 action 에서 **쓰이지 않는 칸을 지운다.**

    모델은 스키마에 있는 칸을 곧잘 곁들여 채운다 — `PROCUREMENT_RUN` 에 `agents`,
    `UNKNOWN` 에 `item` 같은 식으로. 그 값은 **아무도 읽지 않으므로 해가 없다.**
    거부하면 재시도가 돌고, 재시도에서 모델이 답을 무르는 쪽이 훨씬 비싸다.
    """
    action = intent.action
    return intent.model_copy(
        update={
            "agents": list(intent.agents) if action == "STATUS_QUERY" else [],
            "scenario_label": intent.scenario_label if action == "SELECT_SCENARIO" else None,
            "condition": intent.condition if action == "RERUN_WITH_CONDITION" else None,
            "item": None if action == "UNKNOWN" else intent.item,
        }
    )


def validate_intent(raw_output: str, utterance: str) -> Intent:
    """LLM 출력을 검사한다. **닫힌 열거가 대부분을 막고, 나머지를 여기서 막는다.**

    순서가 중요하다 — **먼저 지우고 나서 검사한다.** 안 쓰는 칸 때문에 답이 버려지지
    않게 하되, 필요한 칸이 빈 것과 지어낸 내용은 그대로 잡는다.
    """
    try:
        intent = Intent.model_validate_json(raw_output)
    except ValidationError as error:
        issue = IntentIssue.NOT_JSON if "json_invalid" in str(error) else IntentIssue.SCHEMA
        raise IntentValidationError([issue]) from error

    intent = normalize_intent(intent)
    issues = _issues(intent, utterance)
    if issues:
        raise IntentValidationError(issues)
    return intent


def _issues(intent: Intent, utterance: str) -> list[IntentIssue]:
    """**정규화 뒤에** 남는 문제만 본다 — 빈 필수 칸과 지어낸 내용."""
    out: list[IntentIssue] = []
    action = intent.action

    if action == "STATUS_QUERY":
        if not intent.agents:
            out.append(IntentIssue.AGENTS_MISSING)
        for agent in intent.agents:
            if "STATUS_QUERY" not in agent_allowed_modes(agent):
                out.append(IntentIssue.AGENT_CANNOT_QUERY)
                break

    if action == "SELECT_SCENARIO" and not (intent.scenario_label or "").strip():
        out.append(IntentIssue.LABEL_MISSING)

    if action == "RERUN_WITH_CONDITION":
        condition = (intent.condition or "").strip()
        if not condition:
            out.append(IntentIssue.CONDITION_MISSING)
        elif _invents_digits(condition, utterance):
            out.append(IntentIssue.CONDITION_INVENTED_NUMBER)

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

#: 되묻는 말에 쓰는 부서 이름. `answer.py` 와 같은 어휘다.
_DEPT_LABEL = {"finance": "재무", "inventory": "물류", "purchase": "매입"}


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
            return self._result(
                _UNKNOWN, status="DISABLED", attempts=0, fallback=False, utterance=text
            )
        if not text:
            return self._result(_UNKNOWN, status="SKIPPED_TEMPLATE", attempts=0, fallback=False)
        # 아래 경로는 전부 발화문을 넘긴다 — 빈 발화문에는 이름 붙일 것이 없다.

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
                    utterance=text,
                )
            except IntentValidationError as error:
                guidance = retry_guidance(error.issues)
            except Exception:  # noqa: BLE001 — 분류 실패가 API 를 죽이면 안 된다
                break
        return self._result(
            _UNKNOWN, status="FALLBACK", attempts=attempts, fallback=True, utterance=text
        )

    def _result(
        self,
        intent: Intent,
        *,
        status: LLMStatus,
        attempts: int,
        fallback: bool,
        utterance: str = "",
    ) -> IntentResult:
        """`utterance` 는 **되물을 말을 고르는 데만** 쓴다.

        분류에는 안 쓴다 — 분류는 이미 끝났고, 여기서 발화문을 다시 보면 규칙이
        모델의 판정을 덮게 된다. 여기서 하는 일은 *"없는 것을 없다고 이름 붙이는 것"*
        뿐이다.
        """
        confirm = _needs_confirmation(intent)
        return IntentResult(
            intent=intent,
            llm_status=status,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model or None,
            llm_attempts=attempts,
            llm_fallback_used=fallback,
            needs_confirmation=confirm,
            clarification=_clarification(intent, utterance) if confirm else None,
        )


def _needs_confirmation(intent: Intent) -> bool:
    if intent.action == "UNKNOWN":
        return True
    if intent.confidence != "HIGH":
        return True
    return intent.action not in _NO_CONFIRM_ACTIONS


#: 🔴 **물어볼 만한데 답할 자리가 없는 것.** 이름을 붙여 준다.
#:
#: *"못 알아들었습니다"* 만 적으면 물어본 사람은 **자기가 말을 잘못했다고 생각하고**
#: 표현을 바꿔 다시 묻는다. 그래도 안 된다 — 없는 것이기 때문이다. 없는 것은
#: **없다고 말해야** 그 사람이 다른 길을 찾는다.
#:
#: ★ 여기 없는 말은 종전대로 일반 안내로 간다. 목록을 늘려 가며 맞히는 것이 아니라,
#:   **자주 묻는데 답이 없는 것**만 이름을 준다.
_KNOWN_GAPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("가격", "시세", "단가", "시가"),
        # 🔴 **"mock 이라" 라고 적었다가 거짓말이 됐다 (2026-08-31).** ML backfill 이
        #    들어와 `forecast` 가 `MEASURED` 가 됐는데 이 문장이 안 따라왔다.
        #    **화면 문구에 다른 파트의 상태를 적으면, 그 파트가 바뀔 때 거짓이 된다.**
        #    이제는 "쓰는 자리가 다르다" 만 말한다 — 그건 변하지 않는다.
        (
            "품목 가격·시세를 조회하는 자리는 아직 없습니다. "
            "ML 예측은 매입안을 만드는 데 쓰고 가격 답변으로는 내지 않습니다 — "
            "재무 조회는 회사 자금이지 품목 가격이 아닙니다."
        ),
    ),
)


def _known_gap(utterance: str) -> str | None:
    for words, message in _KNOWN_GAPS:
        if any(word in utterance for word in words):
            return message
    return None


def _clarification(intent: Intent, utterance: str = "") -> str:
    """되물을 말. **규칙이 만든다** — LLM 이 쓰면 사용자 응답 생성(⑥)이 되고, 그건 아직 없다."""
    if intent.action == "UNKNOWN":
        gap = _known_gap(utterance)
        if gap:
            return (
                f"{gap} "
                "매입안 생성 · 부서 상태 조회 · 조건 변경 재요청 · 안 선택은 됩니다."
            )
        return (
            "무엇을 해 드릴지 알아듣지 못했습니다. "
            "매입안 생성 · 부서 상태 조회 · 조건 변경 재요청 · 안 선택 중 하나로 말씀해 주세요."
        )
    if intent.action == "PROCUREMENT_RUN":
        item = intent.item or "품목"
        return f"{item} 매입안을 새로 만들까요? (부서 호출이 일어납니다)"
    if intent.action == "STATUS_QUERY":
        # 확신이 낮아 확인받는 경우다. **어느 부서에 물을 것인지** 되읽어 준다.
        names = ", ".join(_DEPT_LABEL.get(a, a) for a in intent.agents) or "부서"
        return f"{names} 상태를 조회할까요?"
    if intent.action == "SELECT_SCENARIO":
        # **무엇을 고른 것으로 알아들었는지 되읽어 준다.** 승인은 되돌리기 어려우므로
        # "진행할까요?" 만 물으면 사용자가 무엇에 동의하는지 모른 채 누른다.
        return f"'{intent.scenario_label}' 안을 고르신 것으로 승인 기록할까요?"
    if intent.action == "RERUN_WITH_CONDITION":
        return f"'{intent.condition}' 조건을 붙여 다시 만들까요?"
    return "이렇게 이해했습니다. 진행할까요?"


def _user_payload(utterance: str, guidance: list[str] | None) -> str:
    payload: dict[str, Any] = {"utterance": utterance}
    if guidance:
        payload["correction"] = guidance
    return json.dumps(payload, ensure_ascii=False)


def build_provider(settings: LLMSettings) -> TextProvider:
    """설정에 맞는 프로바이더. **모르는 값이면 터뜨리는 것을 돌려준다.**

    ★ 역할(①분류 · ⑥응답 생성)마다 프로바이더를 새로 고르게 하지 않는다. 지금은 둘이
      같은 `.env` 를 보지만, **역할마다 모델 등급이 달라지는 것이 예정된 변화**라
      (분류는 소형 · 판정 검증은 상위 모델) 고르는 자리를 한 곳으로 모아 둔다.
    """
    factory = _PROVIDERS.get(settings.provider)
    return factory(settings) if factory else UnavailableProvider()


def get_intent_service() -> IntentService:
    settings = get_llm_settings()
    return IntentService(settings, build_provider(settings))
