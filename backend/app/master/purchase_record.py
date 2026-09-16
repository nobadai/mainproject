"""purchase_record.py — **실매입 기록**이 전이를 세운다 (설계 260915 안 A §4-3 · §4-6).

```text
[전]  판단 → 승인 ──(같은 커밋)──▶ 전이   ← 안의 계획값
[후]  판단 → 승인(선정만 · 전이 보류) → 실매입 기록 ──▶ 전이   ← 실매입 값
```

★★ **사람이 승인하면 선정만 적힌다** (`decision_service.record_decision`). 사람이
  실제로 산 값을 여기서 적는 순간, 그 값으로 기존 전이(매입 원장 · 매입채무 · 입고
  일정)가 돈다. 그 뒤 입고 → 재고 lot → 판매 · 재무 현금은 **지금 코드 그대로** 이 값을
  따라간다.

🔴 **약정을 새로 짓지 않는다.** 선정안으로 조립하던 그 경로
   (`decision_service.current_approval` → `_commitment_parts` → `build_commitment`)가
   만든 약정의 **사본에 값만 덮는다** (`commitment.with_purchase_record`).

🔴 **기록값이 선정안과 다르면 승인 때와 같은 재검증을 다시 지난다** (§4-6 ①). 기록이
   재무 Cap · 현금흐름 검증을 우회하는 문이 되면 안 된다. 통과 못 하면 기록도 전이도 없다.

🔴 **전이 코드를 안 고친다.** `transition.apply_approval` 을 그대로 부른다. 다만
   **기록 행 적재와 묶지 않는다** — 기록이 제 트랜잭션으로 먼저 커밋되고, 전이는 그
   뒤에 별도 커넥션으로 돈다 (`record_purchase` 독스트링 · 2026-09-16).

⚠️ **자동 승인(`AUTO-BACKFILL`)은 이 경로를 안 탄다** — 지금처럼 승인 즉시 계획값으로
  전이한다 (§2).
"""

from __future__ import annotations

import copy
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from psycopg import errors as pg_errors

from app.finance.db import get_connection
from app.master.commitment import ApprovedCommitment, CommitmentNotBuildable, RecordedLeg
from app.master.decision import (
    PROCUREMENT_CYCLE,
    DecisionRejected,
    PurchaseRecordIn,
    PurchaseRecordLegOut,
    PurchaseRecordOut,
    PurchaseRecordPlanOut,
    PurchaseRecordValuesOut,
    awaits_purchase_record,
)
from app.master.decision_service import (
    CurrentApproval,
    commitment_with_record,
    current_approval,
    revalidate_recorded,
)
from app.master.pending_transition_repository import ledger_purchase_ids
from app.master.purchase_record_repository import (
    insert_purchase_record_legs,
    last_closed_date,
    list_purchase_record_legs,
)
from app.master.revalidation import find_scenario
from app.master.transition import TransitionOut, apply_approval, purchase_id_prefix_for

__all__ = [
    "BEFORE_APPROVAL_MESSAGE",
    "CLOSED_DUE_DATE_MESSAGE",
    "PURCHASE_DATE_MESSAGE",
    "SAME_DAY_DUE_DATE_MESSAGE",
    "WHOLE_AMOUNT_MESSAGE",
    "WHOLE_QTY_MESSAGE",
    "get_purchase_record",
    "record_purchase",
    "recorded_scenario",
]

BEFORE_APPROVAL_MESSAGE = "승인한 날보다 앞선 매입일은 기록할 수 없습니다 — 매입일을 확인해 주세요"
"""매입일이 승인 실행 기준일보다 앞설 때 화면에 나가는 한 줄 (§4-6 ②)."""

CLOSED_DUE_DATE_MESSAGE = "지급기일이 이미 마감된 날보다 앞입니다 — 매입일을 확인해 주세요"
"""회차 지급기일이 마지막 재무 일마감일보다 앞일 때 화면에 나가는 한 줄 (§4-6 ② · 2026-09-16)."""

