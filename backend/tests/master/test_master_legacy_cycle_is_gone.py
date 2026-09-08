"""**옛 사이클 3파일은 `app/master/` 에 없다** (2026-09-08).

`test_orchestrator_is_gone.py` 와 같은 규율이다 — 저기는 *"옛 폴더가 없다"* 이고
여기는 *"그 폴더에서 걸어 들어온 죽은 파일이 없다"* 다.

```text
app/master/cycle.py          344줄   앱 진입점에서 도달 0
app/master/cycle_graph.py    419줄   앱 진입점에서 도달 0
app/master/cycle_graph_b.py  327줄   앱 진입점에서 도달 0
```

🔴 **지우지 못하던 이유는 테스트였다.** `tests/master/critic/test_critic_v0_4.py`
  139건이 이 셋으로 **Critic 에게 먹일 시나리오를 지었다.** 그래서 앱에서 죽은 채로
  남아 있었고, 파일 머리에 *"아직 지우지 못한다"* 가 붙어 있었다.

★ **삭제가 아니라 이사였다.** 시나리오를 짓는 일은 프로덕션이 아니라 **픽스처**다.
  테스트가 실제로 부르던 것만 `tests/master/critic/cycle_harness.py` 로 옮겼고
  (`fixtures.py` · `fixtures_cycle_b.py` · `run_day_stub.py` 옆이 제자리다),
  안 부르던 노드·러너·Protocol 은 같이 버렸다. **139건은 한 건도 안 줄었다.**

⚠️ **`cycle_` 접두는 "옮겨 온 묶음"이라는 뜻이지 "죽었다"는 뜻이 아니다.**
  `cycle_schemas.py` · `cycle_persistence.py` · `cycle_run_repository.py` ·
  `cycle_llm/` 은 **살아 있다** — `app/master/critic/router.py` 와
  `app/master/critic/schemas.py` 가 부른다. 접두만 보고 지우지 않게 아래에서
  같이 잠근다.
"""

from __future__ import annotations

import ast
import pathlib

import app.master

#: 지운 파일. 여기 이름이 `app/master/` 에 다시 나타나면 아래 검사가 운다.
_GONE = frozenset({"cycle.py", "cycle_graph.py", "cycle_graph_b.py"})

#: 🟢 **지우면 안 되는 것.** 앱이 부르는 자리가 있다 — 접두가 같을 뿐이다.
_ALIVE = ("cycle_schemas.py", "cycle_persistence.py", "cycle_run_repository.py")

#: 받침대가 옮겨 간 자리.
_HARNESS = "tests/master/critic/cycle_harness.py"

_MASTER = pathlib.Path(app.master.__file__).parent
_ROOT = _MASTER.parent.parent
_APP = _ROOT / "app"


def _master_module_names(root: pathlib.Path) -> set[str]:
    """`app/master/` 바로 밑의 파이썬 파일 이름."""
    return {p.name for p in root.glob("*.py")}


# ── ① 지운 셋이 다시 생기지 않는다 ──────────────────────────────────────────


def test_옛_사이클_파일이_다시_생기지_않는다():
    """🔴 **이 파일의 주장이다.**

    다시 생기면 앱 폴더에 또 죽은 코드가 눕고, 다음 사람은 그것이 도는 줄 안다.
    같은 일이 필요해지면 픽스처(`cycle_harness.py`)를 고치는 것이 답이다.
    """
    back = sorted(_master_module_names(_MASTER) & _GONE)

    assert not back, (
        f"지운 사이클 파일이 `app/master/` 에 다시 있다: {back}. "
        f"시나리오 조립이 필요하면 {_HARNESS} 를 고친다"
    )


# ── ② 스캐너가 정말 세고 있는가 ─────────────────────────────────────────────


def test_스캐너가_마스터_파일을_실제로_센다():
    """🔴 **0개를 세면 위 검사가 공짜 초록이 된다.**

    `app.master` 가 옮겨지거나 `glob` 패턴이 어긋나면 빈 집합이 돌아오고,
    위 검사는 *"지운 파일이 없다"* 가 아니라 *"아무것도 안 봤다"* 를 초록으로
    보고한다. **무엇을 봤는지를 먼저 잰다.**
    """
    seen = _master_module_names(_MASTER)

    assert len(seen) > 10, f"마스터 파일을 {len(seen)}개밖에 못 봤다 — 스캐너가 폴더를 못 찾는다"
    assert "flow.py" in seen, "마스터의 산 경로(flow.py)를 안 봤다"
    assert "commitment.py" in seen, "승인 약정의 주인(commitment.py)을 안 봤다"


