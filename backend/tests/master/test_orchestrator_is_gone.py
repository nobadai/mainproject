"""옛 `app/orchestrator/` 는 **저장소에 없다.**

지시 2026-09-07 — 폴더를 마스터 밑으로 넣는다.

이 파일은 `test_no_orchestrator_runtime.py` 였다. 뜻을 **뒤집어** 남긴다.

```text
2026-09-01  "모든 경로는 orchestrator/ 쪽으로 가지 않게"
            → 마스터가 그 폴더를 import 하는 것을 금지했다
2026-09-07  폴더 자체를 마스터로 옮긴다
            → 그 이름이 저장소에 남아 있는 것을 금지한다
```

★ **목적은 같다 — 되돌아가지 못하게 한다.** 전에는 *"마스터가 저기로 가지 마라"*
  였고 지금은 *"저기가 없다"* 다. 뒤엣것이 앞엣것을 포함한다.

옮긴 자리는 이렇다.

```text
app/orchestrator/band.py            →  app/master/band.py
app/orchestrator/outbound.py        →  app/master/outbound.py
app/orchestrator/cycle.py           →  app/master/cycle.py
app/orchestrator/schemas.py         →  app/master/cycle_schemas.py
app/orchestrator/persistence.py     →  app/master/cycle_persistence.py
app/orchestrator/run_repository.py  →  app/master/cycle_run_repository.py
app/orchestrator/graph.py           →  app/master/cycle_graph.py
app/orchestrator/graph_b.py         →  app/master/cycle_graph_b.py
app/orchestrator/llm/               →  app/master/cycle_llm/

app/orchestrator/graph_langgraph.py  지웠다 — 앱 참조 0건
app/orchestrator/interpretation.py   지웠다 — 앱 참조 0건
app/orchestrator/contracts_core.py   지웠다 — 재수출 shim ④ 완료
```

🔴 **셋에 `cycle_` 을 붙인 이유는 이름이 겹쳐서다.** `app/master/` 에 이미
  `schemas.py`·`persistence.py`·`run_repository.py` 가 있다. 그대로 옮기면 덮어쓴다.

⚠️ **DB 표 이름(`orchestrator_agent_runs`)은 안 바꿨다.** 코드 경로만 옮겼다 —
  표를 바꾸면 Critic 이 쓰는 행까지 건드려야 하고, 그건 이 판의 합의가 아니다.
"""

from __future__ import annotations

import ast
import pathlib

import app.master

#: 🔴 **일부러 이어 붙인다.** 이 파일에 옛 이름이 통짜로 적혀 있으면 스캐너가
#:   자기 자신을 잡는다. 자기 예외를 두면 이 파일 안에 옛 참조가 되살아나도 안 잡힌다.
#:   그래서 예외를 두는 대신 **리터럴이 없게** 쓴다.
_OLD_PACKAGE = "app" + "." + "orchestrator"

#: 같은 이유. 폴더 이름은 `app/` 밑에서 찾는다.
_OLD_DIRNAME = "orchestrator"

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("app", "tests")

_MASTER_DIR = pathlib.Path(app.master.__file__).parent


# ── 스캐너 ──────────────────────────────────────────────────────────────────


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for base in _SCAN_DIRS:
        out.extend(sorted((root / base).rglob("*.py")))
    return out


def _scan(root: pathlib.Path) -> dict[str, list[str]]:
    """옛 패키지 이름을 적은 줄 → 파일별로 모은다.

    ★ **AST 가 아니라 글자로 훑는다.** `import` 문만 보면 `importlib.import_module`
      이나 문자열로 적은 모듈 이름을 놓친다. 옛 이름은 주석에도 남으면 안 된다 —
      남으면 다음 사람이 그 자리가 아직 있는 줄 안다.
    """
    hits: dict[str, list[str]] = {}
    for path in _python_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - 읽히지 않는 파일
            continue
        found = [line.strip() for line in text.splitlines() if _OLD_PACKAGE in line]
        if found:
            hits[path.relative_to(root).as_posix()] = found
    return hits


# ── ① 옛 이름이 남아 있지 않다 ──────────────────────────────────────────────


def test_옛_패키지를_적은_곳이_없다():
    """🔴 **이 파일의 주장이다.**

    한 곳이라도 남으면 `ModuleNotFoundError` 가 그 줄이 도는 날에야 나온다.
    """
    hits = _scan(_ROOT)

    assert not hits, (
        f"옛 패키지 이름이 남아 있다: {hits}. "
        f"마스터 새 자리로 고쳐라 (이 파일 docstring 의 대응표를 본다)"
    )


def test_옛_폴더가_없다():
    """이름을 다 고쳐도 빈 폴더가 남으면 다음 사람이 거기에 파일을 다시 넣는다."""
    leftovers = [
        d.relative_to(_ROOT).as_posix()
        for d in (_ROOT / "app" / _OLD_DIRNAME, _ROOT / "tests" / _OLD_DIRNAME)
        if d.exists()
    ]

    assert not leftovers, f"옛 폴더가 남아 있다: {leftovers}"


# ── ② 스캐너가 정말 파일을 읽는가 ───────────────────────────────────────────