SAME_DAY_DUE_DATE_MESSAGE = (
    "지급기일이 이미 마감된 날과 같습니다 — 마감한 그날 승인한 그날 매입만 기록할 수 있습니다"
)
"""지급기일이 마감일과 같은데 동일일 예외(승인일 = 매입일 = 마감일)가 아닐 때의 한 줄."""

PURCHASE_DATE_MESSAGE = "매입일은 승인한 날({as_of})과 같아야 합니다 — {seq}회차"
"""첫 회차 매입일이 승인 실행 as_of 와 다를 때의 한 줄 (선검사 · 2026-09-16)."""

WHOLE_QTY_MESSAGE = "수량은 1kg 단위로 적어 주세요 — {seq}회차"
"""수량에 소수점이 있을 때의 한 줄 (선검사 · 2026-09-16)."""

WHOLE_AMOUNT_MESSAGE = "금액은 원 단위 정수로 적어 주세요 — {seq}회차"
"""금액에 소수점이 있을 때의 한 줄 (선검사 · 2026-09-16)."""

#: 승인 때 재검증을 통과로 보는 결과. `CONDITIONAL` · `FAILED` · `ERROR` 는 통과가 아니다
#: (`decision.RevalidationOutcome` 의 표).
_PASSED = "PASSED"


def _approval_to_record(request_id: str) -> CurrentApproval:
    """기록을 걸 **현재 승인**. 없으면 `LookupError` (라우터가 404)."""
    approval = current_approval(request_id)
    if approval is None:
        raise LookupError(f"업무 키 {request_id} 에 유효한 승인이 없다 — 실매입은 승인에만 적는다.")
    if approval.cycle and approval.cycle != PROCUREMENT_CYCLE:
        raise DecisionRejected(
            f"업무 키 {request_id} 의 승인은 매입 승인이 아니다 — 실매입을 적을 수 없다.",
            conflict=True,
        )
    return approval


