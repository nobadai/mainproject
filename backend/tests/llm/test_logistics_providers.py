"""물류 Provider 구현체 — **네트워크 없이** 반환 계약과 예외 계약을 잡는다 (#402 · #406).

★ 실 호출을 섞으면 키가 있는 사람만 돌릴 수 있는 검사가 되고, 그러면 아무도 안 돌린다
  (`test_critic_gemini_provider.py` 와 같은 이유 · 같은 fake `urlopen` 패턴).

🔴 **여기서 latency 를 재지 않는다.** 호출 시간의 주인은 `InterpretationService` 다
  (`test_logistics_interpretation_runtime.py`). Provider 안에서 재면 Ollama·Gemini 두 벌로
  복제되고 **예외로 끝난 호출의 시간을 잃는다** — 그런데 timeout 이야말로 FALLBACK
  진단에서 가장 알고 싶은 숫자다. 그래서 #406 에서 반환형이 `ProviderResult` 로 바뀐
  뒤에도 `usage` 만 싣고 `elapsed` 는 싣지 않는다.

★ usage 는 반대다 — **HTTP 응답 본문 안에만 있어서** Provider 밖에서는 알 방법이 없다.
  그래서 이 파일이 usage 파싱 계약의 정본이다. 여기서 잠그는 것은 두 가지다:
  ① 공식 필드명에서 정확한 숫자를 읽는가 ② 그 숫자가 이상할 때 **업무를 망가뜨리지
  않고** `None` 이 되는가.

★ Provider 에는 호출 상태를 남기지 않는다 (`last_latency` · `last_usage` 등). 그 계약은
  구조 검사로 `test_logistics_interpretation_runtime.py::test_provider_keeps_no_call_state`
  가 잠근다.
"""

from __future__ import annotations

import dataclasses
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
    ProviderResult,
    ProviderUsage,
    UnavailableProvider,
)
from app.logistics.llm.schemas import ContextFact, SanitizedLLMContext

#: 🔴 input 과 output 에 **서로 다른 숫자**를 쓴다. 같은 값이면 두 필드를 뒤바꾼 변이가
#:   모든 검사를 통과한다 (#406 M1 · M2). 자릿수까지 다르게 잡아 눈으로도 구분된다.
_INPUT_TOKENS = 137
_OUTPUT_TOKENS = 24

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


def test_ollama_returns_the_message_content_without_usage(monkeypatch):
    """usage 없는 응답도 **정상 성공**이다 — 두 count 모두 공식 계약상 Optional 이다."""
    seen = _fake_urlopen(monkeypatch, {"message": {"content": _ANSWER}})

    output = OllamaProvider(_settings()).generate(_context())

    assert isinstance(output, ProviderResult), "Provider 반환형은 ProviderResult 다 (#406)"
    assert output.text == _ANSWER
    assert output.usage is None, "인식 가능한 usage 가 없으면 usage 객체 자체가 None 이다"
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["body"]["model"] == "fake-model"
    # #402 계약 유지 — 요청 형태는 그대로다 (usage 를 얻으려고 stream 을 켜지 않았다)
    assert seen["body"]["stream"] is False


