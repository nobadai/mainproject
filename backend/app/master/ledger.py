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

🔴 **다회차 원장은 회차 금액이 다 실려 있을 때만 연다** (2026-09-08). 전에는 회차가
   둘 이상이면 **무조건** 막았다 — 매입이 회차별 금액을 안 보내던 시절의 규칙이다.
   지금은 매입이 `#265` 로 회차 금액을 싣고(seq1 6,182,450 + seq2 6,180,800 = total),
   재무 `_payment_legs`(`app/finance/transition.py:207`)가 **조건부**로 지나가며,
   물류가 `purchase_ids.get(leg.seq)` 로 회차별 매입 ID 를 받는다. 마스터 한 곳만
   막고 있었다. 새 규칙은 **모든 leg 에 `amount_krw` 와 `payment_due_date` 가 다
   있을 때만 연다**이고, 하나라도 없으면 **어느 seq 가 비었는지 이름을 대고** 멈춘다.

   ⚠️ **이 길로 값이 지나간 적이 한 번도 없다** (매입 실측 2026-09-08).
   `purchases` 20행 중 관통 승인분 4행은 전부 `-S1` 이고 번인 seed 16행은 회차
   접미사 자체가 없다. 분할 게이트도 `by_volume 0/31 · by_trend 0/81` 로 한 번도
   열리지 않았다. **여기는 새로 여는 길이고, 처음 밟히는 길이다.**

★ **막는 문장의 주인은 `ledger_block_reason` 하나다.** `transition._ledger_blocked`
  가 앞에서 그것을 부르고 `build_purchase_rows` 가 최후 방어로 다시 부른다 —
  같은 사실을 두 곳이 다른 문장으로 말하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import sql

from app.finance.db import get_db_schema
from app.master.commitment import ApprovedCommitment, ArrivalLeg

# ⚠️ **`transition` 을 모듈 맨 위에서 부르지 않는다.** 전이 경계가 이 파일을 부르고
#    (`apply_approval`), 이 파일은 그쪽이 소유한 ID 짓는 함수를 쓴다 — 양쪽 다 위에서
#    부르면 순환 import 다. ID 규칙을 여기로 복사하면 같은 사실의 주인이 둘이 되므로,
#    **주인은 그대로 두고 부르는 시점만 미룬다.**

