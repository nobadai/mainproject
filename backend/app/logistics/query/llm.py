"""STATUS_QUERY 전용 **LLM function/tool calling 전송** (Issue #789).

🔴 이 파일은 provider 의 **실제 function calling API** 를 쓴다 — LLM 에게 tool schema 를
   주고, LLM 이 낸 `tool_calls`(Ollama) / `functionCall`(Gemini) 를 받아 돌려준다.
   Tool 선택은 LLM 이 한다 — 여기서 topic→tool 매핑을 하지 않는다.

🔴 **한 번 왕복 = `chat(messages, tools) -> AssistantTurn`.** 반환은 «LLM 이 부른 tool
   목록» 이거나 «최종 자연어 답변» 이다. Tool 실행·결과 되먹임의 loop 는 부르는 쪽
   (`status_query.answer_status_question`)이 돈다 — 이 파일은 전송만 한다.

⚠️ **기존 LLM 흐름을 건드리지 않는다.** 조사 planner(`agent/llm_client.py`)와 해석기
   (`llm/runtime.py`)는 각자 자기 계약에 묶여 있어 재사용하지 않고, 설정(`get_llm_settings`)만
   빌린다. provider wire 형식은 조사 planner 의 것을 **복제**했다(공통화하지 않는다).

🔴 **실패는 감추지 않는다.** provider 미설정·타임아웃·비활성이면 `StatusQueryLLMError`
   를 던진다 — 결정론 파서로 조용히 fallback 하지 않는다(#789).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.logistics.llm.runtime import get_llm_settings

__all__ = [
    "AssistantTurn",
    "Chat",
    "StatusQueryLLMError",
    "ToolCall",
    "build_chat",
]

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    """LLM 이 부른 tool 하나. `arguments` 는 provider 가 준 그대로(예: `item_name`)다."""

    id: str
    name: str
    arguments: dict[str, Any]
    #: 🔴 **공급자가 준 원본 파트.** 되돌려줄 때 **그대로** 실어야 하는 불투명 필드가
    #: 있어서다 — Gemini 3.x 는 `functionCall` 파트의 `thoughtSignature` 를 echo 하지
    #: 않으면 다음 턴을 400 으로 거절한다("Function call is missing a thought_signature").
    #: 우리가 `name`·`arguments` 로 재구성하면 그 서명이 유실된다.
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class AssistantTurn:
    """LLM 한 턴의 결과 — tool 을 부르거나(tool_calls) 최종 답(text)을 낸다."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str | None = None


#: chat seam. 부르는 쪽이 이 시그니처의 콜러블을 주입할 수 있다(테스트=가짜 LLM).
Chat = Callable[[list[dict[str, Any]], Sequence[Mapping[str, Any]]], AssistantTurn]


class StatusQueryLLMError(RuntimeError):
    """LLM 전송이 실패했다. `kind` 로 사유를 구분한다(DISABLED · CONFIG · TRANSPORT)."""

    def __init__(self, message: str, *, kind: str = "TRANSPORT") -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# 전송 (표준 라이브러리만 — SDK 없음, 조사 planner 규율과 같다)
# ---------------------------------------------------------------------------


def _post(url: str, *, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _gemini_key() -> str:
    key = os.getenv("LOGISTICS_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise StatusQueryLLMError("Gemini API key is not set", kind="CONFIG")
    return key


def _to_ollama_message(message: Mapping[str, Any]) -> dict[str, Any]:
    role = message["role"]
    if role == "assistant" and message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in message["tool_calls"]
            ],
        }
    if role == "tool":
        return {"role": "tool", "content": message["content"]}
    return {"role": role, "content": message.get("content", "")}


