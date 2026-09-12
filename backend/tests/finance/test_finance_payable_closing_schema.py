"""귀속 원장의 DDL 계약 — 신규 구축과 이관이 같은 것을 말해야 한다.

🔴 둘이 갈리면 **새로 세운 DB 와 이관한 DB 가 다른 규칙으로 돈다.** 그 차이는
   마이그레이션을 돌린 사람만 겪고, 겪는 시점은 값이 이미 틀린 뒤다.
"""

from __future__ import annotations

import re
from pathlib import Path

import app.finance
from app.master.sim_run_open import AXIS_COLUMN

_REPO = Path(app.finance.__file__).parent.parent.parent.parent
_FRESH = _REPO / "database" / "10_domain_schema.sql"
_MIGRATION = _REPO / "database" / "finance" / "payable_closing_recognition.sql"
_TABLE = "finance_payable_closing_events"


def _fresh() -> str:
    return _FRESH.read_text(encoding="utf-8")


def _migration() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_both_ddls_create_the_table_exactly_once():
    for source, sql_text in ((_FRESH, _fresh()), (_MIGRATION, _migration())):
        found = re.findall(rf"CREATE TABLE (?:IF NOT EXISTS )?haetdeul\.{_TABLE}\b", sql_text)
        assert len(found) == 1, f"{source.name} must create {_TABLE} exactly once"


def test_both_ddls_agree_on_the_primary_key():
    """🔴 **PK 가 멱등의 정본이다.** 한쪽만 좁으면 그 DB 에서 같은 채무가 두 번 실린다."""
    axis = "(sim_run_id, payable_id)"
    assert f"{_TABLE}_pkey PRIMARY KEY {axis}" in _fresh()
    assert "PRIMARY KEY (sim_run_id, payable_id)" in _migration()


def test_both_ddls_carry_the_same_columns():
    columns = (
        "sim_run_id",
        "payable_id",
        "recognized_date",
        "recognized_amount_krw",
        "due_date",
        "created_at",
    )
    fresh_sql = _fresh()
    migration_sql = _migration()
    for column in columns:
        assert re.search(rf"^\s+{column}\s", fresh_sql, re.MULTILINE), column
        assert re.search(rf"^\s+{column}\s", migration_sql, re.MULTILINE), column


def test_both_ddls_reference_the_payable_ledger():
    """없는 채무에 귀속이 서지 않는다."""
    for sql_text in (_fresh(), _migration()):
        assert "REFERENCES haetdeul.payables(payable_id)" in sql_text


def test_both_ddls_index_the_closing_lookup():
    """마감이 매번 도는 질의다 — 실행과 실은 날로 찾는다."""
    for sql_text in (_fresh(), _migration()):
        assert f"{_TABLE}_run_date_idx" in sql_text
        assert "(sim_run_id, recognized_date)" in sql_text


def test_both_ddls_refuse_a_negative_recognition():
    for sql_text in (_fresh(), _migration()):
        assert f"{_TABLE}_amount_check" in sql_text


def test_the_table_carries_the_run_axis_so_reset_finds_it():
    """★ sim-run reset 은 표 목록을 손으로 안 적는다.

    `information_schema` 에서 **축 칸을 가진 표**를 읽어 지운다
    (`app/master/sim_run_open.py` `_axis_tables`). 그래서 이 칸이 있으면 새 표도
    자동으로 비워지고, 없으면 이전 실행의 귀속이 남아 다음 실행을 오염시킨다.
    """
    assert AXIS_COLUMN == "sim_run_id"
    assert re.search(rf"^\s+{AXIS_COLUMN}\s", _fresh(), re.MULTILINE)
    assert re.search(rf"^\s+{AXIS_COLUMN}\s", _migration(), re.MULTILINE)


def test_the_migration_does_not_touch_the_payable_ledger():
    """🔴 이번 판은 자리를 만들 뿐이다. 채무 원장을 고치면 지급 계약이 흔들린다."""
    migration_sql = _migration()
    statements = re.findall(
        r"^\s*(ALTER TABLE|UPDATE|INSERT INTO|DELETE FROM)\s+haetdeul\.payables",
        migration_sql,
        re.MULTILINE | re.IGNORECASE,
    )
    assert statements == []
