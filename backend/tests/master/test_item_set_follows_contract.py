"""품목 목록의 주인은 계약 하나다 (2026-09-03 피마늘 제외).

🔴 **왜 이 파일이 생겼나.** 피마늘을 뺄 때 품목 목록이 네 곳에 따로 있었다.

    app/contracts/core.py      ITEMS       배추·무·양파·피마늘
    app/ml/schemas.py          ITEMS       배추·무·양파            ← 이미 셋이었다
    app/master/commitment.py   ITEM_CODES  배추·무·양파·피마늘     ← 따로 셌다
    app/master/llm/schemas.py  ItemName    배추·무·양파·피마늘     ← 따로 셌다

**ML 은 여덟 달째 셋이었는데 계약은 넷이었다.** 그 어긋남을 아무도 못 봤고,
그래서 피마늘 관통이 mock 으로 조용히 돌았다 — `_forecast_fallback` 이 메워서
"되는 것처럼" 보였다. 여기 검사들은 그 어긋남이 다시 생기면 운다.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from app.contracts.core import ITEMS

_APP = Path(__file__).resolve().parents[2] / "app"


def test_계약에_피마늘이_없다():
    assert "피마늘" not in ITEMS
    assert ITEMS == ("배추", "무", "양파")


def test_계약_품목은_전부_ML_예측이_있다():
    """🔴 **이것이 뿌리 불변식이다.**

    예측이 없는 품목이 계약에 있으면, 그 품목의 관통은 mock 으로만 돈다.
    실측처럼 보이는 결과가 나오지만 재료가 가짜다 — 2026-08-31·09-03 에
    마스터가 피마늘로 두 번 실측해 두 번 다 잘못된 결론을 냈다.
    """
    from app.ml.schemas import ITEMS as ML_ITEMS

    missing = set(ITEMS) - set(ML_ITEMS)
    assert not missing, f"예측이 없는 품목이 계약에 있다: {sorted(missing)}"


def test_마스터_약정_어휘를_따로_세지_않는다():
    """`ITEM_CODES` 는 계약에서 온다 — 값이 같은 것으로는 부족하다.

    ⚠️ 값 비교만 하면 **양쪽을 똑같이 손으로 고쳐도** 통과한다. 그러면 다음 번
      변경에서 또 갈린다. 그래서 `frozenset(ITEMS)` 라는 **형태**를 본다.
    """
    from app.master.commitment import ITEM_CODES

    assert ITEM_CODES == frozenset(ITEMS)

    src = (_APP / "master" / "commitment.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "ITEM_CODES"
    ]
    assert len(calls) == 1, "ITEM_CODES 대입을 못 찾았다"
    value = calls[0].value
    assert isinstance(value, ast.Call), "ITEM_CODES 가 호출식이 아니다 — 손으로 센 것 같다"
    assert [
        a.id for a in value.args if isinstance(a, ast.Name)
    ] == ["ITEMS"], "ITEM_CODES 가 계약 ITEMS 를 안 쓴다"


def test_마스터_LLM_어휘가_계약과_같다():
    """`Literal` 은 상수만 받아 계약에서 못 가져온다. 그래서 값으로 건다."""
    from app.master.llm.schemas import ItemName

    assert frozenset(get_args(ItemName)) == frozenset(ITEMS)


#: 품목 목록을 두 번째로 세는 것이 허용된 자리. **늘리려면 이유가 있어야 한다.**
_SECOND_COUNTS = {
    # Literal 은 변수를 못 받는다. 위 test_마스터_LLM_어휘가_계약과_같다 가 지킨다.
    "master/llm/schemas.py",
}


def _item_literals(path: Path) -> list[int]:
    """품목 이름을 둘 이상 담은 리터럴 묶음의 줄 번호."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple | ast.Set):
            continue
        names = {
            e.value for e in node.elts if isinstance(e, ast.Constant) and e.value in set(ITEMS)
        }
        if len(names) >= 2:
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize(
    "rel",
    sorted(
        str(p.relative_to(_APP)).replace("\\", "/")
        for p in (_APP / "master").rglob("*.py")
        if "__pycache__" not in p.parts
    ),
)
def test_마스터가_품목을_다시_세지_않는다(rel: str):
    """🔴 `commitment.py` 가 딱 이것을 했다 — 계약과 나란히 넷을 적어 뒀다.

    계약이 셋이 된 날 이 파일만 넷으로 남았고, 값이 갈렸는데도 아무도 안 울었다.
    """
    hits = _item_literals(_APP / rel)
    if rel in _SECOND_COUNTS:
        assert hits, f"{rel} 이 이제 안 센다 — _SECOND_COUNTS 에서 빼라"
        return
    assert not hits, f"{rel}:{hits} 가 품목 목록을 다시 센다. 계약 ITEMS 를 가져와라"
