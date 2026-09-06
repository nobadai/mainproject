"""receipts.py — 입고 Receipt 행의 **존재와 현재 상태**를 묻는다 (3-B4-C · 3-B4-F).

```text
arrival.select_due_inbound   →  due 행    물류가 아는 사실만으로 자격이 있나
receipts.check_receipt_state →  NEW                이 입고에 Receipt 행이 아직 없다
                                ALREADY_EXISTS     Receipt 행이 이미 존재한다
                                + receipt_status   그 행이 지금 어느 상태인가
```

🔴 **`ALREADY_EXISTS` 를 *"입고 처리가 끝났다"* 로 읽으면 안 된다.**

   ```text
   Receipt 가 ARRIVED 로 있다
   검수 없음 · Lot 없음 · 원장 IN 없음
   ⇒ 행은 있지만 **처리는 끝나지 않았다**
   ```

   그 상태로 건너뛰면 **Receipt 만 남고 재고가 안 들어온 채 영구 고착된다.**
   그래서 3-B4-F 에서 `receipt_status` 를 함께 내보낸다 — 뒤 단계가 가를 수 있게.

⚠️ **어느 상태에서 무엇으로 이어갈지는 여기서 정하지 않는다.** 그 상태기계는 별도
   감사가 정한다 (스키마에 전이 규칙이 없고 씨앗 행도 0건이라 지금 근거로는 증명할
   수 없다 — 3-B4-D 감사 K 항목). 이 파일은 *"실제 상태가 무엇인가"* 만 답한다.

★ **`arrival.py` 와 나눈 이유가 있다.** 저쪽은 **순수 계산**이고 그 순수성을
  테스트가 잠그고 있다(임포트 목록까지 고정한다). 이 파일은 DB 를 읽으므로 섞으면
  그 방어가 통째로 무너진다 — 분류는 저쪽, 조회는 이쪽이다.

🔴 **`repository.py` 에 두지 않았다.** 그쪽은 `db.fetch_all` 로 **자기 커넥션을 연다.**
   이 조회는 나중에 Receipt·검수·Lot·원장 IN 과 **한 트랜잭션**에 들어가야 해서,
   호출자가 준 커넥션만 써야 한다 (`ledger.py` · `transition.persist_inventory` 가
   같은 이유로 `repository` 를 안 쓴다).

🟢 **3-B4-G 부터 이 파일이 Receipt 를 만든다.** 쓰기는 딱 하나뿐이다 —
   `ARRIVED` 행 INSERT. **UPDATE · DELETE 는 없고 앞으로도 이 단계에 없다.**

   ```text
   check_receipt_state    읽기 전용 — 계속 아무것도 안 쓴다
   create_arrived_receipt 잠금 → 재조회 → NEW 일 때만 INSERT 한 번
   ```

⚠️ **읽기만으로는 동시 중복 생성을 막지 못한다 — 그래서 잠금을 붙였다.**

   ```text
   T1  조회 → 0건 (NEW)
   T2  조회 → 0건 (NEW)      ← 둘 다 "새 건" 으로 본다
   T1  INSERT
   T2  INSERT                 여기서야 부딪힌다
   ```

   🟢 **`create_arrived_receipt` 가 그 틈을 닫는다** — 도착 쓰기 **전역** advisory
      xact lock 을 먼저 잡고 **잠금 안에서 다시 조회한다.** 잠금 밖에서 이미 한
      조회를 믿지 않는다.

   🔴 **입고별 잠금을 쓰지 않는다.** `ledger._lock_ledger_writes` 가 적어 둔 교착이
      그대로 재현되기 때문이다 — 한 트랜잭션이 여러 입고를 처리하면 두 트랜잭션이
      **요청하는 잠금 집합 자체가 달라** 전순서를 매길 수 없다. 전역 하나로 합치면
      기다리는 쪽이 아직 아무 자원도 안 쥐고 있어 순환이 생길 자리가 없다.

   ⚠️ **UniqueViolation 을 정상 흐름으로 쓰지 않는다** (`ledger.py` 와 같은 규율).
      DB 무결성 예외는 트랜잭션을 aborted 로 만들어 바깥이 롤백할 수밖에 없게 한다 —
      *"이미 있으니 넘어간다"* 를 그것으로 표현하면 멀쩡한 재실행이 장애가 된다.
      `uq_inbound_receipts_inbound_id` 는 **최종 안전망**으로 남는다. 잠금이 있는데도
      그 그물이 터지면 그것은 버그이므로 **삼키지 않고 그대로 올린다.**

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
from typing import Any, Literal, get_args

from psycopg import sql

from app.logistics.arrival import DueInbound
from app.logistics.db import get_db_schema
from app.logistics.purchase_detail import PurchaseDetail

__all__ = [
    "InvalidInboundIdentity",
    "ReceiptExistence",
    "ReceiptExistenceStatus",
    "ReceiptFactsMissing",
    "ReceiptIntegrityError",
    "ReceiptLookupError",
    "ReceiptRowUnreadable",
    "ReceiptStatus",
    "ReceiptWriteResult",
    "check_receipt_state",
    "create_arrived_receipt",
    "lock_arrival_writes",
    "receipt_id_for",
]


#: 조회가 낸 답. **둘 다 정상이다** — 실패가 아니다.
#:
#: ```text
#: NEW             이 입고에 Receipt 행이 아직 없다. 뒤 단계가 만들 수 있다
#: ALREADY_EXISTS  이 입고에 Receipt 행이 **이미 존재한다**
#: ```
#:
#: 🔴 **`ALREADY_EXISTS` 는 "입고 처리가 끝났다" 가 아니다.** Receipt 행 하나가
#:    있다는 사실일 뿐이고, 검수·Lot·원장 IN 이 아직 없을 수 있다. 그래서 이 축과
#:    별개로 `receipt_status` 를 함께 돌려준다 (`ReceiptExistence` 참조).
ReceiptExistenceStatus = Literal["NEW", "ALREADY_EXISTS"]

#: `inbound_receipts.receipt_status` 의 **DB CHECK 어휘 그대로다.**
#:
#: ```sql
#: ck_inbound_receipts_status
#:   CHECK (receipt_status IN ('ARRIVED','INSPECTING','INSPECTED','PUTAWAY_DONE','CLOSED'))
#: ```
#:
#: 🔴 **새 상태를 만들지 않는다.** 여기 없는 값이 DB 에서 나오면 그것은 우리가 모르는
#:    상태이고, 아는 값으로 바꿔 읽으면 그 순간 모르는 것을 아는 척하게 된다.
ReceiptStatus = Literal["ARRIVED", "INSPECTING", "INSPECTED", "PUTAWAY_DONE", "CLOSED"]

#: 값은 타입에서 **파생한다** — 어휘가 한 번만 적히게 하려는 것이다
#: (`schemas.POLICY_VERSION` 이 같은 이유로 `get_args` 를 쓴다). 둘을 나란히 적으면
#: 어휘가 바뀌는 날 두 줄을 함께 고쳐야 한다.
_RECEIPT_STATUSES: frozenset[str] = frozenset(get_args(ReceiptStatus))

#: 🔴 **한 번에 둘까지만 읽는다.** 셋을 가르는 데 그 이상이 필요 없다.
#:
#: ★ 정확한 개수를 세도 **결정이 달라지지 않는다** — 2건이든 5건이든 우리는 어느
#:   하나도 고르지 않고 멈춘다. 그래서 개수 대신 **부딪힌 두 `receipt_id`** 를
#:   남긴다. 조사에 쓸모 있는 쪽은 그쪽이다.
_CORRUPTION_PROBE_LIMIT = 2

#: 🔴 **도착 쓰기를 하나의 전역 잠금으로 직렬화한다.** `ledger._lock_ledger_writes` 와
#: 같은 판단이고, 같은 `classid` 에 다른 `objid` 를 쓴다.
#:
#: ```text
#: (20260905, 1)  재고 원장 쓰기   ledger.py   ← 실측 확인, 저장소의 유일한 다른 키
#: (20260905, 2)  도착 Receipt 쓰기 이 파일
#: ```
#:
#: ★ **입고별 키를 쓰지 않는다.** 한 트랜잭션이 여러 입고를 처리하면 두 트랜잭션이
#:   요청하는 잠금 **집합 자체가 달라** 전순서를 매길 수 없고, 그것이 `ledger` 가
#:   겪은 교착의 뿌리다 (`_lock_ledger_writes` docstring 에 그 시나리오가 있다).
_ARRIVAL_LOCK_CLASSID = 20260905
_ARRIVAL_LOCK_OBJID = 2

#: 🔴 **도착 시점의 유일한 상태다.** `ck_inbound_receipts_status` 어휘의 첫 값이고,
#: 뒤 넷은 검수·적치 단계의 값이라 여기서 쓰면 **하지 않은 일을 적는 것**이 된다.
_STATUS_ON_ARRIVAL: ReceiptStatus = "ARRIVED"

#: 🔴 **창고에서 사람이 확인한 것이 아니다.** `ck_inbound_receipts_fact_source` 는
#: `HUMAN_RECORDED · SCENARIO_SIMULATED` 둘뿐이고, 자동 시뮬레이션 처리에
#: `HUMAN_RECORDED` 를 쓰면 **거짓을 적는 것**이다.
#:
#: ★ 이 값이 *"`arrived_at` 은 관측된 물리 도착이 아니라 계획된 예정일"* 이라는
#:   사실을 이미 기록한다 — 그래서 그 설명을 `note` 에 또 적지 않는다.
_FACT_SOURCE_SIMULATED = "SCENARIO_SIMULATED"


class ReceiptLookupError(RuntimeError):
    """이 모듈이 내는 실패의 조상.

    ★ `ledger.InventoryLedgerError` 와 같은 결이다 — 호출자가 *"입고 Receipt 처리가
      실패했다"* 를 한 번에 잡을 수 있게 두되, 종류는 아래에서 갈라 둔다.

    ⚠️ 이름이 `Lookup` 인 것은 이 파일이 조회만 하던 시절(3-B4-C)의 흔적이다.
       3-B4-G 부터 쓰기 실패도 이 아래로 온다 — 이름을 바꾸면 이미 이 이름을 잡는
       코드가 조용히 안 잡히게 되므로 **그대로 둔다.**
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


