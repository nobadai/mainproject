"""inspections.py — 도착한 Receipt 를 **검수 결과로 마감한다** (3-B4-H).

```text
ARRIVED Receipt
   → InspectionOutcome (호출자가 준다)
   → inbound_inspections INSERT
   → Receipt 수량 + receipt_status = INSPECTED
```

🔴 **검수 결과를 지어내지 않는다.** 저장소 어디에도 *"자동 시뮬레이션에서 몇 %가
   PASS 인가"* 를 정한 규칙이 없다 (Persona · 정책 · 스키마 · 코드 전부 확인).
   없는 규칙을 여기서 만들면 그 비율이 곧 업무 사실이 되어 원가·폐기 판단으로
   흘러간다. 그래서 판정과 수량은 **호출자가 주는 입력**이고, 이 파일은 그것이
   DB 계약을 어기지 않는지만 본다.

   ⚠️ 시나리오 생성기가 붙는 날 **이 파일을 고칠 필요가 없다** — 결과를 만들어
      넘기기만 하면 된다.

🔴 **`inspector` 도 지어내지 않는다.** 저장소에 시스템 행위자 상수(`SYSTEM_*` 등)
   규약이 없다. `inspector` 는 NOT NULL 이지만 **호출자가 준다** — 없는 사람 이름을
   만드는 것보다 그 값을 정할 자리(시나리오 생성기 · 웹 Form)에 맡기는 것이 맞다.

🔴 **`inspected_at` 에 시계를 읽지 않는다.** `datetime.now()` 를 부르지 않고
   호출자가 준 값을 쓴다 — 같은 시뮬레이션을 다시 돌리면 같은 값이 나와야 한다.
   `arrived_at` 이 `DATE` 인 것과 달리 이 칸은 `TIMESTAMPTZ` 라, **tz 를 단 값만**
   받는다 (naive 를 넣으면 세션 TimeZone 에 따라 뜻이 달라진다).

★ **`receipts.py` 와 나눈 이유.** 저쪽은 *"Receipt 행이 있나 · 만든다"* 이고
  이쪽은 *"검수 사실을 적고 Receipt 를 마감한다"* 다. 잠금은 **저쪽 것을 그대로
  쓴다** — 세 번째 잠금을 만들지 않는다.

🔴 **스키마 실측 (2026-09-05 · 저장소 DDL 과 실 DB 카탈로그 일치).**

  ```text
  inbound_inspections
    PK        inspection_id 단독
    UNIQUE    🔴 receipt_id 에 **없다** — 한 Receipt 에 검수 여러 건이 물리적으로 가능
    FK        receipt_id → inbound_receipts
    CHECK     verdict IN (PASS, HOLD, REJECT)
    CHECK     inspected > 0 · 나머지 >= 0 · accepted + hold + reject = inspected
    CHECK     PASS→hold=0,reject=0 / HOLD→hold>0 / REJECT→accepted=0,reject>0
  ```

  ⚠️ **UNIQUE 가 없다고 스키마를 지금 고치지 않는다.** 대신 조회에서 0 · 1 · 2+ 를
     갈라 방어한다 (`repository` 의 활성 fixture, `receipts` 의 Receipt 중복과 같은
     규율). 첫 행을 집지 않는다.

  ⚠️ **칸 이름이 두 표에서 다르다.** 검수는 `reject_qty_kg`, Receipt 는
     `rejected_qty_kg` 다 — 실측이고, 옮길 때 이 차이를 잊으면 조용히 어긋난다.

  ★ `updated_at` 갱신 **트리거가 없다** (실측). 그래서 Receipt UPDATE 가
    `updated_at = now()` 를 직접 적는다 — DB 의 `DEFAULT now()` 는 INSERT 에만 걸린다.
    ⚠️ 이 `now()` 는 **DB 의 기록 시각**이지 업무 사실이 아니다. 업무 시각인
       `inspected_at` 은 위에서 말한 대로 호출자가 준다.

⚠️ **`inbound_inspection_checks` 를 쓰지 않는다.** 그 표의 주석이 *"사람이 웹 Form
   으로 넣는다"* 이고, 항목이 필수라는 정책이 어디에도 없다. `MOLD=false` 를 채우면
   **하지 않은 관찰을 했다고 적는 것**이 된다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from psycopg import sql

from app.logistics.db import get_db_schema
from app.logistics.receipts import ReceiptStatus, lock_arrival_writes

__all__ = [
    "InspectionConflict",
    "InspectionError",
    "InspectionIntegrityError",
    "InspectionOutcome",
    "InspectionRecord",
    "InspectionVerdict",
    "InspectionWriteResult",
    "InvalidInspectionOutcome",
    "find_inspection",
    "inspection_id_for",
    "record_inspection",
    "validate_outcome",
]


#: `ck_inbound_inspections_verdict` 어휘 그대로다. 새 판정을 만들지 않는다.
InspectionVerdict = Literal["PASS", "HOLD", "REJECT"]

_VERDICTS: frozenset[str] = frozenset(get_args(InspectionVerdict))

#: 🔴 **창고에서 사람이 확인한 것이 아니다.** `ck_inbound_inspections_fact_source` 는
#: `HUMAN_RECORDED · SCENARIO_SIMULATED` 둘뿐이고, 자동 경로에 앞엣것을 쓰면 거짓이다.
_FACT_SOURCE_SIMULATED = "SCENARIO_SIMULATED"

#: 🔴 **둘까지만 읽는다.** 0 · 1 · 2+ 를 가르는 데 그 이상이 필요 없다 —
#: 어차피 어느 하나도 고르지 않고 멈춘다.
_AMBIGUITY_PROBE_LIMIT = 2

#: 검수 사실을 아직 안 가진 Receipt 상태. 여기서만 새 검수를 만들 수 있다.
_BEFORE_INSPECTION: frozenset[str] = frozenset({"ARRIVED", "INSPECTING"})

#: 검수 단계가 끝난 Receipt 상태. **되돌리지 않는다.**
_INSPECTION_DONE: frozenset[str] = frozenset({"INSPECTED", "PUTAWAY_DONE", "CLOSED"})

_INSPECTION_COLUMNS = (
    "inspection_id",
    "verdict",
    "inspected_qty_kg",
    "accepted_qty_kg",
    "hold_qty_kg",
    "reject_qty_kg",
)


class InspectionError(RuntimeError):
    """이 모듈이 내는 실패의 조상 (`receipts.ReceiptLookupError` 와 같은 결)."""


class InvalidInspectionOutcome(InspectionError, ValueError):
    """검수 결과가 DB 계약을 어긴다. **쓰기 전에 막는다.**

    ★ DB CHECK 가 어차피 막지만, 거기까지 가면 **트랜잭션이 aborted 된다** —
      바깥이 롤백할 수밖에 없어 멀쩡한 다른 작업까지 잃는다 (`ledger.py` 가 업무
      검증을 DML 앞에 두는 것과 같은 이유).
    """


class InspectionIntegrityError(InspectionError, ValueError):
    """검수와 Receipt 가 서로를 배반한다. 무결성 위반이다.

    🔴 **조용히 고치지 않는다.** 한 Receipt 에 검수가 둘이거나, Receipt 가
       `INSPECTED` 인데 검수 행이 없거나, 이미 마감된 Receipt 의 수량이 검수와 다른
       경우다 — 어느 쪽이 진짜인지 여기서 고를 근거가 없다.
    """


class InspectionConflict(InspectionError, ValueError):
    """같은 Receipt 에 **다른 사실**의 검수가 이미 있다.

    🔴 **덮어쓰지 않는다.** 기존을 남기면 이번 결과가 조용히 사라지고, 갈아 끼우면
       앞 결과가 사라진다 — 둘 다 *"에러 없이 틀리는"* 쪽이다
       (`transition.InboundScheduleConflict` 와 같은 판단).
    """


@dataclass(frozen=True)
class InspectionOutcome:
    """검수 판정과 수량. **호출자가 주는 사실이다 — 여기서 만들지 않는다.**"""

    verdict: InspectionVerdict
    inspected_qty_kg: Decimal
    accepted_qty_kg: Decimal
    hold_qty_kg: Decimal
    reject_qty_kg: Decimal


@dataclass(frozen=True)
class InspectionRecord:
    """이미 적혀 있던 검수 한 건."""

    inspection_id: str
    outcome: InspectionOutcome


@dataclass(frozen=True)
class InspectionWriteResult:
    """`record_inspection` 의 결과.

    🔴 **`applied=False` 는 "할 일이 없다" 가 아니다.** 뜻은 하나 — *"이 호출이 검수
       행을 새로 만들지 않았다."* Lot · 원장 IN 은 아직 남아 있을 수 있다.
    """

    #: 이번 호출이 검수 행을 **새로 만들었나.**
    applied: bool
    inspection_id: str
    #: 이 호출이 끝난 시점의 Receipt 상태.
    receipt_status: ReceiptStatus
    #: 권위 있는 검수 사실 — 새로 썼으면 그 값, 이미 있었으면 **DB 에 적힌 값.**
    outcome: InspectionOutcome


def inspection_id_for(*, receipt_id: str) -> str:
    """검수 행의 PK. **순수 계산이고 결정론이다.**

    ```text
    INSP-{receipt_id}
    → INSP-RCPT-SIM-BURNIN-202512-INB-H1-THRU-20260105-BAECHU-1-1
    ```

    ★ `receipt_id` 가 이미 실행(`sim_run_id`)과 입고(`inbound_id`) 정체성을 담고
      있어 그 위에 접두사만 얹으면 된다.

    🔴 난수 · 시계 · DB 시퀀스 · 매입 ID 유도를 쓰지 않는다. 같은 Receipt 는 몇 번을
       불러도 같은 값이어야 재시도가 멱등해진다.

    ⚠️ **저장소에 검수 ID 규약이 없었다 (실측: 씨앗 0건 · 코드 0건).** 이 규칙은
       물류가 새로 정한 것이고, `receipt_id_for` 와 같은 결로 맞췄다.
    """
    if not receipt_id or not receipt_id.strip():
        raise InvalidInspectionOutcome(
            f"inspection_id 를 지을 수 없다 — receipt_id 가 비었다: {receipt_id!r}"
        )
    return f"INSP-{receipt_id}"


def _quantity(value: object, *, 칸: str) -> Decimal:
    """수량을 `Decimal` 로 좁힌다. **float 도 비유한값도 받지 않는다.**

    ★ `ledger._quantity` 와 같은 규율이다 — `NaN` · `Infinity` 는 DB CHECK 의
      부등식을 **조용히 통과할 수 있어** 항등식이 깨진 행이 남는다.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise InvalidInspectionOutcome(
            f"{칸} 은 Decimal 이어야 한다 (받은 것: {value!r} · {type(value).__name__})."
            " float 은 이진 오차를 수량에 남긴다."
        )
    if not value.is_finite():
        raise InvalidInspectionOutcome(f"{칸} 이 유한한 수가 아니다: {value!r}")
    return value


