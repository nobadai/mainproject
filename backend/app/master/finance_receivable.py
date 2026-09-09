"""
finance_receivable.py — 마스터 `ReceivableSource` 를 재무 채권 구현체에 잇는 배선.

🔴 **이 파일이 하는 일은 셋이다.**

```text
① 재무 축을 물어본다                     financing_mode 를 마스터가 고르지 않는다
② 그날 확정된 판매를 읽는다               sales 가 정본이다
③ 한 건씩 confirm_receivable 에 넘긴다     채권 원장은 재무 것이다
```

  마스터 Protocol 은 `as_of` 만 나르는데 `ReceivableCreateInput` 은 일곱 칸을
  요구한다. 그 일곱의 **주인이 각각 누구인가**가 이 파일의 전부다.

---

🔴 **값의 주인.**

```text
sim_run_id            🟢 마스터가 정한다 — ledger_repository.BURN_IN_SIM_RUN_ID 하나
financing_mode        🔴 **마스터가 고르지 않는다** — get_finance_runtime_axis() 로 묻는다
sale_date             sales.sale_date
customer_partner_id   sales.customer_partner_id
due_date              🔴 **sales.collection_due_date 를 읽는다**
original_amount_krw   sales.total_amount_krw
sale_id               sales.sale_id
```

  ⚠️ **여기에 `financing_mode` 를 상수로 박으면 안 된다.** 실측으로 `finance_states`
    에 `LOAN_BASELINE` 과 `BASE_NO_LOAN` 이 **공존한다**. 마스터가 하나를 골라 박으면
    *"무차입 상태가 대출 baseline 자리에 조용히 들어오는"* 사고가 나고, 그 사고는
    에러 없이 숫자만 바꾼다 — `FinanceCollectionAdapter` 가 같은 문장을 적어 뒀다.

  🔴 **`due_date` 를 계산하지 않는다.** *"sale_date + payment_days"* 는 판매·재무의
    계약값이다. 그 칸이 비어 있으면 **지어내지 않고 `BLOCKED`** 로 세운다 — 채권의
    기일을 마스터가 발명하면 그 값으로 수금 판정이 돌고, 틀려도 에러가 안 난다.

🔴 **임포트 시점에 DB 를 읽지 않는다.** 축 조회도 판매 조회도 `issue()` **안에서**
  일어난다. 배선이 DB 를 요구하기 시작하면 앱이 뜨는 조건이 조용히 늘어난다.

---

🔴 **축의 `sim_run_id` 가 마스터 것과 다르면 막는다 (fail-closed).**

  재무가 읽은 축이 다른 실행을 가리키는데 마스터 값으로 덮어 쓰고 진행하면 **남의
  실행 장부에 채권을 세운다.** 그것은 에러 없이 남의 채권 잔액을 늘린다.

★ **`BLOCKED` 다.** `NOTHING_DUE` 로 접으면 *"오늘은 판 게 없었다"* 로 읽히고, 서
  있어야 할 채권이 없는 채로 매입 판단이 돈다.

---

🔴 **대상 판매에 `DELIVERED` 를 넣는다.**

```text
WHERE sale_date = as_of
  AND order_status IN ('CONFIRMED', 'READY', 'DELIVERED')
```

  ⚠️ **빼면 다시 걸었을 때 채권이 영영 안 선다.** 하루 실행의 출고 단계가 같은 날
    안에서 `CONFIRMED → DELIVERED` 로 바꾸므로, 두 번째 걸음에서는 그 판매가 대상
    목록에서 사라진다. 첫 걸음에 채권이 안 섰으면 그 뒤로는 기회가 없다.

  🔴 **`outbound_flow.due_sale_items` 와 같은 목록을 쓰지 않는다.** 그쪽은
    *"오늘 나가야 하는가"* 라 `CONFIRMED · READY` 만 본다. 판정 대상이 다르다 —
    한 함수로 묶으면 한쪽 규칙을 고치는 날 다른 쪽이 조용히 바뀐다.

  ⚠️ `CANCELLED` 는 넣지 않는다. 취소된 판매에 채권을 세우면 **없는 돈이 매입 cap 을
    늘린다.**

---

★ **`ReceivablePersistenceConflict` 는 `BLOCKED` 이고 나머지 예외는 올린다.**

```text
ReceivablePersistenceConflict   재무가 "이 사실은 이렇게 못 적는다" 고 말한 것 → BLOCKED
그 밖의 예외                     무엇이 터졌는지 모른다 → 올린다 → 마스터가 롤백하고 FAILED
```

  🔴 **왜 가르나.** 앞은 재무가 **쿼리를 정상으로 마치고** 낸 판정이라 트랜잭션이
    살아 있고, 다음 판매를 계속 볼 수 있다. 뒤는 DB 오류일 수 있고 그때는 트랜잭션이
    이미 죽어 있어 **다음 문장이 전부 같은 오류로 줄줄이 터진다** — 그것을 건별
    사유로 적으면 사고 하나가 사고 여럿으로 보인다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import sql

from app.finance.db import (
    FinanceDataNotReady,
    FinanceRuntimeAxis,
    get_db_schema,
    get_finance_runtime_axis,
)
from app.finance.receivables import ReceivablePersistenceConflict, confirm_receivable
from app.finance.sales_validation import ReceivableCreateInput
from app.master.receivable import ReceivablePartOut

__all__ = [
    "ISSUABLE_ORDER_STATUSES",
    "ConfirmedSale",
    "FinanceReceivableAdapter",
    "read_confirmed_sales",
]

#: 🔴 **채권을 세울 판매 상태.** `DELIVERED` 가 들어 있는 것이 계약이다.
#:
#: ⚠️ 상수로 둔 이유는 검사가 이 값을 읽기 때문이다. SQL 문자열 안에만 있으면
#:   `DELIVERED` 가 빠진 날을 실 DB 없이는 아무도 못 잡는다.
ISSUABLE_ORDER_STATUSES: tuple[str, ...] = ("CONFIRMED", "READY", "DELIVERED")


@dataclass(frozen=True)
class ConfirmedSale:
    """그날 확정된 판매 한 줄. **판매가 소유한 사실을 읽어 온 것뿐이다.**

    ⚠️ `collection_due_date` 가 `date | None` 인 것이 계약이다. `None` 은 *"기일을
      모른다"* 이고 0 도 오늘도 아니다 — 여기서 기본값을 채우면 **마스터가 결제조건을
      발명하는 것**이 된다.
    """

    sale_id: str
    sim_run_id: str
    sale_date: date
    customer_partner_id: str
    collection_due_date: date | None
    total_amount_krw: Decimal


def read_confirmed_sales(
    conn: Any, *, as_of: date, sim_run_id: str
) -> tuple[ConfirmedSale, ...]:
    """`as_of` 가 `sale_date` 인 **이 실행의** 확정 판매. **`DELIVERED` 도 대상이다.**

    🔴 **`sim_run_id` 로 거른다** (마스터 판단 2026-09-09). 안 거르면 남의 실행 판매가
      같은 `sale_date` 에 들어왔을 때 `confirm_receivable` 이 conflict 를 내고 **그날
      전체가 `BLOCKED`** 가 된다. 남의 축 판매는 *"못 만든 것"* 이 아니라 **애초에 내
      대상이 아니다** — 그 둘을 한 값으로 접으면 막힌 날을 나중에 설명할 수 없다.

    ★ **마스터 커넥션으로 읽는다.** 채권을 쓰는 트랜잭션과 같은 커넥션이라야, 읽은
      판매와 쓴 채권이 같은 스냅샷 위에 선다 (`outbound_flow.due_sale_items` 와 같은
      모양이다).

    🔴 **못 읽으면 예외를 그대로 올린다.** `()` 로 접으면 *"오늘 확정된 판매가 없다"*
      와 구별할 수 없다. 접는 판단은 부르는 쪽 몫이다.
    """
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                SELECT sale_id,
                       sim_run_id,
                       sale_date,
                       customer_partner_id,
                       collection_due_date,
                       total_amount_krw
                  FROM {}.sales
                 WHERE sale_date = %s
                   AND sim_run_id = %s
                   AND order_status = ANY(%s)
                 ORDER BY sale_id
                """
            ).format(schema),
            [as_of, sim_run_id, list(ISSUABLE_ORDER_STATUSES)],
        )
        rows = cursor.fetchall()
    return tuple(
        ConfirmedSale(
            sale_id=row["sale_id"],
            sim_run_id=row["sim_run_id"],
            sale_date=row["sale_date"],
            customer_partner_id=row["customer_partner_id"],
            collection_due_date=row["collection_due_date"],
            total_amount_krw=Decimal(row["total_amount_krw"]),
        )
        for row in rows
    )


