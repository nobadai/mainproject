"""Critic 은 **마스터 밑에 있다.** 옛 `app/critic/` 은 저장소에 없다.

지시 2026-09-07 — Critic 을 마스터 에이전트의 툴로 쓰기로 확정했고, 구조를 그
확정에 맞춘다. `#372` 가 `app/orchestrator/` 를 마스터로 옮긴 1판이고 이것이 2판이다.

이 파일은 `test_orchestrator_is_gone.py` 를 본떴다 — 재는 대상만 다르고 규율은 같다.

옮긴 자리는 이렇다.

```text
app/critic/      →  app/master/critic/
app/critic/llm/  →  app/master/critic/llm/
tests/critic/    →  tests/master/critic/
```

🔴 **평평하게 펴지 않고 하위 패키지로 넣었다.** `app/master/` 에 이미 같은 이름이
  넷 있다 — `router.py` · `schemas.py` · `service.py` · `llm/`. 그대로 부으면 넷을
  덮어쓴다. 하위 패키지면 이름을 하나도 안 바꾸고 넷 다 안 겹친다.

🟢 **1판이 세운 지연 import 를 되돌렸다.** `#372` 는 `app/critic/schemas.py` 가
  마스터 밑의 `AllocationIn`·`ScenarioIn` 을 읽어 고리가 닫히는 것을 `TYPE_CHECKING`
  + 함수 안 import 로 끊었고, 그 보고서가 근본 원인을 **입력 계약의 소유**로 짚었다.
  Critic 이 같은 패키지 안으로 들어오면서 그 고리가 없어졌다 — `critic_bridge.py` ·
  `verifier.py` 가 이제 최상단에서 들이고 `_default_critic` 래퍼도 지웠다.

⚠️ **DB 표 이름(`orchestrator_agent_runs`)과 `Literal["critic"]` 값들은 안 건드렸다.**
  코드 경로만 옮겼다. 저 값들은 이미 적재된 행이 쓰는 어휘라 이 판의 합의가 아니다.
"""

from __future__ import annotations

import pathlib

import app.master
import app.master.critic

#: 🔴 **일부러 이어 붙인다.** 이 파일에 옛 이름이 통짜로 적혀 있으면 스캐너가
#:   자기 자신을 잡는다. 자기 예외를 두면 이 파일 안에 옛 참조가 되살아나도 안 잡힌다.
#:   그래서 예외를 두는 대신 **리터럴이 없게** 쓴다.
_OLD_PACKAGE = "app" + "." + "critic"

#: 같은 이유. 폴더 이름은 `app/` 와 `tests/` 밑에서 찾는다.
_OLD_DIRNAME = "critic"

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("app", "tests")

_MASTER_DIR = pathlib.Path(app.master.__file__).parent
_CRITIC_DIR = pathlib.Path(app.master.critic.__file__).parent


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

    ★ **새 이름은 안 잡힌다.** 옛 이름은 `app` 다음에 바로 `critic` 이 오는 모양이고,
      새 이름은 그 사이에 `master` 가 낀다. 부분 문자열로 훑어도 서로 안 걸린다.
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
        f"옛 패키지 이름이 남아 있다: {hits}. 마스터 하위(`app/master/critic/`)로 고쳐라"
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
    assert "app/master/critic/critic_v0_4.py" in seen, "옮겨 온 파일을 안 훑었다"
    assert "tests/master/critic/test_critic_v0_4.py" in seen, "옮겨 온 검사를 안 훑았다"
    assert "tests/master/test_critic_is_under_master.py" in seen, "이 파일 자신을 안 훑었다"


def test_스캐너가_심어_둔_옛_참조를_잡는다(tmp_path):
    """★ **읽기만 하고 못 잡으면 같은 값이다.** 심어 놓고 잡히는지 본다.

    ⚠️ 저장소를 더럽히지 않는다 — `tmp_path` 에 같은 구조를 지어 같은 함수를 돌린다.

    ★ 깨끗한 쪽에 **새 경로**를 둔다. 매처가 너무 넓어 새 이름까지 잡으면 여기서
      빨간불이다 — 옛 것만 잡는지와 새 것을 안 잡는지를 한 번에 잰다.
    """
    for base in _SCAN_DIRS:
        (tmp_path / base).mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "planted.py").write_text(
        f"from {_OLD_PACKAGE}.service import run_critic_procurement\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "clean.py").write_text(
        "from app.master.critic.service import run_critic_procurement\n", encoding="utf-8"
    )

    hits = _scan(tmp_path)

    assert set(hits) == {"app/planted.py"}, f"심어 둔 것만 잡혀야 한다: {hits}"


