"""검사가 **판정 입력을 직접 주입**하는 두 도구 (현서님 §1.4 ②③).

원래 이 스위트는 임계와 상황을 ``constraints.yaml``·mock 밴드에서 **간접적으로** 얻었다.
그래서 ``ci_width_threshold`` 하나를 흔들면 25건이 무너졌는데, 그 25건 중 임계를 재려던
검사는 몇 건뿐이고 나머지는 *"uncertain 인 날 문서를 읽는가"*처럼 임계와 상관없는 것을
재고 있었다. 판정 입력이 데이터에 묻혀 있어서 생긴 결합이다.

여기 둘은 그 입력을 **검사가 직접 준다**:

``swap_threshold``     ② 임계를 주입한다 — "mock 분포가 임계를 넘나"가 아니라
                          "임계보다 크면 uncertain, 작으면 stable 로 갈리나"를 재게 한다
``force_situation``    ③ 상황을 주입한다 — ② 컨텍스트 루프 검증이 임계·mock 밴드와
                          무관해진다
``declare_thresholds`` **선언 자체를 갈아 끼운다** — 임계가 품목별이 되면서, *"없다"*
                          와 *"모양이 다르다"* 를 재려면 사전 자체를 바꿔야 한다.
                          위 둘과 달리 **진짜 YAML 파일**을 만든다 (아래 이유)

⚠️ **mock 을 대체하지 않는다.** ``mocks/`` 의 예측·시세는 그대로 쓰고 ``ci_band`` 도 안
건드린다 (현서님 ④: *"넓히는 게 아니라 떼는 것이 답"*). 밴드의 ``upper`` 는 ``ci_width``
말고 ``compute_max_price`` 도 먹이므로, 넓히면 ⑦ ``check_max_price`` 컷 기준이 함께
풀린다 — 실측으로 전폭 0.5 면 상한이 +17.9% 오르고 여유가 15%→30% 가 된다.

🔴 **그리고 임계 0.08 로는 실제 폭이 전부 ``uncertain`` 이다** (2026-09-10 실측 · 가장 좁은
한 줄인 양파 0.220 도 임계의 2.7배다). 그래서 밴드 현실화는 **컷을 ``upper`` 에서 뗀 뒤**다
— 현서님 말이 그 순서를 이미 가리키고 있었다. 표는 ``mocks/README.md`` 의 「실측 밴드」 절.
"""

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.purchase_agent import graph
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.classify_situation import compute_allowed_axes

#: ``declare_thresholds`` 가 갈아 끼우는 자리. **읽는 모듈을 여기 적는다** — 한 곳을
#: 빠뜨리면 그 경로만 진짜 파일을 계속 읽고, 검사는 *"바꿨는데 안 바뀐다"* 가 아니라
#: **초록으로 지나간다** (그쪽 판정이 원래 값으로 정상 동작하므로).
_THRESHOLD_READERS = (
    "app.purchase_agent.nodes.classify_situation.load_constraints",
    "app.purchase_agent.adapter.load_constraints",
)

#: ② 검사가 주입하는 임계. mock 의 두 층위(stable ≈0.060 / uncertain ≈0.120) **사이**면
#: 아무 값이나 되고, 그 사이가 비어 있다는 사실은
#: ``test_mocks.test_the_two_mock_bands_never_overlap`` 이 따로 잠근다.
#:
#: 🔴 **선언값(현재 0.08)과 일부러 다른 값을 쓴다.** 같은 값이면 주입이 먹었는지 안 먹었는지
#:   구분할 수 없다 — 패치를 통째로 지워도 검사가 그대로 통과한다.
INJECTED_THRESHOLD = 0.09


