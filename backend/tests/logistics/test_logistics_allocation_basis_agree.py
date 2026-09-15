"""`allocation_basis` 어휘를 **세 곳에 함께 고정한다.**

```text
app/logistics/outbound.AllocationBasis        Python 이 받는 값
database/30_logistics_wms_schema.sql          신규 구축 DB 의 CHECK
database/logistics_allocation_basis_fefo_auto.sql   기존 DB 이관 판의 CHECK
```

`database/README.md` §2 가 규약을 적어 뒀다.

> **같은 변경이 두 곳에 있습니다** — 본 DDL(신규 구축용)과 ALTER 판(이관용).
> 어느 하나만 고치면 갈립니다. **둘 다 고칩니다.**

🔴 **적어 두는 것만으로는 안 지켜진다.** 셋이 갈려도 **어느 쪽도 에러를 안 낸다** —
   Python 만 넓히면 INSERT 가 운영 DB 에서만 죽고, 본 DDL 만 넓히면 신규 구축 DB 와
   운영 DB 가 서로 다른 값을 받는다. 둘 다 **테스트를 돌린 자리에서는 안 보인다**
   (DB 테스트가 본 DDL 로만 스키마를 세우기 때문이다).

⚠️ **SQL 텍스트만 본다.** 살아 있는 DB 를 안 읽는다 — DB 없이도 돌아야 `-m db` 없이
   기본 스위트에 들어간다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

import app.logistics
from app.logistics.outbound import AllocationBasis

_REPO = Path(app.logistics.__file__).parent.parent.parent.parent
_DB = _REPO / "database"
_CANONICAL = _DB / "30_logistics_wms_schema.sql"
_MIGRATION = _DB / "logistics_allocation_basis_fefo_auto.sql"

#: 🔴 **계약 어휘 셋.** Master ↔ Logistics 합의값이고, 이 튜플이 세 곳의 기준이다.
_EXPECTED_BASES = ("FEFO_TOOL_CONFIRMED", "HUMAN_OVERRIDE", "FEFO_AUTO_SELECTED")

#: 사람 경로가 쓰던 둘. **뜻도 이름도 그대로여야 한다** — 이번 변경은 넓히기다.
_HUMAN_BASES = ("FEFO_TOOL_CONFIRMED", "HUMAN_OVERRIDE")

#: 세 파일이 **같은 문구**로 적어야 하는 COMMENT.
_EXPECTED_COMMENT = (
    "FEFO_TOOL_CONFIRMED = Tool 후보를 사람이 그대로 확정"
    " / HUMAN_OVERRIDE = 사람이 다르게 정함"
    " / FEFO_AUTO_SELECTED = 사람 없이 FEFO 규칙이 고름 (시뮬레이션)."
    " 기본값을 두지 않는다 — 호출자가 반드시 말한다."
)


def _sql_only(path: Path) -> str:
    """`--` 주석을 걷어낸 **실제로 실행되는 SQL**.

    ⚠️ 주석에도 `DROP CONSTRAINT` 같은 낱말이 나온다 — 이유를 적어 두었기 때문이다.
       그것을 문장으로 세면 순서 검사가 엉뚱한 자리를 본다.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )


def _check_values(path: Path) -> list[str]:
    """그 파일의 `ck_inventory_allocations_basis` 가 허용하는 값들."""
    found = re.findall(
        r"CONSTRAINT ck_inventory_allocations_basis\s+"
        r"CHECK \(allocation_basis IN \((.*?)\)\)",
        _sql_only(path),
        re.DOTALL,
    )
    assert len(found) == 1, f"{path.name} 에 basis CHECK 가 {len(found)} 곳이다"
    return re.findall(r"'([A-Z_]+)'", found[0])


def _comment_in(path: Path) -> str | None:
    """그 파일이 적은 `COMMENT ON COLUMN … allocation_basis` 의 본문."""
    found = re.findall(
        r"COMMENT ON COLUMN haetdeul\.inventory_allocations\.allocation_basis"
        r"\s+IS\s+'((?:[^']|'')*)';",
        _sql_only(path),
    )
    assert len(found) <= 1, f"{path.name} 에 basis COMMENT 가 {len(found)} 곳이다"
    return found[0].replace("''", "'") if found else None