#: 판매를 읽는 방법의 모양. 커넥션과 날짜를 받아 그날 확정분을 준다.
LoadSales = Callable[..., tuple[ConfirmedSale, ...]]


@dataclass
class FinanceReceivableAdapter:
    """`ReceivableSource` 구현. **재무 축을 물어보고 그날 확정분을 재무에 넘긴다.**

    :param sim_run_id: 마스터가 정한 실행. `BURN_IN_SIM_RUN_ID` 하나가 주인이다.
    :param read_axis: 재무 축을 읽는 방법. 기본값이 재무 함수 그대로이고, 검사가
        대역을 끼울 자리다. **마스터가 축을 계산하는 자리가 아니다.**
    :param load_sales: 그날 확정 판매를 읽는 방법. **호출마다 다시 읽는다** — 배선
        시점에 고정하면 판매가 한 줄 들어와도 앱을 다시 띄우기 전까지 아무 일도 안
        일어난다.
    :param confirm: 채권을 세우는 재무 경계. 기본값이 `confirm_receivable` 자체다 —
        **마스터가 원장에 직접 쓰지 않는다.**
    """

    sim_run_id: str
    read_axis: Callable[[], FinanceRuntimeAxis] = field(default=get_finance_runtime_axis)
    load_sales: LoadSales = field(default=read_confirmed_sales)
    confirm: Callable[..., Any] = field(default=confirm_receivable)

    def issue(self, conn: Any, *, as_of: date) -> ReceivablePartOut:
        """재무 축을 읽고 그날 확정 판매를 한 건씩 `confirm_receivable` 에 넘긴다."""
        try:
            axis = self.read_axis()
        except (FinanceDataNotReady, LookupError, ValueError) as exc:
            # ★ **사유를 그대로 옮긴다.** `finance_runtime_axis_ambiguous` 가 여기서
            #   사라지면 *"막혔다"* 만 남고 무엇이 모호했는지가 없어진다.
            return ReceivablePartOut(
                part="finance",
                status="BLOCKED",
                reason=f"재무 축을 읽지 못했다: {exc}",
            )

        if axis["sim_run_id"] != self.sim_run_id:
            # 🔴 **덮어 쓰지 않는다.** 남의 실행 장부에 조용히 채권을 세우는 자리다.
            return ReceivablePartOut(
                part="finance",
                status="BLOCKED",
                reason=(
                    "실행 축이 다르다: 마스터 sim_run_id="
                    f"{self.sim_run_id!r}, 재무 축 sim_run_id={axis['sim_run_id']!r}"
                ),
            )

        try:
            sales = self.load_sales(conn, as_of=as_of, sim_run_id=self.sim_run_id)
        except Exception as exc:  # noqa: BLE001 - 조회 실패를 `()` 로 접지 않는다.
            # 🔴 **없는 것과 못 읽은 것은 다르다.** `()` 로 접으면 표를 못 읽은 날이
            #   *"오늘은 판 게 없었다"* 로 읽힌다.
            return ReceivablePartOut(
                part="finance",
                status="BLOCKED",
                reason=f"확정 판매를 읽지 못했다: {type(exc).__name__}: {exc}",
            )

        if not sales:
            # ★ **`NOTHING_DUE` 다.** 확인했고 그날 확정된 판매가 없었다.
            return ReceivablePartOut(part="finance", status="NOTHING_DUE")

        issued: list[str] = []
        created = 0
        blocked: list[str] = []
        for sale in sales:
            if sale.collection_due_date is None:
                # 🔴 **지어내지 않는다.** 기일은 판매·재무의 계약값이다.
                blocked.append(f"{sale.sale_id}: collection_due_date 가 비어 있다")
                continue
            request = ReceivableCreateInput(
                sale_id=sale.sale_id,
                # 🟢 **마스터가 정한다.**
                sim_run_id=self.sim_run_id,
                # 🔴 **고르지 않는다. 재무가 읽은 값 그대로다.**
                financing_mode=axis["financing_mode"],
                sale_date=sale.sale_date,
                customer_partner_id=sale.customer_partner_id,
                # 🔴 **판매가 정한 기일 그대로다.**
                due_date=sale.collection_due_date,
                original_amount_krw=sale.total_amount_krw,
            )
            try:
                result = self.confirm(conn, request)
            except ReceivablePersistenceConflict as exc:
                # ★ 재무가 정상으로 마치고 낸 판정이다 — 트랜잭션이 살아 있으므로
                #   다음 판매를 계속 본다.
                blocked.append(f"{sale.sale_id}: {exc}")
                continue
            issued.append(result.receivable_id)
            # ⚠️ **멱등이라 0 일 수 있다.** 0 은 *"이미 서 있었다"* 이고 실패가 아니다.
            created += int(result.receivables_written)

        if blocked:
            # 🔴 **하나라도 못 세웠으면 `BLOCKED` 다.** 나머지가 섰다고 지나가면
            #    `receivables_krw` 가 적게 잡힌 채로 매입 판단이 돈다.
            return ReceivablePartOut(
                part="finance",
                status="BLOCKED",
                reason="채권을 못 세운 판매가 있다: " + " · ".join(blocked),
                issued=issued,
                created=created,
            )
        return ReceivablePartOut(
            part="finance",
            status="ISSUED",
            issued=issued,
            created=created,
        )
