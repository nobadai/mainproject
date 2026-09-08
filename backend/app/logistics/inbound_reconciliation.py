"""inbound_reconciliation.py — **사람이 확인한 고착 입고 일정을 명시적으로 걷는다.**

```text
운영자가 확인한 orphan 일정
   → 도착 전역 잠금 → 입고 계보 확인 → fixture 행 FOR UPDATE
   → in_transit · confirmed_inbound 에서 그 inbound_id 를 **함께** 제거
```

🔴 **이 파일은 도착 로직의 버그를 고치는 것이 아니다.** 발주 참조가 없는 일정이
   `ARRIVAL_PURCHASE_REFERENCE_MISSING` 으로 blocked 에 남는 것은 **정상 동작**이다
   (`arrival.py:288-291` — *"물류가 값을 지어내 풀 일이 아니다"*). 여기 있는 것은
   그 판정을 우회하는 길이 아니라, **사람이 따로 확인한 뒤 명시적으로 거두는 길**이다.

---

## 안 하는 것 다섯 — 이 파일의 존재 이유가 여기 있다

```text
① ETA + N일 지났다고 자동 삭제        오래된 것과 잘못된 것은 다른 사실이다
② inbound_id 를 파싱해 purchase_id 조립  발주 ID 의 주인은 물류가 아니다
③ 수량·날짜·품목이 같으면 같은 입고    실데이터에 3,587kg Receipt 가 여럿 있고
                                       inbound_id 는 서로 다르다
④ 한쪽 목록만 삭제                    B-1 위반을 덮는 것이 된다
⑤ Receipt 가 있는데 일정만 삭제        그것은 orphan 이 아니라 무결성 문제다
```

★ 다섯 모두 **자동 판단을 하지 않는다**는 한 줄이다. 이 함수는 *"무엇을 걷을지"* 를
  스스로 고르지 않는다 — `inbound_id` 를 사람이 지목하고, `reason` · `source_ref` 로
  근거를 함께 낸다. 근거 없는 복구는 근거 없는 삭제와 같은 것이다.

---

## 정상 입고 경로를 대체하지 않는다

```text
검수 끝난 입고   inbound_stock.materialize_inspected_inbound   Lot·IN·Receipt·일정 정리
고착 orphan     여기                                          일정만, 사람이 지목해서
```

🔴 **`Receipt` 가 하나라도 있으면 이 함수는 거부한다.** 그 건은 도착 처리가 이미
   시작됐다는 뜻이라 `materialize_inspected_inbound` 의 몫이거나, 아니면 별도
   무결성 문제다 — 어느 쪽이든 일정만 걷어서 될 일이 아니다.

---

## 규율은 새로 만들지 않고 가져다 쓴다

```text
B-1 대조 · 양쪽 제거 · 중복 거부 · CONFIRMED/CONFIRMED_ZERO · 멱등
    → inbound_stock._clear_schedule 을 **그대로** 부른다
도착 전역 advisory 잠금
    → receipts.lock_arrival_writes
usage_scope
    → transition.USAGE_SCOPE
```

🔴 **비교 규칙을 여기에 다시 적지 않는다.** 두 곳이 각자 B-1 을 적으면 한쪽만
   고쳐지는 날이 오고, 그날 일정 두 칸이 조용히 갈린다 (`console_service` 가
   `outbound` 의 상태 어휘를 문자열로 다시 적지 않는 것과 같은 이유다).

⚠️ **`cancellation.py` 를 import 하지 않는다.** 그쪽은 마스터가 임시로 얹은 모듈이라
   `app.master.commitment` · `app.finance.db` 를 끌고 온다. 여기서 부르면 그 의존성이
   물류 코어로 옮겨 붙는다 — 규율은 참고하되 코드는 안 가져온다.

---

## 잠금 순서 — `materialize_inspected_inbound` 과 **같은 순서**

```text
① 도착 전역 advisory (20260905, 2)   receipts.lock_arrival_writes
② 입고 계보 조회 (SELECT)            ①이 잡혀 있어 그 사이 Receipt 가 못 생긴다
③ fixture 행 FOR UPDATE              _clear_schedule 안에서
④ 일정 UPDATE                        〃 같은 잠금 아래
⑤ 커밋은 호출자가 한 번               🔴 이 파일은 commit 도 rollback 도 안 한다
```

⚠️ **②를 ① 앞에 두면 안 된다.** 그러면 계보를 읽은 뒤 걷기 전에 도착 처리가 끼어들어
   *"Receipt 가 없다"* 로 읽은 사실이 걷는 순간 거짓이 된다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema

# 🔴 **B-1 규율을 다시 적지 않고 가져다 쓴다.** 밑줄 이름을 건너 가져오는 것은 이
#    패키지의 기존 방식이다 (`console_service` 가 `outbound._ASSIGNED_ALLOCATION` 을
#    그대로 쓰는 것과 같다 — *"여기에 문자열로 다시 적지 않는다"*).
from app.logistics.inbound_stock import ScheduleIntegrityError, _clear_schedule
from app.logistics.receipts import lock_arrival_writes
from app.logistics.transition import USAGE_SCOPE

__all__ = [
    "InboundReconciliationError",
    "InboundReconciliationResult",
    "InvalidReconciliationRequest",
    "MaterializedInbound",
    "ScheduleAlreadyMaterialized",
    # 🔴 **다시 정의하지 않고 그대로 내보낸다.** 호출자가 `except` 로 잡을 때 두 이름 중
    #    어느 것인지 헷갈리면 한쪽만 잡는 날이 온다 — 이름은 하나여야 한다.
    "ScheduleIntegrityError",
    "reconcile_orphan_inbound_schedule",
]


#: 🔴 원장 어휘 그대로다 (`inventory_moves.move_type` CHECK). 새 낱말을 만들지 않는다.
_IN_MOVE_TYPE = "IN"

#: 계보 조회에서 **읽기만 하는** 칸들. 이 함수는 이 표들에 한 줄도 안 쓴다.
_LINEAGE_COLUMNS = ("receipt_id", "receipt_status", "lot_id", "move_id")


class InboundReconciliationError(RuntimeError):
    """이 모듈이 내는 오류의 뿌리."""


class InvalidReconciliationRequest(InboundReconciliationError, ValueError):
    """요청 자체가 성립하지 않는다. **DB 에 묻기 전에 막는다.**

    🔴 **빈 축으로 물으면 0건이 돌아오고, 그 0건이 *"이미 걷혔다"* 로 읽힌다.**
       없는 것과 물어보지 못한 것은 다른 사실이다
       (`inbound_stock.InvalidReceivingAxis` 와 같은 판단).

    🔴 **`reason` · `source_ref` 도 같은 등급으로 막는다.** 근거 없이 일정을 걷는 것은
       근거 없이 지우는 것과 같다 — 이 함수는 사람의 판단을 실행하는 자리이지 스스로
       판단하는 자리가 아니라서, 그 판단이 무엇이었는지를 빈칸으로 둘 수 없다.
    """


class ScheduleAlreadyMaterialized(InboundReconciliationError, ValueError):
    """그 `inbound_id` 에 **이미 입고 계보가 붙어 있다.** orphan 이 아니다.

    ```text
    Receipt 있음                도착 처리가 시작됐다 — materialize 의 몫이다
    Receipt + Lot 있음          가용재고가 됐다
    Receipt + Lot + IN Move 있음  원장까지 나갔다
    ```

    ⚠️ **셋 중 어느 것이든 일정만 걷으면 안 된다.** 일정이 사라지면 그 재고가 어디서
       왔는지 되짚을 자리가 없어지고, 남은 것은 출처 없는 Lot 이다.
    """


@dataclass(frozen=True)
class MaterializedInbound:
    """거부 근거로 함께 싣는 **계보 한 줄.** 값이고 아무것도 안 쓴다."""

    receipt_id: str
    receipt_status: str
    #: 그 Receipt 에서 나온 Lot. 아직 없으면 `None` (도착만 하고 재고화 전).
    lot_id: str | None
    #: 그 Lot 의 원장 `IN`. 아직 없으면 `None`.
    move_id: str | None


@dataclass(frozen=True)
class InboundReconciliationResult:
    """정리 1회의 결과. **터진 것은 예외로 나가고, 여기 오는 것은 다 정상이다.**

    ```text
    applied=True  · removed=1   두 칸에서 함께 걷었다
    applied=False · removed=0   이미 걷혀 있었다 — 재실행의 정상 경로다
    ```

    🔴 **`removed` 는 걷어낸 *일정 건수* 이지 목록 항목 수가 아니다.** 두 칸에서 한
       건씩 함께 빠지므로 0 아니면 1 이다 — 중복은 `_clear_schedule` 이 무결성
       오류로 막는다.

    ⚠️ **`reason` · `source_ref` 는 되돌려만 주고 DB 에 안 적는다.** 지금 fixture 행에
       이 사실을 담을 칸이 없고(`note` 는 하루 넘김이 쓰는 남의 칸이다), 칸을 만드는
       것은 `database/` 의 일이다. 호출자가 자기 감사 기록에 남긴다.
    """

    applied: bool
    inbound_id: str
    sim_run_id: str
    as_of: date
    usage_scope: str
    removed: int
    #: 사람이 적은 정리 사유. 빈 값은 애초에 못 들어온다.
    reason: str
    #: 그 판단의 근거 참조 (티켓 · 감사 문서 등). 역시 빈 값을 안 받는다.
    source_ref: str


def _require_text(value: Any, *, 칸: str) -> str:
    """비었거나 공백뿐이면 **DB 에 묻기 전에** 멈춘다."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidReconciliationRequest(
            f"입고 일정 정리에 쓸 수 없는 {칸} 다: {value!r}."
            " 빈 축으로 물으면 0건이 돌아오고 그 0건은 '이미 걷혔다' 로 읽힌다 —"
            " 없는 것과 물어보지 못한 것은 다른 사실이다."
        )
    return value


