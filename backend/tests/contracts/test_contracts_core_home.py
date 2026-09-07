"""공용 계약의 집은 `app/contracts/core.py` **하나뿐이다.**

전에는 `test_shim_reexports.py` 였다 — 옛 자리(`app/orchestrator/contracts_core.py`)가
재수출 shim 으로 살아 있는 동안 *"그 shim 이 쓰이는 이름을 다 내보내는가"* 를 쟀다.

```text
①  app/contracts/core.py 로 실체 이동          ✅  2026-09-03
②  옛 자리를 재수출로 남긴다                    ✅  2026-09-03
③  각 파트가 import 를 새 자리로 바꾼다          ✅  파트별
④  구 경로 참조 0건 확인 후 shim 제거            ✅  2026-09-07  ← 이 판
```

🟢 **④ 가 끝나 shim 이 없다.** 그래서 shim 을 재던 검사 넷은 지웠다 — 재는 대상이
  없는 검사는 초록불이어도 아무것도 안 막는다. 그중 하나는 자기 docstring 에
  *"④ 시점이면 지워라"* 라고 적어 뒀다.

  ```text
  지운 것                                        이유
  test_옛_자리가_쓰이는_이름을_전부_내보낸다      shim 이 없다
  test_옛_경로로_비공개_이름을_읽는_곳이_없다     옛 경로가 없다
  test_두_경로가_같은_객체를_가리킨다             경로가 하나다
  test_체크포인트_계약_목록이_비지_않는다         graph_langgraph.py 를 지웠다
  ```

★ **옛 경로가 정말 0건인지는 여기서 안 잰다.** `tests/master/test_orchestrator_is_gone.py`
  가 저장소 전체를 훑어 그것을 잠근다. 사실의 주인은 하나다.

남긴 넷은 shim 과 무관하게 **새 자리에 대해** 참인 주장이다.
"""

from __future__ import annotations

import ast
import pathlib

from app.contracts import core

NEW = "app.contracts.core"

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _imported_names(module_path: str) -> dict[str, set[str]]:
    """저장소에서 그 모듈로부터 import 하는 이름 → 그것을 쓰는 파일들."""
    out: dict[str, set[str]] = {}
    for base in ("app", "tests"):
        for path in (_ROOT / base).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == module_path:
                    for alias in node.names:
                        rel = path.relative_to(_ROOT).as_posix()  # 윈도우 역슬래시를 통일한다
                        out.setdefault(alias.name, set()).add(rel)
    return out


def test_비공개_계약_이름을_읽는_곳을_고정한다():
    """🔴 **자리를 옮겨도 사실은 그대로다** — `_` 를 남이 읽고 있다.

    `_` 는 *"밖에서 쓰지 말라"* 는 표시인데 Critic 이 계약을 그렇게 쓴다.
    자리 이전의 합의가 **위치와 import 만**이라 이름을 안 건드렸다.

    고칠 때까지 **어디가 읽는지 고정한다** — 늘어나면 여기서 빨간불이다
    (`test_known_gaps` 와 같은 규율).
    """
    readers: set[str] = set()
    for name, files in _imported_names(NEW).items():
        if name.startswith("_"):
            readers |= files

    assert readers == {"app/critic/critic.py", "app/critic/critic_v0_4.py"}, (
        f"계약의 비공개 이름을 읽는 곳이 달라졌다: {sorted(readers)}"
    )


def test_스캐너가_파일을_실제로_읽는다():
    """★ **0건을 세면 위 검사가 공짜로 초록이 된다.**

    `_imported_names` 가 아무 파일도 못 읽으면 `readers` 가 빈 집합이 되는데,
    그때 위 검사는 기대값과 안 맞아 빨간불이다 — 그건 다행이다. 하지만 기대값이
    언젠가 빈 집합이 되면 (비공개 읽기가 다 고쳐지면) **스캐너가 죽어도 초록**이다.
    그 전에 스캐너 자체를 잰다.
    """
    hits = _imported_names(NEW)
    assert hits, "계약을 읽는 곳이 하나도 안 잡혔다 — 스캐너가 파일을 못 읽는다"
    assert "Evidence" in hits, f"계약의 핵심 이름이 안 잡혔다: {sorted(hits)[:10]}"


def test_계약_dataclass_의_소속은_새_자리다():
    """`obj.__module__` 로 계약 dataclass 를 고르는 코드가 있었다.

    체크포인트 역직렬화가 그것에 의존했다 — 소속이 흔들리면 목록이 통째로 비고
    한참 뒤 노드에서 `AttributeError: 'dict' object has no attribute ...` 가 났다.
    그 코드(`graph_langgraph.py`)는 2026-09-07 에 지웠지만, **소속이 새 자리라는
    사실 자체**는 계약이 옮겨 온 뒤로 계속 참이어야 한다.
    """
    assert core.Band.__module__ == NEW


def test_공용_계약은_아무_파트도_import_하지_않는다():
    """🔴 **공용 계약이 한 파트를 읽으면 자리를 옮긴 뜻이 없어진다.**

    옛 자리의 문제가 정확히 그것이었다 — 네 파트가 `app/orchestrator/` 를 읽는
    모양이었다. 새 자리가 거꾸로 파트를 읽으면 같은 병이 방향만 바뀐다.
    """
    tree = ast.parse((_ROOT / "app" / "contracts" / "core.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    app_imports = sorted(m for m in imported if m.startswith("app."))
    assert app_imports == [], f"공용 계약이 파트 코드를 읽는다: {app_imports}"
