"""🔴 **모든 마스터 진입점이 개장 Gate 를 지난다** (설계 2026-09-07 §1).

지금까지 개장 판정은 `run_procurement` **안에만** 있었다. 판매 진입점을 만들면서
그것을 복사했고, **다음에 세 번째 진입점이 생길 때 또 잊는다.**

🔴 **잊었을 때 나는 증상이 조용하다.**

```text
막힌 것이 아니라 안 막힌 것이다
안 열린 날 판매가 돈다 — 아무 오류도 안 난다
```

빠뜨린 쪽은 초록불이고, 안 열린 장부 위에서 판단이 서서 나간다. 사람이 그것을 보는
때는 이력에 *"돈 날"* 이 쌓인 뒤다.

★ **주석으로는 안 된다.** 이 저장소에 이미 있는 방식 — `test_execution_day.py` 가
  모듈 **원문을 읽어** 규칙 위반을 막는 것 — 을 쓴다. 원문 문자열 대신 `ast` 로 보는
  것은 `test_envelope_sim_run_id.py` 와 같은 이유다: 주석·docstring 에 `check_day_gate`
  라고 **적기만 해도** 통과하는 검사가 되면 안 된다.

⚠️ **두 관문은 순서가 있고, 하나는 매입만 지난다.**

```text
① 개장 Gate      판매 · 매입 공통      그 날 장부가 열렸는가
② 실행일 Gate    🔴 매입만             장이 서는 날인가 (ML 예측이 있는가)
```

  **판매에 ②를 걸면 안 된다.** 주말에도 판다 — 그것이 개장을 달력일로 정한 이유다.
  아래 ④가 그것을 잠근다.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.master import revalidation, service

#: 🔴 **모듈 하나만 훑으면 그 파일 밖의 진입점이 통째로 안 보인다.**
#:
#:   최종 재검증(M-4)은 `service.py` 에 못 둔다 — 저쪽이 이미 `decision_service` 를
#:   임포트하는데 결정 적재가 재검증을 부르므로 import 가 원을 그린다. 그래서 자기
#:   모듈로 나왔고, **스캐너를 안 넓혔으면 그날부터 재검증은 게이트를 안 지나도
#:   초록이었다.**
#:
#: ★ **다음에 또 늘어난다는 전제로 목록을 여기 하나로 둔다.** 아래 검사는 전부 이
#:   목록에서 진입점을 찾는다 — 모듈을 더하면 검사가 자동으로 따라 는다.
_SCANNED = (service, revalidation)

_TREES = {모듈.__name__: ast.parse(inspect.getsource(모듈)) for 모듈 in _SCANNED}

#: 실행일 판정으로 가는 이름들. **판매 진입점에 이 중 하나라도 들어오면** 토요일
#: 요청이 서고, 2026년 토요일 45일에 판매가 멈춘다.
_EXECUTION_DAY_NAMES = frozenset(
    {"is_execution_day", "next_execution_day", "_execution_day_verdict"}
)


def _entrypoints() -> dict[str, ast.FunctionDef]:
    """훑는 모듈들의 **공개 진입점**.

    ★ **이름을 열거하지 않는다.** 열거하면 새 진입점이 생긴 날 목록만 옛말을 하고
      검사는 초록으로 남는다 (`test_envelope_sim_run_id.py` 가 `app/master/` 전체를
      훑는 것과 같은 이유).

    ★ **판정 기준은 `ExecutionContext` 를 만드느냐다.** 봉투를 만든다는 것은
      *"이 실행이 부서를 부른다"* 는 뜻이고, 부서를 부르려면 그 날 장부가 서 있어야
      한다. 조회 헬퍼(`get_run_history` 등)는 봉투를 안 만들므로 여기 안 걸린다 —
      그쪽은 남은 이력을 읽을 뿐 그 날을 판단하지 않는다.

    ⚠️ **함수 이름으로 키를 잡는다.** 두 모듈에 같은 이름의 공개 진입점이 생기면 한쪽이
      가려지므로, 아래 `test_이름이_겹치지_않는다` 가 그것을 먼저 잡는다.
    """
    found: dict[str, ast.FunctionDef] = {}
    for tree in _TREES.values():
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            if any(
                isinstance(inner, ast.Call) and _called_name(inner) == "ExecutionContext"
                for inner in ast.walk(node)
            ):
                found[node.name] = node
    return found


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _calls_in(node: ast.FunctionDef) -> set[str]:
    return {
        name
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and (name := _called_name(inner)) is not None
    }


# ── ① 스캐너부터 잰다 ───────────────────────────────────────────────────────


def test_스캐너가_진입점을_실제로_찾는다():
    """🔴 **먼저 이것부터.** 스캐너가 0건을 세면 아래 검사가 전부 공짜로 초록이 된다.

    `#320` 의 변이가 안 울었던 이유가 정확히 그 모양이었다 — 재는 줄은 있는데 재는
    대상이 없었다.
    """
    names = set(_entrypoints())

    assert names, "훑은 모듈에서 공개 진입점을 하나도 못 찾았다 — 스캐너가 고장 났다"
    assert {"run_procurement", "run_sales", "revalidate_scenario"} <= names, (
        f"매입·판매·재검증 세 진입점이 다 잡혀야 한다. 잡힌 것: {sorted(names)}"
    )


def test_모든_모듈에서_적어도_하나는_잡힌다():
    """🔴 **모듈을 목록에 더해 놓고 아무것도 안 잡히면 그 모듈은 안 재는 것과 같다.**

    ★ 위 검사는 이름 셋만 보므로, 넷째 모듈을 더했는데 거기서 0건이 잡혀도 통과한다.
      *"재는 줄은 있는데 재는 대상이 없다"* 를 모듈 단위로도 막는다.
    """
    빈_모듈 = [
        이름
        for 이름, tree in _TREES.items()
        if not any(
            isinstance(node, ast.FunctionDef)
            and not node.name.startswith("_")
            and any(
                isinstance(inner, ast.Call) and _called_name(inner) == "ExecutionContext"
                for inner in ast.walk(node)
            )
            for node in tree.body
        )
    ]

    assert 빈_모듈 == [], f"진입점이 하나도 없는 모듈을 훑고 있다: {빈_모듈}"


def test_이름이_겹치지_않는다():
    """⚠️ 두 모듈에 같은 이름의 진입점이 생기면 **한쪽이 사전에서 가려진다.**

    가려진 쪽은 게이트를 안 지나도 검사가 안 돈다 — 스캐너가 0건을 세는 것과 같은 병이
    이름 하나에서만 일어나는 판이다.
    """
    이름들 = [
        node.name
        for tree in _TREES.values()
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and any(
            isinstance(inner, ast.Call) and _called_name(inner) == "ExecutionContext"
            for inner in ast.walk(node)
        )
    ]

    assert len(이름들) == len(set(이름들)), f"진입점 이름이 겹친다: {sorted(이름들)}"


def test_조회_헬퍼는_진입점으로_세지_않는다():
    """★ 이력 조회는 그 날을 판단하지 않는다 — 관문을 요구하면 과녁이 넓어진다."""
    names = set(_entrypoints())

    assert "get_run_history" not in names
    assert "make_request_id" not in names


# ── ② 🔴 전부 개장 Gate 를 지난다 ───────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_entrypoints()))
def test_모든_마스터_진입점은_개장_게이트를_지난다(name):
    """🔴 **새 진입점이 생기면 이 검사가 먼저 빨개진다.**

    `parametrize` 가 진입점 목록에서 나오므로, 진입점을 하나 더 만들면 그 이름으로
    검사가 **자동으로 하나 더 생긴다.** 손으로 추가할 것이 없다.
    """
    assert "check_day_gate" in _calls_in(_entrypoints()[name]), (
        f"{name} 이 개장 Gate 를 안 지난다 — 안 열린 날에도 그대로 돈다. "
        "막힌 게 아니라 안 막힌 것이라 아무 오류도 안 난다"
    )


@pytest.mark.parametrize("name", sorted(_entrypoints()))
def test_부르기만_하고_결과를_버리지_않는다(name):
    """⚠️ **부르는 것과 보는 것은 다르다.**

    `check_day_gate(as_of)` 를 부르고 결과를 안 보면 위 검사는 통과하는데 그 날은
    그대로 돈다. 판정을 실제로 쓰는지는 **`BLOCKED` 과 견주는 줄**이 있는지로 본다.
    """
    body = _entrypoints()[name]
    비교 = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Compare)
        for operand in [*node.comparators, node.left]
        if isinstance(operand, ast.Constant) and operand.value == "BLOCKED"
    ]

    assert 비교, f"{name} 이 개장 판정을 부르기만 하고 BLOCKED 을 안 본다"


# ── ③ 응답까지 묶지는 않는다 ────────────────────────────────────────────────


def test_두_사이클이_응답_조립을_공유하지_않는다():
    """🔴 **공유하는 것은 판정과 순서이지 응답이 아니다** (설계 §1).

    응답 모델도 종료 코드도 다르다. 억지로 한 함수로 묶으면 판매 종료 코드가 매입
    어휘로 새거나 그 반대가 된다 — `SL2_NO_CANDIDATE` 를 `E2_HELD` 로 적는 날이 온다.
    """
    calls = {name: _calls_in(node) for name, node in _entrypoints().items()}

    assert "_empty_response" in calls["run_procurement"]
    assert "_empty_sales_response" in calls["run_sales"]
    assert "_empty_response" not in calls["run_sales"], (
        "판매가 매입의 빈 응답을 쓴다 — end_code 가 E4 로 나간다"
    )
    assert "_empty_sales_response" not in calls["run_procurement"]


# ── ④ 🔴 실행일 Gate 는 매입만 지난다 ───────────────────────────────────────


def test_매입은_실행일_게이트도_지난다():
    """★ ④의 반대쪽. 이것이 없으면 아래 검사가 *"둘 다 안 부른다"* 로도 초록이 된다."""
    assert _calls_in(_entrypoints()["run_procurement"]) & _EXECUTION_DAY_NAMES, (
        "매입이 실행일 판정을 안 부른다 — 주말에 ML 예측 없이 안을 만든다"
    )


def test_판매는_실행일_게이트를_지나지_않는다():
    """🔴 **주말에도 판다.**

    파는 데는 ML 예측이 필요 없다 — 실행일 관문이 막는 것은 *"장이 안 서서 예측이
    없는 날"* 이고, 그건 매입의 물음이다. 여기 실행일 판정을 복사해 넣으면 2026년
    토요일 45일에 판매가 통째로 선다.
    """
    샌_것 = _calls_in(_entrypoints()["run_sales"]) & _EXECUTION_DAY_NAMES

    assert not 샌_것, f"판매에 실행일 게이트가 걸렸다 — 주말 판매가 막힌다: {sorted(샌_것)}"


def test_실행일_게이트는_매입_하나만_지난다():
    """🔴 **재검증에도 걸리면 토요일에 승인이 통째로 막힌다.**

    최종 재검증은 판매 후보를 다시 보는 자리라 판매와 같은 편이다 — 파는 데는 ML
    예측이 필요 없다. 이름을 열거하지 않고 *"매입 말고 아무도"* 로 잠근다.
    """
    샌_진입점 = {
        이름: sorted(_calls_in(node) & _EXECUTION_DAY_NAMES)
        for 이름, node in _entrypoints().items()
        if 이름 != "run_procurement" and _calls_in(node) & _EXECUTION_DAY_NAMES
    }

    assert 샌_진입점 == {}, f"매입 아닌 진입점에 실행일 게이트가 걸렸다: {샌_진입점}"
