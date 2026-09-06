"""
day_open.py — **하루를 여는 진입점**. 그날 상태 행을 보장한다.

지금 그날 상태 행을 만드는 것은 **승인뿐**이다. 승인이 없는 날은 다음 날 행이 안
생기고, 그러면 두 파트가 같이 막힌다.

```text
물류   logistics_runtime_fixture 를 as_of 정확 일치로 고른다 → LookupError
재무   state_date 가 as_of 와 다르면 fail-closed
```

★ **`open_day` 가 그날 상태 행을 보장한다** — 없으면 전날에서 물려받아 만든다.

🔴 **명시적 호출이다. 실행의 부작용이 아니다.**

```text
🔴 안 함   run_procurement 이 시작할 때 자동으로 연다
🟢 함      누군가 "다음 날로 간다" 를 명시적으로 부른다 (POST /master/days/{as_of}/open)
```

  판단 한 번이 장부를 바꾸면 *"같은 as_of 로 백번 돌려도 같은 답"* 이 깨진다.
  조회하려고 돌린 실행이 상태를 만들면 안 된다. **하루가 넘어가는 것은 사건이지
  부작용이 아니다.**

★ **분담은 `transition.py` 와 같다.**

```text
파트    자기 표의 그날 행을 만든다        무엇을 물려받을지는 파트가 안다
마스터  언제 · 어느 날까지 · 한 트랜잭션   달력과 경계는 마스터 것이다
```

🔴 **이 모듈에 SQL 이 있으면 그 분담이 무너진다.** `test_day_open.py` 가 원문을 읽어
   막는다 — `transition.py` 의 같은 검사와 짝이다.

🔴 **`with conn:` 을 쓰지 않는다.** psycopg3 의 커넥션 컨텍스트 매니저는 블록이
   정상 종료하면 자동으로 commit 한다. 그러면 "커밋은 마스터가 한 번만 한다"는
   규율이 문법에 숨고, 변이 검사(커밋 지우기)도 안 걸린다.

⚠️ **등록된 구현이 아직 없다.** 재무는 미회신이고 물류는 파트 소유다. 등록이 0건이어도
   이 경로가 도는 것이 정상이다 — `transition.py` 가 `#238` 에서 그렇게 만들어졌다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.day_opening_repository import record_day_opening
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.calendar_walk import MAX_WALK_DAYS

__all__ = [
    "MAX_CARRY_DAYS",
    "MAX_FORCE_CARRY_DAYS",
    "PARTS",
    "DayOpenOut",
    "DayOpenPart",
    "DayOpenPartOut",
    "DayOpening",
    "missing",
    "open_day",
    "register_day_opening",
    "registered",
    "reset",
]

DayOpenPart = Literal["finance", "logistics"]

#: 하루 넘김에 참여하는 파트와 **호출 순서**. 사유 문장을 실행마다 같게 만든다.
#:
#: ★ `transition.PARTS` 와 달리 **순서에 의존성이 없다.** 두 파트는 서로 다른 표의
#:   서로 다른 행을 만들고 FK 로 엮이지 않는다 (C.1 — 파트마다 따로 걷는다).
#:   그래도 순서를 고정하는 이유는 사유·응답 문장이 실행마다 같아야 하기 때문이다.
PARTS: tuple[DayOpenPart, ...] = ("finance", "logistics")

#: 뒤로 몇 날까지 열린 날을 찾을 것인가.
#:
#: ⚠️ **넘으면 막고 사유를 낸다 — 행을 만들지 않는다.** 실수로 먼 날을 열면 수백 행이
#:    조용히 생긴다. 번인이 30일이므로 그보다 크게 건너뛰는 것은 의도보다 실수일
#:    가능성이 크다.
#:
#: 🔴 **수를 여기 적어 두지 않는다** (`#282`). 달력을 하루씩 걷는 자리가 마스터에 둘이고
#:    (여기는 뒤로, 실행일 달력은 앞으로) 멈추는 이유가 같다. 같은 수를 두 곳에 적으면
#:    언젠가 한쪽만 바뀐다 — 이유와 수는 `calendar_walk.py` 에 한 번만 있다.
#:
#: ★ **이름은 남긴다.** 하루 넘김의 어휘는 *"물려받는다(carry)"* 이지 *"걷는다"* 가
#:   아니고, 밖에서 이 이름을 부르고 있다 (`tests/master/test_day_open.py`).
MAX_CARRY_DAYS = MAX_WALK_DAYS

#: 🔴 **강제 개장이 푸는 상한.** 평소 상한(`MAX_CARRY_DAYS`)만 풀고 그 이상은 안 연다.
#:
#: ★ **강제 개장이 무한대가 아니다** (계약 §5 · `day_gate` 의 `SPLIT_THRESHOLD_DAYS`).
#:   366일을 넘기면 관리자가 눌러도 안 열리고, 그때는 나눠서 불러야 한다
#:   (`SPLIT_FORCE_OPEN_REQUIRED`).
#:
#: ⚠️ **`day_gate` 와 같은 수여야 한다.** 관문이 *"관리자 강제 개장이 필요하다"* 고
#:    말했는데 눌러도 안 열리면 화면이 왜인지 못 말한다.
MAX_FORCE_CARRY_DAYS = 366


class DayOpening(Protocol):
    """그날 상태 행을 보장한다. **파트가 소유한다.**

    ★ **한 메서드가 아니라 둘인 이유.** `open_day(conn, as_of) -> bool` 한 메서드로는
      마스터가 **어디서부터 채울지를 모른다.** 마지막으로 열린 날이 어디인지 물어볼
      자리가 없으면, 마스터는 as_of 하루만 만들거나 아니면 무한정 뒤로 걸어야 한다.
      앞은 구멍을 남기고 뒤는 상한을 못 건다.

      ```text
      is_open     그날 행이 이미 있는가 — 마스터가 달력을 걷는 데 쓴다
      open_day    carry_from 날 행을 물려받아 as_of 날 행을 만든다
      ```

    🔴 **`commit` 하지 않는다. 자기 커넥션을 열지 않는다.** 커넥션은 마스터가 주고
       커밋은 두 파트가 모두 끝난 뒤 `open_day` 가 한 번 한다.

    ★ **`build` / `persist` 로는 안 나눈다.** `transition.py` 의 두 Protocol 은 순수
      계산과 write 를 갈랐는데, 여기서는 그럴 수 없다.

      ```text
      apply_approval   build 가 순수 계산 — 약정만 있으면 된다. 그래서 나눌 수 있다
      open_day         "전날 행" 을 읽어야 계산이 된다 — DB 없이 못 한다
      ```

    🔴 **물류 구현자에게 — `in_transit` 을 물려받으면 `confirmed_inbound` 도 짝으로
       물려받아야 한다.**

       한쪽만 물려받으면 B-1(`tools.py` `find_in_transit_schedule_gap`)이
       `IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE` 로 다음 날을 세운다.

       ★ **실측으로 겪은 자리다 (2026-09-04).** 승인 전이가 `in_transit` 만 채웠더니
         다음 날 물류가 경계를 못 냈고, `#275` 로 `confirmed_inbound` 를 병합해 풀었다.
         **하루 넘김에서 같은 일이 다시 난다.**

    :param as_of: 행이 설 날.
    :param carry_from: 물려받을 날. **언제나 `as_of` 바로 전날이다** — 마스터가 하루씩
        걸으며 구멍을 남기지 않는다.
    """

    def is_open(self, conn: Any, *, as_of: date) -> bool: ...
    def open_day(self, conn: Any, *, as_of: date, carry_from: date) -> None: ...


class DayOpenPartOut(BaseModel):
    """한 파트의 하루 넘김 결과.

    ★ **파트마다 따로 낸다.** 재무와 물류가 서로 다른 날까지 열려 있을 수 있고, 한
      파트가 막혔다고 다른 파트를 되돌리지 않는다 (C.1).
    """

    part: str
    #: 🔴 **계약 어휘 셋이다** (`260904_마스터_전달_재무물류_open_day_파트계약` §어휘).
    #:
    #:   ```text
    #:   PART_OPENED           이번 호출로 내 몫을 열었다
    #:   PART_ALREADY_OPENED   이미 열려 있었다 (멱등 no-op)
    #:   PART_FAILED           못 열었다
    #:   ```
    #:
    #: ⚠️ 전에는 `OPENED | BLOCKED` 였고 *"이미 열려 있었다"* 를 `opened` 가 빈
    #:    목록인 것으로 표현했다. **계약을 내고 그보다 작게 만든 자리**였다
    #:    (재무가 계약 어휘로 회신해 와서 드러났다 · 2026-09-06).
    status: Literal["PART_OPENED", "PART_ALREADY_OPENED", "PART_FAILED"]
    reason: str = ""
    #: 이번에 만든 날. 오래된 날부터이며 **하루도 건너뛰지 않는다.**
    opened: list[date] = Field(default_factory=list)
    #: 🔴 상한을 넘겨 막혔으면 **밀린 날 수.** 마스터가 전체를 `REJECTED_GAP` 으로
    #:   올리는 근거다.
    #:
    #: ★ **사유 문자열을 읽지 않으려고 칸으로 둔다.** 마스터가 파트의 말을 해석하기
    #:   시작하면 `§3.2.5` 가 무너진다 — 그건 `next_action` 을 횟수로 가르는 것과
    #:   같은 판단이다.
    gap_days: int | None = None


class DayOpenOut(BaseModel):
    """하루 넘김 1회의 결과.

    🔴 **세 값을 섞지 않는다** (`TransitionOut` 과 같은 결).

      ```text
      OPENED         한 파트라도 이번에 열었다
      ALREADY_OPENED 전부 이미 열려 있었다 — 할 일이 없었다
      NOT_OPENED     한 파트라도 실패했다 · 미등록이다 · 커밋이 터졌다
      REJECTED_GAP   상한(31일)을 넘겨 거절했다 — 관리자 강제 개장이 필요하다
      ```

    🔴 **`ALREADY_OPENED` 를 `NOT_OPENED` 로 접지 않는다.** 앞은 *"할 일이 없었다"* 이고
       뒤는 *"못 했다"* 다. 접으면 **매일 도는 정상 상태가 실패로 보인다.**

    ⚠️ 전에는 `OPENED / NOT_OPENED / FAILED` 셋이었다 — 계약이 넷인데 구현이 셋이었고,
       `ALREADY_OPENED` 와 `REJECTED_GAP` 이 `NOT_OPENED` 안에 뭉쳐 있었다.
    """

    as_of: date
    status: Literal["OPENED", "ALREADY_OPENED", "NOT_OPENED", "REJECTED_GAP"]
    reason: str = ""
    #: 파트별 결과. `FAILED` 면 비어 있다 — 전부 되돌렸기 때문이다.
    parts: list[DayOpenPartOut] = Field(default_factory=list)
    #: 아직 구현이 없는 파트.
    missing: list[str] = Field(default_factory=list)


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# `transition.py` 의 전이 등록소와 같은 결이다. 다른 점은 담는 것이다 — 저쪽은
# **승인이 장부를 바꾸는 방법**을 담고 여기는 **하루가 넘어가는 방법**을 담는다.
# 한 사전에 섞으면 전이가 없는 것과 하루 넘김이 없는 것이 같은 문장으로 나가고,
# 둘은 다른 사실이다.

_OPENINGS: dict[DayOpenPart, Any] = {}


def register_day_opening(part: DayOpenPart, impl: Any) -> None:
    """하루 넘김 구현을 등록한다. 재무·물류 모듈이 임포트 시점에 부른다.

    ⚠️ **오늘은 부르는 곳이 없다.** 재무는 미회신이고 물류는 파트 소유다.
    """
    if part not in PARTS:
        raise ValueError(f"하루 넘김 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _OPENINGS[part] = impl


def registered() -> Mapping[DayOpenPart, Any]:
    """지금 등록된 하루 넘김. **읽기용 사본**이다 — 밖에서 넣지 못하게 한다."""
    return dict(_OPENINGS)


def missing() -> tuple[str, ...]:
    """아직 하루 넘김 구현이 없는 파트. **`PARTS` 순서를 지킨다.**

    ★ 순서를 지키는 이유는 사유 문장 때문이다. 집합 순서로 적으면 같은 상황이
      실행마다 다른 문장으로 나가 로그를 비교할 수 없다.
    """
    return tuple(part for part in PARTS if part not in _OPENINGS)


def reset() -> None:
    """테스트 전용 — 등록을 비운다."""
    _OPENINGS.clear()


# ── 달력을 걷는다 ───────────────────────────────────────────────────────


def _walk_part(
    part: DayOpenPart, impl: Any, conn: Any, *, as_of: date, limit: int = MAX_CARRY_DAYS
) -> DayOpenPartOut:
    """한 파트의 달력을 걷는다. **구멍을 남기지 않는다.**

    ```text
    ① as_of 부터 하루씩 뒤로 가며 is_open 이 참인 날을 찾는다 (최대 31일)
    ② 못 찾으면 막고 사유를 낸다 — 행을 지어내지 않는다
    ③ 찾으면 그 다음 날부터 as_of 까지 하루씩 open_day(as_of=d, carry_from=d-1)
    ```

    ★ **마지막 행이 12-31 이고 `as_of` 가 01-05 면 다섯 행을 만든다** — 01-01 · 01-02 ·
      01-03 · 01-04 · 01-05. 01-03 을 건너뛰면 나중에 그날을 조회할 때 또 막힌다.

    🔴 **달력은 날마다다. 실행일이 아니다.** 주말·공휴일도 채운다.
       `execution_day.next_execution_day` 를 **쓰지 않는다** — 판단은 평일만이고
       장부는 날마다다. 토요일에도 재고는 늙고 지급일은 온다 (`#240` · `#242` 가
       정한 *"실행일은 평일만, 경과일수는 달력일"* 과 같은 결).
    """
    anchor: date | None = None
    for back in range(limit + 1):
        day = as_of - timedelta(days=back)
        if impl.is_open(conn, as_of=day):
            anchor = day
            break

    if anchor is None:
        # ★ **막는다.** 여기서 상한을 넘겨 걸으면 수백 행이 조용히 생긴다.
        #
        # 🔴 `gap_days` 를 채운다 — 마스터가 이 칸을 보고 전체를 `REJECTED_GAP` 으로
        #    올린다. 사유 문자열을 읽지 않는다.
        return DayOpenPartOut(
            part=part,
            status="PART_FAILED",
            gap_days=limit,
            reason=(
                f"{as_of} 부터 {limit}일 뒤로 가도 열린 날이 없다 —"
                " 상한을 넘는 것은 의도보다 실수일 가능성이 크다. 행을 만들지 않는다"
            ),
        )

    opened: list[date] = []
    day = anchor + timedelta(days=1)
    while day <= as_of:
        # 🔴 `carry_from` 은 **언제나 바로 전날**이다. 건너뛴 날에서 물려받으면 그
        #    사이 하루치 사실이 장부에 없는 채로 다음 행이 선다.
        impl.open_day(conn, as_of=day, carry_from=day - timedelta(days=1))
        opened.append(day)
        day += timedelta(days=1)

    if not opened:
        # ★ **`PART_ALREADY_OPENED` 다.** anchor 가 `as_of` 자신이라 만들 날이 없었다 —
        #   *"할 일이 없었다"* 이지 *"못 했다"* 가 아니다 (계약 §어휘 · 멱등 no-op).
        return DayOpenPartOut(part=part, status="PART_ALREADY_OPENED")
    return DayOpenPartOut(part=part, status="PART_OPENED", opened=opened)


# ── 트랜잭션 경계 ───────────────────────────────────────────────────────


def open_day(
    as_of: date, *, connect: Callable[[], Any] | None = None, force: bool = False
) -> DayOpenOut:
    """`as_of` 날 상태 행을 **파트마다** 보장한다. 한 트랜잭션이다.

    순서가 이 함수의 전부다.

    ```text
    1. 미등록 확인   → 등록이 0건이면 커넥션을 열지 않는다
    2. 파트마다 걷기 → 한 커넥션으로 · 파트끼리 서로를 되돌리지 않는다
    3. commit 한 번  → 실패하면 rollback
    ```

    ★ **파트마다 따로 걷는다** (C.1). 재무와 물류가 서로 다른 날까지 열려 있을 수
      있고, 한 파트가 뒤처졌다고 다른 파트를 되돌리지 않는다.

    ★ **멱등이다** (C.5). 같은 날을 두 번 열면 두 번째는 아무것도 안 한다 —
      `opened` 가 빈 목록으로 나가는 것이 *"이미 열려 있었다"* 다.

    ★ **미등록은 오류가 아니라 상태다** (`transition.py` · `wiring.py` 와 같은 태도).
      등록이 0건이면 커넥션을 열지 않고 사유와 함께 돌아선다. 한쪽만 등록돼 있으면
      **등록된 쪽만 걷는다** — 두 파트가 서로의 트랜잭션에 얹혀 있지 않기 때문이다
      (`apply_approval` 이 반쪽 반영을 막는 것과 다른 자리다).

    🔴 **예외를 밖으로 던지지 않는다.** 하루 넘김이 500 을 내면 사람이 보기에는 다음
       날로 갈 수 없는 것이 되는데, 실제로는 롤백되어 어제 그대로다. **다만 삼키되
       사유는 반드시 남긴다.**

    🔴 **`force` 는 상한 하나만 푼다** (계약 §5).

      ```text
      강제 개장이 푸는 것    31일 상한 → 366일
      강제 개장이 못 푸는 것 PART_FAILED · 366일 초과
      ```

      ★ **실패를 성공으로 승격시키는 문이 아니다.** 강제 개장 중에도 한 파트가
        실패하면 전체는 계속 `NOT_OPENED` 다.

      ★ **파트는 강제인지 아닌지를 모른다.** Protocol 이 안 바뀌고 평소와 똑같이
        답한다 — 마스터가 상한만 풀고 나머지는 동일하게 취합한다.

      ⚠️ **366일을 넘기면 강제로도 안 열린다.** 관리자가 눌러도 안 열리는 것을
        `ADMIN_FORCE_OPEN_REQUIRED` 로 보내면 화면이 왜인지 못 말하므로,
        `day_gate` 가 그 경우를 `SPLIT_FORCE_OPEN_REQUIRED` 로 따로 낸다.

    :param connect: 커넥션 팩토리. 안 주면 `app.finance.db.get_connection` 을 쓴다 —
                    재무·물류가 같은 DB(같은 `DB_*`)를 쓰므로 커넥션도 하나면 된다.
    :param force: 관리자 강제 개장. **상한만 푼다.**
    """
    absent = missing()
    present = [part for part in PARTS if part in _OPENINGS]
    if not present:
        # ★ 여기서 돌아선다 — **커넥션을 열지 않는다.** 열고 나서 아무 일도 안 하면
        #   빈 트랜잭션이 하루 넘김마다 열렸다 닫힌다.
        out = DayOpenOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=f"하루 넘김 미등록: {', '.join(absent)}",
            missing=list(absent),
        )
        # ★ **미등록도 남긴다.** *"안 열렸다"* 는 사실이고, 화면이 그 날을 지나가지
        #   않으려면 정본에 있어야 한다.
        _record(out)
        return out

    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        limit = MAX_FORCE_CARRY_DAYS if force else MAX_CARRY_DAYS
        parts = [
            _walk_part(part, _OPENINGS[part], conn, as_of=as_of, limit=limit) for part in present
        ]
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 하루 넘김 실패가 500 으로 올라가면 안 된다.
        conn.rollback()
        # ⚠️ 계약 어휘에 `FAILED` 가 없다 — *"한 파트라도 실패하면 전체는
        #    `NOT_OPENED`"* 가 계약이고 커밋 실패도 *"못 열었다"* 다. 무엇이 터졌는지는
        #    `reason` 이 나른다.
        out = DayOpenOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=f"하루 넘김 실패: {exc}",
            missing=list(absent),
        )
        _record(out)
        return out
    finally:
        conn.close()

    out = _aggregate(as_of, parts, absent, force=force)
    _record(out)
    return out


def _record(out: DayOpenOut) -> None:
    """개장 정본에 남긴다. **파트 트랜잭션 밖이다.**

    🔴 **실패도 남아야 시도 횟수를 셀 수 있다.** 파트 트랜잭션 안에 넣으면 롤백될 때
       *"실패했다는 사실"* 까지 사라지고, 그러면 `day_gate` 가 재시도와 사람을 못 가른다.

    ★ **적재 실패가 개장을 죽이지 않는다.** 이력이 없는 것보다 하루를 못 여는 것이
      나쁘다 (`try_save_run` 과 같은 판단).
    """
    record_day_opening(
        as_of=out.as_of,
        sim_run_id=BURN_IN_SIM_RUN_ID,
        result=out.status,
        reason=out.reason,
        parts=out.parts,
    )


def _aggregate(
    as_of: date, parts: list[DayOpenPartOut], absent: tuple[str, ...], *, force: bool = False
) -> DayOpenOut:
    """파트 결과를 **전체 어휘 넷**으로 취합한다 (계약 §어휘).

    ```text
    gap_days 가 있는 파트가 하나라도  → REJECTED_GAP   (상한을 넘겼다)
    PART_FAILED 가 하나라도            → NOT_OPENED     (한 파트라도 실패하면 전체가)
    PART_OPENED 가 하나라도            → OPENED
    전부 PART_ALREADY_OPENED           → ALREADY_OPENED
    ```

    🔴 **순서가 계약이다.** `REJECTED_GAP` 을 먼저 보는 이유는 그것만 *"관리자 강제
       개장"* 이라는 다른 다음 걸음을 갖기 때문이다 — `NOT_OPENED` 로 접으면 화면이
       재시도를 권하고, 재시도로는 안 풀린다.
    """
    gapped = [part for part in parts if part.gap_days is not None]
    if gapped:
        names = ", ".join(part.part for part in gapped)
        limit = MAX_FORCE_CARRY_DAYS if force else MAX_CARRY_DAYS
        # ⚠️ **강제 개장이었으면 그것도 적는다.** 관리자가 눌렀는데 또 거절당한 것이라
        #    다음 걸음이 다르다 — 나눠서 불러야 한다.
        꼬리 = " — 강제 개장으로도 못 연다. 나눠서 불러야 한다" if force else ""
        return DayOpenOut(
            as_of=as_of,
            status="REJECTED_GAP",
            reason=f"상한({limit}일)을 넘겨 거절했다: {names}{꼬리}",
            parts=parts,
            missing=list(absent),
        )
    failed = [part for part in parts if part.status == "PART_FAILED"]
    if failed:
        return DayOpenOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=f"막혔다: {', '.join(part.part for part in failed)}",
            parts=parts,
            missing=list(absent),
        )
    if any(part.status == "PART_OPENED" for part in parts):
        return DayOpenOut(as_of=as_of, status="OPENED", parts=parts, missing=list(absent))
    return DayOpenOut(
        as_of=as_of,
        status="ALREADY_OPENED",
        reason="이미 열려 있었다 — 만든 행이 없다",
        parts=parts,
        missing=list(absent),
    )



