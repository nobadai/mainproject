"""테스트는 **어떤 환경에서도 실 LLM 프로바이더를 타지 않는다** (E3-2).

키를 가진 개발자 머신에서 ``pytest``를 돌리면 ⑤의 기본 선택자가 실 API를 호출하게 된다 —
느리고, 비결정적이고, 돈이 든다. 그래서 ``PURCHASE_LLM_ENABLED=false``를 **세션 전체에**
강제한다. 그러면 ``MixSelectionService``가 ``DISABLED`` 상태로 규칙 기본안을 돌려주고,
호출 자체가 일어나지 않는다.

🔵 **막을 자리가 셋으로 늘었다** (2026-09-16). 이 파일을 쓸 때는 LLM을 쓰는 역할이 ⑤
하나여서 ``PURCHASE_LLM_ENABLED`` 한 칸이 전부를 덮었다. 그 뒤 ④·⑧이 생기면서
(``E3-9``·``E3-10``·``E3-11``) 역할마다 **자기 기능 플래그**가 붙었고, 그 플래그들은 위
스위치와 **독립**이다 — ``PURCHASE_LLM_ENABLED=false`` 로도 안 꺼진다.

⚠️ **그래서 위 머리말이 한동안 거짓이었다.** *"어떤 환경에서도"* 라고 적혀 있는데 실제로
덮는 범위는 ⑤ 하나였다. ``.env`` 에 ``PURCHASE_LLM_SELF_REVIEW_ENABLED=true`` 를 넣자
``test_execution_metadata_carries_the_real_llm_state`` 가 깨지면서 드러났다 — ⑤는 꺼졌는데
⑧이 켜져 있어 ``summarize_llm_calls`` 가 「전부 DISABLED」가 아니라고 답한 것이다
(``SKIPPED_TEMPLATE`` = *"켜져 있는데 호출 조건이 아님"*). 🔴 **제품이 아니라 이 파일이
낡은 것이었다.**

🔵 **gemini 키도 비운다** (2026-09-16). 네 번째 프로바이더가 `#693`으로 들어왔는데 아래
키 비우기 목록은 그 전에 쓰였다. 키를 가진 머신에서 provider만 바뀌면 이 파일이 막으려던
바로 그 사고가 **gemini 경로로** 다시 열린다.

``@pytest.mark.llm`` 테스트만 이 강제를 풀고 실 프로바이더를 쓴다. 그 마커는
``pyproject.toml``의 ``addopts = -m 'not llm'``이 기본 실행에서 제외한다 — 실행하려면
``uv run pytest -m llm``으로 명시해야 한다.
"""

import os

import pytest

from app.purchase_agent.features import (
    INFORMATION_REQUESTS,
    SELF_REVIEW,
    SPLIT_ALLOCATION,
)
from app.purchase_agent.llm.runtime import ENV_PREFIX

_LLM_ENV_KEYS = ("PURCHASE_LLM_ENABLED", "LLM_ENABLED")

#: 역할별 기능 플래그 — 위 스위치와 **독립**이라 따로 꺼야 한다.
_FEATURE_KEYS = (SPLIT_ALLOCATION, SELF_REVIEW, INFORMATION_REQUESTS)

#: 프로바이더가 ``if not api_key`` 로 막는 키들. 🔴 gemini는 **전용 → 공용** 순서로
#: 떨어지므로 (``runtime.py``) 둘 다 비워야 한다 — 하나만 비우면 다른 하나로 샌다.
_API_KEY_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    f"{ENV_PREFIX}GEMINI_API_KEY",
)

#: ``pytest_configure``가 덮기 **전**의 값. ``llm`` 마커 테스트가 되살릴 때 쓴다.
_원래값: dict[str, str | None] = {}


def _덮을_것() -> dict[str, str]:
    """이름 → 덮어쓸 값. 🔴 키는 ``""``, 스위치·플래그는 ``"false"`` 다 (아래 참조)."""
    덮는다 = {key: "false" for key in _LLM_ENV_KEYS}
    for key in _FEATURE_KEYS:
        덮는다[f"{ENV_PREFIX}{key}"] = "false"
        덮는다[key] = "false"
    덮는다.update({key: "" for key in _API_KEY_ENV_KEYS})
    return 덮는다


