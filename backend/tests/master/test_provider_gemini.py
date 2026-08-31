"""Gemini 프로바이더 — **네트워크 없이** 스키마 변환과 요청 모양을 잡는다.

실 모델 채점은 `test_intent_llm.py` 가 한다 (`-m llm`). 여기서 보는 것은 그 앞이다 —
**Gemini 로 바꿨을 때 Ollama 와 달라지는 자리 셋.**

```text
① 스키마    Ollama 는 JSON Schema 를 그대로 먹고 Gemini 는 못 먹는다
② 주소      LLM_BASE_URL 기본값이 Ollama 라 그걸 쓰면 로컬로 쏜다
③ 키        헤더로 간다 — 없으면 즉시 터져야 fallback 이 산다
```

★ **네트워크를 안 탄다.** `urlopen` 을 갈아끼워 요청 본문을 들여다본다. 실 호출을
  섞으면 키가 있는 사람만 돌릴 수 있는 테스트가 되고, 그러면 아무도 안 돌린다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.master.llm import runtime
from app.master.llm.answer_runtime import narrative_schema
from app.master.llm.runtime import (
    GeminiProvider,
    LLMSettings,
    _intent_schema,
    _to_gemini_schema,
    build_provider,
    get_llm_settings,
)


def _settings(**over: Any) -> LLMSettings:
    base = {
        "enabled": True,
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
        "base_url": "http://127.0.0.1:11434",  # 🔴 Ollama 기본값 — 안 쓰여야 한다
        "timeout_seconds": 30.0,
        "max_retries": 1,
        "max_output_tokens": 1024,
        "effort": None,
    }
    base.update(over)
    return LLMSettings(**base)


class _FakeResponse:
    def __init__(self, document: dict[str, Any]) -> None:
        self._payload = json.dumps(document).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _capture(monkeypatch: pytest.MonkeyPatch, text: str = '{"summary":"확인되었습니다."}') -> dict:
    """`urlopen` 을 가로채 요청을 담아 둔다.

    🔴 **개발자 기계의 실 키를 지운다.** `MASTER_GEMINI_API_KEY` 가 `.env` 에 있으면
    접두어가 이겨서, 검사가 `GEMINI_API_KEY="test-key"` 를 넣어도 **실 키가 헤더에
    실린다.** 그러면 이 검사는 (ㄱ) 키를 넣은 사람에게만 깨지고 (ㄴ) 실패 출력에
    **진짜 키를 찍는다.** 실제로 한 번 그렇게 깨졌다.
    """
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(
            {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        )

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


# ── ① 스키마 변환 ────────────────────────────────────────────────────────


def test_금지_칸을_버린다():
    """`title`·`default`·`additionalProperties` 는 Gemini 가 거부한다."""
    converted = json.dumps(_to_gemini_schema(_intent_schema()), ensure_ascii=False)
    for banned in ("title", "default", "additionalProperties"):
        assert f'"{banned}"' not in converted


def test_널_유니온은_nullable_로_바뀐다():
    """`item` 은 `anyOf[열거, null]` 이다 — 열거를 잃지 않고 nullable 만 붙어야 한다."""
    item = _to_gemini_schema(_intent_schema())["properties"]["item"]
    assert item["nullable"] is True
    assert item["type"] == "string"
    assert "배추" in item["enum"], "열거를 잃으면 없는 품목을 지어낼 자리가 생긴다"
    assert "anyOf" not in item


def test_설명은_남긴다():
    """🔴 Ollama 는 스키마를 통째로 받아 docstring 을 이미 본다.

    여기서 빼면 **프로바이더를 바꾼 것만으로 모델에게 보이는 지시가 달라진다** —
    분류가 달라져도 모델 탓인지 프롬프트 탓인지 가릴 수 없다.
    """
    assert _to_gemini_schema(_intent_schema()).get("description")


def test_필수_칸이_그대로_넘어간다():
    """`agents`·`item` 을 required 로 올린 것이 변환에서 지워지면 안 된다."""
    assert set(_to_gemini_schema(_intent_schema())["required"]) == {
        "action",
        "agents",
        "item",
        "confidence",
    }


def test_문장_스키마도_바뀐다():
    converted = _to_gemini_schema(narrative_schema())
    assert converted["properties"] == {"summary": {"type": "string"}}
    assert converted["required"] == ["summary"]


def test_모르는_anyOf_는_터뜨린다():
    """조용히 흘리면 Gemini 가 400 을 주는데, 그건 호출 실패로만 보인다."""
    with pytest.raises(TypeError, match="anyOf"):
        _to_gemini_schema({"anyOf": [{"type": "string"}, {"type": "integer"}]})


# ── ② 주소 · 요청 모양 ───────────────────────────────────────────────────


def test_Ollama_주소로_쏘지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 `LLM_BASE_URL` 기본값은 Ollama 다. 그걸 쓰면 provider 만 바꾼 사람이
    **연결 실패**만 보고 원인을 못 찾는다."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("MASTER_GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate("system", "user", narrative_schema())

    assert "11434" not in seen["url"]
    assert seen["url"].startswith("https://generativelanguage.googleapis.com/")
    assert seen["url"].endswith("/models/gemini-3.5-flash-lite:generateContent")


def test_요청_모양(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate("지시문", "발화문", narrative_schema())

    body = seen["body"]
    assert body["system_instruction"]["parts"][0]["text"] == "지시문"
    assert body["contents"][0]["parts"][0]["text"] == "발화문"
    assert body["generationConfig"]["temperature"] == 0
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["required"] == ["summary"]
    assert seen["timeout"] == 30.0


def test_키는_헤더로_간다(monkeypatch: pytest.MonkeyPatch):
    """URL 이나 본문에 키가 실리면 로그·에러 메시지에 남는다."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    seen = _capture(monkeypatch)

    GeminiProvider(_settings()).generate("system", "user", narrative_schema())

    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in seen["url"]
    assert "test-key" not in json.dumps(seen["body"])


