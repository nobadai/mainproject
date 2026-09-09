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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.inbound_schedules import cancel_schedule

# 🔴 **B-1 규율을 다시 적지 않고 가져다 쓴다.** 밑줄 이름을 건너 가져오는 것은 이
#    패키지의 기존 방식이다 (`console_service` 가 `outbound._ASSIGNED_ALLOCATION` 을
#    그대로 쓰는 것과 같다 — *"여기에 문자열로 다시 적지 않는다"*).
from app.logistics.inbound_stock import (
    ScheduleIntegrityError,
    _clear_schedule,
    _fixture_row,
    _찾는다,
)
from app.logistics.receipts import lock_arrival_writes
from app.logistics.schemas import InTransitItem
from app.logistics.transition import USAGE_SCOPE

__all__ = [
    "InboundLineageAmbiguous",
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

#: 🔴 **둘까지만 읽는다.** 0 · 1 · 2+ 를 가르는 데 그 이상이 필요 없다
#: (`inbound_stock._AMBIGUITY_PROBE_LIMIT` 과 같은 태도).
_AMBIGUITY_PROBE_LIMIT = 2


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


class InboundLineageAmbiguous(InboundReconciliationError, ValueError):
    """같은 `inbound_id` 에 Receipt 가 **둘 이상**이다. 어느 것도 고르지 않는다.

    🔴 **첫 행을 임의로 집지 않는다.** 둘 중 무엇이 진짜인지는 데이터가 말해 주지
       않고, 골라 버리면 나머지 하나가 조용히 없는 것이 된다.

    ⚠️ **정상 경로로는 설 수 없는 상태다** — `uq_inbound_receipts_inbound_id`
       (`sim_run_id` + `inbound_id`) 가 막는다. 그래도 여기서 세는 이유는 이 함수가
       *"지워도 되나"* 를 묻는 자리이기 때문이다. 제약이 빠진 판이나 손으로 넣은
       행에서 이 상태가 서면, 모르는 채 지우는 것보다 멈추는 것이 맞다.
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

    ★ **`source_ref` 는 fixture 행에 실제로 적힌다** (`logistics_runtime_fixture.
      source_ref`). 걷어낸 뒤 그 행의 근거는 *"누가 왜 이 목록을 이렇게 만들었나"* 이고,
      그것이 바로 이번 정리이기 때문이다.

    ⚠️ **`reason` 은 DB 에 안 적는다.** 담을 칸이 없고(`note` 는 하루 넘김이 쓰는 남의
       칸이다), 칸을 만드는 것은 `database/` 의 일이다 — 없는 자리에 억지로 끼워 넣지
       않는다. 호출자가 자기 감사 기록에 남긴다.

    ★ **걷어낸 사실 셋을 함께 싣는다.** 지운 뒤에는 fixture 어디에도 안 남으므로,
      *"무엇을 지웠나"* 를 답할 수 있는 곳이 이 결과뿐이다. 재실행 no-op 이면 지운 것이
      없어 셋 다 `None` 이다.
    """

    applied: bool
    inbound_id: str
    sim_run_id: str
    as_of: date
    usage_scope: str
    removed: int
    #: 사람이 적은 정리 사유. 빈 값은 애초에 못 들어온다. **DB 에는 안 적힌다.**
    reason: str
    #: 그 판단의 근거 참조 (티켓 · 감사 문서 등). 역시 빈 값을 안 받는다.
    source_ref: str
    #: 걷어낸 일정의 품목. 재실행 no-op 이면 `None`.
    item: str | None = None
    #: 걷어낸 일정의 수량. 재실행 no-op 이면 `None`.
    quantity_kg: Decimal | None = None
    #: 걷어낸 일정의 도착 예정일. 재실행 no-op 이면 `None`.
    expected_arrival_date: date | None = None


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

    ```text
    Receipt 0건    계보 없음      → 걷기 후보
    Receipt 1건    도착 처리 시작  → ScheduleAlreadyMaterialized (Lot·IN 여부는 사유에)
    Receipt 2건+   모호           → InboundLineageAmbiguous  🔴 첫 행을 안 고른다
    ```

       ★ Lot 도 같은 태도다. 한 Receipt 에 Lot 이 여럿이면 그 행들이 **전부** 사유에
         실린다 — 하나만 보여 주고 나머지를 감추지 않는다.

    🔴 **Receipt 가 0건이면 Lot 도 0건이다.** `inventory_lots.inbound_receipt_id` 가
       Receipt 를 FK 로 가리키므로, 붙을 Receipt 가 없으면 붙은 Lot 도 없다 — 그래서
       Receipt 축 하나로 세는 것으로 충분하다.
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
    계보 = [MaterializedInbound(*_row_values(row)) for row in rows]
    # ★ 행 수가 아니라 **Receipt 수**로 센다 — Lot 이 여럿이면 한 Receipt 도 여러 행이다.
    receipt_ids = {한줄.receipt_id for 한줄 in 계보}
    if len(receipt_ids) >= _AMBIGUITY_PROBE_LIMIT:
        raise InboundLineageAmbiguous(
            f"같은 inbound_id 에 Receipt 가 둘 이상이다 (inbound_id={inbound_id!r},"
            f" sim_run_id={sim_run_id!r}): {sorted(receipt_ids)!r}."
            " 어느 것도 고르지 않는다 — 골라 버리면 나머지가 조용히 없는 것이 된다."
        )
    return 계보


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


def _stamp_source_ref(
    conn: Any,
    schema: sql.Identifier,
    *,
    sim_run_id: str,
    as_of: date,
    usage_scope: str,
    source_ref: str,
) -> None:
    """걷어낸 fixture 행의 `source_ref` 를 이번 정리의 근거로 바꿔 적는다.

    🔴 **`_clear_schedule` 이 방금 `FOR UPDATE` 로 잡은 그 행이다.** 행 잠금은
       트랜잭션 수명이라 아직 우리 것이고, 그래서 읽고-고치는 사이가 비어 있지 않다.

    ★ **`_clear_schedule` 을 안 고쳤다.** 그쪽에 `source_ref` 인자를 더하면 입고 실행
      경로(`materialize_inspected_inbound`)의 서명까지 함께 넓어진다 — 이 한 줄을
      여기서 쓰는 편이 기존 코드 변경이 더 작다.

    ⚠️ **재실행 no-op 에는 안 부른다.** 아무것도 안 걷은 호출이 그 행의 근거를 자기
       것으로 덮으면, 실제로 걷은 사람의 근거가 사라진다.

    ⚠️ **덮어쓰기다.** 이 칸은 하나뿐이라 이전 값(씨앗·하루 넘김이 적어 둔 것)은
       남지 않는다 — 그 값을 함께 보관할 자리가 지금 스키마에 없다.

    ★ `updated_at` 은 `_clear_schedule` 이 이미 찍었다. 여기서 다시 안 만진다.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.logistics_runtime_fixture
                SET source_ref = %s
                WHERE sim_run_id = %s AND as_of = %s AND usage_scope = %s
                """
            ).format(schema),
            (source_ref, sim_run_id, as_of, usage_scope),
        )