def swap_threshold(
    monkeypatch: pytest.MonkeyPatch, threshold: float | Mapping[str, float]
) -> None:
    """② — ``ci_width_threshold`` **선언만** 바꾼다. 코드는 한 줄도 안 건드린다.

    **두 가지를 받는다** (임계가 품목별이 된 뒤):

    ``float``    전 품목에 같은 값을 편다. *"임계보다 크면 uncertain"* 처럼 품목이
                 변수가 아닌 검사가 쓴다 — 그런 검사에 품목별 dict 를 적게 하면
                 품목 목록이 검사마다 복사된다
    ``Mapping``  품목마다 다른 값. *"한 품목만 흔들어도 다른 품목이 안 움직이나"*
                 처럼 **갈림 자체가 단언인** 검사가 쓴다

    🔴 **``float`` 을 펴는 대상은 mock 품목이 아니라 선언에 있는 품목이다.**
      검사가 품목 목록을 들면 선언에서 한 품목을 빼도 이 도구는 그대로 돌아간다 —
      "선언을 바꾸면 따라 움직이나"를 재는 도구가 선언을 안 보는 꼴이 된다.

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
    declared = real["situation"]["ci_width_threshold"]
    spread = (
        dict(threshold)
        if isinstance(threshold, Mapping)
        else dict.fromkeys(declared, float(threshold))
    )
    swapped = {**real, "situation": {**real["situation"], "ci_width_threshold": spread}}
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

    🔴 **보유도 같이 뗀다** (``drop_holdings``). 이 함수를 쓰는 검사는 전부 *"상황이 이러면
      안이 몇 개인가 · 축이 무엇인가"* 를 재는데, 보유를 켜 두면 mock 앵커에서 보수안이
      사라져 그 단언이 **보유 때문에** 무너진다. 재려는 축과 무관한 이유로 죽는 검사를
      만들지 않는다 — 보유 자체는 ``test_holdings_deduction`` 이 잰다.
    """
    out: dict[str, dict] = {}
    for situation in ("stable", "uncertain"):
        with pytest.MonkeyPatch.context() as mp:
            force_situation(mp, situation)
            drop_holdings(mp)
            out[situation] = {as_of: run(item, as_of) for as_of in anchors}
    return out


def declare_thresholds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, declared: Any
) -> Path:
    """``situation.ci_width_threshold`` 를 ``declared`` 로 바꾼 **진짜 YAML** 을 읽게 한다.

    ``swap_threshold`` 는 값만 바꾸므로 *"임계가 아예 없다"* · *"``null`` 이다"* ·
    *"스칼라로 되돌아갔다"* 를 못 만든다 — 셋 다 **사전의 모양**이 다른 경우다.

    🔴 **dict 를 손으로 만들어 넘기지 않고 파일을 거친다.** 재려는 것이
      *"선언에 없으면 멈추나"* 인데, 검사가 dict 를 직접 지어 주면 **YAML 이 그 모양을
      실제로 실어 나를 수 있는지**는 아무도 안 본다. ``load_constraints`` 의 섹션 검사가
      두 항목짜리 사전을 통과시킨다는 것도 여기서 같이 확인된다.

    ⚠️ **주석은 사라진다.** ``safe_load`` → ``safe_dump`` 왕복이라 값만 남는다.
      원본 파일은 안 건드리므로 문제가 아니지만, 이 파일을 사람이 읽을 것으로 기대하면 안 된다.

    🔴 **``config.CONSTRAINTS_PATH`` 를 안 바꾼다** — ``swap_threshold`` 와 같은 이유로
      그건 전역이라 같은 프로세스의 다른 검사까지 닿는다. 대신 **읽는 모듈의 이름**을
      갈아 끼운다 (``_THRESHOLD_READERS``).
    """
    loaded = load_constraints()
    loaded["situation"]["ci_width_threshold"] = declared
    target = tmp_path / "constraints.yaml"
    target.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
    for name in _THRESHOLD_READERS:
        monkeypatch.setattr(name, lambda path=target: load_constraints(path))
    return target


#: ``no_holdings`` 가 보유를 떼는 두 자리. **경로가 둘이라 둘 다 적는다** —
#: mock 은 포트로 받고 (``build_initial_state``), 운영·어댑터 검사는 봉투로 받는다
#: (``build_state``). 한 곳만 막으면 다른 경로의 검사는 보유가 실린 채로 돈다.
_HOLDINGS_SOURCES = (
    "app.purchase_agent.ports.get_inventory",
    "app.purchase_agent.adapter.absorb_inventory",
)


def _without_holdings(inventory: Any) -> Any:
    """로트는 **남기고** ``available_qty_kg`` 만 0 으로 내린다.

    🔴 **로트를 지우지 않는다.** 등급·잔여신선도는 ⑤ 등급 배분과 ⑥ 근거 문장이 쓰므로,
      통째로 비우면 차감과 **상관없는** 문장이 같이 사라진다 — 그러면 이 도구가 재려던
      것보다 넓게 끈 것이 된다.
    """
    if not isinstance(inventory, Mapping):
        return inventory
    lots = inventory.get("lots")
    if not isinstance(lots, list):
        return inventory
    emptied = [
        {**lot, "available_qty_kg": 0} if isinstance(lot, Mapping) else lot for lot in lots
    ]
    return {**inventory, "lots": emptied}


