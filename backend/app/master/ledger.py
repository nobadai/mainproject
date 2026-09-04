"""ledger.py — 승인 약정 → **매입 원장** (`purchases` · `purchase_items`)

승인이 재무 채무와 물류 입고 예정으로는 흘러갔는데, **정작 매입 원장에는 아무것도
남지 않았다.** `git grep "INSERT INTO purchases"` 가 0건이었다. 그 자리가 여기다.

```text
승인 → ApprovedCommitment → build_purchase_rows  (순수 계산 · DB 를 안 부른다)
                          → persist_purchases    (주어진 conn 으로 write · commit 안 함)
```

🔴 **왜 `transition.py` 가 아니라 새 파일인가.** 마스터 전이 경계에는 SQL 을 넣을 수
   없다 — `test_전이_모듈에_SQL_이_없다` 가 원문을 읽어 막는다. 그 검사는 분담을
   지키는 검사이므로 고치지 않는다. `transition.py` 는 **언제 부를지**만 정하고,
   무슨 값을 어느 칸에 쓸지는 이 파일이 안다.

★ **재무·물류 전이와 같은 모양이다.** `build` 는 순수, `persist` 는 `(conn, rows)`
  둘뿐이고 commit 하지 않는다. 커밋은 두 파트가 끝난 뒤 마스터가 한 번 한다.

★ **`purchases` 는 마스터 소유가 맞다.** 재무 `payables.purchase_id` 와 물류
  `inbound source_ref` 가 둘 다 승인이 만든 매입 ID 를 가리키는데, 그 ID 를 짓는
  자리는 승인을 쥔 마스터다 (`transition.purchase_id_for`). 부모 행이 없으면
  재무 FK 가 막는다 — 그래서 원장 쓰기가 재무보다 **먼저**다.

🔴 **회차 하나짜리 길만 있다.** 회차가 둘 이상이면 여기까지 오지 않는다 —
   `transition.apply_approval` 이 앞에서 멈춘다. 매입이 회차별 금액을 아직 안 보내
   어느 회차에 얼마가 걸리는지 말할 방법이 없기 때문이고, 재무 `_single_leg` 이
   이미 같은 이유로 막고 있다. **같은 사실을 두 곳이 다르게 판정하지 않게** 한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import sql

from app.finance.db import get_db_schema
from app.master.commitment import ApprovedCommitment
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

# ⚠️ **`transition` 을 모듈 맨 위에서 부르지 않는다.** 전이 경계가 이 파일을 부르고
#    (`apply_approval`), 이 파일은 그쪽이 소유한 ID 짓는 함수를 쓴다 — 양쪽 다 위에서
#    부르면 순환 import 다. ID 규칙을 여기로 복사하면 같은 사실의 주인이 둘이 되므로,
#    **주인은 그대로 두고 부르는 시점만 미룬다.**

__all__ = [
    "MASTER_PURCHASE_TYPE",
    "PurchaseLedgerNotWritable",
    "PurchaseWrite",
    "build_purchase_rows",
    "persist_purchases",
    "sim_run_id_for",
]

#: 매입 유형. `purchase_type` 에는 CHECK 가 없고 번인이 `SAFETY_STOCK_INIT` ·
#: `BURNIN_REPLENISHMENT` 를 쓴다. 승인이 만든 매입은 그 둘 중 어느 것도 아니다.
#:
#: ★ **새 축을 만들지 않고 이미 쓰는 어휘에 맞춘다.** 물류 inbound `source_ref` 가
#:   이미 `MASTER-APPROVAL:{approval_id}` 를 쓴다 — 같은 사실을 두 이름으로 부르지
#:   않으려고 그쪽 어휘를 따른다.
MASTER_PURCHASE_TYPE = "MASTER_APPROVAL"

#: `settlement_status` CHECK 는 `SETTLED · OPEN · CANCELLED` 셋이다. 승인 시점의
#: 매입은 아직 정산되지 않았다.
_OPEN = "OPEN"

#: `numeric(18,6)` 이 담는 자릿수. 여기서 미리 맞춰 두지 않으면 DB 가 반올림한 뒤에
#: `purchase_items_check` 가 걸린다.
_SCALE = Decimal("0.000001")

#: DB CHECK: `|line_amount_krw - quantity_kg × unit_price_krw_per_kg| < 0.1`.
_LINE_TOLERANCE = Decimal("0.1")


class PurchaseLedgerNotWritable(ValueError):
    """원장을 쓸 수 없다. **틀린 행을 대신 쓰지 않는다.**

    ★ 여기서 값을 맞춰 넣으면 그 순간 마스터가 남의 숫자를 만든 것이 된다.
      멈추면 `apply_approval` 이 `FAILED` 로 사유를 남긴다.
    """


def sim_run_id_for(commitment: ApprovedCommitment) -> str:
    """이 승인이 속한 시뮬레이션 실행.

    🔴 **마스터가 지어내지 않는다.** `purchases.sim_run_id` 는 NOT NULL 이고
       `sim_runs` 를 참조하는 FK 다 — 없는 키를 넣으면 FK 가 막는다.

    ⚠️ **오늘 마스터가 승인마다 들고 다니는 `sim_run_id` 는 없다.** 재무는
       `load_finance_state_row(as_of)["sim_run_id"]` 로, 물류는 runtime fixture 의
       행으로 각자 자기 것을 찾는다. 마스터가 그 둘 중 하나를 다시 읽으면 남의 조회를
       베끼는 것이 되므로, **마스터가 이미 소유한 하나뿐인 포인터**를 쓴다 —
       `ledger_repository.BURN_IN_SIM_RUN_ID` 이고 그 모듈이 *"지금은 하나뿐이라
       상수로 둔다 — 여러 개가 되면 요청 파라미터로 올린다"* 고 적어 둔 자리다.

    🔴 **실행이 둘이 되는 날 여기가 갈린다.** 그때는 마스터 실행 이력이
       `sim_run_id` 를 싣도록 계약을 세우고 이 함수가 그것을 읽어야 한다.
       상수를 늘리는 것으로 때우면 재무 채무와 매입 원장이 서로 다른 실행에 앉는다.
    """
    return BURN_IN_SIM_RUN_ID


@dataclass(frozen=True)
class PurchaseWrite:
    """승인 **회차 하나**가 만드는 매입 Header 한 행과 그 아래 품목 한 줄.

    ★ 한 덩어리로 두는 이유는 둘이 같은 사실의 앞뒤이기 때문이다 — header 만 쓰고
      품목을 빠뜨리면 `purchases.total_amount_krw` 가 `purchase_items` 합계와 갈린다.

    🔴 **`item_id` 가 여기 없다.** 그것은 `items` 표가 주인이고, 표를 읽으려면
      커넥션이 필요하다 — 순수 계산 자리에서 한글 품목명을 `ITEM-…` 로 바꾸는
      하드코딩 맵을 만들면 그 맵이 또 하나의 어휘가 되어 `items` 와 갈린다.
      여기는 **품목명을 그대로 들고** 있고, 조회는 `persist_purchases` 가 한다.
    """

    purchase_id: str
    sim_run_id: str
    purchase_date: date
    payment_due_date: date
    total_amount_krw: Decimal
    proposal_id: str
    scenario_id: str
    #: 계약 품목명(`배추` 등). `items.item_name` 으로 조회할 열쇠다.
    item_name: str
    quantity_kg: Decimal
    unit_price_krw_per_kg: Decimal
    line_amount_krw: Decimal


def build_purchase_rows(
    commitment: ApprovedCommitment, *, purchase_ids: Mapping[int, str]
) -> tuple[PurchaseWrite, ...]:
    """승인 약정을 매입 원장 행으로 옮긴다. **계산만 한다 — DB 를 부르지 않는다.**

    :param purchase_ids: 회차(`seq`) → `purchase_id` 매핑. 재무에 넘기는 것과 **같은
        매핑**이다 — 여기서 따로 지으면 `payables.purchase_id` 가 가리키는 부모 행과
        이름이 갈린다.
    :raises PurchaseLedgerNotWritable: 지급일이 없거나, 단가가 DB CHECK 를 못 지킬 때.
    """
    from app.master.transition import purchase_id_for

    legs = tuple(commitment.arrival_schedule)
    if not legs:
        # ★ **빈 것은 예외가 아니다.** 회차 일정을 못 만든 약정도 승인은 살아 있고
        #   (`commitment.notes` 가 왜 못 만들었는지 적는다), 그때 원장에 쓸 매입이
        #   **없다**는 것은 정상 상태다 — 물류 `build_next_inventory` 가 빈 목록을
        #   정상으로 보는 것과 같다. 매입일도 지급일도 여기서 지어내지 않는다.
        return ()
    if len(legs) > 1:
        # ★ 여기까지 오면 앞에서 막았어야 하는 것이 안 막힌 것이다. 조용히 첫 회차만
        #   쓰면 나머지 회차의 매입이 원장에서 사라진다.
        raise PurchaseLedgerNotWritable(
            f"회차가 {len(legs)}개다 — 회차별 금액이 없어 원장을 쓸 수 없다."
        )
    leg = legs[0]

    if leg.payment_due_date is None:
        raise PurchaseLedgerNotWritable(
            "재무 purchase_payment_days(N5) 가 없어 지급일을 만들 수 없다"
            " — purchases.payment_due_date 는 NOT NULL 이고 지어내지 않는다."
        )
    if leg.seq not in purchase_ids:
        # 🔴 매핑에 값이 하나뿐이라고 그것을 집지 않는다 (재무 `_purchase_id_for_leg`
        #    와 같은 규율). 엉뚱한 매입에 품목이 붙어도 에러가 안 난다.
        raise PurchaseLedgerNotWritable(f"{leg.seq}회차의 purchase_id 가 없다.")
    purchase_id = purchase_ids[leg.seq]
    if purchase_id != purchase_id_for(commitment, leg.seq):
        raise PurchaseLedgerNotWritable(
            f"{leg.seq}회차 purchase_id 가 승인이 짓는 값과 다르다: {purchase_id!r}"
        )

    amount = _scaled(commitment.total_amount_krw)
    quantity = _scaled(leg.qty_kg)
    total_qty = _scaled(commitment.total_qty_kg)
    if total_qty <= 0:
        raise PurchaseLedgerNotWritable("총량이 0 이하라 단가를 만들 수 없다.")

    # ★ 단가는 **총액 ÷ 총량**이다. 회차가 하나뿐이라 회차 단가와 같은 값이고,
    #   회차별 금액이 실리는 날 이 식이 회차 금액 ÷ 회차 수량으로 바뀐다.
    unit_price = _scaled(amount / total_qty)
    line_amount = amount
    drift = abs(line_amount - quantity * unit_price)
    if drift >= _LINE_TOLERANCE:
        # 🔴 **단가를 억지로 맞추지 않는다.** `numeric(18,6)` 으로 잘린 단가로는
        #    `quantity × unit_price` 가 총액을 못 맞추는 날이 있고, 그때 line 금액을
        #    곱셈 결과로 바꾸면 원장 총액이 승인 총액과 갈린다.
        raise PurchaseLedgerNotWritable(
            f"단가 자릿수로 Line 금액을 맞출 수 없다 (차 {drift}원, DB 허용 0.1 미만)."
            " 총액을 고쳐 맞추지 않는다."
        )

    return (
        PurchaseWrite(
            purchase_id=purchase_id,
            sim_run_id=sim_run_id_for(commitment),
            purchase_date=leg.purchase_date,
            payment_due_date=leg.payment_due_date,
            total_amount_krw=amount,
            proposal_id=f"PROP-{commitment.request_id}",
            scenario_id=f"SCN-{commitment.request_id}-{commitment.scenario_label}",
            item_name=leg.item,
            quantity_kg=quantity,
            unit_price_krw_per_kg=unit_price,
            line_amount_krw=line_amount,
        ),
    )


def persist_purchases(conn: Any, rows: Sequence[PurchaseWrite]) -> dict[str, int]:
    """계산된 매입 원장 행을 **부르는 쪽 커넥션으로** 기록한다.

    🔴 **commit 하지 않는다.** 커밋은 재무·물류 write 와 함께 마스터가 한 번 한다.
       여기서 커밋하면 매입만 먼저 확정되고, 뒤이어 재무가 터졌을 때 **채무 없는
       매입**이 남는다.

    ★ 같은 승인을 다시 반영해도 행이 늘지 않는다 — `purchase_id` 와
      `purchase_item_id` 가 둘 다 PK 이고 `ON CONFLICT DO NOTHING` 이 받는다.
      `purchase_id_for` 가 결정론이라 두 번째 반영도 같은 키가 나온다.

    :raises PurchaseLedgerNotWritable: 품목명을 `items` 에서 못 찾을 때.
    """
    from app.master.transition import purchase_item_id_for

    schema = sql.Identifier(get_db_schema())
    written = {"purchases": 0, "purchase_items": 0}
    with conn.cursor() as cursor:
        for row in rows:
            item_id = _item_id_of(cursor, schema, row.item_name)
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.purchases (
                        purchase_id, sim_run_id, supplier_partner_id,
                        purchase_date, payment_due_date, purchase_type,
                        source_market_label, total_amount_krw, settlement_status,
                        proposal_id, scenario_id, source_event_id, evidence_id, note
                    )
                    VALUES (
                        %s, %s, NULL,
                        %s, %s, %s,
                        NULL, %s, %s,
                        %s, %s, NULL, NULL, NULL
                    )
                    ON CONFLICT (purchase_id) DO NOTHING
                    """
                ).format(schema),
                [
                    row.purchase_id,
                    row.sim_run_id,
                    row.purchase_date,
                    row.payment_due_date,
                    MASTER_PURCHASE_TYPE,
                    row.total_amount_krw,
                    _OPEN,
                    row.proposal_id,
                    row.scenario_id,
                ],
            )
            written["purchases"] += cursor.rowcount

            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.purchase_items (
                        purchase_item_id, purchase_id, item_id, grade, market_name,
                        quantity_kg, unit_price_krw_per_kg, line_amount_krw, source_quote_id
                    )
                    VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, NULL)
                    ON CONFLICT (purchase_item_id) DO NOTHING
                    """
                ).format(schema),
                [
                    # ★ 접미사는 `item_id` 에서 `ITEM-` 을 뗀 나머지다 — 번인의
                    #   `PITEM-SAFETY-001-BAECHU` 가 그 모양이다.
                    purchase_item_id_for(row.purchase_id, _item_code_of(item_id)),
                    row.purchase_id,
                    item_id,
                    # 🔴 `grade` 는 NULL 이다. 약정이 등급을 안 싣는다 (`sourcing_plan`
                    #    은 매입 안에만 있고 약정으로 안 온다). **지어내지 않는다** —
                    #    등급 사다리(#69) 뒤의 자리다.
                    row.quantity_kg,
                    row.unit_price_krw_per_kg,
                    row.line_amount_krw,
                ],
            )
            written["purchase_items"] += cursor.rowcount
    return written


def _item_id_of(cursor: Any, schema: sql.Identifier, item_name: str) -> str:
    """계약 품목명(`배추`)을 `items.item_id`(`ITEM-BAECHU`)로.

    🔴 **하드코딩 맵을 만들지 않는다.** 그 맵이 또 하나의 어휘가 되어 `items` 표와
       갈린다. 주인은 `items` 표이고 마스터는 읽기만 한다.

    🔴 **못 찾으면 조용히 넘기지 않는다.** 물류가 오늘 같은 자리를 고쳤다 —
       *"`ITEM-BAECHU` 를 내보내면 매입이 `배추` 로 찾을 때 매칭 0건인데 에러가
       안 납니다."* 여기서 넘어가면 `item_id` 가 없는 채로 FK 에 걸리거나, 더 나쁘게
       엉뚱한 품목에 수량이 붙는다.
    """
    cursor.execute(
        sql.SQL("SELECT item_id FROM {}.items WHERE item_name = %s").format(schema),
        [item_name],
    )
    row = cursor.fetchone()
    if not row:
        raise PurchaseLedgerNotWritable(
            f"items 표에 품목명이 없다: {item_name!r} — item_id 를 지어내지 않는다."
        )
    item_id = row["item_id"] if isinstance(row, Mapping) else row[0]
    if not isinstance(item_id, str) or not item_id.strip():
        raise PurchaseLedgerNotWritable(f"items 표의 item_id 를 읽을 수 없다: {item_name!r}")
    return item_id


def _item_code_of(item_id: str) -> str:
    """`ITEM-BAECHU` → `BAECHU`. 접두사가 겹치면 `PITEM-…-ITEM-BAECHU` 가 된다."""
    prefix = "ITEM-"
    return item_id.removeprefix(prefix)


def _scaled(value: Any) -> Decimal:
    """`numeric(18,6)` 자릿수로 맞춘다.

    ★ `Decimal(str(x))` 를 쓴다. `Decimal(float)` 은 0.1 이 갖고 있는 이진 오차를
      그대로 들여와 금액에 안 보이는 꼬리를 남긴다 (물류 `transition.py` 와 같은 이유).
    """
    raw = value if isinstance(value, Decimal) else Decimal(str(value))
    return raw.quantize(_SCALE, rounding=ROUND_HALF_UP)