def validate_outcome(outcome: InspectionOutcome) -> None:
    """DB CHECK 와 **같은 규칙**을 쓰기 전에 건다. 순수 계산이다.

    ```text
    inspected > 0 · accepted/hold/reject >= 0
    accepted + hold + reject = inspected          ← 항등식
    PASS    hold = 0 · reject = 0
    HOLD    hold > 0
    REJECT  accepted = 0 · reject > 0
    ```

    🔴 **값을 고쳐 맞추지 않는다.** 합이 안 맞으면 어느 쪽이 맞는지 우리가 모른다.
    """
    if outcome.verdict not in _VERDICTS:
        raise InvalidInspectionOutcome(
            f"검수 판정이 계약 어휘 밖이다: {outcome.verdict!r}. 허용: {sorted(_VERDICTS)}."
        )

    inspected = _quantity(outcome.inspected_qty_kg, 칸="inspected_qty_kg")
    accepted = _quantity(outcome.accepted_qty_kg, 칸="accepted_qty_kg")
    hold = _quantity(outcome.hold_qty_kg, 칸="hold_qty_kg")
    reject = _quantity(outcome.reject_qty_kg, 칸="reject_qty_kg")

    if inspected <= 0:
        raise InvalidInspectionOutcome(f"검수량은 0보다 커야 한다 (받은 것: {inspected})")
    for 칸, 값 in (("accepted", accepted), ("hold", hold), ("reject", reject)):
        if 값 < 0:
            raise InvalidInspectionOutcome(f"{칸} 수량이 음수다: {값}")
    if accepted + hold + reject != inspected:
        raise InvalidInspectionOutcome(
            f"수량 항등식이 깨졌다: {accepted} + {hold} + {reject} != {inspected}."
            " 어느 쪽이 맞는지 여기서 고치지 않는다."
        )

    if outcome.verdict == "PASS" and (hold != 0 or reject != 0):
        raise InvalidInspectionOutcome(f"PASS 인데 보류 {hold} · 거부 {reject} 가 있다.")
    if outcome.verdict == "HOLD" and hold <= 0:
        raise InvalidInspectionOutcome("HOLD 인데 보류 수량이 0 이다.")
    if outcome.verdict == "REJECT" and (accepted != 0 or reject <= 0):
        raise InvalidInspectionOutcome(f"REJECT 인데 수용 {accepted} · 거부 {reject} 다.")


