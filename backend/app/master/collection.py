"""
collection.py — **채권이 실제로 돈으로 들어오는 자리.** 다섯째 등록소.

🔴 **부르는 곳이 0건이다** (실측 2026-09-07).

```text
app/finance/collection.py :196   apply_explicit_collection(conn, CollectionEvent(...))
그런데 이것을 부르는 production 호출자가 **0건**이다 — 검사 파일 하나가 전부다
```

★★ **재무 경계는 이미 서 있다.** 누적 target 을 delta 로 접는 전이도, 역행·초과를
  막는 불변식도, 멱등도 `app/finance/collection.py` 안에 다 있다. **없는 것은 그것을
  날마다 부르는 자리**이고, 이 등록소가 그 자리를 만든다.

⚠️ `register_inbound` 호출이 0건이던 것(`#337`)과 **같은 모양**이다 — 구현이 다 섰는데
  배선이 없어 매일 조용히 아무 일도 안 일어났다.

---

★ **재무 결정 2026-09-07.**

```text
Finance   실제 수금 사건의 정본 의미 소유 — receivable_id · collection_date
          · cumulative target_received_total_krw · (sim_run_id, financing_mode) 축
Master    날짜에 해당하는 사건을 **운반하고 호출**한다.
          수금 대상이나 수금액을 **스스로 결정하지 않는다**
Sales     계약 결제조건·채권 발생의 상업적 원천 — "오늘 얼마 입금됐다"는 안 만든다
```

🔴 **due_date 경과 ≠ 자동 수금. fixture 를 만들 때도 그렇다.**

  ⚠️ *"기일이 지났으니 들어왔겠지"* 는 **마스터가 재무 사실을 발명하는 것**이다. 실제
    입금은 안 됐는데 장부에 현금이 늘고, 그 현금으로 매입 판단이 돈다.

🔴 **마스터는 `as_of` 만 준다 — `InboundExecution` 과 같은 이유다.** *"오늘 무엇이
   수금됐나"* 를 마스터가 모르고 **재무가 읽어야** 안다.

---

★ **하루의 걸음이 각자 멱등하고 각자 실패한다.**

```text
① open_day(as_of)          상태 행을 보장한다        ← 먼저 (적을 자리가 있어야 한다)
② receive_arrivals(as_of)  도착분을 실제로 받는다
③ collect_receipts(as_of)  수금 사건을 반영한다       ← 여기
④ run_procurement(as_of)   그 위에서 판단한다
```

  ⚠️ **`run_procurement` 안에 넣지 않는다.** 넣으면 판단 한 번이 현금을 움직이고
    *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다 — 개장·입고를 판단 밖에 둔
    이유와 같다.

🔴 **날마다다. 실행일이 아니다.** 입금은 토요일에도 찍힌다 — `is_open` 은 **시장이
  서는가**이지 은행이 여는가가 아니다. `open_day` · `receive_arrivals` 와 같은 결이다
  (`#240` — *"실행일은 평일만, 경과일수는 달력일"*).

⚠️ **파트가 재무 하나다.** 수금이 현금을 늘리면 판매 쪽 채권 잔액도 움직여야 할 수
  있는데, **그 판단은 재무 몫**이라 여기서 정하지 않는다. 등록소를 파트별로 두는
  이유가 그것이다 — 판매가 필요하다고 하면 한 줄로 붙는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.day_gate import check_day_gate
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_binding import bind_sim_run

__all__ = [
    "PARTS",
    "CollectionOut",
    "CollectionPart",
    "CollectionPartOut",
    "CollectionSource",
    "collect_receipts",
    "missing",
    "register_collection",
    "registered",
    "reset",
]

CollectionPart = Literal["finance"]

#: 수금을 실행하는 파트.
#:
#: ★ **지금은 재무 하나다.** 판매는 계약 결제조건과 채권 발생의 상업적 원천이지
#:   *"오늘 얼마 입금됐다"* 를 만들지 않는다. 물류·매입은 현금 흐름에 손대지 않는다.
#:
#: ⚠️ **하나짜리 등록소가 과한 것이 아니다.** 이것이 있어야 *"구현이 없다"* 와
#:    *"오늘 들어올 것이 없다"* 를 가를 수 있다. 둘은 다른 사실이고, 뭉치면 재무
#:    구현체가 빠진 날 **조용히 아무 일도 안 일어난다.**
PARTS: tuple[CollectionPart, ...] = ("finance",)


class CollectionSource(Protocol):
    """그날 수금 사건을 반영하는 방법. **재무가 소유한다.**

    🔴 **마스터는 `as_of` 만 준다 — 어느 채권을 얼마 수금할지는 재무 사실이다.**
      `InboundExecution` 과 같은 이유다: *"오늘 무엇이 자격을 얻었나"* 를 마스터가
      모르고 파트가 읽어야 안다.

    ⚠️ **`due_date` 경과를 수금으로 읽지 않는다.** 기일이 지난 것과 돈이 들어온 것은
      다른 사실이고, 접으면 **없는 현금으로 매입 판단이 돈다.**

    ★ **`conn` 은 받기만 한다.** commit·rollback·close 를 하지 않는다 — 트랜잭션
      경계는 마스터가 쥔다.

    🔴 **멱등이어야 한다.** 같은 날 두 번 불러도 두 번 수금되면 안 된다. 재무 전이가
      **누적 target** 으로 적히는 것이 그 근거다 — 같은 target 을 두 번 넣으면 delta 가
      0 이다 (`app/finance/collection.py` 의 *"cumulative collection cannot regress"*).

    🔴 **정본 축은 `(sim_run_id, financing_mode)` 다.**

      ⚠️ **이 Protocol 이 그 둘을 안 받는다.** *"어느 실행의 장부인가"* 는 실행
        정체성이라 **어댑터 생성 인자**로 온다 — `LogisticsInboundExecution` ·
        `LogisticsTransitionAdapter` 와 같은 자리이고, 배선(`app/main.py`)에서 눈에
        보이게 주입한다. 호출마다 나르면 마스터가 매번 그 값을 정하는 셈이 된다.

    :param as_of: 수금일. **달력일**이다 (토·일·공휴일 포함).
    :returns: 무엇이 수금됐는지. 들어올 것이 없으면 `NOTHING_DUE` 이고 그것은 정상이다.
    """

    def collect(self, conn: Any, *, as_of: date) -> CollectionPartOut: ...


class CollectionPartOut(BaseModel):
    """한 파트의 수금 실행 결과.

    ★ **`NOTHING_DUE` 를 `COLLECTED` 로 접지 않는다.** *"들어올 것이 없었다"* 와
      *"들어왔다"* 는 다른 사실이고, 뭉치면 **수금 사건이 안 잡히는 버그**가 매일
      성공으로 보인다.
    """

    part: str
    status: Literal["COLLECTED", "NOTHING_DUE", "BLOCKED"]
    reason: str = ""
    #: **이번 호출에서 수금 사건을 반영한 채권.** `receivable_id` 들이다.
    #:
    #: ⚠️ **금액을 여기 싣지 않는다.** 얼마가 들어왔는지의 권위 사실은 재무 원장
    #: (`receivables` · `finance_states`)이 갖는다 — 여기에 복사해 두면 같은 사실의
    #: 주인이 둘이 되고, 롤백된 날 이 목록만 살아남는다.
    #:
    #: ★ 빈 목록이면 **이 호출에서 반영한 채권이 없다** — 이미 다 수금됐거나 들어올
    #: 것이 없었다. 어느 쪽인지는 `status` 가 말한다.
    collected: list[str] = Field(default_factory=list)


class CollectionOut(BaseModel):
    """수금 실행 1회의 결과. **`InboundOut` 과 같은 다섯 갈래다.**

    ```text
    COLLECTED    한 파트라도 실제로 수금했다
    NOTHING_DUE  **수금할 대상이 없었다** — 오늘 수금 사건이 없었거나 미등록이다
    BLOCKED      **수금할 대상은 있는데 반영할 수 없다** — 축 불일치 · 깨진 원장
    NOT_OPENED   **그날 장부가 안 열렸다** — 수금할 것이 있는지조차 묻지 않았다
    FAILED       반영하려다 실패했다 — **아무것도 안 바뀌었다**
    ```

    🔴 **`NOT_OPENED` 를 `BLOCKED` 로 접지 않는다.**

      ```text
      BLOCKED      수금할 대상이 있는데 **그 건이** 반영 불가 — receivable_id 가 나온다
      NOT_OPENED   **아직 아무것도 안 봤다** — 장부가 없어 물어보지도 못했다
      ```

      ⚠️ 접으면 *"채권에 문제가 있다"* 와 *"어제 개장을 안 돌렸다"* 가 같은 문장으로
        나간다. **고칠 곳이 완전히 다른데** 화면은 같아 보인다.

      ★ **다음에 할 일도 다르다.** `BLOCKED` 는 그 채권을 봐야 하고, `NOT_OPENED` 는
        `open_day` 를 부르면 된다 — 그래서 `next_action` 을 같이 싣는다.

    🔴 **`BLOCKED` 를 `NOTHING_DUE` 로 접지 않는다** (`inbound.py` 의 그 규칙 그대로 ·
       물류 지적 2026-09-06 에서 배운 것이다).

      ```text
      NOTHING_DUE   실제로 수금할 대상이 없음
      BLOCKED       수금할 대상은 존재하지만 반영할 수 없음
      ```

      ⚠️ 접으면 **막힌 수금을 뒤의 orchestration 이 정상으로 오해한다.** 축 불일치나
        깨진 원장으로 막힌 날이 *"오늘은 들어올 게 없었다"* 로 보이고, **들어왔어야 할
        현금이 장부에 없는 채로** 매입 판단이 돈다.

      ★ **파트가 `BLOCKED` 면 전체도 `BLOCKED` 다.** 한 파트라도 수금했더라도 그렇다 —
        *"들어올 게 있었는데 못 받았다"* 가 *"받았다"* 보다 먼저 알려야 하는 사실이다
        (`day_open` 이 `REJECTED_GAP` 을 먼저 보는 것과 같은 판단).
    """

    as_of: date
    status: Literal["COLLECTED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED"]
    reason: str = ""
    parts: list[CollectionPartOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: 🔴 **키가 항상 있고 막히지 않았으면 `None` 이다.** `DayGate.next_action` 과 같은
    #: 모양이다 — 칸을 없애면 화면이 `'next_action' in resp` 를 먼저 물어야 한다.
    #: `NOT_OPENED` 일 때 개장 Gate 가 준 값을 **해석하지 않고 그대로** 옮긴다.
    next_action: str | None = None


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# 🔴 **다섯 번째 등록소다.** 전이(승인이 장부를 바꾸는 방법) · 하루 넘김(하루가
#    넘어가는 방법) · 취소(승인을 물리는 방법) · 입고(도착분을 받는 방법) ·
#    여기(수금 사건을 반영하는 방법). 한 사전에 섞으면 *"입고는 되는데 수금은 안 되는"*
#    상태를 표현할 수 없고, **지금이 정확히 그 상태다.**

_COLLECTIONS: dict[CollectionPart, Any] = {}


def register_collection(part: CollectionPart, impl: Any) -> None:
    """수금 실행 구현을 등록한다. 재무 모듈이 임포트 시점에 부른다."""
    if part not in PARTS:
        raise ValueError(f"수금 실행 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _COLLECTIONS[part] = impl


def registered() -> Mapping[CollectionPart, Any]:
    """지금 등록된 수금 실행. **읽기용 사본**이다."""
    return dict(_COLLECTIONS)


def missing() -> tuple[str, ...]:
    """아직 수금 실행 구현이 없는 파트. **`PARTS` 순서를 지킨다.**"""
    return tuple(part for part in PARTS if part not in _COLLECTIONS)


def reset() -> None:
    """등록을 비운다. 검사용이다."""
    _COLLECTIONS.clear()


# ── 경계 ────────────────────────────────────────────────────────────────


def collect_receipts(
    as_of: date, *, connect: Any = None, sim_run_id: str = BURN_IN_SIM_RUN_ID
) -> CollectionOut:
    """`as_of` 의 수금 사건을 **한 트랜잭션으로** 반영한다.

    ★ **`open_day` 다음이다.** 상태 행이 있어야 수금을 적을 자리가 있다. 다만 **함수는
      따로다** — 묶으면 실패 원인이 뭉개진다.

    ★ **예외를 밖으로 내지 않는다.** `receive_arrivals` · `apply_approval` 과 같다 —
      수금 실패가 판단을 멈추면 그날 하루가 통째로 서고, 그건 수금 하나보다 크다.

    🔴 **예외 전파 여부와 후속 진행 여부는 다른 물음이다.**

      ```text
      예외를 안 올린다        🟢 이 함수의 계약
      그러니 판단을 계속한다   🔴 **그런 뜻이 아니다**
      ```

      ⚠️ **수금이 `FAILED` · `BLOCKED` 인데 매입 판단을 계속하면 가용 현금이 실제보다
        적게 반영된 상태로 판단한다.** 들어왔어야 할 돈이 장부에 없는 채로 *"현금이
        없으니 사지 말자"* 가 나온다.

      ★ **부르는 쪽이 정한다.** 이 함수는 상태를 값으로 돌려주고, `run_procurement`
        진행 여부는 그것을 본 orchestration 의 결정이다.

    ⚠️ **달력일이다.** 입금은 토요일에도 찍힌다. 실행일 달력을 쓰지 않는다.

    ---

    🔴 **개장 Gate 를 먼저 본다** (`receive_arrivals` 와 같은 이유).

      순서를 문장으로만 적어 두면 그것을 지키는 책임이 부르는 쪽에 통째로 있고, 안
      지킨 날 `NOTHING_DUE` 가 나가 *"오늘은 들어올 게 없었다"* 로 읽힌다.

      ```text
      Gate BLOCKED   →  NOT_OPENED     수금할 것이 있는지조차 안 묻는다
      Gate PASS      →  평소대로
      ```

      ★ **`check_day_gate` 는 열지 않는다. 묻기만 한다.** 여기서 `open_day` 를 부르면
        수금이 개장의 부작용이 되고, `router.py` 가 개장에 대해 적어 둔 *"명시적
        호출이다. 실행의 부작용이 아니다"* 를 수금이 어긴다.

      ⚠️ **미등록은 PASS 다** (`day_gate` 계약). 정본 표가 없는 환경에서 이 Gate 가
        수금을 막지 않는다 — 없는 것과 안 열린 것은 다르다.

    :param sim_run_id: 어느 실행의 장부인가 (`#531` 후속). 🔴 **여기는 기본값이
                    있다** — 라우터가 이 칸을 안 주고 이번 판은 운영 동작을 안
                    바꾼다. 걷기는 `run_scheduled_day` 가 자기 축을 실어 준다.
    """
    gate = check_day_gate(as_of, connect=connect)
    if gate.gate == "BLOCKED":
        return CollectionOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=gate.reason,
            # ★ **해석하지 않고 옮긴다.** 무엇을 해야 하는지는 개장이 아는 사실이고,
            #   수금이 다시 판정하면 같은 사실의 주인이 둘이 된다.
            next_action=gate.next_action,
        )

    absent = missing()
    if absent:
        # ★ **미등록은 오류가 아니다.** 그 파트가 아직 수금을 실행하지 않는다는 뜻이고,
        #   *"오늘 들어올 것이 없다"* 와 다른 사실이다.
        return CollectionOut(
            as_of=as_of,
            status="NOTHING_DUE",
            reason=f"수금 실행 미등록: {', '.join(absent)}",
            missing=list(absent),
        )

    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        # 🔴 **등록소가 든 축이 아니라 이번 호출의 축으로 묶는다** (`#531` 후속).
        #    `FinanceCollectionAdapter` 는 그 축을 재무 축과 대조해 fail-closed 한다 —
        #    등록소가 프로세스 시작 때 든 상수로 쓰면 **매입 원장만 새 실행에
        #    앉고 이쪽은 번인에 남는다.**
        #
        # ★ **`try` 안이다.** 축이 비면 `bind_sim_run` 이 막는데, 그 실패도 예외로
        #   올라가지 않고 아래 `except` 가 `FAILED` + 사유로 옮긴다 — 수금이
        #   그날을 통째로 세우면 안 된다는 이 함수의 계약 그대로다.
        adapters = {
            part: bind_sim_run(impl, sim_run_id) for part, impl in registered().items()
        }
        results = [adapters[part].collect(conn, as_of=as_of) for part in PARTS]
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 수금 실패가 그날을 통째로 세우면 안 된다.
        conn.rollback()
        return CollectionOut(as_of=as_of, status="FAILED", reason=f"수금 실행 실패: {exc}")
    finally:
        conn.close()

    return _aggregate(as_of, results)


def _aggregate(as_of: date, parts: list[CollectionPartOut]) -> CollectionOut:
    """파트 결과를 전체 어휘로 취합한다.

    ```text
    BLOCKED 가 하나라도    → BLOCKED     들어올 게 있었는데 못 받았다
    COLLECTED 가 하나라도  → COLLECTED
    전부 NOTHING_DUE       → NOTHING_DUE
    ```

    🔴 **순서가 계약이다.** `BLOCKED` 를 먼저 보는 이유는 그것이 **사람이 봐야 하는
       사실**이기 때문이다 — `COLLECTED` 뒤로 밀면 *"오늘 받았다"* 로 지나간다.
    """
    blocked = [part for part in parts if part.status == "BLOCKED"]
    if blocked:
        return CollectionOut(
            as_of=as_of,
            status="BLOCKED",
            reason=f"수금할 것이 있는데 막혔다: {', '.join(part.part for part in blocked)}",
            parts=parts,
        )
    if any(part.status == "COLLECTED" for part in parts):
        return CollectionOut(as_of=as_of, status="COLLECTED", parts=parts)
    return CollectionOut(as_of=as_of, status="NOTHING_DUE", parts=parts)
