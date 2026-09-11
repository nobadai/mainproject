"""판매 한 건의 흐름 — **저장된 연결키로만 잇는다.**

실측(2026-09-11)으로 확인한 연결키다. 각 화살표는 표에 실제로 있는 칸이다:

```text
sales.sale_id
  ← sale_items.sale_id
        ← inventory_moves.sale_item_id      (move_type = 'OUT')
  ← inventory_reservations.sale_id
  ← receivables.sale_id
        ← master_collection_events.receivable_id
```

🔴 **확정 앞 단계로는 잇지 못한다.** `sales` 에는 `request_id` 도 `decision_id` 도
   `scenario_id` 도 없다. 그래서 *"어느 후보가 이 판매가 됐는가"* 를 **아무도 저장하지
   않았다.**

   그 구간을 *"같은 날짜 · 같은 품목 · 같은 거래처 · 가장 최근 행"* 으로 이으면 화면은
   그럴듯한 계보를 그린다. 그리고 그 계보는 **틀렸을 수 있는데, 틀렸다는 사실이 어디에도
   남지 않는다.** 그래서 그 구간은 `BLOCKED` 로 두고 왜 막혔는지를 이름으로 말한다.

★ 부분 상태가 정상이다. 확정 이후는 `LIVE`, 확정 이전은 `BLOCKED` — 한 화면에서 두
  사실이 같이 보이는 것이 *"전부 안 된다"* 보다 정확하다.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from psycopg import sql
from pydantic import BaseModel

from app.contracts.aging import classify_receivable_aging
from app.sales.db import fetch_all, get_db_schema

#: 한 단계가 가질 수 있는 상태.
#:
#: ``BLOCKED`` 는 *"아직 안 만들었다"* 가 아니라 **연결을 저장한 곳이 없다**는 뜻이다.
#: ``NOT_DUE`` 는 아직 그 단계에 올 때가 아니라는 사실이고, ``MISSING`` 은 올 때가
#: 지났는데 행이 없다는 사실이다 — 둘을 한 칸에 뭉치지 않는다.
LifecycleStatus = Literal["DONE", "OPEN", "NOT_DUE", "MISSING", "BLOCKED"]


class LifecycleStage(BaseModel):
    stage: str
    status: LifecycleStatus
    #: 이 단계를 가리키는 저장된 식별자. 없으면 `null` 이다.
    reference: str | None = None
    #: 그 단계가 기록된 시각/날짜. 없으면 `null` — 오늘로 메우지 않는다.
    occurred_at: date | datetime | None = None
    #: 사람이 읽을 사유. 왜 이 상태인지를 말한다.
    detail: str
    #: 되짚을 근거. 저장된 참조만 싣는다.
    evidence: list[str] = []


class ConsoleSaleLifecycle(BaseModel):
    sale_id: str
    sim_run_id: str
    as_of: date
    #: 확정 이후 구간이 저장된 연결키로 이어졌는가.
    confirmed_lineage: Literal["LIVE", "PARTIAL"]
    #: 후보 → 판매 구간. 저장된 연결키가 없어 늘 `BLOCKED` 다.
    agent_lineage: Literal["BLOCKED"] = "BLOCKED"
    stages: list[LifecycleStage]


def _rows(statement: sql.Composed, params: list[object]) -> list[dict[str, object]]:
    return fetch_all(statement, params)


def _sale(*, sim_run_id: str, sale_id: str) -> dict[str, object] | None:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT sale_id, sim_run_id, customer_partner_id, order_date, sale_date,
               collection_due_date, total_quantity_kg, total_amount_krw,
               contribution_profit_krw, collection_status, order_status, source_order_id
        FROM {}.sales WHERE sim_run_id = %s AND sale_id = %s
        """
    ).format(sql.Identifier(schema))
    found = _rows(statement, [sim_run_id, sale_id])
    return found[0] if found else None


def _outbound(*, sim_run_id: str, sale_id: str) -> list[dict[str, object]]:
    """출고는 `sale_items.sale_item_id` 를 거쳐 붙는다 — 날짜로 잇지 않는다."""
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT m.move_id, m.moved_at, m.quantity_kg, m.lot_id, m.sale_item_id
        FROM {schema}.inventory_moves m
        JOIN {schema}.sale_items i ON i.sale_item_id = m.sale_item_id
        WHERE m.sim_run_id = %s AND i.sale_id = %s AND m.move_type = 'OUT'
        ORDER BY m.moved_at ASC, m.move_id ASC
        """
    ).format(schema=sql.Identifier(schema))
    return _rows(statement, [sim_run_id, sale_id])


def _reservations(*, sim_run_id: str, sale_id: str) -> list[dict[str, object]]:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT reservation_id, status, reserved_qty_kg, required_qty_kg, created_at
        FROM {}.inventory_reservations
        WHERE sim_run_id = %s AND sale_id = %s
        ORDER BY reservation_id ASC
        """
    ).format(sql.Identifier(schema))
    return _rows(statement, [sim_run_id, sale_id])


def _receivables(*, sim_run_id: str, sale_id: str) -> list[dict[str, object]]:
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT receivable_id, issued_date, due_date, original_amount_krw,
               received_amount_krw, outstanding_amount_krw, status
        FROM {}.receivables
        WHERE sim_run_id = %s AND sale_id = %s
        ORDER BY due_date ASC, receivable_id ASC
        """
    ).format(sql.Identifier(schema))
    return _rows(statement, [sim_run_id, sale_id])


def _collections(*, sim_run_id: str, receivable_ids: list[str]) -> list[dict[str, object]]:
    if not receivable_ids:
        return []
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT receivable_id, collection_date, target_received_total_krw
        FROM {}.master_collection_events
        WHERE sim_run_id = %s AND receivable_id = ANY(%s)
        ORDER BY collection_date ASC
        """
    ).format(sql.Identifier(schema))
    return _rows(statement, [sim_run_id, receivable_ids])