class ReceiptRowUnreadable(ReceiptLookupError, ValueError):
    """Receipt 행은 있는데 그 값을 계약대로 읽을 수 없다. 무결성 위반이다.

    🔴 **모르는 상태를 아는 값으로 바꿔 읽지 않는다.** DB CHECK 밖의
       `receipt_status` 가 나왔다면 그 행은 우리가 모르는 상태이고,
       `ARRIVED` 로 대신 읽으면 **아직 안 한 일을 안 했다고 다시 적게 된다** —
       검수·Lot 이 이미 있는 행을 처음부터 다시 도는 자리가 정확히 거기다.

    ⚠️ `ck_inbound_receipts_status` 가 있으면 원래 못 생기는 상태다. 그래도 검사하는
       이유는 **제약이 아직 안 적용된 DB 도 있을 수 있어서**이고,
       `ReceiptIntegrityError` 가 중복에 대해 같은 이유로 존재하는 것과 같다.
    """


class ReceiptFactsMissing(ReceiptLookupError, ValueError):
    """Receipt 를 쓰려는데 **권위 있는 매입 사실이 비어 있다.**

    🔴 **`purchase_item_id=NULL` 로 Receipt 를 만들지 않는다.** 그러면 다음 실행이
       `ALREADY_EXISTS` 를 보고 **그 입고를 영영 건너뛴다** — Receipt 만 남고 재고가
       안 들어온 채 고착되고, 나중에 매입 참조가 생겨도 그 행을 보강할 경로가
       저장소에 없다 (3-B4-D 감사 F 항목).

    ★ 상류가 이미 막는다 — `arrival.select_due_inbound` 이 참조 없는 행을
      `ARRIVAL_PURCHASE_REFERENCE_MISSING` 으로 `blocked` 에 둔다. 여기서 다시 보는
      것은 **이 함수가 그 경로 밖에서도 안전해야** 하기 때문이다.
    """


