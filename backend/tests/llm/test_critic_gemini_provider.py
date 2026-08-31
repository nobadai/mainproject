"""Critic Gemini 프로바이더 — **네트워크 없이** 요청 모양과 모델 선택을 잡는다.

★ 실 호출을 섞으면 키가 있는 사람만 돌릴 수 있는 검사가 되고, 그러면 아무도 안
  돌린다 (매입 8/31 회신 §5 와 같은 이유다).
"""

from __future__ import annotations

import json
from typing import Any, Self

import pytest

from app.critic.llm import runtime
from app.critic.llm.runtime import GeminiProvider, LLMSettings, get_judge_service
from app.critic.llm.schemas import JudgeInterpretation, SanitizedLLMContext


def _settings(**over: Any) -> LLMSettings:
    base = {
        "enabled": True,
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "base_url": "http://127.0.0.1:11434",  # 🔴 Ollama 기본값 — 안 쓰여야 한다
        "timeout_seconds": 30.0,
        "max_retries": 1,
    }
    base.update(over)
    return LLMSettings(**base)


class _FakeResponse:
    def __init__(self, document: dict[str, Any]) -> None:
        self._payload = json.dumps(document).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


_ANSWER = '{"summary":"모순 없음","verdict":"PASS","note":"근거가 facts 에 있다"}'


def _context() -> SanitizedLLMContext:
    return SanitizedLLMContext.model_construct()


def _capture(monkeypatch: pytest.MonkeyPatch, parts: list[dict] | None = None) -> dict:
    """🔴 개발자 `.env` 의 실 키를 먼저 지운다 — 안 지우면 검사가 진짜 키를 쓰고,
    실패 출력에 그것이 찍힌다 (마스터에서 실제로 한 번 그렇게 깨졌다)."""
    monkeypatch.delenv("CRITIC_GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {"candidates": [{"content": {"parts": parts or [{"text": _ANSWER}]}}]}
        )

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


# ── 요청 모양 ────────────────────────────────────────────────────────────


def test_Ollama_주소로_쏘지_않는다(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CRITIC_GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate(_context())

    assert "11434" not in seen["url"]
    assert seen["url"].endswith("/models/gemini-3.5-flash:generateContent")


def test_판정은_흔들리면_안_된다(monkeypatch: pytest.MonkeyPatch):
    """§6.4 — temperature 0 고정."""
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate(_context())

    assert seen["body"]["generationConfig"]["temperature"] == 0


def test_응답_스키마가_판정_모델과_맞는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 `_GEMINI_RESPONSE_SCHEMA` 를 손으로 적었다 — `JudgeInterpretation` 이
    바뀌면 여기도 바꿔야 한다. **이 검사가 둘을 대조한다.**"""
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate(_context())

    sent = seen["body"]["generationConfig"]["responseSchema"]
    model_schema = JudgeInterpretation.model_json_schema()
    assert set(sent["properties"]) == set(model_schema["properties"])
    assert set(sent["required"]) == set(model_schema["required"])
    assert sent["properties"]["verdict"]["enum"] == model_schema["properties"]["verdict"]["enum"]


def test_키는_헤더로_간다(monkeypatch: pytest.MonkeyPatch):
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate(_context())

    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in seen["url"]
    assert "test-key" not in json.dumps(seen["body"])


def test_되물음이_실린다(monkeypatch: pytest.MonkeyPatch):
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate(_context(), retry_guidance=["숫자를 빼라"])

    sent = json.loads(seen["body"]["contents"][0]["parts"][0]["text"])
    assert sent["correction"] == ["숫자를 빼라"]


# ── 사고 조각 · 오류 ─────────────────────────────────────────────────────


def test_사고_조각을_건너뛴다(monkeypatch: pytest.MonkeyPatch):
    """🔴 마스터에서 이것 때문에 호출이 성공했는데 FALLBACK 으로 떨어졌다.
    판정에서 같은 일이 나면 **검증이 조용히 안 돈다.**"""
    _capture(monkeypatch, parts=[{"thought": True, "text": "생각"}, {"text": _ANSWER}])

    assert GeminiProvider(_settings()).generate(_context()) == _ANSWER


def test_HTTP_오류는_상태_코드를_잃지_않는다(monkeypatch: pytest.MonkeyPatch):
    import urllib.error
    import urllib.request

    monkeypatch.delenv("CRITIC_GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with pytest.raises(urllib.error.HTTPError) as caught:
        GeminiProvider(_settings()).generate(_context())
    assert caught.value.code == 429


def test_키가_없으면_즉시_터진다(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CRITIC_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiProvider(_settings()).generate(_context())


# ── 모델 선택 ────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """`.env` 를 안 읽는 상태에서 환경변수만으로 판정한다 — 안 그러면 결과가
    **개발자 기계의 `.env` 에 따라 달라진다.**"""
    monkeypatch.setattr(runtime, "load_dotenv", lambda *_a, **_k: None)
    for key in ("LLM_PROVIDER", "LLM_MODEL", "CRITIC_LLM_PROVIDER", "CRITIC_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_Critic_만_Gemini_로_가도_전역_모델을_안_물어온다(env: pytest.MonkeyPatch):
    env.setenv("LLM_PROVIDER", "ollama")
    env.setenv("LLM_MODEL", "gemma3:4b")  # 재무·오케가 같이 보는 줄
    env.setenv("CRITIC_LLM_PROVIDER", "gemini")

    settings = runtime.get_llm_settings()

    assert settings.provider == "gemini"
    assert settings.model == "gemini-3.5-flash"


def test_판정_모델은_생성_모델과_다르다(env: pytest.MonkeyPatch):
    """🔴 §6.4 — 같은 모델·같은 논리면 자기가 만든 설명을 자기가 승인한다.

    마스터·물류가 `gemini-3.5-flash-lite` 를 쓰므로 Critic 기본값은 그것이면 안 된다.
    """
    assert runtime._DEFAULT_MODELS["gemini"] != "gemini-3.5-flash-lite"


def test_직접_지정한_모델이_이긴다(env: pytest.MonkeyPatch):
    """지정을 무시하는 것이 더 나쁘다 — provider 를 바꾸며 이 줄을 안 고치면 404 다."""
    env.setenv("LLM_PROVIDER", "ollama")
    env.setenv("CRITIC_LLM_PROVIDER", "gemini")
    env.setenv("CRITIC_LLM_MODEL", "gemini-2.5-pro")

    assert runtime.get_llm_settings().model == "gemini-2.5-pro"


def test_프로바이더가_같으면_전역_모델을_그대로_쓴다(env: pytest.MonkeyPatch):
    env.setenv("LLM_PROVIDER", "ollama")
    env.setenv("CRITIC_LLM_MODEL", "qwen2.5:7b")

    settings = runtime.get_llm_settings()

    assert (settings.provider, settings.model) == ("ollama", "qwen2.5:7b")


def test_모르는_프로바이더는_터뜨린다(env: pytest.MonkeyPatch):
    """조용히 무시하면 **판정이 안 도는데 통과로 보인다.**"""
    env.setenv("CRITIC_LLM_PROVIDER", "openai")

    service = get_judge_service()

    with pytest.raises(RuntimeError, match="not supported"):
        service.provider.generate(_context())