def _ollama_chat(
    settings: Any, messages: list[dict[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> AssistantTurn:
    body = {
        "model": settings.model,
        "stream": False,
        "think": False,
        "tools": [{"type": "function", "function": dict(tool)} for tool in tools],
        "messages": [_to_ollama_message(m) for m in messages],
        "options": {"temperature": 0},
    }
    document = _post(
        f"{settings.base_url}/api/chat", body=body, headers={}, timeout=settings.timeout_seconds
    )
    message = document.get("message") or {}
    raw_calls = message.get("tool_calls") or []
    calls = [
        ToolCall(
            id=f"call-{index}",
            name=(item.get("function") or {}).get("name") or "",
            arguments=dict((item.get("function") or {}).get("arguments") or {}),
        )
        for index, item in enumerate(raw_calls)
    ]
    # ★ Ollama 는 되돌릴 때 불투명 필드를 요구하지 않아 `raw` 를 안 싣는다 —
    #   `_to_ollama_message` 가 name·arguments 로 재구성해도 계약이 성립한다.
    if calls:
        return AssistantTurn(tool_calls=calls, text=None)
    return AssistantTurn(tool_calls=[], text=message.get("content") or "")


def _to_gemini_content(message: Mapping[str, Any]) -> dict[str, Any]:
    role = message["role"]
    if role == "assistant" and message.get("tool_calls"):
        # 🔴 원본 파트가 있으면 **그대로** 돌려준다 — `thoughtSignature` 를 재구성으로
        #    떨어뜨리면 Gemini 가 다음 턴을 400 으로 거절한다.
        return {
            "role": "model",
            "parts": [
                c["raw"]
                if isinstance(c.get("raw"), Mapping)
                else {"functionCall": {"name": c["name"], "args": c["arguments"]}}
                for c in message["tool_calls"]
            ],
        }
    if role == "assistant":
        return {"role": "model", "parts": [{"text": message.get("content", "")}]}
    if role == "tool":
        try:
            response = json.loads(message["content"])
        except (json.JSONDecodeError, TypeError):
            response = {"result": message["content"]}
        return {
            "role": "user",
            "parts": [{"functionResponse": {"name": message["name"], "response": response}}],
        }
    return {"role": "user", "parts": [{"text": message.get("content", "")}]}


def _gemini_chat(
    settings: Any, messages: list[dict[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> AssistantTurn:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [_to_gemini_content(m) for m in messages if m["role"] != "system"],
        # 🔴 tool schema 를 provider 에 그대로 전달한다 — 우리 스키마는 $ref/const/anyOf 가
        #    없어 Gemini OpenAPI 부분집합에 이미 맞는다.
        "tools": [{"function_declarations": [dict(tool) for tool in tools]}],
        # mode=AUTO — LLM 이 tool 을 더 부르거나 최종 답(text)을 낼 수 있게 둔다.
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"temperature": 0},
    }
    document = _post(
        f"{_GEMINI_BASE_URL}/models/{settings.model}:generateContent",
        body=body,
        headers={"x-goog-api-key": _gemini_key()},
        timeout=settings.timeout_seconds,
    )
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    calls = [
        ToolCall(
            id=f"call-{index}",
            name=part["functionCall"].get("name") or "",
            arguments=dict(part["functionCall"].get("args") or {}),
            # 🔴 파트를 통째로 보존한다 — `thoughtSignature` 같은 불투명 필드를 다음 턴에
            #    그대로 돌려줘야 한다(재구성하면 400).
            raw=dict(part),
        )
        for index, part in enumerate(parts)
        if isinstance(part.get("functionCall"), Mapping)
    ]
    if calls:
        return AssistantTurn(tool_calls=calls, text=None)
    text = "".join(str(part.get("text", "")) for part in parts if "text" in part)
    return AssistantTurn(tool_calls=[], text=text)


def build_chat() -> Chat:
    """설정된 provider 에 묶인 `chat` 콜러블. 🔴 실패는 `StatusQueryLLMError` 로 올린다.

    ★ 테스트는 이 함수를 부르지 않고 `answer_status_question(chat=<가짜>)` 로 주입한다.
    """
    settings = get_llm_settings()
    if not settings.enabled:
        raise StatusQueryLLMError("Logistics LLM is disabled", kind="DISABLED")
    provider = settings.provider
    if provider == "ollama":
        transport = _ollama_chat
    elif provider == "gemini":
        transport = _gemini_chat
    else:
        raise StatusQueryLLMError(f"unsupported provider: {provider}", kind="CONFIG")

    def _chat(messages: list[dict[str, Any]], tools: Sequence[Mapping[str, Any]]) -> AssistantTurn:
        try:
            return transport(settings, messages, tools)
        except StatusQueryLLMError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as error:
            raise StatusQueryLLMError(f"LLM transport failed: {error!r}") from error

    return _chat
