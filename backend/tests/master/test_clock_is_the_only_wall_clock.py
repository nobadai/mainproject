"""**`app/master/` 에서 벽시계를 읽는 곳은 `clock.py` 하나다.**

🔴 **왜 검사로 지키는가.** 벽시계는 한 줄이면 다시 생긴다. `datetime.now()` 를
어딘가에 적는 것은 아무 마찰도 없고, 스위트도 안 깨진다 — **오늘 날짜로 답해도
오늘 도는 검사는 통과하기 때문이다.** 그러다 백테스트를 돌리는 날, 그 한 줄부터
아래로 전부 오늘로 답하기 시작하고 성적이 조용히 무효가 된다.

```text
지금   벽시계를 새로 적는다 → 이 파일이 그 자리에서 운다
없으면 벽시계를 새로 적는다 → 아무 일도 안 난다 → 백테스트에서 뒤늦게 안다
```

★ **막는 파일이 아니라 세우는 파일이다.** 스케줄러는 `as_of` 를 아무한테서도 못
  받으므로 시계를 읽어야만 시작한다. 그 예외를 **한 파일이 혼자 지게** 두고, 그
  아래로는 `as_of` 를 인자로만 흘린다 (`clock.py` docstring).

🔴 **자기 생존 검사를 같이 둔다.** 스캐너가 `clock.py` 의 그 호출을 **실제로
  찾는지** 부터 본다. 안 그러면 스캐너가 망가져 0건을 세는 날 **공짜 초록**이 나고,
  그때는 벽시계가 열 군데 생겨도 이 파일이 통과한다.

⚠️ **`tests/` 는 대상이 아니다.** 검사가 고정 시각을 만드는 것은 벽시계가 아니다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.master

_MASTER = Path(app.master.__file__).parent

#: 벽시계를 읽는 호출 이름.
#:
#: ★ `now` 하나로는 못 잡는다 — `date.today()` 도 `datetime.utcnow()` 도 같은 일을
#:   하고, 셋 중 하나만 열려 있으면 그리로 샌다.
_WALL_CLOCK_ATTRS = frozenset({"today", "now", "utcnow"})

#: 그 호출을 받는 이름. `datetime.datetime.now()` 처럼 점이 더 붙어도 끝만 본다.
_CLOCK_RECEIVERS = ("date", "datetime")

#: 벽시계를 읽어도 되는 **단 하나의 파일.**
_ALLOWED = "clock.py"

#: 🟡 **날짜가 아니라 사건 시각을 찍는 자리.** 축이 달라 이 규율의 대상이 아니다.
#:
#: ```text
#: as_of        "오늘이 며칠인가"        판단이 그 위에 선다   → clock.py 하나
#: ended_at     "이 실행이 언제 끝났나"   지나간 일의 기록      → 여기
#: ```
#:
#: ★ `app/contracts/core.py:1715` 의 `started_at: datetime = field(default_factory=
#:   datetime.utcnow)` 와 **짝**이다. 한쪽만 timezone-aware 로 바꾸면 두 칸의 뜻이
#:   갈리고, 그것은 FROZEN 계약을 건드리는 판이라 여기서 하지 않는다.
#:
#: 🔴 **목록이지 면제가 아니다.** 여기 없는 자리가 새로 생기면 아래 검사가 운다.
#:   그리고 `test_예외_자리는_날짜를_안_만든다` 가 이 자리가 정말 날짜로 안 새는지
#:   따로 확인한다.
#:
#: ⚠️ **줄 번호는 안 적는다** — 편집마다 흔들린다
#:   (`test_external_use_of_master_internals.py` 와 같은 규율).
#:
#: 🟢 **2026-09-08 에 비었다.** 유일한 자리였던 `cycle_graph.py` 의 `node_t4_commit`
#:   이 파일과 함께 지워졌다 (`test_master_legacy_cycle_is_gone.py`). 목록이 빈 것은
#:   *"예외가 없다"* 이고, 새 자리가 생기면 아래 검사가 그날 운다.
_TIMESTAMP_ONLY: dict[str, set[str]] = {}


def _clock_nodes(path: Path) -> list[tuple[ast.Call, str]]:
    """그 파일의 벽시계 호출 노드와 이름. `(Call, "datetime.now")`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[ast.Call, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _WALL_CLOCK_ATTRS:
            continue
        receiver = ast.unparse(func.value)
        if receiver.split(".")[-1] not in _CLOCK_RECEIVERS:
            continue
        found.append((node, f"{receiver}.{func.attr}"))
    return found


def _wall_clock_calls(path: Path) -> set[str]:
    """그 파일이 벽시계를 읽는 이름들. `{"datetime.now"}`."""
    return {name for _, name in _clock_nodes(path)}


def _scan() -> dict[str, set[str]]:
    """`app/master/` 전체를 훑는다. 파일 이름 → 벽시계 호출 이름."""
    hits: dict[str, set[str]] = {}
    for path in sorted(_MASTER.rglob("*.py")):
        calls = _wall_clock_calls(path)
        if calls:
            hits[path.relative_to(_MASTER).as_posix()] = calls
    return hits


def test_스캐너가_clock의_벽시계를_실제로_찾는다():
    """🔴 **자기 생존 검사.** 이게 없으면 아래 검사는 0건을 세고도 초록이 된다.

    ★ 스캐너가 죽으면 *"벽시계가 하나도 없다"* 와 *"스캐너가 아무것도 못 본다"* 가
      **같은 결과**로 보인다. 둘을 가르는 것이 이 검사다.
    """
    calls = _wall_clock_calls(_MASTER / _ALLOWED)

    assert calls, (
        "스캐너가 clock.py 의 벽시계 호출을 못 찾았다 —"
        " 스캐너가 망가졌거나 clock.py 가 시계를 안 읽는다."
        " 어느 쪽이든 아래 검사는 공짜 초록이다"
    )
    assert "datetime.now" in calls, f"찾은 것이 벽시계가 아니다: {calls}"


def test_스캐너가_예외_자리도_실제로_찾는다():
    """🔴 **자기 생존 검사 두 번째.** 예외 목록이 실재하는 자리를 가리키는지 본다.

    ★ 예외 목록이 **없어진 자리**를 가리키면, 그 목록은 아무것도 안 봐 주면서
      *"봐 주고 있다"* 는 인상만 남긴다. 아래 검사는 그때도 통과한다.

    🟢 목록이 비어 있으면 **봐 줄 자리가 없다는 주장**이다. 그때는 빈 반복으로
      공짜 초록이 되지 않게, 스캐너가 실제로 예외 없는 상태를 보고 있는지 잰다.
    """
    if not _TIMESTAMP_ONLY:
        assert set(_scan()) <= {_ALLOWED}, (
            f"예외 목록이 비었는데 {_ALLOWED} 밖에서 벽시계를 읽는다: {_scan()}"
        )
        return

    for name, expected in _TIMESTAMP_ONLY.items():
        assert _wall_clock_calls(_MASTER / name) >= expected, (
            f"{name} 에 {expected} 가 없다 — 예외 목록이 유령을 가리킨다."
            " 그 자리가 사라졌으면 목록에서도 지워야 한다"
        )


def test_벽시계를_읽는_곳은_clock_하나뿐이다():
    """**`clock.py` 밖에서 벽시계를 읽으면 여기서 운다.**

    ⚠️ 고치는 법은 그 자리에서 `datetime.now()` 를 지우고 **`as_of` 를 인자로 받는
      것**이다. `clock.today_in_seoul()` 을 그 자리에서 부르는 것도 답이 아니다 —
      그러면 백테스트가 그 지점부터 오늘로 답한다. 시계를 읽는 것은 **진입점 하나**
      이고, 나머지는 받은 `as_of` 를 흘리기만 한다.

    🟡 `_TIMESTAMP_ONLY` 는 **날짜가 아니라 사건 시각**을 찍는 자리다. 목록에 없는
      새 자리가 생기면 여기서 운다 — 면제가 아니라 고정이다.
    """
    hits = _scan()
    others = {name: calls for name, calls in hits.items() if name != _ALLOWED}

    assert others == _TIMESTAMP_ONLY, (
        f"clock.py 밖에서 벽시계를 읽는다: {others}. 이 자리는 as_of 를 인자로 받아야 한다"
    )


def test_예외_자리는_날짜를_안_만든다():
    """🔴 **예외가 `as_of` 로 새지 않는지 따로 본다.**

    `ended_at` 은 지나간 일의 기록이라 이 규율의 대상이 아니지만, 같은 호출에
    `.date()` 를 붙이는 순간 **그것이 곧 "오늘이 며칠인가" 가 된다.** 목록에 있다는
    이유로 그 변신까지 통과시키면 예외가 구멍이 된다.
    """
    for name in _TIMESTAMP_ONLY:
        clock_calls = {ast.unparse(node) for node, _ in _clock_nodes(_MASTER / name)}
        tree = ast.parse((_MASTER / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `<벽시계 호출>.date()` 를 찾는다 — 시각이 날짜로 바뀌는 유일한 모양이다.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "date"
                and ast.unparse(node.func.value) in clock_calls
            ):
                raise AssertionError(
                    f"{name}:{node.lineno} 의 {ast.unparse(node)} 가 날짜를 만든다 —"
                    " 그것은 as_of 이고, as_of 의 주인은 clock.py 하나다"
                )


def test_clock_말고는_ZoneInfo_로_시간대를_안_만든다():
    """★ 시간대의 주인도 하나다.

    🔴 `timezone(timedelta(hours=9))` 를 각자 들고 있으면 조용히 갈린다. 실제로
      `revalidation.py` 가 `_KST` 를 따로 들고 있었고(2026-09-08 정리), 값이 같아서
      아무도 못 봤다 — **같아서 못 본 것이지 안 갈릴 이유가 있던 것이 아니다.**
    """
    owners = []
    for path in sorted(_MASTER.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] in (
                "ZoneInfo",
                "timezone",
            ):
                owners.append(path.relative_to(_MASTER).as_posix())
                break

    assert owners == [_ALLOWED], f"시간대를 만드는 곳이 clock.py 말고 또 있다: {owners}"
