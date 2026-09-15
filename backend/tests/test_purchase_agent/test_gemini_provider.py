"""네 번째 프로바이더 — **gemini 를 열었을 때 깨지는 자리들**.

🔴 이 파일이 재는 것은 「gemini 가 좋은 답을 내나」가 아니다. 그건 키가 있는 환경에서
따로 잰다. 여기서 재는 것은 **열었기 때문에 생긴 길**이다.

```text
① 스키마 변환   Gemini 는 JSON Schema 를 그대로 못 먹는다 — 특히 `$ref`
② 응답 읽기     첫 조각이 text 가 아닐 수 있다 (사고 조각)
③ 키 없음       터지되 **조립은 안 죽는다** — fallback 으로 가야 한다
④ 오류 보존     HTTPError 를 감싸면 429(한도)와 서버 죽음이 같아 보인다
```

★ ①이 이 판에서 **처음 생긴 자리**다. 팀의 다른 파트는 전부 평면 스키마라 `$ref` 를
다룬 적이 없고, 우리 ⑧ ``ReviewOutput`` 만 그것을 쓴다 — 그대로 보내면 **⑧ 을 켠 날에야
처음 터진다.**
"""

import json
import urllib.error

import pytest

from app.purchase_agent.llm import runtime as rt
from app.purchase_agent.llm.review_schemas import ReviewOutput
from app.purchase_agent.llm.schemas import GradeMixInterpretation
from app.purchase_agent.llm.split_schemas import SplitAllocationChoice


@pytest.fixture(autouse=True)
def 망을_막는다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **이 파일의 어떤 검사도 밖으로 나가지 않는다.**

    ⚠️ 실제로 한 번 나갔다. ``monkeypatch.delenv`` 로 키를 지워도 ``get_llm_settings()``
    가 ``load_dotenv()`` 를 부르면서 **``.env`` 의 키를 다시 넣는다** — 그래서 "키가
    없을 때" 를 재려던 검사가 진짜 호출을 보내고 404 를 받았다. 검사가 팀 공용 한도를
    깎는 것은 **어떤 이유로도 안 된다.**

    ★ 개별 검사가 자기 가짜를 덮어써도 된다 — 나중에 건 것이 이긴다.
    """

    def 막힘(*args, **kwargs):
        raise AssertionError("검사가 망으로 나가려 했다")

    monkeypatch.setattr("urllib.request.urlopen", 막힘)
    return monkeypatch


#: 세 역할이 실제로 내보내는 응답 계약. 🔴 **목록을 손으로 적지 않는다** 대신 세 모델을
#: 직접 든다 — 역할이 늘면 여기서 걸리라고 둔 자리다.
역할_스키마 = {
    "⑤ mix": GradeMixInterpretation,
    "④ split": SplitAllocationChoice,
    "⑧ review": ReviewOutput,
}

#: Gemini ``responseSchema`` 가 거부하거나 무시하는 키. 남아 있으면 400 이 나고, 그 400 은
#: fallback 에 삼켜져 **「모델이 실패했다」로 읽힌다.**
금지_키 = {
    "$ref",
    "$defs",
    "$schema",
    "additionalProperties",
    "title",
    "default",
    "examples",
    "minLength",
    "maxLength",
}


def _키를_훑는다(node, 찾을_키):
    """중첩 어디에 있든 찾는다 — 최상위만 보면 ``items`` 안에 남은 것을 놓친다."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in 찾을_키:
                yield key
            yield from _키를_훑는다(value, 찾을_키)
    elif isinstance(node, list):
        for item in node:
            yield from _키를_훑는다(item, 찾을_키)


@pytest.mark.parametrize("이름", sorted(역할_스키마))
def test_세_역할_스키마가_전부_gemini_모양으로_낮춰진다(이름: str) -> None:
    변환 = rt._to_gemini_schema(역할_스키마[이름].model_json_schema())
    남은 = sorted(set(_키를_훑는다(변환, 금지_키)))
    assert not 남은, f"{이름} 에 Gemini 가 못 먹는 키가 남았다: {남은}"


