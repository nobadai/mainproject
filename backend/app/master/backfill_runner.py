"""자동 백필 진입점 — **세어 보이는 것이 기본이고, `--commit` 만 적는다.**

```text
python -m app.master.backfill_runner
    --sim-run-id SIM-WALK-202601 --start 2026-02-07 --end 2026-09-09
        → 세어서 보여 준다 · 🟢 한 행도 안 쓴다

같은 명령에 --commit 을 더하면
        → 그때만 적는다
```

★ **`backfill.py` 에 진입점을 안 만들었다.** 그쪽은 *"CLI 진입점이 없다 — 일부러
  없다"* 를 자기 문서와 검사로 잠갔고, 그 잠금을 풀면 **판단이 사는 파일과 문이 사는
  파일이 같아진다.** 문은 여기 있고 판단은 저기 있다 — `backtest_runner` 가 `walk`
  과 `main` 을 한 파일에 둔 것과 다른 선택이고, 다른 이유는 저 잠금이다.

---

## 🔴 문에 붙은 가드 — 셋

```text
① 기본이 「안 쓴다」        --commit 없이는 승인 문을 한 번도 안 부른다
② --sim-run-id 필수        기본값 없음 · 안 주면 터진다
③ 번인 실행이면 터진다      SIM-BURNIN-202512 은 모든 실행의 기초 상태다
```

★★ **왜 「세어 보이는 것」이 기본인가.** `master_decisions` 는 append-only 라 못
  지운다. 179일 × 3품목의 승인이 한 번 들어가면 되돌릴 방법이 없고, 그래서 기본이
  쓰는 쪽이면 **실수 한 번이 영구적인 일**이 된다.

⚠️ **경계(`BACKFILL_BOUNDARY_AS_OF`)를 여기서 다시 검사하지 않는다.** `backfill.py`
  안의 상수가 이미 행마다 막는다 — 두 곳에서 막으면 언젠가 한쪽만 고쳐지고, 그때
  어느 쪽이 진짜 경계인지 아무도 모른다. 이 파일은 **막힌 날을 세어 보이기만** 한다.

---

## 🔴 이 진입점은 **불러야 돈다**

`bootstrap` 에 안 끼운다. 저절로 승인이 나면 **자동 승인을 명시로만 켠다는 규율이
프로세스 시작 한 번으로 뚫린다** — 사람이 이 명령을 직접 쳐야 장부가 선다.

⚠️ **걷기 안에도 승인 자리가 생겼다** (2026-09-11 · `scheduler.run_scheduled_day`).
  거기는 `backfill_decisions` 를 부르지 이 파일을 부르지 않고, **기본이 꺼짐**이라
  `--auto-approve` 를 명시로 줘야 선다 — 규율은 같고 스위치가 하나 더 있는 것이다.

```text
이 문           과거 구간을 **한 번에** 채운다     · 기본이 「안 쓴다」(--commit)
걷기 안 승인    걷는 날마다 **그 자리에서** 선다   · 기본이 「안 켠다」(--auto-approve)
```

★ **왜 둘 다 있나.** 걷기 안 승인이 없으면 **다음 날이 어제 산 것을 못 본다** —
  179일을 걸어도 재고가 안 쌓인다. 이 문은 **이미 걸어 둔 과거**를 채우는 자리다.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from app.master.backfill import BackfillOut, backfill_decisions
from app.master.backtest_runner import _use_utf8_output
from app.master.bootstrap import wire_registries
from app.master.decision import DecisionIn, DecisionOut
from app.master.decision_service import record_decision
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

__all__ = [
    "BackfillRunOut",
    "CountingDoor",
    "NotWritten",
    "format_summary",
    "main",
    "run_backfill",
]


@dataclass(frozen=True)
class NotWritten:
    """세어 보기가 승인 문 자리에서 돌려주는 값. **적히지 않았다는 사실 그 자체.**

    🔴 **`DecisionOut` 을 흉내 내지 않는다.** 결정 번호도 적재 시각도 지어내지
      않는다 — 지어내면 *"적힌 결정"* 과 *"적었을 결정"* 이 같은 타입으로 흘러다니고,
      그 둘을 되가를 방법이 없어진다.

    ★ `revalidation_outcome` 이 `None` 인 것은 정직하다. **재검증을 안 돌렸기
      때문**이고, `DecisionOut` 도 그 칸의 `None` 을 *"재검증을 하지 않았다"* 로
      읽는다 — 같은 뜻을 같은 값으로 적는다.
    """

    revalidation_outcome: None = None


class CountingDoor:
    """`--commit` 없이 돌 때 **승인 문 자리에 서는 것.** 세기만 하고 안 적는다.

    ★★ **이것은 대역이 아니라 세어 보기의 본체다.** 검사용 가짜가 아니라 운영
      경로의 한 갈래이고, 하는 일이 *"적지 않는다"* 라 흉내 낼 것도 없다.

    🔴 **판단을 여기서 다시 하지 않는다.** 경계 · 종료 코드 · 규칙 · 기존 결정 ·
      안의 유무는 `backfill_decisions` 가 그대로 다 본다. 이 자리에 오는 것은 **그
      다섯 관문을 전부 통과한 행**뿐이라, 세어 보이기의 `RECORDED` 수는 `--commit`
      을 줬을 때 실제로 적힐 수와 **같은 판단에서 나온 수**다.

    ⚠️ 그래서 요약이 *"적었다"* 를 말하면 안 된다 — 판단은 같아도 **적지는 않았다.**
    """

    def __init__(self) -> None:
        #: 문까지 온 것들. 🔴 **수의 주인이 아니다** — 어휘별 수의 주인은
        #: `BackfillOut.outcomes` 하나이고, 이 목록은 되짚을 때 눈으로 보는 것뿐이다.
        self.would_record: list[tuple[str, DecisionIn]] = []

    def __call__(self, request_id: str, payload: DecisionIn) -> NotWritten:
        self.would_record.append((request_id, payload))
        return NotWritten()


@dataclass(frozen=True)
class BackfillRunOut:
    """진입점 한 번의 결과. **「적었는가」를 값으로 든다.**

    🔴 **`committed` 를 결과에서 빼지 않는다.** 빼면 요약을 읽는 사람이 *"세어 본
      것"* 과 *"적은 것"* 을 문장으로만 구별하게 되고, 그 문장은 언제든 바뀐다.
    """

    #: 승인 문을 실제로 지났는가. `False` 면 **한 행도 안 썼다.**
    committed: bool
    #: 백필이 낸 값 그대로. ⚠️ **여기서 다시 세지 않는다** — 어휘별 수의 주인은 저쪽이다.
    result: BackfillOut


def run_backfill(
    *,
    sim_run_id: str,
    start: date,
    end: date,
    commit: bool = False,
    backfill_fn: Callable[..., BackfillOut] = backfill_decisions,
    door: Callable[[str, DecisionIn], DecisionOut] = record_decision,
) -> BackfillRunOut:
    """가드를 지나고 백필을 부른다. **`commit` 이 참일 때만 승인 문을 넘긴다.**

    :param sim_run_id: 어느 실행의 장부를 채우는가. 🔴 **기본값이 없다 — 빈
        문자열도 막는다** (`backtest_runner.walk` 과 같은 규율). 어느 실행에
        쌓는지가 곧 그 장부의 정체다.
    :param commit: 🔴 **기본이 거짓이다.** 거짓이면 승인 문을 한 번도 안 부르고,
        `master_decisions` 에 한 행도 안 쓴다.
    :param backfill_fn: 백필 본체. 기본이 `backfill_decisions` 자체다.
    :param door: 🔴 **승인 문.** 기본이 `record_decision` 자체다 — `None` 을 안
        받는다. `commit` 이 거짓이면 이 값은 **한 번도 안 쓰인다.**
    :raises ValueError: `sim_run_id` 가 비었거나 **번인 실행**일 때.
    """
    if not sim_run_id.strip():
        raise ValueError(
            "sim_run_id 없이는 백필할 수 없다 — 어느 실행의 장부인지가 그 장부의 정체다."
            " 실행을 먼저 만들고(`sim_run.create_sim_run`) 그 이름을 넘겨라"
        )
    if sim_run_id == BURN_IN_SIM_RUN_ID:
        # 🔴 **번인은 모든 실행의 기초 상태다.** 여기에 자동 승인이 얹히면 사람이
        #    심어 둔 장부가 오염되고, `master_decisions` 는 append-only 라 못 지운다.
        raise ValueError(
            f"번인 실행({BURN_IN_SIM_RUN_ID})은 자동으로 안 채운다"
            " — 모든 실행이 그 장부를 기초 상태로 읽는다. 채울 실행을 따로 만들어라"
        )

    # 🔴 **여기가 「쓴다 / 안 쓴다」가 갈리는 유일한 자리다.** 아래 한 줄이 거짓
    #    쪽으로 서 있는 한 승인 문은 이름조차 안 불린다.
    decide: Callable[..., object] = door if commit else CountingDoor()
    result = backfill_fn(sim_run_id=sim_run_id, start=start, end=end, decide=decide)
    return BackfillRunOut(committed=commit, result=result)


# ── 진입점 — 인자만 받는다 ─────────────────────────────────────────────
#
# 🔴 **로직이 여기 없다.** 가드는 `run_backfill` 이, 판단은 `backfill_decisions` 가
#    안다. 이 아래는 문자열을 날짜로 바꾸고 결과를 사람이 읽게 찍는 것뿐이다.


def _parser() -> argparse.ArgumentParser:
    """인자 정의. 🔴 **실행 축에도 날짜에도 기본값이 없고, `--commit` 은 꺼져 있다.**"""
    parser = argparse.ArgumentParser(
        prog="python -m app.master.backfill_runner",
        description=(
            "과거 구간의 승인을 규칙대로 채운다."
            " 🔴 기본은 세어 보이기다 — 적으려면 --commit 을 줘야 한다"
        ),
    )
    parser.add_argument(
        "--sim-run-id",
        required=True,
        help="어느 실행의 장부인가 (예: SIM-WALK-202601) · 🔴 기본값 없음 · 번인은 거부한다",
    )
    parser.add_argument("--start", required=True, help="백필 시작일 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="백필 종료일 (YYYY-MM-DD · 포함)")
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="🔴 실제로 적는다. 안 주면 세어서 보여 주기만 하고 한 행도 안 쓴다",
    )
    return parser


def format_summary(run: BackfillRunOut) -> str:
    """결과를 사람이 읽을 줄로. **값을 새로 만들지 않는다.**

    🔴 **「썼는가」를 첫 줄에 둔다.** 조용히 안 쓰면 사람이 썼다고 믿는다 —
      요약 어디에 묻혀 있으면 안 읽히고, 안 읽히면 없는 것과 같다.
    """
    result = run.result
    written = (
        "🔴 적었다 (--commit) — 승인 문을 지났다"
        if run.committed
        else "🟢 안 썼다 — master_decisions 에 한 행도 안 들어갔다. 쓰려면 --commit 을 줘라"
    )
    lines = [
        f"실행      {result.sim_run_id}",
        f"범위      {result.start.isoformat()} ~ {result.end.isoformat()}",
        f"쓰기      {written}",
        f"상태      {result.status}",
        # ★ 어휘별로 나눠 보인다. 🔴 **접지 않는다** — 무엇이 왜 안 채워졌는지를
        #   이 한 줄이 답한다.
        f"어휘      {dict(sorted(result.outcomes.items()))}",
        f"본 행     {len(result.runs)}건",
    ]
    if result.reason is not None:
        lines.append(f"사유      {result.reason}")
    if result.rules is not None:
        lines.append(f"규칙      매입 {result.rules.procurement} · 판매 {result.rules.sales}")
    if result.blocked_days:
        # ⚠️ **조용히 자르지 않는다.** 경계 밖이라 자동으로는 못 채우는 날이다 —
        #    `backfill.py` 가 이미 막았고, 여기서는 그 사실을 보이기만 한다.
        first = result.blocked_days[0].isoformat()
        last = result.blocked_days[-1].isoformat()
        lines.append(f"경계 밖   {len(result.blocked_days)}일 ({first} ~ {last}) — 사람만 승인한다")
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    """진입점. **인자만 받아 `run_backfill` 에 넘긴다.**

    :returns: 규칙대로 다 돌았고 터진 행이 없으면 0. 아니면 1 — **조용히 0 을 내지
        않는다.** 세어 보기로 돌아도 0 이다 (세는 데 성공했다).
    """
    args = _parser().parse_args(argv)
    _use_utf8_output()
    # 🔴 **등록소를 채운다.** 이 진입점은 `app/main.py` 를 안 거치므로, 이 줄이
    #    없으면 재검증이 빈 등록소 위에서 돈다 (`backtest_runner.main` 과 같은 이유).
    #
    # ★ **여기서 무엇을 등록할지 정하지 않는다.** 목록의 주인은 `bootstrap` 하나다.
    wire_registries()
    run = run_backfill(
        sim_run_id=args.sim_run_id,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        commit=args.commit,
    )
    print(format_summary(run))
    return 0 if run.result.status == "RAN" and not run.result.outcomes.get("FAILED") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
