"""ledger.py — 재고 수량이 바뀔 때 **원장과 잔량을 한 트랜잭션에서 함께** 바꾼다.

```text
inventory_moves                  앞으로 생기는 증감의 감사 가능한 원장
inventory_lots.remaining_qty_kg  기존 Agent 가 계속 읽는 현재 잔량
```

🔴 **둘 중 하나만 바뀌는 경로를 만들지 않는 것이 이 모듈의 존재 이유다.**
   따로 바뀌면 *"왜 그 숫자인지"* 를 설명할 수 없는 잔량이 생기고, 그 순간
   `v_move_line_integrity` 도 `inventory_moves` 도 사후 검증 도구로 쓸모가 없어진다.

⚠️ **정본을 옮기지 않았다.** DB 주석(`inventory_lots.remaining_qty_kg`)은
   *"정본은 원장"* 이라 적고 있지만 `repository.py` 는 아직 Lot 잔량을 읽는다.
   이 모듈은 그 차이를 **뒤집지 않는다** — 앞으로의 변경이 둘 다 건드리게만 한다.
   기존 232 Move · 80 Lot 을 재계산하지도 Backfill 하지도 않는다.

★ 이번 판이 아는 Move Type 은 `IN` · `OUT` **둘뿐이다.**
  `DISPOSE` · `ADJUST` 는 DB CHECK 어휘로 남아 있지만 실행 기능을 만들지 않았다
  (`ADJUST_IN`/`ADJUST_OUT` 분할도 하지 않는다). 모르는 것을 아는 척하지 않으려고
  명시적으로 막는다 — 조용히 통과시키면 검증 안 된 경로로 원장이 자란다.

🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지도 않는다.**
   `transition.persist_inventory` 와 같은 규율이다. 마스터가 재무 write 와 한 번에
   커밋할 수 있어야 하고, 여기서 커밋하면 **재고만 먼저 확정된 반쪽 장부**가 남는다.
   그래서 `db.fetch_all` · `db.execute_returning_one` 을 쓰지 않는다 — 그것들은
   자기 커넥션을 연다.

⚠️ **재고 원장 쓰기는 트랜잭션 수명의 advisory lock 하나로 의도적으로 직렬화한다.**
   한 바깥 트랜잭션이 여러 재고 이동을 기록할 때 생기는 자원 교차 교착
   (advisory lock ↔ row lock)을 이렇게 없앤다. MVP 의 정확성 우선 결정이며, 필요해지면
   나중에 일괄 잠금 프로토콜로 최적화할 수 있다 (`_lock_ledger_writes` 에 상세).

⚠️ **실패에는 두 종류가 있고 트랜잭션 상태가 다르다. 하나로 뭉뚱그리지 않는다.**

```text
업무 검증 실패                        DML 전에 멈춘다      트랜잭션은 계속 쓸 수 있다
  InvalidMoveQuantity                 아무것도 안 썼다
  UnsupportedMoveType
  MoveLineTotalMismatch
  MoveIdConflict
  RemainingQuantityInsufficient
  OriginalQuantityExceeded
  LotNotFound

DB 무결성 실패                        DML 중에 터진다      🔴 트랜잭션이 aborted 다
  없는 pallet_id · location_id (FK)   INSERT 가 나간 뒤다
  CHECK 위반 (remaining >= 0 등)
  UniqueViolation
```

  🔴 **DB 무결성 실패는 바깥 트랜잭션이 rollback 해야 한다.** 그 뒤로는 같은 커넥션에
     어떤 문장도 못 보낸다 (`current transaction is aborted`). 이 모듈은 그것을 잡지도
     삼키지도 않는다 — 마스터가 경계의 주인이라 되돌릴 곳도 마스터다.
  ★ 그렇다고 FK 를 미리 다 조회해 막지 않는다. 조회와 INSERT 사이는 여전히 비어 있어
    경합을 못 막고, 검사만 두 배로 늘어난다. 막을 수 있는 것(업무 규칙)을 앞에서 막고,
    DB 가 주인인 것은 DB 에 맡긴다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "InvalidMoveQuantity",
    "InventoryLedgerError",
    "LedgerResult",
    "LotNotFound",
    "MoveIdConflict",
    "MoveLine",
    "MoveLineTotalMismatch",
    "OriginalQuantityExceeded",
    "RemainingQuantityInsufficient",
    "UnsupportedMoveType",
    "record_inventory_move",
]

#: **공개 진입점**(`record_inventory_move`)이 실행할 수 있는 Move Type.
#: DB CHECK 은 `DISPOSE` · `ADJUST` 도 받지만 여기서는 받지 않는다.
MoveType = Literal["IN", "OUT"]
_SUPPORTED_MOVE_TYPES: frozenset[str] = frozenset({"IN", "OUT"})

#: 🔴 **`DISPOSE` 는 폐기 확정 전용 진입점(`_record_disposal_move`)으로만 들어온다.**
#:
#: ★ 아무 데서나 쓸 수 있게 열지 않는다 — 폐기는 되돌릴 수 없고 `ADJUST_IN` 도 없어서,
#:   *"업무 규칙을 통과한 경로"* 하나만 남겨 두는 것이 이 상수의 일이다.
#:   그 규칙(폐기대기 근거 · 예약/할당 보호 · 수량 한도)은 `disposal.py` 가 갖는다.
#:
#: ⚠️ `ADJUST` 는 여전히 어느 쪽으로도 안 들어온다 — 실사가 이번 범위 밖이다.
_DISPOSE_MOVE_TYPES: frozenset[str] = frozenset({"DISPOSE"})

#: 같은 `move_id` 로 다시 들어왔을 때 *"같은 건인가"* 를 가르는 칸.
#: 🔴 `note` 도 넣는다 — 같은 id 로 다른 설명이 오면 그것도 다른 사실이다.
#:    빼면 "무결성 오류로 처리한다" 가 조용히 통과로 바뀐다.
_IDENTITY_COLUMNS = (
    "sim_run_id",
    "lot_id",
    "sale_item_id",
    "move_type",
    "quantity_kg",
    "moved_at",
    "reason_code",
    "note",
)

#: Move Line 하나를 **사실로 식별**하는 칸. Header 와 같은 규율이다 — 같은 `move_id` 로
#: 다른 Pallet·Location·수량·설명이 오면 그것도 다른 사실이다.
#:
#: 🔴 `move_line_id` 는 여기 없다. 그것은 BIGSERIAL 이라 **재실행할 때마다 달라지는
#:    값**이고, 넣으면 정상 재시도가 영원히 Conflict 가 된다.
#: ★ `lot_id` 도 없다 — Header 의 것을 그대로 쓰므로 Header 대조에서 이미 갈린다.
_LINE_IDENTITY_COLUMNS = (
    "pallet_id",
    "location_id",
    "quantity_kg",
    "note",
)

#: 재고 원장 쓰기 잠금(`LOGISTICS_LEDGER_WRITE_LOCK`)의 좌표. **기술적 동시성 잠금이지
#: 업무 정책값이 아니다** — 그래서 DB(`agent_policy_config`)에 두지 않고 여기 상수로 둔다.
#:
#: ★ 두-정수 형태(`pg_advisory_xact_lock(classid, objid)`)를 쓰는 이유: advisory lock 은
#:   DB 전체가 나눠 쓰는 64비트 공간이라, 한-정수 형태로 쓰면 **다른 서브시스템의 잠금과
#:   숫자가 겹칠 수 있다.** classid 를 물류 전용으로 고정하면 겹침이 우리 안에서만 일어난다.
#: 🔴 다른 파트가 advisory lock 을 쓰게 되면 이 숫자를 피해야 한다 — 그래서 여기 적어 둔다.
#: ★ 잠금이 하나뿐이라 `move_id` 를 해싱할 일이 없다 — objid 도 고정값이다.
_LEDGER_LOCK_CLASSID = 20260905
_LEDGER_LOCK_OBJID = 1


class InventoryLedgerError(RuntimeError):
    """원장 기록을 멈춘 이유. 아래 넷의 공통 조상이다."""


class LotNotFound(InventoryLedgerError, LookupError):
    """대상 Lot 이 없다. **만들지 않는다** — Lot 생성은 입고 단계 소유다."""


class UnsupportedMoveType(InventoryLedgerError, ValueError):
    """이번 판이 실행하지 않는 Move Type. `DISPOSE` · `ADJUST` 가 여기 걸린다."""


class InvalidMoveQuantity(InventoryLedgerError, ValueError):
    """수량이 양수가 아니다. DB CHECK(`quantity_kg > 0`)보다 먼저 막는다."""


class RemainingQuantityInsufficient(InventoryLedgerError, ValueError):
    """OUT 수량이 현재 잔량보다 크다. **Move 를 쓰기 전에** 멈춘다."""


class OriginalQuantityExceeded(InventoryLedgerError, ValueError):
    """IN 을 반영하면 `remaining_qty_kg > original_qty_kg` 가 된다.

    🔴 DB CHECK `inventory_lots_check` 를 우회하지 않는다 — 그 앞에서 막을 뿐이다.
       Lot 의 최초 수량을 늘리는 것은 원장이 할 일이 아니다 (입고 단계 소유).
    """


class MoveIdConflict(InventoryLedgerError, ValueError):
    """같은 `move_id` 가 **다른 사실**로 이미 있다. 무결성 위반이다.

    ★ 어느 쪽이 진짜인지 여기서 고르지 않는다 — 덮어쓰면 이전 사실이 에러 없이
      사라지고, 사라진 뒤에는 없었던 것과 구별되지 않는다.
    """


class MoveLineTotalMismatch(InventoryLedgerError, ValueError):
    """Move Line 합계가 Header 수량과 다르다.

    `v_move_line_integrity` 가 **사후에** 검출하는 상태를 애초에 못 만들게 막는다
    (그 뷰는 "비어 있어야 정상" 이다). Line 0 건은 정상이라 여기 걸리지 않는다 —
    Pallet 확정 전 입고가 그 상태다.
    """


@dataclass(frozen=True)
class MoveLine:
    """Move 한 건의 Pallet 단위 내역.

    ⚠️ **Pallet 도 Location 도 여기서 만들지 않는다.** 없는 id 를 주면 FK 가 막는다.
       Pallet 배치는 후속 단계 소유다.
    """

    quantity_kg: Decimal
    pallet_id: str | None = None
    location_id: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class LedgerResult:
    """한 번의 기록이 남긴 것.

    ★ `applied` 가 계약의 핵심이다 — `False` 는 **실패가 아니라 이미 반영됨**이다.
      호출자가 둘을 못 가리면 재시도가 잔량을 두 번 바꾸거나, 정상 재시도를
      오류로 읽는다.
    """

    move_id: str
    #: True = 이번 호출이 Move 를 넣고 잔량을 바꿨다 / False = 같은 Move 가 이미 있었다
    applied: bool
    #: 이 호출이 끝난 시점의 Lot 잔량 (반영했으면 반영 후, 아니면 현재값)
    remaining_qty_kg: Decimal
    #: 이번 호출이 넣은 Move Line 수. `applied=False` 면 항상 0 이다.
    line_count: int = 0


def _cell(row: Any, index: int, name: str) -> Any:
    """`fetchone()` 결과에서 한 칸을 꺼낸다.

    ★ row_factory 가 무엇이냐에 따라 튜플로도 매핑으로도 온다. 커넥션을 만드는 곳은
      배선 자리(`app/main.py` · 마스터)이고 이 모듈은 받아 쓸 뿐이라, 여기서 한쪽
      모양을 강요하지 않는다 (`transition._confirmed_inbound_json` 과 같은 이유다).
    """
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _quantity(value: object, *, label: str) -> Decimal:
    """수량을 정규화한다.

    ```text
    Decimal          그대로 쓴다
    int              정확한 값이라 Decimal 로 옮긴다
    float            거절 — 이진 오차가 원장에 영원히 남는다
    non-finite       거절 — NaN · sNaN · Infinity · -Infinity
    0 이하           거절
    ```

    🔴 `Decimal(float)` 은 0.1 이 갖고 있는 이진 오차를 그대로 들여와 수량에 안 보이는
       꼬리를 남긴다 (`transition.build_next_inventory` 가 같은 이유로
       `Decimal(str(x))` 를 쓴다). 원장은 그 꼬리가 영원히 남는 자리라 아예 막는다.

    🔴 **유한성 검사가 부호 검사보다 먼저다.** 순서를 바꾸면 `Decimal("NaN") <= 0` 이
       `decimal.InvalidOperation` 을 올려 **우리 예외가 아닌 것이 밖으로 샌다.**
       `sNaN` 은 더해 보기만 해도 신호를 낸다. `is_finite()` 는 신호를 내지 않는
       술어라 여기서 안전하게 가를 수 있다.
    """
    if isinstance(value, bool):
        raise InvalidMoveQuantity(f"{label} 은 boolean 이 될 수 없다")
    if isinstance(value, Decimal):
        quantity = value
    elif isinstance(value, int):
        quantity = Decimal(value)
    else:
        raise InvalidMoveQuantity(
            f"{label} 은 Decimal 또는 int 여야 한다 (받은 것: {type(value).__name__})."
            " float 은 이진 오차를 원장에 남기므로 받지 않는다."
        )
    if not quantity.is_finite():
        raise InvalidMoveQuantity(
            f"{label} 은 유한한 수여야 한다 (받은 것: {quantity!s})."
            " NaN · sNaN · Infinity 는 수량이 아니다 — DB 에 닿기 전에 막는다."
        )
    if quantity <= 0:
        raise InvalidMoveQuantity(f"{label} 은 0보다 커야 한다 (받은 것: {quantity})")
    return quantity


def _lock_ledger_writes(cursor: Any) -> None:
    """재고 원장 쓰기를 **하나의 전역 잠금으로 직렬화한다.**

    ```text
    재고 원장 쓰기는 트랜잭션 수명의 advisory lock 하나로 **의도적으로 직렬화한다.**

    한 바깥 트랜잭션이 여러 재고 이동을 기록할 때 생기는
    자원 교차 교착(advisory lock ↔ row lock)을 이렇게 없앤다.

    MVP 의 정확성 우선 결정이며, 필요해지면 나중에
    일괄 잠금 프로토콜로 최적화할 수 있다.
    ```

    🔴 **`move_id` 별 잠금은 교착을 못 막았다.** 종전 판이 *"여러 Move 를 기록할 때는
       `move_id` 순서를 고정하면 안전하다"* 고 적었는데 **틀린 말이었다.**

    ```text
    T1  record(MOVE-1, LOT-A)   → adv(MOVE-1) 획득 · LOT-A row lock 획득
    T2  record(MOVE-2, LOT-A)   → adv(MOVE-2) 획득 · LOT-A 를 기다린다
    T1  record(MOVE-2, LOT-A)   → adv(MOVE-2) 를 기다린다
        ⇒ T1 은 T2 의 advisory 를, T2 는 T1 의 row lock 을 기다린다 — 교착
    ```

    ★ **정렬로 못 푸는 이유:** T2 는 `MOVE-1` 을 **요청한 적이 없다.** 두 트랜잭션이
      요청하는 잠금 **집합 자체가 달라서** 전순서를 매길 수 없다. 두 잠금 모두
      트랜잭션이 끝날 때까지 안 풀리는 것이 이 교착의 뿌리다.

    ⇒ 잠금을 **하나로 합치면** 전순서가 저절로 성립한다. 기다리는 쪽은 아직 아무
      자원도 안 쥐고 있어 순환이 생길 자리가 없다.

    ★ **같은 트랜잭션 안에서는 재진입한다.** 같은 세션이 같은 키를 몇 번 잡아도
      자기를 막지 않으므로, 한 트랜잭션이 `record_inventory_move` 를 여러 번 불러도
      두 번째 호출이 스스로 멈추지 않는다 (실측 확인).

    ★ **transaction-level(`_xact_`) 이다.** 바깥 트랜잭션의 커밋/롤백과 함께 자동으로
      풀린다 — 이 모듈은 unlock 을 부르지 않고, 부를 수도 없어야 한다. session-level 을
      쓰면 풀어 줄 주인이 없어 커넥션에 잠금이 눌어붙는다.

    ★ **커넥션을 새로 열지도, 커밋하지도 않는다.** 호출자가 준 커서로 건다.

    ⚠️ **대가는 쓰기 동시성이다.** 서로 다른 Lot 에 대한 재고 이동도 한 줄로 선다.
       MVP 에서 이 거래는 받아들이기로 한 것이다 — 원장이 어긋나는 것보다 느린 것이 낫다.
    """
    cursor.execute(
        sql.SQL("SELECT pg_advisory_xact_lock(%s, %s)"),
        (_LEDGER_LOCK_CLASSID, _LEDGER_LOCK_OBJID),
    )


def _record_disposal_move(
    conn: Any,
    *,
    move_id: str,
    sim_run_id: str,
    lot_id: str,
    quantity_kg: Decimal,
    moved_at: date,
    reason_code: str,
    note: str | None = None,
) -> LedgerResult:
    """폐기 확정이 남기는 `DISPOSE` Move. **`disposal.confirm_disposal()` 전용이다.**

    🔴 **공개 API 가 아니다.** `__all__` 에 없고 이름도 밑줄로 시작한다 —
       폐기의 업무 진입점은 `disposal.confirm_disposal()` **하나**여야 하고,
       이 함수가 밖에서 보이면 후보 검증·예약 보호를 건너뛰는 우회로가 생긴다.

    🔴 **다른 곳에서 부르지 않는다.** 폐기 여부·수량 한도·예약 보호는 `disposal.py`
       가 판단하고, 이 함수는 그 판단이 끝난 뒤 **원장에 적는 일만** 한다.
       여기에 업무 규칙을 얹으면 원장이 다시 판단하는 자리가 되고, 두 곳이 갈린다.

    ★ **검증된 경로를 그대로 쓴다.** 잠금 → 멱등 대조 → Lot `FOR UPDATE` → 수량 검사
      → 쓰기 순서가 `record_inventory_move` 와 **같은 함수**다. 폐기만 다른 길로
      가면 그 길에서 잔량이 어긋난다.

    ⚠️ `sale_item_id` 와 `lines` 를 받지 않는다 — 폐기는 판매 건도 Pallet 배분도 없다.
    """
    return _record_move(
        conn,
        move_id=move_id,
        sim_run_id=sim_run_id,
        lot_id=lot_id,
        move_type="DISPOSE",
        quantity_kg=quantity_kg,
        moved_at=moved_at,
        reason_code=reason_code,
        sale_item_id=None,
        note=note,
        lines=(),
        allowed_move_types=_DISPOSE_MOVE_TYPES,
    )


def record_inventory_move(
    conn: Any,
    *,
    move_id: str,
    sim_run_id: str,
    lot_id: str,
    move_type: MoveType,
    quantity_kg: Decimal,
    moved_at: date,
    reason_code: str,
    sale_item_id: str | None = None,
    note: str | None = None,
    lines: Sequence[MoveLine] = (),
) -> LedgerResult:
    """Move 를 남기고 Lot 잔량을 **같은 트랜잭션에서** 바꾼다.

    ```text
    ① 입력·Line 검증                   DB 를 안 만진다
    ② 원장 쓰기 advisory xact lock     재고 원장 쓰기 전체를 한 줄로 세운다
    ③ 같은 move_id 가 이미 있나
       ├ 있음  Header 대조 → Line 대조 → Lot 잔량 **읽기** → applied=False
       └ 없음  ↓
    ④ Lot 을 FOR UPDATE 로 잠근다      잔량을 바꾸는 행을 고정한다
    ⑤ 수량 규칙을 검사한다             ★ 쓰기 전에 한다 — 실패해도 Move 가 안 남는다
    ⑥ Move INSERT
    ⑦ Move Line INSERT (주어진 경우만)
    ⑧ Lot UPDATE
    ```

    🔴 **② 가 ③ 앞이고 ③ 이 ④ 앞인 것이 이 함수의 동시성 계약이다.**

    ```text
    ② 없으면   같은 move_id 를 다른 Lot 으로 보낸 둘이 서로 다른 행을 잠그고 지나쳐,
               뒤엣것이 MoveIdConflict 가 아니라 raw UniqueViolation 으로 터진다
    ③ 이 ④ 뒤면 "같은 move_id 인데 없는 Lot" 이 LotNotFound 로 나간다 —
               그것은 부재가 아니라 멱등 키 충돌이다
    ⑤ 가 ⑥ 뒤면 초과 OUT 이 "Move 는 남고 잔량은 그대로" 를 만들고,
               정리를 호출자의 rollback 에 떠넘기게 된다
    ```

    ★ **한 바깥 트랜잭션이 이 함수를 여러 번 불러도 안전하다.** ② 의 잠금은 하나뿐이고
      같은 트랜잭션 안에서 재진입하므로 두 번째 호출이 자기를 막지 않는다. 잠금이
      `move_id` 별이던 종전 판은 여기서 교착이 났다 (`_lock_ledger_writes` 참조).

    🔴 **커밋·롤백하지 않고 커넥션을 새로 열지 않는다.** 인자로 받은 `conn` 만 쓴다.
       advisory lock 도 transaction-level 이라 **호출자의 커밋/롤백과 함께** 풀린다 —
       이 모듈은 unlock 을 부르지 않는다.

    ★ ③ 이 ⑤ 보다 **먼저**여야 한다. 순서를 바꾸면 이미 반영된 OUT 을 다시 보냈을 때
      (잔량이 이미 줄어 있으므로) 초과 판정이 나서, **정상 재시도가 오류가 된다.**

    ⚠️ **멱등 보장은 READ COMMITTED 를 전제로 한다** (PostgreSQL 기본값이자 이 DB 의
       설정값). REPEATABLE READ 이상에서는 두 번째 트랜잭션의 스냅샷이 advisory lock 을
       기다리기 **전에** 찍혀, 먼저 커밋된 Move 를 못 보고 INSERT 로 나아갈 수 있다.
       바깥 트랜잭션의 격리수준은 마스터가 정하므로 여기서 강제하지 않고 밝혀만 둔다.

    :param conn: 마스터/호출자가 쥔 커넥션. 이 함수는 소유하지 않는다.
    :param move_id: 원장 PK 이자 **멱등 키**. 같은 값을 두 번 보내도 잔량은 한 번만 바뀐다.
    :param lines: Pallet 단위 내역. 비어 있어도 된다 — Pallet 확정 전이 그 상태다.
        주면 합계가 `quantity_kg` 와 정확히 같아야 하고, 재실행 시 **Line 사실도**
        같아야 한다 (순서는 무관, 개수는 유의미 — `_assert_same_lines`).
    :raises LotNotFound: 대상 Lot 이 없다. 만들지 않는다.
    :raises UnsupportedMoveType: `IN`/`OUT` 이 아니다.
    :raises MoveIdConflict: 같은 `move_id` 가 다른 Header 사실 **또는 다른 Line** 으로
        이미 있다.
    :raises InvalidMoveQuantity: 수량이 Decimal/int 가 아니거나, 유한하지 않거나, 양수가
        아니다.
    :raises RemainingQuantityInsufficient: OUT 이 현재 잔량을 넘는다.
    :raises OriginalQuantityExceeded: IN 이 Lot 최초 수량을 넘게 만든다.
    :raises MoveLineTotalMismatch: Line 합계가 Header 수량과 다르다.
    """
    return _record_move(
        conn,
        move_id=move_id,
        sim_run_id=sim_run_id,
        lot_id=lot_id,
        move_type=move_type,
        quantity_kg=quantity_kg,
        moved_at=moved_at,
        reason_code=reason_code,
        sale_item_id=sale_item_id,
        note=note,
        lines=lines,
        allowed_move_types=_SUPPORTED_MOVE_TYPES,
    )


def _record_move(
    conn: Any,
    *,
    move_id: str,
    sim_run_id: str,
    lot_id: str,
    move_type: str,
    quantity_kg: Decimal,
    moved_at: date,
    reason_code: str,
    sale_item_id: str | None,
    note: str | None,
    lines: Sequence[MoveLine],
    allowed_move_types: frozenset[str],
) -> LedgerResult:
    """원장 쓰기의 **공통 경로**. 두 공개 진입점이 이 하나를 나눠 쓴다.

    ★ **어떤 Move Type 을 받을지는 부르는 쪽이 정한다** (`allowed_move_types`).
      그래서 `DISPOSE` 가 열려도 `record_inventory_move` 는 여전히 IN·OUT 만 받는다 —
      폐기가 아무 데서나 새어 들어오지 않는 자리가 여기다.
    """
    if move_type not in allowed_move_types:
        raise UnsupportedMoveType(
            f"이 진입점이 받는 Move Type 이 아니다 (받은 것: {move_type!r},"
            f" 허용: {sorted(allowed_move_types)})."
            " DISPOSE 는 폐기 확정 전용 진입점으로만 들어오고, ADJUST 는 아직"
            " 업무 규칙이 정해지지 않았다."
        )
    quantity = _quantity(quantity_kg, label="quantity_kg")
    line_quantities = [_quantity(line.quantity_kg, label="lines[].quantity_kg") for line in lines]
    if line_quantities and sum(line_quantities, start=Decimal(0)) != quantity:
        raise MoveLineTotalMismatch(
            f"Move Line 합계({sum(line_quantities, start=Decimal(0))})가"
            f" Header 수량({quantity})과 다르다 (move_id={move_id})."
            " v_move_line_integrity 가 사후에 잡을 상태를 만들지 않는다."
        )

    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        # ★ **아무것도 읽거나 쓰기 전에 원장 잠금을 잡는다.** 기다리는 쪽이 아직 아무
        #   자원도 안 쥐고 있어야 순환이 생길 자리가 없다 (`_lock_ledger_writes` 참조).
        _lock_ledger_writes(cursor)

        requested = {
            "sim_run_id": sim_run_id,
            "lot_id": lot_id,
            "sale_item_id": sale_item_id,
            "move_type": move_type,
            "quantity_kg": quantity,
            "moved_at": moved_at,
            "reason_code": reason_code,
            "note": note,
        }
        existing = _existing_move(cursor, schema, move_id=move_id)
        if existing is not None:
            # 🔴 **Lot 을 보기 전에 사실부터 대조한다.** Lot 을 먼저 읽으면 *"같은 move_id
            #    인데 없는 Lot 을 가리킨다"* 가 `LotNotFound` 로 나간다 — 그것은 부재가
            #    아니라 **멱등 키 충돌**이고, 부르는 쪽이 둘을 가려야 한다.
            _assert_same_facts(move_id=move_id, existing=existing, requested=requested)
            # 🔴 **Header 만 보면 안 된다.** 같은 20kg 이어도 어느 Pallet 에서 어느 자리로
            #    나갔는지가 다르면 다른 사실이다 — Header 만 대조하면 그 차이가 에러 없이
            #    삼켜지고, 두 번째 요청의 Line 은 장부 어디에도 안 남는다.
            _assert_same_lines(
                move_id=move_id,
                existing=_existing_move_lines(cursor, schema, move_id=move_id),
                requested=[
                    (line.pallet_id, line.location_id, line_quantity, line.note)
                    for line, line_quantity in zip(lines, line_quantities, strict=True)
                ],
            )
            # ★ 같은 건이다. 잔량을 **다시 바꾸지 않는다.** 실패가 아니라 이미 반영됨이다.
            #   ⚠️ 여기서는 Lot 을 **읽기만** 한다 — 바꿀 것이 없는데 행 잠금을 잡으면
            #      교착 면적만 넓어진다.
            return LedgerResult(
                move_id=move_id,
                applied=False,
                remaining_qty_kg=_current_remaining(
                    cursor, schema, lot_id=lot_id, sim_run_id=sim_run_id
                ),
                line_count=0,
            )

        current_remaining, original = _lock_lot(
            cursor, schema, lot_id=lot_id, sim_run_id=sim_run_id
        )
        next_remaining = _next_remaining(
            move_type=move_type,
            quantity=quantity,
            current_remaining=current_remaining,
            original=original,
            lot_id=lot_id,
            move_id=move_id,
        )

        _insert_move(cursor, schema, move_id=move_id, values=requested)
        for line, line_quantity in zip(lines, line_quantities, strict=True):
            _insert_move_line(
                cursor, schema, move_id=move_id, lot_id=lot_id, line=line, quantity=line_quantity
            )
        _update_remaining(
            cursor,
            schema,
            lot_id=lot_id,
            sim_run_id=sim_run_id,
            next_remaining=next_remaining,
            move_id=move_id,
        )

    return LedgerResult(
        move_id=move_id,
        applied=True,
        remaining_qty_kg=next_remaining,
        line_count=len(line_quantities),
    )


def _lock_lot(
    cursor: Any, schema: sql.Identifier, *, lot_id: str, sim_run_id: str
) -> tuple[Decimal, Decimal]:
    """대상 Lot 을 잠그고 (현재 잔량, 최초 수량)을 읽는다.

    ★ **이 모듈의 동시성 방어는 잠금 둘이 나눠 맡는다.**

    ```text
    _lock_ledger_writes()   원장 쓰기 전체를 전역으로 직렬화한다 (advisory xact lock)
    여기의 FOR UPDATE       수량을 바꾸는 그 Lot 행을 잡아 둔다
    ```

    🔴 **둘 다 바깥 트랜잭션이 끝날 때까지 풀리지 않는다.** 그래서 잠금 순서가
       **원장 잠금 → Lot 행** 한 방향으로 고정되어야 하고, 이 함수는 원장 잠금을 이미
       쥔 뒤에만 불린다 (`record_inventory_move` 의 ② → ④).

    ⚠️ **전역 직렬화가 있으니 이 행 잠금이 남아도는 것은 아니다.** 원장 잠금은 이 모듈을
       지나는 쓰기만 세우고, `inventory_lots` 는 원장 밖에서도 갱신될 수 있는 표다
       (`database/mvp_demo_remove_pimanul.sql` 같은 직접 UPDATE 가 실제로 있었다).
       행 잠금이 없으면 그런 경로와 겹칠 때 두 쪽이 같은 잔량을 읽고 각자 빼서, 각각은
       검사를 통과하는데 합쳐 놓으면 음수가 된다 — DB CHECK 이 마지막에 잡더라도 그때는
       어느 쪽이 틀렸는지 알 수 없다.

    ★ `sim_run_id` 를 조건에 넣는다 — 잔량을 바꾸는 쪽은 *"어느 실행의 장부인가"* 를
      알고 부른다 (`transition.persist_inventory` 의 WHERE 와 같은 규율).
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT remaining_qty_kg, original_qty_kg
            FROM {}.inventory_lots
            WHERE lot_id = %s AND sim_run_id = %s
            FOR UPDATE
            """
        ).format(schema),
        (lot_id, sim_run_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise LotNotFound(
            f"재고 Lot 이 없다 (lot_id={lot_id}, sim_run_id={sim_run_id})."
            " 새로 만들지 않는다 — Lot 생성은 입고 단계 소유다."
        )
    return _cell(row, 0, "remaining_qty_kg"), _cell(row, 1, "original_qty_kg")


def _current_remaining(
    cursor: Any, schema: sql.Identifier, *, lot_id: str, sim_run_id: str
) -> Decimal:
    """Lot 잔량을 **읽기만** 한다. 멱등 재시도가 현재값을 돌려줄 때 쓴다.

    ★ `FOR UPDATE` 를 걸지 않는다 — 이 경로는 아무것도 바꾸지 않는다. 바꾸지 않는데
      행 잠금을 잡으면 교착이 날 수 있는 면적만 넓어진다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT remaining_qty_kg
            FROM {}.inventory_lots
            WHERE lot_id = %s AND sim_run_id = %s
            """
        ).format(schema),
        (lot_id, sim_run_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise LotNotFound(
            f"재고 Lot 이 없다 (lot_id={lot_id}, sim_run_id={sim_run_id})."
            " 새로 만들지 않는다 — Lot 생성은 입고 단계 소유다."
        )
    return _cell(row, 0, "remaining_qty_kg")


def _existing_move(cursor: Any, schema: sql.Identifier, *, move_id: str) -> dict[str, Any] | None:
    """같은 `move_id` 가 이미 있으면 그 사실들을 돌려준다.

    ★ 새 멱등 컬럼을 만들지 않았다 — `inventory_moves_pkey` 가 이미 `move_id` 라
      그 하나로 *"같은 건인가"* 를 물을 수 있다. 마이그레이션이 필요 없다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT sim_run_id, lot_id, sale_item_id, move_type,
                   quantity_kg, moved_at, reason_code, note
            FROM {}.inventory_moves
            WHERE move_id = %s
            """
        ).format(schema),
        (move_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {name: _cell(row, index, name) for index, name in enumerate(_IDENTITY_COLUMNS)}


def _existing_move_lines(
    cursor: Any, schema: sql.Identifier, *, move_id: str
) -> list[tuple[Any, ...]]:
    """이미 있는 Move 의 Line 사실들.

    ★ **중복 판정에 걸렸을 때만 읽는다.** 정상 경로(새 Move)는 이 조회를 지나지 않는다.
    ★ `ORDER BY` 를 걸지 않는다 — 순서는 업무 사실이 아니라서 multiset 으로 대조한다
      (`_assert_same_lines`). 정렬해도 결과가 같으니 DB 에 일을 시키지 않는다.
    """
    cursor.execute(
        sql.SQL(
            """
            SELECT pallet_id, location_id, quantity_kg, note
            FROM {}.inventory_move_lines
            WHERE move_id = %s
            """
        ).format(schema),
        (move_id,),
    )
    return [
        tuple(_cell(row, index, name) for index, name in enumerate(_LINE_IDENTITY_COLUMNS))
        for row in cursor.fetchall()
    ]


def _assert_same_facts(
    *, move_id: str, existing: Mapping[str, Any], requested: Mapping[str, Any]
) -> None:
    """같은 id 의 두 사실을 대조한다. 다르면 **어느 쪽도 고르지 않고** 멈춘다."""
    differing = [
        f"{name}: 기존={existing[name]!r} 요청={requested[name]!r}"
        for name in _IDENTITY_COLUMNS
        if existing[name] != requested[name]
    ]
    if differing:
        raise MoveIdConflict(
            f"같은 move_id 가 다른 사실로 이미 있다 (move_id={move_id}). "
            + " / ".join(differing)
            + ". 덮어쓰지 않는다 — 이전 사실이 에러 없이 사라지면 없었던 것과"
            " 구별되지 않는다."
        )


def _assert_same_lines(
    *,
    move_id: str,
    existing: Sequence[tuple[Any, ...]],
    requested: Sequence[tuple[Any, ...]],
) -> None:
    """같은 id 의 두 Line 묶음을 대조한다. **순서는 사실이 아니고 개수는 사실이다.**

    ```text
    P1 10kg · P2 10kg   ↔   P2 10kg · P1 10kg      같다   (순서만 다르다)
    P1 10kg · P1 10kg   ↔   P1 10kg                다르다 (개수가 다르다)
    Line 0건            ↔   Line 1건               다르다
    ```

    ★ **multiset 으로 본다.** 집합으로 보면 중복이 뭉개져 *"P1 두 판"* 과 *"P1 한 판"* 이
      같아지고, 순서까지 보면 같은 물건을 다른 차례로 적은 정상 재시도가 Conflict 가 된다.

    ★ `Decimal("10")` 과 `Decimal("10.000000")` 은 같은 수량이다 — Python 은 값이 같은
      Decimal 의 해시를 같게 보장하므로 `Counter` 대조가 DB 왕복(numeric(18,6))을 넘어
      성립한다. 여기가 흔들리면 **정상 재시도가 Conflict 로 뒤집힌다.**
    """
    existing_counts = Counter(existing)
    requested_counts = Counter(requested)
    if existing_counts == requested_counts:
        return

    only_existing = existing_counts - requested_counts
    only_requested = requested_counts - existing_counts
    # ★ `key=repr` 로 정렬한다 — Line 튜플에는 `None` 과 `Decimal` 이 섞여 있어
    #   자연 정렬은 TypeError 를 낸다. 메시지 순서를 고정하려다 **진단이 터지면 안 된다.**
    detail = [f"Line 수: 기존={len(existing)} 요청={len(requested)}"]
    detail += [
        f"기존에만 {count}건: {line!r}" for line, count in sorted(only_existing.items(), key=repr)
    ]
    detail += [
        f"요청에만 {count}건: {line!r}" for line, count in sorted(only_requested.items(), key=repr)
    ]
    raise MoveIdConflict(
        f"같은 move_id 가 다른 Move Line 으로 이미 있다 (move_id={move_id}). "
        + " / ".join(detail)
        + f". 대조 칸은 {', '.join(_LINE_IDENTITY_COLUMNS)} 이고 순서는 보지 않는다."
        " 덮어쓰지 않는다 — 어느 Pallet 에서 나갔는지가 조용히 바뀌면"
        " 재고 실사에서 되짚을 근거가 사라진다."
    )


def _next_remaining(
    *,
    move_type: str,
    quantity: Decimal,
    current_remaining: Decimal,
    original: Decimal,
    lot_id: str,
    move_id: str,
) -> Decimal:
    """반영 후 잔량. **쓰기 전에** 계산하고 검사한다.

    ⚠️ DB CHECK 둘(`remaining >= 0` · `remaining <= original`)을 우회하지 않는다.
       같은 규칙을 앞에서 한 번 더 볼 뿐이고, 그래야 실패해도 트랜잭션이 살아 있다.
    """
    # ★ `DISPOSE` 도 잔량을 **줄이는** 방향이다 — OUT 과 같은 규칙을 쓴다.
    #   두 방향을 한 자리에서 보게 두어야 "폐기만 다르게 센다" 가 생기지 않는다.
    if move_type in ("OUT", "DISPOSE"):
        if quantity > current_remaining:
            raise RemainingQuantityInsufficient(
                f"{move_type} 수량({quantity})이 현재 잔량({current_remaining})보다 크다"
                f" (lot_id={lot_id}, move_id={move_id}). Move 를 남기지 않는다."
            )
        return current_remaining - quantity

    next_remaining = current_remaining + quantity
    if next_remaining > original:
        raise OriginalQuantityExceeded(
            f"IN 을 반영하면 잔량({next_remaining})이 Lot 최초 수량({original})을 넘는다"
            f" (lot_id={lot_id}, move_id={move_id}). Move 를 남기지 않는다 —"
            " 최초 수량을 늘리는 것은 원장이 아니라 입고 단계가 할 일이다."
        )
    return next_remaining


def _insert_move(
    cursor: Any, schema: sql.Identifier, *, move_id: str, values: Mapping[str, Any]
) -> None:
    """원장 Header 한 줄.

    ★ `ON CONFLICT` 를 쓰지 않는다 — 중복은 위에서 이미 사실 대조로 가렸다.
      여기서 조용히 넘기면 *"같은 id 인데 다른 사실"* 이 통과한다.
    """
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {}.inventory_moves (
                move_id, sim_run_id, lot_id, sale_item_id,
                move_type, quantity_kg, moved_at, reason_code, note
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        ).format(schema),
        (
            move_id,
            values["sim_run_id"],
            values["lot_id"],
            values["sale_item_id"],
            values["move_type"],
            values["quantity_kg"],
            values["moved_at"],
            values["reason_code"],
            values["note"],
        ),
    )


def _insert_move_line(
    cursor: Any,
    schema: sql.Identifier,
    *,
    move_id: str,
    lot_id: str,
    line: MoveLine,
    quantity: Decimal,
) -> None:
    """Pallet 단위 내역 한 줄.

    ★ `lot_id` 는 Header 의 것을 그대로 쓴다 — 호출자가 따로 주지 않는다.
      복합 FK 둘(`fk_move_lines_move_lot` · `fk_move_lines_pallet_lot`)이 Line 의 Lot 을
      Header·Pallet 과 일치시키는데, 호출자가 다른 Lot 을 넣을 수 있게 두면
      그 제약에 걸리는 것이 **업무 실수가 아니라 API 실수**가 된다.
    """
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {}.inventory_move_lines (
                move_id, lot_id, pallet_id, location_id, quantity_kg, note
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """
        ).format(schema),
        (move_id, lot_id, line.pallet_id, line.location_id, quantity, line.note),
    )


