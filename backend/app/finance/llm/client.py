"""Provider HTTP 호출과 **가용성 실패 판별**.

★ 가용성 실패만 Provider 대체 사유다 (§17). 429·5xx·타임아웃·네트워크·키 없음은
  *"지금 못 부른다"* 이고, 그 외 오류는 *"불렀는데 답이 틀렸다"* 라 대체로 숨기면
  안 된다 — 다른 Provider 로 옮겨도 같은 답이 나온다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from app.finance.llm import config as _config

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
    _config._load_finance_environment()
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
