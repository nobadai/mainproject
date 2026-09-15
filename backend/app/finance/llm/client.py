"""Finance LLM 전송 계층 — HTTP · 설정 · 가용성 판별.

이 파일이 소유하는 것
    Finance LLM 설정(활성화 · Provider · 모델) · Gemini/Ollama HTTP 호출 ·
    응답 파싱 · **가용성 실패 판별** · Gemini 전송 형식 낮추기

여기 **없는 것**
    무엇을 부를지의 판단 · 재무 계산 · 설명 선택
    → `planner` · `capabilities` · `finalizer` 소유다.

★ 가용성 실패만 Provider 대체 사유다. 429·5xx·타임아웃·네트워크·키 없음은
  *"지금 못 부른다"* 이고, 그 외 오류는 *"불렀는데 답이 틀렸다"* 라 대체로 숨기면
  안 된다 — 다른 Provider 로 옮겨도 같은 답이 나온다.

★ 설정이 여기 있는 이유: *"어느 Provider 로 어떤 모델을 부르는가"* 는 전송의 일부다.
  전역 `LLM_PROVIDER` 를 상속하지 않는다 — 전역을 ollama 로 둔 배포에서 재무가 조용히
  Gemini 를 떠나면 값은 멀쩡히 나오고 아무도 눈치채지 못한다.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Finance LLM 설정
# ---------------------------------------------------------------------------

_DEFAULT_MODELS = {
    "ollama": "gemma3:4b",
    "gemini": "gemini-3.5-flash-lite",
}

#: Ollama 로 **Planner** 를 돌릴 때의 기본 모델. `_DEFAULT_MODELS["ollama"]` 와
#: 일부러 다르다.
#:
#: 🔴 재무 Planner 는 tool calling 으로 돈다. `gemma3` 계열은 Ollama 에서 tool 을
#:    지원하지 않아 선언을 실으면 **HTTP 400 (`does not support tools`)** 이다.
#:    그래서 Gemini 가 못 뜰 때 대체가 같이 죽었다 — 대체 경로는 대체가 필요한
#:    날에만 도는 코드라 설정만으로는 이 사실이 드러나지 않는다.
#:
#: ★ 설명(Finalizer)은 tool calling 이 아니라서 기존 기본값을 그대로 쓴다. 여기서
#:   바꾸는 것은 **Tool 을 부르는 자리뿐**이다.
#:
#: ★ 추론형(thinking) 모델은 고르지 않는다. 대체는 Gemini 가 못 뜬 날에 도는 경로라
#:   `LLM_TIMEOUT_SECONDS` 안에 답해야 뜻이 있다 — 한 단계에 30 초를 넘기면 대체가
#:   있어도 실행은 그대로 실패한다.
_DEFAULT_OLLAMA_TOOL_CALLING_MODEL = "llama3.2:3b"


def _ollama_tool_calling_model() -> str:
    """Ollama Planner 모델. 설치된 모델은 배포마다 다르므로 재무 키로 덮을 수 있다."""
    _load_finance_environment()
    return (
        os.getenv("FINANCE_OLLAMA_PLANNER_MODEL")
        or _DEFAULT_OLLAMA_TOOL_CALLING_MODEL
    )
_ENV_FILES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
)


def _load_finance_environment() -> None:
    for env_file in _ENV_FILES:
        load_dotenv(env_file, override=False)


def _read_bool(key: str) -> bool | None:
    """설정된 경우에만 bool 을 돌려준다. 미설정과 false 를 섞지 않기 위해서다."""
    value = os.getenv(key)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def finance_llm_enabled() -> bool:
    """Finance Agent LLM 활성화 여부.

    ``FINANCE_LLM_ENABLED`` → ``LLM_ENABLED`` → 기본 활성. 재무만 끄고 싶은 경우와
    전역으로 끈 경우를 구분한다 (재무 전용 키가 전역 키를 이긴다).
    """
    _load_finance_environment()
    finance = _read_bool("FINANCE_LLM_ENABLED")
    if finance is not None:
        return finance
    shared = _read_bool("LLM_ENABLED")
    if shared is not None:
        return shared
    return True


def _finance_provider_name() -> str:
    """★ 전역 ``LLM_PROVIDER`` 를 상속하지 않는다.

    전역은 레거시 Ollama 해석 계층이 쓰는 값이다. 그것을 상속하면 전역을 ollama 로
    둔 배포에서 재무 Agent 가 조용히 Gemini 를 떠난다 — 재무 Provider 정책은 재무
    키로만 정해진다.
    """
    _load_finance_environment()
    provider = (
        os.getenv("FINANCE_LLM_PROVIDER")
        or "gemini"
    ).strip().lower()
    if provider not in _DEFAULT_MODELS:
        raise RuntimeError("Configured Finance LLM provider is not supported")
    return provider


def _finance_model(provider: str) -> str:
    _load_finance_environment()
    explicit = os.getenv("FINANCE_LLM_MODEL")
    if explicit:
        return explicit
    global_provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    global_model = os.getenv("LLM_MODEL")
    if provider == global_provider and global_model:
        return global_model
    return _DEFAULT_MODELS[provider]


def finance_planner_model(provider: str) -> str:
    """Planner 가 실제로 부를 모델.

    ★ 재무 전용 설정(`FINANCE_LLM_MODEL`)이 있으면 그대로 따른다 — 운영자가 고른
      모델을 우리가 덮지 않는다.

    🔴 설정이 없을 때 **전역 `LLM_MODEL` 을 물려받지 않는다.** 전역은 레거시 해석
       계층(tool 을 부르지 않는다)이 쓰는 값이고, 그것을 Planner 가 상속하면 tool 을
       지원하지 않는 모델로 tool calling 을 시도하게 된다.
    """
    _load_finance_environment()
    explicit = os.getenv("FINANCE_LLM_MODEL")
    if explicit:
        return explicit
    if provider == "ollama":
        return _ollama_tool_calling_model()
    return _DEFAULT_MODELS[provider]


# ---------------------------------------------------------------------------
# Provider HTTP 와 가용성 실패 판별
# ---------------------------------------------------------------------------

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _gemini_response_text(document: dict[str, Any]) -> str:
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    for part in parts:
        if part.get("thought"):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    raise TypeError("Finance Gemini response did not contain text content")


def _gemini_generate(
    *, model: str, system_prompt: str, user_payload: dict[str, Any], response_schema: dict[str, Any]
) -> str:
    _load_finance_environment()
    api_key = os.getenv("FINANCE_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Finance Gemini API key is not set")
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(user_payload, default=str)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }
    request = urllib.request.Request(
        f"{_GEMINI_BASE_URL}/models/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        ) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError("Finance Gemini request failed") from error
    return _gemini_response_text(document)


def _gemini_availability_failure_reason(error: Exception) -> str | None:
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 429:
            return "HTTP_429"
        if 500 <= error.code < 600:
            return "HTTP_5XX"
        return None
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, urllib.error.URLError):
        return "NETWORK_ERROR"
    if (
        isinstance(error, RuntimeError)
        and str(error) == "Finance Gemini API key is not set"
    ):
        return "API_KEY_MISSING"
    if isinstance(error.__cause__, TimeoutError):
        return "TIMEOUT"
    if isinstance(error.__cause__, urllib.error.URLError):
        return "NETWORK_ERROR"
    return None


def _is_gemini_availability_failure(error: Exception) -> bool:
    return _gemini_availability_failure_reason(error) is not None


def _ollama_availability_failure_reason(error: Exception) -> str | None:
    """Ollama가 지금 Planner 요청을 수행할 수 없는 경우만 분류한다.

    404는 endpoint 또는 model 부재이고, 429/5xx·timeout·network 오류도 provider가
    현재 실행 불가한 상태다. 반면 400은 tool/schema 계약 문제일 수 있으므로 가용성
    장애로 낮추지 않는다.
    """
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 404:
            return "HTTP_404"
        if error.code == 429:
            return "HTTP_429"
        if 500 <= error.code < 600:
            return "HTTP_5XX"
        return None
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, urllib.error.URLError):
        return "NETWORK_ERROR"
    if isinstance(error.__cause__, TimeoutError):
        return "TIMEOUT"
    if isinstance(error.__cause__, urllib.error.URLError):
        return "NETWORK_ERROR"
    return None


def _ollama_base_url() -> str:
    return os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _llm_timeout_seconds() -> float:
    return float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))


def _gemini_safe_schema(node: Any) -> Any:
    """Tool 인자 스키마를 **Gemini 가 받는 표현으로** 낮춘다.

    🔴 계약을 낮추는 것이 아니라 표현만 낮춘다. Gemini Schema 는 OpenAPI 3.0 부분집합이라
       세 가지를 못 받고, 그대로 보내면 **HTTP 400** 이다 — 재무가 아니라 전송 형식이
       문제인데 재무 Planner 가 매 호출 실패한다.

         · `const`                 → STRING + 한 값짜리 `enum`
         · `anyOf` 안의 `type:null` → 그 갈래를 빼고 `nullable`
         · `additionalProperties`  → 제거

    ★ pydantic 이 만드는 모양이라 손으로 피할 수 없다. `Literal["amount"]` 은 `const` 를,
      `float | None` 은 null 갈래를 낸다 — 두 표현 모두 우리가 쓰고 싶은 계약이다.
    """
    if not isinstance(node, dict):
        return node
    safe = {
        key: value
        for key, value in node.items()
        if key not in {"const", "anyOf", "additionalProperties"}
    }
    if "const" in node:
        safe["type"] = "string"
        safe["enum"] = [node["const"]]
    if "anyOf" in node:
        branches = [
            branch
            for branch in node["anyOf"]
            if isinstance(branch, dict) and branch.get("type") != "null"
        ]
        if len(branches) != len(node["anyOf"]):
            safe["nullable"] = True
        if len(branches) == 1:
            safe.update(_gemini_safe_schema(branches[0]))
        elif branches:
            safe["anyOf"] = [_gemini_safe_schema(branch) for branch in branches]
    if "properties" in node:
        safe["properties"] = {
            name: _gemini_safe_schema(child) for name, child in node["properties"].items()
        }
    if "items" in node:
        safe["items"] = _gemini_safe_schema(node["items"])
    return safe


def _gemini_tool_call(
    *,
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    tool_declarations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gemini function calling. 이번 호출에서 **부를 수 있는 함수만** 선언한다.

    ``mode: ANY`` + ``allowedFunctionNames`` 로 자유 문장 답을 닫는다. 다만 이것은
    전송 계층 강제일 뿐이라, 돌아온 이름이 정말 허용된 것인지는 Planner 사후 검증과
    Harness 가 다시 본다 — 구조화 출력을 무시하는 모델이 있다.
    """
    _load_finance_environment()
    api_key = os.getenv("FINANCE_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Finance Gemini API key is not set")
    names = [item["name"] for item in tool_declarations]
    declarations = [
        {**item, "parameters": _gemini_safe_schema(item.get("parameters", {}))}
        for item in tool_declarations
    ]
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {"role": "user", "parts": [{"text": json.dumps(user_payload, default=str)}]}
        ],
        "tools": [{"function_declarations": declarations}],
        "toolConfig": {
            "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": names}
        },
        "generationConfig": {"temperature": 0},
    }
    request = urllib.request.Request(
        f"{_GEMINI_BASE_URL}/models/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_llm_timeout_seconds()) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError("Finance Gemini request failed") from error
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    calls = [
        {"name": part["functionCall"].get("name"), "args": part["functionCall"].get("args") or {}}
        for part in parts
        if isinstance(part.get("functionCall"), dict)
    ]
    return calls


def _ollama_tool_call(
    *,
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    tool_declarations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ollama tool calling. Gemini 와 **같은 Tool 목록**을 받는다.

    Provider 마다 허용 범위가 달라지면 같은 재무 상태가 다른 Tool 을 부를 수 있게
    열린다 — 선언은 한 곳(`tool_adapter`)에서 만들어 양쪽에 그대로 간다.
    """
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "tools": [
            {"type": "function", "function": declaration}
            for declaration in tool_declarations
        ],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ],
        "options": {"temperature": 0},
    }
    request = urllib.request.Request(
        f"{_ollama_base_url()}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_llm_timeout_seconds()) as response:
        document = json.loads(response.read().decode())
    raw_calls = (document.get("message") or {}).get("tool_calls") or []
    return [
        {
            "name": (item.get("function") or {}).get("name"),
            "args": (item.get("function") or {}).get("arguments") or {},
        }
        for item in raw_calls
    ]
