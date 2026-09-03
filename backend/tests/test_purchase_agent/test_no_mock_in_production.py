"""운영에서 mock 을 집으면 막는다 (2026-09-03 · 사용자 지시).

> ML DB 가 문제가 되면 작동이 되면 안 된다. **무조건 오류가 나고 문제가 되었다는 것이
> 나와야 한다.**

🔴 **전에는 조용히 mock 으로 떨어졌습니다.**

```text
실 공급자를 안 꽂았다
  → mock 시세를 쓴다
  → 실 ML 예측에서 나온 상한과 비교한다
  → 전 안이 컷된다                      ← #226 이 고친 그것
```

★ 이 파일은 **막는 자리 자체**를 잽니다. `#226` 은 배선 하나를 고쳤고 여기는
  *"안 꽂으면 아예 못 돈다"* 를 고정합니다 — 다음 포트가 늘어도 같은 규칙이 섭니다.

⚠️ **테스트에서는 열려 있어야 합니다.** 결정론 스위트 전량이 mock 으로 돌고,
  DB 에 묶이면 사내망 밖에서 전원 빨간불이 됩니다 (`pyproject.toml` 규약).
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path

import pytest

from app.purchase_agent import ports
from app.purchase_agent.ports import MockNotAllowed, _no_mock_in_production, _under_pytest

AS_OF = date(2025, 12, 31)

#: mock 을 백엔드로 갖는 포트 전부. **늘어나면 여기도 늘어야 한다.**
_MOCK_BACKED = (
    "get_forecast",
    "get_market_quotes",
    "get_inventory",
    "get_confirmed_orders",
    "get_projected_cash_min",
    "get_snapshot_extras",
    "get_context_docs",
)


def test_테스트_안에서는_열려_있다():
    """★ 이것이 먼저다. 막기만 하고 열지 않으면 스위트가 통째로 죽는다."""
    assert _under_pytest()
    _no_mock_in_production("아무거나")  # 예외가 안 난다


def test_운영에서는_막힌다(monkeypatch):
    """🔴 pytest 신호를 지우면 — 그것이 운영이다."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("app.purchase_agent.ports.sys.modules", {})

    with pytest.raises(MockNotAllowed, match="mock 뿐이라 운영에서 쓸 수 없다"):
        _no_mock_in_production("ML 예측")


def test_막을_때_무엇이_막혔는지_말한다(monkeypatch):
    """*"막혔다"* 만 내면 다음에 무엇을 꽂아야 하는지 모른다."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("app.purchase_agent.ports.sys.modules", {})

    with pytest.raises(MockNotAllowed) as caught:
        _no_mock_in_production("등급별 시세(get_market_quotes)")

    assert "get_market_quotes" in str(caught.value)


@pytest.mark.parametrize("name", _MOCK_BACKED)
def test_mock_백엔드_포트가_전부_가드를_지난다(name: str):
    """AST 로 함수 본문을 본다.

    ⚠️ **호출해 보는 것으로는 부족하다.** 테스트 안에서는 가드가 열려 있어 예외가
      안 나므로, *"가드가 있는가"* 를 값이 아니라 **코드로** 봐야 한다.
    """
    source = inspect.getsource(getattr(ports, name))
    tree = ast.parse(source.lstrip())
    guarded = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_no_mock_in_production"
        for node in ast.walk(tree)
    )
    assert guarded, f"{name} 이 mock 을 가드 없이 돌려준다"


def test_mock_을_돌려주는_곳이_전부_목록에_있다():
    """🔴 **새 포트가 늘면 여기가 운다.**

    위 검사는 목록에 있는 것만 본다. 목록에 없는 포트가 mock 을 집으면 아무도 모른다.
    """
    source = Path(inspect.getsourcefile(ports)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        touches_mock = any(
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "mocks"
            for sub in ast.walk(node)
        )
        if touches_mock and node.name not in _MOCK_BACKED:
            offenders.append(node.name)

    assert not offenders, f"mock 을 집는데 목록에 없다: {offenders}"


def test_실_공급자를_꽂으면_가드를_안_탄다():
    """★ 막는 것은 mock 이지 포트가 아니다. 실 공급자는 그대로 돈다."""

    def fake_source(item: str, as_of: date) -> list[dict]:
        return [{"market": "가락", "grade": "특", "price": 824}]

    got = ports.get_market_quotes("배추", AS_OF, source=fake_source)

    assert got == [{"market": "가락", "grade": "특", "price": 824}]
