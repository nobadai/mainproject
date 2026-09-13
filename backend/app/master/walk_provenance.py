"""walk_provenance.py — **걷기가 받은 `--now` 를 실행 행에 남긴다** (2026-09-13).

```text
record_walked_now(sim_run_id=..., walked_now=...)   커넥션을 열고 · 적고 · 커밋한다
stamp_walked_now(conn, sim_run_id=..., walked_now=...)  부르는 쪽 커넥션으로 잰다·적는다
```

```json
{"baseline": {...}, "backfill": {...},
 "provenance": {"commit": "0e635f8", "walked_now": "2026-09-13T16:00+09:00"}}
```

★★ **같은 코드로 건 두 판이 왜 다른지 아무도 못 읽었다.** 걷기는 `--now` 의
  **시각 부분**만 날마다 붙여 쓰고(`backtest_runner._moment_on`), 마감(10:30) 전
  시각을 주면 예측이 아직 안 온 날이 전부 `WAIT` 로 **안 돈다** (V9① 때는 배치가
  없는 날도 그랬다 · 2026-09-13 부터는 `NO_ML_BATCH` 로 장부가 돈다). 그 값이 어디에도 안
  남아서, V8(16:00) 과 V9①(09:00) 이 통째로 갈린 이유가 코드가 아니라 입력 하나였다는
  것을 원장에서 못 읽었다. **칸이 아니면 없는 것과 같다** (`#622` 의 기준 커밋과 같은 결).

---

🔴 **걷기가 쓴다 — 문(`sim_run_runner`)이 안 받는다.**

  진짜 값을 아는 것은 걷기뿐이다. 여는 자리에서 받으면 실제로 건 값과 갈릴 수 있고,
  그러면 칸이 거짓말을 한다.

🔴 **`provenance.commit` 은 안 건드린다.** 그건 여는 자리 것이다. 그래서 칸을 통째로
  덮지 않고 **한 키만 붙인다** (`jsonb_set` · `||`).

🔴 **값은 받은 문자열 그대로 싣는다. 정규화하지 않는다.** 사람이 준 것과 코드가
  쓰는 것이 갈려야 왜 갈렸는지가 읽힌다 — 여기서 `isoformat()` 으로 다시 쓰면 사람이
  준 것이 사라진다.

---

🔴 **이미 다른 값이 있으면 막는다.**

```text
없으면          → 쓴다
같은 문자열이면 → 그대로 간다 (이어 걷기는 정상이다)
다르면          → 🔴 걷기 전에 멈추고 사유를 낸다
```

⚠️ **한 실행을 두 기준 시각으로 이어 걸으면** 그 판의 절반은 마감 전, 절반은 마감
  뒤가 된다. 그 판은 아무것도 증명하지 않는다. 다른 시각으로 다시 걸려면
  `sim_run_runner --reset` 으로 새로 열면 된다 — 이미 있는 길이다.

🔴 **덮어쓰기 옵션을 만들지 않는다.** 이 저장소에 `--force` 류가 없는 이유와 같다.

★ **잴 때 행을 잠근다** (`FOR UPDATE`). 두 걷기가 같은 실행에 동시에 들어오면
  둘 다 「없다」를 보고 각자 적을 수 있다 — 재는 것과 쓰는 것이 한 트랜잭션이다.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any, Literal

from psycopg import sql

from app.finance.db import get_connection, get_db_schema
from app.master.sim_run_open import RUN_TABLE

__all__ = [
    "WALKED_NOW_KEY",
    "WalkedNowConflict",
    "WalkedNowStamp",
    "record_walked_now",
    "stamp_walked_now",
]

#: `provenance` 칸 안에서 **걷기가 받은 `--now`** 가 앉는 키 이름.
#:
#: 🔴 **여기가 유일한 주인이다.** 읽는 쪽이 생기면 이 이름을 가져다 쓴다
#:   (`PROVENANCE_CONFIG_KEY` · `BACKFILL_CONFIG_KEY` 와 같은 이유).
WALKED_NOW_KEY = "walked_now"

#: 한 번 잰 결과. **두 값뿐이다** — 다르면 값이 아니라 예외다.
#:
#: ```text
#: WRITTEN        없어서 적었다
#: ALREADY_SAME   같은 문자열이 이미 있었다 · 아무것도 안 썼다
#: ```
WalkedNowStamp = Literal["WRITTEN", "ALREADY_SAME"]


class WalkedNowConflict(ValueError):
    """🔴 **그 실행에 이미 다른 기준 시각이 있다.** 걷기 전에 막는다.

    ★ `ValueError` 를 잇는다 — 걷기가 걷기 전에 막는 다른 사유(범위 · 시간대 ·
      `--auto-approve` 규칙)와 같은 결로 잡힌다.
    """


def stamp_walked_now(conn: Any, *, sim_run_id: str, walked_now: str) -> WalkedNowStamp:
    """그 실행에 **걷기가 받은 시각**을 잰다 · 없으면 적는다. 🔴 **커밋하지 않는다.**

    :param walked_now: 🔴 **받은 문자열 그대로** 싣는다 — 여기서 파싱도 정규화도
        안 한다. 같은지도 **문자열로** 잰다.
    :raises WalkedNowConflict: 이미 **다른** 값이 있을 때. 🔴 덮지 않는다.
    :raises LookupError: 그 실행 행이 없을 때. **지어내지 않는다.**
    :raises ValueError: `provenance` 칸이 객체가 아닐 때. 한 키를 붙일 자리가 없다.
    """
    # 🔴 **칸 이름의 주인에서 읽는다 — 여기서 `"provenance"` 를 다시 적지 않는다.**
    #
    # ⚠️ **함수 안에서 들여온다.** `sim_run_runner` 가 모듈 머리에서
    #   `backtest_runner` 를 들여오고 `backtest_runner` 가 이 파일을 들여오므로,
    #   머리에서 들여오면 셋이 고리가 되어 먼저 읽힌 쪽이 반쯤 선 모듈을 본다.
    from app.master.sim_run_runner import PROVENANCE_CONFIG_KEY

    표 = sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(RUN_TABLE))
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT config_json FROM {} WHERE sim_run_id = %s FOR UPDATE").format(표),
            [sim_run_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError(
                f"실행 {sim_run_id!r} 이 없다 — 기준 시각을 남길 자리가 없다."
                " 실행을 먼저 열어라(`sim_run_runner`)"
            )

        config = row["config_json"]
        자취 = config.get(PROVENANCE_CONFIG_KEY) if isinstance(config, Mapping) else None
        if 자취 is not None and not isinstance(자취, Mapping):
            raise ValueError(
                f"실행 {sim_run_id!r} 의 {PROVENANCE_CONFIG_KEY} 칸이 객체가 아니다"
                f" ({type(자취).__name__}) — 한 키를 붙일 자리가 없다. 덮지 않는다"
            )

        이미 = None if 자취 is None else 자취.get(WALKED_NOW_KEY)
        if 이미 is not None:
            if 이미 == walked_now:
                # ★ **이어 걷기는 정상이다.** 같은 값을 다시 쓰지도 않는다.
                return "ALREADY_SAME"
            # 🔴 **덮지 않는다. 덮는 옵션도 없다.**
            raise WalkedNowConflict(
                f"실행 {sim_run_id!r} 은 이미 기준 시각 {이미!r} 로 걸렸다 — 받은 값은"
                f" {walked_now!r} 다. 한 실행을 두 기준 시각으로 이어 걸으면 그 판의"
                " 절반은 마감 전, 절반은 마감 뒤가 되어 아무것도 증명하지 않는다."
                " 다른 시각으로 걸려면 `sim_run_runner --reset` 으로 새로 열어라"
            )

        # 🔴 **한 키만 붙인다.** `commit` 은 여는 자리 것이라 안 건드린다 —
        #    칸을 통째로 덮으면 여는 자리가 적은 커밋이 사라진다.
        cursor.execute(
            sql.SQL(
                "UPDATE {} SET config_json = jsonb_set("
                "config_json, ARRAY[%s]::text[],"
                " COALESCE(config_json -> %s::text, '{{}}'::jsonb)"
                " || jsonb_build_object(%s::text, %s::text), true)"
                " WHERE sim_run_id = %s"
            ).format(표),
            [PROVENANCE_CONFIG_KEY, PROVENANCE_CONFIG_KEY, WALKED_NOW_KEY, walked_now, sim_run_id],
        )
    return "WRITTEN"


def record_walked_now(*, sim_run_id: str, walked_now: str) -> WalkedNowStamp:
    """커넥션을 열어 `stamp_walked_now` 를 부르고 **한 번 커밋한다.** 걷기의 기본값이다.

    ⚠️ **막히면 롤백한다.** 잰 것만 있고 쓴 것이 없어도 행 잠금은 풀어야 한다.
    """
    conn = get_connection()
    try:
        stamp = stamp_walked_now(conn, sim_run_id=sim_run_id, walked_now=walked_now)
    except Exception:
        with suppress(Exception):
            conn.rollback()
        raise
    else:
        conn.commit()
        return stamp
    finally:
        conn.close()
