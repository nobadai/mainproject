"""검사가 **판정 입력을 직접 주입**하는 두 도구 (현서님 §1.4 ②③).

원래 이 스위트는 임계와 상황을 ``constraints.yaml``·mock 밴드에서 **간접적으로** 얻었다.
그래서 ``ci_width_threshold`` 하나를 흔들면 25건이 무너졌는데, 그 25건 중 임계를 재려던
검사는 몇 건뿐이고 나머지는 *"uncertain 인 날 문서를 읽는가"*처럼 임계와 상관없는 것을
재고 있었다. 판정 입력이 데이터에 묻혀 있어서 생긴 결합이다.

여기 둘은 그 입력을 **검사가 직접 준다**:

``swap_threshold``   ② 임계를 주입한다 — "mock 분포가 임계를 넘나"가 아니라
                        "임계보다 크면 uncertain, 작으면 stable 로 갈리나"를 재게 한다
``force_situation``  ③ 상황을 주입한다 — ② 컨텍스트 루프 검증이 임계·mock 밴드와
                        무관해진다

⚠️ **mock 을 대체하지 않는다.** ``mocks/`` 의 예측·시세는 그대로 쓰고 ``ci_band`` 도 안
건드린다 (현서님 ④: *"넓히는 게 아니라 떼는 것이 답"*). 밴드의 ``upper`` 는 ``ci_width``
말고 ``compute_max_price`` 도 먹이므로, 넓히면 ⑦ ``check_max_price`` 컷 기준이 함께
풀린다 — 실측으로 전폭 0.5 면 상한이 +17.9% 오르고 여유가 15%→30% 가 된다.
"""

from collections.abc import Callable
from typing import Any

import pytest

from app.purchase_agent import graph
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.classify_situation import compute_allowed_axes

#: ② 검사가 주입하는 임계. mock 의 두 층위(stable ≈0.060 / uncertain ≈0.120) **사이**면
#: 아무 값이나 되고, 그 사이가 비어 있다는 사실은
#: ``test_mocks.test_the_two_mock_bands_never_overlap`` 이 따로 잠근다.
#:
#: 🔴 **선언값(현재 0.08)과 일부러 다른 값을 쓴다.** 같은 값이면 주입이 먹었는지 안 먹었는지
#:   구분할 수 없다 — 패치를 통째로 지워도 검사가 그대로 통과한다.
INJECTED_THRESHOLD = 0.09


def swap_threshold(monkeypatch: pytest.MonkeyPatch, threshold: float) -> None:
    """② — ``ci_width_threshold`` **선언만** 바꾼다. 코드는 한 줄도 안 건드린다.

    ``test_judgment_day._swap_judgment_day`` 와 같은 형태다. 다른 점은 패치 대상이다:
    거기는 검사 모듈이 직접 ``load_constraints`` 를 부르므로 자기 이름을 갈아 끼우지만,
    여기서 판정하는 것은 **① 노드**라 그 모듈에 붙은 이름을 갈아 끼운다
    (``from … import`` 라 원본 모듈을 패치해도 안 먹는다).

    🔴 **``config.CONSTRAINTS_PATH`` 를 바꾸는 방식은 쓰지 않는다.** 그건 전역이라 같은
      프로세스의 다른 검사까지 닿는다 — 임계를 흔든 채로 시세·분할 검사가 돌아버린다.
      ``load_constraints`` 가 캐시를 안 하기 때문에 그 오염은 **예외 없이 결과만 조용히**
      바꾼다.

    ⚠️ **어댑터는 안 따라온다.** ``adapter.py`` 도 근거 문장을 만들 때 임계를 따로 읽는데
      (거기는 판정이 아니라 문장이다), 여기서는 ① 만 바꾼다. 어댑터까지 흔들 일이 생기면
      그 모듈도 같이 패치해야 한다.
    """
    real = load_constraints()
    swapped = {**real, "situation": {**real["situation"], "ci_width_threshold": threshold}}
    monkeypatch.setattr(
        "app.purchase_agent.nodes.classify_situation.load_constraints", lambda: swapped
    )


def force_situation(monkeypatch: pytest.MonkeyPatch, situation: str) -> None:
    """③ — ① 을 **상황만 고정한 스텁**으로 갈아 끼운다. 임계·mock 밴드와 무관해진다.

    **축은 진짜 함수를 그대로 부른다.** ``compute_allowed_axes`` 를 검사가 재구현하면
    축 규칙이 두 곳에 생기고 한쪽만 바뀐다 — ①이 상황을 인자로 받는 구조라 재구현할
    이유가 없다.

    🔴 **``state["situation"]`` 에 미리 심는 방식은 안 된다.** ① 이 그래프의 첫 노드라
      무조건 덮어쓴다 (실측: 8/21 에 ``uncertain`` 을 심고 돌려도 ``stable`` 이 나온다).
      갈아 끼울 자리는 State 가 아니라 **노드**다.

    ``NODES`` 를 ``setitem`` 으로 바꾸는 이유: ``build_graph`` 가 이 사전을 **호출 시점에**
    읽으므로 어댑터 경로(``adapter.py`` → ``build_graph``)까지 같이 닿고, ``monkeypatch``
    가 검사 끝에 원복한다.
    """

    def node(state: dict) -> dict[str, Any]:
        constraints = load_constraints()
        return {
            "situation": situation,
            "allowed_axes": compute_allowed_axes(state, situation, constraints),
        }

    monkeypatch.setitem(graph.NODES, "classify_situation", node)


def forced_proposals(run: Callable[..., dict], item: str, anchors: tuple) -> dict[str, dict]:
    """``{상황: {앵커: 제안}}`` — ③ 을 걸고 앵커를 전부 돌린다.

    모듈 스코프 픽스처에서 부른다. ``monkeypatch`` 픽스처는 함수 스코프라 여기서는
    ``MonkeyPatch.context()`` 를 직접 연다 — 블록을 벗어나면 ``NODES`` 가 원복된다.

    ⚠️ 기존 ``proposals`` 픽스처를 **대체하지 않는다.** 그쪽은 실제 분류 경로를 그대로
      돌려 사중 일치·시세 실재 같은 상황 무관 검사를 먹인다. 여기 것은 상황이 **단언의
      일부인** 검사만 쓴다.
    """
    out: dict[str, dict] = {}
    for situation in ("stable", "uncertain"):
        with pytest.MonkeyPatch.context() as mp:
            force_situation(mp, situation)
            out[situation] = {as_of: run(item, as_of) for as_of in anchors}
    return out
