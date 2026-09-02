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

🔴 **`contracts_core` 하나는 이 테스트가 안 막는다.** 마스터만 빠지면 **계약 어휘가
  갈라져서**다 — `Evidence`·`EndCode` 를 마스터가 따로 정의하면 봉투 검증이 남의
  타입을 못 알아본다. 옮겨야 할 것은 **모듈의 위치**이지 마스터의 사용이 아니고,
  그건 다섯 파트가 같이 움직여야 하는 변경이다.
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
)

#: 공용 모듈. 여기 있는 것은 **위치가 잘못된 것**이지 마스터가 잘못 쓰는 것이 아니다.
#:
#: 하나만 남았다 — 계약 어휘라 마스터만 빠질 수 없다 (모듈 헤더 참조).
SHARED = ("app.orchestrator.contracts_core",)


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


def test_공용_모듈_의존은_늘지_않는다():
    """★ **이 목록이 줄어드는 것은 좋고 늘어나는 것은 검토가 필요하다.**

    지금 값을 고정해 둔다. 새 파일이 `contracts_core` 를 하나 더 부르는 것은 자연스럽지만,
    **모르는 사이에 늘어나는 것**과 정하고 늘리는 것은 다르다.
    """
    users = {
        name: sorted(mods & set(SHARED))
        for name, mods in _master_imports().items()
        if mods & set(SHARED)
    }

    # ★ 2026-09-02 에 셋이 빠졌다 — decision_service · persistence · service 가
    #   `app.orchestrator.run_repository` 를 부르던 자리다. 마스터가 자기 표로
    #   나오면서 없어졌고, 그 모듈은 이제 FORBIDDEN 이다.
    #   남은 것은 `contracts_core` 하나뿐이다.
    assert users == {
        "critic_bridge.py": ["app.orchestrator.contracts_core"],
        "envelope.py": ["app.orchestrator.contracts_core"],
        "flow.py": ["app.orchestrator.contracts_core"],
        "router.py": ["app.orchestrator.contracts_core"],
        "schemas.py": ["app.orchestrator.contracts_core"],
        "verifier.py": ["app.orchestrator.contracts_core"],
    }, f"공용 모듈 의존이 바뀌었다 — 의도한 변경이면 이 기대값을 같이 고친다: {users}"


def test_런타임_금지_목록이_실재하는_모듈이다():
    """★ 오타로 목록이 비면 위 검사가 **아무것도 안 막는다.**"""
    orchestrator = Path(app.master.__file__).parent.parent / "orchestrator"

    for module in FORBIDDEN + SHARED:
        stem = module.rsplit(".", 1)[-1]
        assert (orchestrator / f"{stem}.py").exists(), f"목록에 없는 모듈: {module}"
