"""inbound_schedules.py — 입고 예정을 **날짜에 안 묶인 업무 Entity 로** 적는다 (W3-1).

```text
승인   record_schedule   INSERT 1행                     (날짜별 복제 없음)
취소   cancel_schedule   cancelled_as_of UPDATE          (과거는 안 고친다)
조회   load_inbound_schedules  created_as_of <= as_of    (W3-2 Reader 가 재사용)
```

🔴 **입고 예정의 정본은 이 표 하나다 (W3-3 완료).**

```text
Reader   inbound_schedules                       운송 중 · 도착 처리 · Capacity
Writer   inbound_schedules                       승인 · 취소 · orphan 정리
Header   logistics_runtime_fixture.*_status      «그 축을 확인했나» 만
```

   `logistics_runtime_fixture` 의 두 JSON 칸은 더 이상 읽히지도 쓰이지도 않는다 —
   DROP 대상이다 (`database/logistics_drop_inbound_json.sql`).

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
   나머지 소비자가 틀린다. 소비자별 종료조건은 이 파일 아래쪽 Reader 절에 있다.

🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** 승인·취소가 같은 트랜잭션에서
   쓰는 다른 사실(매입 원장 · 재무 · Header status)과 **한 덩어리로 서거나 함께
   물러나야** 한다 (`transition.persist_inventory` 와 같은 규율).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.schemas import InTransitItem, ScheduledQuantity

__all__ = [
    "InboundSchedule",
    "InboundScheduleView",
    "ScheduleAlreadyCancelled",
    "ScheduleCancelConflict",
    "ScheduleConflict",
    "ScheduleReceiptExists",
    "ScheduleReferenceBroken",
    "ScheduleReferenceMissing",
    "assert_cancellable",
    "cancel_schedule",
    "in_transit_at",
    "load_inbound_schedules",
    "load_schedule_views",
    "pending_inbound_at",
    "receivable_at",
    "record_schedule",
]


class ScheduleConflict(ValueError):
    """같은 `(sim_run_id, inbound_id)` 가 **다른 사실**로 이미 있다. 무결성 위반이다.

    🔴 **기존 행을 UPDATE 해서 맞추지 않는다.** 어느 쪽이 진짜인지 여기서 고를 근거가
       없고, 고치면 그 순간 과거 사실이 조용히 바뀐다
       (`transition.InboundScheduleConflict` 와 같은 규율).
    """


class ScheduleAlreadyCancelled(ValueError):
    """**취소된** 일정을 같은 승인으로 다시 적으려 한다.

    🔴 **조용히 되살리지 않는다.** 대조 넷(`purchase_item_id` · `quantity_kg` ·
       `expected_arrival_date` · `created_as_of`)이 같아도, 이미 *"그날부터 없다"* 고
       적힌 행을 no-op 으로 넘기면 **되살리는 결정을 아무도 내리지 않은 채** 그 일정이
       다시 사는 것처럼 읽힌다.

    ```text
    승인 → 취소 → 같은 승인 재실행 → 여기서 멈춘다
    ```

    ⚠️ **과거에는 이것이 두 저장소를 갈랐다** (W3-1 Dual Write 시절). 그때는 Legacy
       JSON 이 되살아나고 일정만 취소로 남았다. 지금은 정본이 하나라 갈릴 곳이 없지만,
       **되살림 자체를 결정 없이 하지 않는다**는 규율은 그대로다.

    ★ **취소 이력을 지워 `cancelled_as_of = NULL` 로 되돌리지 않는다.** 그것이
      옳으려면 *"취소를 무를 수 있다"* 는 업무 계약이 있어야 하는데, 저장소 어디에도
      그 계약이 없다 — 마스터 `undo_approval` 은 취소만 있고 되돌리기가 없다.
      근거 없이 과거 취소 이력을 지우는 쪽이 조용히 되살리는 것보다 더 위험하다.

    ⚠️ 예외라서 **바깥 트랜잭션이 통째로 롤백된다** — JSON 도 안 되살아난다.
       그것이 이 예외가 지키는 것이다.
    """


class ScheduleReferenceBroken(RuntimeError):
    """일정은 있는데 그 `purchase_item_id` 가 가리키는 매입 줄(또는 품목)이 없다.

    🔴 **«입고 없음» 으로 읽지 않는다.** 종전 Reader 는 `purchase_items` 를 `JOIN`
       해서 그 일정이 **결과에서 통째로 사라졌다** — 승인은 났는데 도착 조회에 안
       잡히는 상태이고, 그것이 정확히 FIRSTINB 사고의 모양이다.

    ```text
    참조 정상   일정이 나온다
    참조 깨짐   🔴 종전: 0건 (조용히 사라짐)   지금: 여기서 멈춘다
    ```

    ★ **`purchase_item_id` 에 FK 를 아직 안 걸었기 때문에 생기는 자리다**
      (`purchase_items → purchases` 가 `ON DELETE CASCADE` 라, FK 를 걸면 매입 삭제가
      과거 재현용 일정까지 지운다 — 그 정책이 미정이다). DB 가 못 막는 동안
      **Reader 가 막는다.**
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
    없음                 INSERT                     → True
    같은 사실 · 살아있음   아무것도 안 한다            → False   ★ 같은 승인 재반영이 여기다
    취소된 일정           ScheduleAlreadyCancelled   ★ 되살리지 않는다
    다른 사실            ScheduleConflict           ★ 덮지 않는다
    ```

    🔴 **취소 여부를 대조 넷보다 **먼저** 본다.** 값이 같아도 그 행은 이미 *"그날부터
       없다"* 고 적힌 행이다 (`ScheduleAlreadyCancelled` 참조).

    🔴 **대조 대상 넷이 계약이다** — `purchase_item_id` · `quantity_kg` ·
       `expected_arrival_date` · `created_as_of`. `source_ref` · `note` 는 근거
       기록이라 대조에서 뺀다(같은 사실을 다른 경로로 다시 적을 수 있다).

    ⚠️ **`quantity_kg` 를 `purchase_items.quantity_kg` 와 같게 강제하지 않는다.**
       이 값은 **회차 수량**이고 매입 줄은 그 회차들의 합일 수 있다 — 실측 5/5 가
       같은 것은 지금 분할 회차가 없어서지 계약이 아니다. 없는 규칙을 만들지 않는다.

    :returns: 이번 호출이 실제로 행을 만들었나.
    :raises ScheduleAlreadyCancelled: 그 일정이 이미 취소돼 있을 때.
    :raises ScheduleConflict: 같은 열쇠가 다른 사실로 이미 있을 때.
    """
    기존 = _existing(conn, sim_run_id=sim_run_id, inbound_id=inbound_id)
    if 기존 is not None:
        # 🔴 **취소가 먼저다.** 값이 같아도 되살리는 것은 별개의 결정이고,
        #    그 결정을 여기서 조용히 내리지 않는다.
        if 기존["cancelled_as_of"] is not None:
            raise ScheduleAlreadyCancelled(
                f"이미 취소된 입고 일정을 다시 적으려 한다 (sim_run_id={sim_run_id!r},"
                f" inbound_id={inbound_id!r}, cancelled_as_of={기존['cancelled_as_of']})."
                " 같은 승인을 다시 반영해도 취소를 무르지 않는다 —"
                " 취소를 되돌리는 업무 계약이 저장소에 없다."
                " 되살려야 한다면 그 근거를 먼저 정하고 이 자리를 고친다."
            )
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
    conn: Any,
    *,
    sim_run_id: str,
    inbound_id: str,
    cancelled_as_of: date,
    cancel_source_ref: str | None = None,
) -> bool:
    """입고 예정을 **그날부터** 취소한다. 과거는 고치지 않는다.

    ```text
    as_of <  cancelled_as_of   그날 이 일정은 여전히 존재한다
    as_of >= cancelled_as_of   그날부터 취소다
    ```

    🔴 **`cancel_source_ref` 는 취소 근거다. `source_ref`(생성 근거)를 덮지 않는다.**
       안 주면 `NULL` 로 남고 취소 자체는 그대로 된다 — 그날 살아 있었나는 계속
       `cancelled_as_of` 하나가 답한다.

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
                SET cancelled_as_of = %s, cancel_source_ref = %s
                WHERE sim_run_id = %s AND inbound_id = %s AND cancelled_as_of IS NULL
                """
            ).format(_schema()),
            (cancelled_as_of, cancel_source_ref, sim_run_id, inbound_id),
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
    """취소해도 되는 입고들인가. **취소 UPDATE 를 쓰기 전에 먼저 묻는다.**

    🔴 **한쪽만 바뀌는 상태를 만들지 않으려고 앞에서 전부 본다.** 하나씩 걷다가
       중간에 막히면 앞의 것은 이미 닫힌 뒤다 — 같은 트랜잭션이라 롤백은 되지만,
       판정이 쓰기와 섞이면 *"무엇이 왜 막혔나"* 가 흐려진다.

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


# ── W3-2 Reader — 소비자마다 종료조건이 다르다 ──────────────────────────
#
# 🔴 **Entity 하나 ≠ Reader 종료조건 하나.** 이것이 이 절의 전부다.
#
# ```text
# 상태                        운송 중   도착 처리   Capacity
# ETA 전 · Receipt 없음          O        X          O
# ETA 도달 · Receipt 없음        O        O          O
# Receipt 있음 · Lot 없음        X        O          O    ← 여기가 갈리는 자리다
# Lot + IN Move 완료             X        X          X    (그때부터 on_hand 가 센다)
# 취소됨                        X        X          X
# ```
#
#   ⚠️ **Receipt 가 생겼다고 모든 Reader 에서 빼지 않는다.** 검수가 막히면 Receipt 만
#      선 채 며칠 간다(`inbound_execution._receive_one` 의 `INSPECTION_FACT_UNAVAILABLE`).
#      그 물건은 창고에 와 있고(→ Capacity 계상), 다음 실행이 이어받아야 하며(→ 도착
#      처리 대상), 다만 *"운송 중"* 은 아니다.
#
# 🔴 **`in_transit` 과 `confirmed_inbound` 이 같아야 한다는 불변조건을 만들지 않는다.**
#    Legacy 에서 둘이 같았던 것은 발주 확정 단계가 비어 승인을 두 칸에 겹쳐 적었기
#    때문이다(`transition.py` 의 *"임시 조치"*). 신규 구조에서는 종료조건이 달라
#    `in_transit ⊆ confirmed_inbound` 다 — B-1 은 그 방향만 보므로 여전히 통과한다.


