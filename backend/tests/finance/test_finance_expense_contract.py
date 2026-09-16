"""비용 생명주기가 **원장 스키마와 API 계약에 실제로 서 있는가.**

마이그레이션 SQL 만 만들고 끝내면, 코드가 쓰는 칸과 DB 의 칸이 다른 날이 온다. 정본
스키마(`10_domain_schema.sql`)와 마이그레이션이 **같은 계약**을 말해야 한다.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.finance.expenses import KNOWN_EXPENSE_CATEGORIES
from app.main import app

_ROOT = Path(__file__).parents[3]
_MIGRATION = (_ROOT / "database" / "finance" / "expense_lifecycle.sql").read_text(
    encoding="utf-8"
)
_CANONICAL = (_ROOT / "database" / "10_domain_schema.sql").read_text(encoding="utf-8")


# ── 마이그레이션 ──────────────────────────────────────────────────────────


def test_the_migration_adds_the_two_expense_dates_and_the_closing_bucket():
    assert "ADD COLUMN IF NOT EXISTS due_date DATE NULL" in _MIGRATION
    assert "ADD COLUMN IF NOT EXISTS paid_date DATE NULL" in _MIGRATION
    assert "ADD COLUMN IF NOT EXISTS operating_expense_cash_out_krw" in _MIGRATION


def test_every_added_column_is_conditional_so_the_script_can_run_twice():
    """🔴 **세 칸은 공용 DB 에 이미 있다** (2026-09-16 실측).

    조건 없는 `ADD COLUMN` 은 이미 있는 환경에서 그냥 터진다. 이 스크립트는 몇 번을
    돌려도 같은 자리에 서야 한다.
    """
    added = [line for line in _MIGRATION.splitlines() if "ADD COLUMN" in line]

    assert added, "칸을 더하는 줄이 하나도 없다"
    for line in added:
        assert "IF NOT EXISTS" in line, line


def test_the_closing_bucket_is_added_without_a_default_so_null_keeps_its_meaning():
    """🔴 **`NULL` 은 «기록하지 않았다» 이고 0 은 «세어 보니 없었다» 다.**

    기본값을 걸면 그 순간 과거 마감 전부가 «운영비 0원» 이 되고, 아무도 그날 운영비가
    정말 없었는지 다시 묻지 않는다.
    """
    line = next(
        item for item in _MIGRATION.splitlines()
        if "operating_expense_cash_out_krw" in item and "ADD COLUMN" in item
    )

    assert "DEFAULT" not in line.upper(), line
    assert "NOT NULL" not in line.upper(), line


def test_the_migration_never_promotes_a_second_evidence_contract():
    """🔴 비용 원장의 근거 정본은 `evidence_id` 다. 같은 사실에 이름을 둘로 만들지 않는다.

    ★ **이미 있는 `source_ref` · `recorded_by` 를 지우지도 않는다.** 앞선 작업이 남긴
      칸이고, 지우는 것은 과거 데이터를 버리는 일이다 — 쓰기 계약으로 올리지만 않는다.
    """
    statements = "\n".join(
        line for line in _MIGRATION.splitlines() if not line.strip().startswith("--")
    )

    assert "source_ref" not in statements
    assert "recorded_by" not in statements
    assert "DROP" not in statements.upper()


def test_the_migration_is_additive_only():
    """과거 사실을 고쳐 쓰지 않는다 — 추측한 날짜는 사실이 아니다.

    ★ **주석은 빼고 본다.** 머리말이 «DROP 없음» 이라고 적어 둔 것까지 금칙어로 세면,
      규율을 설명한 문장이 규율 위반으로 읽힌다.
    """
    statements = "\n".join(
        line for line in _MIGRATION.splitlines() if not line.strip().startswith("--")
    ).upper()

    for forbidden in ("DROP ", "DELETE ", "TRUNCATE ", "UPDATE "):
        assert forbidden not in statements, forbidden


def test_the_canonical_schema_carries_the_same_contract():
    """마이그레이션과 정본 스키마가 다른 말을 하면 어느 쪽이 정본인지 알 수 없다."""
    assert "    due_date date," in _CANONICAL
    assert "    paid_date date," in _CANONICAL
    #  🔴 기본값도 NOT NULL 도 없다 — 마이그레이션과 같은 말을 해야 한다.
    assert "    operating_expense_cash_out_krw numeric(18,6)\n" in _CANONICAL
    assert "operating_expense_cash_out_krw numeric(18,6) DEFAULT 0" not in _CANONICAL


def test_the_canonical_expenses_table_records_the_columns_that_already_exist():
    """★ 정본 스키마는 **있는 것을 적는다.**

    `source_ref` · `recorded_by` 는 앞선 작업이 공용 DB 에 이미 만들어 둔 칸이다. 정본이
    그것을 안 적으면 정본으로 다시 세운 DB 가 실제와 달라진다 — 적되, 이번 생명주기의
    쓰기 계약이 아니라는 것을 주석이 말한다.
    """
    start = _CANONICAL.index("CREATE TABLE haetdeul.expenses (")
    table = _CANONICAL[start : _CANONICAL.index(");", start)]

    assert "evidence_id" in table
    assert "source_ref text," in table
    assert "recorded_by text," in table
    assert "비용 생명주기의 쓰기 계약이 아니다" in _CANONICAL


# ── API 계약 ──────────────────────────────────────────────────────────────


def test_the_three_lifecycle_routes_are_registered():
    """★ 공개 계약(OpenAPI)에서 본다 — 라우터 객체를 뒤지면 붙는 방식이 바뀔 때 깨진다."""
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "post" in paths["/finance/expenses"]
    assert "post" in paths["/finance/expenses/{expense_id}/settle"]
    assert "post" in paths["/finance/expenses/{expense_id}/cancel"]
    #  🔴 지급·취소에 DELETE 를 열지 않는다. 비용은 지우는 것이 아니라 상태가 바뀐다.
    assert "delete" not in paths["/finance/expenses/{expense_id}/cancel"]


def test_the_category_list_is_served_so_the_screen_never_retypes_it():
    """화면이 분류를 손으로 적으면, 원장이 새 분류를 받는 날 화면만 모르게 된다."""
    response = TestClient(app).get("/finance/expense-categories")

    assert response.status_code == 200
    assert response.json()["categories"] == sorted(KNOWN_EXPENSE_CATEGORIES)


def test_creating_an_expense_never_accepts_a_status_from_the_caller():
    """🔴 «적으면서 바로 지급» 이 생기면 그 경로는 현금 차감을 건너뛴다."""
    schema = TestClient(app).get("/openapi.json").json()
    body = schema["paths"]["/finance/expenses"]["post"]["requestBody"]
    ref = body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    properties = schema["components"]["schemas"][ref]["properties"]

    assert "status" not in properties
    assert "paid_date" not in properties
    assert "due_date" in properties
    assert "evidence_id" in properties
    #  🔴 **원장에 칸이 있다고 계약으로 올리지 않는다.** 근거의 정본은 `evidence_id`
    #     하나이고, 같은 사실에 이름이 둘이 되면 어느 쪽이 정본인지 아무도 말 못 한다.
    assert "source_ref" not in properties
    assert "recorded_by" not in properties


# ── NULL 은 0 이 아니다 ───────────────────────────────────────────────────


def test_a_new_closing_always_writes_an_explicit_operating_expense_value():
    """★ 새 마감은 **세어 본 값을 적는다** — 운영비가 없으면 0 이고, 그 0 은 사실이다.

    🔴 그래서 `NULL` 은 새 마감에서 절대 안 나온다. `NULL` 이 남아 있는 행은 이 축이
       생기기 전에 닫힌 마감뿐이고, 그 의미는 «기록하지 않았다» 다.
    """
    from app.finance import closing

    source = Path(closing.__file__).read_text(encoding="utf-8")

    assert "operating_expense_cash_out_krw: Decimal" in source
    assert '"operating_expense_cash_out_krw": facts.operating_expense_cash_out_krw' in source
    assert "%(operating_expense_cash_out_krw)s" in source


def test_the_read_contract_lets_an_unrecorded_closing_stay_unknown():
    """🔴 조회 계약이 `None` 을 못 담으면, 읽는 순간 «기록 없음» 이 «0원» 이 된다."""
    from app.finance.schemas import FinanceClosingItem

    field = FinanceClosingItem.model_fields["operating_expense_cash_out_krw"]

    assert field.default is None
    assert "NoneType" in str(field.annotation) or "None" in str(field.annotation)


def test_the_closing_reader_never_substitutes_zero_for_an_unrecorded_axis():
    """조회가 `.get(key, 0)` 로 메우면 그 순간 두 사실이 같아진다."""
    from app.finance import dashboard

    source = Path(dashboard.__file__).read_text(encoding="utf-8")

    assert 'row.get("operating_expense_cash_out_krw", 0)' not in source
    assert '"operating_expense_cash_out_krw",' in source
