"""여신한도 쓰기·기간 이력 조회 계약."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app

CLIENT = TestClient(app)
PARTNER = "CUST-001"


class _Cursor:
    def __init__(
        self,
        *,
        partner_exists: bool = True,
        active_rows: list[dict] | None = None,
        history_rows: list[dict] | None = None,
    ):
        self.partner_exists = partner_exists
        self.active_rows = active_rows or []
        self.history_rows = history_rows or []
        self.last = ""
        self.calls: list[tuple[str, list[object]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.last = str(query)
        self.calls.append((self.last, list(params or [])))

    def fetchone(self):
        if ".partners" in self.last:
            return {"exists": 1} if self.partner_exists else None
        return None

    def fetchall(self):
        if "AS is_current" in self.last:
            return [dict(row) for row in self.history_rows]
        return [dict(row) for row in self.active_rows]


class _Connection:
    def __init__(self, cursor: _Cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def _install(monkeypatch, cursor: _Cursor) -> None:
    monkeypatch.setattr("app.finance.router.get_db_schema", lambda: "haetdeul")
    monkeypatch.setattr("app.finance.router.get_connection", lambda: _Connection(cursor))


def _body(**changes) -> dict:
    return {
        "partner_id": PARTNER,
        "credit_limit_krw": "30000000",
        "effective_from": "2026-09-16",
        "evidence_grade": "VENDOR",
        "source_ref": "CONTRACT-CUST-001-20260916",
        "recorded_by": "채훈",
        "note": "계약 확인",
        **changes,
    }


def _insert_params(cursor: _Cursor) -> list[object]:
    return next(params for statement, params in cursor.calls if "INSERT INTO" in statement)


def test_first_credit_limit_is_inserted_with_distinct_source_and_recorder(monkeypatch):
    cursor = _Cursor()
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body())

    assert response.status_code == 201
    params = _insert_params(cursor)
    assert params[2] == Decimal(30000000)
    assert params[5] == "CONTRACT-CUST-001-20260916"
    assert params[6] == "채훈"
    assert params[9] == "계약 확인"


def test_newer_credit_closes_the_open_period_and_inserts_a_new_row(monkeypatch):
    cursor = _Cursor(
        active_rows=[
            {
                "partner_credit_limit_id": "CREDIT-OLD",
                "effective_from": date(2026, 8, 1),
                "effective_to": None,
            }
        ]
    )
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body())

    assert response.status_code == 201
    update = next(params for statement, params in cursor.calls if "SET effective_to" in statement)
    assert update == [date(2026, 9, 15), "CREDIT-OLD"]
    assert _insert_params(cursor)[3] == date(2026, 9, 16)


def test_zero_credit_limit_is_a_real_value(monkeypatch):
    cursor = _Cursor()
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body(credit_limit_krw="0"))

    assert response.status_code == 201
    assert _insert_params(cursor)[2] == Decimal(0)


def test_negative_credit_and_invalid_grade_are_rejected_before_database(monkeypatch):
    cursor = _Cursor()
    _install(monkeypatch, cursor)

    assert (
        CLIENT.post("/finance/credit-limits", json=_body(credit_limit_krw="-1")).status_code == 422
    )
    assert (
        CLIENT.post("/finance/credit-limits", json=_body(evidence_grade="HEARSAY")).status_code
        == 422
    )
    assert cursor.calls == []


def test_blank_source_reference_is_rejected(monkeypatch):
    cursor = _Cursor()
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body(source_ref="   "))

    assert response.status_code == 422
    assert cursor.calls == []


def test_unknown_partner_is_not_created_implicitly(monkeypatch):
    cursor = _Cursor(partner_exists=False)
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body())

    assert response.status_code == 404
    assert not any("INSERT INTO" in statement for statement, _params in cursor.calls)


@pytest.mark.parametrize("new_date", ["2026-08-01", "2026-07-31"])
def test_same_or_earlier_effective_date_conflicts(monkeypatch, new_date):
    cursor = _Cursor(
        active_rows=[
            {
                "partner_credit_limit_id": "CREDIT-OLD",
                "effective_from": date(2026, 8, 1),
                "effective_to": None,
            }
        ]
    )
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body(effective_from=new_date))

    assert response.status_code == 409


def test_multiple_overlapping_or_future_rows_conflict(monkeypatch):
    cursor = _Cursor(
        active_rows=[
            {
                "partner_credit_limit_id": "CREDIT-A",
                "effective_from": date(2026, 8, 1),
                "effective_to": None,
            },
            {
                "partner_credit_limit_id": "CREDIT-B",
                "effective_from": date(2026, 10, 1),
                "effective_to": None,
            },
        ]
    )
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body())

    assert response.status_code == 409


@pytest.mark.parametrize("grade", ["OFFICIAL", "VENDOR", "SIM_FIXED"])
def test_supported_evidence_grades_are_preserved(monkeypatch, grade):
    cursor = _Cursor()
    _install(monkeypatch, cursor)

    response = CLIENT.post("/finance/credit-limits", json=_body(evidence_grade=grade))

    assert response.status_code == 201
    assert _insert_params(cursor)[4] == grade


def _history_row(**changes) -> dict:
    return {
        "partner_credit_limit_id": "CREDIT-NEW",
        "partner_id": PARTNER,
        "credit_limit_krw": Decimal(0),
        "effective_from": date(2026, 9, 16),
        "effective_to": None,
        "evidence_grade": "VENDOR",
        "source_ref": "CONTRACT-CUST-001-20260916",
        "recorded_by": "채훈",
        "policy_version": "manual-v1",
        "usage_scope": "USER_RECORDED",
        "note": "0원도 명시적 한도",
        "is_active": True,
        "is_current": True,
        **changes,
    }


def test_credit_history_returns_stored_fields_in_database_order(monkeypatch):
    newer = _history_row()
    older = _history_row(
        partner_credit_limit_id="CREDIT-OLD",
        credit_limit_krw=Decimal(30000000),
        effective_from=date(2026, 8, 1),
        effective_to=date(2026, 9, 15),
        source_ref="CONTRACT-CUST-001-20260801",
        recorded_by="재무팀",
        note=None,
        is_current=False,
    )
    cursor = _Cursor(history_rows=[newer, older])
    _install(monkeypatch, cursor)

    response = CLIENT.get(
        "/finance/credit-limits",
        params={"partner_id": PARTNER, "as_of": "2026-09-16"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["partner_credit_limit_id"] for row in body] == ["CREDIT-NEW", "CREDIT-OLD"]
    assert body[0]["credit_limit_krw"] == "0"
    assert body[0]["source_ref"] == "CONTRACT-CUST-001-20260916"
    assert body[0]["recorded_by"] == "채훈"
    assert body[0]["evidence_grade"] == "VENDOR"
    assert body[0]["note"] == "0원도 명시적 한도"
    assert body[1]["effective_to"] == "2026-09-15"
    statement, params = next(call for call in cursor.calls if "AS is_current" in call[0])
    assert "ORDER BY effective_from DESC" in statement
    assert params == [date(2026, 9, 16), date(2026, 9, 16), PARTNER]


def test_existing_partner_without_history_returns_empty_list(monkeypatch):
    cursor = _Cursor(history_rows=[])
    _install(monkeypatch, cursor)

    response = CLIENT.get(
        "/finance/credit-limits",
        params={"partner_id": PARTNER, "as_of": "2026-09-16"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_unknown_partner_history_is_404(monkeypatch):
    cursor = _Cursor(partner_exists=False)
    _install(monkeypatch, cursor)

    response = CLIENT.get(
        "/finance/credit-limits",
        params={"partner_id": "UNKNOWN", "as_of": "2026-09-16"},
    )

    assert response.status_code == 404