def _cell(row: Any, index: int, name: str) -> Any:
    """row_factory 가 튜플이든 매핑이든 한 칸을 꺼낸다 (`receipts._cell` 과 같다)."""
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def find_inspection(conn: Any, *, receipt_id: str) -> InspectionRecord | None:
    """그 Receipt 의 검수 한 건. **읽기만 한다.**

    ```text
    0행      None
    1행      InspectionRecord
    2행 이상  InspectionIntegrityError    ★ 첫 행을 고르지 않는다
    ```

    ⚠️ DB 에 `receipt_id` UNIQUE 가 **없다** (실측). 그래서 여기가 유일한 방어선이다.
    """
    if not receipt_id or not receipt_id.strip():
        raise InvalidInspectionOutcome(f"검수 조회에 쓸 수 없는 receipt_id 다: {receipt_id!r}")

    schema = sql.Identifier(get_db_schema())
    query = sql.SQL(
        """
        SELECT inspection_id, verdict, inspected_qty_kg,
               accepted_qty_kg, hold_qty_kg, reject_qty_kg
        FROM {}.inbound_inspections
        WHERE receipt_id = %s
        ORDER BY inspection_id
        LIMIT {}
        """
    ).format(schema, sql.Literal(_AMBIGUITY_PROBE_LIMIT))

    with conn.cursor() as cursor:
        cursor.execute(query, (receipt_id,))
        # 🔴 `fetchone()` 을 쓰지 않는다 — 2행 이상을 조용히 첫 행으로 돌려준다.
        rows = cursor.fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        보인것 = [_cell(row, 0, "inspection_id") for row in rows]
        raise InspectionIntegrityError(
            f"한 Receipt 에 검수가 둘 이상이다 (receipt_id={receipt_id!r}): {보인것!r} …."
            " 어느 것이 진짜인지 여기서 고르지 않는다."
        )

    값 = {name: _cell(rows[0], index, name) for index, name in enumerate(_INSPECTION_COLUMNS)}
    return InspectionRecord(
        inspection_id=값["inspection_id"],
        outcome=InspectionOutcome(
            verdict=값["verdict"],
            inspected_qty_kg=값["inspected_qty_kg"],
            accepted_qty_kg=값["accepted_qty_kg"],
            hold_qty_kg=값["hold_qty_kg"],
            reject_qty_kg=값["reject_qty_kg"],
        ),
    )


