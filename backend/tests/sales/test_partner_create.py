"""거래처 등록 — 표가 가진 칸만, 덮어쓰지 않고, 여신은 건드리지 않는다."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.sales.partner_profile import (
    PARTNER_TYPES,
    PartnerAlreadyExists,
    PartnerProfileCreate,
    create_partner_profile,
)
from app.sales.router import add_partner_profile

NEW = "TEST_FACTORY_900"


def _stored(**over) -> dict:
    return {
        "partner_id": NEW,
        "partner_name": "새 김치공장",
        "partner_type": "CUSTOMER",
        "client_type": "김치제조공장",
        "factory_region": "충청북도",
        "factory_city": "괴산군",
        "factory_area": None,
        "sales_collection_days": 30,
        "pricing_contract_type": None,
        "active": True,
        #  ★ DB 기본값이다. 입력에는 없고 `RETURNING` 으로만 온다.
        "provisional": False,
        "note": None,
        **over,
    }


class _Writer:
    """`execute_returning_one` 흉내. 행이 없으면 실제 헬퍼와 같이 `RuntimeError` 다."""

    def __init__(self, row: dict | None):
        self.row = row
        self.statements: list[tuple[str, list]] = []

    def __call__(self, query, params):
        self.statements.append((str(query), list(params)))
        if self.row is None:
            raise RuntimeError("Database write did not return a row")
        return dict(self.row)


def _patch(monkeypatch, writer: _Writer, rows=None) -> None:
    monkeypatch.setattr("app.sales.partner_profile.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.sales.partner_profile.execute_returning_one", writer)
    monkeypatch.setattr(
        "app.sales.partner_profile.fetch_all", lambda *_a, **_k: list(rows or [])
    )


def test_a_new_partner_is_created_and_the_stored_row_comes_back(monkeypatch):
    writer = _Writer(_stored())
    _patch(monkeypatch, writer)

    profile = create_partner_profile(
        create=PartnerProfileCreate(
            partner_id=NEW,
            partner_name="새 김치공장",
            partner_type="CUSTOMER",
            client_type="김치제조공장",
            factory_region="충청북도",
            factory_city="괴산군",
            sales_collection_days=30,
        )
    )

    assert profile.partner_id == NEW
    assert profile.partner_name == "새 김치공장"
    #  ★ 돌려주는 것은 입력이 아니라 저장된 행이다 — DB 기본값도 함께 온다.
    assert profile.provisional is False
    assert profile.credit_source == "finance:partner_credit_limits"


def test_the_insert_writes_only_columns_the_table_has(monkeypatch):
    writer = _Writer(_stored())
    _patch(monkeypatch, writer)

    create_partner_profile(
        create=PartnerProfileCreate(
            partner_id=NEW, partner_name="새 김치공장", partner_type="CUSTOMER"
        )
    )

    statement, params = writer.statements[0]
    assert "INSERT INTO" in statement
    #  ★ 쓰는 칸은 `RETURNING` 앞쪽이다. 뒤쪽은 읽어 오는 칸이라 목록이 다르다.
    written = statement.split("RETURNING")[0]
    #  🔴 자료 등급 칸은 화면이 정하는 값이 아니다 — DB 기본값으로 둔다.
    assert "provisional" not in written
    #  🔴 여신 정본 표를 판매가 건드리지 않는다.
    assert "partner_credit_limits" not in statement
    assert "credit" not in written
    assert len(params) == written.count("Placeholder()")


def test_a_duplicate_code_is_refused_instead_of_overwriting(monkeypatch):
    """🔴 이미 있는 코드를 덮으면 «새로 만들었다» 가 남의 이름을 바꾼 것이 된다."""
    writer = _Writer(None)
    _patch(monkeypatch, writer)

    with pytest.raises(PartnerAlreadyExists):
        create_partner_profile(
            create=PartnerProfileCreate(
                partner_id="KIMCHI_FACTORY_001",
                partner_name="덮어쓰기 시도",
                partner_type="CUSTOMER",
            )
        )

    statement, _ = writer.statements[0]
    assert "ON CONFLICT (partner_id) DO NOTHING" in statement
    assert "DO UPDATE" not in statement


def test_required_fields_are_not_invented():
    with pytest.raises(ValidationError):
        PartnerProfileCreate(partner_name="이름만", partner_type="CUSTOMER")
    with pytest.raises(ValidationError):
        PartnerProfileCreate(partner_id=NEW, partner_type="CUSTOMER")
    with pytest.raises(ValidationError):
        PartnerProfileCreate(partner_id=NEW, partner_name="", partner_type="CUSTOMER")


def test_a_partner_type_the_table_rejects_is_caught_before_the_database():
    """⚠️ DB CHECK 까지 보내면 사용자가 «constraint» 라는 말을 화면에서 읽는다."""
    with pytest.raises(ValidationError):
        PartnerProfileCreate(partner_id=NEW, partner_name="이름", partner_type="고객")

    for allowed in PARTNER_TYPES:
        assert PartnerProfileCreate(
            partner_id=NEW, partner_name="이름", partner_type=allowed
        ).partner_type == allowed


def test_a_negative_or_absurd_collection_term_is_refused():
    with pytest.raises(ValidationError):
        PartnerProfileCreate(
            partner_id=NEW, partner_name="이름", partner_type="CUSTOMER",
            sales_collection_days=-1,
        )
    with pytest.raises(ValidationError):
        PartnerProfileCreate(
            partner_id=NEW, partner_name="이름", partner_type="CUSTOMER",
            sales_collection_days=9999,
        )


def test_an_unknown_field_is_refused_rather_than_silently_dropped():
    with pytest.raises(ValidationError):
        PartnerProfileCreate(
            partner_id=NEW, partner_name="이름", partner_type="CUSTOMER",
            provisional=True,
        )


def test_the_route_refuses_a_credit_limit_instead_of_ignoring_it():
    """🔴 조용히 무시하면 사용자는 한도가 저장된 줄 알고 화면을 닫는다."""
    with pytest.raises(HTTPException) as raised:
        add_partner_profile(
            {
                "partner_id": NEW,
                "partner_name": "이름",
                "partner_type": "CUSTOMER",
                "credit_limit_krw": 10_000_000,
            }
        )

    assert raised.value.status_code == 422
    assert "partner_credit_limits" in str(raised.value.detail)


def test_the_route_reports_a_duplicate_as_a_conflict_not_a_bad_field(monkeypatch):
    _patch(monkeypatch, _Writer(None))

    with pytest.raises(HTTPException) as raised:
        add_partner_profile(
            {
                "partner_id": "KIMCHI_FACTORY_001",
                "partner_name": "이름",
                "partner_type": "CUSTOMER",
            }
        )

    assert raised.value.status_code == 409
    assert "KIMCHI_FACTORY_001" in str(raised.value.detail)


def test_the_route_names_the_field_a_user_sees_rather_than_the_model(monkeypatch):
    _patch(monkeypatch, _Writer(_stored()))

    with pytest.raises(HTTPException) as raised:
        add_partner_profile({"partner_name": "이름", "partner_type": "CUSTOMER"})

    detail = str(raised.value.detail)
    assert raised.value.status_code == 422
    assert "내부 거래처 코드" in detail
    #  ⚠️ 내부 모델 이름이 화면에 뜨지 않는다.
    assert "PartnerProfileCreate" not in detail


def test_creating_a_partner_returns_the_row_the_route_hands_back(monkeypatch):
    _patch(monkeypatch, _Writer(_stored()))

    profile = add_partner_profile(
        {"partner_id": NEW, "partner_name": "새 김치공장", "partner_type": "CUSTOMER"}
    )

    assert profile.partner_id == NEW
    assert profile.active is True
