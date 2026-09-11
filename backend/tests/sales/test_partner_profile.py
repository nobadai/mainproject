"""거래처 기본정보 — 스키마가 가진 칸만, 남의 도메인 값은 거절."""

import pytest
from pydantic import ValidationError

from app.sales.partner_profile import (
    FOREIGN_FIELDS,
    PartnerProfileUpdate,
    get_partner_profile,
    update_partner_profile,
)

PARTNER = "KIMCHI_FACTORY_001"


def _row(**over) -> dict:
    return {
        "partner_id": PARTNER,
        "partner_name": "김치제조공장",
        "partner_type": "CUSTOMER",
        "client_type": "김치제조공장",
        "factory_region": "경기도",
        "factory_city": "안성시",
        "factory_area": "원곡면",
        "sales_collection_days": 30,
        "pricing_contract_type": "MARKET_LINKED_COST_PLUS_CM",
        "active": True,
        "provisional": True,
        "note": None,
        **over,
    }


class _Writer:
    def __init__(self, row: dict | None = None):
        self.row = row
        self.statements: list[tuple[str, list]] = []

    def __call__(self, query, params):
        self.statements.append((str(query), list(params)))
        if self.row is None:
            raise RuntimeError("Database write did not return a row")
        return dict(self.row)


def _patch(monkeypatch, *, writer=None, rows=None) -> None:
    monkeypatch.setattr("app.sales.partner_profile.get_db_schema", lambda: "haetdeul")
    if writer is not None:
        monkeypatch.setattr("app.sales.partner_profile.execute_returning_one", writer)
    monkeypatch.setattr(
        "app.sales.partner_profile.fetch_all", lambda *_a, **_k: list(rows or [])
    )


def test_reading_a_partner_returns_the_stored_row(monkeypatch):
    _patch(monkeypatch, rows=[_row()])

    profile = get_partner_profile(partner_id=PARTNER)

    assert profile is not None
    assert profile.partner_name == "김치제조공장"
    assert profile.sales_collection_days == 30
    # 🔴 여신은 이 행에 없다. 어디에 물어야 하는지를 칸으로 말한다.
    assert profile.credit_source == "finance:partner_credit_limits"


def test_an_unknown_partner_is_not_invented(monkeypatch):
    _patch(monkeypatch, rows=[])

    assert get_partner_profile(partner_id="NOPE") is None


def test_only_the_given_fields_are_written(monkeypatch):
    """⚠️ 안 준 칸은 안 고친다 — 전체 덮어쓰기가 아니다."""
    writer = _Writer(_row(partner_name="새 이름"))
    _patch(monkeypatch, writer=writer)

    profile = update_partner_profile(
        partner_id=PARTNER, update=PartnerProfileUpdate(partner_name="새 이름")
    )

    assert profile is not None and profile.partner_name == "새 이름"
    statement, params = writer.statements[0]
    assert "partner_name" in statement
    #  준 칸 하나와 WHERE 의 거래처 하나 — 다른 칸은 SET 에 없다.
    assert params == ["새 이름", PARTNER]
    assert "factory_region" not in statement.split("WHERE")[0]


def test_updating_an_unknown_partner_reports_missing(monkeypatch):
    _patch(monkeypatch, writer=_Writer(None))

    assert (
        update_partner_profile(
            partner_id="NOPE", update=PartnerProfileUpdate(partner_name="x")
        )
        is None
    )


def test_the_result_is_what_was_stored_not_what_was_sent(monkeypatch):
    """★ `RETURNING` 을 돌려준다 — 화면이 «고쳐졌다고 믿는 값» 이 아니라 저장된 값이다."""
    writer = _Writer(_row(partner_name="장부가 가진 이름"))
    _patch(monkeypatch, writer=writer)

    profile = update_partner_profile(
        partner_id=PARTNER, update=PartnerProfileUpdate(partner_name="보낸 이름")
    )

    assert profile is not None
    assert profile.partner_name == "장부가 가진 이름"


def test_credit_limit_is_not_a_partner_field():
    """🔴 여신 한도를 거래처 기본정보로 중복 저장하지 않는다."""
    assert "credit_limit" in FOREIGN_FIELDS
    assert "credit_limit_krw" in FOREIGN_FIELDS
    with pytest.raises(ValidationError):
        PartnerProfileUpdate.model_validate({"credit_limit": 10_000_000})


@pytest.mark.parametrize("name", ["contact", "phone", "email"])
def test_fields_the_schema_lacks_are_refused_not_stuffed_into_note(name):
    """🔴 없는 칸을 `note` 에 밀어 넣지 않는다 — 검색도 검증도 안 되는 값이 된다."""
    assert name in FOREIGN_FIELDS
    with pytest.raises(ValidationError):
        PartnerProfileUpdate.model_validate({name: "값"})


def test_a_negative_collection_day_is_refused():
    with pytest.raises(ValidationError):
        PartnerProfileUpdate.model_validate({"sales_collection_days": -1})


def test_an_empty_update_touches_nothing(monkeypatch):
    writer = _Writer(_row())
    _patch(monkeypatch, writer=writer, rows=[_row()])

    profile = update_partner_profile(partner_id=PARTNER, update=PartnerProfileUpdate())

    assert profile is not None
    #  🔴 빈 수정은 `UPDATE` 를 돌리지 않는다 — 안 바뀐 행을 굳이 다시 쓰지 않는다.
    assert writer.statements == []


def test_the_partner_row_is_not_run_scoped(monkeypatch):
    """★ 거래처 원장은 실행과 무관하다 — `sim_run_id` 를 받지도 걸지도 않는다."""
    writer = _Writer(_row())
    _patch(monkeypatch, writer=writer)

    update_partner_profile(partner_id=PARTNER, update=PartnerProfileUpdate(active=False))

    statement, _ = writer.statements[0]
    assert "sim_run_id" not in statement