def _같은_사실(기존: InspectionOutcome, 이번: InspectionOutcome) -> bool:
    """두 검수 결과가 같은 사실인가.

    ★ `Decimal` 로 비교한다 — `numeric` 이라 `10` 과 `10.000000` 이 같은 수량인데
      문자열은 다르다. 그대로 비교하면 **정상 재실행이 Conflict 로 뒤집힌다**
      (`transition._같은_사실` 이 같은 함정을 피한다).
    """
    return (
        기존.verdict == 이번.verdict
        and 기존.inspected_qty_kg == 이번.inspected_qty_kg
        and 기존.accepted_qty_kg == 이번.accepted_qty_kg
        and 기존.hold_qty_kg == 이번.hold_qty_kg
        and 기존.reject_qty_kg == 이번.reject_qty_kg
    )


def _receipt_facts(conn: Any, schema: sql.Identifier, *, receipt_id: str) -> dict[str, Any]:
    """Receipt 의 상태와 수량. **PK 로 한 행을 읽는다.**"""
    query = sql.SQL(
        """
        SELECT receipt_status, accepted_qty_kg, hold_qty_kg, rejected_qty_kg
        FROM {}.inbound_receipts
        WHERE receipt_id = %s
        """
    ).format(schema)
    이름 = ("receipt_status", "accepted_qty_kg", "hold_qty_kg", "rejected_qty_kg")

    with conn.cursor() as cursor:
        cursor.execute(query, (receipt_id,))
        rows = cursor.fetchall()

    if not rows:
        raise InspectionIntegrityError(
            f"검수를 적을 Receipt 가 없다: receipt_id={receipt_id!r}."
            " 도착 기록 없이 검수만 적지 않는다."
        )
    return {name: _cell(rows[0], index, name) for index, name in enumerate(이름)}


