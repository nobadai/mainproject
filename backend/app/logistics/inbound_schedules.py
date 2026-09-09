"""inbound_schedules.py — 입고 예정을 **날짜에 안 묶인 업무 Entity 로** 적는다 (W3-1).

```text
승인   record_schedule   INSERT 1행                     (날짜별 복제 없음)
취소   cancel_schedule   cancelled_as_of UPDATE          (과거는 안 고친다)
조회   load_inbound_schedules  created_as_of <= as_of    (W3-2 Reader 가 재사용)
```

🔴 **이번 판(W3-1)은 정본을 바꾸지 않는다.**

```text
Reader   아직 Legacy JSON (logistics_runtime_fixture 두 칸)
Writer   Legacy JSON + 이 표          ← Dual Write
```

   정본 전환은 W3-2 다. 그래서 이 모듈은 **읽는 쪽을 하나도 안 건드린다** —
   `load_inbound_schedules` 는 대조·검증용으로 먼저 서지만, 그 함수 자체가
   W3-2 Reader 가 쓸 그 함수다 (검증 전용 임시 함수를 만들지 않는다).

🔴 **왜 표를 따로 만드는가 — 날짜별 복제가 사고를 냈다.**

   종전 입고 예정은 `in_transit_json` · `confirmed_inbound_json` 안에 **날짜마다
   복제되어** 살았고, 하루 넘김 carry-forward 가 그것을 유지했다. 그래서 미래 날짜
   행이 **먼저 열려 있으면** 그 행은 나중에 난 승인을 모른 채 굳는다.

   ```text
   2026-01-15 fixture 생성 (in_transit = [])   ← 먼저 열렸다
   2026-01-14 승인 → 01-14 행에만 기록
   2026-01-15 도착 조회 → 볼 것이 없다
   ⇒ Receipt 0 · Lot 0 · IN Move 0
   ```

   실측(2026-09-09) `INB-H1-REQ-FIRSTINB-20260113-1-1` 이 그 상태이고
   `payables … OPEN 3,066,885원` 이 그 채무를 들고 있다. 전방 전파
   (`master.day_opening_repository.opened_days_after`)가 그날을 못 본 이유는
   `master_day_openings` 에 2026-01-10 ~ 01-19 가 **한 행도 없어서**다.

   ⇒ 이 표는 **한 번 INSERT 하고 날짜로 질의한다.** 미래 날짜 행을 만들지도 고치지도
     않으므로 같은 사고가 구조적으로 재현되지 않는다.

🔴 **완료 컬럼이 없다. `status` 컬럼도 없다.**

   ```text
   완료      downstream 사실로 유도 (Receipt · Lot · IN Move)   소비자마다 다르다
   취소      cancelled_as_of 한 칸                              모순 조합이 없다
   ```

   Receipt 생성과 재고 반영 완료는 **다른 사건**이다 — `inbound_execution._receive_one`
   은 검수 사실이 없으면 `INSPECTION_FACT_UNAVAILABLE` 로 돌아서고, 그때 Receipt 만
   선 채 커밋된다. 그 상태가 며칠 이어질 수 있어 하나를 골라 `COMPLETED` 로 적으면
   나머지 소비자가 틀린다 (소비자별 종료조건은 W3-2 에서 구현한다).

🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** Legacy JSON 쓰기와
   **같은 `conn` · 같은 바깥 트랜잭션**이어야 한다 — 한쪽만 커밋되면 두 정본 후보가
   갈린 채 남는다 (`transition.persist_inventory` 와 같은 규율).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema

__all__ = [
    "InboundSchedule",
    "ScheduleCancelConflict",
    "ScheduleConflict",
    "ScheduleReceiptExists",
    "ScheduleReferenceMissing",
    "cancel_schedule",
    "load_inbound_schedules",
    "record_schedule",
]


class ScheduleConflict(ValueError):
    """같은 `(sim_run_id, inbound_id)` 가 **다른 사실**로 이미 있다. 무결성 위반이다.

    🔴 **기존 행을 UPDATE 해서 맞추지 않는다.** 어느 쪽이 진짜인지 여기서 고를 근거가
       없고, 고치면 그 순간 과거 사실이 조용히 바뀐다
       (`transition.InboundScheduleConflict` 와 같은 규율).
    """


class ScheduleCancelConflict(ValueError):
    """이미 **다른 날짜로** 취소된 일정을 또 다른 날짜로 취소하려 한다.

    ⚠️ 같은 날짜로 다시 취소하는 것은 정상 재시도라 no-op 이다. 다른 날짜는
       *"언제 취소됐나"* 가 둘이 되는 것이라 막는다.
    """


class ScheduleReceiptExists(ValueError):
    """도착 Receipt 가 이미 있는 입고를 취소하려 한다.

    🔴 **물건이 도착했으면 취소가 아니라 다른 업무 흐름이다** (반품 · 폐기 · 실사).
       `inbound_reconciliation` 도 같은 선을 긋는다 — *"Receipt 가 하나라도 있으면
       이 함수는 거부한다"*.

    ★ 거부는 값이 아니라 예외다. 마스터 `undo_approval` 이 파트 거절을 받으면
      **전이 전체를 롤백**하므로, 매입·재무만 취소되고 물류만 남는 반쪽 상태가
      생기지 않는다.
    """


class ScheduleReferenceMissing(ValueError):
    """`purchase_id` 가 없어 `purchase_item_id` 를 세울 수 없다.

    🔴 **비워 두고 넘어가지 않는다.** 이 표는 W3-2 에서 정본이 되고, 그때 빠진 행은
       *"승인은 났는데 도착 조회에 안 잡히는 입고"* 가 된다 — 그것이 정확히 FIRSTINB
       사고의 모양이다. 지금 조용히 빼면 그 사고를 다시 심는 셈이다.

    ★ 마스터는 `#311`(2026-09-06)부터 `purchase_ids` 를 실제로 넘긴다 —
      실측 in_transit 5/5 가 `PUR-…` 값을 갖고 있어 정상 경로에서는 뜨지 않는다.
    """


@dataclass(frozen=True)
class InboundSchedule:
    """입고 예정 한 건. **그날 살아 있었나는 두 날짜가 답한다.**"""

    inbound_id: str
    sim_run_id: str
    purchase_item_id: str
    quantity_kg: Decimal
    expected_arrival_date: date
    created_as_of: date
    cancelled_as_of: date | None
    source_ref: str
    note: str | None


def _schema() -> sql.Identifier:
    return sql.Identifier(get_db_schema())


def _rows(conn: Any, query: sql.Composed, params: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _existing(conn: Any, *, sim_run_id: str, inbound_id: str) -> dict[str, Any] | None:
    """그 일정 한 행을 **잠그고** 읽는다.

    🔴 **`FOR UPDATE` 가 이 모듈의 동시성 방어다.** 읽고-고치고-쓰는 사이에 같은
       `inbound_id` 를 겨냥한 다른 트랜잭션이 끼어들면 멱등 판정이 무너진다
       (`transition.persist_inventory` 가 fixture 행을 잠그는 것과 같은 이유).
    """
    found = _rows(
        conn,
        sql.SQL(
            """
            SELECT inbound_id, sim_run_id, purchase_item_id, quantity_kg,
                   expected_arrival_date, created_as_of, cancelled_as_of, source_ref, note
            FROM {}.inbound_schedules
            WHERE sim_run_id = %s AND inbound_id = %s
            FOR UPDATE
            """
        ).format(_schema()),
        (sim_run_id, inbound_id),
    )
    return found[0] if found else None


def record_schedule(
    conn: Any,
    *,
    sim_run_id: str,
    inbound_id: str,
    purchase_item_id: str,
    quantity_kg: Decimal,
    expected_arrival_date: date,
    created_as_of: date,
    source_ref: str,
    note: str | None = None,
) -> bool:
    """입고 예정 한 건을 적는다. **같은 사실이면 no-op, 다른 사실이면 멈춘다.**

    ```text
    없음            INSERT                 → True
    같은 사실       아무것도 안 한다        → False   ★ 같은 승인 재반영이 여기다
    다른 사실       ScheduleConflict       ★ 덮지 않는다
    ```

    🔴 **대조 대상 넷이 계약이다** — `purchase_item_id` · `quantity_kg` ·
       `expected_arrival_date` · `created_as_of`. `source_ref` · `note` 는 근거
       기록이라 대조에서 뺀다(같은 사실을 다른 경로로 다시 적을 수 있다).

    ⚠️ **`quantity_kg` 를 `purchase_items.quantity_kg` 와 같게 강제하지 않는다.**
       이 값은 **회차 수량**이고 매입 줄은 그 회차들의 합일 수 있다 — 실측 5/5 가
       같은 것은 지금 분할 회차가 없어서지 계약이 아니다. 없는 규칙을 만들지 않는다.

    :returns: 이번 호출이 실제로 행을 만들었나.
    :raises ScheduleConflict: 같은 열쇠가 다른 사실로 이미 있을 때.
    """
    기존 = _existing(conn, sim_run_id=sim_run_id, inbound_id=inbound_id)
    if 기존 is not None:
        같음 = (
            기존["purchase_item_id"] == purchase_item_id
            and Decimal(str(기존["quantity_kg"])) == Decimal(str(quantity_kg))
            and 기존["expected_arrival_date"] == expected_arrival_date
            and 기존["created_as_of"] == created_as_of
        )
        if 같음:
            # ★ 같은 승인을 두 번 반영해도 행이 안 부푼다 — 멱등이 계약이다.
            return False
        # ★ 어느 칸이 다른지 보이게 적는다. "다르다" 만으로는 고칠 사람이 못 찾는다.
        비교 = ("purchase_item_id", "quantity_kg", "expected_arrival_date", "created_as_of")
        이번값 = {
            "purchase_item_id": purchase_item_id,
            "quantity_kg": quantity_kg,
            "expected_arrival_date": expected_arrival_date,
            "created_as_of": created_as_of,
        }
        raise ScheduleConflict(
            f"같은 입고 일정이 다른 사실로 이미 있다 (sim_run_id={sim_run_id!r},"
            f" inbound_id={inbound_id!r})."
            f" 기존={ {칸: 기존[칸] for 칸 in 비교} } 이번={이번값}."
            " 덮지도 버리지도 않는다 — 어느 쪽이 진짜인지 여기서 고를 근거가 없다."
        )

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {}.inbound_schedules (
                    inbound_id, sim_run_id, purchase_item_id, quantity_kg,
                    expected_arrival_date, created_as_of, source_ref, note
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(_schema()),
            (
                inbound_id,
                sim_run_id,
                purchase_item_id,
                quantity_kg,
                expected_arrival_date,
                created_as_of,
                source_ref,
                note,
            ),
        )
    return True


def has_receipt(conn: Any, *, sim_run_id: str, inbound_id: str) -> bool:
    """그 입고의 도착 Receipt 가 있나.

    ★ `inbound_receipts` 에 `UNIQUE(sim_run_id, inbound_id)` 가 있어 0 아니면 1 이다.
    """
    found = _rows(
        conn,
        sql.SQL(
            "SELECT 1 AS 있음 FROM {}.inbound_receipts WHERE sim_run_id = %s AND inbound_id = %s"
        ).format(_schema()),
        (sim_run_id, inbound_id),
    )
    return bool(found)


def cancel_schedule(
    conn: Any, *, sim_run_id: str, inbound_id: str, cancelled_as_of: date
) -> bool:
    """입고 예정을 **그날부터** 취소한다. 과거는 고치지 않는다.

    ```text
    as_of <  cancelled_as_of   그날 이 일정은 여전히 존재한다
    as_of >= cancelled_as_of   그날부터 취소다
    ```

    🔴 **`cancelled_on` 이 아니라 `target_state_date` 를 받는다.** 현재 계약이
       *"01-07 취소 → 01-08 상태에서 제거"* 이고, 취소일 자체를 적으면 **이미 지나간
       하루의 사실이 바뀐다.** 승인이 `commitment.as_of + 1` 행에 서는 것과 같은 결이다.

    ```text
    없음                 아무것도 안 한다        → False   ★ 오류가 아니다
    아직 안 취소         NULL → cancelled_as_of  → True
    같은 날짜로 취소됨    아무것도 안 한다        → False   멱등 재시도
    다른 날짜로 취소됨    ScheduleCancelConflict
    ```

    🔴 **Receipt 가 있으면 취소하지 않는다.** 판정은 호출부가 `withdraw_inventory`
       에서 **Legacy JSON 을 고치기 전에** 한다 — 한쪽만 바뀌는 상태를 만들지 않기
       위해서다. 이 함수는 그 뒤에 불린다.

    :returns: 이번 호출이 실제로 취소를 적었나.
    :raises ScheduleCancelConflict: 이미 다른 날짜로 취소돼 있을 때.
    """
    기존 = _existing(conn, sim_run_id=sim_run_id, inbound_id=inbound_id)
    if 기존 is None:
        # ★ Backfill 이전에 사라진 일정도 있을 수 있다. 없는 것을 걷어도 오류가
        #   아니다 — `withdraw_inventory` 의 같은 태도다.
        return False
    이미 = 기존["cancelled_as_of"]
    if 이미 is not None:
        if 이미 == cancelled_as_of:
            return False
        raise ScheduleCancelConflict(
            f"이미 다른 날짜로 취소된 일정이다 (sim_run_id={sim_run_id!r},"
            f" inbound_id={inbound_id!r}, 기존 cancelled_as_of={이미},"
            f" 이번={cancelled_as_of}). 언제 취소됐나가 둘이 될 수 없다."
        )

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.inbound_schedules
                SET cancelled_as_of = %s
                WHERE sim_run_id = %s AND inbound_id = %s AND cancelled_as_of IS NULL
                """
            ).format(_schema()),
            (cancelled_as_of, sim_run_id, inbound_id),
        )
    return True