def test_ollama_reads_the_official_prompt_and_eval_counts(monkeypatch):
    """공식 `/api/chat` 응답의 두 필드만 읽는다 — duration 계열은 가져오지 않는다."""
    _fake_urlopen(
        monkeypatch,
        {
            "message": {"content": _ANSWER},
            "done": True,
            "prompt_eval_count": _INPUT_TOKENS,
            "eval_count": _OUTPUT_TOKENS,
            # 아래는 토큰이 아니라 서버측 시간(ns)이다 — 읽히면 안 된다 (#406)
            "total_duration": 5191566416,
            "load_duration": 2154458,
            "prompt_eval_duration": 383809000,
            "eval_duration": 4799921000,
        },
    )

    output = OllamaProvider(_settings()).generate(_context())

    assert output.text == _ANSWER
    assert output.usage == ProviderUsage(input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS)
    # 🔴 뒤바꿈 방어 — 두 값이 다르므로 서로 바뀌면 여기서 즉시 드러난다 (M2)
    assert output.usage.input_tokens == _INPUT_TOKENS
    assert output.usage.output_tokens == _OUTPUT_TOKENS


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            {"prompt_eval_count": _INPUT_TOKENS},
            ProviderUsage(input_tokens=_INPUT_TOKENS, output_tokens=None),
            id="input만",
        ),
        pytest.param(
            {"eval_count": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="output만",
        ),
        pytest.param(
            {"prompt_eval_count": 0, "eval_count": 0},
            ProviderUsage(input_tokens=0, output_tokens=0),
            id="명시적_0",
        ),
        pytest.param(
            {"prompt_eval_count": "many", "eval_count": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="문자열은_그_칸만_None",
        ),
        pytest.param(
            {"prompt_eval_count": True, "eval_count": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="bool은_1이_아니라_None",
        ),
        pytest.param(
            {"prompt_eval_count": -5, "eval_count": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="음수는_None",
        ),
        pytest.param(
            {"prompt_eval_count": 12.5, "eval_count": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="float는_반올림하지_않고_None",
        ),
        pytest.param(
            {"prompt_eval_count": None, "eval_count": None},
            None,
            id="null_둘이면_usage_자체가_None",
        ),
        pytest.param(
            {"prompt_eval_count": "many", "eval_count": []},
            None,
            id="인식_가능한_값_0개면_usage_자체가_None",
        ),
        pytest.param({"total_duration": 5191566416}, None, id="duration만_있으면_usage_없음"),
    ],
)
def test_ollama_usage_parsing_never_breaks_the_text(monkeypatch, document, expected):
    """🔴 **usage 가 이상해도 text 는 그대로 성공한다** (#406 M8).

    관측 실패가 업무 결과를 바꾸면, 계측을 붙인 대가로 LLM 가용성이 떨어진다. 이 이슈의
    목적은 observability 이지 새 실패 원인을 만드는 것이 아니다.
    """
    _fake_urlopen(monkeypatch, {"message": {"content": _ANSWER}, **document})

    output = OllamaProvider(_settings()).generate(_context())

    assert output.text == _ANSWER, "usage 파싱은 text 를 건드리지 않는다"
    assert output.usage == expected


def test_ollama_does_not_read_other_usage_fields(monkeypatch):
    """공식 계약에 있어도 **이번 범위 밖인 값**은 읽지 않는다 (#406 §29).

    `prompt_eval_cached_count` 는 Ollama 공식 필드지만 Gemini `cachedContentTokenCount`
    와 세는 대상이 다르다(프리픽스 KV 캐시 재사용 vs 명시적 CachedContent 리소스).
    개념이 다른 둘을 공통 칸에 넣지 않기로 했으므로 아예 읽지 않는다.
    """
    _fake_urlopen(
        monkeypatch,
        {
            "message": {"content": _ANSWER},
            "prompt_eval_count": _INPUT_TOKENS,
            "eval_count": _OUTPUT_TOKENS,
            "prompt_eval_cached_count": 99,
        },
    )

    usage = OllamaProvider(_settings()).generate(_context()).usage

    assert usage == ProviderUsage(input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS)
    assert 99 not in dataclasses.astuple(usage), "cached count 는 어느 칸에도 들어가지 않는다"


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


def _gemini_document(**extra: Any) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": _ANSWER}]}}], **extra}


def test_gemini_returns_the_candidate_text_without_usage(monkeypatch):
    """`usageMetadata` 는 공식 레퍼런스상 **Optional** — 없어도 성공이다."""
    _pin_key(monkeypatch)
    monkeypatch.delenv("LOGISTICS_GEMINI_BASE_URL", raising=False)
    seen = _fake_urlopen(monkeypatch, _gemini_document())

    output = GeminiProvider(_settings(provider="gemini", model="gemini-fake")).generate(_context())

    assert isinstance(output, ProviderResult)
    assert output.text == _ANSWER
    assert output.usage is None
    assert seen["url"].endswith("/models/gemini-fake:generateContent")
    # 🔴 Ollama 기본 base_url 은 쓰이지 않는다 — 두 provider 의 주소는 다른 축이다
    assert "11434" not in seen["url"]


def test_gemini_reads_the_official_prompt_and_candidates_token_counts(monkeypatch):
    """공식 `usageMetadata` 두 칸만 읽는다 — total·cached·thoughts 는 범위 밖이다."""
    _pin_key(monkeypatch)
    monkeypatch.delenv("LOGISTICS_GEMINI_BASE_URL", raising=False)
    _fake_urlopen(
        monkeypatch,
        _gemini_document(
            usageMetadata={
                "promptTokenCount": _INPUT_TOKENS,
                "candidatesTokenCount": _OUTPUT_TOKENS,
                # 🔴 아래는 읽지 않는다. totalTokenCount 는 prompt + thoughts +
                #    candidates 라 input+output 과 정의가 다르고, Ollama 에는 대응
                #    필드가 없다 — 공통 칸을 만들면 두 정의가 한 컬럼에 산다.
                "totalTokenCount": 999,
                "cachedContentTokenCount": 77,
                "thoughtsTokenCount": 55,
            }
        ),
    )

    output = GeminiProvider(_settings(provider="gemini")).generate(_context())

    assert output.text == _ANSWER
    assert output.usage == ProviderUsage(input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS)
    # 🔴 뒤바꿈 방어 (M1) · 범위 밖 값이 어느 칸에도 새지 않았음
    assert output.usage.input_tokens == _INPUT_TOKENS
    assert output.usage.output_tokens == _OUTPUT_TOKENS
    assert {999, 77, 55}.isdisjoint(set(dataclasses.astuple(output.usage)))


@pytest.mark.parametrize(
    ("usage_metadata", "expected"),
    [
        pytest.param(
            {"promptTokenCount": _INPUT_TOKENS},
            ProviderUsage(input_tokens=_INPUT_TOKENS, output_tokens=None),
            id="input만",
        ),
        pytest.param(
            {"candidatesTokenCount": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="output만",
        ),
        pytest.param(
            {"promptTokenCount": 0, "candidatesTokenCount": 0},
            ProviderUsage(input_tokens=0, output_tokens=0),
            id="명시적_0",
        ),
        pytest.param(
            {"promptTokenCount": "137", "candidatesTokenCount": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="문자열은_그_칸만_None",
        ),
        pytest.param(
            {"promptTokenCount": True, "candidatesTokenCount": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="bool은_1이_아니라_None",
        ),
        pytest.param(
            {"promptTokenCount": -1, "candidatesTokenCount": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="음수는_None",
        ),
        pytest.param(
            {"promptTokenCount": 137.0, "candidatesTokenCount": _OUTPUT_TOKENS},
            ProviderUsage(input_tokens=None, output_tokens=_OUTPUT_TOKENS),
            id="float는_반올림하지_않고_None",
        ),
        pytest.param({}, None, id="빈_usageMetadata"),
        pytest.param(
            {"totalTokenCount": 999, "thoughtsTokenCount": 55},
            None,
            id="범위_밖_필드만_있으면_usage_없음",
        ),
        pytest.param(
            {"promptTokenCount": {"value": 1}, "candidatesTokenCount": [24]},
            None,
            id="인식_가능한_값_0개면_usage_자체가_None",
        ),
        pytest.param("not-an-object", None, id="usageMetadata가_객체가_아님"),
        pytest.param(None, None, id="usageMetadata가_null"),
    ],
)
def test_gemini_usage_parsing_never_breaks_the_text(monkeypatch, usage_metadata, expected):
    """🔴 usage 계측 실패로 FALLBACK 하지 않는다 (#406 M8)."""
    _pin_key(monkeypatch)
    monkeypatch.delenv("LOGISTICS_GEMINI_BASE_URL", raising=False)
    _fake_urlopen(monkeypatch, _gemini_document(usageMetadata=usage_metadata))

    output = GeminiProvider(_settings(provider="gemini")).generate(_context())

    assert output.text == _ANSWER, "usage 파싱은 text 를 건드리지 않는다"
    assert output.usage == expected


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
    """미지원 provider 이름의 자리 — 다시 불러도 같으므로 즉시 FALLBACK 이다.

    ★ **빈 `ProviderResult` 를 내는 정상 경로를 만들지 않았다** (#406). HTTP 응답을
      얻지 못한 호출에 usage 는 없고, 빈 결과를 돌려주면 *"불렀는데 usage 가 없었다"*
      로 읽힌다 — 사실은 **부르지도 못한 것**이다.
    """
    with pytest.raises(ProviderConfigurationError):
        UnavailableProvider().generate(_context())


def test_unavailable_provider_opens_no_socket(monkeypatch):
    def boom(*_: object, **__: object) -> None:
        raise AssertionError("UnavailableProvider 가 네트워크를 열었다")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with pytest.raises(ProviderConfigurationError):
        UnavailableProvider().generate(_context())


# ── 전송 계약의 구조 ─────────────────────────────────────────────────────


def test_provider_result_carries_only_text_and_usage():
    """🔴 **raw 응답이 들어올 자리를 구조로 막는다** (#406 M7).

    `raw_response` · `response_json` · `full_provider_payload` 같은 칸이 하나라도 생기면
    completion text · prompt metadata · Provider 식별자가 실행이력까지 따라 나갈 길이
    열린다. 필요한 숫자는 Provider 안에서 즉시 뽑고 문서는 그 자리에서 버린다.
    """
    assert {field.name for field in dataclasses.fields(ProviderResult)} == {"text", "usage"}
    금지 = {"raw_response", "response_json", "full_provider_payload", "document", "headers"}
    assert 금지 & set(dir(ProviderResult)) == set()


def test_provider_usage_carries_only_the_two_common_axes():
    """공식 계약에서 **의미가 같다고 확인된 두 축**뿐이다 (#406 §29).

    total 을 넣지 않는 것이 계약이다 — Ollama `/api/chat` 에 공식 combined total 이 없고,
    Gemini `totalTokenCount` 는 prompt + thoughts + candidates 라 `input + output` 과
    뜻이 다르다. cached · thoughts · duration 도 같은 이유로 자리가 없다.
    """
    assert {field.name for field in dataclasses.fields(ProviderUsage)} == {
        "input_tokens",
        "output_tokens",
    }
    금지 = {"total_tokens", "cached_tokens", "thoughts_tokens", "total_duration"}
    assert 금지 & set(dir(ProviderUsage)) == set()


@pytest.mark.parametrize("model", [ProviderResult, ProviderUsage])
def test_transport_types_are_frozen(model):
    """값은 호출한 쪽의 지역 변수로만 산다 — 만든 뒤 누가 고쳐 쓸 수 없다."""
    assert model.__dataclass_params__.frozen


def test_provider_usage_never_represents_nothing_as_an_object():
    """🔴 `ProviderUsage(None, None)` 은 파서가 만들지 않는다 (#406 M10).

    "이 호출의 usage 를 못 봤다" 는 사실의 표현은 **`usage is None` 하나뿐**이다. 표현이
    둘이면 누적 규칙(`_add_observed`)과 검사가 둘 다 갈라진다. 아래 두 fixture 는 그
    상태를 만들 수 있는 유일한 입력들이고, 둘 다 `None` 으로 정규화되어야 한다.
    """
    from app.logistics.llm.runtime import _provider_usage

    assert _provider_usage(None, None) is None
    assert _provider_usage("many", -1) is None
    assert _provider_usage(0, None) == ProviderUsage(input_tokens=0, output_tokens=None)