def test_참조가_실제로_펼쳐진다() -> None:
    """🔴 **키가 사라진 것과 내용이 펼쳐진 것은 다르다.**

    ``$ref`` 를 그냥 버려도 위 검사는 통과한다 — 금지 키가 없어지니까. 그러면 스키마가
    ``{}`` 로 비고 모델은 아무 모양이나 내도 된다. 그래서 **들어 있어야 할 필드가 그
    자리에 있는지**를 따로 본다.
    """
    변환 = rt._to_gemini_schema(ReviewOutput.model_json_schema())
    항목 = 변환["properties"]["findings"]["items"]
    필드 = set(항목.get("properties") or {})
    assert 항목.get("type") == "object"
    assert 필드 == {"code", "target_ref_id"}, 필드
    assert 항목.get("required") == ["code"]


def test_널_허용은_nullable_로_바뀐다() -> None:
    """``anyOf[str, null]`` 은 Gemini 의 표현이 아니다 — 그쪽 말로 옮긴다."""
    항목 = rt._to_gemini_schema(ReviewOutput.model_json_schema())["properties"]["findings"]["items"]
    대상 = 항목["properties"]["target_ref_id"]
    assert 대상["type"] == "string"
    assert 대상["nullable"] is True


def test_설명은_남는다() -> None:
    """🔴 **provider 를 바꾼 것만으로 모델에게 보이는 지시가 달라지면 안 된다.**

    Ollama 에는 스키마를 통째로 넘기고 있어 모델이 클래스 docstring 을 이미 보고 있다.
    여기서 ``description`` 을 빼면 판단이 달라져도 그게 모델 탓인지 우리 탓인지 못 가른다.
    """
    원본 = ReviewOutput.model_json_schema()
    변환 = rt._to_gemini_schema(원본)
    assert list(_키를_훑는다(변환, {"description"})) == list(_키를_훑는다(원본, {"description"}))


def test_못_푸는_참조는_조용히_넘어가지_않는다() -> None:
    """정의가 없는 ``$ref`` 를 빈 dict 로 두면 **아무 모양이나 받는 스키마**가 된다."""
    with pytest.raises(KeyError):
        rt._to_gemini_schema({"$ref": "#/$defs/없는것"})


def test_변환이_같은_입력에_같은_결과를_낸다() -> None:
    """두 번 부르면 같아야 한다 — ``$defs`` 를 집는 자리가 입력을 고치면 어긋난다."""
    원본 = ReviewOutput.model_json_schema()
    첫판 = json.dumps(rt._to_gemini_schema(원본), sort_keys=True)
    둘째 = json.dumps(rt._to_gemini_schema(원본), sort_keys=True)
    assert 첫판 == 둘째
    # 🔴 원본을 안 건드렸나 — 같은 스키마 객체를 다른 프로바이더도 읽는다.
    assert "$defs" in 원본


# ── 응답 읽기 ──────────────────────────────────────────────────────────────


def test_사고_조각이_앞에_와도_텍스트를_집는다() -> None:
    """🔴 ``parts[0]`` 을 읽으면 **호출은 성공했는데 FALLBACK** 으로 떨어진다.

    마스터 실측에서 12번 중 11번이 이 모양으로 죽었다.
    """
    문서 = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True},
                        {"text": "   "},
                        {"text": '{"chosen_candidate_id": "BASE_ONLY", "reason": "기본"}'},
                    ]
                }
            }
        ]
    }
    assert json.loads(rt._gemini_text(문서))["chosen_candidate_id"] == "BASE_ONLY"


def test_텍스트가_하나도_없으면_터진다() -> None:
    """빈 응답을 빈 문자열로 돌려주면 검증이 «스키마 위반» 으로 읽는다 — 사유가 흐려진다."""
    with pytest.raises(TypeError):
        rt._gemini_text({"candidates": [{"content": {"parts": [{"thought": True}]}}]})


# ── 키 · 조립 ──────────────────────────────────────────────────────────────