@dataclass(frozen=True)
class InboundScheduleView:
    """일정 한 건 + **그날까지의 입고 계보.** 종료조건 판정은 소비자가 한다.

    ★ **`purchase_id` · `item_id` · `item_name` 을 표에 저장하지 않고 JOIN 으로 얻는다**
      (`purchase_items` · `items` 가 그 값의 주인이다). 복사해 두면 매입이 값을 고치는
      날 일정만 옛 값을 들고 남는다.
    """

    inbound_id: str
    sim_run_id: str
    purchase_item_id: str
    purchase_id: str
    item_id: str
    item_name: str
    quantity_kg: Decimal
    expected_arrival_date: date
    created_as_of: date
    #: 그날까지 도착 Receipt 가 있었나 (`arrived_at <= as_of`).
    has_receipt: bool
    #: 그날까지 **재고가 실제로 섰나** — Lot 과 원장 IN 이 **둘 다** 있어야 참이다.
    #: 🔴 Receipt 존재로 대신하지 않는다. 그 둘은 다른 사건이다.
    stock_applied: bool

    def as_in_transit(self) -> InTransitItem:
        """Legacy 계약 그대로의 운송 중 한 줄. **DTO 를 새로 만들지 않는다.**"""
        return InTransitItem(
            inbound_id=self.inbound_id,
            purchase_id=self.purchase_id,
            item=self.item_name,
            quantity_kg=self.quantity_kg,
            expected_arrival_date=self.expected_arrival_date,
        )

    def as_scheduled_quantity(self) -> ScheduledQuantity:
        """Capacity 가 읽는 일정 한 줄. 🔴 **필드 이름이 다르다** (`date`)."""
        return ScheduledQuantity(
            inbound_id=self.inbound_id,
            item=self.item_name,
            quantity_kg=self.quantity_kg,
            date=self.expected_arrival_date,
        )