@dataclass(frozen=True)
class ReceiptExistence:
    """조회 결과. **작게 둔다.**

    ★ `arrived_at` · 수량들 · `fact_source` 는 싣지 않는다 — 지금 쓰지 않는 값이고,
      실어 두면 뒤 단계가 **여기서 읽은 낡은 값**을 쓰게 된다.

    🔴 **`receipt_status` 는 예외다. 이것은 실어야 한다.**
       `ALREADY_EXISTS` 만으로는 *"이 입고를 더 볼 필요가 없다"* 와 구별이 안 된다 —

    ```text
    Receipt 가 ARRIVED 로 있다
    검수 없음 · Lot 없음 · 원장 IN 없음
    ⇒ 행은 있지만 **입고 처리는 끝나지 않았다**
    ```

       그 상태로 건너뛰면 Receipt 만 남고 재고가 안 들어온 채 영구 고착된다.
       뒤 단계가 그것을 가르려면 **지금 그 사실이 나가 있어야** 한다.

    ⚠️ **여기서 진행 여부를 정하지는 않는다.** 어느 상태에서 무엇으로 이어갈지는
       별도의 상태기계 감사가 정한다 — 이 판은 *"실제 상태가 무엇인가"* 만 답한다.
    """

    status: ReceiptExistenceStatus
    #: `ALREADY_EXISTS` 면 그 행의 권위 있는 `receipt_id`, `NEW` 면 `None`.
    receipt_id: str | None
    #: `ALREADY_EXISTS` 면 DB 에 적힌 `receipt_status` 그대로, `NEW` 면 `None`.
    #:
    #: 🔴 **기본값을 두지 않는다.** `NEW` 일 때 `"ARRIVED"` 를 넣으면 *"아직 없다"* 와
    #:    *"막 도착했다"* 가 같은 값이 된다.
    receipt_status: ReceiptStatus | None


