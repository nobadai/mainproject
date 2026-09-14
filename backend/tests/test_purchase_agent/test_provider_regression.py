"""**모르는 ``LLM_PROVIDER`` 에서도 그래프가 선다** — 기본 경로의 회귀 관문.

🔴 **실제로 깨졌던 자리다.** ④·⑧ 이 프로바이더를 ``(settings, ROLE)`` 로 조립하는데
``UnavailableProvider`` 만 인자를 안 받아, ``LLM_PROVIDER`` 가 오타면 **새 기능이 전부
꺼져 있어도** ``build_graph()`` 가 ``TypeError`` 로 죽었다. 기능이 꺼졌는데 조립이
죽는 것은 fallback 성질의 회귀다.

★ 그래서 이 파일이 재는 것은 셋이다::

    ① 모르는 provider 로도 **조립과 실행이 된다**
    ② 그때 산출물이 **아는 provider 일 때와 같다** — 규칙 기본안이므로
    ③ 상태가 **기존 의미 그대로** 적힌다 (꺼짐은 ``DISABLED`` · 부르고 실패는 ``FALLBACK``)

⚠️ 여기서 네트워크를 타지 않는다. 모르는 provider 는 ``UnavailableProvider`` 로 가고
그것은 부르는 즉시 터진다 — 그 예외를 ``run_with_fallback`` 이 받는다.
"""

from datetime import date

import pytest

from app.purchase_agent.adapter import (
    RATIONALE_SELF_REVIEW,
    SOURCING_SELECTION,
    SPLIT_ALLOCATION_SELECTION,
    _llm_calls,
)
from app.purchase_agent.features import (
    INFORMATION_REQUESTS,
    SELF_REVIEW,
    SPLIT_ALLOCATION,
)
from app.purchase_agent.graph import build_graph, run_purchase_agent
from app.purchase_agent.llm import runtime
from app.purchase_agent.llm.runtime import ENV_PREFIX, build_provider, get_llm_settings
from app.purchase_agent.state import build_initial_state

ITEM = "배추"
AS_OF = date(2026, 8, 21)
#: ⑤ 가 **실제로 불리는** 앵커. 다른 앵커에서는 규칙이 중품을 안 골라 판단자를 안 부른다 —
#: 그러면 ``FALLBACK`` 경로를 하나도 안 밟고 지나간다 (실측으로 확인했다).
MIX_CALLED = date(2026, 9, 11)

#: 어느 표에도 없는 이름. 🔴 값이 아니라 **없다는 사실**이 이 파일의 입력이다.
UNSUPPORTED = "nope"


def 새_기능을_전부_끈다(monkeypatch: pytest.MonkeyPatch) -> None:
    """셋 다 **기본값과 같은 상태**로 명시한다 — 기본이 바뀌어도 이 검사의 전제는 안 바뀐다."""
    for key in (SPLIT_ALLOCATION, SELF_REVIEW, INFORMATION_REQUESTS):
        monkeypatch.setenv(f"{ENV_PREFIX}{key}", "false")
        monkeypatch.setenv(key, "false")


def 모르는_프로바이더로(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}LLM_PROVIDER", UNSUPPORTED)
    monkeypatch.setenv("LLM_PROVIDER", UNSUPPORTED)


def test_모르는_이름은_표에_없다() -> None:
    """전제 확인 — 이 이름이 어느 날 표에 들어오면 이 파일은 아무것도 안 재게 된다."""
    assert UNSUPPORTED not in runtime.PROVIDERS


