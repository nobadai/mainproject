"""옛 자리(`app.orchestrator.contracts_core`)가 **아직 다 내보내는가.**

2026-09-03 · `contracts_core` 자리 이전 ①② (다섯 파트 동의).

```text
①  app/contracts/core.py 로 실체 이동          ✅
②  옛 자리를 재수출로 남긴다                    ✅  ← 이 파일이 잠근다
③  각 파트가 import 를 새 자리로 바꾼다          ⬜
④  구 경로 참조 0건 확인 후 shim 제거            ⬜
```

🔴 **②가 있어야 ③을 파트별로 나눠 할 수 있다.** 한 번에 다 바꾸면 다섯 파트가 같은
판에서 움직여야 하고, 깨졌을 때 누구 것인지 못 가린다.

★ **이름을 손으로 세지 않는다.** 저장소를 AST 로 훑어 *"옛 경로에서 실제로 import
  하는 이름"* 을 모은다. `import *` 가 빠뜨리는 것이 있으면 여기서 빨간불이다 —
  실제로 `_DEPT_AXES` 하나가 그렇게 걸렸다 (언더스코어라 별표가 건너뛴다).
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

from app.contracts import core
from app.orchestrator import contracts_core as shim

OLD = "app.orchestrator.contracts_core"
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
                        out.setdefault(alias.name, set()).add(str(path.relative_to(_ROOT)))
    return out


# ── ① 핵심 — 옛 경로가 쓰이는 이름을 다 갖고 있는가 ─────────────────────────


def test_옛_자리가_쓰이는_이름을_전부_내보낸다():
    """🔴 **이 파일의 주장이다.**

    `import *` 는 언더스코어 이름을 건너뛴다. 사람이 목록을 세면 반드시 빠뜨린다.
    """
    used = _imported_names(OLD)
    assert used, "옛 경로를 쓰는 곳이 하나도 없다 — 이 검사가 무의미하다 (④ 시점이면 지워라)"

    missing = {name: files for name, files in used.items() if not hasattr(shim, name)}
    assert not missing, f"shim 이 안 내보내는 이름: {missing}"


def test_별표가_건너뛰는_이름이_실제로_있다():
    """전제 단언. 언더스코어 이름을 아무도 안 쓰면 위 검사가 `import *` 만으로도 통과한다.

    ⚠️ 비공개 이름을 남이 읽는 것 자체가 문제지만, 이 판은 **위치와 import 만**
      바꾸기로 합의했으므로 이름을 안 건드린다. 별건이다.
    """
    used = _imported_names(OLD)
    private = sorted(n for n in used if n.startswith("_"))

    assert private == ["_DEPT_AXES"], (
        f"밖에서 읽히는 비공개 계약 이름이 달라졌다: {private}. "
        f"shim 의 명시 재수출을 같이 고쳐야 한다"
    )


# ── ② 같은 객체여야 한다 ────────────────────────────────────────────────────


def test_두_경로가_같은_객체를_가리킨다():
    """🔴 **정의를 복사하면 안 된다.**

    복사하면 `ContractViolation` 을 옛 경로로 잡는 코드가 새 경로에서 던진 것을
    못 잡고, `isinstance` 도 갈린다. 증상이 한참 뒤에 나온다.
    """
    for name in ("Evidence", "SuggestedAdjustment", "ContractViolation", "Band", "T0Snapshot"):
        assert getattr(shim, name) is getattr(core, name), f"{name} 이 두 경로에서 다른 객체다"


def test_계약_dataclass_의_소속은_새_자리다():
    """⚠️ **shim 이 못 덮는 것이 있다** — `__module__` 은 재수출로 안 바뀐다.

    `graph_langgraph._CONTRACT_TYPES` 가 `obj.__module__` 로 계약 dataclass 를
    골라 체크포인트 역직렬화에 쓴다. 옛 경로를 보면 목록이 통째로 비고,
    한참 뒤 노드에서 `AttributeError: 'dict' object has no attribute ...` 가 난다.

    **경로가 아니라 모듈 정체성에 의존하는 코드는 ③ 을 먼저 해야 한다.**
    """
    assert core.Band.__module__ == NEW
    assert shim.Band.__module__ == NEW, "shim 을 거쳐도 소속은 새 자리다 — 이것이 shim 의 한계다"


def test_체크포인트_계약_목록이_비지_않는다():
    """위 사실이 실제로 무엇을 깨뜨렸는지 그 자리에서 잠근다."""
    from app.orchestrator.graph_langgraph import _CONTRACT_TYPES

    assert _CONTRACT_TYPES, "계약 dataclass 목록이 비었다 — 체크포인트가 dict 로 무너진다"
    assert all(dataclasses.is_dataclass(t) for t in _CONTRACT_TYPES)


# ── ③ 새 자리가 실행 코드를 안 들인다 ───────────────────────────────────────


def test_공용_계약은_아무_파트도_import_하지_않는다():
    """🔴 **공용 계약이 한 파트를 읽으면 자리를 옮긴 뜻이 없어진다.**

    옛 자리의 문제가 정확히 그것이었다 — 네 파트가 `app/orchestrator/` 를 읽는
    모양이었다. 새 자리가 거꾸로 파트를 읽으면 같은 병이 방향만 바뀐다.
    """
    tree = ast.parse((_ROOT / "app" / "contracts" / "core.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    app_imports = sorted(m for m in imported if m.startswith("app."))
    assert app_imports == [], f"공용 계약이 파트 코드를 읽는다: {app_imports}"