__all__ = [
    "MASTER_PURCHASE_TYPE",
    "PurchaseLedgerNotWritable",
    "PurchaseWrite",
    "build_purchase_rows",
    "cancel_purchases",
    "ledger_block_reason",
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


def _seq_목록(seqs: Sequence[int]) -> str:
    """비어 있는 회차를 **이름으로** 부른다.

    🔴 *"회차별 금액이 아직 없다"* 처럼 뭉뚱그린 문장을 쓰지 않는다. 회차가 넷인데
       둘만 비어 있을 때 그 문장은 어느 것을 채워야 하는지 말해 주지 않는다.
    """
    return ", ".join(str(seq) for seq in seqs)


def ledger_block_reason(commitment: ApprovedCommitment) -> str:
    """매입 원장을 쓸 수 없는 사유. 쓸 수 있으면 **빈 문자열**이다.

    ★ **이 문장의 주인은 여기 하나다.** `transition._ledger_blocked` 가 앞에서
      부르고 `build_purchase_rows` 가 최후 방어로 다시 부른다.

    🔴 **회차가 둘 이상이면 회차마다 금액이 있어야 한다.** 없는 회차를 총액이나
       수량 비율로 채우면 회차마다 단가가 다른 분할 매입에서 **조용히 틀린 원장**이
       생긴다. 재무 `_payment_legs`(`app/finance/transition.py:207`)가 같은 자리를
       같은 조건으로 막는다 — 마스터가 앞에서 먼저 멈추는 것뿐이다.

    ★ **회차가 하나면 금액이 없어도 막지 않는다.** 축이 하나뿐이라 총액이 곧 그
      회차 금액이고, 이것이 회차 금액을 안 싣던 옛 입력이 지나가던 길이다.
      재무가 `len(legs) > 1` 을 조건으로 붙여 같은 보존을 한다.

    🔴 **지급일이 없으면 쓰지 않는다.** `purchases.payment_due_date` 는 NOT NULL 이고
       그 값의 근거는 재무 N5 다. 없는 날짜를 지어내지 않는다.

    ★ 회차가 **하나도 없는** 경우는 여기서 가르지 않는다 — 그건 원장 이전에 재무가
      `commitment_arrival_schedule` 로 먼저 막는 상태이고, 그 사유를 여기서 다시
      쓰면 같은 사실이 두 문장으로 나간다.
    """
    legs = tuple(commitment.arrival_schedule)
    if len(legs) > 1:
        빈금액 = [leg.seq for leg in legs if leg.amount_krw is None]
        if 빈금액:
            return (
                f"{_seq_목록(빈금액)}회차 금액이 없어 원장을 쓸 수 없다"
                " — 매입이 그 회차 금액을 아직 안 보냈다"
            )
    빈지급일 = [leg.seq for leg in legs if leg.payment_due_date is None]
    if 빈지급일:
        return (
            f"{_seq_목록(빈지급일)}회차 지급일이 없다"
            " — 재무 purchase_payment_days(N5) 가 없어 만들 수 없다"
        )
    return ""


def sim_run_id_for(commitment: ApprovedCommitment, *, sim_run_id: str | None) -> str:
    """이 승인이 속한 시뮬레이션 실행. **받은 축을 돌려준다.**

    🔴 **마스터가 지어내지 않는다.** `purchases.sim_run_id` 는 NOT NULL 이고
       `sim_runs` 를 참조하는 FK 다 — 없는 키를 넣으면 FK 가 막는다.

    ★★ **그 날이 왔다** (2026-09-10). 이 함수는 *"실행이 둘이 되는 날 여기가 갈린다 —
      그때는 마스터 실행 이력이 `sim_run_id` 를 싣도록 계약을 세우고 이 함수가 그것을
      읽어야 한다"* 고 적어 두었었다. `master_agent_runs.sim_run_id` 가 그 계약이고
      (`Refs #150` · 2026-09-08), `decision_service.record_decision` 이 결정이 걸린
      실행 행에서 그 값을 읽어 여기까지 흘린다.

    ```text
    ~2026-09-09   BURN_IN_SIM_RUN_ID 를 돌려준다        실행이 하나뿐이었다
    2026-09-10~   받은 축을 돌려준다                     안 받으면 터진다
    ```

    🔴 **상수로 메우지 않는다.** 축을 못 받았을 때 조용히 번인으로 떨어지면 **재무
       채무와 매입 원장이 서로 다른 실행에 앉는다** — 재무는 자기 축(`finance_states`)
       을 읽고 여기만 번인을 쓰기 때문이고, 그 어긋남은 아무 오류도 안 낸다.

    ⚠️ **`commitment` 을 여전히 받는다.** 지금은 안 읽지만 *"이 승인의 축"* 이라는
      물음이 그대로이고, 부르는 쪽이 승인마다 축을 짚는다는 사실이 인자에 남아야 한다.

    :param sim_run_id: 이 결정이 걸린 실행. 🔴 **기본값이 없다.**
    :raises ValueError: 축이 비었을 때. **막고 사유를 낸다.**
    """
    if not sim_run_id or not sim_run_id.strip():
        raise ValueError(
            "sim_run_id 없이 매입 원장을 쓸 수 없다 — 어느 실행의 장부인지가 없으면"
            " 재무 채무와 다른 실행에 앉는다. 상수로 메우지 않는다"
        )
    return sim_run_id


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
    commitment: ApprovedCommitment, *, purchase_ids: Mapping[int, str], sim_run_id: str | None
) -> tuple[PurchaseWrite, ...]:
    """승인 약정을 매입 원장 행으로 옮긴다. **계산만 한다 — DB 를 부르지 않는다.**

    ★ **회차마다 한 행이다.** `purchases.purchase_date` 가 header 에 하나뿐이라
      매입일이 다른 회차를 한 header 에 담을 수 없다 (`transition.purchase_id_for`).

    :param purchase_ids: 회차(`seq`) → `purchase_id` 매핑. 재무에 넘기는 것과 **같은
        매핑**이다 — 여기서 따로 지으면 `payables.purchase_id` 가 가리키는 부모 행과
        이름이 갈린다.
    :param sim_run_id: 어느 실행의 장부인가. 🔴 **받아서 흘린다** — 여기서 상수를
        읽지 않는다 (`sim_run_id_for` 의 근거).
    :raises PurchaseLedgerNotWritable: 회차 금액이나 지급일이 없거나, 단가가
        DB CHECK 를 못 지킬 때.
    :raises ValueError: 축을 못 받았을 때.
    """
    legs = tuple(commitment.arrival_schedule)
    if not legs:
        # ★ **빈 것은 예외가 아니다.** 회차 일정을 못 만든 약정도 승인은 살아 있고
        #   (`commitment.notes` 가 왜 못 만들었는지 적는다), 그때 원장에 쓸 매입이
        #   **없다**는 것은 정상 상태다 — 물류 `build_next_inventory` 가 빈 목록을
        #   정상으로 보는 것과 같다. 매입일도 지급일도 여기서 지어내지 않는다.
        return ()
    # ★ **최후 방어다.** `transition._ledger_blocked` 가 같은 함수를 앞에서 이미
    #   불렀다. 그래도 여기서 다시 부르는 이유는 원장 계산이 전이 밖에서도 불릴 수
    #   있기 때문이고, 두 자리가 **같은 문장**을 쓰므로 판정이 갈리지 않는다.
    blocked = ledger_block_reason(commitment)
    if blocked:
        raise PurchaseLedgerNotWritable(blocked)

    축 = sim_run_id_for(commitment, sim_run_id=sim_run_id)
    return tuple(
        _row_for_leg(commitment, leg, purchase_ids=purchase_ids, sim_run_id=축) for leg in legs
    )