def _materialized_lineage(
    conn: Any, schema: sql.Identifier, *, sim_run_id: str, inbound_id: str
) -> list[MaterializedInbound]:
    """이 `inbound_id` 에 붙은 **입고 계보**를 읽는다. 쓰기가 없다.

    ```text
    inbound_receipts.inbound_id      ← 이 축으로 찾는다 (uq: sim_run_id + inbound_id)
    inventory_lots.inbound_receipt_id  → Receipt 에서 나온 Lot
    inventory_moves.lot_id (IN)        → 그 Lot 의 원장 입고
    ```

    🔴 **수량·날짜·품목으로 찾지 않는다.** 실데이터에 같은 규모의 Receipt 가 여럿
       있고 `inbound_id` 는 서로 다르다 — 닮았다는 이유로 같은 입고라고 하면 **남의
       입고를 지운다.** 계보는 `inbound_id` 한 축으로만 따라간다.

    ★ **`LEFT JOIN` 이다.** Receipt 만 있고 Lot 이 없는 상태(도착·검수 중)도 계보가
      **있는** 것이다 — 그 건도 일정만 걷어서 될 일이 아니다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT r.receipt_id, r.receipt_status, l.lot_id, m.move_id
                FROM {}.inbound_receipts AS r
                LEFT JOIN {}.inventory_lots AS l
                       ON l.inbound_receipt_id = r.receipt_id
                LEFT JOIN {}.inventory_moves AS m
                       ON m.lot_id = l.lot_id AND m.move_type = %s
                WHERE r.sim_run_id = %s AND r.inbound_id = %s
                ORDER BY r.receipt_id, l.lot_id, m.move_id
                """
            ).format(schema, schema, schema),
            (_IN_MOVE_TYPE, sim_run_id, inbound_id),
        )
        rows = cursor.fetchall()
    return [MaterializedInbound(*_row_values(row)) for row in rows]