def record_purchase(
    request_id: str,
    body: PurchaseRecordIn,
    *,
    connect: Callable[[], Any] | None = None,
    apply_fn: Callable[..., TransitionOut] = apply_approval,
) -> TransitionOut:
    """실매입을 적고 **그 값으로 전이를 세운다.**

    ```text
    검증  승인(APPROVE) 존재 · 사람 승인 · 아직 기록 없음 · 회차 집합 == 선정안 회차 집합
          선검사  첫 회차 매입일 == 승인 실행 as_of · 수량 · 금액이 정수
          회차마다 매입일 >= 승인 실행 as_of · 지급기일 > 마지막 재무 일마감일 (같은 날은 예외 하나)
    재검증 기록값이 선정안과 하나라도 다르면 · 기록값 안 사본으로 · PASSED 가 아니면 멈춘다
    ①    master_purchase_records 에 회차 행 — **제 커넥션 · 제 커밋**
    ②    선정안 약정 사본에 기록값을 덮는다
    ③    apply_approval(사본) — ① 이 커밋된 **뒤** · **별도 커넥션**
    ```

    🔴 **기록과 전이는 두 트랜잭션이다** (2026-09-16 · `#729`). 전에는 한 커넥션으로
       묶었는데, `apply_approval` 은 적재하다 터지면 그 커넥션을 `rollback` 하므로
       **전이가 실패하면 기록 행까지 사라졌다.**

       ★ 그런데 **전이 실패는 예외가 아니라 정상 경로다.** 전이는 언제나 하루 앞
         (도착일)의 물류 runtime fixture 행을 보는데 그 행은 다음 개장에 열린다 —
         그래서 **승인 당일의 전이는 언제나 `FAILED` 이고 다음 날 「미적용 전이
         재시도」가 세운다** (`pending_transition.py` 머리말).

       ```text
       실측  dev@983c85b · SIM-CHECK-HOLIDAY-0916 · 2026-04-13 무
         POST .../purchase-record → 201 {"status":"FAILED","reason":"전이 적재 실패:
           갱신할 물류 runtime fixture 행이 없다 (as_of=2026-04-14)..."}
         SELECT ... FROM master_purchase_records WHERE sim_run_id='SIM-CHECK-HOLIDAY-0916'
           → 0행                      🔴 사람이 적은 사실이 지워졌다
         다음 날 걷기: 미적용 전이 재시도 NOTHING_DUE — 기록이 없어 재시도 대상에서도
           빠지고(`#718`) 매입은 영영 안 선다
       ```

       ★ **기록은 사람이 진술한 사실이고 전이는 그 귀결이다.** 장부가 아직 준비되지
         않았다는 이유로 사람의 진술이 지워지면 안 된다. 그 상태는 설계에 이미 있다 —
         `PurchaseRecordStatus.NOT_APPLIED`("기록 있음 · 아직 원장에 없다 → 다음 개장
         뒤 재시도가 기록값으로", `decision.py`).

    ★ 그래서 **전이가 무엇을 돌려주든 기록은 남는다** — 커넥션 앞에서 돌아서든
      (`NOT_APPLIED`) 적재하다 롤백하든(`FAILED`). 사람은 다시 보내지 않고, 다음
      개장 뒤 재시도가 기록값으로 세운다.
    🔴 **기록 삽입 자체가 실패하면 전이를 안 부른다** — 없는 기록의 귀결은 없다.
      이미 기록된 승인이면 `UniqueViolation` 이 409 로 접힌다 (커밋이 앞당겨져도 같다).

    :raises LookupError: 승인이 없다 (404).
    :raises DecisionRejected: 지금 상태에서 받을 수 없다(409) · 기록이 선정안과 안 맞다 ·
        마감된 날짜다 · 재검증을 통과하지 못했다(422).
    """
    approval = _approval_to_record(request_id)
    decision = approval.decision
    if body.decision_seq != decision.decision_seq:
        raise DecisionRejected(
            f"회차 {body.decision_seq} 는 현재 승인이 아니다"
            f" (현재 승인 회차 {decision.decision_seq}).",
            conflict=True,
        )
    if not awaits_purchase_record(decision.decided_by):
        raise DecisionRejected(
            "자동 승인은 실매입 기록 대상이 아니다 — 승인 즉시 계획값으로 반영된다.",
            conflict=True,
        )
    sim_run_id = approval.sim_run_id
    if sim_run_id is None:
        raise DecisionRejected(
            "원 실행의 sim_run_id 를 못 읽어 어느 장부에 반영할지 정할 수 없다.",
            conflict=True,
        )
    plan = approval.plan
    if plan is None:
        reason = approval.plan_out.reason if approval.plan_out is not None else None
        raise DecisionRejected(
            f"선정안 약정이 서지 않아 기록할 회차가 없다: {reason or '사유 없음'}",
            conflict=True,
        )
    if list_purchase_record_legs(
        sim_run_id=sim_run_id, request_id=request_id, decision_seq=decision.decision_seq
    ):
        raise DecisionRejected(
            f"이 승인(회차 {decision.decision_seq})에는 이미 실매입이 기록됐다 — 고쳐 쓰지 않는다.",
            conflict=True,
        )

    legs = tuple(
        RecordedLeg(
            seq=leg.seq,
            qty_kg=leg.qty_kg,
            amount_krw=leg.amount_krw,
            purchase_date=leg.purchase_date,
            arrival_date=leg.arrival_date,
        )
        for leg in body.legs
    )
    # ★ **선검사가 재검증보다 앞이다** — 재검증이 계약에서 떨어지면 사람에게는
    #   「재검증 통과 못 함」만 남는다 (2026-09-16).
    _check_recordable_values(approval, legs)
    grade = body.grade.strip()
    try:
        recorded = commitment_with_record(approval, legs, grade)
    except CommitmentNotBuildable as exc:
        raise DecisionRejected(str(exc)) from exc
    # ★ 경계는 **덮은 약정**으로 잰다 — 지급기일의 주인이 약정 덮기(`with_purchase_record`)다.
    _check_purchase_dates(approval, recorded, sim_run_id=sim_run_id)

    if differs_from_plan(plan, legs, grade):
        _revalidate_or_reject(approval, legs, grade)

    open_connection = get_connection if connect is None else connect
    # ① 기록을 **먼저 · 제 트랜잭션으로** 커밋한다. 이 커넥션은 전이에 넘기지 않는다 —
    #    넘기면 전이의 rollback 이 기록까지 되감는다 (위 독스트링 실측).
    conn = open_connection()
    try:
        insert_purchase_record_legs(
            conn,
            sim_run_id=sim_run_id,
            request_id=request_id,
            decision_seq=decision.decision_seq,
            grade=grade,
            recorded_by=body.recorded_by.strip(),
            legs=legs,
        )
        conn.commit()
    except pg_errors.UniqueViolation as exc:
        conn.rollback()
        raise DecisionRejected(
            f"이 승인(회차 {decision.decision_seq})에는 이미 실매입이 기록됐다.",
            conflict=True,
        ) from exc
    except Exception:
        # 🔴 기록이 안 앉았으면 전이를 안 부른다 — 없는 기록의 귀결은 없다.
        conn.rollback()
        raise
    finally:
        conn.close()

    # ③ 전이는 **커밋된 기록 뒤에** 제 커넥션으로 돈다. 실패해도 기록은 남고,
    #    다음 개장 뒤 「미적용 전이 재시도」가 기록값으로 세운다.
    return apply_fn(recorded, sim_run_id=sim_run_id, connect=connect)


