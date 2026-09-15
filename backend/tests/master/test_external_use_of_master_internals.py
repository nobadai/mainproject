"""마스터 **내부**를 밖에서 부르는 곳을 고정한다.

2026-09-03 · 매입 질의(`_purchase_input` 공개 여부)에 대한 답의 절반.

🔴 **바꾸는 판은 마스터 것인데 고치는 손은 남의 파트다.** 그리고 마스터는 그 사실을
**모르는 채로** 바꿀 수 있다 — 마스터 스위트는 안 깨지니까.

```text
전     마스터가 이름을 바꾼다 → 남의 스위트가 깨진다 → 남이 고친다
지금   마스터가 이름을 바꾼다 → 이 파일이 먼저 운다 → 마스터가 같이 고친다
```

★ **막지 않는다.** 이름을 바꾸는 것은 여전히 마스터 자유다. 바꾸는 순간
  *"남의 픽스처도 같이 고쳐야 한다"* 가 마스터 화면에 뜨는 것이 이 파일의 일이다.

★ **부르는 쪽을 나무라는 파일이 아니다.** 매입 픽스처가 `_purchase_input` 을 부르는
  것은 **옳은 선택**이다 — 손으로 조립하면 조용히 어긋나고, 실제로 `#175`
  (`asdict` → `_wire`)가 그렇게 어긋났는데 아무도 못 봤다. 비공개를 부르면 이름이
  바뀔 때 즉시 터진다. `06` 문서 §7 이 *"파트 간 관통 검사가 없다"* 를 가장 큰
  구멍으로 적어 뒀고, 그 픽스처가 그 구멍을 메운 첫 사례다.

⚠️ **`test_retry_cap_ownership.py` 와 같은 규율이다.**

```text
값이 두 곳에 있으면 조용히 갈린다     → 대조는 테스트가 한다
이름이 밖에서 불리면 모르고 바꾼다    → 대조는 테스트가 한다
```
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.master
from app.master import critic_bridge
from app.master.flow import ProcurementFlow

_ROOT = Path(app.master.__file__).parent.parent.parent

#: 마스터가 **자기 것을 자기가 쓰는** 곳. 여기는 대상이 아니다.
_OWN = "tests/master"

#: 밖에서 만지는 마스터 내부. **줄 번호는 안 적는다** — 편집마다 흔들린다.
#:
#: 🔴 **매입만이 아니다.** 매입이 물어와서 본 것인데 물류가 둘 더 부르고 있었고
#:   아무도 안 물었다. *"묻는 쪽만 보이는"* 것이 이 검사가 필요한 이유다.
EXPECTED: dict[str, set[str]] = {
    "tests/logistics/test_logistics_adapter.py": {
        "app.master.critic_bridge._replies_in",
        "app.master.critic_bridge._dept_meta_in",
    },
    "tests/test_purchase_agent/test_feedback_intake.py": {
        "ProcurementFlow._purchase_input",
        "ProcurementFlow.suggested_adjustments",
    },
}

#: `ProcurementFlow` 인스턴스에서 밖이 만지는 이름. 실재 여부를 따로 확인한다.
_FLOW_TOUCHED = ("_purchase_input", "suggested_adjustments")

#: `critic_bridge` 의 비공개 함수 중 밖이 부르는 것.
_BRIDGE_TOUCHED = ("_replies_in", "_dept_meta_in")

_FLOW_SOURCE = _ROOT / "app" / "master" / "flow.py"


def _flow_defines(name: str) -> bool:
    """`ProcurementFlow` 가 그 이름을 갖는가. **클래스 속성만 보면 안 된다.**

    🔴 `hasattr(ProcurementFlow, "suggested_adjustments")` 는 **False** 다.
      `__init__` 안에서 대입으로만 생기는 인스턴스 속성이라 클래스에는 없다.
      처음 이 검사를 `hasattr` 로 썼다가 그 자리에서 빨간불이 났다.

    ⚠️ **그것이 바로 이 이름이 위험한 이유다** — 선언이 없으니 오타 대입이
      새 속성을 만들고, 지워도 아무것도 안 터진다.

    그래서 소스를 읽어 `self.<name> = ...` 대입까지 본다.
    """
    if hasattr(ProcurementFlow, name):
        return True
    tree = ast.parse(_FLOW_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == name
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    return True
    return False


def _scan() -> dict[str, set[str]]:
    """`tests/` 에서 마스터 내부를 만지는 자리를 모은다.

    셋을 본다.

    ```text
    ① app.master.* 에서 언더스코어 이름을 import   던더는 뺀다 (남의 것이 아니다)
    ② ProcurementFlow 인스턴스의 언더스코어 속성
    ③ ProcurementFlow 인스턴스에 대한 대입          공개 이름이어도 내부 상태다
    ```

    🔴 **③ 이 ① · ② 보다 위험하다.** 언더스코어를 지우면 `AttributeError` 로 즉시
      터지는데, `suggested_adjustments` 를 지우면 **파이썬이 새 속성을 만들어 준다.**
      아무것도 안 터지고, `_purchase_input` 이 안 읽으므로 조정안이 빈 목록으로
      나가고, 픽스처는 통과하고 **잠그던 것만 사라진다.**

    ⚠️ ③ 은 `ProcurementFlow` 를 import 한 파일에서만 본다. 안 그러면 남의 객체의
      같은 이름을 오탐한다 (`test_logistics_adapter.py:56` 의 `item` 대입이 그 예다).
    """
    found: dict[str, set[str]] = {}
    for path in sorted(_ROOT.joinpath("tests").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel.startswith(_OWN):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        hits: set[str] = set()
        imports_flow = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith("app.master"):
                    continue
                for alias in node.names:
                    if alias.name == "ProcurementFlow":
                        imports_flow = True
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        hits.add(f"{node.module}.{alias.name}")

        if imports_flow:
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                    and _flow_defines(node.attr)
                ):
                    hits.add(f"ProcurementFlow.{node.attr}")
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr in _FLOW_TOUCHED:
                            hits.add(f"ProcurementFlow.{target.attr}")

        if hits:
            found[rel] = hits
    return found


# ── ① 핵심 — 목록이 조용히 늘지 않는다 ─────────────────────────────────────


def test_마스터_내부를_밖에서_부르는_곳을_고정한다():
    """🔴 **이 파일의 주장이다.**

    늘어나는 것은 나쁜 것이 아니다. **모르는 사이에** 늘어나는 것이 나쁘다.
    새로 생기면 여기서 빨간불이고, 그때 `EXPECTED` 를 같이 고치면 된다 —
    고치는 순간 마스터가 그 결합을 **알게 된다.**
    """
    assert _scan() == EXPECTED, (
        "마스터 내부를 밖에서 부르는 자리가 달라졌다. 의도한 변경이면 EXPECTED 를 "
        "같이 고치고, 이름을 바꾼 것이면 부르는 쪽 픽스처도 같은 PR 에서 고친다"
    )


def test_고정_목록이_비어_있지_않다():
    """전제 단언. 스캔이 빗나가 늘 빈 dict 를 돌려주면 위 검사가 **아무것도 안 막는다.**

    `06` 문서 §4 — *"전제를 테스트 안에서 단언한다"*.
    """
    assert EXPECTED, "고정 목록이 비면 위 검사는 항상 통과한다"
    assert _scan(), "스캔이 0건이다 — 경로나 파서가 빗나갔다"


# ── ② 이름이 실재하는가 — 여기가 실제 방어다 ────────────────────────────────


def test_밖이_부르는_Flow_이름이_실재한다():
    """🔴 **이름을 바꾸면 여기가 운다.**

    위 스캔만으로는 부족하다. `_purchase_input` 을 `_build_payload` 로 바꿔도
    매입 파일은 여전히 옛 이름을 적고 있어 스캔 결과가 `EXPECTED` 와 **같다.**
    실재 여부를 따로 봐야 *"부르는데 없다"* 가 잡힌다.

    ⚠️ **매입 스위트를 안 돌려도 마스터 스위트에서 먼저 운다** — 그것이 이 파일의
      목적이다.

    🔴 처음에 `hasattr` 로 썼다가 이 검사가 **그 자리에서 빨간불**이 됐다 —
      `suggested_adjustments` 가 클래스에 없는 인스턴스 속성이어서다.
      `_flow_defines` 가 `__init__` 대입까지 보는 이유가 그것이고, **선언이 없다는
      사실 자체가 아래 `test_suggested_adjustments_는_지워도_안_터진다` 의 전제**다.
    """
    missing = [name for name in _FLOW_TOUCHED if not _flow_defines(name)]
    assert not missing, (
        f"ProcurementFlow 에 {missing} 이 없다. 밖에서 부르고 있다 — "
        f"이름을 바꿨다면 tests/test_purchase_agent/test_feedback_intake.py 도 "
        f"같은 PR 에서 고친다"
    )


def test_밖이_부르는_critic_bridge_이름이_실재한다():
    """물류가 `_replies_in` · `_dept_meta_in` 을 직접 부른다.

    ⚠️ **매입이 물어와서 알게 된 것이고, 물류 쪽은 아무도 안 물었다.**
      이 검사가 그 비대칭을 없앤다 — 묻든 안 묻든 마스터가 안다.
    """
    missing = [name for name in _BRIDGE_TOUCHED if not hasattr(critic_bridge, name)]
    assert not missing, (
        f"critic_bridge 에 {missing} 이 없다. 밖에서 부르고 있다 — "
        f"이름을 바꿨다면 tests/logistics/test_logistics_adapter.py 도 같이 고친다"
    )


def test_suggested_adjustments_는_지워도_안_터진다():
    """🔴 **왜 ③ 을 따로 세는지**를 그 자리에서 보인다.

    언더스코어 이름은 지우면 `AttributeError` 로 즉시 터진다. 그런데 인스턴스
    속성은 **파이썬이 대입만으로 새로 만들어 준다** — `__slots__` 가 없으므로.

    그래서 마스터가 `suggested_adjustments` 를 다른 이름으로 바꾸면 매입 픽스처는
    **조용히 통과하고 잠그던 것만 사라진다.** 이 파일이 그 자리를 대신 지킨다.
    """
    assert not hasattr(ProcurementFlow, "__slots__"), (
        "__slots__ 가 생겼다 — 그러면 오타 대입이 즉시 터지므로 이 위험이 사라진다. "
        "좋은 변화이니 이 검사와 EXPECTED 의 ③ 항목을 같이 정리하라"
    )