def load_inbound_schedules(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[InboundSchedule, ...]:
    """`as_of` 시점에 **살아 있던** 입고 예정 전부.

    ```text
    created_as_of <= as_of                              그날 이미 장부에 서 있었다
    cancelled_as_of IS NULL OR cancelled_as_of > as_of   그날 아직 취소 전이었다
    ```

    🔴 **소비자별 종료조건은 여기서 걸지 않는다 (W3-2).** Receipt · Lot · 원장 IN 을
       어디까지 봐야 하는지가 소비자마다 다르다.

    ```text
    운송 중 조회   Receipt 생성 전까지
    도착 · 용량    Lot + 원장 IN 완료 전까지
    취소 · 정리    Receipt 0건일 때만
    ```

       하나를 이 함수에 박으면 나머지가 틀린다 — 그래서 **시점 축만** 자르고,
       종료조건은 부르는 쪽이 얹는다.

    ⚠️ **W3-1 에서는 이 함수를 Runtime 이 쓰지 않는다.** Legacy JSON 과 대조하는
       자리에서만 부른다. Reader 전환은 W3-2 다.
    """
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT inbound_id, sim_run_id, purchase_item_id, quantity_kg,
                   expected_arrival_date, created_as_of, cancelled_as_of, source_ref, note
            FROM {}.inbound_schedules
            WHERE sim_run_id = %(sim)s
              AND created_as_of <= %(as_of)s
              AND (cancelled_as_of IS NULL OR cancelled_as_of > %(as_of)s)
            ORDER BY expected_arrival_date, inbound_id
            """
        ).format(_schema()),
        {"sim": sim_run_id, "as_of": as_of},
    )
    return tuple(
        InboundSchedule(
            inbound_id=row["inbound_id"],
            sim_run_id=row["sim_run_id"],
            purchase_item_id=row["purchase_item_id"],
            quantity_kg=row["quantity_kg"],
            expected_arrival_date=row["expected_arrival_date"],
            created_as_of=row["created_as_of"],
            cancelled_as_of=row["cancelled_as_of"],
            source_ref=row["source_ref"],
            note=row["note"],
        )
        for row in rows
    )


def assert_cancellable(
    conn: Any, *, sim_run_id: str, inbound_ids: Sequence[str]
) -> None:
    """취소해도 되는 입고들인가. **Legacy JSON 을 고치기 전에 먼저 묻는다.**

    🔴 **한쪽만 바뀌는 상태를 만들지 않으려고 앞에서 전부 본다.** 하나씩 지우면서
       중간에 막히면 앞의 것은 이미 JSON 에서 빠진 뒤다 — 같은 트랜잭션이라 롤백은
       되지만, 판정이 쓰기와 섞이면 *"무엇이 왜 막혔나"* 가 흐려진다.

    :raises ScheduleReceiptExists: 하나라도 Receipt 가 있을 때. **어느 것인지 적는다.**
    """
    도착함 = [
        inbound_id
        for inbound_id in inbound_ids
        if inbound_id and has_receipt(conn, sim_run_id=sim_run_id, inbound_id=inbound_id)
    ]
    if 도착함:
        raise ScheduleReceiptExists(
            f"도착 Receipt 가 이미 있는 입고는 취소할 수 없다 (sim_run_id={sim_run_id!r}):"
            f" {sorted(도착함)}. 물건이 도착했으면 취소가 아니라 반품·폐기·실사이고,"
            " 그 판단은 여기서 대신 내리지 않는다."
        )
