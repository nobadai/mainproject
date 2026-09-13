"""🔴 마스터가 `sales` 를 읽는 **모든 조회**에 `sim_run_id` 조건이 있다 (소스 스캔).

2026-09-13 · 매입 입력 `confirmed_orders` 가 축 없이 남의 실행 판매를 읽었다.

```text
finance_receivable.read_confirmed_sales   2026-09-09 에 축을 걸었다
outbound_flow.due_sale_items               2026-09-11 에 축을 걸었다
inputs._orders_from_db                     2026-09-13 에야 걸었다  ← 셋째 자리가 빠져 있었다
```

★ **같은 판단을 자리마다 기억에 맡기면 다음 자리에서 또 빠진다.** 그래서 소스를 훑어
  `sales` 를 FROM/JOIN 하는 SQL 문자열을 전부 찾고, 그 문자열 안에 `sim_run_id = %s` 가
  있는지 본다.

🔴 **0건을 세면 그것도 막는다.** 스캔 대상이 비거나 패턴이 어긋나 아무것도 못 찾으면
  *"빠진 곳이 없다"* 가 아니라 *"안 봤다"* 다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

#: 스캔 대상. **마스터 소유 코드 전부**다.
SCAN_ROOTS: tuple[Path, ...] = (BACKEND / "app" / "master",)

#: 지금 반드시 걸려야 하는 조회 자리 (파일 이름). 하나라도 안 걸리면 스캔이 눈을 감은 것이다.
EXPECTED_FILES = frozenset({"finance_receivable.py", "outbound_flow.py", "inputs.py"})

#: `FROM {}.sales` · `JOIN {sch}.sales s` · `FROM haetdeul.sales` 모양. `sale_items` 는 안 걸린다.
_READS_SALES = re.compile(r"\b(?:FROM|JOIN)\s+(?:\{\w*\}\.|\"?\w+\"?\.)?sales\b", re.IGNORECASE)
_RUN_AXIS = re.compile(r"\bsim_run_id\s*=\s*%s")


def _sales_queries() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _READS_SALES.search(node.value)
                ):
                    found.append((path.name, node.lineno, node.value))
    return found


def test_스캔이_sales_조회를_실제로_찾는다():
    """🔴 **0건이면 실패한다.** 그리고 지금 알려진 셋이 전부 걸려야 한다."""
    found = _sales_queries()

    assert found, "sales 를 읽는 조회를 하나도 못 찾았다 — 스캔이 눈을 감았다"
    files = {name for name, _, _ in found}
    missed = sorted(EXPECTED_FILES - files)
    assert not missed, f"알려진 조회 자리가 안 걸렸다: {missed}"


@pytest.mark.parametrize(
    "name, lineno, text", _sales_queries() or [("<none>", 0, "")], ids=lambda v: str(v)[:40]
)
def test_sales_조회에는_실행_축이_있다(name, lineno, text):
    """🔴 축 없는 `sales` 조회는 남의 실행 판매를 이번 실행의 사실로 읽는다."""
    assert name != "<none>", "스캔 대상이 0건이다"
    assert _RUN_AXIS.search(text), f"{name}:{lineno} 의 sales 조회에 sim_run_id 조건이 없다"