def _check_recordable_values(approval: CurrentApproval, legs: Sequence[RecordedLeg]) -> None:
    """**선검사** — 재검증이 알기 어려운 말로 막기 전에 사람 말로 거부한다 (2026-09-16).

    ```text
    첫 회차 매입일 == 승인 실행 as_of
    회차마다 수량 · 금액이 정수
    ```

    ★ **왜 매입일을 승인일에 묶는가.** 기록값 재검증은 안 사본을 매입안 계약
      (`purchase_agent.schemas.PurchaseProposal.validate_proposal_rules`)으로 다시 읽는데,
      그 계약이 `split_plan[0].date == meta.as_of` 를 요구한다. 사본의 `meta.as_of` 는
      **승인 실행의 as_of** 라(`decision_service.revalidate_recorded`), 사람이 첫 회차
      매입일을 승인일과 다른 날로 적으면 계약이 그 자리에서 떨어진다. 그때 사람이
      보는 것은 「재검증 통과 못 함」뿐이라 무엇을 고쳐야 하는지 알 수 없다.

      ★ 계약이 묶는 것은 **첫 회차 하나뿐이다.** 2회차 이후 매입일은 선정안대로
        뒤 날짜여도 된다 — 여기서 같이 묶으면 분할 선정안을 그대로 기록하는 것조차
        막힌다.

      🔴 **발표 뒤 과제 — 계약을 넓힌다.** 실매입은 승인한 날과 다른 날에도 일어날 수
        있다. 옳은 자리는 「기록 사본의 as_of 를 기록 매입일로 싣는다」이거나 「재검증
        경로에서 첫 회차 날짜 규칙을 푼다」이고, 둘 다 매입 계약을 건드린다. 발표
        전에는 입구에서 막아 사람이 알아볼 수 있는 말을 듣게 한다.

    ★ **왜 정수인가.** 매입안 계약의 수량 · 등급 단가 칸이 정수라(`SourcingPlanItem`),
      소수점 수량 · 금액은 재검증에서 떨어진다. 반올림해 통과시키면 **기록한 값과 다른
      값이 검증을 지난다** — `#727` 이 그래서 반올림 대신 막아 두었고 그 판단을 유지한다.

    ⚠️ **승인 실행 as_of 를 못 읽으면 매입일은 안 잰다.** 못 잰 것을 틀렸다고 하지 않는다
      — 뒤의 경계(`_check_purchase_dates`)도 같은 규율이다.
    """
    for leg in legs:
        if not float(leg.qty_kg).is_integer():
            raise DecisionRejected(WHOLE_QTY_MESSAGE.format(seq=leg.seq))
        if not float(leg.amount_krw).is_integer():
            raise DecisionRejected(WHOLE_AMOUNT_MESSAGE.format(seq=leg.seq))
    as_of = approval.as_of
    if as_of is None or not legs:
        return
    # ★ 안의 `split_plan[0]` 에 얹히는 회차가 첫 회차다 (`recorded_scenario` 는 seq 로 짝짓고,
    #   계약의 `validate_split_sequence` 가 seq 를 1부터 세게 한다).
    first = min(legs, key=lambda one: one.seq)
    if first.purchase_date != as_of:
        raise DecisionRejected(PURCHASE_DATE_MESSAGE.format(as_of=as_of, seq=first.seq))


