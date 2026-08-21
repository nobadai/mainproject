"""Finance P0의 PostgreSQL 연결 및 조회 기능."""

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

Query = str | sql.Composed
Params = Sequence[object] | Mapping[str, object] | None

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
_CONNECTION_ENV_KEYS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _load_environment() -> None:
    load_dotenv(_ENV_FILE)


def _required_environment(keys: tuple[str, ...]) -> dict[str, str]:
    _load_environment()
    values = {key: os.getenv(key, "") for key in keys}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required database environment variables: {', '.join(missing)}")
    return values


def get_db_schema() -> str:
    """설정된 PostgreSQL Schema 이름을 반환한다."""
    return _required_environment(("DB_SCHEMA",))["DB_SCHEMA"]


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """환경변수 설정으로 새 PostgreSQL Connection을 생성한다."""
    config = _required_environment(_CONNECTION_ENV_KEYS)
    return psycopg.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        row_factory=dict_row,
    )


def fetch_one(query: Query, params: Params = None) -> dict[str, Any] | None:
    """Parameter binding을 사용해 단건 SELECT 결과를 반환한다."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()


def fetch_all(query: Query, params: Params = None) -> list[dict[str, Any]]:
    """Parameter binding을 사용해 다건 SELECT 결과를 반환한다."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()
