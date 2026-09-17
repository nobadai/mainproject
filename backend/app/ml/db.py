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

#: 접속이 안 되면 이만큼 기다리고 포기한다 (초). 매입 파트 #81 · #358.
#:
#:   libpq 기본은 0 = **무제한**이다. DB 가 "안 됩니다" 라고 거절하면 즉시
#:   오류가 나지만, **아무 답도 안 하면** 계속 기다린다. 방화벽이 패킷을
#:   조용히 버리는 경우가 그렇다.
#:
#:   실측 (2026-09-07 · 응답 없는 주소로 접속)
#:       connect_timeout=3   3.1초 만에 ConnectionTimeout
#:       없음                60초가 지나도 안 끝남
#:
#:   그리고 프론트에도 타임아웃이 없어(`api.ts` 에 AbortController 0건)
#:   **끊는 쪽이 아무도 없다.** 화면은 오류도 없이 멈춘 채로 남는다.
#:
#:   5초는 매입이 `purchase_agent/db.py` 에 넣은 값과 맞춘 것이다 (#305).
#:   파트마다 다르면 어느 쪽이 먼저 끊겼는지 화면만 보고 알 수 없다.
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
    """서비스 창고 연결. 예측 결과를 적재하는 곳이다."""
    config = _required_environment(_CONNECTION_ENV_KEYS)
    return psycopg.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
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
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


#: 읽기 한 판이 빌려 쓰는 커넥션 자리. **창고가 둘이라 칸도 둘**이다
#: (`service` · `source`) — 섞으면 서비스 질의가 원본 창고로 간다.
_READ_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "ml_read_connection_scope", default=None
)


@contextmanager
def read_connection_scope() -> Iterator[None]:
    """이 블록 안의 **SELECT 들이 창고마다 커넥션 하나를 나눠 쓴다** (2026-09-17).

    ```text
    종전   fetch_all 한 번 = psycopg.connect 한 번   예측 한 판에 4개
    지금   창고마다 처음 한 번만 연다               서비스 1 · 원본 1
    ```

    🔴 **창고를 섞지 않는다.** `source=True` 와 `source=False` 는 **다른 데이터베이스**라
       커넥션 칸을 따로 둔다 (`service` · `source`). 한 칸으로 두면 서비스 조회가
       원본 창고에 가서 «표가 없다» 가 된다.

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
        for slot in ("service", "source"):
            borrowed = holder.pop(slot, None)
            if borrowed is not None:
                borrowed.close()


@contextmanager
def _read_cursor(*, source: bool) -> Iterator[Any]:
    """읽기 커서 하나. 범위가 열려 있으면 **그 창고의** 커넥션을 빌린다."""
    connect = get_source_connection if source else get_connection
    holder = _READ_SCOPE.get()
    if holder is None:
        with connect() as connection, connection.cursor() as cursor:
            yield cursor
        return
    slot = "source" if source else "service"
    connection = holder.get(slot)
    if connection is None:
        connection = connect()
        holder[slot] = connection
    try:
        with connection.cursor() as cursor:
            yield cursor
    except Exception:
        holder.pop(slot, None)
        try:
            connection.close()
        except Exception:  # noqa: BLE001,S110  이미 끊긴 커넥션은 조용히 버린다 —
            pass  #  닫다가 난 오류를 올리면 **진짜 오류(아래 raise)를 덮는다**
        raise


def fetch_all(query: Query, params: Params = None, *, source: bool = False) -> list[dict[str, Any]]:
    """다건 조회. ``source=True`` 면 원본 창고에서 읽는다."""
    with _read_cursor(source=source) as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def fetch_one(
    query: Query, params: Params = None, *, source: bool = False
) -> dict[str, Any] | None:
    """단건 조회. ``source=True`` 면 원본 창고에서 읽는다."""
    with _read_cursor(source=source) as cursor:
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