@dataclass(frozen=True)
class ReceiptWriteResult:
    """`create_arrived_receipt` 의 결과. **작게 둔다.**

    🔴 **`applied=False` 는 "입고 처리가 끝났다" 가 아니다.** 뜻은 하나뿐이다 —
       *"이 호출이 새 Receipt 를 만들지 않았다. 행이 이미 있었기 때문이다."*
       그 행이 `ARRIVED` 인데 검수·Lot·원장 IN 이 없을 수 있고, 그때 건너뛰면
       재고가 안 들어온 채 고착된다.

    ★ `ledger.LedgerResult.applied` 와 같은 뜻이다 — 멱등 재실행에서 *"이번에 실제로
      바꿨나"* 를 답하는 축이지, *"할 일이 남았나"* 가 아니다.
    """

    #: 이번 호출이 Receipt 행을 **새로 만들었나.**
    applied: bool
    #: 새로 만들었으면 그 id, 이미 있었으면 **DB 에 적힌 권위 있는 id.**
    receipt_id: str
    #: 새로 만들었으면 `ARRIVED`, 이미 있었으면 **그 행의 현재 상태 그대로.**
    #:
    #: 🔴 **상태를 진행시키지 않는다.** 이 함수는 `ARRIVED` 를 만들 뿐이고, 어느
    #:    상태에서 무엇으로 이어갈지는 별도 상태기계 감사가 정한다.
    receipt_status: ReceiptStatus