def test_마스터_접두어가_전역보다_앞선다(monkeypatch: pytest.MonkeyPatch):
    """물류와 키를 나눠 쓸 수 있어야 한다 — 한 키가 막히면 두 파트가 같이 죽는다."""
    seen = _capture(monkeypatch)  # 실 키를 먼저 지운다
    monkeypatch.setenv("GEMINI_API_KEY", "global")
    monkeypatch.setenv("MASTER_GEMINI_API_KEY", "master")

    GeminiProvider(_settings()).generate("system", "user", narrative_schema())

    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["x-goog-api-key"] == "master"


# ── ③ 키 없음 · 등록 ─────────────────────────────────────────────────────


def test_키가_없으면_즉시_터진다(monkeypatch: pytest.MonkeyPatch):
    """재시도해도 같다. 여기서 터져야 서비스가 fallback 으로 보내고 이력에 남는다."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiProvider(_settings()).generate("system", "user", narrative_schema())


def test_응답에_문장이_없으면_터진다(monkeypatch: pytest.MonkeyPatch):
    """빈 응답을 빈 문장으로 통과시키면 **답이 조용히 사라진다.**"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse({"candidates": []}),
    )
    with pytest.raises(TypeError, match="text content"):
        GeminiProvider(_settings()).generate("system", "user", narrative_schema())


def test_provider_로_gemini_를_고르면_Gemini_가_나온다():
    assert isinstance(build_provider(_settings()), GeminiProvider)


def test_기본_모델이_물류와_같다():
    """두 파트가 다른 모델을 쓰면 '모델이 달라서 그런가' 가 모든 조사에 끼어든다."""
    assert runtime._DEFAULT_MODELS["gemini"] == "gemini-3.5-flash-lite"


# ── ④ 전역 설정과의 충돌 ─────────────────────────────────────────────────
#
# 🔴 `LLM_MODEL` 은 **재무·Critic·오케가 같이 보는 값**이다 (재무는 접두어도 없이
#    `os.getenv("LLM_MODEL")` 로 읽는다). 마스터만 Gemini 로 가려고 그 줄을 지우면
#    재무가 같이 바뀐다. 그래서 **지우지 않고도 되어야 한다.**


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """`.env` 를 안 읽는 상태에서 환경변수만으로 판정한다.

    `load_dotenv` 는 os.environ 에 없는 키를 채우므로, 지운 변수를 다시 살려 낸다 —
    그러면 이 검사들이 **개발자 기계의 `.env` 에 따라 결과가 달라진다.**
    """
    monkeypatch.setattr(runtime, "load_dotenv", lambda *_a, **_k: None)
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "MASTER_LLM_PROVIDER",
        "MASTER_LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_마스터만_Gemini_로_가도_전역_모델을_안_물어온다(env: pytest.MonkeyPatch):
    """**재무의 `LLM_MODEL` 을 지우지 않아도 된다.**

    상속하면 Gemini 에 `gemma3:4b` 를 요청해 404 가 난다 — 물류가 #95 에서 겪은
    사고다. 프로바이더가 다르면 전역 모델을 건너뛴다.
    """
    env.setenv("LLM_PROVIDER", "ollama")
    env.setenv("LLM_MODEL", "gemma3:4b")  # 재무·Critic·오케가 같이 보는 줄
    env.setenv("MASTER_LLM_PROVIDER", "gemini")

    settings = get_llm_settings()

    assert settings.provider == "gemini"
    assert settings.model == "gemini-3.5-flash-lite"
    assert settings.model != "gemma3:4b", "전역 모델을 물어오면 Gemini 가 404 를 준다"