def _update_receipt(
    conn: Any,
    schema: sql.Identifier,
    *,
    receipt_id: str,
    outcome: InspectionOutcome,
) -> None:
    """검수 수량을 Receipt 에 옮기고 `INSPECTED` 로 넘긴다.

    ⚠️ **칸 이름이 다르다** — 검수의 `reject_qty_kg` 가 Receipt 에서는
       `rejected_qty_kg` 다 (실측).

    🔴 **위치·팔레트는 건드리지 않는다.** 그것들은 적치 단계의 사실이고,
       `PUTAWAY_DONE` 으로도 넘기지 않는다.

    ★ `updated_at = now()` 는 **DB 의 기록 시각**이다. 갱신 트리거가 없어(실측)
      여기서 직접 적는다 — 업무 시각인 `inspected_at` 과 다른 축이다.
    """
    query = sql.SQL(
        """
        UPDATE {}.inbound_receipts
        SET accepted_qty_kg = %s,
            hold_qty_kg = %s,
            rejected_qty_kg = %s,
            receipt_status = %s,
            updated_at = now()
        WHERE receipt_id = %s
        """
    ).format(schema)

    with conn.cursor() as cursor:
        cursor.execute(
            query,
            (
                outcome.accepted_qty_kg,
                outcome.hold_qty_kg,
                outcome.reject_qty_kg,
                "INSPECTED",
                receipt_id,
            ),
        )


def _receipt_수량이_같나(receipt: Mapping[str, Any], outcome: InspectionOutcome) -> bool:
    """Receipt 에 옮겨진 수량이 검수와 일치하나. `None` 은 아직 안 옮긴 것이다."""
    return (
        receipt["accepted_qty_kg"] == outcome.accepted_qty_kg
        and receipt["hold_qty_kg"] == outcome.hold_qty_kg
        and receipt["rejected_qty_kg"] == outcome.reject_qty_kg
    )