def drop_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    """④ — 보유를 0 으로 주입한다. **이 검사는 보유를 재지 않는다**는 선언이다.

    보유 차감(상세설계 §4-③)이 들어오면서 mock 앵커가 *"보유가 커버 창을 다 덮는 세계"*
    가 됐다 — 배추 3,000kg ÷ 일평균 1,286 = 2.33일치라 보수(D=2)가 통째로 0이 된다.
    그래서 **보유와 무관한 검사 21개**가 한꺼번에 무너졌다.

    ```text
    무너진 검사 21개 중   보유를 재는 것          0건
                          수량을 절대값으로 단언   0건
    ```

    ★ ``swap_threshold`` 가 생긴 이유와 **같은 모양**이다 (이 파일 머리말) — 판정 입력이
      데이터에 묻혀 있어서 생긴 결합이다. 답도 같다: 검사가 그 입력을 **직접 준다**.

    🔴 **«주입으로 막았다» 로 끝내지 않는다.** 보유가 수요를 덮는 것은 진짜 동작 변화이고,
      그것은 ``test_holdings_deduction`` 이 보유를 **크게** 주입해서 정면으로 잰다. 이
      도구는 그 축을 **여기서 안 잰다**고 적는 것이지, 변화를 가리는 것이 아니다.

    함수로 두고 픽스처(``no_holdings``)를 따로 감싸는 이유: 모듈 스코프 픽스처가 그래프를
    미리 돌려 두는 검사들이 있는데(``proposals`` · ``forced``), 함수 스코프 픽스처는 그때
    이미 늦다. 그런 자리는 ``MonkeyPatch.context()`` 를 직접 열고 이 함수를 부른다.
    """
    for name in _HOLDINGS_SOURCES:
        module_path, _, attr = name.rpartition(".")
        module = __import__(module_path, fromlist=[attr])
        original = getattr(module, attr)

        def patched(*args: Any, __original: Any = original, **kwargs: Any) -> Any:
            return _without_holdings(__original(*args, **kwargs))

        monkeypatch.setattr(name, patched)


@pytest.fixture
def no_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``drop_holdings`` 의 함수 스코프 픽스처판. 뜻은 그쪽 docstring 에 있다."""
    drop_holdings(monkeypatch)


def inject_arrival_cap(state: dict, cap_kg: float | None, *, lead_days: int = 2) -> str:
    """④ — **도착일 창고 여유를 검사가 준다** (`#308`). 도착일 ISO 문자열을 돌려준다.

    분할 진입 임계가 선언(``split_entry_qty_kg: 20000``)에서 **물류가 낸 그날 값**
    (``cap_by_date[as_of + N4]``)으로 바뀌면서 필요해졌다. mock 경로는 N4 도
    ``cap_by_date`` 도 없어서 — 둘 다 미결이다 — **수량 가지가 아예 판정되지 않는다.**
    그래서 그 가지를 재려면 검사가 직접 넣어야 한다.

    ⚠️ **N4 를 State 최상위에 넣는다.** ``pending_value`` 가 보는 자리가 거기다
      (``inventory`` 안에만 넣으면 값이 있는데도 «미결» 로 읽힌다 — 실제로 났던 버그).

    ``cap_kg`` 가 ``None`` 이면 **N4 만 주고 여유는 안 준다** — "못 봤다"(규칙 3)를
    재는 자리다.

    ⚠️ **하루치만 넣는다.** 진입 게이트가 보는 날은 ``as_of + N4`` 하나뿐이고, 창을 통째로
      채우면 ⑥ ``cap_constrained_quantities`` 가 회차 물량을 재배분하기 시작해 **재려던
      것과 다른 것이 바뀐다.** 여러 날이 필요한 검사는 그쪽에서 직접 채운다.
    """
    state["inbound_lead_days"] = lead_days
    arrival = (date.fromisoformat(state["date"]) + timedelta(days=lead_days)).isoformat()
    cap_by_date = {} if cap_kg is None else {arrival: cap_kg}
    state["inventory"] = {**state["inventory"], "cap_by_date": cap_by_date}
    return arrival
