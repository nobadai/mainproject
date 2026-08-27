"""ML 파트의 PostgreSQL 접근 기능.

## 왜 연결이 두 개인가

ML 파이프라인은 **창고를 두 개** 쓴다.

    원본 창고 (SOURCE)  경락가·중도매가·소매가 원자료와 학습 테이블이 있는 곳
    서비스 창고 (기본)   다른 Agent 와 같은 곳. 예측 결과를 여기에 넣는다

두 창고는 **같은 서버의 다른 데이터베이스**다. 원본 창고에는 원자료가
수백만 행 쌓여 있어 서비스 창고와 섞지 않는다.

기본 연결(``get_connection``)은 다른 모듈과 동일하게 ``DB_*`` 를 쓴다.
원본 연결(``get_source_connection``)만 ``ML_SOURCE_DB_*`` 를 본다.
없으면 기본값을 물려받되 데이터베이스 이름만 바꾼다.
"""

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


def _required_environment(keys: tuple[str, ...]) -> dict[str, str]:
    load_dotenv(_ENV_FILE)
    values = {key: os.getenv(key, "") for key in keys}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required database environment variables: {', '.join(missing)}")
    return values


def get_db_schema() -> str:
    """설정된 PostgreSQL Schema 이름을 반환한다."""
    return _required_environment(("DB_SCHEMA",))["DB_SCHEMA"]


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """서비스 창고 연결. 예측 결과를 적재하는 곳이다."""
    config = _required_environment(_CONNECTION_ENV_KEYS)
    return psycopg.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        row_factory=dict_row,
    )


def get_source_connection() -> psycopg.Connection[dict[str, Any]]:
    """원본 창고 연결. 원자료와 학습 테이블이 있는 곳이다.

    ``ML_SOURCE_DB_*`` 가 있으면 그것을, 없으면 기본 ``DB_*`` 를 쓰되
    데이터베이스 이름만 ``ML_SOURCE_DB_NAME`` 으로 바꾼다.
    같은 서버의 다른 데이터베이스이므로 접속 정보를 두 벌 관리할 이유가 없다.
    """
    load_dotenv(_ENV_FILE)
    base = _required_environment(_CONNECTION_ENV_KEYS)
    name = os.getenv("ML_SOURCE_DB_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "ML_SOURCE_DB_NAME 이 필요합니다. 원본 데이터가 있는 데이터베이스 이름입니다."
        )
    return psycopg.connect(
        host=os.getenv("ML_SOURCE_DB_HOST", base["DB_HOST"]),
        port=os.getenv("ML_SOURCE_DB_PORT", base["DB_PORT"]),
        dbname=name,
        user=os.getenv("ML_SOURCE_DB_USER", base["DB_USER"]),
        password=os.getenv("ML_SOURCE_DB_PASSWORD", base["DB_PASSWORD"]),
        row_factory=dict_row,
    )


def fetch_all(query: Query, params: Params = None, *, source: bool = False) -> list[dict[str, Any]]:
    """다건 조회. ``source=True`` 면 원본 창고에서 읽는다."""
    connect = get_source_connection if source else get_connection
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def fetch_one(
    query: Query, params: Params = None, *, source: bool = False
) -> dict[str, Any] | None:
    """단건 조회. ``source=True`` 면 원본 창고에서 읽는다."""
    connect = get_source_connection if source else get_connection
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()


def execute_many(query: Query, rows: Sequence[Sequence[object]]) -> int:
    """서비스 창고에 여러 행을 쓴다. 적재된 행 수를 돌려준다."""
    if not rows:
        return 0
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.executemany(query, rows)
        connection.commit()
        return len(rows)