def _check_purchase_dates(
    approval: CurrentApproval, recorded: ApprovedCommitment, *, sim_run_id: str
) -> None:
    """매입일 경계 (§4-6 ② · 2026-09-16 변경). **회차마다 두 줄.**

    ```text
    매입일   >= 승인 실행 as_of
    지급기일 >  그 sim_run_id 의 마지막 재무 일마감일   (지급기일 = 매입일 + N5)
    지급기일 == 마지막 마감일   승인 기준일 == 매입일 == 마지막 마감일 일 때만 받는다
    ```

    ★ **매입일이 아니라 지급기일로 마감일과 견준다** (마스터 확정 · 재무 요청 취지).
      걷기가 D 를 마감한 뒤 사람이 D 매입을 승인 · 기록하는 것이 정상 순서다. 막아야
      하는 것은 *"이미 지난 지급기일의 채무가 새로 생기는 것"* 이다 — 그 채무는 마감된
      날의 지급에 한 번도 안 잡힌다.

    ★ **D 마감 뒤 입력되는 동일일 실매입만 예외다** (2026-09-16 재무 합의). 재무 마감
      (`finance/closing._recognize_due_payables`)은 `issued_date <= as_of AND due_date <= as_of`
      이고 아직 마감 사건이 없는 채무를 **다음 마감에서 한 번** 반영한다. 그래서 D 에
      승인한 D 매입(D+0 지급)은 현금이 D+1 마감에서 한 번 잡힌다. 🔴 그 밖의 동일일
      (과거 승인 · N5 가 있어 지급기일이 마침 마감일인 경우)은 거부한다.

    ★ **D 마감 숫자는 흔들리지 않는다.** 전이(`finance/transition.py`)는 as_of 날 상태를
      읽기만 하고 `as_of + 1` 상태에 쓴다 (`master/transition._target_state_date`).

    ★ **지급기일의 주인은 덮은 약정이다** (`ArrivalLeg.payment_due_date`). 여기서 N5 를
      다시 더하지 않는다.

    🔴 **마감이 있는데 지급기일을 모르면 거부한다.** 못 잰 것을 통과로 두지 않는다.
    """
    as_of = approval.as_of
    closed = last_closed_date(sim_run_id=sim_run_id)
    for leg in recorded.arrival_schedule:
        if as_of is not None and leg.purchase_date < as_of:
            raise DecisionRejected(
                f"{BEFORE_APPROVAL_MESSAGE}"
                f" ({leg.seq}회차 매입일 {leg.purchase_date} · 승인 기준일 {as_of})"
            )
        if closed is None:
            continue
        due = leg.payment_due_date
        if due is None:
            raise DecisionRejected(
                "지급기일을 계산할 수 없어 마감 여부를 확인하지 못했습니다"
                f" — 재무 지급 일수를 확인해 주세요 ({leg.seq}회차 · 마지막 마감일 {closed})"
            )
        if due < closed:
            raise DecisionRejected(
                f"{CLOSED_DUE_DATE_MESSAGE} ({leg.seq}회차 지급기일 {due} · 마지막 마감일 {closed})"
            )
        if due == closed and not (as_of == leg.purchase_date == closed):
            raise DecisionRejected(
                f"{SAME_DAY_DUE_DATE_MESSAGE}"
                f" ({leg.seq}회차 승인 기준일 {as_of} · 매입일 {leg.purchase_date}"
                f" · 지급기일 {due} · 마지막 마감일 {closed})"
            )


