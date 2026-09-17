"""영업 Agent의 PostgreSQL 접근 기능.

기존 재고·물류 Agent와 같은 환경변수 기반 연결을 사용한다.
"""

import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
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
CONNECT_TIMEOUT_SECONDS = 5


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
    """환경변수 설정으로 새 PostgreSQL Connection을 생성한다."""
    config = _required_environment(_CONNECTION_ENV_KEYS)
    return psycopg.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        row_factory=dict_row,
    )


#: 읽기 한 판이 빌려 쓰는 커넥션 자리. 규칙·경고는 `read_connection_scope` 에 있고
#: **`app/finance/db.py` 의 같은 이름과 같은 물건**이다 (두 파일은 원래 같은 모양이다).
_READ_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "sales_read_connection_scope", default=None
)


@contextmanager
def read_connection_scope() -> Iterator[None]:
    """이 블록 안의 **SELECT 들이 커넥션 하나를 나눠 쓴다** (2026-09-17).

    ```text
    종전   fetch_all 한 번 = psycopg.connect 한 번   영업 한 판에 6개
    지금   범위 안에서 처음 한 번만 연다
    ```

    🔴 **읽기에만 건다** · **질의가 터지면 그 커넥션을 버린다** · **스레드마다 따로다.**
       세 규칙의 이유는 `app/finance/db.py::read_connection_scope` 에 적어 두었다.

    ★ 범위를 안 열면 아무것도 안 바뀐다.
    """
    holder: dict[str, Any] = {}
    token = _READ_SCOPE.set(holder)
    try:
        yield
    finally:
        _READ_SCOPE.reset(token)
        borrowed = holder.pop("conn", None)
        if borrowed is not None:
            borrowed.close()


@contextmanager
def _read_cursor() -> Iterator[Any]:
    """읽기 커서 하나. 범위가 열려 있으면 그 커넥션을 빌린다."""
    holder = _READ_SCOPE.get()
    if holder is None:
        with get_connection() as connection, connection.cursor() as cursor:
            yield cursor
        return
    connection = holder.get("conn")
    if connection is None:
        connection = get_connection()
        holder["conn"] = connection
    try:
        with connection.cursor() as cursor:
            yield cursor
    except Exception:
        holder.pop("conn", None)
        try:
            connection.close()
        except Exception:  # noqa: BLE001,S110  이미 끊긴 커넥션은 조용히 버린다 —
            pass  #  닫다가 난 오류를 올리면 **진짜 오류(아래 raise)를 덮는다**
        raise


def fetch_one(query: Query, params: Params = None) -> dict[str, Any] | None:
    """Parameter binding을 사용해 단건 조회 결과를 반환한다."""
    with _read_cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()


def fetch_all(query: Query, params: Params = None) -> list[dict[str, Any]]:
    """Parameter binding을 사용해 다건 조회 결과를 반환한다."""
    with _read_cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def execute_returning_one(query: Query, params: Params = None) -> dict[str, Any]:
    """변경 SQL을 실행하고 RETURNING 단건 결과를 반환한다."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Database write did not return a row")
        return row
