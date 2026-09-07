"""
day_gate.py — **그날이 열렸는가.** 요청 진입 뒤 첫 관문.

🔴 **두 Gate 는 다른 물음이다** (계약 `260904_마스터_통보_개장Gate_응답모양_next_action`).

```text
요청 진입
  → open_day Gate       "그날 장부가 열렸는가"     ← 이 모듈
  → execution day Gate  "그날 판단을 도는가"       execution_day.py
  → Purchase Flow
```

★ **토요일은 첫 관문을 통과하고 둘째에서 막힌다.** 개장은 달력일 전부이고 매입 판단은
  평일만이다 — `#240` 의 *"실행일은 평일만, 경과일수는 달력일"* 이 여기서 두 관문으로
  나타난다.

🔴 **`gate` 를 `result` 와 따로 둔다** (판매 요청).

```text
gate = PASS      OPENED · ALREADY_OPENED
gate = BLOCKED   NOT_OPENED · REJECTED_GAP · NEVER_OPENED
```

  ★ **화면은 `gate` 만 보고 막을지 정한다.** `result` 다섯 중 어느 것이 통과인지를
    화면이 알아야 한다면 **그건 값 파싱과 같다.**

★ **이 모듈은 열지 않는다. 물어보기만 한다.** 여는 것은 `open_day` 이고 별도
  진입점이다 — 판단이 지나가는 길에 장부를 만들면, *"조회했을 뿐인데 행이 생겼다"* 가
  된다.

⚠️ **미등록은 통과다.** 하루 넘김 구현이 없는 파트에 대고 *"안 열렸다"* 고 말할 수
  없다. `apply_approval` 이 미등록을 오류로 안 보는 것과 같은 태도이고, 이것이 없으면
  개장 구현이 붙기 전까지 **모든 판단이 막힌다.**

🟢 **`RETRY_OPEN_DAY` / `CONTACT_OPERATOR` 를 이제 횟수로 가른다** (정본 표가 섰다).

  ```text
  연속 실패 0~1회   → RETRY_OPEN_DAY      다시 부르면 풀릴 수 있다
  연속 실패 2회 이상 → CONTACT_OPERATOR    재시도로 안 풀린다
  ```

  ★ **`attempt_count` 가 아니라 `failure_count`(연속 실패)를 본다.** 어제 성공하고
    오늘 처음 실패한 것을 *"2번째"* 로 세면 **재시도 한 번 없이 사람을 부른다.**

  ⚠️ **정본을 못 읽으면 근사한다.** 그때는 `RETRY_OPEN_DAY` 를 내고 **근사라는 것을
    사유에 적는다** — 못 읽었다고 판단을 멈추지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.calendar_walk import MAX_WALK_DAYS
from app.master.day_open import PARTS, registered
from app.master.day_opening_repository import read_day_opening
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

__all__ = ["DayGate", "FailedPart", "check_day_gate"]

#: 366일을 넘기면 관리자 강제 개장으로도 못 연다 — 합의된 절대 상한이다.
#:
#: ★ `ADMIN_FORCE_OPEN_REQUIRED` 로 보내면 관리자가 눌렀는데 안 열리고, 화면은 왜인지
#:   못 말한다. 그래서 `SPLIT_FORCE_OPEN_REQUIRED` 를 따로 둔다.
SPLIT_THRESHOLD_DAYS = 366

#: 마지막 개장을 **뒤로 몇 날까지 찾을 것인가.**
#:
#: 🔴 `SPLIT_THRESHOLD_DAYS` 보다 커야 한다. 366 에서 멈추면 *"366일 넘게 밀렸다"* 와
#:    *"한 번도 안 열었다"* 가 **같아 보이고**, 둘은 다음 걸음이 다르다
#:    (`SPLIT_FORCE_OPEN_REQUIRED` vs `OPEN_DAY_REQUIRED`).
#:
#: ⚠️ **이 너머는 못 가른다.** 2년을 걸어도 못 찾으면 `NEVER_OPENED` 로 적고, 그것이
#:    *"정말 처음"* 인지 *"2년 넘게 밀림"* 인지는 이 Protocol 로 알 수 없다 —
#:    `is_open(day)` 하나로는 *"마지막으로 열린 날"* 을 물을 수 없기 때문이다.
#:    **그 한계를 사유에 적는다.**
#:
#: 🟡 막힌 날에만 걷는다. 통과하는 날은 하루만 묻고 끝난다.
SEARCH_LIMIT_DAYS = 730


class FailedPart(BaseModel):
    """열지 못한 파트 하나. **`NOT_OPENED` 일 때만 채워진다.**"""

    part: str
    reason: str = ""


class DayGate(BaseModel):
    """개장 관문 응답. **계약 §1 의 여덟 칸 그대로다.**"""

    as_of: date
    #: 🔴 **화면은 이것만 보고 막는다.** `result` 를 해석하게 두지 않는다.
    gate: Literal["PASS", "BLOCKED"]
    result: Literal["OPENED", "ALREADY_OPENED", "NOT_OPENED", "REJECTED_GAP", "NEVER_OPENED"]
    #: 마지막으로 열린 날. 못 찾으면 `None`.
    last_opened_date: date | None = None
    #: `last_opened_date` 와 `as_of` 의 **달력일** 차이. 못 찾으면 `None`.
    gap_days: int | None = None
    failed_parts: list[FailedPart] = Field(default_factory=list)
    reason: str = ""
    #: 🔴 **키가 항상 있고 `PASS` 면 `None` 이다.** 칸을 없애면 화면이
    #: `'next_action' in resp` 를 먼저 물어야 하고, 그게 판매가 피하자고 한 모양이다.
    next_action: (
        Literal[
            "OPEN_DAY_REQUIRED",
            "RETRY_OPEN_DAY",
            "ADMIN_FORCE_OPEN_REQUIRED",
            "SPLIT_FORCE_OPEN_REQUIRED",
            "CONTACT_OPERATOR",
        ]
        | None
    ) = None


def _passed(as_of: date) -> DayGate:
    return DayGate(as_of=as_of, gate="PASS", result="ALREADY_OPENED", last_opened_date=as_of)


def check_day_gate(as_of: date, *, connect: Callable[[], Any] | None = None) -> DayGate:
    """그날이 열렸는지 **물어보기만** 한다. 열지 않는다.

    ★ **등록된 파트 전부가 열려 있어야 통과다.** 하나라도 안 열렸으면 그 날 장부는
      온전하지 않고, 그 위에서 판단하면 **없는 상태를 읽거나 남의 날 상태를 읽는다.**

    ⚠️ **예외를 밖으로 내지 않는다.** 관문이 500 을 내면 판단이 아예 안 도는데, 못 물어본
      것과 안 열린 것은 다르다 — 못 물어보면 `BLOCKED` + `CONTACT_OPERATOR` 다.
    """
    present = [part for part in PARTS if part in registered()]
    if not present:
        # ⚠️ 물어볼 데가 없다. **없는 구현에 대고 "안 열렸다" 고 말하지 않는다.**
        return _passed(as_of)

    adapters = registered()
    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        closed = [part for part in present if not adapters[part].is_open(conn, as_of=as_of)]
        if not closed:
            return _passed(as_of)
        last, gap = _last_opened(adapters, present, conn, as_of=as_of)
    except Exception as exc:  # noqa: BLE001 - 못 물어본 것과 안 열린 것은 다르다.
        return DayGate(
            as_of=as_of,
            gate="BLOCKED",
            result="NOT_OPENED",
            reason=f"개장 여부를 못 읽었다: {type(exc).__name__}",
            next_action="CONTACT_OPERATOR",
        )
    finally:
        conn.close()

    return _blocked(as_of, closed=closed, last=last, gap=gap, connect=connect)


def _last_opened(
    adapters: Any, present: list[str], conn: Any, *, as_of: date
) -> tuple[date | None, int | None]:
    """마지막으로 열린 날과 그 간격. **모든 파트가 열려 있던 가장 최근 날이다.**

    ★ 한 파트만 열려 있던 날은 *"그 날이 열렸다"* 가 아니다 — 전체 어휘의 기준과 같다.

    ⚠️ `SEARCH_LIMIT_DAYS` 까지만 걷는다. 못 찾으면 `(None, None)` 이고 그것이
      `NEVER_OPENED` 다 — 그 너머는 이 Protocol 로 못 가른다.
    """
    from datetime import timedelta

    for back in range(1, SEARCH_LIMIT_DAYS + 1):
        day = as_of - timedelta(days=back)
        if all(adapters[part].is_open(conn, as_of=day) for part in present):
            return day, back
    return None, None


def _blocked(
    as_of: date,
    *,
    closed: list[str],
    last: date | None,
    gap: int | None,
    connect: Callable[[], Any] | None = None,
) -> DayGate:
    """막힌 이유와 다음 걸음. **판정 규칙은 계약 §2 그대로다.**

    ```text
    NEVER_OPENED  · gap 없음          → OPEN_DAY_REQUIRED
    REJECTED_GAP  · 31 < gap ≤ 366    → ADMIN_FORCE_OPEN_REQUIRED
    REJECTED_GAP  · gap > 366         → SPLIT_FORCE_OPEN_REQUIRED
    NOT_OPENED    · gap ≤ 31          → RETRY_OPEN_DAY  🟡 횟수를 아직 안 센다
    ```
    """
    failed = [FailedPart(part=part, reason="그 날짜 상태가 없다") for part in closed]
    if gap is None:
        return DayGate(
            as_of=as_of,
            gate="BLOCKED",
            result="NEVER_OPENED",
            failed_parts=failed,
            reason=(
                f"{as_of.isoformat()} 부터 {SEARCH_LIMIT_DAYS}일을 뒤로 걸어도 열린 날이"
                " 없다 — 한 번도 안 열었거나 그보다 더 밀렸다."
                " 이 Protocol 로는 둘을 못 가른다"
            ),
            next_action="OPEN_DAY_REQUIRED",
        )
    if gap > SPLIT_THRESHOLD_DAYS:
        action = "SPLIT_FORCE_OPEN_REQUIRED"
        why = f"{gap}일이 밀렸다 — 한 번에 못 연다. 나눠서 불러야 한다"
    elif gap > MAX_WALK_DAYS:
        action = "ADMIN_FORCE_OPEN_REQUIRED"
        why = f"{gap}일이 밀려 상한({MAX_WALK_DAYS}일)을 넘었다 — 관리자 강제 개장이 필요하다"
    else:
        # 🟢 **연속 실패 횟수로 가른다** (계약 §2 · 정본 표가 섰다).
        # 🔴 **`connect` 를 넘긴다.** 안 넘기면 호출자가 대역을 줘도 이 한 줄만
        #    실 DB 로 샌다 — 그 사이 표가 생기면 검사가 조용히 다른 것을 잰다.
        #    실제로 그랬다: `master_day_openings` 를 공유 DB 에 적용한 날
        #    (2026-09-07) 대역을 쓰던 검사가 빨간불이 됐다. **안 터지던 이유가
        #    '표가 없다' 였고, 그 이유가 사라진 것이다.**
        record = read_day_opening(as_of=as_of, sim_run_id=BURN_IN_SIM_RUN_ID, connect=connect)
        if record is None:
            # ⚠️ 못 읽었거나 한 번도 안 불렀다. **근사하되 근사라고 적는다.**
            꼬리 = " 개장 정본이 없어 첫 실패로 본다"
            action = "RETRY_OPEN_DAY"
        elif record.failure_count >= 2:
            꼬리 = f" 연속 {record.failure_count}회 실패했다 — 재시도로 안 풀린다"
            action = "CONTACT_OPERATOR"
        else:
            꼬리 = f" 연속 실패 {record.failure_count}회 — 다시 부르면 풀릴 수 있다"
            action = "RETRY_OPEN_DAY"
        return DayGate(
            as_of=as_of,
            gate="BLOCKED",
            result="NOT_OPENED",
            last_opened_date=last,
            gap_days=gap,
            failed_parts=failed,
            reason=f"{', '.join(closed)} 가 안 열렸다 (마지막 개장 {last} · {gap}일 전).{꼬리}",
            next_action=action,  # type: ignore[arg-type]
        )
    return DayGate(
        as_of=as_of,
        gate="BLOCKED",
        result="REJECTED_GAP",
        last_opened_date=last,
        gap_days=gap,
        failed_parts=failed,
        reason=why,
        next_action=action,  # type: ignore[arg-type]
    )
