"""테스트는 **어떤 환경에서도 실 LLM 프로바이더를 타지 않는다** (E3-2).

키를 가진 개발자 머신에서 ``pytest``를 돌리면 ⑤의 기본 선택자가 실 API를 호출하게 된다 —
느리고, 비결정적이고, 돈이 든다. 그래서 ``PURCHASE_LLM_ENABLED=false``를 **세션 전체에**
강제한다. 그러면 ``MixSelectionService``가 ``DISABLED`` 상태로 규칙 기본안을 돌려주고,
호출 자체가 일어나지 않는다.

``@pytest.mark.llm`` 테스트만 이 강제를 풀고 실 프로바이더를 쓴다. 그 마커는
``pyproject.toml``의 ``addopts = -m 'not llm'``이 기본 실행에서 제외한다 — 실행하려면
``uv run pytest -m llm``으로 명시해야 한다.
"""

import os

import pytest

_LLM_ENV_KEYS = ("PURCHASE_LLM_ENABLED", "LLM_ENABLED")


@pytest.fixture(autouse=True)
def _disable_llm_by_default(request, monkeypatch):
    """``llm`` 마커가 없는 모든 테스트에서 LLM을 끈다.

    ``autouse``라 테스트가 잊어버릴 수 없다 — 새 테스트가 추가돼도 자동으로 걸린다.
    ``monkeypatch``라 세션이 끝나면 원래 환경으로 돌아간다.
    """
    if request.node.get_closest_marker("llm"):
        return
    for key in _LLM_ENV_KEYS:
        monkeypatch.setenv(key, "false")
    # 키가 실수로 읽히는 경로도 막는다 — 설정이 켜지는 회귀가 나도 호출까지 가지 않는다.
    #
    # ⚠️ **지우지 않고 빈 문자열로 둔다.** ``get_llm_settings()``가 ``load_dotenv()``를
    # 부르는데, 그건 기본이 ``override=False``라 **이미 설정된** 변수만 안 덮어쓴다.
    # ``delenv``로 지우면 "미설정"이 되어 ``.env``의 진짜 키가 다시 실린다 —
    # 키를 가진 개발자 머신에서 테스트가 실 API를 타는 경로다 (Codex 교차검증).
    # 빈 문자열은 설정된 값이라 덮어쓰이지 않고, 프로바이더의 ``if not api_key``에 걸린다.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "")
    assert os.getenv("PURCHASE_LLM_ENABLED") == "false"