def test_스캐너가_심어_둔_파일을_잡는다(tmp_path):
    """★ **세기만 하고 못 잡으면 같은 값이다.** 심어 놓고 잡히는지 본다.

    ⚠️ 저장소를 더럽히지 않는다 — `tmp_path` 에 같은 구조를 지어 같은 함수를 돌린다.
    ⚠️ 미끼도 같이 심는다. `cycle_schemas.py` 까지 잡히면 이 검사는 접두만 보는
      것이고, 그건 **살아 있는 파일을 지우라고 말하는 검사**가 된다.
    """
    (tmp_path / "cycle_graph.py").write_text("# 되살아난 파일\n", encoding="utf-8")
    (tmp_path / "cycle_schemas.py").write_text("# 살아 있는 파일\n", encoding="utf-8")
    (tmp_path / "flow.py").write_text("# 산 경로\n", encoding="utf-8")

    caught = _master_module_names(tmp_path) & _GONE

    assert caught == {"cycle_graph.py"}, f"심어 둔 것만 잡혀야 한다: {sorted(caught)}"


# ── ③ 접두가 같다고 같이 지우지 않는다 ──────────────────────────────────────


def test_살아_있는_cycle_모듈은_그대로_있다():
    """🟢 **`cycle_` 은 옮겨 온 묶음이라는 뜻이지 죽었다는 뜻이 아니다.**

    ```text
    app/master/critic/router.py:21   from app.master.cycle_persistence import record
    app/master/critic/router.py:22   from app.master.cycle_run_repository import ...
    app/master/critic/schemas.py:20  from app.master.cycle_schemas import ...
    ```

    앱 경로에서 부르는 자리가 있으므로 이 셋은 죽지 않았다.
    """
    missing = [name for name in _ALIVE if not (_MASTER / name).exists()]

    assert not missing, (
        f"살아 있는 사이클 모듈이 없어졌다: {missing}. "
        f"접두가 같다고 같이 지운 것이 아닌지 본다 — 부르는 자리가 앱에 있다"
    )
    assert (_MASTER / "cycle_llm").is_dir(), "cycle_llm/ 이 없어졌다 — cycle_schemas 가 쓴다"


# ── ④ 받침대는 픽스처 자리에 있고, 앱은 그것을 안 부른다 ────────────────────


def test_옮긴_받침대가_제자리에_있다():
    """★ **테스트를 잃지 않았다는 것이 이 판의 조건이었다.**

    받침대가 사라지면 139건이 통째로 사라지고, 스위트는 **줄어든 채로 초록**이 된다.
    """
    harness = _ROOT / _HARNESS

    assert harness.exists(), f"받침대가 없다: {_HARNESS}"

    text = harness.read_text(encoding="utf-8")
    for name in (
        "class CycleHooks",
        "def run_subcycle",
        "def run_day",
        "def build_commitment",
        "def node_t3_combine",
        "def build_cycle_b_hooks",
    ):
        assert name in text, f"받침대에 `{name}` 이 없다 — Critic 테스트가 그것을 부른다"


def _imports_harness(path: pathlib.Path) -> bool:
    """그 파일이 받침대를 **임포트**하는가.

    ⚠️ 글자로 훑지 않는다. 문서에서 *"받침대는 저기 있다"* 고 가리키는 것과 코드가
      그것을 **부르는** 것은 다르다 — `app/master/commitment.py` 가 실제로 앞엣것을
      한다. 글자로 재면 그 문장이 위반으로 잡히고, 그러면 다음 사람이 검사를 피해
      문서를 지운다.
    """
    stem = pathlib.Path(_HARNESS).stem
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == stem:
            return True
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == stem for a in node.names):
            return True
    return False


def test_앱이_받침대를_부르지_않는다():
    """🔴 **픽스처는 테스트에만 있는다.** 운영 경로가 이 자리를 부르면 방향이 뒤집힌다.

    받침대는 옛 사이클을 되살려 둔 것이지 마스터의 산 경로가 아니다. 앱이 여기를
    부르는 순간, 지운 코드가 `tests/` 안에서 되살아나 프로덕션을 돈다.
    """
    scanned = sorted(_APP.rglob("*.py"))

    # 🔴 앱 폴더를 못 찾으면 부르는 곳 0건이 **아무것도 안 봤다**는 뜻이 된다.
    assert any(p.name == "main.py" for p in scanned), (
        f"앱 진입점을 안 훑었다 — 스캐너가 {_APP} 를 못 찾는다"
    )

    callers = [p.relative_to(_ROOT).as_posix() for p in scanned if _imports_harness(p)]

    assert not callers, f"앱이 테스트 받침대를 부른다: {callers}"


def test_받침대_임포트_스캐너가_심어_둔_것을_잡는다(tmp_path):
    """🔴 **자기 생존 검사.** 위 검사는 0건을 세고도 초록이 된다.

    ⚠️ 문서에서 이름만 언급한 파일은 **안 잡혀야 한다** — 그게 위 스캐너가 글자가
      아니라 임포트를 보는 이유다.
    """
    stem = pathlib.Path(_HARNESS).stem
    calling = tmp_path / "calling.py"
    calling.write_text(f"from {stem} import run_day\n", encoding="utf-8")
    mentioning = tmp_path / "mentioning.py"
    mentioning.write_text(f'"""받침대는 {stem}.py 에 있다."""\n', encoding="utf-8")

    assert _imports_harness(calling), "임포트를 못 잡으면 위 검사는 공짜 초록이다"
    assert not _imports_harness(mentioning), "문서에서 가리키기만 한 것은 부르는 것이 아니다"