# ── ③ 자리 자체 — 마스터 **밑**이다 ────────────────────────────────────────


def test_크리틱은_마스터의_하위_패키지다():
    """🔴 **평평하게 펴는 것을 막는다.**

    `app.master.critic` 이 import 되는 것만으로는 부족하다 — 파일이 `app/master/`
    바로 밑으로 흩어져도 그 이름은 안 산다. **폴더가 마스터 안에 있고 그 안에
    자기 폴더를 갖는다**는 것이 이 판의 결정이다.
    """
    assert _CRITIC_DIR.parent == _MASTER_DIR, f"Critic 이 마스터 밑이 아니다: {_CRITIC_DIR}"
    assert _CRITIC_DIR.name == _OLD_DIRNAME
    assert (_CRITIC_DIR / "llm" / "__init__.py").exists(), "Critic 의 LLM 층이 같이 안 왔다"


def test_이름이_마스터와_겹치지_않는다():
    """★ **하위 패키지로 넣은 이유가 이것이다.**

    `router.py` · `schemas.py` · `service.py` · `llm/` 이 양쪽에 다 있다. 평평하게
    폈다면 넷이 덮어써졌다. 겹치는 이름이 실제로 있다는 것을 여기서 고정한다 —
    없어지면 *"굳이 하위로 안 넣어도 됐다"* 가 되므로 그때 다시 판단하면 된다.
    """
    master_names = {p.name for p in _MASTER_DIR.glob("*.py")} | {
        d.name for d in _MASTER_DIR.iterdir() if d.is_dir() and (d / "__init__.py").exists()
    }
    critic_names = {p.name for p in _CRITIC_DIR.glob("*.py")} | {
        d.name for d in _CRITIC_DIR.iterdir() if d.is_dir() and (d / "__init__.py").exists()
    }

    assert {"router.py", "schemas.py", "service.py", "llm"} <= (master_names & critic_names), (
        f"겹치던 이름이 달라졌다 — 마스터 {sorted(master_names)} / Critic {sorted(critic_names)}"
    )


def test_지연_import_를_되돌렸다():
    """🟢 **이 판의 이득이다.** 1판이 순환 때문에 미뤄 둔 것을 다시 최상단으로 올렸다.

    ★ **없어진 것을 재는 검사는 되돌아가는 것을 막는다.** 누가 다시 함수 안으로
      내리면 여기서 빨간불이고, 그때는 *"왜 또 순환인가"* 를 먼저 봐야 한다.

    ⚠️ **글자가 아니라 구조로 잰다.** 두 파일의 주석이 1판의 사정을 설명하느라
      그 이름들을 적는다 — 글자로 훑으면 설명 자체가 빨간불이 된다.
    """
    import ast

    import app.master.critic_bridge as bridge
    from app.master import verifier

    for module in (bridge, verifier):
        tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))

        deferred = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "typing"
            for alias in node.names
            if alias.name == "TYPE_CHECKING"
        ]
        assert not deferred, f"{module.__name__} 에 지연 import 블록이 되살아났다"

        top_level = {
            node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
        }
        assert any(m.startswith("app.master.critic.") for m in top_level), (
            f"{module.__name__} 이 Critic 을 최상단에서 안 들인다: {sorted(top_level)}"
        )

    assert not hasattr(verifier, "_default_critic"), "래퍼가 되살아났다 — 순환이 다시 생겼는가"

    from app.master.critic.service import run_critic_procurement

    assert verifier.MasterVerifier().critic is run_critic_procurement, (
        "기본 Critic 이 실제 진입점이 아니다"
    )


def test_기본값_없음과_None_은_다른_뜻이다():
    """🔴 **`None` 을 센티넬로 쓰지 않는다** (`#372` 가 세운 규율).

    `critic=None` 은 *"Critic 을 안 돌렸다"* 는 뜻이고 그 사실이 `skipped` 에 남는다.
    기본값과 뜻이 다르므로 기본값 자리에 `None` 이 오면 안 된다.
    """
    from app.master.verifier import MasterVerifier

    assert MasterVerifier().critic is not None, "기본값이 '안 돌렸다'가 됐다"
    assert MasterVerifier(critic=None).critic is None, "명시한 None 이 기본값에 먹혔다"