def load_schedule_views(
    conn: Any, *, sim_run_id: str, as_of: date
) -> tuple[InboundScheduleView, ...]:
    """`as_of` 시점에 살아 있던 일정 + 그날까지의 계보. **한 질의다.**

    ```text
    created_as_of <= as_of                              그날 이미 장부에 서 있었다
    cancelled_as_of IS NULL OR cancelled_as_of > as_of   그날 아직 취소 전이었다
    ```

    🔴 **계보도 `as_of` 로 자른다.** Receipt 는 `arrived_at <= as_of`, Lot 은
       `received_at <= as_of`, 원장 IN 은 `moved_at <= as_of` 다 — 오늘 상태를
       과거 날짜 답에 섞으면 Historical 조회가 거짓말을 한다.

    🔴 **계보는 `EXISTS` 로 묻는다. `LEFT JOIN` 으로 끌어오지 않는다.**

    ```text
    Receipt 1 → Lot 2      LEFT JOIN 이면 일정 한 건이 2줄이 된다
    Lot 1 → IN Move 2      〃
    ```

       DDL 이 그 둘을 막지 않는다 — `inventory_lots.inbound_receipt_id` 에도
       `inventory_moves` 의 `(lot_id, move_type)` 에도 UNIQUE 가 없다. JOIN 곱으로
       늘어나면 `pending_inbound_at` 이 **같은 수량을 두 번 세어** Capacity 가
       틀린다. `EXISTS` 는 있고 없음만 묻고 행을 늘리지 않는다 —
       *"일정 1건 → Reader 1건"* 이 구조적으로 성립한다.

    🔴 **`purchase_items` 는 `LEFT JOIN` 하고 없으면 멈춘다.** 종전에는 `JOIN` 이라
       참조가 깨진 일정이 **결과에서 조용히 사라졌다**(실측 재현). 깨진 참조를
       *"입고 없음"* 으로 읽으면 그것이 곧 FIRSTINB 사고의 모양이다 —
       `ScheduleReferenceBroken` 으로 드러낸다.

    :raises ScheduleReferenceBroken: 일정의 매입 줄·품목 참조가 깨졌을 때.
    """
    schema = _schema()
    rows = _rows(
        conn,
        sql.SQL(
            """
            SELECT s.inbound_id, s.sim_run_id, s.purchase_item_id,
                   pi.purchase_id, pi.item_id, i.item_name,
                   s.quantity_kg, s.expected_arrival_date, s.created_as_of,
                   EXISTS (
                       SELECT 1 FROM {schema}.inbound_receipts r
                        WHERE r.sim_run_id = s.sim_run_id
                          AND r.inbound_id = s.inbound_id
                          AND r.arrived_at <= %(as_of)s
                   ) AS has_receipt,
                   EXISTS (
                       SELECT 1
                         FROM {schema}.inbound_receipts r
                         JOIN {schema}.inventory_lots l
                           ON l.inbound_receipt_id = r.receipt_id
                          AND l.sim_run_id = s.sim_run_id
                          AND l.received_at <= %(as_of)s
                         JOIN {schema}.inventory_moves mv
                           ON mv.lot_id = l.lot_id
                          AND mv.sim_run_id = s.sim_run_id
                          AND mv.move_type = 'IN'
                          AND mv.moved_at <= %(as_of)s
                        WHERE r.sim_run_id = s.sim_run_id
                          AND r.inbound_id = s.inbound_id
                          AND r.arrived_at <= %(as_of)s
                   ) AS stock_applied
            FROM {schema}.inbound_schedules s
            LEFT JOIN {schema}.purchase_items pi
                   ON pi.purchase_item_id = s.purchase_item_id
            LEFT JOIN {schema}.items i ON i.item_id = pi.item_id
            WHERE s.sim_run_id = %(sim)s
              AND s.created_as_of <= %(as_of)s
              AND (s.cancelled_as_of IS NULL OR s.cancelled_as_of > %(as_of)s)
            ORDER BY s.expected_arrival_date, s.inbound_id
            """
        ).format(schema=schema),
        {"sim": sim_run_id, "as_of": as_of},
    )
    _reject_broken_reference(rows, sim_run_id=sim_run_id, as_of=as_of)
    return tuple(
        InboundScheduleView(
            inbound_id=row["inbound_id"],
            sim_run_id=row["sim_run_id"],
            purchase_item_id=row["purchase_item_id"],
            purchase_id=row["purchase_id"],
            item_id=row["item_id"],
            item_name=row["item_name"],
            quantity_kg=row["quantity_kg"],
            expected_arrival_date=row["expected_arrival_date"],
            created_as_of=row["created_as_of"],
            has_receipt=bool(row["has_receipt"]),
            stock_applied=bool(row["stock_applied"]),
        )
        for row in rows
    )