def differs_from_plan(plan: ApprovedCommitment, legs: Sequence[RecordedLeg], grade: str) -> bool:
    """기록값이 선정안과 **하나라도 다른가.** 같으면 재검증을 생략한다 (§4-6 ①).

    ★ 등급은 NFC 로 비교한다 (`ApprovedCommitment.grades` 와 같은 규율). 선정안 등급이
      하나가 아니면 다른 것으로 본다 — 한 기록 = 한 등급이다.
    """
    planned = {leg.seq: leg for leg in plan.arrival_schedule}
    for one in legs:
        leg = planned.get(one.seq)
        if leg is None:
            return True
        if (
            leg.qty_kg != one.qty_kg
            or leg.amount_krw != one.amount_krw
            or leg.purchase_date != one.purchase_date
            or leg.arrival_date != one.arrival_date
        ):
            return True
    grades = plan.grades
    return len(grades) != 1 or unicodedata.normalize("NFC", grades[0]) != unicodedata.normalize(
        "NFC", grade
    )


def _revalidate_or_reject(
    approval: CurrentApproval, legs: Sequence[RecordedLeg], grade: str
) -> None:
    """기록값 안 사본으로 재검증한다. **`PASSED` 가 아니면 422** — 저장도 전이도 없다."""
    label = approval.decision.scenario_label or ""
    scenario = find_scenario(approval.response_payload, label)
    if scenario is None:
        raise DecisionRejected(
            f"승인한 안 '{label}' 을 원 실행에서 유일하게 찾지 못해 기록값을 재검증할 수 없다.",
            conflict=True,
        )
    copied = recorded_scenario(
        scenario,
        legs=legs,
        grade=grade,
        purchase_payment_days=approval.purchase_payment_days,
    )
    result = revalidate_recorded(approval, copied)
    if result.outcome != _PASSED:
        raise DecisionRejected(
            f"기록값으로 다시 검증했더니 통과하지 못해 기록하지 않았습니다"
            f" ({result.outcome}): {result.reason or '사유 없음'}"
        )


def _as_number(value: float) -> int | float:
    """정수로 떨어지면 정수로 싣는다 — 매입 안의 수량 · 금액 칸이 정수다."""
    return int(value) if float(value).is_integer() else value


def _grade_lines(total_qty: float, total_amount: float) -> list[tuple[int | float, int | float]]:
    """기록 총량 · 총액을 **등급 배분 줄**로 편다. `(수량, 단가)` 목록이다.

    ```text
    계약   sourcing_plan[].grade_unit_price 는 정수 원/kg (`SourcingPlanItem`)
    검사   total_amount_krw == Σ(qty_kg × grade_unit_price)   (`Scenario.validate_quadruple_match`)
    ```

    🔴 **금액을 고치지 않는다.** 기록 총액은 사람이 실제로 낸 돈이고 그것이 정본이다.
      그래서 총액 ÷ 총량이 정수로 안 떨어지면 **단가를 반올림해 총액을 흔드는 대신**
      나머지를 한 줄 더 얹는다.

      ```text
      300kg · 271,000원   →  (197kg × 903) + (103kg × 904) = 271,000
      ```

      ★ 합이 **정확히** 총액이다 — `divmod` 의 몫과 나머지를 그대로 쓴다. 새 업무
        숫자를 만든 것이 아니라, 기록한 한 사실을 계약이 요구하는 정수 단가 모양으로
        적은 것이다.

    ⚠️ **등급이 둘이 되는 것이 아니다.** 두 줄 다 기록한 그 등급이고, 부르는 쪽이
      등급 이름을 얹는다 — 여기는 수량과 단가만 센다.

    🔴 **정수가 아닌 기록은 그대로 흘린다** (kg 에 소수점이 있는 경우). 매입 계약의
      수량 · 금액 칸이 정수라 부서 파싱에서 걸리는데, **여기서 반올림해 통과시키면
      기록값과 다른 값이 검증을 지난다.** 못 적는 것은 못 적는 대로 막힌다.

      ⚠️ 이 갈래는 이제 사실상 안 불린다 — 입구(`_check_recordable_values`)가 소수점
        수량 · 금액을 사람 말로 먼저 거부한다 (2026-09-16). **마지막 방어선으로 남긴다**:
        입구를 안 지나는 부름이 생겨도 반올림한 값이 조용히 검증을 지나면 안 된다.
    """
    if not (float(total_qty).is_integer() and float(total_amount).is_integer()):
        return [(total_qty, total_amount / total_qty if total_qty else total_amount)]
    qty = int(total_qty)
    unit, rest = divmod(int(total_amount), qty)
    if rest == 0:
        return [(qty, unit)]
    # `rest < qty` 라 앞 줄 수량은 항상 1 이상이다.
    return [(qty - rest, unit), (rest, unit + 1)]


