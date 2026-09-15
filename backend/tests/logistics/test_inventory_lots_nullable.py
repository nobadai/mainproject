"""`inventory_lots.grade` · `derivation_status` 의 NOT NULL 해제를 **두 경로에 고정한다.**

`database/README.md` §2 가 규약을 적어 뒀다.

> **같은 변경이 두 곳에 있습니다** — 본 DDL(신규 구축용)과 ALTER 판(이관용).
> 어느 하나만 고치면 갈립니다. **둘 다 고칩니다.**

🔴 **적어 두는 것만으로는 안 지켜진다.** 신규 DB 는 본 DDL 로 서고 운영 DB 는 ALTER 판으로
   가는데, 둘이 갈리면 **어느 쪽도 에러를 안 낸다.** 서로 다른 스키마가 조용히 생긴다.
   `tests/master/test_schema_files_agree.py` 가 뷰 본문에 대해 하는 일을, 이 파일이
   이 두 칸의 nullability 와 COMMENT 에 대해 한다.

⚠️ **SQL 텍스트만 본다.** 살아 있는 DB 를 안 읽는다 — DB 없이도 돌아야 `-m db` 없이
   기본 스위트에 들어간다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.logistics

_REPO = Path(app.logistics.__file__).parent.parent.parent.parent
_DB = _REPO / "database"
_CANONICAL = _DB / "10_domain_schema.sql"
_MIGRATION = _DB / "logistics_inventory_lots_nullable.sql"

#: 이번에 푼 두 칸.
_NULLABLE_NOW = ("grade", "derivation_status")

#: **함께 있어야 하는 것들.** 이 칸들까지 풀리면 Lot 이 뜻을 잃는다 —
#: 수량·원가·보관 Zone·상태가 없는 재고는 재고가 아니다.
_STILL_NOT_NULL = (
    "lot_id",
    "sim_run_id",
    "purchase_item_id",
    "item_id",
    "received_at",
    "original_qty_kg",
    "remaining_qty_kg",
    "unit_cost_krw_per_kg",
    "storage_zone",
    "status",
)

#: 두 파일이 **같은 문구**로 적어야 하는 COMMENT.
_EXPECTED_COMMENTS = {
    "grade": "권위 있는 품질등급. 미확정이면 NULL.",
    "derivation_status": (
        "Burn-in Lot이 어떤 파생규칙으로 생성됐는지 나타내는 상태. Burn-in이 아닌 Lot은 NULL."
    ),
}


def _canonical_columns() -> dict[str, str]:
    """본 DDL 의 `inventory_lots` CREATE 블록에서 칸 이름 → 정의 줄."""
    text = _CANONICAL.read_text(encoding="utf-8")
    block = re.search(r"CREATE TABLE haetdeul\.inventory_lots\s*\((.*?)\n\);", text, re.DOTALL)
    assert block is not None, "10_domain_schema.sql 에 inventory_lots CREATE 블록이 없다"
    columns: dict[str, str] = {}
    for line in block.group(1).splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or stripped.startswith(("--", "CONSTRAINT")):
            continue
        columns[stripped.split()[0]] = stripped
    return columns


def _comment_in(path: Path, column: str) -> str | None:
    """그 파일이 적은 `COMMENT ON COLUMN inventory_lots.<column>` 의 본문."""
    text = path.read_text(encoding="utf-8")
    found = re.findall(
        rf"COMMENT ON COLUMN haetdeul\.inventory_lots\.{column} IS '((?:[^']|'')*)';", text
    )
    assert len(found) <= 1, f"{path.name} 에 {column} COMMENT 가 {len(found)} 곳이다"
    return found[0].replace("''", "'") if found else None


# ── 본 DDL (신규 구축 경로) ──────────────────────────────────────────────


@pytest.mark.parametrize("column", _NULLABLE_NOW)
def test_본_DDL_이_두_칸을_nullable_로_적는다(column: str):
    """🔴 실입고 Lot 은 권위 있는 등급도 Burn-in 파생규칙도 없다 — NULL 이 그 사실이다."""
    정의 = _canonical_columns()[column]

    assert "NOT NULL" not in 정의, f"{column} 이 다시 NOT NULL 로 잠겼다: {정의!r}"


@pytest.mark.parametrize("column", _STILL_NOT_NULL)
def test_나머지_칸은_NOT_NULL_그대로다(column: str):
    """★ 함께 풀리면 안 되는 칸들. 수량·원가·Zone 없는 재고는 재고가 아니다."""
    정의 = _canonical_columns()[column]

    assert "NOT NULL" in 정의, f"{column} 의 NOT NULL 이 사라졌다: {정의!r}"


def test_status_어휘를_건드리지_않았다():
    """이번 변경은 nullability 하나다 — 상태 어휘는 그대로다."""
    text = _CANONICAL.read_text(encoding="utf-8")

    assert (
        "CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'DEPLETED'::text,"
        " 'DISPOSED'::text, 'HOLD'::text])))" in text
    )


# ── ALTER 판 (기존 DB 이관 경로) ────────────────────────────────────────


@pytest.mark.parametrize("column", _NULLABLE_NOW)
def test_이관판이_같은_칸의_NOT_NULL_을_푼다(column: str):
    text = _MIGRATION.read_text(encoding="utf-8")

    assert (
        re.search(
            rf"ALTER TABLE haetdeul\.inventory_lots\s+ALTER COLUMN {column} DROP NOT NULL;",
            text,
        )
        is not None
    ), f"이관판에 {column} DROP NOT NULL 이 없다"


def _실행문만(path: Path) -> str:
    """`--` 주석을 걷어낸 **실제로 실행되는 SQL**.

    ⚠️ 원문을 그대로 뒤지면 *"UPDATE 가 없다"* 고 **설명하는 주석**이 UPDATE 로 잡힌다.
       설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.
    """
    return "\n".join(
        line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_이관판이_행을_건드리지_않는다():
    """🔴 기존 80개 Burn-in Lot 의 값은 그대로 남아야 한다 — 이관은 허용 범위만 바꾼다."""
    실행문 = _실행문만(_MIGRATION).upper()

    for 금지 in ("UPDATE ", "DELETE ", "INSERT ", "TRUNCATE "):
        assert 금지 not in 실행문, f"이관판에 {금지.strip()} 가 있다 — 데이터를 건드리면 안 된다"


def test_이관판이_동적_SQL_이나_불필요한_가드를_쓰지_않는다():
    """★ `DROP NOT NULL` 은 이미 nullable 인 칸에 아무 일도 안 한다 — 가드가 필요 없다."""
    실행문 = _실행문만(_MIGRATION)

    assert "EXECUTE" not in 실행문.upper(), "동적 SQL 을 쓰지 않는다"
    assert "DO $$" not in 실행문, "사소한 DROP NOT NULL 을 PL/pgSQL 로 감싸지 않는다"


# ── 두 경로가 갈리지 않는다 ─────────────────────────────────────────────


@pytest.mark.parametrize("column", _NULLABLE_NOW)
def test_본_DDL_과_이관판의_COMMENT_가_같다(column: str):
    """🔴 메타데이터 계약이 갈리면 *"어느 쪽이 맞나"* 를 아무도 말해 주지 않는다."""
    본판 = _comment_in(_CANONICAL, column)
    이관판 = _comment_in(_MIGRATION, column)

    assert 본판 == _EXPECTED_COMMENTS[column], f"본 DDL 의 {column} COMMENT 가 다르다: {본판!r}"
    assert 이관판 == 본판, f"{column} COMMENT 가 두 파일에서 갈렸다: {본판!r} vs {이관판!r}"


def test_이관판이_README_이관표에_적혀_있다():
    """★ 적히지 않은 이관 스크립트는 운영 DB 에서 영영 안 돌아간다."""
    readme = (_DB / "README.md").read_text(encoding="utf-8")

    assert "logistics_inventory_lots_nullable.sql" in readme
