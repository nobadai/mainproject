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


#: 접속 시도를 포기하는 시각(초). ``psycopg.connect`` 로 그대로 넘어간다.
#:
#: ⚠️ **없으면 libpq 기본이 0(무제한)** 이라 TCP connect 가 커널 재시도 정책까지
#:   매달린다. 접속이 **거부**되는 경우와 **무응답**인 경우가 다르게 동작한다
#:   (``#81`` 실측 2026-08-28)::
#:
#:       접속 거부     16ms 에 E4_NOT_STARTED 로 정상 종료
#:       접속 무응답   180초 클라이언트 타임아웃까지 CONNECT 3회 · SQL 0건
#:
#: 🔴 **화면은 지금도 안 멈춘다** — 프론트가 15초에 끊고 폴백한다
#:   (``frontend/src/lib/api.ts``). 물리는 것은 **백엔드 워커**이고, 요청이 쌓이면
#:   고갈된다.
#:
#: **왜 5초인가**
#:
#: - LAN(``192.168.0.38``)이고 정상 접속은 밀리초 단위다
#: - 프론트 15초의 1/3 — **백엔드가 먼저 정리돼야** 워커가 안 물린다
#: - **재시도 로직이 우리 코드에 없다** (``quotes.py`` 에 retry 0건). 너무 짧으면
#:   일시 실패에 그대로 0안이 된다 — ``#81`` 본문이 *"``No route to host`` 로 한 번
#:   실패한 적이 있고 재시도에서 붙었다"* 를 적어 두었다
#:
#: ⚠️ ``#81`` 이 ⓑ(공통 헬퍼)로 정해지면 이 인자가 헬퍼로 옮겨간다. 그때 이 줄은
#:   지워도 되고 **값은 따라간다.**
CONNECT_TIMEOUT_SECONDS = 5


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
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
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