def test_모르는_프로바이더도_같은_모양으로_조립된다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **조립은 한 자리에서만 한다.** 역할마다 베끼면 이 자리가 또 갈라진다."""
    모르는_프로바이더로(monkeypatch)
    settings = get_llm_settings()
    assert settings.provider == UNSUPPORTED
    for spec in (runtime.MIX_ROLE,):
        assert isinstance(build_provider(settings, spec), runtime.UnavailableProvider)
    # 역할 spec 을 넘기는 쪽도 같은 모양으로 선다 — 그게 깨졌던 자리다.
    from app.purchase_agent.llm.self_review import ROLE as REVIEW_ROLE
    from app.purchase_agent.llm.split_allocation import ROLE as SPLIT_ROLE

    for spec in (SPLIT_ROLE, REVIEW_ROLE):
        assert isinstance(build_provider(settings, spec), runtime.UnavailableProvider)


def test_모르는_프로바이더에서_그래프가_선다(monkeypatch: pytest.MonkeyPatch) -> None:
    """새 기능이 전부 꺼져 있는데 조립이 죽으면 **fallback 성질의 회귀**다."""
    모르는_프로바이더로(monkeypatch)
    새_기능을_전부_끈다(monkeypatch)
    assert build_graph() is not None


def test_모르는_프로바이더에서도_산출물이_같다(monkeypatch: pytest.MonkeyPatch) -> None:
    """② 규칙 산출물 유지 — 판단자가 못 서면 **규칙 기본안**이고, 그건 provider 와 무관하다."""
    새_기능을_전부_끈다(monkeypatch)
    monkeypatch.setenv(f"{ENV_PREFIX}LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    아는_것 = run_purchase_agent(ITEM, AS_OF)

    모르는_프로바이더로(monkeypatch)
    모르는_것 = run_purchase_agent(ITEM, AS_OF)

    assert 모르는_것 == 아는_것


def _final(as_of: date = AS_OF) -> dict:
    """🔴 **invoke 가 돌려준 것**을 본다 — 넣은 dict 를 그대로 읽으면 뒤 노드가 더한 칸이 빠진다."""
    return build_graph().invoke(build_initial_state(ITEM, as_of))


def test_꺼져_있으면_상태가_꺼짐_그대로다(monkeypatch: pytest.MonkeyPatch) -> None:
    """③ ``LLM_ENABLED=false`` 면 provider 가 무엇이든 **부르지 않았다**가 정답이다."""
    모르는_프로바이더로(monkeypatch)
    새_기능을_전부_끈다(monkeypatch)
    기록 = _llm_calls(_final())
    다섯 = [c for c in 기록 if c.role == SOURCING_SELECTION]
    assert len(다섯) == 1
    assert 다섯[0].status == "DISABLED"
    # 🔴 안 불렀으므로 이름을 안 적는다 — 「안 불렀다」와 「빈 이름으로 불렀다」는 다르다.
    assert (다섯[0].provider, 다섯[0].model) == (None, None)
    assert 다섯[0].attempts == 0
    assert all(c.status == "DISABLED" for c in 기록)


def test_켜져_있고_프로바이더가_없으면_떨어진_실행이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """③ 부르고 실패한 것은 ``FALLBACK`` 이다 — 「안 켰다」로 접지 않는다.

    ⚠️ 켜지만 **네트워크를 안 탄다.** 모르는 provider 는 부르는 즉시 터지고, 그 예외를
    골격이 받아 규칙 기본안으로 보낸다.
    """
    모르는_프로바이더로(monkeypatch)
    새_기능을_전부_끈다(monkeypatch)
    monkeypatch.setenv(f"{ENV_PREFIX}LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_ENABLED", "true")

    기록 = _llm_calls(_final(MIX_CALLED))
    호출 = {c.role: c for c in 기록}
    다섯 = 호출[SOURCING_SELECTION]
    assert 다섯.status == "FALLBACK"
    assert 다섯.fallback_used is True
    assert 다섯.attempts >= 1
    # ④ 는 기능이 꺼져 있고 분할 판단 자체가 없었으니 줄을 안 남긴다.
    assert SPLIT_ALLOCATION_SELECTION not in 호출
    # ⑧ 은 꺼져 있어도 **안마다 한 줄**을 남긴다 — 안 남기면 「봤는데 깨끗했다」와 같아진다.
    검토 = [c for c in 기록 if c.role == RATIONALE_SELF_REVIEW]
    assert 검토 and all(c.status == "DISABLED" for c in 검토)


def test_켜도_업무값은_규칙_기본안_그대로다(monkeypatch: pytest.MonkeyPatch) -> None:
    """부르고 전부 떨어져도 **업무값이 붙이기 전과 같다** — 회귀가 아니라 무변화다.

    🔴 **``risks`` 만은 다르다 — 그게 정상이다.** ⑥ 이 *"판단자 응답 실패"* 를 고지하기
    때문이고, 그 고지가 없으면 메타데이터는 ``FALLBACK`` 인데 화면은 아무 말이 없어
    **두 값이 서로를 부정한다.** 그래서 「전부 같다」가 아니라 「``risks`` 를 뺀 전부가
    같다」로 잰다 (실측으로 다른 칸이 하나도 없음을 확인했다).
    """
    새_기능을_전부_끈다(monkeypatch)
    꺼짐 = run_purchase_agent(ITEM, MIX_CALLED)

    모르는_프로바이더로(monkeypatch)
    monkeypatch.setenv(f"{ENV_PREFIX}LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_ENABLED", "true")
    떨어짐 = run_purchase_agent(ITEM, MIX_CALLED)

    assert _risks를_뺀다(떨어짐) == _risks를_뺀다(꺼짐)
    # 고지는 **늘어난다** — 줄어들면 「떨어졌는데 아무 말도 안 한」 것이다.
    for 전, 후 in zip(꺼짐["scenarios"], 떨어짐["scenarios"], strict=True):
        assert len(후["risks"]) >= len(전["risks"])


def _risks를_뺀다(proposal: dict) -> dict:
    return {
        **proposal,
        "scenarios": [
            {k: v for k, v in 안.items() if k != "risks"} for 안 in proposal["scenarios"]
        ],
    }