def _row_for_leg(
    commitment: ApprovedCommitment,
    leg: ArrivalLeg,
    *,
    purchase_ids: Mapping[int, str],
    sim_run_id: str,
) -> PurchaseWrite:
    """회차 하나가 만드는 매입 Header 한 행과 품목 한 줄."""
    from app.master.transition import purchase_id_for

    if leg.seq not in purchase_ids:
        # 🔴 매핑에 값이 하나뿐이라고 그것을 집지 않는다 (재무 `_purchase_id_for_leg`
        #    와 같은 규율). 엉뚱한 매입에 품목이 붙어도 에러가 안 난다.
        raise PurchaseLedgerNotWritable(f"{leg.seq}회차의 purchase_id 가 없다.")
    purchase_id = purchase_ids[leg.seq]
    if purchase_id != purchase_id_for(commitment, leg.seq):
        raise PurchaseLedgerNotWritable(
            f"{leg.seq}회차 purchase_id 가 승인이 짓는 값과 다르다: {purchase_id!r}"
        )

    quantity = _scaled(leg.qty_kg)
    if leg.amount_krw is None:
        # ★ **회차가 하나뿐인 옛 입력만 여기로 온다** (`ledger_block_reason` 이 다회차의
        #   빈 금액을 위에서 이미 막았다). 축이 하나뿐이라 총액이 곧 그 회차 금액이고,
        #   재무 `_payment_legs`(`app/finance/transition.py:207`)가 같은 보존을 한다.
        amount = _scaled(commitment.total_amount_krw)
        나눌_수량 = _scaled(commitment.total_qty_kg)
    else:
        # 🔴 **다회차에서 총액을 쓰지 않는다.** 회차마다 총액이 실리면 원장이 승인
        #    총액의 회차 수만큼 부풀어 오르고, 재무 채무 합과도 갈린다. 금액과 수량을
        #    **같은 축에서** 집는다 — 회차 금액 ÷ 회차 수량이다.
        amount = _scaled(leg.amount_krw)
        나눌_수량 = quantity
    if 나눌_수량 <= 0:
        raise PurchaseLedgerNotWritable(f"{leg.seq}회차 수량이 0 이하라 단가를 만들 수 없다.")

    # ⚠️ **회차 금액 합 = 총액은 여기서 다시 세지 않는다.** `commitment.__post_init__`
    #    이 이미 본다 (`commitment.py:139-141`). 두 곳이 세면 허용 오차가 갈리는 날
    #    같은 약정을 한 곳은 통과시키고 한 곳은 막는다.
    unit_price = _scaled(amount / 나눌_수량)
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

    if leg.payment_due_date is None:
        # ★ 여기까지 오면 `build_purchase_rows` 의 최후 방어를 지나쳐 들어온 것이다.
        #   사유 문장은 지어내지 않고 **주인에게 다시 묻는다.**
        raise PurchaseLedgerNotWritable(ledger_block_reason(commitment))

    return PurchaseWrite(
        purchase_id=purchase_id,
        sim_run_id=sim_run_id,
        purchase_date=leg.purchase_date,
        payment_due_date=leg.payment_due_date,
        total_amount_krw=amount,
        proposal_id=f"PROP-{commitment.request_id}",
        scenario_id=f"SCN-{commitment.request_id}-{commitment.scenario_label}",
        item_name=leg.item,
        quantity_kg=quantity,
        unit_price_krw_per_kg=unit_price,
        line_amount_krw=line_amount,
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


def cancel_purchases(conn: Any, purchase_ids: Iterable[str]) -> int:
    """이 승인이 만든 매입 header 를 `CANCELLED` 로 **적는다.**

    🔴 **DELETE 하지 않는다.** 승인이 있었다는 사실이 사라지면 *"승인했고 취소했다"*
       를 아무도 말할 수 없다 — `master_decisions` 가 append-only 인 것과 같은 규율이다.
       `purchases.settlement_status` 에 `CANCELLED` 칸이 이미 있다 (실 DB CHECK 실측).

    🔴 **commit 하지 않는다.** 커밋은 재무·물류 취소와 함께 마스터가 한 번 한다.
       여기서 커밋하면 매입만 먼저 물리고, 뒤이어 재무가 터졌을 때 **채무는 살아 있는데
       매입은 취소된** 장부가 남는다.

    ★ **이미 `CANCELLED` 인 행은 안 센다.** `WHERE settlement_status <> 'CANCELLED'`
      가 재시도를 멱등으로 만든다 — 두 번째 취소는 **0** 을 돌려준다. 재무 `#302` 의
      *"retry no-op"* 과 같은 모양이고, 그래야 마스터가 *"이번에 실제로 물린 것"* 을
      말할 수 있다.

    ⚠️ **`SETTLED` 도 물린다.** 지급 여부를 여기서 판정하지 않는다 — 그 판정은
      `payables` 를 든 재무 몫이고(`OPEN + paid=0` 만 취소), 재무가 거절하면 이
      write 도 **같은 트랜잭션에서 롤백된다.** 두 곳이 각자 판정하면 어느 날 갈린다.

    :returns: 이번 호출로 `CANCELLED` 가 된 header 수.
    """
    ids = [pid for pid in purchase_ids if pid]
    if not ids:
        # ★ 회차 일정이 없던 약정도 승인은 살아 있다 — 물릴 원장이 **없다**는 것은
        #   정상 상태이지 예외가 아니다 (`build_purchase_rows` 와 같은 태도).
        return 0
    schema = sql.Identifier(get_db_schema())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}.purchases
                   SET settlement_status = 'CANCELLED'
                 WHERE purchase_id = ANY(%s)
                   AND settlement_status <> 'CANCELLED'
                """
            ).format(schema),
            (ids,),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


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
