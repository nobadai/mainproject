"""승인된 매입 약정 → **다음 재무 Actual State.**

```text
T0 상태 읽기 → 승인 약정 수신 → build_finance_transition (계산만)
             → persist_finance_transition(conn, ...)  ← 연결은 부르는 쪽 것
             → T1 실행이 as_of 로 그 결과를 읽는다
```

이 파일이 소유하는 것
    재무 상태 전이 계산 · 지급 시점 해석 · 재무 원장 변경 SQL

여기 **없는 것**
    트랜잭션 경계 · commit · rollback · 물류 재고 전이 · 승인 시점 결정

★ **값은 부서가, 트랜잭션은 마스터가.** `persist_finance_transition` 은 연결을
  받기만 하고 열지도 닫지도 않는다 — 재무 쓰기와 물류 쓰기가 한 번에 서거나 한
  번에 물러나야 하는데, 그 판단은 이 파일이 할 수 없다.

🔴 **승인은 현금을 줄이지 않는다.** 승인 시점에 생기는 것은 **매입채무**다.
   N5=0(당일 지급)이어도 마찬가지다 — 매입 회차가 미래이면 그날 만기가 서고,
   현금은 실제 지급일에 나간다. 승인일에 현금을 깎으면 아직 나가지 않은 돈이
   사라지고, 정작 지급일에는 아무 일도 일어나지 않는다.

🔴 **다음 상태가 설 날을 재무가 정하지 않는다.** `target_state_date` 를 **인자로
   받는다** — 재무는 그 날짜가 승인일보다 뒤인지만 본다.

   ★ **토요일 상태는 정상이다.** 매입 판단은 평일만 돌지만 장부는 매 달력일
     전진한다 — 주말에도 판매와 원장 활동이 일어나기 때문이다. 그래서 다음 상태
     날짜는 `as_of + 1 달력일`(토·일·공휴일 포함)이고, 재무가 **평일로 미루지
     않는다.** 예전 주석이 이 자리를 "다음 실행일" 로 적었는데 그건 틀렸다 —
     실행일과 장부일은 다른 축이다.

   🔴 그래서 `master.execution_day.next_execution_day` 를 쓰지 않는다. 재무는
     그 모듈을 import 하지도 않고 평일 계산을 하지도 않는다. 날짜는 마스터가 준다.

🔴 **어댑터는 섰지만 아직 등록하지 않는다.** `FinanceTransitionAdapter` 는 마스터
   Protocol 모양에 맞지만, `register_transition("finance", ...)` 은 이 브랜치가
   하지 않는다. `payables.purchase_id` 가 참조하는 `purchases` 부모 행을 재무
   persist **전에** 누가 넣는지가 아직 안 서 있고, 물류 쪽 연결도 남아 있다.
   재무만 먼저 등록하면 마스터가 *"아직 안 돈다"* (`NOT_APPLIED`) 대신 **승인마다
   `FAILED`** 를 내게 된다 — 미구현이 장애로 둔갑한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from psycopg import Connection, sql

from app.finance.db import (
    FinanceDataNotReady,
    get_active_finance_policy,
    get_db_schema,
    load_finance_state_row,
)

__all__ = [
    "ApprovedCommitmentFacts",
    "FinancePayableWrite",
    "FinanceTransitionAdapter",
    "FinanceTransitionPlan",
    "build_finance_transition",
    "persist_finance_transition",
]


class ArrivalLegFacts(Protocol):
    seq: int
    purchase_date: date


class ApprovedCommitmentFacts(Protocol):
    """승인 약정에서 **재무가 읽는 것만**.

    ★ 정본은 마스터의 `ApprovedCommitment` 다. 재무는 그 모듈을 import 하지 않는다 —
      재무가 닿아도 되는 마스터 표면은 공유 계약(`envelope` · `critic_bridge`)뿐이고,
      그 선은 `test_finance_sales_orchestration_boundary` 가 지킨다. 여기 적힌 것은
      복제한 모델이 아니라 **의존하는 필드 목록**이다.
    """

    approval_id: str
    as_of: date
    total_amount_krw: float
    arrival_schedule: Sequence[ArrivalLegFacts]


#: 승인 전이가 만든 상태임을 상태 행에 남긴다. `state_type` 은 재무 소유 컬럼이고
#: 공유 CHECK 도 enum 도 없다 — 다만 `DAY30` 을 그대로 물려주면 승인으로 생긴 행이
#: 번인 마감처럼 읽힌다.
H1_STATE_TYPE = "H1_COMMITMENT"


@dataclass(frozen=True)
class FinancePayableWrite:
    """승인이 만드는 매입채무 한 건. **금액도 날짜도 지어내지 않는다.**"""

    payable_id: str
    sim_run_id: str
    purchase_id: str
    issued_date: date
    #: **계약 만기일**이다. 주말이어도 그대로 원장에 남는다 — 현금이 실제로 나가는
    #: 날은 `tools.effective_cash_date` 가 조회 시점에 정한다.
    due_date: date
    amount_krw: Decimal


@dataclass(frozen=True)
class FinanceTransitionPlan:
    """승인 1건이 만드는 재무 변경 전부. **아직 아무것도 쓰지 않았다.**"""

    approval_id: str
    sim_run_id: str
    #: 이 계산이 딛고 선 T0 행. 나머지 컬럼은 여기서 그대로 이어 간다.
    source_finance_state_id: str
    next_finance_state_id: str
    #: 다음 상태가 서는 날. **부르는 쪽이 준 값**이다 — 재무가 세지 않는다.
    #: 달력일이라 토·일도 그대로 선다.
    next_state_date: date
    payables: tuple[FinancePayableWrite, ...]
    #: 승인 뒤 미결제 매입채무 총액. 현금은 **바뀌지 않는다.**
    next_unsettled_purchase_payables_krw: Decimal

    @property
    def payable_total_krw(self) -> Decimal:
        return sum((row.amount_krw for row in self.payables), Decimal(0))


def build_finance_transition(
    commitment: ApprovedCommitmentFacts,
    *,
    target_state_date: date,
    purchase_ids: Mapping[int, str],
) -> FinanceTransitionPlan:
    """승인 약정을 재무 변경으로 옮긴다. **계산만 한다 — DB 를 바꾸지 않는다.**

    :param target_state_date: 승인 결과 상태가 설 날. **마스터가 준다.**
        마스터 계약상 `commitment.as_of + 1 달력일` 이고 토·일·공휴일도 그대로다 —
        재무는 계산하지 않고 승인일보다 뒤인지만 본다.
    :param purchase_ids: 회차(`seq`) → `purchase_id` 매핑. **마스터가 만든다.**
        `payables.purchase_id` 는 `purchases` 를 참조하는 NOT NULL 컬럼이라 재무가
        지어낼 수 없다. 재무는 자기 회차의 값을 **`seq` 로 찾아 쓰기만** 한다 —
        하나뿐이라고 첫 값을 집거나 정렬해서 고르지 않는다.
    :raises FinanceDataNotReady: 재무가 지급 시점이나 금액을 확정할 수 없을 때.
    """
    if target_state_date <= commitment.as_of:
        # 재무 정합성 조건이지 일정 규칙이 아니다 — 승인일 이하로 두면 T0 행과 같은
        # 날에 두 상태가 서고, 그다음부터 어느 쪽이 그날의 사실인지 말할 수 없다.
        raise ValueError("target_state_date must be after the approval as_of")

    state = load_finance_state_row(commitment.as_of)
    if state["state_date"] != commitment.as_of:
        # 승인일 잔액을 다른 날 잔액으로 대신 계산하지 않는다.
        raise FinanceDataNotReady("historical_finance_position")

    policy = get_active_finance_policy()
    if policy.purchase_payment_days is None:
        raise FinanceDataNotReady("purchase_payment_days")

    amount = Decimal(str(commitment.total_amount_krw))
    if amount <= 0:
        raise FinanceDataNotReady("commitment_total_amount")

    leg = _single_leg(commitment)
    purchase_id = _purchase_id_for_leg(purchase_ids, leg.seq)
    purchase_date = leg.purchase_date
    # N5 는 **계약 지급일까지의 달력일수**다 (현재 0 = 매입 당일). 주말 보정은 여기서
    # 하지 않는다 — 원장은 계약일을 그대로 들고, 현금이 나가는 날은 현금흐름 조회가
    # `tools.effective_cash_date` 로 정한다.
    due_date = purchase_date + timedelta(days=int(policy.purchase_payment_days))

    sim_run_id = str(state["sim_run_id"])
    payable = FinancePayableWrite(
        payable_id=f"AP-{commitment.approval_id}",
        sim_run_id=sim_run_id,
        purchase_id=purchase_id,
        issued_date=commitment.as_of,
        due_date=due_date,
        amount_krw=amount,
    )
    return FinanceTransitionPlan(
        approval_id=commitment.approval_id,
        sim_run_id=sim_run_id,
        source_finance_state_id=str(state["finance_state_id"]),
        next_finance_state_id=f"FIN-{commitment.approval_id}",
        next_state_date=target_state_date,
        payables=(payable,),
        next_unsettled_purchase_payables_krw=(
            Decimal(str(state["unsettled_purchase_payables_krw"])) + amount
        ),
    )


def _single_leg(commitment: ApprovedCommitmentFacts) -> ArrivalLegFacts:
    """채무를 세울 회차. **지금은 하나일 때만 세울 수 있다.**

    🔴 **회차가 둘 이상이면 매입일이 같아도 합치지 않는다.** 마스터 계약상 회차마다
       `purchases` 한 행이 서므로 회차가 둘이면 매입 의무도 구조적으로 둘이다.
       그런데 약정이 드는 금액은 `total_amount_krw` 하나뿐이라 어느 회차에 얼마가
       걸리는지 말할 방법이 없다. 수량 비율로 쪼개면 회차마다 단가가 다른 분할
       매입에서 조용히 틀린 채무가 생긴다 — 회차별 지급액을 실어 주는 계약이
       설 때까지 여기는 닫혀 있다.

    🔴 **회차가 없으면 채무도 없다.** 회차가 없으면 `purchase_ids` 도 비어 있고,
       재무는 매입 ID 를 지어낼 수 없다. 승인일을 매입일로 대신 쓰던 예전 경로는
       이제 없는 ID 로 채무를 세우게 되므로 막는다.

    ★ **`commitment_purchase_ids` 와 합치지 않는다.** 여기서 없는 것은 매핑이 아니라
      **일정 자체**다 — 매핑이 빈 것은 결과이지 원인이 아니고, 원인은 대개 매입이
      N4 미결로 일정을 못 만든 것이다(`commitment.notes` 가 그 사유를 든다).
      매핑 쪽을 가리키면 읽는 사람이 마스터를 먼저 뒤진다. 같은 코드 모양을
      `capabilities/scenario.py` 가 이미 이렇게 나눠 둔다 — 구조가 없는 것은
      `scenario_split_plan`, 그 안의 칸이 없는 것은 `scenario_split_plan_date`.
    """
    legs = tuple(commitment.arrival_schedule)
    if not legs:
        raise FinanceDataNotReady("commitment_arrival_schedule")
    if len(legs) > 1:
        raise FinanceDataNotReady("commitment_payment_amounts")
    return legs[0]


def _purchase_id_for_leg(purchase_ids: Mapping[int, str], seq: int) -> str:
    """이 회차의 매입 ID. **없으면 다른 값으로 대신하지 않는다.**

    🔴 매핑에 값이 하나뿐이라고 그것을 집으면, 마스터가 다른 회차 ID 를 실어 준
       날에 **엉뚱한 매입에 채무가 붙는다.** 에러 없이 원장만 어긋난다.
    """
    if seq not in purchase_ids:
        raise FinanceDataNotReady("commitment_purchase_ids")
    purchase_id = purchase_ids[seq]
    if not isinstance(purchase_id, str) or not purchase_id.strip():
        raise FinanceDataNotReady("commitment_purchase_ids")
    return purchase_id


def persist_finance_transition(
    conn: Connection[dict[str, object]], transition: FinanceTransitionPlan
) -> dict[str, int]:
    """재무 소유 원장을 **부르는 쪽 연결로** 기록한다.

    ★ commit 도 rollback 도 하지 않는다 — 승인 트랜잭션은 부르는 쪽 것이다.
      물류 쓰기가 뒤에서 실패하면 이 쓰기도 함께 물러나야 한다.

    ★ 같은 승인을 다시 적용해도 새 의무가 생기지 않는다. `finance_states` 는 PK,
      `payables` 는 `purchase_id` UNIQUE 가 DB 에서 막는다 — 두 번째 적용은
      쓴 행 수 0 으로 돌아온다.
    """
    schema = sql.Identifier(get_db_schema())
    written = {"finance_states": 0, "payables": 0}
    with conn.cursor() as cursor:
        for payable in transition.payables:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.payables (
                        payable_id, sim_run_id, purchase_id, issued_date, due_date,
                        original_amount_krw, paid_amount_krw, outstanding_amount_krw, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 0, %s, 'OPEN')
                    ON CONFLICT (purchase_id) DO NOTHING
                    """
                ).format(schema),
                [
                    payable.payable_id,
                    payable.sim_run_id,
                    payable.purchase_id,
                    payable.issued_date,
                    payable.due_date,
                    payable.amount_krw,
                    payable.amount_krw,
                ],
            )
            written["payables"] += cursor.rowcount

        # ★ 나머지 컬럼은 **원천 행에서 그대로 이어 간다.** 재고 평가액처럼 재무가
        #   만들지 않는 값을 여기서 다시 적으면, 옮겨 적는 순간 남의 숫자가 된다.
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {schema}.finance_states (
                    finance_state_id, sim_run_id, state_date, state_type, financing_mode,
                    current_cash_krw, minimum_operating_cash_krw, committed_outflows_krw,
                    unsettled_purchase_payables_krw, receivables_krw,
                    inventory_book_value_krw, operational_inventory_value_krw,
                    current_debt_krw, recommended_loan_amount_krw, note
                )
                SELECT
                    %s, sim_run_id, %s, %s, financing_mode,
                    current_cash_krw, minimum_operating_cash_krw, committed_outflows_krw,
                    %s, receivables_krw,
                    inventory_book_value_krw, operational_inventory_value_krw,
                    current_debt_krw, recommended_loan_amount_krw, %s
                FROM {schema}.finance_states
                WHERE finance_state_id = %s
                ON CONFLICT (finance_state_id) DO NOTHING
                """
            ).format(schema=schema),
            [
                transition.next_finance_state_id,
                transition.next_state_date,
                H1_STATE_TYPE,
                transition.next_unsettled_purchase_payables_krw,
                f"H1 승인 {transition.approval_id} 반영",
                transition.source_finance_state_id,
            ],
        )
        written["finance_states"] += cursor.rowcount
    return written


class FinanceTransitionAdapter:
    """마스터 전이 Protocol 이 부르는 재무 쪽 얇은 입구.

    ★ **여기에는 업무가 없다.** 계산은 `build_finance_transition`, 쓰기는
      `persist_finance_transition` 이 한다. 이 클래스가 하는 일은 마스터가 쓰는
      호출 모양에 이름을 맞춰 주는 것뿐이다 — 얇게 두어야 계약이 바뀔 때 고칠
      자리가 한 곳으로 남는다.

    ★ **마스터를 import 하지 않는다.** Protocol 은 구조적 타이핑이라 상속이 필요
      없고, 재무가 닿아도 되는 마스터 표면은 공유 계약뿐이다.

    🔴 연결을 열지 않고 commit·rollback 도 하지 않는다. 승인 트랜잭션은 마스터 것이다.
    """

    def build(
        self,
        commitment: ApprovedCommitmentFacts,
        *,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
    ) -> FinanceTransitionPlan:
        return build_finance_transition(
            commitment,
            target_state_date=target_state_date,
            purchase_ids=purchase_ids,
        )

    def persist(
        self, conn: Connection[dict[str, object]], row: FinanceTransitionPlan
    ) -> dict[str, int]:
        return persist_finance_transition(conn, row)