def _row_values(row: Any) -> tuple[Any, ...]:
    """`dict_row` 든 tuple 이든 같은 순서로 읽는다.

    ★ 커넥션의 `row_factory` 가 호출자마다 다르다 — `db.get_connection` 은
      `dict_row` 를 쓰고, 남의 트랜잭션을 물려받으면 기본 tuple 일 수 있다.
    """
    if isinstance(row, dict):
        return tuple(row[이름] for 이름 in _LINEAGE_COLUMNS)
    if isinstance(row, Sequence):
        return tuple(row[i] for i in range(len(_LINEAGE_COLUMNS)))
    raise InboundReconciliationError(f"입고 계보 행을 못 읽는다: {row!r}")


def reconcile_orphan_inbound_schedule(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    inbound_id: str,
    reason: str,
    source_ref: str,
    usage_scope: str = USAGE_SCOPE,
) -> InboundReconciliationResult:
    """사람이 확인한 고착 입고 일정 **한 건**을 두 칸에서 함께 걷는다.

    ```text
    ① 도착 전역 잠금
    ② inbound_id 의 입고 계보 조회   Receipt·Lot·IN Move
    ③ 계보가 있으면 거부              ScheduleAlreadyMaterialized
    ④ fixture 행 FOR UPDATE + B-1 재검증 + 양쪽 제거   inbound_stock._clear_schedule
    ```

    🔴 **`inbound_id` 를 이 함수가 고르지 않는다.** 무엇이 잘못된 일정인지는 사람이
       판단하고, 이 함수는 그 판단을 **정확히 한 건만** 실행한다. 그래서 조건 검색도
       일괄 정리도 없다.

    ★ **멱등이다.** 이미 걷힌 건을 같은 요청으로 다시 불러도 예외가 아니라
      `applied=False · removed=0` 이다.

      ⚠️ **한쪽에만 남은 상태는 멱등이 아니다.** 그것은 이미 B-1 위반이라
         `ScheduleIntegrityError` 로 멈춘다 — no-op 으로 접으면 위반을 덮는 것이 된다.

    🔴 **커밋도 롤백도 하지 않는다.** advisory 잠금도 행 잠금도 트랜잭션 수명이라
       호출자의 커밋/롤백과 함께 풀린다 (`materialize_inspected_inbound` 과 같은 규율).

    :param conn: 호출자가 소유한 커넥션. 이 함수는 수명을 관리하지 않는다.
    :param sim_run_id: 어느 실행의 장부인가. **비울 수 없다.**
    :param as_of: 걷어낼 fixture 행의 날짜. 🔴 **그날 행 하나만 본다** — 과거 행은
        그때 실제로 오는 중이었으므로 고치지 않는다 (`cancellation.py` 와 같은 규율).
    :param inbound_id: 사람이 지목한 입고 건. **추측하지 않는다.**
    :param reason: 왜 걷는가. 빈 문자열을 안 받는다.
    :param source_ref: 그 판단의 근거 참조. 빈 문자열을 안 받는다.
    :raises InvalidReconciliationRequest: 축이나 근거가 비었을 때. **DML 전에 막는다.**
    :raises ScheduleAlreadyMaterialized: 그 `inbound_id` 에 Receipt·Lot·IN 이 있을 때.
    :raises ScheduleIntegrityError: 그날 fixture 행이 없거나, 두 칸이 짝이 안 맞거나,
        중복이거나, 두 칸의 사실(B-1)이 다를 때. **아무것도 안 지운다.**
    """
    _require_text(sim_run_id, 칸="sim_run_id")
    _require_text(inbound_id, 칸="inbound_id")
    _require_text(usage_scope, 칸="usage_scope")
    _require_text(reason, 칸="reason")
    _require_text(source_ref, 칸="source_ref")

    schema = sql.Identifier(get_db_schema())

    # ── ① 도착 전역 잠금이 먼저다 ─────────────────────────────────────
    #    ⚠️ 계보 조회보다 앞이어야 한다. 뒤에 두면 "Receipt 가 없다" 로 읽은 사실이
    #       걷는 순간 거짓이 될 수 있다.
    with conn.cursor() as cursor:
        lock_arrival_writes(cursor)

    # ── ② · ③ 계보가 있으면 여기서 멈춘다 ─────────────────────────────
    계보 = _materialized_lineage(conn, schema, sim_run_id=sim_run_id, inbound_id=inbound_id)
    if 계보:
        raise ScheduleAlreadyMaterialized(
            f"이미 입고 계보가 붙은 건이라 일정만 걷을 수 없다 (inbound_id={inbound_id!r},"
            f" sim_run_id={sim_run_id!r}): {계보!r}."
            " orphan 일정과 입고된 사실은 다른 문제다 — 일정을 지우면 그 재고의"
            " 출처를 되짚을 자리가 없어진다."
        )

    # ── ④ 걷는 규율은 입고 경로의 것을 그대로 쓴다 ────────────────────
    applied = _clear_schedule(
        conn,
        schema,
        sim_run_id=sim_run_id,
        as_of=as_of,
        usage_scope=usage_scope,
        inbound_id=inbound_id,
    )
    return InboundReconciliationResult(
        applied=applied,
        inbound_id=inbound_id,
        sim_run_id=sim_run_id,
        as_of=as_of,
        usage_scope=usage_scope,
        # ★ 두 칸에서 **한 건씩 함께** 빠진다. 중복은 위에서 이미 막혔다.
        removed=1 if applied else 0,
        reason=reason,
        source_ref=source_ref,
    )
