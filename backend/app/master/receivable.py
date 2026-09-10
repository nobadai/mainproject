"""
receivable.py — **판매 확정이 채권을 만드는 자리.** 여섯째 등록소.

🔴 **부르는 곳이 0건이다** (실측 2026-09-09).

```text
app/finance/receivables.py :46   confirm_receivable(conn, ReceivableCreateInput(...))
그런데 이것을 부르는 production 호출자가 **0건**이다 — 검사 파일이 전부다

haetdeul.sales   2026-01-05 · 01-07 · 01-09  CONFIRMED→DELIVERED
haetdeul.receivables 에 그 셋의 채권       **0행**   ← 배선이 없어서
```

★★ **재무 경계는 이미 서 있다.** `receivables` 원장에 멱등으로 넣는 것도,
  `finance_states.receivables_krw` 를 같이 올리는 것도, 같은 `sale_id` 로 다른 사실이
  들어오면 막는 것도 `app/finance/receivables.py` 안에 다 있다. **없는 것은 그것을
  판매 확정 뒤에 부르는 자리**이고, 이 등록소가 그 자리를 만든다.

⚠️ `register_inbound` 호출이 0건이던 것(`#337`) · `register_collection` 이 0건이던
  것(`#427`)과 **같은 모양**이다 — 구현이 다 섰는데 배선이 없어 매일 조용히 아무 일도
  안 일어났다.

---

★ **판매·재무 결정 2026-09-09.**

```text
판매 확정 → receivables_krw += 원금   (issued_date = sale_date)
수금      → current_cash_krw += · receivables_krw −=

"Sales 에서 Finance 를 직접 호출하지 않는다.
 Master 가 확정 트랜잭션을 잇는 위치에서 Finance boundary 를 호출하는 것이 맞다."
```

```text
Sales     확정된 판매의 정본 — sale_date · collection_due_date · total_amount_krw
Finance   채권 원장의 정본 — receivable_id · issued_date · (sim_run_id, financing_mode) 축
Master    그날 확정분을 **운반하고 호출**한다.
          채권 금액도 기일도 **스스로 계산하지 않는다**
```

🔴 **`due_date` 를 마스터가 계산하지 않는다.**

  ⚠️ *"sale_date + payment_days"* 는 판매·재무의 계약값이다. 마스터가 그 식을 다시
    쓰면 결제조건이 바뀌는 날 **두 곳이 다른 답을 내고**, 그 사고는 에러 없이
    기일만 바꾼다. `sales.collection_due_date` 를 읽고, **그 칸이 비어 있으면
    지어내지 않고 막는다.**

---

★ **하루의 걸음이 각자 멱등하고 각자 실패한다.**

```text
① open_day(as_of)           상태 행을 보장한다        ← 먼저 (적을 자리가 있어야 한다)
② receive_arrivals(as_of)   도착분을 실제로 받는다
③ issue_receivables(as_of)  판매 확정분을 채권으로 세운다  ← 여기
④ collect_receipts(as_of)   수금 사건을 반영한다
⑤ run_procurement(as_of)    그 위에서 판단한다
```

🔴 **수금보다 앞이다.** 채권이 서야 수금할 것이 있다. 지금 데이터는 결제조건이
  30일이라 같은 날 수금될 일이 없지만, **순서가 계약**이다.

  ⚠️ **`run_procurement` 안에 넣지 않는다.** 넣으면 판단 한 번이 채권 잔액을 움직이고
    *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다 — 개장·입고·수금을 판단 밖에 둔
    이유와 같다.

🔴 **날마다다. 실행일이 아니다.** `sales.sale_date` 는 판매가 정한 날이고, 마스터가
  실행일 달력으로 그것을 밀면 **토요일 판매의 채권이 월요일 장부에 선다**
  (`collection.py` · `inbound.py` 와 같은 결).
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
    "ReceivableOut",
    "ReceivablePart",
    "ReceivablePartOut",
    "ReceivableSource",
    "issue_receivables",
    "missing",
    "register_receivable",
    "registered",
    "reset",
]

ReceivablePart = Literal["finance"]

#: 채권을 세우는 파트.
#:
#: ★ **재무 하나다.** 판매는 확정 사실의 원천이지 채권 원장을 갖지 않고, 물류·매입은
#:   채권에 손대지 않는다.
#:
#: ⚠️ **하나짜리 등록소가 과한 것이 아니다.** 이것이 있어야 *"구현이 없다"* 와
#:    *"오늘 확정된 판매가 없다"* 를 가를 수 있다. 둘은 다른 사실이고, 뭉치면 재무
#:    구현체가 빠진 날 **조용히 아무 일도 안 일어난다.**
PARTS: tuple[ReceivablePart, ...] = ("finance",)


class ReceivableSource(Protocol):
    """그날 확정된 판매를 채권으로 세우는 방법. **재무가 소유한다.**

    🔴 **마스터는 `as_of` 만 준다.** 어느 판매가 대상이고 얼마짜리 채권이 되는지는
      판매·재무 사실이다 — `CollectionSource` · `InboundExecution` 과 같은 이유다.

    ★ **`conn` 은 받기만 한다.** commit·rollback·close 를 하지 않는다 — 트랜잭션
      경계는 마스터가 쥔다.

    🔴 **멱등이어야 한다.** 같은 날을 두 번 걸어도 채권이 하나여야 한다.
      `confirm_receivable` 이 `ON CONFLICT (sale_id) DO NOTHING` 으로 그렇게 적어
      뒀지만, **믿는 것과 검사로 박는 것은 다르다** — `tests/master/test_receivable.py`
      가 두 번 불러 잰다.

    🔴 **정본 축은 `(sim_run_id, financing_mode)` 다.**

      ⚠️ **이 Protocol 이 그 둘을 안 받는다.** *"어느 실행의 장부인가"* 는 실행
        정체성이라 **어댑터 생성 인자**로 온다 — `FinanceCollectionAdapter` ·
        `LogisticsInboundExecution` 과 같은 자리이고, 배선(`app/master/bootstrap.py`)
        에서 눈에 보이게 주입한다.

    :param as_of: 판매 확정일. **`sales.sale_date` 와 맞춘다** — 달력일이다.
    :returns: 무엇이 채권으로 섰는지. 그날 확정 판매가 없으면 `NOTHING_DUE` 이고
        그것은 정상이다.
    """

    def issue(self, conn: Any, *, as_of: date) -> ReceivablePartOut: ...


class ReceivablePartOut(BaseModel):
    """한 파트의 채권 발행 결과.

    ★ **`NOTHING_DUE` 를 `ISSUED` 로 접지 않는다.** *"그날 확정 판매가 없었다"* 와
      *"채권이 섰다"* 는 다른 사실이고, 뭉치면 **판매가 확정됐는데 채권이 안 서는
      버그**가 매일 성공으로 보인다.
    """

    part: str
    status: Literal["ISSUED", "NOTHING_DUE", "BLOCKED"]
    reason: str = ""
    #: **그날 대상 판매에 대해 서 있는 채권.** `receivable_id` 들이다.
    #:
    #: ⚠️ **금액을 여기 싣지 않는다.** 얼마짜리 채권인지의 권위 사실은 재무 원장
    #: (`receivables` · `finance_states`)이 갖는다 — 여기에 복사해 두면 같은 사실의
    #: 주인이 둘이 되고, 롤백된 날 이 목록만 살아남는다.
    #:
    #: ★ **이번 호출에서 새로 만든 것만이 아니다.** 멱등이라 이미 있으면 안 만들고,
    #: 그래도 채권은 서 있다. 새로 만든 건수는 `created` 가 따로 나른다.
    issued: list[str] = Field(default_factory=list)
    #: 🔴 **이번 호출에서 실제로 새로 만든 건수.**
    #:
    #: ⚠️ **`issued` 와 한 값으로 접으면 안 된다.** 접으면 *"이미 있어서 안 만들었다"*
    #: 와 *"대상이 없었다"* 가 같아 보이고, 멱등 재실행이 매일 *"아무것도 안 했다"* 로
    #: 읽힌다. `ISSUED` 인데 `created == 0` 인 것이 **정상 상태**다.
    created: int = 0


class ReceivableOut(BaseModel):
    """채권 발행 1회의 결과. **`CollectionOut` 과 같은 다섯 갈래다.**

    ```text
    ISSUED       그날 대상이 있었고 채권이 서 있다 (새로 만든 건수는 `created` 가 나른다)
    NOTHING_DUE  **그날 sale_date 인 확정 판매가 없다** — 또는 미등록이다
    BLOCKED      **대상은 있는데 권위 있는 입력이 없어 못 만든다** — 축 불일치 · 기일 없음
    NOT_OPENED   **그날 장부가 안 열렸다** — 세울 것이 있는지조차 묻지 않았다
    FAILED       세워 보다 터졌다 — **아무것도 안 바뀌었다**
    ```

    🔴 **`NOTHING_DUE` 를 `BLOCKED` 로 접지 않는다** (`inbound.py` 의 그 규칙 그대로 ·
       물류 지적 2026-09-06 에서 배운 것이다).

      ```text
      NOTHING_DUE   실제로 그날 확정된 판매가 없음
      BLOCKED       확정 판매는 존재하지만 채권을 못 세움
      ```

      ⚠️ 접으면 **막힌 발행을 뒤의 orchestration 이 정상으로 오해한다.** 축 불일치나
        `collection_due_date` 결측으로 막힌 날이 *"오늘은 판 게 없었다"* 로 보이고,
        **서 있어야 할 채권이 장부에 없는 채로** 매입 판단이 돈다 —
        `receivables_krw` 는 매입 cap 이 보는 값이다.

    🔴 **`NOT_OPENED` 를 `BLOCKED` 로 접지 않는다.**

      ```text
      BLOCKED      세울 대상이 있는데 **그 건이** 발행 불가 — sale_id 가 나온다
      NOT_OPENED   **아직 아무것도 안 봤다** — 장부가 없어 물어보지도 못했다
      ```

      ⚠️ 접으면 *"판매 데이터에 문제가 있다"* 와 *"어제 개장을 안 돌렸다"* 가 같은
        문장으로 나간다. **고칠 곳이 완전히 다른데** 화면은 같아 보인다.

      ★ **다음에 할 일도 다르다.** `BLOCKED` 는 그 판매를 봐야 하고, `NOT_OPENED` 는
        `open_day` 를 부르면 된다 — 그래서 `next_action` 을 같이 싣는다.

    ★ **파트가 `BLOCKED` 면 전체도 `BLOCKED` 다.** 한 건이라도 섰더라도 그렇다 —
      *"세울 게 있었는데 못 세웠다"* 가 *"세웠다"* 보다 먼저 알려야 하는 사실이다.
    """

    as_of: date
    status: Literal["ISSUED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED"]
    reason: str = ""
    parts: list[ReceivablePartOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: 🔴 **키가 항상 있고 막히지 않았으면 `None` 이다.** `CollectionOut.next_action`
    #: 과 같은 모양이다. `NOT_OPENED` 일 때 개장 Gate 가 준 값을 **해석하지 않고
    #: 그대로** 옮긴다.
    next_action: str | None = None


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# 🔴 **여섯 번째 등록소다.** 전이(승인이 장부를 바꾸는 방법) · 하루 넘김(하루가
#    넘어가는 방법) · 취소(승인을 물리는 방법) · 입고(도착분을 받는 방법) ·
#    수금(돈이 들어오는 방법) · 여기(판매 확정이 채권을 만드는 방법).
#
# ⚠️ **앞의 다섯 중 아무 데도 합치지 않는다.** 합치면 *"수금은 되는데 채권은 안 서는"*
#    상태를 표현할 수 없고, **지금이 정확히 그 상태다.**

_RECEIVABLES: dict[ReceivablePart, Any] = {}


def register_receivable(part: ReceivablePart, impl: Any) -> None:
    """채권 발행 구현을 등록한다. 배선(`bootstrap.py`)이 부른다."""
    if part not in PARTS:
        raise ValueError(f"채권 발행 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _RECEIVABLES[part] = impl


def registered() -> Mapping[ReceivablePart, Any]:
    """지금 등록된 채권 발행. **읽기용 사본**이다."""
    return dict(_RECEIVABLES)


def missing() -> tuple[str, ...]:
    """아직 채권 발행 구현이 없는 파트. **`PARTS` 순서를 지킨다.**"""
    return tuple(part for part in PARTS if part not in _RECEIVABLES)


def reset() -> None:
    """등록을 비운다. 검사용이다."""
    _RECEIVABLES.clear()


# ── 경계 ────────────────────────────────────────────────────────────────


def issue_receivables(
    as_of: date, *, connect: Any = None, sim_run_id: str = BURN_IN_SIM_RUN_ID
) -> ReceivableOut:
    """`as_of` 에 확정된 판매를 **한 트랜잭션으로** 채권으로 세운다.

    ★ **`open_day` 다음이고 `collect_receipts` 앞이다.** 상태 행이 있어야 채권을 적을
      자리가 있고, 채권이 서야 수금할 것이 있다. 다만 **함수는 따로다** — 묶으면
      실패 원인이 뭉개진다.

    ★ **예외를 밖으로 내지 않는다.** `receive_arrivals` · `collect_receipts` 와 같다 —
      발행 실패가 판단을 멈추면 그날 하루가 통째로 서고, 그건 채권 하나보다 크다.

    🔴 **예외 전파 여부와 후속 진행 여부는 다른 물음이다.**

      ```text
      예외를 안 올린다        🟢 이 함수의 계약
      그러니 판단을 계속한다   🔴 **그런 뜻이 아니다**
      ```

      ⚠️ **채권이 안 서면 `receivables_krw` 가 적게 잡히고, 그것은 매입 cap 이 보는
        값이다.** 그래서 `scheduler._ledger_gap` 이 이 값도 본다 — 진행 여부를 정하는
        것은 부르는 쪽이고, 이 함수는 상태를 값으로 돌려줄 뿐이다.

    ⚠️ **달력일이다.** `sales.sale_date` 가 정본이고 실행일 달력으로 밀지 않는다.

    ---

    🔴 **개장 Gate 를 먼저 본다** (`collect_receipts` 와 같은 이유).

      ```text
      Gate BLOCKED   →  NOT_OPENED     세울 것이 있는지조차 안 묻는다
      Gate PASS      →  평소대로
      ```

      ★ **`check_day_gate` 는 열지 않는다. 묻기만 한다.** 여기서 `open_day` 를 부르면
        발행이 개장의 부작용이 된다.

      ⚠️ **미등록은 PASS 다** (`day_gate` 계약).

    :param sim_run_id: 어느 실행의 장부인가 (`#531` 후속). 🔴 **여기는 기본값이
                    있다** — 라우터가 이 칸을 안 주고 이번 판은 운영 동작을 안
                    바꾼다. 걷기는 `run_scheduled_day` 가 자기 축을 실어 준다.
    """
    gate = check_day_gate(as_of, connect=connect)
    if gate.gate == "BLOCKED":
        return ReceivableOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=gate.reason,
            # ★ **해석하지 않고 옮긴다.** 무엇을 해야 하는지는 개장이 아는 사실이다.
            next_action=gate.next_action,
        )

    absent = missing()
    if absent:
        # ★ **미등록은 오류가 아니다.** 그 파트가 아직 채권을 세우지 않는다는 뜻이고,
        #   *"오늘 확정된 판매가 없다"* 와 다른 사실이다 — `missing` 이 그것을 가른다.
        return ReceivableOut(
            as_of=as_of,
            status="NOTHING_DUE",
            reason=f"채권 발행 미등록: {', '.join(absent)}",
            missing=list(absent),
        )

    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        # 🔴 **등록소가 든 축이 아니라 이번 호출의 축으로 묶는다** (`#531` 후속).
        #    `FinanceReceivableAdapter` 는 그 축을 재무 축과 대조해 fail-closed 한다 —
        #    등록소가 프로세스 시작 때 든 상수로 쓰면 **매입 원장만 새 실행에
        #    앉고 이쪽은 번인에 남는다.**
        #
        # ★ **`try` 안이다.** 축이 비면 `bind_sim_run` 이 막는데, 그 실패도 예외로
        #   올라가지 않고 아래 `except` 가 `FAILED` + 사유로 옮긴다 — 채권이
        #   그날을 통째로 세우면 안 된다는 이 함수의 계약 그대로다.
        adapters = {
            part: bind_sim_run(impl, sim_run_id) for part, impl in registered().items()
        }
        results = [adapters[part].issue(conn, as_of=as_of) for part in PARTS]
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 발행 실패가 그날을 통째로 세우면 안 된다.
        conn.rollback()
        return ReceivableOut(as_of=as_of, status="FAILED", reason=f"채권 발행 실패: {exc}")
    finally:
        conn.close()

    return _aggregate(as_of, results)


def _aggregate(as_of: date, parts: list[ReceivablePartOut]) -> ReceivableOut:
    """파트 결과를 전체 어휘로 취합한다.

    ```text
    BLOCKED 가 하나라도  → BLOCKED     세울 게 있었는데 못 세웠다
    ISSUED 가 하나라도   → ISSUED
    전부 NOTHING_DUE     → NOTHING_DUE
    ```

    🔴 **순서가 계약이다.** `BLOCKED` 를 먼저 보는 이유는 그것이 **사람이 봐야 하는
       사실**이기 때문이다 — `ISSUED` 뒤로 밀면 *"오늘 세웠다"* 로 지나간다.
    """
    blocked = [part for part in parts if part.status == "BLOCKED"]
    if blocked:
        return ReceivableOut(
            as_of=as_of,
            status="BLOCKED",
            reason=f"채권을 세울 것이 있는데 막혔다: {', '.join(part.part for part in blocked)}",
            parts=parts,
        )
    if any(part.status == "ISSUED" for part in parts):
        return ReceivableOut(as_of=as_of, status="ISSUED", parts=parts)
    return ReceivableOut(as_of=as_of, status="NOTHING_DUE", parts=parts)