def record_inspection(
    conn: Any,
    *,
    receipt_id: str,
    inspected_at: datetime,
    inspector: str,
    outcome: InspectionOutcome,
) -> InspectionWriteResult:
    """검수 결과를 적고 Receipt 를 `INSPECTED` 로 마감한다. **멱등하다.**

    ```text
    ① 결과·인자 검증                  DB 를 안 만진다
    ② 도착 쓰기 전역 advisory lock     receipts 의 그 잠금을 그대로 쓴다
    ③ Receipt 상태 읽기 (PK)
    ④ 기존 검수 읽기 (0 · 1 · 2+)
    ⑤ 판단 → 필요할 때만 INSERT · UPDATE
    ```

    **상태기계 (MVP):**

    ```text
    상태                  검수 0행              검수 1행 (같은 사실)   검수 1행 (다른 사실)
    ARRIVED · INSPECTING  INSERT + INSPECTED    Receipt 만 맞춘다      InspectionConflict
    INSPECTED             🔴 무결성 오류         그대로 (applied=False) InspectionConflict
    PUTAWAY_DONE · CLOSED 🔴 무결성 오류         그대로 (applied=False) InspectionConflict
    어느 상태든 2행 이상   InspectionIntegrityError
    ```

    🔴 **`INSPECTED` 인데 검수 행이 0 이면 조용히 다시 만들지 않는다.** 그 상태는
       *"검수를 이미 했다"* 는 주장이고, 사실이 없는데 새로 적으면 **사라진 결과가
       있었다는 것조차 안 남는다.** 복구 경로는 스키마에도 코드에도 없다.

    ★ **`ARRIVED` 인데 검수 행이 이미 있으면 — 사실이 같을 때만 Receipt 를 맞춘다.**
      그때 쓰는 값은 정상 경로가 썼을 값과 **글자 그대로 같아서** 새 정보를 만들지
      않는다. 사실이 다르면 고치지 않고 멈춘다. 이것이 이 자리에서 방어할 수 있는
      가장 작은 행동이다.

    ⚠️ **이미 마감된 Receipt(`INSPECTED` 이상)의 상태는 되돌리지도 앞당기지도
       않는다.** 그 수량이 검수와 다르면 그것은 무결성 오류다 — 나중 단계가 이미
       그 값으로 움직였을 수 있어 덮어쓰면 안 된다.

    🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** 잠금도 트랜잭션
       수명이라 호출자의 커밋/롤백과 함께 풀린다.

    :param inspected_at: 검수 시각. **호출자가 준다** — 시계를 읽지 않는다.
        `TIMESTAMPTZ` 라 tz 를 단 값만 받는다.
    :param inspector: 검수자. **호출자가 준다** — 없는 사람을 지어내지 않는다.
    """
    # ── ① 검증 — DB 를 만나기 전에 끝낸다 ──────────────────────────────
    inspection_id = inspection_id_for(receipt_id=receipt_id)
    validate_outcome(outcome)
    if not isinstance(inspected_at, datetime):
        raise InvalidInspectionOutcome(
            f"inspected_at 은 datetime 이어야 한다 (받은 것: {inspected_at!r})."
        )
    if inspected_at.tzinfo is None or inspected_at.utcoffset() is None:
        # 🔴 naive 를 TIMESTAMPTZ 에 넣으면 세션 TimeZone 에 따라 **뜻이 달라진다.**
        raise InvalidInspectionOutcome(
            f"inspected_at 에 시간대가 없다: {inspected_at!r}."
            " TIMESTAMPTZ 라 tz 없는 값은 세션 설정에 따라 다른 시각이 된다."
        )
    if not inspector or not inspector.strip():
        raise InvalidInspectionOutcome(
            "inspector 가 비었다. NOT NULL 이고 물류가 지어내지 않는다 —"
            " 저장소에 시스템 행위자 규약이 없어 호출자가 정할 값이다."
        )

    schema = sql.Identifier(get_db_schema())

    # ── ② 잠금이 먼저다 (도착 쓰기와 같은 전역 키) ─────────────────────
    with conn.cursor() as cursor:
        lock_arrival_writes(cursor)

    # ── ③④ 잠금 안에서 두 사실을 읽는다 ───────────────────────────────
    receipt = _receipt_facts(conn, schema, receipt_id=receipt_id)
    상태 = receipt["receipt_status"]
    기존 = find_inspection(conn, receipt_id=receipt_id)

    # ── ⑤ 판단 ────────────────────────────────────────────────────────
    if 기존 is not None:
        if not _같은_사실(기존.outcome, outcome):
            raise InspectionConflict(
                f"같은 Receipt 에 다른 사실의 검수가 이미 있다 (receipt_id={receipt_id!r})."
                f" 기존={기존.outcome!r} 이번={outcome!r}."
                " 덮지도 버리지도 않는다 — 어느 쪽이 진짜인지 여기서 고를 근거가 없다."
            )
        if 상태 in _BEFORE_INSPECTION:
            # ★ 반쪽 상태를 맞춘다. 쓰는 값이 정상 경로와 **같아서** 안전하다.
            _update_receipt(conn, schema, receipt_id=receipt_id, outcome=기존.outcome)
            상태 = "INSPECTED"
        elif not _receipt_수량이_같나(receipt, 기존.outcome):
            raise InspectionIntegrityError(
                f"이미 마감된 Receipt 의 수량이 검수와 다르다"
                f" (receipt_id={receipt_id!r}, receipt_status={상태!r})."
                " 뒤 단계가 이미 그 값으로 움직였을 수 있어 덮어쓰지 않는다."
            )
        return InspectionWriteResult(
            applied=False,
            inspection_id=기존.inspection_id,
            receipt_status=상태,
            outcome=기존.outcome,
        )

    if 상태 in _INSPECTION_DONE:
        raise InspectionIntegrityError(
            f"Receipt 는 {상태!r} 인데 검수 행이 없다 (receipt_id={receipt_id!r})."
            " 검수를 이미 했다는 상태이므로 여기서 새로 적지 않는다 —"
            " 사라진 결과가 있었다는 사실조차 안 남는다."
        )
    if 상태 not in _BEFORE_INSPECTION:
        raise InspectionIntegrityError(
            f"검수를 적을 수 없는 Receipt 상태다: {상태!r} (receipt_id={receipt_id!r})."
        )

    insert_query = sql.SQL(
        """
        INSERT INTO {}.inbound_inspections (
            inspection_id, receipt_id, inspected_at, inspector, verdict,
            inspected_qty_kg, accepted_qty_kg, hold_qty_kg, reject_qty_kg, fact_source
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
    ).format(schema)

    with conn.cursor() as cursor:
        cursor.execute(
            insert_query,
            (
                inspection_id,
                receipt_id,
                inspected_at,
                inspector,
                outcome.verdict,
                outcome.inspected_qty_kg,
                outcome.accepted_qty_kg,
                outcome.hold_qty_kg,
                outcome.reject_qty_kg,
                _FACT_SOURCE_SIMULATED,
            ),
        )
    _update_receipt(conn, schema, receipt_id=receipt_id, outcome=outcome)

    return InspectionWriteResult(
        applied=True,
        inspection_id=inspection_id,
        receipt_status="INSPECTED",
        outcome=outcome,
    )