def _reject_broken_reference(
    rows: list[dict[str, Any]], *, sim_run_id: str, as_of: date
) -> None:
    """매입 줄·품목 참조가 깨진 일정이 있으면 멈춘다. **0건으로 답하지 않는다.**

    ★ 어느 일정이 어느 참조를 잃었는지 적는다 — `purchase_item_id` 까지 보여야
      고칠 사람이 매입 쪽을 찾아갈 수 있다.
    """
    깨진것 = [
        f"{row['inbound_id']}→{row['purchase_item_id']}"
        for row in rows
        if row["purchase_id"] is None or row["item_id"] is None or row["item_name"] is None
    ]
    if 깨진것:
        raise ScheduleReferenceBroken(
            f"입고 일정이 가리키는 매입 줄·품목이 없다 (sim_run_id={sim_run_id!r},"
            f" as_of={as_of}): {sorted(깨진것)}."
            " 이 상태를 «입고 없음» 으로 읽지 않는다 —"
            " 승인은 났는데 도착 조회에 안 잡히는 입고가 되기 때문이다."
        )


def in_transit_at(conn: Any, *, sim_run_id: str, as_of: date) -> list[InTransitItem]:
    """**운송 중** — 아직 창고에 도착하지 않은 입고.

    ```text
    종료조건   Receipt 가 생기면 빠진다
    ```

    🔴 **ETA 가 지났다고 빼지 않는다.** ETA 01-15 · 오늘 01-17 · Receipt 없음이면
       그것은 사라진 입고가 아니라 **연체된 미도착**이다
       (`arrival.select_due_inbound` 이 `overdue_count` 로 세는 그 상태다).
    """
    views = load_schedule_views(conn, sim_run_id=sim_run_id, as_of=as_of)
    return [view.as_in_transit() for view in views if not view.has_receipt]


