"""품목 비중의 **분모는 계약 품목만이다** (`#286` · 매입 실측 2026-09-10).

🔴 `_mix_ratio_from_demand()` 의 SQL 에 `WHERE` 가 없었다. 그래서 계약 밖 품목까지
분모에 들어가 **비중이 눌렸다.**

```text
partner_item_demands 5행
  배추   717.3   비중 0.7643
  무     154.4        0.1645
  양파    14.3        0.0152
  건고추  30.3        0.0323   ← 계약 밖
  피마늘  22.2        0.0237   ← 계약 밖

계약 셋만 분모로 쓰면   배추 0.8096 (전 0.7643 · −4.5%p)
```

★ **「보일 때 거르기」로는 안 된다.** 비중은 이미 눌린 값이라 받는 쪽에서 되돌릴 수
  없다. 되돌릴 수 없는 것은 **원천에서** 막는다.

⚠️ **DB 행은 안 고친다.** 건고추·피마늘 행은 그대로 둔다 — 지난 기록을 고쳐 쓰면
  기록이 거짓이 된다. **읽을 때만** 거른다.

⚠️ **DB 를 타지 않는다.** `fetch_all` 을 대역으로 갈아 끼운다. 대역은 psycopg 가
  `= ANY(%s)` 로 하는 일만 흉내낸다 — **params 로 목록이 오면 거르고, 안 오면 전부
  준다.** 그래야 `WHERE` 를 지우는 변이가 값으로 잡힌다.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.contracts.core import ITEMS
from app.master import inputs

#: 매입이 실측한 다섯 행 그대로. 🔴 **뒤 둘은 계약 밖이다.**
DEMAND_ROWS = [
    {"item_name": "배추", "daily_demand_kg": Decimal("717.300")},
    {"item_name": "무", "daily_demand_kg": Decimal("154.400")},
    {"item_name": "양파", "daily_demand_kg": Decimal("14.300")},
    {"item_name": "건고추", "daily_demand_kg": Decimal("30.300")},
    {"item_name": "피마늘", "daily_demand_kg": Decimal("22.200")},
]

계약_밖 = ("건고추", "피마늘")


def _대역_DB(찍힌: list[tuple[Any, Any]]):
    """psycopg 가 `= ANY(%s)` 로 하는 일만 흉내낸 대역.

    🔴 **params 가 비면 거르지 않는다** — `WHERE` 를 지우면 DB 는 다섯 행을 다 준다.
      그 사실을 대역이 흉내내야 변이가 **값으로** 잡힌다.
    """

    def fetch_all(query: Any, params: Any = None) -> list[dict[str, Any]]:
        찍힌.append((query, params))
        원하는 = None
        for p in params or ():
            if isinstance(p, list | tuple):
                원하는 = set(p)
        if 원하는 is None:
            return list(DEMAND_ROWS)
        return [r for r in DEMAND_ROWS if r["item_name"] in 원하는]

    return fetch_all


@pytest.fixture
def 찍힌_질의(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    찍힌: list[tuple[Any, Any]] = []
    monkeypatch.setattr(inputs, "fetch_all", _대역_DB(찍힌))
    monkeypatch.setattr(inputs, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")
    return 찍힌


# ── ① 값 — 계약 밖이 분모에 안 든다 ───────────────────────────────────────


def test_계약_밖_품목은_분모에_안_든다(찍힌_질의):
    """🔴 전에는 배추가 `0.7643` 이었다. 계약 셋만 분모면 `0.8096` 이다."""
    ratios = inputs._mix_ratio_from_demand()

    분모 = 717.3 + 154.4 + 14.3
    assert ratios["배추"] == pytest.approx(717.3 / 분모, abs=1e-4)
    assert ratios["배추"] == pytest.approx(0.8096, abs=1e-4), (
        f"계약 밖 품목이 분모에 들어 비중이 눌렸다: 배추 {ratios['배추']}"
    )


def test_계약_밖_품목은_표에도_안_나온다(찍힌_질의):
    """★ 분모에서 뺐는데 표에는 남으면 합이 1 을 넘어 받는 쪽이 다시 눌러 읽는다."""
    ratios = inputs._mix_ratio_from_demand()

    for 이름 in 계약_밖:
        assert 이름 not in ratios, f"계약 밖 품목이 비중표에 남았다: {이름}"
    assert set(ratios) == set(ITEMS)
    assert sum(ratios.values()) == pytest.approx(1.0, abs=1e-3)


def test_정책값까지_눌리지_않고_간다(찍힌_질의):
    """★ **원천만 재지 않는다.** `policy_values` 로 실려 나가는 값이 눌리면 소용없다."""
    got = inputs.load_policy_values("배추", None)  # type: ignore[arg-type]

    assert got.grade == "DERIVED"
    assert got.payload["item_mix_ratio"]["배추"] == pytest.approx(0.8096, abs=1e-4)
    for 이름 in 계약_밖:
        assert 이름 not in got.payload["item_mix_ratio"]


# ── ② 배선 — 좁히기가 SQL 에서 일어난다 ───────────────────────────────────


def test_질의가_계약_세_품목만_묻는다(찍힌_질의):
    """★ **원천에서 막는다.** 다 읽고 파이썬에서 거르면 분모가 이미 눌린 뒤다."""
    inputs._mix_ratio_from_demand()

    assert 찍힌_질의, "질의가 안 나갔다"
    query, params = 찍힌_질의[0]
    물어본 = [p for p in (params or ()) if isinstance(p, list | tuple)]
    assert 물어본, f"질의가 품목을 안 좁혔다 — params 가 {params!r} 이다"
    assert tuple(물어본[0]) == tuple(ITEMS), (
        f"계약 정본과 다른 목록으로 물었다: {물어본[0]!r} vs {ITEMS!r}"
    )
    assert "WHERE" in query.as_string(None).upper(), "SQL 에 좁히는 절이 없다"


# ── ③ 정본 — 세 이름을 적재층이 다시 적지 않는다 ──────────────────────────


def _코드에_박힌_문자열(path: Path) -> set[str]:
    """파일 안의 **코드** 문자열만 모은다.

    ⚠️ **주석과 docstring 은 걷어낸다.** 안 걷어내면 *"배추 0.7643"* 이라고 적어 둔
      설명 한 줄에 자기 검사가 걸린다 — 오늘 물류가 자기 오류 메시지에 걸렸다.
      주석은 `ast` 가 애초에 안 싣고, docstring 만 따로 뺀다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = (node.body or [None])[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_계약_품목_이름이_적재층에_안_박혀_있다():
    """🔴 **품목 목록의 주인은 하나다** — `app.contracts.core.ITEMS`.

    두 벌을 두면 계약이 늘거나 줄 때 한쪽만 바뀐다. `commitment.py` 가 피마늘로
    어긋났던 자리가 정확히 이것이다.
    """
    박힌 = _코드에_박힌_문자열(Path(inputs.__file__))
    겹치는 = sorted(박힌 & set(ITEMS))

    assert not 겹치는, (
        f"계약 품목 이름이 inputs.py 코드에 문자열로 박혔다: {겹치는} — "
        f"정본은 app.contracts.core.ITEMS 하나다"
    )
