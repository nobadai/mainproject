"""receipts.py — 입고 Receipt 의 **존재 여부만** 묻는다 (3-B4-C).

```text
arrival.select_due_inbound  →  due 행           물류가 아는 사실만으로 자격이 있나
receipts.check_receipt_state →  NEW / ALREADY_EXISTS   이 건이 이미 처리됐나
```

★ **`arrival.py` 와 나눈 이유가 있다.** 저쪽은 **순수 계산**이고 그 순수성을
  테스트가 잠그고 있다(임포트 목록까지 고정한다). 이 파일은 DB 를 읽으므로 섞으면
  그 방어가 통째로 무너진다 — 분류는 저쪽, 조회는 이쪽이다.

🔴 **`repository.py` 에 두지 않았다.** 그쪽은 `db.fetch_all` 로 **자기 커넥션을 연다.**
   이 조회는 나중에 Receipt·검수·Lot·원장 IN 과 **한 트랜잭션**에 들어가야 해서,
   호출자가 준 커넥션만 써야 한다 (`ledger.py` · `transition.persist_inventory` 가
   같은 이유로 `repository` 를 안 쓴다).

🔴 **읽기만 한다.** INSERT · UPDATE · DELETE · commit · rollback 이 없다.
   Receipt 를 만드는 것은 다음 단계다.

⚠️ **이 조회 하나로는 동시 중복 생성을 막지 못한다.**

   ```text
   T1  조회 → 0건 (NEW)
   T2  조회 → 0건 (NEW)      ← 둘 다 "새 건" 으로 본다
   T1  INSERT
   T2  INSERT                 여기서야 부딪힌다
   ```

   🔴 **여기서 미리 풀지 않는다.** 쓰기 경로가 아직 없어서 무엇을 어떤 순서로
      잠글지 정할 근거가 없고, 근거 없이 만든 잠금은 `ledger.py` 가 겪은 교착을
      다시 부른다. 막는 것은 **쓰기 단계의 일**이고 재료는 둘이다 —
      이미 있는 `uq_inbound_receipts_inbound_id UNIQUE (sim_run_id, inbound_id)` 와,
      INSERT 설계를 감사한 뒤 고를 트랜잭션 잠금 전략.

   ⚠️ **UniqueViolation 을 정상 흐름으로 쓰지 않는다** (`ledger.py` 와 같은 규율).
      DB 무결성 예외는 트랜잭션을 aborted 로 만들어 바깥이 롤백할 수밖에 없게 한다 —
      *"이미 있으니 넘어간다"* 를 그것으로 표현하면 멀쩡한 재실행이 장애가 된다.

★ **스키마 실측 (2026-09-05 · 현재 브랜치 `database/30_logistics_wms_schema.sql`).**

  ```text
  PRIMARY KEY   inbound_receipts_pkey (receipt_id)
  UNIQUE        uq_inbound_receipts_inbound_id (sim_run_id, inbound_id)
  sim_run_id    TEXT NOT NULL   FK → sim_runs
  inbound_id    TEXT            🔴 nullable
  receipt_id    TEXT NOT NULL
  ```

  🔴 **`inbound_id` 가 nullable 이라 UNIQUE 가 완전하지 않다.** PostgreSQL 은 UNIQUE
     에서 NULL 을 서로 다른 값으로 보므로 `inbound_id IS NULL` 인 행은 몇 개든 선다.
     그래서 이 조회는 **빈 식별자를 아예 받지 않는다** — 없는 열쇠로 물으면
     *"0건이니 새 건"* 이라는 틀린 답이 나오고, 뒤 단계가 그 쓰레기 식별자로 행을
     만들어 UNIQUE 축을 오염시킨다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "InvalidInboundIdentity",
    "ReceiptExistence",
    "ReceiptExistenceStatus",
    "ReceiptIntegrityError",
    "ReceiptLookupError",
    "check_receipt_state",
]


#: 조회가 낸 답. **둘 다 정상이다** — 실패가 아니다.
#:
#: ```text
#: NEW             아직 Receipt 가 없다. 뒤 단계가 만들 수 있다
#: ALREADY_EXISTS  이미 있다. 멱등 재실행이지 오류가 아니다
#: ```
ReceiptExistenceStatus = Literal["NEW", "ALREADY_EXISTS"]

#: 🔴 **한 번에 둘까지만 읽는다.** 셋을 가르는 데 그 이상이 필요 없다.
#:
#: ★ 정확한 개수를 세도 **결정이 달라지지 않는다** — 2건이든 5건이든 우리는 어느
#:   하나도 고르지 않고 멈춘다. 그래서 개수 대신 **부딪힌 두 `receipt_id`** 를
#:   남긴다. 조사에 쓸모 있는 쪽은 그쪽이다.
_CORRUPTION_PROBE_LIMIT = 2


class ReceiptLookupError(RuntimeError):
    """이 모듈이 내는 실패의 조상.

    ★ `ledger.InventoryLedgerError` 와 같은 결이다 — 호출자가 *"입고 조회가
      실패했다"* 를 한 번에 잡을 수 있게 두되, 종류는 아래에서 갈라 둔다.
    """


class InvalidInboundIdentity(ReceiptLookupError, ValueError):
    """조회 열쇠로 쓸 수 없는 `inbound_id` 다.

    🔴 **없는 열쇠로 DB 에 묻지 않는다.** 물으면 0건이 나오고, 그 0건은
       *"아직 Receipt 가 없다"* 로 읽힌다 — **없는 것과 물어보지 못한 것이 같은
       답으로 뭉개진다.**

    ★ 도착 후보 선택(`arrival.select_due_inbound`)이 이미 같은 눈으로 걸러 준다
      (`ARRIVAL_INBOUND_ID_MISSING`). 여기서 다시 보는 것은 **이 함수가 그 경로
      밖에서도 안전해야** 하기 때문이지, 저쪽을 못 믿어서가 아니다.
    """


class ReceiptIntegrityError(ReceiptLookupError, ValueError):
    """같은 `(sim_run_id, inbound_id)` 에 Receipt 가 둘 이상이다. 무결성 위반이다.

    🔴 **어느 것이 진짜인지 여기서 고르지 않는다.** 최신을 고르는 것도, 첫 행을
       고르는 것도, 조용히 하나로 합치는 것도 **전부 고르는 것**이다. 고른 뒤에는
       버려진 쪽이 있었다는 사실조차 남지 않는다.

    ★ `repository.get_active_logistics_runtime_fixture` 가 활성 fixture 2건에,
      `transition._index_by_inbound_id` 가 중복 `inbound_id` 에 하는 일과 같다 —
      깨진 상태 위에서 계속 걷지 않는다.

    ⚠️ `uq_inbound_receipts_inbound_id` 가 있으면 원래 못 생기는 상태다. 그래도
       검사하는 이유는 **제약이 아직 안 적용된 DB 도 있을 수 있어서**다 (그 표는
       `30_logistics_wms_schema.sql` 로 최근에 회수됐다).
    """


@dataclass(frozen=True)
class ReceiptExistence:
    """조회 결과. **작게 둔다.**

    ★ Receipt 의 다른 칸(`receipt_status` · `arrived_at` · 수량들)을 싣지 않았다.
      지금 필요한 판단은 *"있나 없나"* 하나이고, 안 쓰는 값을 실어 두면 뒤 단계가
      **여기서 읽은 낡은 값**을 쓰게 된다.
    """

    status: ReceiptExistenceStatus
    #: `ALREADY_EXISTS` 면 그 행의 권위 있는 `receipt_id`, `NEW` 면 `None`.
    receipt_id: str | None


def _cell(row: Any, index: int, name: str) -> Any:
    """`fetchall()` 한 행에서 한 칸을 꺼낸다.

    ★ row_factory 가 무엇이냐에 따라 튜플로도 매핑으로도 온다. 커넥션을 만드는 곳은
      배선 자리이고 이 모듈은 받아 쓸 뿐이라 한쪽 모양을 강요하지 않는다
      (`ledger._cell` · `transition._stored_json` 과 같은 이유다).
    """
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def check_receipt_state(
    conn: Any,
    *,
    sim_run_id: str,
    inbound_id: str,
) -> ReceiptExistence:
    """이 입고 건에 Receipt 가 이미 있는가. **읽기만 한다.**

    ```text
    0건      NEW              아직 없다 — 뒤 단계가 만들 수 있다
    1건      ALREADY_EXISTS   이미 있다 — 멱등 재실행이다. 예외가 아니다
    2건 이상  ReceiptIntegrityError
    ```

    🔴 **조회 열쇠는 `(sim_run_id, inbound_id)` 다.** DB 의 유일성 축과 **같아야**
       한다 — 다른 축으로 물으면 *"있다/없다"* 와 *"두 번 못 선다"* 가 서로 다른
       것을 뜻하게 된다.

    🔴 **`purchase_id` · `approval_id` · 품목명 · 도착예정일로 찾지 않는다.**
       그것들은 Receipt 의 정체성이 아니다. 매입 참조로 찾으면 회차가 여럿인 매입
       하나에 여러 입고가 달릴 때 남의 건을 자기 것으로 본다.

    ★ **`ALREADY_EXISTS` 는 실패가 아니다.** `ledger.record_inventory_move` 가 같은
      `move_id` 를 다시 받았을 때 `applied=False` 로 돌려주는 것과 같은 자리다 —
      멱등 재실행은 정상이고, 그 사실을 예외로 표현하면 바깥이 롤백하게 된다.

    ⚠️ **찾은 Receipt 를 고치지 않는다.** 일정(`in_transit` · `confirmed_inbound`)도
       건드리지 않는다. 이 함수는 아무것도 쓰지 않는다.

    🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** 받은 `conn` 만 쓴다 —
       나중에 Receipt·검수·Lot·원장 IN·일정 정리가 **한 바깥 트랜잭션**으로 묶여야
       하고, 그 커밋은 호출자가 한 번 한다.

    :param conn: 호출자가 소유한 커넥션. 이 함수는 수명을 관리하지 않는다.
    :param sim_run_id: 어느 실행의 장부인가. **마스터가 정하는 값**이다.
    :param inbound_id: 물류 입고 정체성(`INB-{approval_id}-{seq}`). 비어 있으면 안 된다.
    :raises InvalidInboundIdentity: `inbound_id` 가 비었거나 공백뿐일 때.
    :raises ReceiptIntegrityError: 같은 열쇠에 Receipt 가 둘 이상일 때.
    """
    # ★ **DB 에 묻기 전에 막는다.** `inbound_id IS NULL` 이나 빈 문자열로 조회하면
    #   0건이 돌아오고, 그 0건은 "아직 없다" 로 읽힌다 (`InvalidInboundIdentity`).
    if not inbound_id or not inbound_id.strip():
        raise InvalidInboundIdentity(
            f"Receipt 조회에 쓸 수 없는 inbound_id 다: {inbound_id!r}"
            f" (sim_run_id={sim_run_id!r})."
            " 없는 열쇠로 물으면 0건이 돌아오고 그것은 '아직 Receipt 가 없다' 로"
            " 읽힌다 — 없는 것과 물어보지 못한 것은 다른 사실이다."
        )
    if not sim_run_id or not sim_run_id.strip():
        # ★ 같은 이유다. 열쇠는 두 값이 함께여야 유일성 축이 된다.
        raise InvalidInboundIdentity(
            f"Receipt 조회에 쓸 수 없는 sim_run_id 다: {sim_run_id!r}"
            f" (inbound_id={inbound_id!r}). 어느 실행의 장부인지 없이 물을 수 없다."
        )

    schema = sql.Identifier(get_db_schema())
    # ★ `ORDER BY receipt_id` 는 **깨진 경우의 메시지를 결정적으로** 만든다.
    #   같은 손상 상태를 두 번 조회하면 같은 두 id 가 같은 순서로 나온다.
    query = sql.SQL(
        """
        SELECT receipt_id
        FROM {}.inbound_receipts
        WHERE sim_run_id = %s
          AND inbound_id = %s
        ORDER BY receipt_id
        LIMIT {}
        """
    ).format(schema, sql.Literal(_CORRUPTION_PROBE_LIMIT))

    with conn.cursor() as cursor:
        cursor.execute(query, (sim_run_id, inbound_id))
        # 🔴 **`fetchone()` 을 쓰지 않는다.** 그것은 2건 이상을 **조용히 첫 행으로**
        #    돌려준다 — 무결성 위반이 정상 응답으로 나가는 자리가 정확히 거기다.
        rows = cursor.fetchall()

    if not rows:
        return ReceiptExistence(status="NEW", receipt_id=None)

    receipt_ids = [_cell(row, 0, "receipt_id") for row in rows]
    if len(receipt_ids) > 1:
        raise ReceiptIntegrityError(
            f"같은 입고 건에 Receipt 가 둘 이상이다"
            f" (sim_run_id={sim_run_id!r}, inbound_id={inbound_id!r}):"
            f" {receipt_ids!r} …."
            " 어느 것이 진짜인지 여기서 고르지 않는다 — 최신도 첫 행도 고르지 않고,"
            " 조용히 합치지도 않는다."
        )

    return ReceiptExistence(status="ALREADY_EXISTS", receipt_id=receipt_ids[0])
