"""마스터는 **오케스트레이터 런타임으로 가지 않는다.**

지시 2026-09-01 — *"이제부터 모든 경로는 orchestrator/ 쪽으로 가지 않게"*.

실측해 보니 **런타임 쪽은 이미 0건**이었다. 그래서 이 테스트는 고치는 것이 아니라
**되돌아가지 못하게 잠그는 것**이다.

```text
app/orchestrator/
  band.py · cycle.py · outbound.py          런타임 — 마스터가 안 쓴다  ← 여기를 잠근다
  graph.py · graph_b.py · graph_langgraph.py  오케 selector — 앱에서 안 돈다
  interpretation.py · schemas.py

  run_repository.py    옛 공용 실행이력 — **마스터는 2026-09-02 에 빠져나왔다**
  persistence.py       Critic 이 쓴다 (`app/critic/router.py`)

  contracts_core.py    공용 계약 어휘 — 재무·물류·매입·Critic 이 전부 쓴다
```

★ **`run_repository` 가 공용에서 금지로 옮겨왔다 (2026-09-02).**
  마스터가 자기 표(`master_agent_runs`)와 자기 저장소(`app/master/run_repository.py`)를
  가지면서 옛 모듈을 쓸 이유가 없어졌다. Critic 은 옛 표를 그대로 쓰므로 옛 모듈도
  남는다 — 남의 코드를 건드리지 않는다.

  표를 나눈 이유는 어휘의 소유였다. 조회(`STATUS`)를 이력에 남기려 해도 CHECK 를
  고치려면 오케·Critic 행의 뜻까지 건드려야 해서 지금까지 못 했다.

🟢 **`contracts_core` 하나가 남아 있었고, 이제 없다** (2026-09-03).

  전에는 이렇게 적어 뒀다.

  > 마스터만 빠지면 계약 어휘가 갈라져서다. **옮겨야 할 것은 모듈의 위치**이지
  > 마스터의 사용이 아니고, 그건 다섯 파트가 같이 움직여야 하는 변경이다.

  다섯 파트가 같이 움직였고 계약이 `app/contracts/core.py` 로 갔다. 그래서 이
  파일의 방향을 뒤집는다 — **`app.orchestrator.contracts_core` 는 이제 금지**이고,
  마스터가 공용 계약을 쓰는 것 자체는 새 자리로 정상이다.

  ⚠️ **옛 경로는 아직 살아 있다** (재수출 shim). 재무·매입·물류가 아직 쓴다.
    마스터가 거기로 돌아갈 이유만 없어졌다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.master

_MASTER_DIR = Path(app.master.__file__).parent

#: 마스터가 어떤 이유로도 부르지 않는다. 클리핑·밴드 결합·승인 변환은 오케 소유고,
#: M-1 관통은 그 경로를 안 쓴다 (`_acceptable` 이 부서 판정 하나로 정한다).
FORBIDDEN = (
    "app.orchestrator.band",
    "app.orchestrator.cycle",
    "app.orchestrator.outbound",
    "app.orchestrator.graph",
    "app.orchestrator.graph_b",
    "app.orchestrator.graph_langgraph",
    "app.orchestrator.interpretation",
    # 2026-09-02 — 마스터가 자기 표로 나오면서 금지로 옮겼다.
    # 되돌아가면 실행이력이 두 표로 갈리고, 결정의 FK 가 어느 표를 가리키는지 흐려진다.
    "app.orchestrator.run_repository",
    "app.orchestrator.persistence",
    # 2026-09-03 — 계약이 app/contracts/core.py 로 갔다. 옛 자리는 재수출 shim 이고
    # ④ 에서 지운다. 마스터가 거기로 돌아가면 그날 같이 깨진다.
    "app.orchestrator.contracts_core",
)

#: 공용 계약. 마스터가 이것을 쓰는 것은 **정상**이다 — 어휘가 갈라지지 않으려면 써야 한다.
#:
#: 🟢 자리가 바로잡혔다 (2026-09-03). 전에는 `app/orchestrator/` 아래라 *"마스터가
#: 오케를 임포트하는"* 모양이었고, 지금은 어느 파트도 아닌 곳에 있다.
SHARED = ("app.contracts.core",)


def _imported_modules(path: Path) -> set[str]:
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


def test_런타임을_부르지_않는다():
    offenders = {
        name: sorted(mods & set(FORBIDDEN))
        for name, mods in _master_imports().items()
        if mods & set(FORBIDDEN)
    }

    assert not offenders, (
        f"마스터가 오케 런타임을 부른다: {offenders}. "
        "클리핑·밴드 결합·승인 변환은 오케 소유이고 M-1 관통은 그 경로를 쓰지 않는다."
    )


def test_공용_계약을_쓰는_파일이_늘지_않는다():
    """★ **이 목록이 줄어드는 것은 좋고 늘어나는 것은 검토가 필요하다.**

    지금 값을 고정해 둔다. 새 파일이 계약을 하나 더 부르는 것은 자연스럽지만,
    **모르는 사이에 늘어나는 것**과 정하고 늘리는 것은 다르다.

    🟢 **2026-09-03 에 경로가 바뀌었다.** 값은 같은 여섯 파일이고 가리키는 자리만
      `app.orchestrator.contracts_core` → `app.contracts.core` 로 옮겨졌다.
      **쓰는 것이 문제가 아니라 자리가 문제였다**는 것이 이 대조로 보인다.
    """
    users = {
        name: sorted(mods & set(SHARED))
        for name, mods in _master_imports().items()
        if mods & set(SHARED)
    }

    # ★ 2026-09-02 에 셋이 빠졌다 — decision_service · persistence · service 가
    #   `app.orchestrator.run_repository` 를 부르던 자리다. 마스터가 자기 표로
    #   나오면서 없어졌고, 그 모듈은 이제 FORBIDDEN 이다.
    assert users == {
        "critic_bridge.py": ["app.contracts.core"],
        "envelope.py": ["app.contracts.core"],
        "flow.py": ["app.contracts.core"],
        "router.py": ["app.contracts.core"],
        "schemas.py": ["app.contracts.core"],
        "verifier.py": ["app.contracts.core"],
    }, f"공용 계약 의존이 바뀌었다 — 의도한 변경이면 이 기대값을 같이 고친다: {users}"


def test_옛_자리로_돌아간_파일이_없다():
    """🔴 **shim 이 살아 있어서 돌아가도 안 깨진다** — 그래서 검사가 필요하다.

    `app.orchestrator.contracts_core` 는 아직 동작한다 (재무·매입·물류가 쓴다).
    마스터가 실수로 옛 경로를 적어도 스위트가 초록불이고, **④ 에서 shim 을 지우는
    날 한꺼번에 깨진다.**
    """
    back = {
        name: sorted(mods)
        for name, mods in _master_imports().items()
        if "app.orchestrator.contracts_core" in mods
    }

    assert not back, f"마스터가 옛 계약 자리로 돌아갔다: {back}. app.contracts.core 를 쓴다"


def test_금지_목록이_실재하는_모듈이다():
    """★ 오타로 목록이 비면 위 검사가 **아무것도 안 막는다.**"""
    backend = Path(app.master.__file__).parent.parent.parent

    for module in FORBIDDEN + SHARED:
        path = backend / Path(*module.split(".")).with_suffix(".py")
        assert path.exists(), f"목록에 없는 모듈: {module} ({path})"