def test_스캐너가_파일을_실제로_읽는다():
    """🔴 **0건을 세면 위 검사가 공짜로 초록이 된다.**

    `_SCAN_DIRS` 에 오타가 나거나 폴더가 옮겨지면 `rglob` 이 빈 목록을 돌려주고,
    위 검사는 *"옛 참조가 없다"* 가 아니라 *"아무것도 안 봤다"* 를 초록으로 보고한다.
    **무엇을 봤는지를 먼저 잰다.**
    """
    seen = {p.relative_to(_ROOT).as_posix() for p in _python_files(_ROOT)}

    assert len(seen) > 100, f"훑은 파일이 {len(seen)}개뿐이다 — 스캐너가 폴더를 못 찾는다"
    assert "app/main.py" in seen, "앱 진입점을 안 훑었다"
    assert "app/master/band.py" in seen, "옮겨 온 파일을 안 훑었다"
    assert "tests/master/test_orchestrator_is_gone.py" in seen, "이 파일 자신을 안 훑었다"


def test_스캐너가_심어_둔_옛_참조를_잡는다(tmp_path):
    """★ **읽기만 하고 못 잡으면 같은 값이다.** 심어 놓고 잡히는지 본다.

    ⚠️ 저장소를 더럽히지 않는다 — `tmp_path` 에 같은 구조를 지어 같은 함수를 돌린다.
    """
    for base in _SCAN_DIRS:
        (tmp_path / base).mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "planted.py").write_text(
        f"from {_OLD_PACKAGE}.band import clip_all\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "clean.py").write_text("from app.master.band import clip_all\n", "utf-8")

    hits = _scan(tmp_path)

    assert set(hits) == {"app/planted.py"}, f"심어 둔 것만 잡혀야 한다: {hits}"


# ── ③ 마스터의 공용 계약 의존 ───────────────────────────────────────────────

#: 공용 계약. 마스터가 이것을 쓰는 것은 **정상**이다 — 어휘가 갈라지지 않으려면 써야 한다.
SHARED = ("app.contracts.core",)


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return out


def _master_imports() -> dict[str, set[str]]:
    return {path.name: _imported_modules(path) for path in sorted(_MASTER_DIR.glob("*.py"))}


def test_공용_계약을_쓰는_파일이_늘지_않는다():
    """★ **이 목록이 줄어드는 것은 좋고 늘어나는 것은 검토가 필요하다.**

    지금 값을 고정해 둔다. 새 파일이 계약을 하나 더 부르는 것은 자연스럽지만,
    **모르는 사이에 늘어나는 것**과 정하고 늘리는 것은 다르다.

    🟢 **2026-09-03 에 경로가 바뀌었다.** 값은 같은 여섯 파일이고 가리키는 자리만
      옛 자리에서 `app.contracts.core` 로 옮겨졌다. **쓰는 것이 문제가 아니라 자리가
      문제였다**는 것이 이 대조로 보인다.

    ★ **2026-09-07 에 다섯이 늘었다** — `band.py`·`outbound.py`·`cycle.py`·
      `cycle_graph.py`·`cycle_graph_b.py`. 새로 계약을 부르기 시작한 것이 아니라
      **옛 폴더에서 걸어 들어온 것**이다. 그 파일들은 옮기기 전에도 같은 계약을
      같은 자리에서 읽고 있었다 — 이 판이 로직을 한 줄도 안 바꿨다는 것이 여기서
      보인다. 늘어난 다섯이 정확히 옮겨 온 파일과 같다.
    """
    users = {
        name: sorted(mods & set(SHARED))
        for name, mods in _master_imports().items()
        if mods & set(SHARED)
    }

    # ★ 2026-09-02 에 셋이 빠졌다 — decision_service · persistence · service 가
    #   옛 공용 실행이력을 부르던 자리다. 마스터가 자기 표로 나오면서 없어졌다.
    # ★ 2026-09-03 에 commitment.py 가 들어왔다 — 피마늘을 빼면서 `ITEM_CODES` 를
    #   계약에서 가져오게 했다. 품목 목록을 여기서 다시 세던 것이 어긋남의 뿌리였다.
    # ★ 2026-09-06 에 sales_flow.py 가 들어왔다 — 판매 Flow 골격이 부서 조정안을
    #   `SuggestedAdjustment` 표준형으로 나른다.
    assert users == {
        "band.py": ["app.contracts.core"],
        "commitment.py": ["app.contracts.core"],
        "critic_bridge.py": ["app.contracts.core"],
        "cycle.py": ["app.contracts.core"],
        "cycle_graph.py": ["app.contracts.core"],
        "cycle_graph_b.py": ["app.contracts.core"],
        "envelope.py": ["app.contracts.core"],
        "flow.py": ["app.contracts.core"],
        "outbound.py": ["app.contracts.core"],
        "router.py": ["app.contracts.core"],
        "sales_flow.py": ["app.contracts.core"],
        "schemas.py": ["app.contracts.core"],
        "verifier.py": ["app.contracts.core"],
    }, f"공용 계약 의존이 바뀌었다 — 의도한 변경이면 이 기대값을 같이 고친다: {users}"


def test_공용_계약이_실재하는_모듈이다():
    """★ 오타로 목록이 비면 위 검사가 **아무것도 안 막는다.**"""
    for module in SHARED:
        path = _ROOT / pathlib.Path(*module.split(".")).with_suffix(".py")
        assert path.exists(), f"목록에 없는 모듈: {module} ({path})"