# ── Python 계약 ────────────────────────────────────────────────────────


def test_Python_이_어휘_셋을_받는다():
    """★ `AllocationBasis` 가 계약 어휘의 주인이다 — `_ALLOCATION_BASES` 가 여기서 난다."""
    assert set(get_args(AllocationBasis)) == set(_EXPECTED_BASES)


@pytest.mark.parametrize("basis", _HUMAN_BASES)
def test_사람_경로_어휘가_그대로_남아_있다(basis: str):
    """🔴 이번 변경은 **넓히기**다. 사람 경로 값을 지우거나 바꾸지 않았다."""
    assert basis in get_args(AllocationBasis)


# ── 본 DDL (신규 구축 경로) ──────────────────────────────────────────────


def test_본_DDL_이_같은_어휘_셋을_적는다():
    assert _check_values(_CANONICAL) == list(_EXPECTED_BASES)


# ── ALTER 판 (기존 DB 이관 경로) ────────────────────────────────────────


def test_이관판이_같은_어휘_셋을_적는다():
    assert _check_values(_MIGRATION) == list(_EXPECTED_BASES)


def test_이관판이_제약을_지웠다_다시_건다():
    """★ PostgreSQL 은 CHECK 를 제자리에서 못 넓힌다 — 지우고 다시 거는 것이 유일한 길이다."""
    본문 = _sql_only(_MIGRATION)

    assert "DROP CONSTRAINT IF EXISTS ck_inventory_allocations_basis;" in 본문
    assert "ADD CONSTRAINT ck_inventory_allocations_basis" in 본문
    # 🔴 한 트랜잭션 안이어야 제약 없는 순간이 밖에서 안 보인다.
    assert 본문.index("BEGIN;") < 본문.index("DROP CONSTRAINT")
    assert 본문.index("ADD CONSTRAINT") < 본문.index("COMMIT;")


def test_이관판이_기존_행을_건드리지_않는다():
    """🔴 이 이관이 바꾸는 것은 *"앞으로 어떤 값이 허용되는가"* 뿐이다."""
    본문 = _sql_only(_MIGRATION).upper()

    for 금지 in ("UPDATE ", "DELETE ", "INSERT ", "TRUNCATE "):
        assert 금지 not in 본문, f"이관판이 행을 건드린다: {금지.strip()}"


# ── 셋이 같은 말을 하는가 ──────────────────────────────────────────────


def test_본_DDL_과_이관판이_같은_COMMENT_를_적는다():
    """신규 구축 DB 와 이관된 DB 의 메타데이터가 갈리면 어느 쪽이 맞는지 아무도 모른다."""
    assert _comment_in(_CANONICAL) == _EXPECTED_COMMENT
    assert _comment_in(_MIGRATION) == _EXPECTED_COMMENT


def test_자동_선택_어휘가_세_곳에_함께_있다():
    """🔴 Python 만 넓히면 운영 DB 의 CHECK 에서 INSERT 가 죽는다."""
    assert "FEFO_AUTO_SELECTED" in get_args(AllocationBasis)
    assert "FEFO_AUTO_SELECTED" in _check_values(_CANONICAL)
    assert "FEFO_AUTO_SELECTED" in _check_values(_MIGRATION)


def test_README_가_이관판을_적어_두었다():
    """★ 목록에 없으면 이관 담당이 이 파일의 존재를 모른다 (README §2 · §3)."""
    readme = (_DB / "README.md").read_text(encoding="utf-8")

    assert "logistics_allocation_basis_fefo_auto.sql" in readme
    # §3 소유 표에 물류로 적혀 있어야 한다.
    소유줄 = [
        line
        for line in readme.splitlines()
        if "logistics_allocation_basis_fefo_auto.sql" in line and "**물류**" in line
    ]
    assert 소유줄, "README §3 소유 표에 물류로 적힌 줄이 없다"
