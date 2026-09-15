"""물류 DB 설정 파일(.env)을 프로세스에서 한 번만 읽는지 확인한다 (실 DB 연결 없음)."""

import pytest

from app.logistics import db


@pytest.fixture
def load_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """`load_dotenv` 를 기록기로 바꾸고 한 번 읽음 표시를 이 검사 안에서 처음 상태로 되돌린다."""
    calls: list[object] = []
    monkeypatch.setattr(db, "load_dotenv", lambda path: calls.append(path))
    monkeypatch.setattr(db, "_env_file_loaded", False)
    monkeypatch.setenv("DB_SCHEMA", "logistics_test")
    for key in db._CONNECTION_ENV_KEYS:
        monkeypatch.setenv(key, "x")
    return calls


def test_여러_번_불러도_설정_파일은_한_번만_읽는다(load_calls: list[object]) -> None:
    for _ in range(5):
        assert db.get_db_schema() == "logistics_test"
    for _ in range(5):
        db._required_environment(db._CONNECTION_ENV_KEYS)

    assert load_calls == [db._ENV_FILE]


def test_값은_매번_환경변수에서_읽는다(
    load_calls: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert db.get_db_schema() == "logistics_test"
    monkeypatch.setenv("DB_SCHEMA", "logistics_other")

    assert db.get_db_schema() == "logistics_other"
    assert len(load_calls) == 1


def test_빠진_변수는_그대로_멈춘다(
    load_calls: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DB_SCHEMA")

    with pytest.raises(
        RuntimeError, match="Missing required database environment variables: DB_SCHEMA"
    ):
        db.get_db_schema()
    assert len(load_calls) == 1
