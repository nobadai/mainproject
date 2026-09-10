"""새 실행을 여는 문 — **셋을 한 트랜잭션으로 부른다.**

```text
python -m app.master.sim_run_runner
    --sim-run-id SIM-WALK-202601 --financing-mode LOAN_BASELINE
    --baseline-run-id SIM-BURNIN-202512 --baseline-state-id FIN-DAY30-LOAN
    ...
        → 실행 행 · 시작 재무 상태가 선다 · 🔴 커밋은 여기서 한 번
```

★ **조각은 다 서 있었고 묶는 문이 없었다.**

```text
sim_run.create_sim_run              실행 한 행          (#531)
sim_run_open.seed_opening_finance_state   시작 재무 상태  (#545)
sim_run_open.reset_sim_run_ledger         다시 열 때 장부 비우기  (#545)
```

`#545` 는 **일부러** 진입점을 안 만들었다 — *"돌리는 문은 별도 판이다."* 이 파일이
그 문이다.

---

## 🔴 판단을 여기서 새로 만들지 않는다

이 문이 하는 일은 **셋을 순서대로 부르는 것**뿐이다.

```text
정합성 셋 (출발점이 있는가 · 실행 축이 맞는가 · 조달 방식이 같은가)
                                    → seed_opening_finance_state 안에 있다
번인 실행 거부                        → reset_sim_run_ledger 안에 있다
지울 표 목록 · 지우는 순서             → reset_sim_run_ledger 안에 있다
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
--reset     그때만 장부를 지우고 다시 연다
```

⚠️ **지우는 것은 되돌릴 수 없다.** `backfill_runner` 의 `--commit` 과 같은 규율이다 —
  **기본이 「안 지운다」** 여야 실수 한 번이 영구적인 일이 되지 않는다.

🔴 `--reset` 이어도 **번인 실행이면 거부된다** — `reset_sim_run_ledger` 가 이미 막는다.
   여기서 다시 검사하지 않는다.

---

## 🔴 커밋을 **이 문이** 한다

★ `create_sim_run` 도 `seed_opening_finance_state` 도 커밋을 안 한다 (두 파일이 그렇게
  적어 뒀다). **부르는 쪽이 하는 것**이고, 지금까지 부르는 쪽이 없었다.

```text
🔴 셋이 **한 트랜잭션**이다
   실행 행만 서고 시작 상태가 없으면 첫날 마감이 baseline 을 못 찾는다
   장부만 지워지고 시작 상태 적재가 터지면 **출발점 없는 빈 실행**이 남는다
```

⚠️ 중간에 터지면 **롤백한다.** 반쪽 실행을 남기지 않는다.

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
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from typing import Any

from psycopg import sql

from app.finance.db import get_connection, get_db_schema
from app.master.backtest_runner import _use_utf8_output
from app.master.sim_run import create_sim_run
from app.master.sim_run_open import (
    RUN_TABLE,
    BaselineLineage,
    LedgerReset,
    reset_sim_run_ledger,
    seed_opening_finance_state,
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
    period_start: date
    period_end: date
    #: 지운 결과. 🔴 **`None` 은 「안 지웠다」** — `--reset` 을 안 줬다는 뜻이다.
    #: 0 행을 지운 것과 아예 안 지운 것을 같은 값으로 적지 않는다.
    ledger_reset: LedgerReset | None


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
    reset: bool = False,
    note: str | None = None,
    reset_fn: Callable[..., LedgerReset] = reset_sim_run_ledger,
    create_fn: Callable[..., str] = create_sim_run,
    seed_fn: Callable[..., str] = seed_opening_finance_state,
) -> SimRunOpened:
    """실행을 연다. **셋을 순서대로 부르고 한 번 커밋한다.**

    :param baseline: 🔴 **호출자가 `financing_mode` 와 함께 명시한다** — 한쪽을 보고
        다른 쪽을 고르지 않는다 (모듈 docstring · 재무 청함).
    :param reset: 🔴 **기본이 거짓이다.** 거짓이면 지우는 함수를 **한 번도 안 부른다.**
    :raises ValueError: 실행이 이미 있는데 `reset` 을 안 줬을 때. **조용히 덮지 않는다.**
    """
    _assert_openable(conn, sim_run_id=sim_run_id, reset=reset)

    try:
        # 🔴 **순서가 여기다.** 지우는 것이 맨 앞이고, 실행 행이 서야 시작 상태가
        #    그 축을 가리킬 수 있다 (`finance_states.sim_run_id` 가 `sim_runs` 를
        #    참조하는 FK 다 — 뒤집으면 FK 가 막는다).
        #
        # 🔴 **이 한 줄이 「지운다 / 안 지운다」가 갈리는 유일한 자리다.**
        #    거짓 쪽으로 서 있는 한 지우는 함수는 이름조차 안 불린다.
        ledger_reset = reset_fn(conn, sim_run_id=sim_run_id) if reset else None

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
            # 🟢 **lineage 만 싣는다.** 잔액도 한도도 안 싣는다 — 값은 늘
            #    `finance_state_id` 가 가리키는 행에서 읽는다 (`sim_run_open`).
            config_json=baseline.as_config(),
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
    except Exception:
        # ⚠️ **반쪽 실행을 남기지 않는다.** 되돌리기가 또 터져도 원래 사유를 덮지
        #   않는다 — 무엇이 터졌는지가 먼저다.
        with suppress(Exception):
            conn.rollback()
        raise

    # 🔴 **여기가 유일한 커밋이다.** 셋 중 하나라도 터졌으면 위에서 이미 나갔다.
    conn.commit()
    return SimRunOpened(
        sim_run_id=sim_run_id,
        financing_mode=financing_mode,
        baseline=baseline,
        opening_finance_state_id=opening_finance_state_id,
        period_start=period_start,
        period_end=period_end,
        ledger_reset=ledger_reset,
    )


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
            "새 실행을 연다 — 실행 행 · 시작 재무 상태를 한 트랜잭션으로 세운다. 🟡 걷지는 않는다"
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
        "--reset",
        action="store_true",
        default=False,
        help="🔴 그 실행의 장부를 지우고 다시 연다 · 되돌릴 수 없다 · 안 주면 한 행도 안 지운다",
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
    return "\n".join(
        [
            f"실행      {opened.sim_run_id}",
            f"기간      {opened.period_start.isoformat()} ~ {opened.period_end.isoformat()}",
            f"조달      {opened.financing_mode}",
            f"출발      {opened.baseline.from_sim_run_id} / {opened.baseline.finance_state_id}",
            f"시작상태  {opened.opening_finance_state_id}",
            f"장부      {지움}",
            "",
            "🟡 열었다. 걷지는 않았다 — 걸으려면 다음을 부른다:",
            (
                f"   {WALK_ENTRYPOINT} --sim-run-id {opened.sim_run_id}"
                f" --start {opened.period_start.isoformat()}"
                f" --end {opened.period_end.isoformat()} --now <ISO 8601 · 시간대 필수>"
            ),
        ]
    )


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