_UNLINKED = (
    "이 단계와 확정 판매를 잇는 키가 저장되어 있지 않습니다. "
    "`sales` 에 request_id · decision_id · scenario_id 칸이 없어, 어느 후보가 이 판매가 "
    "됐는지는 기록에 없습니다. 날짜나 품목으로 추정해 잇지 않습니다."
)


def get_console_sale_lifecycle(
    *, sim_run_id: str, sale_id: str, as_of: date
) -> ConsoleSaleLifecycle | None:
    """판매 한 건의 흐름. 없는 판매면 `None` 이다."""
    sale = _sale(sim_run_id=sim_run_id, sale_id=sale_id)
    if sale is None:
        return None

    stages: list[LifecycleStage] = [
        LifecycleStage(stage=name, status="BLOCKED", detail=_UNLINKED)
        for name in ("candidate", "finance_validation", "logistics_validation", "master_decision")
    ]

    stages.append(
        LifecycleStage(
            stage="sale",
            status="DONE",
            reference=str(sale["sale_id"]),
            occurred_at=sale["sale_date"],
            detail=f"{sale['order_status']} · 회수 {sale['collection_status']}",
            evidence=[f"sales/{sale['sale_id']}"],
        )
    )

    reservations = _reservations(sim_run_id=sim_run_id, sale_id=sale_id)
    stages.append(
        LifecycleStage(
            stage="reservation",
            status="DONE" if reservations else "MISSING",
            reference=None if not reservations else str(reservations[0]["reservation_id"]),
            occurred_at=None if not reservations else reservations[0]["created_at"],
            detail=(
                f"예약 {len(reservations)}건"
                if reservations
                else "이 판매에 붙은 재고 예약 행이 없습니다."
            ),
            evidence=[f"inventory_reservations/{row['reservation_id']}" for row in reservations],
        )
    )

    moves = _outbound(sim_run_id=sim_run_id, sale_id=sale_id)
    shipped = sum((Decimal(str(row["quantity_kg"])) for row in moves), start=Decimal(0))
    stages.append(
        LifecycleStage(
            stage="outbound",
            status="DONE" if moves else "MISSING",
            reference=None if not moves else str(moves[0]["move_id"]),
            occurred_at=None if not moves else moves[0]["moved_at"],
            detail=(
                f"출고 {len(moves)}건 · {shipped}kg"
                if moves
                else "이 판매의 판매줄에 붙은 출고 이동이 없습니다."
            ),
            evidence=[f"inventory_moves/{row['move_id']}" for row in moves],
        )
    )

    receivables = _receivables(sim_run_id=sim_run_id, sale_id=sale_id)
    if receivables:
        row = receivables[0]
        bucket, overdue = classify_receivable_aging(
            outstanding_amount_krw=Decimal(str(row["outstanding_amount_krw"])),
            due_date=row["due_date"],
            as_of=as_of,
        )
        detail = f"{row['status']} · {bucket}"
        if overdue:
            detail += f" · {overdue}일 연체"
        stages.append(
            LifecycleStage(
                stage="receivable",
                status="DONE" if bucket == "PAID" else "OPEN",
                reference=str(row["receivable_id"]),
                occurred_at=row["issued_date"],
                detail=detail,
                evidence=[f"receivables/{item['receivable_id']}" for item in receivables],
            )
        )
    else:
        stages.append(
            LifecycleStage(
                stage="receivable",
                status="MISSING",
                detail="이 판매에 붙은 매출채권 행이 없습니다.",
            )
        )

    identifiers = [str(row["receivable_id"]) for row in receivables]
    collections = _collections(sim_run_id=sim_run_id, receivable_ids=identifiers)
    if collections:
        stages.append(
            LifecycleStage(
                stage="collection",
                status="DONE",
                reference=str(collections[0]["receivable_id"]),
                occurred_at=collections[0]["collection_date"],
                detail=f"수금 기록 {len(collections)}건",
                evidence=[
                    f"master_collection_events/{row['receivable_id']}@{row['collection_date']}"
                    for row in collections
                ],
            )
        )
    else:
        #  ⚠️ 만기 전이면 «아직 아니다» 이고, 만기가 지났으면 «없다» 이다. 다른 사실이다.
        due = receivables[0]["due_date"] if receivables else None
        not_due = due is not None and as_of <= due
        stages.append(
            LifecycleStage(
                stage="collection",
                status="NOT_DUE" if not_due else "MISSING",
                detail=(
                    f"회수 기준일({due})이 아직 지나지 않았습니다."
                    if not_due
                    else "이 채권에 붙은 수금 기록이 없습니다."
                ),
            )
        )

    confirmed = [
        stage
        for stage in stages
        if stage.stage != "collection" and stage.status != "BLOCKED"
    ]
    return ConsoleSaleLifecycle(
        sale_id=str(sale["sale_id"]),
        sim_run_id=sim_run_id,
        as_of=as_of,
        confirmed_lineage=(
            "LIVE" if all(stage.status in {"DONE", "OPEN"} for stage in confirmed) else "PARTIAL"
        ),
        stages=stages,
    )
