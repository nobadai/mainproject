"""저장소의 `.sql` 파일 하나를 **있는 그대로** 실행한다.

이 저장소에는 `psql` 이 깔려 있지 않다. 그렇다고 DDL 을 손으로 옮겨 적으면
**파일과 실제로 돈 것이 갈리고**, 그때 어느 쪽이 맞는지 답할 방법이 없다.
그래서 파일을 읽어 그대로 보낸다 — 이 스크립트는 SQL 을 **한 글자도 만들지 않는다.**

```bash
# 무엇이 돌지 먼저 본다 (DB 에 붙지 않는다)
.venv/Scripts/python.exe scripts/apply_sql.py database/파일.sql --dry-run

# 실제로 적용한다
.venv/Scripts/python.exe scripts/apply_sql.py database/파일.sql --apply
```

🔴 **`--apply` 를 안 주면 아무것도 안 한다.** 기본이 「보여주기」인 이유는, 공유 DB 에
   스키마를 바꾸는 일은 되돌리기가 어렵고 *"돌릴 생각은 없었다"* 가 한 번이면
   충분하기 때문이다.

★ 접속 정보는 `backend/.env` 에서 읽는다. 여기에 호스트도 비밀번호도 안 적는다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

_ENV = Path(__file__).resolve().parent.parent / ".env"


def _dsn() -> str:
    load_dotenv(_ENV)
    빠진것 = [이름 for 이름 in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER") if not os.getenv(이름)]
    if 빠진것:
        raise SystemExit(f"🔴 {_ENV} 에 없는 값: {', '.join(빠진것)}")
    return (
        f"host={os.getenv('DB_HOST')} port={os.getenv('DB_PORT')}"
        f" dbname={os.getenv('DB_NAME')} user={os.getenv('DB_USER')}"
        f" password={os.getenv('DB_PASSWORD')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="`.sql` 파일 하나를 있는 그대로 실행한다")
    parser.add_argument("sql_file", help="실행할 파일 (저장소 루트 기준 또는 절대 경로)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="🔴 실제로 적용한다. 안 주면 내용만 보여주고 DB 에 안 붙는다",
    )
    parser.add_argument("--dry-run", action="store_true", help="내용만 본다 (기본 동작)")
    args = parser.parse_args()

    경로 = Path(args.sql_file)
    if not 경로.is_absolute():
        경로 = (Path(__file__).resolve().parent.parent.parent / args.sql_file).resolve()
    if not 경로.exists():
        raise SystemExit(f"🔴 파일이 없다: {경로}")

    본문 = 경로.read_text(encoding="utf-8")
    print(f"파일   {경로}")
    print(f"길이   {len(본문):,}자 · {본문.count(chr(10)) + 1}줄")
    print("─" * 70)
    print(본문)
    print("─" * 70)

    if not args.apply:
        print("🟡 안 돌렸다. 실제로 적용하려면 --apply 를 준다.")
        return 0

    load_dotenv(_ENV)
    print(f"🔴 적용한다 — {os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cursor:
        # ★ 파일이 `BEGIN;` / `COMMIT;` 을 직접 들고 있을 수 있으므로 autocommit 으로
        #   보낸다 — 여기서 트랜잭션을 또 열면 파일의 경계와 두 벌이 된다.
        cursor.execute(본문)
    print("🟢 끝났다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