def _update_remaining(
    cursor: Any,
    schema: sql.Identifier,
    *,
    lot_id: str,
    sim_run_id: str,
    next_remaining: Decimal,
    move_id: str,
) -> None:
    """Lot 잔량을 반영값으로 놓는다.

    ★ `status` 를 건드리지 않는다 — 잔량이 0 이 돼도 `DEPLETED` 로 바꾸지 않고,
      IN 이 들어와도 `ACTIVE` 로 되돌리지 않는다. 상태 결정은 입고·출고 단계 소유다
      (미결 사항으로 보고했다).

      ⚠️ 그래도 기존 Agent 계약은 깨지지 않는다 —
      `repository.get_current_logistics_read()` 가 `remaining_qty_kg > 0` 으로 거르므로
      잔량 0 인 Lot 은 status 와 무관하게 Snapshot 에서 빠진다.

    ★ `rowcount` 를 본다. 잠글 때 있던 행이라 0 이 나올 수 없지만, 나오면 잠금 조건과
      쓰기 조건이 갈렸다는 뜻이라 조용히 지나가면 안 된다.
    """
    cursor.execute(
        sql.SQL(
            """
            UPDATE {}.inventory_lots
            SET remaining_qty_kg = %s
            WHERE lot_id = %s AND sim_run_id = %s
            """
        ).format(schema),
        (next_remaining, lot_id, sim_run_id),
    )
    if cursor.rowcount != 1:
        raise LotNotFound(
            f"잔량을 갱신할 Lot 이 없다 (lot_id={lot_id}, sim_run_id={sim_run_id},"
            f" move_id={move_id}). 잠근 행과 쓰는 행이 갈렸다."
        )
