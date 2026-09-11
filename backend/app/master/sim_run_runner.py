"""새 실행을 여는 문 — **다섯을 한 트랜잭션으로 부른다.**

```text
python -m app.master.sim_run_runner
    --sim-run-id SIM-WALK-202601 --financing-mode LOAN_BASELINE
    --baseline-run-id SIM-BURNIN-202512 --baseline-state-id FIN-DAY30-LOAN
    ...
        → 실행 행 · 시작 재무 상태 · 시작 물류 fixture 가 선다 · 🔴 커밋은 여기서 한 번
```

★ **조각은 다 서 있었고 묶는 문이 없었다.**

```text
sim_run.create_sim_run                        실행 한 행              (#531)
sim_run_open.seed_opening_finance_state       시작 재무 상태          (#545)
sim_run_open.seed_opening_logistics_fixture   시작 물류 fixture       (#551)
sim_run_open.reset_sim_run_ledger             다시 열 때 장부 비우기  (#545)
sim_run_open.delete_sim_run_row               다시 열 때 실행 행 삭제
```

`#545` 는 **일부러** 진입점을 안 만들었다 — *"돌리는 문은 별도 판이다."* 이 파일이
그 문이다.

---

## 🔴 판단을 여기서 새로 만들지 않는다

이 문이 하는 일은 **다섯을 순서대로 부르는 것**뿐이다.

```text
정합성 셋 (출발점이 있는가 · 실행 축이 맞는가 · 조달 방식이 같은가)
                                    → seed_opening_finance_state 안에 있다
번인 실행 거부                        → reset_sim_run_ledger 안에 있다
지울 표 목록 · 지우는 순서             → reset_sim_run_ledger 안에 있다
장부가 남았는지 (FK 자기 검사)          → delete_sim_run_row 안에 있다
```

⚠️ **두 곳에서 막으면 언젠가 한쪽만 고쳐진다.** 그때 어느 쪽이 진짜 규칙인지
  아무도 못 답한다 — `backfill_runner` 가 경계를 다시 안 재는 것과 같은 규율이다.

---

## 🔴 인자에 **기본값이 없다**

```text
--sim-run-id · --financing-mode · --baseline-run-id · --baseline-state-id
```

★★ **재무가 명시적으로 청한 것이다** — *"한 값을 보고 다른 값을 추측하지 않는다."*

```text
❌ financing_mode 를 보고 baseline 을 고른다
❌ finance_state_id 의 글자(LOAN / BASE)를 읽어 어느 쪽인지 판정한다
❌ build_sim_run_id 로 이름을 지어 주고 그 이름을 파싱해 run_type 을 되읽는다
🟢 부르는 쪽이 넷을 **함께** 명시한다
```

⚠️ 기본값을 하나라도 두면 **그 값이 곧 업무 규칙이 된다** — 아무도 그것을 정한 적이
  없는데 실행마다 그 출발점이 찍히고, 나중에 *"왜 저 baseline 인가"* 에 답할 사람이
  없다 (`backtest_runner._parser` 가 날짜에 기본값을 안 두는 것과 같은 이유).

---

## 🔴 다시 여는 것은 **따로 밝혀야** 한다

```text
기본        실행이 이미 있으면 **터진다** · 아무것도 안 지운다
--reset     그때만 ① 장부를 지우고 ② 실행 행을 지우고 ③ 다시 넣는다
```

⚠️ **지우는 것은 되돌릴 수 없다.** `backfill_runner` 의 `--commit` 과 같은 규율이다 —
  **기본이 「안 지운다」** 여야 실수 한 번이 영구적인 일이 되지 않는다.

🔴 `--reset` 이어도 **번인 실행이면 거부된다** — `reset_sim_run_ledger` 가 이미 막는다.
   여기서 다시 검사하지 않는다.

### 🔴 **실행 행을 지우고 다시 넣는다** — `ON CONFLICT` 로 풀지 않는다

★★ 장부만 지우고 실행 행을 남기면 `create_sim_run` 이 같은 이름으로 INSERT 하다
  **PK 에 걸린다.** 그래서 `--reset` 이 한 번도 성공한 적이 없었다.

```text
❌ create_sim_run 에 ON CONFLICT 를 붙인다
   → **다른 설정으로 만들려던 실행**이 옛 행 위에 조용히 앉고,
     그 뒤의 179일이 어느 설정으로 걸린 것인지 아무도 못 답한다
🟢 실행 행을 지우고 새로 넣는다 → 새 설정이 **새 행으로** 선다
```

⚠️ 그 삭제는 **장부를 다 지웠는지에 대한 자기 검사**이기도 하다 — 남은 표가 있으면
  FK 가 막고, 사유에 어느 표가 남았는지가 적힌다 (`delete_sim_run_row`).

---

## 🔴 커밋을 **이 문이** 한다

★ `create_sim_run` 도 `seed_opening_finance_state` 도 커밋을 안 한다 (두 파일이 그렇게
  적어 뒀다). **부르는 쪽이 하는 것**이고, 지금까지 부르는 쪽이 없었다.

```text
🔴 다섯이 **한 트랜잭션**이다
   실행 행만 서고 시작 상태가 없으면 첫날 마감이 baseline 을 못 찾는다
   장부만 지워지고 시작 상태 적재가 터지면 **출발점 없는 빈 실행**이 남는다
   실행 행만 지워지고 다시 안 서면 **설정도 기간도 없는 자리**가 남는다
```

⚠️ 중간에 터지면 **롤백한다.** 반쪽 실행을 남기지 않는다.

---

## 🔴 물류 씨앗도 **같은 트랜잭션**이다

★★ 재무만 놓고 물류를 안 놓으면 새 실행에 물류 행이 **한 행도 없고**, 그때 개장은
  상한만큼 거슬러도 anchor 를 못 찾아 `REJECTED_GAP` 으로 거절한다 (`#551`).

```text
--opening-fixture-id      새로 만들 물류 씨앗 행의 이름  (--opening-state-id 와 대칭)
--opening-usage-scope     어느 usage_scope 를 이관하나
--baseline-run-id         어느 실행에서 이관하나          ← **이미 있는 것을 쓴다**
```

🔴 **`usage_scope` 를 이 문에 박지 않는다** (물류 상수를 import 하는 것도 아니다) —
  그 어휘의 주인은 물류이고, 마스터가 제 코드에 박으면 물류가 값을 바꾸는 날
  말없이 갈린다.

★★ **물류용 날짜 인자를 새로 만들지 않는다.** `--opening-state-date` 하나를 재무
  씨앗과 물류 씨앗이 **둘 다** 쓴다 — 둘이 갈리면 두 파트의 anchor 가 갈리고,
  그러면 이유 없이 한 파트만 며칠 더 걷는다. **갈릴 자리를 안 만드는 것**이다.

---

## 🔴 백필 규칙은 **파일로 받아 그대로 싣는다** (2026-09-11)

```text
--backfill-rules rules.json   →  config_json 의 backfill 칸에 그대로 앉는다
안 주면                        →  그 칸이 아예 안 선다 · --auto-approve 로 못 걷는다
```

```json
{"procurement": {"rule": "...", "scenario_label": "..."},
 "sales":       {"rule": "...", "scenario_type":  "..."}}
```

★★ **이 문은 파일 내용을 모른다.** 규칙 이름도 라벨도 축 이름도 안 읽는다 —
  그 어휘의 주인은 매입과 판매이고, `--opening-usage-scope` 를 여기 안 박은 것과
  **같은 이유·같은 모양**이다.

🔴 **인자로 쪼개 받지 않는다.** 쪼개면 이 문이 *"매입은 라벨을, 판매는 축을 든다"*
  는 **모양까지** 알게 되고, 그 모양은 부서가 규칙을 늘리는 날 갈린다. 파일은
  그대로 지나가고, 무엇을 실었는지는 **파일 하나로 리뷰에 남는다.**

⚠️ **아는 모양인지는 여는 자리에서 검사한다** (`read_rules` 를 부른다 — 판정을
  베끼지 않는다). 179일을 걷고 나서 *"모르는 규칙이었다"* 를 알면 늦다.

---

## 🟡 걷기는 이 문이 안 한다

```text
🔴 이 문은 **여는 것**까지다
🟡 걷는 것은 backtest_runner 이고 이미 있다
```

★ 둘을 한 문에 묶지 않는다 — *"열었는데 안 걸었다"* 와 *"열고 걸었다"* 를 사람이
  고를 수 있어야 한다. 열고 나서 무엇을 부르면 걷는지는 **요약 마지막 줄**이 적어
  준다 (사람이 두 번째 명령을 찾아 헤매지 않게).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from psycopg import sql

from app.finance.db import get_connection, get_db_schema
from app.master.backfill import BACKFILL_CONFIG_KEY, read_rules
from app.master.backtest_runner import _use_utf8_output
from app.master.sim_run import create_sim_run
from app.master.sim_run_open import (
    RUN_TABLE,
    BaselineLineage,
    LedgerReset,
    delete_sim_run_row,
    reset_sim_run_ledger,
    seed_opening_finance_state,
    seed_opening_logistics_fixture,
)

__all__ = [
    "SimRunOpened",
    "format_summary",
    "main",
    "open_sim_run",
]

#: 열고 나서 걸으려면 부를 것. 🟡 **이 문은 안 부른다** — 사람이 고르는 두 번째 명령이다.
WALK_ENTRYPOINT = "python -m app.master.backtest_runner"


@dataclass(frozen=True)
class SimRunOpened:
    """문 한 번의 결과. **무엇이 섰고 무엇을 지웠는지**를 값으로 든다."""

    sim_run_id: str
    financing_mode: str
    #: 어디서 출발하는가. 🔴 **가리키기만 한다** — 숫자를 안 담는다.
    baseline: BaselineLineage
    #: 새로 선 시작 재무 상태의 이름.
    opening_finance_state_id: str
    #: 새로 선 시작 물류 fixture 의 이름. 🔴 이것이 없으면 개장이 anchor 를 못 찾는다.
    opening_logistics_fixture_id: str
    period_start: date
    period_end: date
    #: 지운 결과. 🔴 **`None` 은 「안 지웠다」** — `--reset` 을 안 줬다는 뜻이다.
    #: 0 행을 지운 것과 아예 안 지운 것을 같은 값으로 적지 않는다.
    ledger_reset: LedgerReset | None
    #: 실행 행을 몇 행 지웠나. 🔴 **`None` 은 「안 지웠다」** — `--reset` 을 안 줬다는
    #: 뜻이고, `0` 은 *"지우려 했는데 그 이름의 행이 없었다"* 다.
    #:
    #: ★ **장부와 따로 든다.** 장부는 비웠는데 실행 행이 안 지워졌으면 다음 INSERT 가
    #:   PK 에 걸리고, 그 둘을 한 값에 뭉치면 어느 쪽이 안 된 것인지 못 가른다.
    deleted_run_rows: int | None = None
    #: 실행 행에 실은 백필 규칙. 🔴 **`None` 은 「안 실었다」** — 그 실행은
    #: `--auto-approve` 로 걸을 수 없다 (걷기가 걷기 전에 막는다).
    #:
    #: ⚠️ **받은 것을 그대로 든다.** 이 문은 규칙 이름도 라벨도 축 이름도 모른다.
    backfill_rules: Mapping[str, Any] | None = None


def open_sim_run(
    conn: Any,
    *,
    sim_run_id: str,
    company_persona_id: str,
    run_type: str,
    period_start: date,
    period_end: date,
    as_of: date,
    status: str,
    financing_mode: str,
    baseline: BaselineLineage,
    opening_finance_state_id: str,
    opening_state_date: date,
    opening_state_type: str,
    opening_fixture_id: str,
    opening_usage_scope: str,
    backfill_rules: Mapping[str, Any] | None = None,
    reset: bool = False,
    note: str | None = None,
    reset_fn: Callable[..., LedgerReset] = reset_sim_run_ledger,
    delete_run_fn: Callable[..., int] = delete_sim_run_row,
    create_fn: Callable[..., str] = create_sim_run,
    seed_fn: Callable[..., str] = seed_opening_finance_state,
    logistics_seed_fn: Callable[..., str] = seed_opening_logistics_fixture,
) -> SimRunOpened:
    """실행을 연다. **다섯을 순서대로 부르고 한 번 커밋한다.**

    :param baseline: 🔴 **호출자가 `financing_mode` 와 함께 명시한다** — 한쪽을 보고
        다른 쪽을 고르지 않는다 (모듈 docstring · 재무 청함).
    :param opening_state_date: 🔴 **재무 씨앗과 물류 씨앗이 둘 다 쓴다.** 물류용
        날짜를 따로 두면 두 파트의 anchor 가 갈린다.
    :param opening_usage_scope: 🔴 **어휘의 주인은 물류다** — 여기 박지 않고 받는다.
    :param backfill_rules: 그 실행이 쓸 백필 규칙 (2026-09-11). 🔴 **어휘의 주인은
        부서다** — 이 문은 규칙 이름도 라벨도 축 이름도 모르고, 받은 것을 그대로
        `config_json` 에 싣는다. 안 주면 그 칸이 아예 안 선다.
    :param reset: 🔴 **기본이 거짓이다.** 거짓이면 지우는 함수 **둘 다 한 번도 안
        부른다** — 장부도 실행 행도 그대로 둔다.
    :raises ValueError: 실행이 이미 있는데 `reset` 을 안 줬을 때. **조용히 덮지 않는다.**
    :raises RuntimeError: `reset` 인데 장부가 남아 실행 행 삭제가 FK 에 막힐 때
        (`delete_sim_run_row`). 🟢 **그 막힘이 자기 검사다.**
    :raises BackfillRuleMissing: `backfill_rules` 가 아는 모양이 아닐 때.
        🔴 **여는 자리에서 터진다** — 179일을 걷고 나서 알면 늦다.
    """
    _assert_openable(conn, sim_run_id=sim_run_id, reset=reset)
    config_json = _config_json(baseline, backfill_rules)

    try:
        # 🔴 **순서가 여기다.** 지우는 것이 맨 앞이고, 실행 행이 서야 시작 상태가
        #    그 축을 가리킬 수 있다 (`finance_states.sim_run_id` 가 `sim_runs` 를
        #    참조하는 FK 다 — 뒤집으면 FK 가 막는다).
        #
        # 🔴 **이 한 자리가 「지운다 / 안 지운다」가 갈리는 유일한 곳이다.**
        #    거짓 쪽으로 서 있는 한 지우는 함수는 이름조차 안 불린다.
        ledger_reset: LedgerReset | None = None
        deleted_run_rows: int | None = None
        if reset:
            ledger_reset = reset_fn(conn, sim_run_id=sim_run_id)
            # 🔴 **장부를 지운 다음, 만들기 전이다.** 실행 행이 남아 있으면 바로
            #    아래 INSERT 가 같은 이름의 PK 에 걸린다 — `--reset` 이 한 번도
            #    성공한 적이 없던 이유가 그것이다.
            #
            # ⚠️ **`ON CONFLICT` 로 풀지 않는다** (모듈 docstring) — 조용히 덮으면
            #   다른 설정으로 만들려던 실행이 옛 행 위에 앉는다.
            #
            # 🟢 장부를 다 안 지웠으면 **이 줄이 FK 에 막힌다.** 그것이 자기 검사다.
            deleted_run_rows = delete_run_fn(conn, sim_run_id=sim_run_id)

        create_fn(
            conn,
            sim_run_id=sim_run_id,
            company_persona_id=company_persona_id,
            run_type=run_type,
            period_start=period_start,
            period_end=period_end,
            as_of=as_of,
            status=status,
            financing_mode=financing_mode,
            # 🟢 **lineage 와 백필 규칙만 싣는다.** 잔액도 한도도 안 싣는다 —
            #    값은 늘 `finance_state_id` 가 가리키는 행에서 읽는다 (`sim_run_open`).
            config_json=config_json,
            note=note,
        )

        seed_fn(
            conn,
            sim_run_id=sim_run_id,
            financing_mode=financing_mode,
            baseline=baseline,
            finance_state_id=opening_finance_state_id,
            state_date=opening_state_date,
            state_type=opening_state_type,
        )

        # 🔴 **재무만 놓고 물류를 안 놓으면 새 실행에 물류 행이 한 행도 없다.**
        #    그때 개장은 상한만큼 거슬러도 anchor 를 못 찾고 거절한다 (`#551`).
        #
        # ★ **날짜가 재무 씨앗과 같은 값이다** — 갈릴 자리를 안 만든다.
        logistics_seed_fn(
            conn,
            sim_run_id=sim_run_id,
            baseline_run_id=baseline.from_sim_run_id,
            fixture_id=opening_fixture_id,
            as_of=opening_state_date,
            usage_scope=opening_usage_scope,
        )
    except Exception:
        # ⚠️ **반쪽 실행을 남기지 않는다.** 되돌리기가 또 터져도 원래 사유를 덮지
        #   않는다 — 무엇이 터졌는지가 먼저다.
        with suppress(Exception):
            conn.rollback()
        raise

    # 🔴 **여기가 유일한 커밋이다.** 넷 중 하나라도 터졌으면 위에서 이미 나갔다.
    conn.commit()
    return SimRunOpened(
        sim_run_id=sim_run_id,
        financing_mode=financing_mode,
        baseline=baseline,
        opening_finance_state_id=opening_finance_state_id,
        opening_logistics_fixture_id=opening_fixture_id,
        period_start=period_start,
        period_end=period_end,
        ledger_reset=ledger_reset,
        deleted_run_rows=deleted_run_rows,
        backfill_rules=backfill_rules,
    )


def _config_json(
    baseline: BaselineLineage, backfill_rules: Mapping[str, Any] | None
) -> dict[str, Any]:
    """실행 행에 실을 설정. **계보 옆에 규칙 칸을 붙인다** (2026-09-11).

    🔴 **칸 이름을 여기서 다시 적지 않는다.** `BACKFILL_CONFIG_KEY` 를 가져다 쓴다 —
      읽는 쪽(`backfill.read_rules`)과 쓰는 쪽이 문자열을 두 벌로 들면, 한쪽만
      고치는 날 **규칙을 실은 실행이 규칙 없는 실행으로 읽힌다.**

    🔴 **여기서 규칙을 검사하지도 지어내지도 않는다.** 아는 모양인지는
      `read_rules` 가 판정한다 — 판정을 두 곳에 두면 언젠가 한쪽만 고쳐지고,
      그때 어느 쪽이 진짜 규칙인지 아무도 못 답한다 (`--commit` · 경계와 같은 규율).

    ★ **안 주면 칸이 아예 안 선다.** 빈 칸을 만들어 두면 *"규칙을 안 정했다"* 와
      *"규칙을 비워 뒀다"* 가 같아지고, 걷기가 그 둘을 못 가른다.
    """
    if backfill_rules is None:
        return dict(baseline.as_config())
    # 🔴 **여는 자리에서 검사한다.** 179일을 걷고 나서 *"모르는 규칙이었다"* 를
    #    알면 늦다 — 그 사이 승인은 한 건도 안 서 있다.
    read_rules({BACKFILL_CONFIG_KEY: backfill_rules})
    return {**baseline.as_config(), BACKFILL_CONFIG_KEY: dict(backfill_rules)}


def _assert_openable(conn: Any, *, sim_run_id: str, reset: bool) -> None:
    """이미 있는 실행 위에 **조용히 앉지 않는다.**

    ★ **번인 가드를 여기서 안 만든다.** `reset_sim_run_ledger` 가 이미 막는다 —
      두 곳에서 막으면 언젠가 한쪽만 고쳐진다.
    """
    if reset:
        return
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT 1 AS present FROM {}.{} WHERE sim_run_id = %s").format(
                sql.Identifier(get_db_schema()),
                sql.Identifier(RUN_TABLE),
            ),
            [sim_run_id],
        )
        if cursor.fetchone() is not None:
            raise ValueError(
                f"실행 {sim_run_id!r} 이 이미 있다 — 조용히 덮지 않는다."
                " 다시 열 것이면 --reset 을 줘라 (🔴 그 실행의 장부를 지운다"
                " · 되돌릴 수 없다)"
            )


# ── 진입점 — 인자만 받는다 ─────────────────────────────────────────────
#
# 🔴 **로직이 여기 없다.** 순서와 트랜잭션은 `open_sim_run` 이, 판단은 `sim_run` 과
#    `sim_run_open` 이 안다. 이 아래는 문자열을 날짜로 바꾸고 결과를 찍는 것뿐이다.


def _parser() -> argparse.ArgumentParser:
    """인자 정의. 🔴 **`--reset` 말고는 기본값이 하나도 없다.**"""
    parser = argparse.ArgumentParser(
        prog="python -m app.master.sim_run_runner",
        description=(
            "새 실행을 연다 — 실행 행 · 시작 재무 상태 · 시작 물류 fixture 를"
            " 한 트랜잭션으로 세운다. 🟡 걷지는 않는다"
        ),
    )
    parser.add_argument("--sim-run-id", required=True, help="새 실행의 이름 · 🔴 기본값 없음")
    parser.add_argument(
        "--company-persona-id", required=True, help="어느 회사 설정 위에서 걷는가 · 🔴 기본값 없음"
    )
    parser.add_argument("--run-type", required=True, help="실행 종류 (예: WALK) · 🔴 기본값 없음")
    parser.add_argument("--period-start", required=True, help="실행 기간 시작 (YYYY-MM-DD)")
    parser.add_argument("--period-end", required=True, help="실행 기간 끝 (YYYY-MM-DD · 포함)")
    parser.add_argument("--as-of", required=True, help="실행 기준일 (YYYY-MM-DD)")
    parser.add_argument("--status", required=True, help="실행 상태 (예: RUNNING) · 🔴 기본값 없음")
    parser.add_argument(
        "--financing-mode",
        required=True,
        help="조달 방식 · 🔴 기본값 없음 — baseline 과 **함께** 명시한다 (재무 청함)",
    )
    parser.add_argument(
        "--baseline-run-id",
        required=True,
        help="어느 실행에서 출발하는가 · 🔴 기본값 없음 — financing-mode 로 추측하지 않는다",
    )
    parser.add_argument(
        "--baseline-state-id",
        required=True,
        help="어느 재무 상태에서 출발하는가 · 🔴 기본값 없음 — 이름을 파싱하지 않는다",
    )
    parser.add_argument(
        "--opening-state-id", required=True, help="새로 만들 시작 재무 상태의 이름 · 🔴 기본값 없음"
    )
    parser.add_argument("--opening-state-date", required=True, help="시작 재무 상태의 날짜")
    parser.add_argument(
        "--opening-state-type", required=True, help="시작 재무 상태의 종류 (예: OPENING)"
    )
    parser.add_argument(
        "--opening-fixture-id",
        required=True,
        help="새로 만들 시작 물류 fixture 의 이름 · 🔴 기본값 없음",
    )
    parser.add_argument(
        "--opening-usage-scope",
        required=True,
        help="어느 usage_scope 를 이관하나 · 🔴 기본값 없음 — 어휘의 주인은 물류다",
    )
    parser.add_argument(
        "--backfill-rules",
        default=None,
        help=(
            "백필 규칙 JSON 파일 경로 (선택) · 🔴 규칙 이름과 라벨의 주인은 부서다"
            " — 이 문은 파일 내용을 읽지 않고 그대로 실행 행에 싣는다"
            " · 안 주면 그 실행은 --auto-approve 로 못 걷는다"
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help=(
            "🔴 그 실행의 장부를 지우고 **실행 행도 지운 뒤** 같은 이름으로 다시 연다"
            " · 되돌릴 수 없다 · 안 주면 한 행도 안 지운다"
        ),
    )
    parser.add_argument("--note", default=None, help="실행 행에 남길 메모 (선택)")
    return parser


def format_summary(opened: SimRunOpened) -> str:
    """결과를 사람이 읽을 줄로. **값을 새로 만들지 않는다.**

    🟡 **마지막 줄이 다음 명령이다.** 이 문은 여는 것까지고 걷는 것은 따로 부른다 —
      무엇을 부르는지 안 적으면 사람이 두 번째 명령을 찾아 헤맨다.
    """
    지움 = (
        "🟢 안 지웠다 — 장부에 손대지 않았다"
        if opened.ledger_reset is None
        else (
            f"🔴 지웠다 (--reset) — {opened.ledger_reset.total_deleted}행"
            f" · {dict(sorted(opened.ledger_reset.deleted.items()))}"
        )
    )
    # 🔴 **실행 행을 지웠다는 사실도 장부와 같은 결로 적는다.** 안 적으면 사람이
    #    *"다시 열었다"* 와 *"처음 열었다"* 를 요약에서 못 가른다.
    #
    # ★ **장부 줄에 뭉치지 않는다.** 장부는 비웠는데 실행 행이 안 지워진 상태가
    #   따로 있고, 한 줄로 합치면 그때 무엇이 안 됐는지가 안 보인다.
    실행행 = (
        "🟢 안 지웠다 — 실행 행에 손대지 않았다"
        if opened.deleted_run_rows is None
        else f"🔴 지우고 다시 넣었다 (--reset) — {opened.deleted_run_rows}행"
    )
    # 🔴 **「규칙을 실었나」를 요약이 말한다.** 안 실은 실행은 `--auto-approve` 로
    #    못 걷고, 그 사실을 여기서 안 보이면 사람이 두 번째 명령에서야 알게 된다.
    규칙 = (
        "🟡 안 실었다 — 이 실행은 --auto-approve 로 못 걷는다"
        if opened.backfill_rules is None
        else f"🔴 실었다 — {dict(sorted(opened.backfill_rules.items()))}"
    )
    # ★ **켜는 것은 명시로만이라 명령줄에도 명시로 붙는다.** 규칙을 안 실은 실행에
    #   이 인자를 적어 주면 사람이 그대로 붙여 넣고 걷기 첫 줄에서 막힌다.
    승인인자 = "" if opened.backfill_rules is None else " --auto-approve"
    return "\n".join(
        [
            f"실행      {opened.sim_run_id}",
            f"기간      {opened.period_start.isoformat()} ~ {opened.period_end.isoformat()}",
            f"조달      {opened.financing_mode}",
            f"출발      {opened.baseline.from_sim_run_id} / {opened.baseline.finance_state_id}",
            f"시작상태  {opened.opening_finance_state_id}",
            f"물류씨앗  {opened.opening_logistics_fixture_id}",
            f"장부      {지움}",
            f"실행행    {실행행}",
            f"백필규칙  {규칙}",
            "",
            "🟡 열었다. 걷지는 않았다 — 걸으려면 다음을 부른다:",
            (
                f"   {WALK_ENTRYPOINT} --sim-run-id {opened.sim_run_id}"
                f" --start {opened.period_start.isoformat()}"
                f" --end {opened.period_end.isoformat()} --now <ISO 8601 · 시간대 필수>"
                f"{승인인자}"
            ),
        ]
    )


def _load_backfill_rules(path: str | None) -> Mapping[str, Any] | None:
    """`--backfill-rules` 가 가리키는 파일을 읽는다. **읽기만 한다.**

    🔴 **내용을 한 글자도 해석하지 않는다.** 객체인지조차 여기서 안 본다 —
      아는 모양인지의 주인은 `backfill.read_rules` 하나이고, 그것이
      `_config_json` 에서 그대로 판정한다. 여기서 한 번 더 보면 판정이 두 곳에
      생기고, 부서가 규칙을 늘리는 날 **문이 먼저 거절한다.**

    ★ **왜 파일인가.** 규칙에 부서의 어휘(라벨·축 이름)가 들어간다. 인자로 쪼개
      받으면 이 문이 *"매입은 라벨을, 판매는 축을 든다"* 는 **모양까지** 알게 되고,
      그 모양은 규칙 이름이 늘어나는 날 갈린다. 파일은 그대로 지나가고, 무엇을
      실었는지는 **파일 하나로 리뷰에 남는다.**
    """
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def main(argv: Sequence[str]) -> int:
    """진입점. **인자만 받아 `open_sim_run` 에 넘긴다.**

    :returns: 열었으면 0. 터졌으면 1 — **조용히 0 을 내지 않는다.**
    """
    args = _parser().parse_args(argv)
    _use_utf8_output()
    conn = get_connection()
    try:
        opened = open_sim_run(
            conn,
            sim_run_id=args.sim_run_id,
            company_persona_id=args.company_persona_id,
            run_type=args.run_type,
            period_start=date.fromisoformat(args.period_start),
            period_end=date.fromisoformat(args.period_end),
            as_of=date.fromisoformat(args.as_of),
            status=args.status,
            financing_mode=args.financing_mode,
            baseline=BaselineLineage(
                from_sim_run_id=args.baseline_run_id,
                finance_state_id=args.baseline_state_id,
            ),
            opening_finance_state_id=args.opening_state_id,
            opening_state_date=date.fromisoformat(args.opening_state_date),
            opening_state_type=args.opening_state_type,
            opening_fixture_id=args.opening_fixture_id,
            opening_usage_scope=args.opening_usage_scope,
            backfill_rules=_load_backfill_rules(args.backfill_rules),
            reset=args.reset,
            note=args.note,
        )
    except Exception as exc:  # noqa: BLE001 - 못 연 것도 **결과**다. 조용히 0 을 내지 않는다.
        print(f"열지 못했다: {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()
    print(format_summary(opened))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