def pytest_configure(config: pytest.Config) -> None:
    """🔵 **수집보다 먼저 덮는다** (2026-09-16).

    아래 fixture 는 ``monkeypatch`` 를 쓰므로 **함수 스코프**다. 그런데 이 폴더에는
    ``scope="module"`` fixture 가 여섯 있고(``test_graph`` · ``test_context`` ·
    ``test_split`` · ``test_sourcing`` · ``test_lookahead``), 넓은 스코프가 **먼저**
    선다 — 그 안에서는 env 가 아직 ``.env`` 날것이다.

    🔴 **그래서 실제로 새고 있었다.** 소켓 층에서 세어 보니 이 폴더가 실 gemini 로
    **48회** 나갔고, 나간 검사 여섯이 그 모듈 fixture 여섯과 정확히 같았다. 함수 스코프
    fixture 만으로는 못 막는다 — 막는 자리가 **호출보다 뒤**이기 때문이다.

    ⚠️ 이 구멍은 ``#693``(네 번째 프로바이더 ``gemini``) 전에는 **키가 없어서** 안
    보였다. 그때는 이 목록이 ``ANTHROPIC``·``OPENAI`` 뿐이었고 그 둘은 ``.env`` 에도
    없어서, 모듈 fixture 가 켜진 설정으로 들어가도 프로바이더가 키에서 죽었다.
    """
    for key, value in _덮을_것().items():
        _원래값[key] = os.environ.get(key)
        os.environ[key] = value


def pytest_unconfigure(config: pytest.Config) -> None:
    """세션이 끝나면 되돌린다 — 덮은 것은 이 프로세스 안에서만 살아야 한다."""
    for key, value in _원래값.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _원래값.clear()


@pytest.fixture(autouse=True)
def _disable_llm_by_default(request, monkeypatch):
    """``llm`` 마커가 없는 모든 테스트에서 LLM을 끈다.

    ``autouse``라 테스트가 잊어버릴 수 없다 — 새 테스트가 추가돼도 자동으로 걸린다.
    ``monkeypatch``라 세션이 끝나면 원래 환경으로 돌아간다.

    🔵 **``llm`` 마커는 되살린다** (2026-09-16). ``pytest_configure`` 가 세션 전체를
    덮어 버리므로, 예전처럼 «그냥 지나간다» 로는 실 프로바이더 테스트가 **키 없이**
    돌게 된다. 덮기 전 값을 도로 넣어 준다 — 🔴 그 값이 원래 «미설정» 이었다면
    ``delenv`` 로 다시 미설정으로 만든다. 빈 문자열로 두면 ``.env`` 의 진짜 키가
    ``load_dotenv`` 에 안 실려 «키 없음» 이 되기 때문이다.
    """
    if request.node.get_closest_marker("llm"):
        for key, value in _원래값.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        return
    for key in _LLM_ENV_KEYS:
        monkeypatch.setenv(key, "false")
    # 역할별 기능 플래그도 끈다 (④ 회차 배분 · ⑧ 근거 자기 검토 · 구조화 요청).
    #
    # 🔴 **빈 문자열이 아니라 ``"false"`` 다.** ``features.enabled`` 가
    # ``os.getenv(PREFIX + key) or os.getenv(key)`` 라 빈 문자열은 falsy여서 접두 없는
    # 쪽으로 떨어진다 — 그러면 ``.env``의 값이 그대로 실린다. 위 키 비우기와 규칙이
    # **반대**인 이유가 이것이다: 저쪽은 "값이 없다"를 만들고 이쪽은 "거짓"을 만든다.
    #
    # ★ 같은 관용구가 ``test_provider_regression.py`` 의 ``새_기능을_전부_끈다`` 에 있다 —
    #   거기서는 한 검사의 전제였고, 여기서는 세션 전체의 전제다.
    for key in _FEATURE_KEYS:
        monkeypatch.setenv(f"{ENV_PREFIX}{key}", "false")
        monkeypatch.setenv(key, "false")
    # 키가 실수로 읽히는 경로도 막는다 — 설정이 켜지는 회귀가 나도 호출까지 가지 않는다.
    #
    # ⚠️ **지우지 않고 빈 문자열로 둔다.** ``get_llm_settings()``가 ``load_dotenv()``를
    # 부르는데, 그건 기본이 ``override=False``라 **이미 설정된** 변수만 안 덮어쓴다.
    # ``delenv``로 지우면 "미설정"이 되어 ``.env``의 진짜 키가 다시 실린다 —
    # 키를 가진 개발자 머신에서 테스트가 실 API를 타는 경로다 (Codex 교차검증).
    # 빈 문자열은 설정된 값이라 덮어쓰이지 않고, 프로바이더의 ``if not api_key``에 걸린다.
    for key in _API_KEY_ENV_KEYS:
        monkeypatch.setenv(key, "")
    assert os.getenv("PURCHASE_LLM_ENABLED") == "false"


# ``no_holdings`` 는 ``_injection.py`` 에 산다 — 주입 도구는 한 파일에 모아 둔다. 픽스처는
# import 로 등록되므로 여기서 이름만 끌어온다 (다른 도구들은 함수라 각 검사가 직접 import
# 한다). 🔴 **재export 를 지우면 21개 검사가 «픽스처를 못 찾는다» 로 한꺼번에 운다.**
from _injection import no_holdings  # noqa: F401