def recorded_scenario(
    scenario: Mapping[str, Any],
    *,
    legs: Sequence[RecordedLeg],
    grade: str,
    purchase_payment_days: Any,
) -> dict[str, Any]:
    """선정안 **안 사본**에 기록값을 맞춘다. 🔴 **원본을 안 건드린다.**

    ```text
    split_plan[]         qty_kg · amount_krw · date(매입일) · expected_arrival_date
    total_qty_kg · total_amount_krw   기록 회차 합
    payment_schedule[]   purchase_date · payment_date(매입일 + N5) · qty_kg · amount_krw
                         (amount_max_krw 는 qty_kg × max_price 로 다시 센다 · 안에 있을 때만)
    sourcing_plan[]      grade · qty_kg · grade_unit_price 를 기록값으로 다시 놓는다
    ```

    ⚠️ **없는 칸을 만들지 않는다.** `payment_schedule` 이 없는 안(일괄 1회차)에는 싣지
      않는다 — 재무가 `split_plan` 에서 재구성한다.

    🔴 **`sourcing_plan` 은 줄을 다시 놓는다** (2026-09-16 실측 · `#722` 뒤). 전에는
      `grade` 만 덮고 줄이 하나일 때만 `qty_kg` 를 총량으로 바꿨다. `grade_unit_price`
      는 선정안 값 그대로였으므로 **기록 총액과 등급 배분 금액이 어긋났고**, 두 부서가
      이 payload 를 `PurchaseProposal` 로 파싱하다 그 자리에서 떨어졌다.

      ```text
      실측  SIM-TEST-PURREC-0916 · 01-05 배추 · 300kg 270,000원 기록
            finance   ERROR  payload {"validation_errors": ["scenarios.0"]}
            inventory RUNTIME_NOT_READY  missing_data ["purchase_proposal"]
            Scenario.validate_quadruple_match
              「total_amount_krw must equal sourcing_plan amount total」
      ```

      ★ **한 기록 = 한 등급이다** (`differs_from_plan` 과 같은 규율). 그래서 기록값
        배분은 그 등급 한 줄이고, 총액이 정수 단가로 안 떨어질 때만 나머지 줄이 하나
        더 붙는다 (`_grade_lines`).
    """
    by_seq = {leg.seq: leg for leg in legs}
    out: dict[str, Any] = copy.deepcopy(dict(scenario))
    # ★ N5 는 약정 덮기(`with_purchase_record`)가 이미 일수로 읽히는지 막았다.
    days = (
        int(purchase_payment_days)
        if isinstance(purchase_payment_days, (int, float))
        and not isinstance(purchase_payment_days, bool)
        else None
    )

    split_plan = out.get("split_plan")
    if isinstance(split_plan, list):
        for index, raw in enumerate(split_plan, 1):
            if not isinstance(raw, dict):
                continue
            one = by_seq.get(int(raw.get("seq") or index))
            if one is None:
                continue
            raw["qty_kg"] = _as_number(one.qty_kg)
            raw["amount_krw"] = _as_number(one.amount_krw)
            raw["date"] = one.purchase_date.isoformat()
            raw["expected_arrival_date"] = one.arrival_date.isoformat()

    total_qty = sum(leg.qty_kg for leg in legs)
    out["total_qty_kg"] = _as_number(total_qty)
    out["total_amount_krw"] = _as_number(sum(leg.amount_krw for leg in legs))

    schedule = out.get("payment_schedule")
    max_price = out.get("max_price")
    if isinstance(schedule, list):
        for index, raw in enumerate(schedule, 1):
            if not isinstance(raw, dict):
                continue
            one = by_seq.get(int(raw.get("seq") or index))
            if one is None:
                continue
            raw["purchase_date"] = one.purchase_date.isoformat()
            if days is not None:
                raw["payment_date"] = (one.purchase_date + timedelta(days=days)).isoformat()
            raw["qty_kg"] = _as_number(one.qty_kg)
            raw["amount_krw"] = _as_number(one.amount_krw)
            if "amount_max_krw" in raw and isinstance(max_price, (int, float)):
                raw["amount_max_krw"] = _as_number(one.qty_kg * max_price)

    sourcing = out.get("sourcing_plan")
    if isinstance(sourcing, list):
        lines = [line for line in sourcing if isinstance(line, dict)]
        if lines:
            # ★ 등급 밖의 칸(지금은 `market`)은 선정안 첫 줄에서 그대로 온다 — 기록이
            #   시장을 바꾸지 않는다 (`PurchaseRecordIn` 은 시장을 받지 않는다).
            keep = {
                key: value
                for key, value in lines[0].items()
                if key not in {"grade", "qty_kg", "grade_unit_price"}
            }
            out["sourcing_plan"] = [
                {
                    **keep,
                    "grade": grade,
                    "qty_kg": _as_number(qty),
                    "grade_unit_price": _as_number(unit),
                }
                for qty, unit in _grade_lines(total_qty, sum(leg.amount_krw for leg in legs))
            ]
    return out