def _removed_facts(in_transit: Any, inbound_id: str) -> Mapping[str, Any] | None:
    """걷어낼 항목의 **원본 dict** 를 집어 둔다. 판정하지 않는다.

    🔴 **여기서 검증하지 않는다.** 짝·중복·B-1 은 `_clear_schedule` 이 판정하고, 이
       함수는 *"지운 것이 무엇이었나"* 를 결과에 싣기 위해 사본을 쥐고 있을 뿐이다.
       두 곳이 판정하면 두 번째 정본이 생긴다.

    ★ 정확히 1건일 때만 집는다. 0건이면 걷을 것이 없고, 2건 이상이면 어느 것인지
      고르지 않는다 — 둘 다 뒤이어 `_clear_schedule` 이 제 규율로 답한다.
    """
    찾은것 = _찾는다(in_transit, inbound_id)
    return 찾은것[0] if len(찾은것) == 1 else None


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
    ② inbound_id 의 입고 계보 조회   Receipt 0 / 1 / 2+ 를 가른다
    ③ 계보가 있으면 거부              ScheduleAlreadyMaterialized · InboundLineageAmbiguous
    ④ fixture 행 FOR UPDATE · 지울 항목 사본 확보 (판정은 안 한다)
    ⑤ B-1 재검증 + 양쪽 제거          inbound_stock._clear_schedule
    ⑥ 걷었으면 그 행의 source_ref 를 이번 정리로 바꿔 적는다
    ⑦ 같은 일정을 inbound_schedules 에서도 그날부터 닫는다   (W3-1 Dual Write)
    ```

    🔴 **⑦ 이 없으면 두 저장소가 갈린다** (2026-09-09 보정). ⑤ 는 Legacy JSON 두 칸만
       걷고 신규 표를 안 건드렸다 — 그러면 정리한 뒤에도

    ```text
    Legacy JSON        없음        걷었다
    inbound_schedules  살아있음     ← 그대로다
    ```

       가 되어, W3-2 에서 Reader 가 신규 표로 옮겨 가는 순간 **방금 치운 orphan 이
       되살아난다.** 이 함수가 하는 일이 «그 일정을 그날부터 없앤다» 이므로 신규 표의
       `cancelled_as_of` 가 그 사실의 자리다.

    🔴 **나이로 지우지 않는다.** `expected_arrival_date` 가 얼마나 지났는지, 발주 참조가
       비었는지, `ARRIVAL_PURCHASE_REFERENCE_MISSING` 인지를 **조건으로 쓰지 않는다** —
       참조 전달이 늦은 정상 입고와 구별되지 않기 때문이다. 이 함수가 보는 것은 사람이
       지목한 `inbound_id` 하나의 정합성뿐이다.

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
    :raises InboundLineageAmbiguous: 같은 `inbound_id` 에 Receipt 가 둘 이상일 때.
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

    # ── ④ 지울 것의 사본을 먼저 쥔다 (판정은 안 한다) ─────────────────
    #    ★ `_fixture_row` 가 그 행을 `FOR UPDATE` 로 잡는다 — ⑤ 가 같은 잠금 아래
    #      이어서 돌므로 읽기와 쓰기 사이가 비지 않는다.
    in_transit, _ = _fixture_row(
        conn, schema, sim_run_id=sim_run_id, as_of=as_of, usage_scope=usage_scope
    )
    지울것 = _removed_facts(in_transit, inbound_id)

    # ── ⑤ 걷는 규율은 입고 경로의 것을 그대로 쓴다 ────────────────────
    applied = _clear_schedule(
        conn,
        schema,
        sim_run_id=sim_run_id,
        as_of=as_of,
        usage_scope=usage_scope,
        inbound_id=inbound_id,
    )

    # ── ⑥ 걷었을 때만 그 행의 근거를 이번 정리로 바꿔 적는다 ──────────
    사실 = None
    if applied:
        _stamp_source_ref(
            conn,
            schema,
            sim_run_id=sim_run_id,
            as_of=as_of,
            usage_scope=usage_scope,
            source_ref=source_ref,
        )
        # ★ 여기서 파싱한다. ⑤ 를 지났다는 것이 곧 **B-1 을 통과한 항목**이라는 뜻이라,
        #   계약 모델이 터질 자리가 없다 — 앞에서 파싱하면 `_clear_schedule` 이 낼
        #   `ScheduleIntegrityError` 가 pydantic 오류로 바뀌어 나간다.
        사실 = InTransitItem.model_validate(지울것) if 지울것 is not None else None

        # ── ⑦ 신규 표도 같은 트랜잭션에서 닫는다 (W3-1 Dual Write) ────
        #    ★ `applied` 일 때만 부른다 — 멱등 재호출(이미 걷힘)에서는 ⑤ 가
        #      아무것도 안 했으므로 여기서도 손댈 것이 없다.
        #    ⚠️ 이미 같은 날짜로 닫혀 있으면 `cancel_schedule` 이 no-op 이고,
        #       다른 날짜로 닫혀 있으면 `ScheduleCancelConflict` 로 멈춘다.
        cancel_schedule(
            conn, sim_run_id=sim_run_id, inbound_id=inbound_id, cancelled_as_of=as_of
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
        item=None if 사실 is None else 사실.item,
        quantity_kg=None if 사실 is None else 사실.quantity_kg,
        expected_arrival_date=None if 사실 is None else 사실.expected_arrival_date,
    )