def receivable_at(conn: Any, *, sim_run_id: str, as_of: date) -> list[InTransitItem]:
    """**도착 처리 대상** — 아직 재고가 서지 않은 입고.

    ```text
    종료조건   Lot 과 원장 IN 이 둘 다 서면 빠진다
    ```

    🔴 **Receipt 존재로 빼지 않는다.** 검수에서 막힌 건(`Receipt=ARRIVED` · Lot 없음)은
       **다음 실행이 이어받아야 한다** — `inbound_execution._receive_one` 이
       `check_receipt_state` 로 마지막 성공 단계 다음부터 잇는 구조라, 여기서 빼면
       그 입고가 영구 고착된다.

    ★ 날짜(`eta <= as_of`)로 자르지 않는다 — 그 판정은 `arrival.select_due_inbound` 이
      네 갈래(`due` · `blocked` · `not_due` · `unresolved`)로 나누며 소유한다.
      여기서 미리 자르면 *"아직 안 온 것"* 과 *"못 받은 것"* 이 구별되지 않는다.
    """
    views = load_schedule_views(conn, sim_run_id=sim_run_id, as_of=as_of)
    return [view.as_in_transit() for view in views if not view.stock_applied]


def pending_inbound_at(conn: Any, *, sim_run_id: str, as_of: date) -> list[ScheduledQuantity]:
    """**미래 점유로 셀 입고** — Capacity 가 읽는 일정.

    ```text
    종료조건   Lot 과 원장 IN 이 둘 다 서면 빠진다 (그때부터 on_hand 가 센다)
    ```

    🔴 **한 번만 계상하기 위한 경계다.**

    ```text
    재고 반영 전   여기가 센다              on_hand 에는 없다
    재고 반영 후   여기서 빠진다            on_hand 가 센다
    ```

       Receipt 만 있고 Lot 이 없는 1,000kg 을 여기서 빼면 **창고에 와 있는 물건이
       점유에서 사라져** 없는 여유가 생긴다. 반대로 Lot 이 선 뒤에도 남기면
       같은 수량을 두 번 센다.
    """
    views = load_schedule_views(conn, sim_run_id=sim_run_id, as_of=as_of)
    return [view.as_scheduled_quantity() for view in views if not view.stock_applied]