def get_purchase_record(request_id: str) -> PurchaseRecordOut:
    """화면용 — 선정안 회차(기본값) · 기록(있으면) · 반영 상태.

    :raises LookupError: 승인이 없다 (404).
    """
    approval = _approval_to_record(request_id)
    decision = approval.decision
    plan = approval.plan
    plan_out = PurchaseRecordPlanOut(
        grade=plan.grades[0] if plan is not None and plan.grades else None,
        legs=[
            PurchaseRecordLegOut(
                seq=leg.seq,
                qty_kg=leg.qty_kg,
                amount_krw=leg.amount_krw,
                purchase_date=leg.purchase_date,
                arrival_date=leg.arrival_date,
            )
            for leg in (plan.arrival_schedule if plan is not None else ())
        ],
    )
    base = {
        "request_id": request_id,
        "decision_seq": decision.decision_seq,
        "scenario_label": decision.scenario_label,
        "decided_by": decision.decided_by,
        "plan": plan_out,
    }
    if not awaits_purchase_record(decision.decided_by):
        return PurchaseRecordOut(
            **base,
            status="NOT_REQUIRED",
            reason="자동 승인은 실매입 기록 없이 계획값으로 반영됩니다",
        )

    sim_run_id = approval.sim_run_id
    rows = (
        list_purchase_record_legs(
            sim_run_id=sim_run_id, request_id=request_id, decision_seq=decision.decision_seq
        )
        if sim_run_id is not None
        else []
    )
    if not rows or sim_run_id is None:
        reason = "실매입을 기록하면 반영됩니다"
        if plan is None and approval.plan_out is not None and approval.plan_out.reason:
            reason = f"선정안 약정이 서지 않았다: {approval.plan_out.reason}"
        return PurchaseRecordOut(**base, status="AWAITING_PURCHASE_RECORD", reason=reason)

    record = PurchaseRecordValuesOut(
        grade=str(rows[0]["grade"]),
        recorded_by=str(rows[0]["recorded_by"]),
        recorded_at=rows[0]["recorded_at"],
        legs=[
            PurchaseRecordLegOut(
                seq=int(row["leg_seq"]),
                qty_kg=float(row["quantity_kg"]),
                amount_krw=float(row["amount_krw"]),
                purchase_date=row["purchase_date"],
                arrival_date=row["arrival_date"],
            )
            for row in rows
        ],
    )
    prefix = purchase_id_prefix_for(request_id, decision.decision_seq)
    applied = any(one.startswith(prefix) for one in ledger_purchase_ids(sim_run_id=sim_run_id))
    if applied:
        return PurchaseRecordOut(**base, status="APPLIED", record=record)
    return PurchaseRecordOut(
        **base,
        status="NOT_APPLIED",
        reason=(
            "기록은 남았고 아직 매입 원장에 반영되지 않았습니다"
            " · 다음 개장 뒤 재시도가 기록값으로 다시 반영합니다"
        ),
        record=record,
    )
