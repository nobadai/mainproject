"""물류 Provider 구현체 — **네트워크 없이** 반환 계약과 예외 계약을 잡는다 (#402).

★ 실 호출을 섞으면 키가 있는 사람만 돌릴 수 있는 검사가 되고, 그러면 아무도 안 돌린다
  (`test_critic_gemini_provider.py` 와 같은 이유 · 같은 fake `urlopen` 패턴).

🔴 **여기서 latency 를 재지 않는다.** Provider 는 계속 `-> str` 이고, 호출 시간의 주인은
  `InterpretationService` 다 (`test_logistics_interpretation_runtime.py`). Provider 안에서
  재면 Ollama·Gemini 두 벌로 복제되고 **예외로 끝난 호출의 시간을 잃는다** — 그런데
  timeout 이야말로 FALLBACK 진단에서 가장 알고 싶은 숫자다.

★ Provider 에는 호출 상태를 남기지 않는다 (`last_latency` 등). 그 계약은 구조 검사로
  `test_logistics_interpretation_runtime.py::test_provider_keeps_no_call_state` 가 잠근다.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Self

import pytest

from app.logistics.llm.runtime import (
    GeminiProvider,
    LLMSettings,
    OllamaProvider,
    ProviderAuthError,
    ProviderConfigurationError,
    UnavailableProvider,
)
from app.logistics.llm.schemas import ContextFact, SanitizedLLMContext

_ANSWER = json.dumps(
    {
        "summary": "신선도 임박 Lot 검토가 필요합니다.",
        "risks": ["INVENTORY_FRESHNESS_PRESSURE"],
        "suggested_adjustment": None,
    },
    ensure_ascii=False,
)


def _settings(**over: Any) -> LLMSettings:
    base = {
        "enabled": True,
        "provider": "ollama",
        "model": "fake-model",
        "base_url": "http://127.0.0.1:11434",
        "timeout_seconds": 1.0,
        "max_retries": 1,
    }
    base.update(over)
    return LLMSettings(**base)


def _context() -> SanitizedLLMContext:
    return SanitizedLLMContext(
        signals=["INVENTORY_FRESHNESS_PRESSURE"],
        facts=[
            ContextFact(
                fact_id="freshness_risk_lot_count",
                label="신선도 임박 가용 Lot 수",
                display_value="3개",
            )
        ],
        allowed_adjustments=["quantity", "timing"],
    )


class _FakeResponse:
    def __init__(self, document: Any, *, raw: bytes | None = None) -> None:
        self._payload = raw if raw is not None else json.dumps(document).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _fake_urlopen(monkeypatch, document: Any, *, raw: bytes | None = None) -> dict:
    seen: dict[str, Any] = {}

    def urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(document, raw=raw)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


# ── Ollama ───────────────────────────────────────────────────────────────


def test_ollama_returns_the_message_content_string(monkeypatch):
    seen = _fake_urlopen(monkeypatch, {"message": {"content": _ANSWER}})

    output = OllamaProvider(_settings()).generate(_context())

    assert output == _ANSWER
    assert isinstance(output, str), "Provider 반환형은 계속 str 이다 (#402)"
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["body"]["model"] == "fake-model"


def test_ollama_carries_correction_only_when_given(monkeypatch):
    seen = _fake_urlopen(monkeypatch, {"message": {"content": _ANSWER}})
    provider = OllamaProvider(_settings())

    provider.generate(_context())
    user_payload = json.loads(seen["body"]["messages"][1]["content"])
    assert "correction" not in user_payload

    provider.generate(_context(), retry_guidance=["숫자를 새로 만들지 마세요."])
    user_payload = json.loads(seen["body"]["messages"][1]["content"])
    assert user_payload["correction"] == ["숫자를 새로 만들지 마세요."]


def test_ollama_missing_content_is_a_type_error(monkeypatch):
    """분류는 `classify_llm_error` 몫이다 — Provider 는 예외 종류만 정직하게 낸다."""
    _fake_urlopen(monkeypatch, {"message": {}})

    with pytest.raises(TypeError):
        OllamaProvider(_settings()).generate(_context())


def test_ollama_malformed_json_becomes_a_runtime_error(monkeypatch):
    """JSONDecodeError 는 원인 체인에 남는다 — 재시도 판정이 그것을 읽는다."""
    _fake_urlopen(monkeypatch, None, raw=b"not json")

    with pytest.raises(RuntimeError) as caught:
        OllamaProvider(_settings()).generate(_context())
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


# ── Gemini ───────────────────────────────────────────────────────────────


def _pin_key(monkeypatch, value: str | None = "test-key") -> None:
    """🔴 개발자 `.env` 의 실 키를 먼저 지운다 — 안 지우면 검사가 진짜 키를 쓴다."""
    monkeypatch.delenv("LOGISTICS_GEMINI_API_KEY", raising=False)
    if value is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", value)


def test_gemini_returns_the_candidate_text_string(monkeypatch):
    _pin_key(monkeypatch)
    monkeypatch.delenv("LOGISTICS_GEMINI_BASE_URL", raising=False)
    seen = _fake_urlopen(monkeypatch, {"candidates": [{"content": {"parts": [{"text": _ANSWER}]}}]})

    output = GeminiProvider(_settings(provider="gemini", model="gemini-fake")).generate(_context())

    assert output == _ANSWER
    assert isinstance(output, str)
    assert seen["url"].endswith("/models/gemini-fake:generateContent")
    # 🔴 Ollama 기본 base_url 은 쓰이지 않는다 — 두 provider 의 주소는 다른 축이다
    assert "11434" not in seen["url"]


def test_gemini_sends_the_key_in_the_header_not_the_url(monkeypatch):
    _pin_key(monkeypatch, "super-secret")
    monkeypatch.delenv("LOGISTICS_GEMINI_BASE_URL", raising=False)
    seen = _fake_urlopen(monkeypatch, {"candidates": [{"content": {"parts": [{"text": _ANSWER}]}}]})

    GeminiProvider(_settings(provider="gemini")).generate(_context())

    assert seen["headers"]["X-goog-api-key"] == "super-secret"
    assert "super-secret" not in seen["url"]


def test_gemini_without_a_key_raises_provider_auth_error(monkeypatch):
    """전송 전 실패다 — `classify_llm_error` 가 AUTH_ERROR 로 읽어 재시도하지 않는다."""
    _pin_key(monkeypatch, None)

    with pytest.raises(ProviderAuthError) as caught:
        GeminiProvider(_settings(provider="gemini")).generate(_context())
    assert "super" not in str(caught.value), "키 원문을 예외 메시지에 싣지 않는다"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"candidates": []}, id="빈_candidates"),
        pytest.param({"candidates": [{"content": {}}]}, id="parts_없음"),
        pytest.param({}, id="candidates_없음"),
    ],
)
def test_gemini_malformed_response_is_a_type_error(monkeypatch, document):
    _pin_key(monkeypatch)
    monkeypatch.delenv("LOGISTICS_GEMINI_BASE_URL", raising=False)
    _fake_urlopen(monkeypatch, document)

    with pytest.raises(TypeError):
        GeminiProvider(_settings(provider="gemini")).generate(_context())


# ── Unavailable ──────────────────────────────────────────────────────────


def test_unavailable_provider_always_raises_configuration_error():
    """미지원 provider 이름의 자리 — 다시 불러도 같으므로 즉시 FALLBACK 이다."""
    with pytest.raises(ProviderConfigurationError):
        UnavailableProvider().generate(_context())


def test_unavailable_provider_opens_no_socket(monkeypatch):
    def boom(*_: object, **__: object) -> None:
        raise AssertionError("UnavailableProvider 가 네트워크를 열었다")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with pytest.raises(ProviderConfigurationError):
        UnavailableProvider().generate(_context())
