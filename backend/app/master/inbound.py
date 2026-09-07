"""
inbound.py — **도착 예정이 실제로 들어오는 자리.** 하루의 세 걸음 중 둘째.

🔴 **지금 아프고 있는 자리다** (실측 2026-09-06).

```text
INB-H1-THRU-20260105-BAECHU-1-1   expected_arrival_date = 2026-01-07
그런데 2026-02-06 까지 **32행 내내** in_transit 에 그대로 있다
```

★★ **도착일이 한 달 지났는데 *"오는 중"* 이다.** 입고 실행이 없어서 영원히 안 빠지고,
  그동안 **창고 점유를 계속 먹는다** — `cap_by_date` 가 안 온 물건을 30일 내내 예정으로
  잡는다.

⚠️ **관통을 길게 돌릴수록 창고가 가짜로 찬다.** 목표가 `2026-01-01 ~ 09-13`(209 개장일)
  이 된 이상, 이 진입점이 없으면 그 209일이 안 오는 물건으로 가득 찬다.

🔴 **`day_open` 이 아니다.** 물류가 후보로 물어 왔고(2026-09-06), 아닌 이유가 둘이다.

```text
① day_open 의 계약은 "그날 상태 행을 보장한다" 이고 **아무것도 실행하지 않는다**
   Arrival 이하는 **사건**이다 — 로트를 만들고 재고를 늘린다

② day_open 은 마스터가 **모든 파트에 대해** 부르는 공통 진입점이다
   거기에 물류 전용 실행을 넣으면 **재무가 열릴 때도 입고가 돈다**
```

  ⚠️ 그리고 *"하루를 열었다"* 와 *"물건을 받았다"* 가 한 함수가 되면 **실패했을 때
    무엇이 안 됐는지가 뭉개진다.**

★ **하루의 세 걸음이 각자 멱등하고 각자 실패한다.**

```text
① open_day(as_of)          상태 행을 보장한다        ← 먼저 (적을 자리가 있어야 한다)
② receive_arrivals(as_of)  도착분을 실제로 받는다     ← 여기
③ run_procurement(as_of)   그 위에서 판단한다
```

  ⚠️ **한 함수로 묶지 않는다.** 묶으면 *"왜 실패했나"* 가 뭉개지고, 셋이 각자 멱등할
    때 재시도가 안전하다.

🔴 **날마다다. 실행일이 아니다.** 창고는 토요일에도 물건을 받는다 — `is_open` 은
  **시장이 서는가**이지 창고가 여는가가 아니다. `open_day` 와 같은 결이다
  (`#240` — *"실행일은 평일만, 경과일수는 달력일"*).

⚠️ **파트가 물류 하나다.** 입고가 재고를 늘리면 재무 `inventory_book_value_krw` 도
  움직여야 할 수 있는데, **그 판단은 재무 몫**이라 여기서 정하지 않는다. 등록소를
  파트별로 두는 이유가 그것이다 — 재무가 필요하다고 하면 한 줄로 붙는다.

⚠️ **입고된 뒤에는 취소가 안 된다.** 물건이 창고에 있으면 취소가 아니라 반품이다
  (재무의 *"`SETTLED` 는 fail-closed"* 와 같은 성격). **그 방어는 파트가 세운다** —
  이 모듈은 파트가 거절하면 통째로 롤백한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.day_gate import check_day_gate

__all__ = [
    "PARTS",
    "InboundExecution",
    "InboundOut",
    "InboundPart",
    "InboundPartOut",
    "missing",
    "receive_arrivals",
    "register_inbound",
    "registered",
    "reset",
]

InboundPart = Literal["logistics"]

#: 입고를 실행하는 파트.
#:
#: ★ **지금은 물류 하나다.** 재무·매입은 도착 자체를 실행하지 않는다 — 재무는 지급일에
#:   움직이고 매입은 승인에서 끝난다.
#:
#: ⚠️ **하나짜리 등록소가 과한 것이 아니다.** 이것이 있어야 *"구현이 없다"* 와
#:    *"오늘 받을 것이 없다"* 를 가를 수 있다. 둘은 다른 사실이고, 뭉치면 물류
#:    어댑터가 빠진 날 **조용히 아무 일도 안 일어난다.**
PARTS: tuple[InboundPart, ...] = ("logistics",)


class InboundExecution(Protocol):
    """도착분을 실제로 받는 방식. **물류가 소유한다.**

    ★ **`build` 를 순수하게 나누지 않는다.** *"오늘 무엇이 도착 예정인가"* 를 마스터가
      모르고 **물류가 읽어야** 안다 — `DayOpening` · `ApprovalCancellation` 과 같은
      이유다.

    ★ **`conn` 은 받기만 한다.** commit·rollback·close 를 하지 않는다 — 트랜잭션
      경계는 마스터가 쥔다.

    🔴 **멱등이어야 한다.** 같은 날 두 번 불러도 두 번 입고되면 안 된다. **판정 기준은
       물류가 정한다** — `inbound_id` 로 볼지, 로트 존재로 볼지는 물류 지식이다.

    🔴 **당일 도착만 처리하는 것이 아니다** (물류 회신 2026-09-06 §5).

      ```text
      expected_arrival_date >  as_of   not_due
      expected_arrival_date == as_of   due
      expected_arrival_date <  as_of   **overdue 이지만 여전히 due**   ← 빠뜨리면 안 된다
      ```

      ★ **어느 날 `receive_arrivals` 가 안 돌았어도 다음 날이 밀린 것을 받는다.** 당일만
        보면 그 하루가 영원히 안 오는 물건으로 남고, 지금 아프고 있는 자리가 정확히
        그것이다 (도착 예정 2026-01-07 이 02-06 까지 `in_transit` 에 남아 있다).

      ⚠️ **판정은 파트가 한다.** 마스터는 `as_of` 만 준다 — *"무엇이 도착 자격을
        얻었나"* 는 물류 지식이다.

    🔴 **멱등 축은 `(sim_run_id, inbound_id)` 다** (물류 회신 §6).

      ```text
      도메인 식별          inbound_id
      실행/DB 중복 방지    (sim_run_id, inbound_id)
      ```

      ★ 같은 `inbound_id` 문자열이라도 **다른 `sim_run_id` 는 별개의 실행 장부**다.

      ⚠️ **`sim_run_id` 는 이 Protocol 이 안 받는다.** *"어느 실행의 장부인가"* 는 실행
        정체성이라 **어댑터 생성 인자**로 온다 — `LogisticsTransitionAdapter` ·
        `LogisticsCancellationAdapter` 와 같은 자리이고, 배선(`app/main.py`)에서 눈에
        보이게 주입한다. 호출마다 나르면 마스터가 매번 그 값을 정하는 셈이 된다.

    :param as_of: 받는 날. **달력일**이다 (토·일·공휴일 포함).
    :returns: 무엇을 받았는지. 받을 것이 없으면 `NOTHING_DUE` 이고 그것은 정상이다.
    """

    def receive(self, conn: Any, *, as_of: date) -> InboundPartOut: ...


class InboundPartOut(BaseModel):
    """한 파트의 입고 실행 결과.

    ★ **`NOTHING_DUE` 를 `RECEIVED` 로 접지 않는다.** *"받을 것이 없었다"* 와
      *"받았다"* 는 다른 사실이고, 뭉치면 **도착 예정이 안 잡히는 버그**가 매일
      성공으로 보인다.
    """

    part: str
    status: Literal["RECEIVED", "NOTHING_DUE", "BLOCKED"]
    reason: str = ""
    #: **이번 호출에서 입고 처리 파이프라인을 성공적으로 완료한 건.**
    #:
    #: 🔴 **신규 재고화만이 아니다** (물류 확정 2026-09-07). 이미 재고화된 건의
    #: **멱등 검증**과 **남은 schedule 정리** 성공도 포함한다.
    #:
    #: ```text
    #: 01-28   received=[A]   재고 +3,587kg   신규 재고화 완료
    #: 01-29   received=[A]   재고 불변       기존 재고화 검증 + 남은 일정 정리 완료
    #: ```
    #:
    #: ⚠️ **`received` 포함 여부만으로 이번 호출에서 새 Lot·Move 가 생겼다고 읽지
    #: 않는다.** 신규 재고화 여부의 **권위 사실은 Receipt · Lot · Move 원장**이 갖는다.
    #:
    #: ★ **전에는 *"이번에 실제로 받은 입고 건"* 이라고 적었고 그것이 좁았다.** 물류
    #: `_receive_one` 은 *"마지막 성공 단계 다음부터 이어 처리한다"* 이고,
    #: `PUTAWAY_DONE`·`CLOSED` 여도 `materialize` 로 기존 Lot·Move 를 검증하고 남은
    #: 일정을 걷은 뒤 성공으로 끝낸다 — **그 날도 자기 몫을 다 한 것**이다. 제 문장이
    #: 물류 설계보다 좁아서 실측(2026-09-07 회귀)에서 어긋나 보였다.
    #:
    #: ★ 빈 목록이면 **이 호출에서 완료한 건이 없다** — 이미 다 끝났거나 받을 것이
    #: 없었다. 어느 쪽인지는 `status` 가 말한다.
    received: list[str] = Field(default_factory=list)


class InboundOut(BaseModel):
    """입고 실행 1회의 결과. **`DayOpenOut` 과 같은 세 갈래다.**

    ```text
    RECEIVED     한 파트라도 실제로 받았다
    NOTHING_DUE  **받을 대상이 없었다** — 오늘 도착 예정이 없었거나 미등록이다
    BLOCKED      **받을 대상은 있는데 처리할 수 없다** — purchase 참조 누락 · 깨진 상태
    NOT_OPENED   **그날 장부가 안 열렸다** — 받을 것이 있는지조차 묻지 않았다
    FAILED       받으려다 실패했다 — **아무것도 안 바뀌었다**
    ```

    🔴 **`NOT_OPENED` 를 `BLOCKED` 로 접지 않는다** (물류 물음 2026-09-07 · `§3`).

      ```text
      BLOCKED      받을 대상이 있는데 **그 건이** 처리 불가 — inbound_id 가 나온다
      NOT_OPENED   **아직 아무것도 안 봤다** — 장부가 없어 물어보지도 못했다
      ```

      ⚠️ 접으면 *"도착분에 문제가 있다"* 와 *"어제 개장을 안 돌렸다"* 가 같은 문장으로
        나간다. 고칠 곳이 완전히 다른데 화면은 같아 보인다 — `#316` 에서 `BLOCKED` 를
        `NOTHING_DUE` 로 접었다가 물류가 잡아 준 것과 **같은 병**이다.

      ★ **다음에 할 일도 다르다.** `BLOCKED` 는 그 건을 봐야 하고, `NOT_OPENED` 는
        `open_day` 를 부르면 된다 — 그래서 `next_action` 을 같이 싣는다.

    🔴 **`BLOCKED` 를 `NOTHING_DUE` 로 접지 않는다** (물류 지적 2026-09-06).

      ```text
      NOTHING_DUE   실제로 받을 대상이 없음
      BLOCKED       받을 대상은 존재하지만 처리할 수 없음
      ```

      ⚠️ 접으면 **due 입고가 막힌 상태를 뒤의 orchestration 이 정상으로 오해한다.**
        `purchase_id` 누락이나 깨진 참조로 막힌 날이 *"오늘은 올 게 없었다"* 로 보인다.

      ★ **파트가 `BLOCKED` 면 전체도 `BLOCKED` 다.** 한 파트라도 받았더라도 그렇다 —
        *"받을 게 있었는데 못 받았다"* 가 *"받았다"* 보다 먼저 알려야 하는 사실이다
        (`day_open` 이 `REJECTED_GAP` 을 먼저 보는 것과 같은 판단).
    """

    as_of: date
    status: Literal["RECEIVED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED"]
    reason: str = ""
    parts: list[InboundPartOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: 🔴 **키가 항상 있고 막히지 않았으면 `None` 이다.** `DayGate.next_action` 과 같은
    #: 모양이다 — 칸을 없애면 화면이 `'next_action' in resp` 를 먼저 물어야 한다.
    #: `NOT_OPENED` 일 때 개장 Gate 가 준 값을 **해석하지 않고 그대로** 옮긴다.
    next_action: str | None = None


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# 🔴 **네 번째 등록소다.** 전이(승인이 장부를 바꾸는 방법) · 하루 넘김(하루가 넘어가는
#    방법) · 취소(승인을 물리는 방법) · 여기(도착분을 받는 방법). 한 사전에 섞으면
#    *"전이는 되는데 입고는 안 되는"* 상태를 표현할 수 없고, **지금이 정확히 그
#    상태다.**

_INBOUNDS: dict[InboundPart, Any] = {}


def register_inbound(part: InboundPart, impl: Any) -> None:
    """입고 실행 구현을 등록한다. 물류 모듈이 임포트 시점에 부른다."""
    if part not in PARTS:
        raise ValueError(f"입고 실행 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _INBOUNDS[part] = impl


def registered() -> Mapping[InboundPart, Any]:
    """지금 등록된 입고 실행. **읽기용 사본**이다."""
    return dict(_INBOUNDS)


def missing() -> tuple[str, ...]:
    """아직 입고 실행 구현이 없는 파트. **`PARTS` 순서를 지킨다.**"""
    return tuple(part for part in PARTS if part not in _INBOUNDS)


def reset() -> None:
    """등록을 비운다. 검사용이다."""
    _INBOUNDS.clear()


# ── 경계 ────────────────────────────────────────────────────────────────


def receive_arrivals(as_of: date, *, connect: Any = None) -> InboundOut:
    """`as_of` 에 도착 예정인 것을 **한 트랜잭션으로** 받는다.

    ★ **`open_day` 다음이다.** 상태 행이 있어야 입고를 적을 자리가 있다. 다만 **함수는
      따로다** — 묶으면 실패 원인이 뭉개진다.

    ★ **예외를 밖으로 내지 않는다.** `apply_approval` · `undo_approval` 과 같다 —
      입고 실패가 판단을 멈추면 그날 하루가 통째로 서고, 그건 입고 하나보다 크다.

    🔴 **예외 전파 여부와 후속 진행 여부는 다른 물음이다** (물류 지적 2026-09-06).

      ```text
      예외를 안 올린다        🟢 이 함수의 계약
      그러니 판단을 계속한다   🔴 **그런 뜻이 아니다**
      ```

      ⚠️ **입고가 `FAILED` · `BLOCKED` 인데 매입 판단을 계속하면 현재고와 capacity 가
        실제보다 적게 반영된 상태로 판단한다.** 받았어야 할 물건이 장부에 없는 채로
        *"창고가 비었으니 더 사자"* 가 나온다.

      ★ **부르는 쪽이 정한다.** 이 함수는 상태를 값으로 돌려주고, `run_procurement`
        진행 여부는 그것을 본 orchestration 의 결정이다.

    ⚠️ **달력일이다.** 창고는 토요일에도 받는다. 실행일 달력을 쓰지 않는다.

    ---

    🔴 **개장 Gate 를 먼저 본다** (물류 물음 2026-09-07).

      전에는 *"`open_day` 다음이다"* 라고 **문장으로만** 적어 두고 코드가 아무것도
      안 봤다. 그러면 순서를 지키는 책임이 부르는 쪽에 통째로 있고, 안 지킨 날
      `NOTHING_DUE` 가 나가 *"오늘은 올 게 없었다"* 로 읽힌다.

      ```text
      Gate BLOCKED   →  NOT_OPENED     받을 것이 있는지조차 안 묻는다
      Gate PASS      →  평소대로
      ```

      ★ **`check_day_gate` 는 열지 않는다. 묻기만 한다.** 여기서 `open_day` 를 부르면
        입고가 개장의 부작용이 되고, `router.py` 가 개장에 대해 적어 둔
        *"명시적 호출이다. 실행의 부작용이 아니다"* 를 입고가 어긴다.

      ⚠️ **미등록은 PASS 다** (`day_gate` 계약). 정본 표가 없는 환경에서 이 Gate 가
        입고를 막지 않는다 — 없는 것과 안 열린 것은 다르다.
    """
    gate = check_day_gate(as_of, connect=connect)
    if gate.gate == "BLOCKED":
        return InboundOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=gate.reason,
            # ★ **해석하지 않고 옮긴다.** 무엇을 해야 하는지는 개장이 아는 사실이고,
            #   입고가 다시 판정하면 같은 사실의 주인이 둘이 된다.
            next_action=gate.next_action,
        )

    absent = missing()
    if absent:
        # ★ **미등록은 오류가 아니다.** 그 파트가 아직 입고를 실행하지 않는다는 뜻이고,
        #   *"오늘 받을 것이 없다"* 와 다른 사실이다.
        return InboundOut(
            as_of=as_of,
            status="NOTHING_DUE",
            reason=f"입고 실행 미등록: {', '.join(absent)}",
            missing=list(absent),
        )

    adapters = registered()
    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        results = [adapters[part].receive(conn, as_of=as_of) for part in PARTS]
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 입고 실패가 그날을 통째로 세우면 안 된다.
        conn.rollback()
        return InboundOut(as_of=as_of, status="FAILED", reason=f"입고 실행 실패: {exc}")
    finally:
        conn.close()

    return _aggregate(as_of, results)


def _aggregate(as_of: date, parts: list[InboundPartOut]) -> InboundOut:
    """파트 결과를 전체 어휘로 취합한다.

    ```text
    BLOCKED 가 하나라도   → BLOCKED     받을 게 있었는데 못 받았다
    RECEIVED 가 하나라도  → RECEIVED
    전부 NOTHING_DUE      → NOTHING_DUE
    ```

    🔴 **순서가 계약이다.** `BLOCKED` 를 먼저 보는 이유는 그것이 **사람이 봐야 하는
       사실**이기 때문이다 — `RECEIVED` 뒤로 밀면 *"오늘 받았다"* 로 지나간다.
    """
    blocked = [part for part in parts if part.status == "BLOCKED"]
    if blocked:
        return InboundOut(
            as_of=as_of,
            status="BLOCKED",
            reason=f"받을 것이 있는데 막혔다: {', '.join(part.part for part in blocked)}",
            parts=parts,
        )
    if any(part.status == "RECEIVED" for part in parts):
        return InboundOut(as_of=as_of, status="RECEIVED", parts=parts)
    return InboundOut(as_of=as_of, status="NOTHING_DUE", parts=parts)