def receipt_id_for(*, sim_run_id: str, inbound_id: str) -> str:
    """Receipt 행의 PK. **순수 계산이고 결정론이다.**

    ```text
    RCPT-{sim_run_id}-{inbound_id}
    → RCPT-SIM-BURNIN-202512-INB-H1-THRU-20260105-BAECHU-1-1
    ```

    🔴 **난수 · 시계 · DB 시퀀스를 쓰지 않는다.** 같은 `(sim_run_id, inbound_id)` 는
       몇 번을 불러도 같은 값이어야 재시도가 멱등해진다 — 마스터 `purchase_id_for` ·
       물류 `inbound_id` 가 같은 이유로 결정론이다.

    🔴 **`sim_run_id` 를 반드시 담는다.** PK 는 `receipt_id` **단독**인데 유일성 축은
       `(sim_run_id, inbound_id)` 다. 즉 스키마가 *"같은 `inbound_id` 가 다른 실행에
       있는 것은 합법"* 이라고 선언하고 있어서, `inbound_id` 만으로 지으면 두 실행의
       같은 입고가 **PK 에서 충돌한다.**

    ★ **`receipt_id` 는 정체성이 아니라 그 정체성의 행 표현이다.** 정체성은 여전히
      `(sim_run_id, inbound_id)` 이고 조회도 그 축으로 한다.

    🔴 **매입 ID 에서 유도하지 않는다.** `purchase_id` 를 뜯거나 접두사를 떼어 붙이지
       않는다 — 그 값의 주인은 마스터이고, 물류가 그 모양에 기대면 마스터가 형식을
       바꾸는 날 조용히 어긋난다.

    :raises InvalidInboundIdentity: 둘 중 하나라도 비었거나 공백뿐일 때.
    """
    if not sim_run_id or not sim_run_id.strip():
        raise InvalidInboundIdentity(
            f"receipt_id 를 지을 수 없다 — sim_run_id 가 비었다: {sim_run_id!r}"
            f" (inbound_id={inbound_id!r})."
        )
    if not inbound_id or not inbound_id.strip():
        raise InvalidInboundIdentity(
            f"receipt_id 를 지을 수 없다 — inbound_id 가 비었다: {inbound_id!r}"
            f" (sim_run_id={sim_run_id!r})."
        )
    return f"RCPT-{sim_run_id}-{inbound_id}"


