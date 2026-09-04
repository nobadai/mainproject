"""매입 에이전트의 PostgreSQL **읽기 전용** 접근 (CLAUDE.md 규칙 2).

🔴 **쓰기 헬퍼를 두지 않는다.** 다른 파트의 ``db.py`` 를 복사해 오면
``execute_returning_one`` · ``execute_many`` 가 따라온다. 매입은 read-only 에이전트라
그 함수들이 **있기만 해도** 규칙 2가 "지켜지고 있다"에서 "깨질 수 있다"로 내려간다.
없으면 쓰려다 import 에서 막히고, 있으면 리뷰가 잡아야 한다 — 앞쪽이 싸다.
이 사실은 계약 테스트가 잠근다 (``test_auction_quotes.py``).

**시세만 여기로 온다.** 예측·재고·주문·현금은 마스터 봉투로 오거나 mock 이고, 시세만
매입 자기 도메인이라 우리가 직접 읽는다 (정의서 §4.1 · ``adapter.build_state`` 주석).

⚠️ **#228(2026-09-03) 이후로 위 문장의 "mock 이고" 는 pytest 안에서만 참이다.** 운영
경로에서 mock 포트를 부르면 ``MockNotAllowed`` 로 막힌다 (``ports.py`` —
``PYTEST_CURRENT_TEST`` 또는 ``sys.modules`` 로 판단). **실운영 등록은 ``main.py`` 가
실 공급자를 꽂는다** (#226 · ``partial(purchase_port, quotes=auction_quote_source())``).
운영에서 예측·재고·주문·현금은 **봉투로만** 오고, 안 오면 mock 으로 메우는 대신
``missing_data`` 로 나간다 (#227 · ``adapter.validate_payload``).

🔴 **``get_db_schema`` 를 두지 않는다.** 다른 파트의 ``db.py`` 에는 있지만, 우리가
읽는 스키마는 ``constraints.yaml`` 의 ``market_quotes.source`` 가 정한다 (``quotes.source_table``).
헬퍼를 남겨 두면 다음 사람이 그걸로 다시 배선하고, 그 순간 **``.env`` 가 어느 테이블을
읽을지 정하게 된다** — ``DB_SCHEMA`` 가 ``haetdeul`` 인 머신은 3일 된 사본을 보게 된다.

접속 정보는 다른 파트와 같은 ``DB_*`` 환경변수를 쓴다. **이건 접속 정보이지
"mock 이냐 DB 냐"의 스위치가 아니다** — 그 선택은 환경변수가 아니라 명시 주입이다
(``ports.get_market_quotes(source=...)``). 환경변수로 갈리면 ``.env`` 가 테스트 결과를
좌우하고, 그 상태는 이미 한 번 겪었다 (2026-08-31 · LLM_PROVIDER 건).
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


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """읽기용 연결. 이 모듈은 이 연결로 ``SELECT`` 만 보낸다."""
    config = _required_environment(_CONNECTION_ENV_KEYS)
    return psycopg.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        row_factory=dict_row,
    )


def fetch_all(query: Query, params: Params = None) -> list[dict[str, Any]]:
    """다건 조회."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def fetch_one(query: Query, params: Params = None) -> dict[str, Any] | None:
    """단건 조회."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()