def test_마스터_모델을_직접_주면_그것을_쓴다(env: pytest.MonkeyPatch):
    """건너뛰는 것은 **기본값으로 되돌리는 것**이지 지정을 무시하는 것이 아니다."""
    env.setenv("LLM_PROVIDER", "ollama")
    env.setenv("LLM_MODEL", "gemma3:4b")
    env.setenv("MASTER_LLM_PROVIDER", "gemini")
    env.setenv("MASTER_LLM_MODEL", "gemini-3.5-flash")

    assert get_llm_settings().model == "gemini-3.5-flash"


def test_프로바이더가_같으면_전역_모델을_그대로_쓴다(env: pytest.MonkeyPatch):
    """둘 다 ollama 면 전역 모델은 **정당한 상속**이다 — 여기까지 끊으면 안 된다."""
    env.setenv("LLM_PROVIDER", "ollama")
    env.setenv("LLM_MODEL", "qwen2.5:3b")

    settings = get_llm_settings()

    assert settings.provider == "ollama"
    assert settings.model == "qwen2.5:3b"


def test_마스터_접두어가_없으면_전역_프로바이더를_따른다(env: pytest.MonkeyPatch):
    env.setenv("LLM_PROVIDER", "gemini")

    settings = get_llm_settings()

    assert settings.provider == "gemini"
    assert settings.model == "gemini-3.5-flash-lite"


# ── ⑤ 사고 조각 ─────────────────────────────────────────────────────────


def test_사고_조각을_건너뛰고_답을_찾는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 `parts[0]` 만 보면 **호출이 성공했는데 FALLBACK 으로 떨어진다.**

    `gemini-3.5-flash-lite` 는 생각을 켜고 답하며 `thought: true` 조각이 앞에 붙는다.
    실측에서 `SELECT_SCENARIO` 가 12번 중 11번 이렇게 죽었다 — 승인 마디가 통째로
    안 되는 상황이고, 화면에는 "못 알아들음" 으로 보여 **모델이 틀린 것처럼 읽혔다.**
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "사용자가 안을 고르고 있다"},
                                {"text": '{"summary":"진짜 답"}'},
                            ]
                        }
                    }
                ]
            }
        ),
    )

    got = GeminiProvider(_settings()).generate("system", "user", narrative_schema())

    assert got == '{"summary":"진짜 답"}'


def test_사고_조각만_오면_터진다(monkeypatch: pytest.MonkeyPatch):
    """빈 답을 통과시키면 **답이 조용히 사라진다** — 터져야 fallback 이 산다."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(
            {"candidates": [{"content": {"parts": [{"thought": True, "text": "생각만"}]}}]}
        ),
    )
    with pytest.raises(TypeError, match="text content"):
        GeminiProvider(_settings()).generate("system", "user", narrative_schema())


def test_thoughtSignature_가_붙은_답도_읽는다(monkeypatch: pytest.MonkeyPatch):
    """실 응답은 답 조각에 `thoughtSignature` 를 같이 싣는다 — 그건 사고 조각이 아니다."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": '{"summary":"답"}', "thoughtSignature": "El4KXA=="}
                            ]
                        }
                    }
                ]
            }
        ),
    )

    assert GeminiProvider(_settings()).generate("s", "u", narrative_schema()) == '{"summary":"답"}'


def test_HTTP_오류는_상태_코드를_잃지_않는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 `HTTPError` 는 `URLError` 의 하위다 — 함께 감싸면 **상태 코드가 사라진다.**

    실측에서 429(quota 초과)가 `RuntimeError("Master Gemini request failed")` 로
    덮여, **한도에 걸린 것과 서버가 죽은 것이 로그에서 같아 보였다.**
    """
    import urllib.error
    import urllib.request

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with pytest.raises(urllib.error.HTTPError) as caught:
        GeminiProvider(_settings()).generate("s", "u", narrative_schema())
    assert caught.value.code == 429


def test_연결_실패는_키를_안_싣고_감싼다(monkeypatch: pytest.MonkeyPatch):
    import urllib.error
    import urllib.request

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("MASTER_GEMINI_API_KEY", raising=False)

    def boom(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with pytest.raises(RuntimeError) as caught:
        GeminiProvider(_settings()).generate("s", "u", narrative_schema())
    assert "test-key" not in str(caught.value)
