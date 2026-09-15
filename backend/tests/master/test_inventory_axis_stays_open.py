"""재고 축은 `ITEMS` 로 안 좁힌다 (2026-09-03 · 물류 회신 §5).

🔴 **두 축이 다르다.**

```text
제안 축   "사자고 제안한 품목"   ITEMS 로 거른다 (critic_v0_4.py:226 · 문 앞 검증)
재고 축   "창고에 있는 품목"     자유 문자열 — 좁히지 않는다
```

계약 `ITEMS` 에서 피마늘을 뺐지만 **창고의 8.9kg 은 그대로 있었다.**
`E-UNKNOWN-ITEM` 이 보는 것은 제안 축이지 재고 축이 아니라서 아무것도 안 깨졌다.

⚠️ **지금 위험은 어휘가 없는 것이 아니다.** 나중에 누가 `item` 을 `ItemName` 으로
좁히는 날 `ITEMS` 밖 재고가 검증 에러로 죽는다. 그때는 원칙이 조용히 뒤집히고
아무도 못 본다 — 그래서 여기서 못 박는다.

★ 원칙의 출처는 물류 `tools.py` 의 `build_inventory_by_item` docstring 이다.

> 예상 판매·계획 출고는 차감하지 않는다.
> ML Forecast 유무는 재고 사실과 무관하다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.master
from app.contracts.core import ITEMS

_BACKEND = Path(app.master.__file__).parent.parent.parent
_APP = _BACKEND / "app"

#: (파일, 클래스, 열려 있어야 하는 필드)
_OPEN_FIELDS = [
    ("logistics/schemas.py", "InventoryByItem", "item"),
    ("sales/schemas.py", "LogisticsInventoryByItem", "item"),
]


def _annotation_of(path: Path, cls_name: str, field: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name]
    assert len(classes) == 1, f"{path.name} 에 {cls_name} 이 {len(classes)} 개다"
    for node in classes[0].body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == field
        ):
            return ast.unparse(node.annotation)
    raise AssertionError(f"{cls_name} 에 {field} 칸이 없다")


@pytest.mark.parametrize(("rel", "cls_name", "field"), _OPEN_FIELDS)
def test_재고_축은_자유_문자열이다(rel: str, cls_name: str, field: str):
    """🔴 `Literal` 이나 `ItemName` 으로 좁히면 여기가 운다."""
    annotation = _annotation_of(_APP / rel, cls_name, field)

    assert annotation == "str", (
        f"{rel} 의 {cls_name}.{field} 이 {annotation!r} 로 좁혀졌다. "
        f"ITEMS 밖 재고가 검증 에러로 죽는다 — 예측이 없다는 이유로 재고를 숨기지 않는다"
    )


def test_대조가_공허하지_않다():
    """⚠️ **파서가 도는지부터 단언한다.**

    `_annotation_of` 가 아무거나 `"str"` 로 되돌리면 위 검사가 공허하다.
    좁혀져 있는 것으로 알려진 칸을 하나 읽어 실제로 구분하는지 본다.
    """
    narrowed = _annotation_of(_APP / "master/llm/schemas.py", "Intent", "item")

    assert narrowed != "str", "파서가 좁혀진 칸도 str 로 읽는다 — 위 검사가 공허하다"


def test_물류와_판매는_ITEMS_를_아예_안_본다():
    """★ **읽는 곳이 없다는 것이 가장 강한 보장이다.**

    두 파트가 계약 목록을 아예 모르므로, 계약이 좁아져도 재고가 안 죽는다.
    누가 참조를 들이면 그때부터 결합이 생기므로 여기서 0 을 고정한다.
    """
    offenders: dict[str, list[int]] = {}
    for part in ("logistics", "sales"):
        for path in sorted((_APP / part).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lines = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "ITEMS"
            ]
            if lines:
                offenders[str(path.relative_to(_APP)).replace("\\", "/")] = lines

    assert not offenders, (
        f"물류·판매가 ITEMS 를 참조하기 시작했다: {offenders}. "
        f"재고 축이 제안 축에 묶이는 첫 걸음이다"
    )


def test_계약_밖_품목도_재고로_설_수_있다():
    """실제로 세워 본다. **타입 검사만으로는 부족하다.**"""
    from app.logistics.schemas import InventoryByItem

    outside = "피마늘"
    assert outside not in ITEMS, "이 검사는 계약 밖 품목이어야 뜻이 있다"

    entry = InventoryByItem(item=outside, available_qty_kg=8.88)

    assert entry.item == outside