def test_키가_둘_다_없으면_부를_때_터진다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ **조립이 아니라 호출에서** 터져야 ``run_with_fallback`` 이 받는다.

    🔴 **설정을 먼저 만들고 키를 지운다.** ``get_llm_settings()`` 가 ``load_dotenv()`` 를
    부르므로, 지운 뒤에 부르면 ``.env`` 의 키가 **되살아난다** — 실제로 그렇게 밟았다.
    """
    settings = rt.get_llm_settings()  # 🔴 먼저 — 이 안에서 .env 를 읽는다
    monkeypatch.delenv(f"{rt.ENV_PREFIX}GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = rt.GeminiProvider(settings)  # 🟢 여기서는 안 터진다
    with pytest.raises(RuntimeError) as 터짐:
        provider.generate(_컨텍스트())
    # 🔴 어느 이름을 봤는지 알려 준다 — 「키가 없다」만으로는 뭘 넣을지 모른다.
    assert f"{rt.ENV_PREFIX}GEMINI_API_KEY" in str(터짐.value)
    assert "GEMINI_API_KEY" in str(터짐.value)


def test_키가_없어도_그래프_조립은_선다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 미지원 provider 에서 ``build_graph()`` 가 죽던 것과 **같은 결**이다."""
    monkeypatch.delenv(f"{rt.ENV_PREFIX}GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv(f"{rt.ENV_PREFIX}LLM_PROVIDER", "gemini")
    from app.purchase_agent.graph import build_graph

    assert build_graph() is not None
    assert isinstance(rt.build_provider(rt.get_llm_settings()), rt.GeminiProvider)


def test_전용키가_공용키보다_먼저다(monkeypatch: pytest.MonkeyPatch) -> None:
    """팀 관례 — ``PURCHASE_GEMINI_API_KEY`` → ``GEMINI_API_KEY``.

    🔴 **값을 비교하지 않는다.** 어느 쪽을 집었는지는 보낸 헤더로만 알 수 있고, 그것을
    검사에 찍으면 키가 로그에 남는다. 대신 **공용만 있을 때도 안 터진다**로 방향을 잰다.
    """
    보낸다 = {}

    def 가짜(request, timeout=None):
        보낸다["키있음"] = bool(request.headers.get("X-goog-api-key"))
        raise urllib.error.URLError("stop")

    monkeypatch.setattr("urllib.request.urlopen", 가짜)
    monkeypatch.delenv(f"{rt.ENV_PREFIX}GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    with pytest.raises(RuntimeError):
        rt.GeminiProvider(rt.get_llm_settings()).generate(_컨텍스트())
    assert 보낸다["키있음"] is True


def test_HTTPError_는_감싸지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 감싸면 **상태 코드가 사라진다** — 429(한도)와 서버 죽음이 같아 보인다."""

    def 가짜(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "quota", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", 가짜)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    with pytest.raises(urllib.error.HTTPError) as 터짐:
        rt.GeminiProvider(rt.get_llm_settings()).generate(_컨텍스트())
    assert 터짐.value.code == 429


def test_주소가_ollama_기본값을_쓰지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 ``LLM_BASE_URL`` 기본이 ollama 라, 거기서 읽으면 **로컬 포트로 쏜다.**"""
    주소 = {}

    def 가짜(request, timeout=None):
        주소["url"] = request.full_url
        raise urllib.error.URLError("stop")

    monkeypatch.setattr("urllib.request.urlopen", 가짜)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv(f"{rt.ENV_PREFIX}LLM_BASE_URL", "http://127.0.0.1:11434")
    with pytest.raises(RuntimeError):
        rt.GeminiProvider(rt.get_llm_settings()).generate(_컨텍스트())
    assert "127.0.0.1" not in 주소["url"]
    assert 주소["url"].startswith(rt._GEMINI_BASE_URL)


def _컨텍스트():
    from app.purchase_agent.llm.mix import build_mix_context
    from app.purchase_agent.llm.schemas import MixCandidate

    return build_mix_context(
        "배추",
        spread_widened=True,
        shelf_days=6.0,
        shelf_tight=True,
        signals=["GRADE_SPREAD_WIDENED"],
        facts=["등급 스프레드가 평시보다 확대됐다."],
        candidates=[MixCandidate(candidate_id="BASE_ONLY", summary="기본")],
    )
