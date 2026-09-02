"""Finance 실행이력 `mode` 제약 — 신규 DDL 과 기존 DB 마이그레이션.

★ 이 파일이 지키는 것은 **저장이 판정을 막지 않는 것**이다.
    · 신규 DDL 이 SALES_VALIDATION 을 허용한다
    · 이미 만들어진 DB 를 위한 마이그레이션이 따로 있다
    · 기존 두 mode 를 빼지 않는다
    · 마이그레이션이 표를 다시 만들지 않는다 (행을 잃지 않는다)
    · 여러 번 실행해도 안전하다

★ **실제 DB 에 대고 돌리지 않는다.** 저장소에 격리된 일회용 PostgreSQL 픽스처가
  없고, 공유 DB 에 파괴적 마이그레이션을 실행할 수는 없다. 그래서 여기서는 SQL
  **본문 계약**을 읽어서 검사한다 — 실제 적용 검증은 격리된 DB 가 생기면 그때 붙인다.
"""

import pathlib
import re

DATABASE = pathlib.Path(__file__).resolve().parents[3] / "database"
FRESH_DDL = DATABASE / "finance_agent_runs_v22.sql"
MIGRATION = DATABASE / "finance_agent_runs_v22_sales_validation.sql"

FINANCE_RUN_MODES = ("PRE_PURCHASE", "SCENARIO_VALIDATION", "SALES_VALIDATION")


def _fresh() -> str:
    return FRESH_DDL.read_text(encoding="utf-8")


def _migration() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _statements(sql: str) -> str:
    """`--` 주석을 걷어낸 **실행되는 SQL** 만 남긴다.

    주석에 'TRUNCATE 를 쓰지 않는다' 라고 적어 둔 것을 위반으로 읽으면, 설명을
    적을수록 검사가 깨진다.
    """
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _mode_check_body(sql: str) -> str:
    """`mode IN (...)` 의 괄호 안쪽만 꺼낸다."""
    match = re.search(r"mode\s+IN\s*\(([^)]*)\)", sql, flags=re.IGNORECASE)
    assert match is not None, "mode IN (...) 제약을 찾지 못했다"
    return match.group(1)


# ---------------------------------------------------------------------------
# 신규 DDL
# ---------------------------------------------------------------------------


def test_fresh_ddl_allows_sales_validation():
    assert "SALES_VALIDATION" in _mode_check_body(_fresh())


def test_fresh_ddl_keeps_both_purchase_modes():
    body = _mode_check_body(_fresh())

    assert "PRE_PURCHASE" in body
    assert "SCENARIO_VALIDATION" in body


def test_fresh_ddl_allows_exactly_the_three_finance_run_modes():
    body = _mode_check_body(_fresh())
    allowed = tuple(sorted(re.findall(r"'([A-Z_]+)'", body)))

    assert allowed == tuple(sorted(FINANCE_RUN_MODES))


def test_fresh_ddl_mode_vocabulary_matches_the_finance_mode_contract():
    """DDL 과 코드가 갈리면 **판정은 되는데 저장이 안 되는** 날이 온다."""
    from typing import get_args

    from app.finance.schemas import FinanceMode

    body = _mode_check_body(_fresh())
    assert set(re.findall(r"'([A-Z_]+)'", body)) == set(get_args(FinanceMode))


# ---------------------------------------------------------------------------
# 기존 DB 마이그레이션
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    assert MIGRATION.exists(), MIGRATION


def test_migration_allows_sales_validation_without_dropping_existing_modes():
    body = _mode_check_body(_statements(_migration()))

    for mode in FINANCE_RUN_MODES:
        assert mode in body, mode


def test_migration_never_recreates_or_empties_the_table():
    sql = _statements(_migration()).upper()

    # 표를 다시 만들거나 비우는 순간 되돌릴 수 없는 사고가 된다.
    for destructive in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "CREATE TABLE"):
        assert destructive not in sql, destructive


def test_migration_only_touches_the_mode_constraint():
    sql = _statements(_migration()).upper()

    assert "DROP CONSTRAINT" in sql
    assert "ADD CONSTRAINT" in sql
    # 컬럼을 지우거나 타입을 바꾸지 않는다.
    assert "DROP COLUMN" not in sql
    assert "ALTER COLUMN" not in sql


def test_migration_is_safe_to_run_more_than_once():
    sql = _statements(_migration())

    # 이름이 무엇이든 mode CHECK 를 찾아 지운 뒤 다시 단다 — 두 번째 실행도 같은 상태로 끝난다.
    assert "DROP CONSTRAINT" in sql.upper()
    assert "pg_constraint" in sql
    assert "to_regclass" in sql


def test_migration_does_not_hardcode_a_single_constraint_name_blindly():
    """손으로 만든 DB 는 제약 이름이 다를 수 있다 — 이름을 찾아서 지운다."""
    sql = _statements(_migration())

    assert "conname" in sql
    assert "contype = 'c'" in sql
    # `mode` 한 컬럼만 걸린 제약으로 좁힌다 — 관계없는 CHECK 를 날리지 않는다.
    assert "conkey" in sql
    assert "attname = 'mode'" in sql


def test_migration_targets_the_repository_schema():
    sql = _statements(_migration())

    assert "haetdeul.finance_agent_runs_v22" in sql
    assert "nspname = 'haetdeul'" in sql


def test_migration_runs_in_one_transaction():
    sql = _statements(_migration()).upper()

    assert sql.count("BEGIN;") == 1
    assert sql.count("COMMIT;") == 1


def test_migration_does_not_touch_other_finance_tables():
    sql = _statements(_migration())
    tables = set(re.findall(r"haetdeul\.([a-z_0-9]+)", sql))

    assert tables == {"finance_agent_runs_v22"}, tables