def lock_arrival_writes(cursor: Any) -> None:
    """도착 Receipt 쓰기를 **하나의 전역 잠금으로 직렬화한다.**

    🔴 **이 잠금이 read-before-write 의 틈을 닫는다.**

    ```text
    잠금 없이   T1 조회 0건 · T2 조회 0건 → 둘 다 INSERT 시도
    잠금 있으면 T2 는 T1 의 커밋/롤백까지 기다렸다가 **다시 조회한다**
    ```

    ★ **transaction-level(`_xact_`) 이다.** 바깥 트랜잭션의 커밋/롤백과 함께 자동으로
      풀린다 — 이 모듈은 unlock 을 부르지 않고, 부를 수도 없어야 한다. session-level
      을 쓰면 풀어 줄 주인이 없어 커넥션에 잠금이 눌어붙는다.

    ★ **같은 트랜잭션 안에서는 재진입한다.** 한 트랜잭션이 여러 입고를 처리해도 두
      번째 호출이 스스로 멈추지 않는다.

    🔴 **잠금 순서 계약** (`ledger.py` 와 함께 지켜야 한다):

    ```text
    ① 도착 전역 (20260905, 2)   ← 이 잠금. 가장 먼저
    ② fixture 행 FOR UPDATE      일정 정리 단계에서 (아직 이 판에 없다)
    ③ 원장 전역 (20260905, 1)    record_inventory_move 안에서
    ④ Lot 행 FOR UPDATE          〃
    ```

       ⚠️ **원장 전역을 fixture 행보다 먼저 잡는 경로를 만들면 안 된다.** 지금 그런
          경로는 없고, 그 규칙이 이 전순서를 성립시킨다.

    ★ **커넥션을 새로 열지도, 커밋하지도 않는다.** 호출자가 준 커서로 건다.

    ⚠️ **대가는 도착 처리의 직렬화다.** 서로 다른 입고도 한 줄로 선다 — MVP 에서
       받아들인 거래이고, `ledger` 가 같은 거래를 이미 받아들였다.
    """
    cursor.execute(
        sql.SQL("SELECT pg_advisory_xact_lock(%s, %s)"),
        (_ARRIVAL_LOCK_CLASSID, _ARRIVAL_LOCK_OBJID),
    )


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
    0건      NEW              Receipt 행이 아직 없다 — 뒤 단계가 만들 수 있다
                              receipt_id=None · receipt_status=None
    1건      ALREADY_EXISTS   Receipt 행이 **이미 존재한다** — 멱등 재실행이다
                              receipt_id · receipt_status 를 DB 값 그대로 싣는다
    2건 이상  ReceiptIntegrityError
    ```

    🔴 **`ALREADY_EXISTS` 는 "입고 처리가 끝났다" 가 아니다.** Receipt 행 하나가
       있다는 사실뿐이고, 그 행이 `ARRIVED` 인데 검수·Lot·원장 IN 이 없을 수 있다.
       그래서 `receipt_status` 를 함께 돌려준다 — 뒤 단계가 그것을 보고 갈라야 한다.

    ⚠️ **여기서 진행 여부를 정하지 않는다.** 어느 상태에서 무엇으로 이어갈지는
       별도의 상태기계 감사가 정한다. 이 함수는 *"실제 상태가 무엇인가"* 만 답한다.

    🔴 **조회 열쇠는 `(sim_run_id, inbound_id)` 다.** DB 의 유일성 축과 **같아야**
       한다 — 다른 축으로 물으면 *"있다/없다"* 와 *"두 번 못 선다"* 가 서로 다른
       것을 뜻하게 된다.

    🔴 **`purchase_id` · `approval_id` · 품목명 · 도착예정일로 찾지 않는다.**
       그것들은 Receipt 의 정체성이 아니다. 매입 참조로 찾으면 회차가 여럿인 매입
       하나에 여러 입고가 달릴 때 남의 건을 자기 것으로 본다.

    ★ **`ALREADY_EXISTS` 는 실패가 아니다.** `ledger.record_inventory_move` 가 같은
      `move_id` 를 다시 받았을 때 `applied=False` 로 돌려주는 것과 같은 자리다 —
      멱등 재실행은 정상이고, 그 사실을 예외로 표현하면 바깥이 롤백하게 된다.
      **행이 있다는 이유만으로 예외를 올리지 않는다.**

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
    :raises ReceiptRowUnreadable: 찾은 행의 `receipt_id` 가 비었거나
        `receipt_status` 가 DB CHECK 어휘 밖일 때. **대체값으로 읽지 않는다.**
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
        SELECT receipt_id, receipt_status
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
        # 🔴 세 칸을 **함께** 비운다. `NEW` 에 `"ARRIVED"` 를 얹으면 *"아직 없다"* 와
        #    *"막 도착했다"* 가 같은 값이 된다.
        return ReceiptExistence(status="NEW", receipt_id=None, receipt_status=None)

    receipt_ids = [_cell(row, 0, "receipt_id") for row in rows]
    if len(receipt_ids) > 1:
        raise ReceiptIntegrityError(
            f"같은 입고 건에 Receipt 가 둘 이상이다"
            f" (sim_run_id={sim_run_id!r}, inbound_id={inbound_id!r}):"
            f" {receipt_ids!r} …."
            " 어느 것이 진짜인지 여기서 고르지 않는다 — 최신도 첫 행도 고르지 않고,"
            " 조용히 합치지도 않는다."
        )

    receipt_id = receipt_ids[0]
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        # ★ `receipt_id` 는 PK · NOT NULL 이다. 비어 오면 읽은 것이 그 행이 아니다.
        raise ReceiptRowUnreadable(
            f"Receipt 행의 receipt_id 를 읽을 수 없다: {receipt_id!r}"
            f" (sim_run_id={sim_run_id!r}, inbound_id={inbound_id!r})."
        )

    receipt_status = _cell(rows[0], 1, "receipt_status")
    if receipt_status not in _RECEIPT_STATUSES:
        # 🔴 **`ARRIVED` 로 대신 읽지 않는다.** 모르는 상태를 아는 값으로 바꾸면
        #    검수·Lot 이 이미 있는 행을 처음부터 다시 돌게 된다.
        raise ReceiptRowUnreadable(
            f"Receipt 행의 receipt_status 가 계약 어휘 밖이다: {receipt_status!r}"
            f" (receipt_id={receipt_id!r}, sim_run_id={sim_run_id!r},"
            f" inbound_id={inbound_id!r}). 허용: {sorted(_RECEIPT_STATUSES)}."
            " 아는 값으로 바꿔 읽지 않는다 — 모르는 상태를 아는 척하게 된다."
        )

    return ReceiptExistence(
        status="ALREADY_EXISTS",
        receipt_id=receipt_id,
        # ★ **DB 에 적힌 값 그대로다.** 여기서 진행 여부를 판단하지 않는다.
        receipt_status=receipt_status,
    )


def create_arrived_receipt(
    conn: Any,
    *,
    sim_run_id: str,
    inbound: DueInbound,
    purchase_detail: PurchaseDetail,
) -> ReceiptWriteResult:
    """도착한 입고 한 건을 `ARRIVED` Receipt 로 적는다. **멱등하다.**

    ```text
    ① 입력 검증                     DB 를 안 만진다
    ② 도착 쓰기 전역 advisory lock   여기서부터 이 입고를 다루는 것은 나 하나다
    ③ 잠금 안에서 **다시** 조회      ★ 잠금 밖의 조회를 믿지 않는다
       ├ 있음  applied=False, DB 값 그대로 → INSERT 없음
       └ 없음  ↓
    ④ INSERT (ARRIVED · SCENARIO_SIMULATED)
    ```

    🔴 **③ 이 ② 뒤인 것이 이 함수의 동시성 계약이다.** 호출자가 앞서
       `check_receipt_state` 를 불렀더라도 그 답은 잠금 **밖**의 사실이라 이미
       낡았을 수 있다. 다시 묻지 않으면 두 트랜잭션이 함께 *"새 건"* 을 보고 둘 다
       INSERT 로 간다.

    ★ **읽는 사실은 전부 앞 단계가 검증한 것이다.** 여기서 매입을 조회하지도,
      `purchase_id` 를 뜯지도, 품목명을 번역하지도, 단가를 계산하지도 않는다 —
      `DueInbound` 와 `PurchaseDetail` 이 이미 그 일을 마쳤다.

    🔴 **`arrived_at = inbound.expected_arrival_date` 다.**
       `date.today()` · `as_of` · `CURRENT_DATE` · `created_at` 을 쓰지 않는다.

    ```text
    expected_arrival_date = 2026-01-05
    처리 as_of             = 2026-01-07
    arrived_at            = 2026-01-05     ★ 연체분도 원래 예정일을 지킨다
    ```

       ⚠️ **이것은 "그날 물리적으로 도착한 것을 관측했다" 는 주장이 아니다.**
          `SCENARIO_SIMULATED` 에는 별도의 실제 도착일 원천이 없어, 계획된 예정일을
          **모의 도착일로 쓴다.** 그 사실은 `fact_source` 가 이미 기록한다.
          `as_of` 로 옮기면 그 로트가 이틀 더 신선한 것처럼 보이고, 신선도는 폐기·판매
          판단으로 흘러간다.

    ⚠️ **일정 수량과 매입 수량을 대조하지 않는다 (3-B4-E 결론).** 두 값은 같은
       `leg.qty_kg` 에서 오지만 가공이 다르다 — 매입은 6자리로 quantize 하고 물류는
       원값을 유지해서, 소수 6자리를 넘으면 **정당하게 달라진다.** 임의의 허용오차를
       지어내지 않고, `ordered_qty_kg` 는 **권위 있는 매입 사실**을 그대로 쓴다.
       대조 규칙은 나중의 통합 불변식으로 남긴다.

    🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** 잠금도 트랜잭션 수명이라
       호출자의 커밋/롤백과 함께 풀린다.

    ⚠️ **일정(`in_transit` · `confirmed_inbound`)을 건드리지 않는다.** 그래서 fixture
       행을 `FOR UPDATE` 로 잡지도 않는다 — 이 판은 일정을 바꾸지 않는다.

    :param conn: 호출자가 소유한 커넥션. 이 함수는 수명을 관리하지 않는다.
    :param sim_run_id: 어느 실행의 장부인가. **마스터가 정하는 값**이다.
    :param inbound: `arrival.select_due_inbound` 이 `due` 로 가른 행.
    :param purchase_detail: `purchase_detail.fetch_purchase_detail` 이 읽은 매입 줄.
    :raises InvalidInboundIdentity: 식별자가 비었거나 공백뿐일 때.
    :raises ReceiptFactsMissing: 매입 상세의 필수 값이 비었을 때.
    :raises ReceiptIntegrityError: 같은 열쇠에 Receipt 가 둘 이상일 때.
    :raises ReceiptRowUnreadable: 기존 행의 값을 계약대로 읽을 수 없을 때.
    """
    # ── ① 입력 검증 — DB 를 만나기 전에 끝낸다 ──────────────────────────
    receipt_id = receipt_id_for(sim_run_id=sim_run_id, inbound_id=inbound.inbound_id)
    if not purchase_detail.purchase_item_id or not purchase_detail.purchase_item_id.strip():
        raise ReceiptFactsMissing(
            f"매입 상세에 purchase_item_id 가 없다 (inbound_id={inbound.inbound_id!r})."
            " NULL 로 Receipt 를 만들면 다음 실행이 ALREADY_EXISTS 를 보고 이 입고를"
            " 영영 건너뛴다 — 보강할 경로가 없다."
        )
    if not purchase_detail.item_id or not purchase_detail.item_id.strip():
        raise ReceiptFactsMissing(
            f"매입 상세에 item_id 가 없다 (inbound_id={inbound.inbound_id!r})."
            " inbound_receipts.item_id 는 NOT NULL 이고, 품목명으로 따로 번역해"
            " 채우지 않는다 — 그러면 purchase_item_id 와 다른 품목을 가리킬 수 있다."
        )

    schema = sql.Identifier(get_db_schema())
    # ★ nullable 칸을 **아예 안 적는다.** 값을 지어내는 대신 DB 기본값(NULL)에 맡긴다.
    #   accepted/hold/rejected 수량 · receiving_location_id · 팔레트 수 · received_by
    #   는 검수·적치 단계의 사실이고, `created_at`/`updated_at` 은 DB DEFAULT 다.
    insert_query = sql.SQL(
        """
        INSERT INTO {}.inbound_receipts (
            receipt_id, sim_run_id, inbound_id, purchase_item_id, item_id,
            arrived_at, ordered_qty_kg, receipt_status, fact_source
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
    ).format(schema)

    with conn.cursor() as cursor:
        # ── ② 잠금이 먼저다 ────────────────────────────────────────────
        lock_arrival_writes(cursor)

    # ── ③ 잠금 안에서 다시 묻는다 ──────────────────────────────────────
    existing = check_receipt_state(conn, sim_run_id=sim_run_id, inbound_id=inbound.inbound_id)
    if existing.status == "ALREADY_EXISTS":
        # ★ 타입 좁히기 — `ALREADY_EXISTS` 면 두 값이 다 있다 (`check_receipt_state`
        #   이 비거나 어휘 밖인 값을 이미 막는다).
        assert existing.receipt_id is not None
        assert existing.receipt_status is not None
        # 🔴 **고치지 않는다.** 상태를 진행시키지도, 일정을 건드리지도 않는다.
        #    `receipt_id` 는 우리가 지은 값이 아니라 **DB 에 적힌 값**을 돌려준다 —
        #    이 규칙이 생기기 전에 만들어진 행이라도 그 행이 진짜다.
        return ReceiptWriteResult(
            applied=False,
            receipt_id=existing.receipt_id,
            receipt_status=existing.receipt_status,
        )

    # ── ④ 없을 때만 쓴다 ──────────────────────────────────────────────
    with conn.cursor() as cursor:
        cursor.execute(
            insert_query,
            (
                receipt_id,
                sim_run_id,
                inbound.inbound_id,
                purchase_detail.purchase_item_id,
                purchase_detail.item_id,
                # 🔴 예정일 그대로다 — 연체분도 옮기지 않는다.
                inbound.expected_arrival_date,
                # ★ **권위 있는 매입 사실**이다. 일정 수량으로 덮어쓰지 않는다.
                purchase_detail.quantity_kg,
                _STATUS_ON_ARRIVAL,
                _FACT_SOURCE_SIMULATED,
            ),
        )

    return ReceiptWriteResult(
        applied=True,
        receipt_id=receipt_id,
        receipt_status=_STATUS_ON_ARRIVAL,
    )
