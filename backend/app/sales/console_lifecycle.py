"""판매 한 건의 흐름 — **저장된 연결키로만 잇는다.**

실측(2026-09-11)으로 확인한 연결키다. 각 화살표는 표에 실제로 있는 칸이다:

```text
sales.source_order_id  (= 마스터 업무 키)
  → master_agent_runs.request_id          후보를 만든 판매 사이클 실행
  → master_decisions.request_id           그 판단의 승인 기록
sales.sale_id
  ← sale_items.sale_id
        ← inventory_moves.sale_item_id      (move_type = 'OUT')
  ← inventory_reservations.sale_id
  ← receivables.sale_id
        ← master_collection_events.receivable_id
```

★ **확정 앞 구간이 2026-09-11 에 이어졌다.** 마스터가 확정 때 업무 키를 `source_order_id`
  로 싣기 시작하면서(`sales_approval` — *"확정이 업무 키를 원본 주문으로 싣는다"*),
  *"어느 판단이 이 판매를 낳았나"* 가 **저장된 값**이 됐다. 실측에서 판매 51건이 그
  키로 마스터 판단과 이어진다.

🔴 **그 키가 없는 판매는 여전히 잇지 않는다.** 업무 키를 싣기 전에 만들어진 행과 사람이
   심은 seed 행은 `source_order_id` 가 마스터 요청이 아니다(실측 23건). 그때는
   *"같은 날짜 · 같은 품목 · 가장 최근 행"* 으로 이으면 그럴듯하고 틀린 계보가 서고,
   틀렸다는 사실이 어디에도 남지 않는다. `BLOCKED` 로 두고 왜 막혔는지를 말한다.

★ 부분 상태가 정상이다. 한 화면에서 이어진 구간과 못 이은 구간이 같이 보이는 것이
  *"전부 된다"* 나 *"전부 안 된다"* 보다 정확하다.
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
    #: 후보 → 판매 구간. 업무 키(`source_order_id`)가 마스터 요청을 가리키면 `LIVE`,
    #: 그 키가 없는 옛 행이면 `BLOCKED` 다 — 추정으로 메우지 않는다.
    agent_lineage: Literal["LIVE", "BLOCKED"]
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
    "이 판매에는 마스터 업무 키가 실려 있지 않습니다. "
    "`sales.source_order_id` 가 마스터 요청(REQ-…)이 아니라서 어느 판단이 이 판매를 "
    "낳았는지 기록에 없습니다. 날짜나 품목으로 추정해 잇지 않습니다."
)

#: 마스터 업무 키의 모양. 이 접두사가 아니면 마스터 요청이 아니다.
_REQUEST_PREFIX = "REQ-"


def _master_run(*, sim_run_id: str, request_id: str) -> dict[str, object] | None:
    """이 판매를 낳은 판매 사이클 실행. **업무 키로만 찾는다.**"""
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT run_id, end_code, runtime_status, created_at
        FROM {}.master_agent_runs
        WHERE sim_run_id = %s AND request_id = %s AND cycle = 'SALES'
        ORDER BY run_seq DESC, created_at DESC
        LIMIT 1
        """
    ).format(sql.Identifier(schema))
    found = _rows(statement, [sim_run_id, request_id])
    return found[0] if found else None


def _master_decision(*, request_id: str) -> dict[str, object] | None:
    """그 판단의 승인 기록.

    ⚠️ `master_decisions` 에는 실행 축 칸이 없다. 업무 키가 실행 이름을 품고 있어
      (`REQ-DAILY-SALES-{실행}-…`) 키 자체가 축을 나르지만, 여기서 **이름을 쪼개
      뜻을 읽지 않는다** — 위 `_master_run` 이 이미 실행 축으로 걸렀고, 이 조회는
      그 판단에 붙은 결정을 가져오는 것뿐이다.
    """
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT decision_id, decision, scenario_label, end_code_at_decision,
               decided_by, created_at
        FROM {}.master_decisions
        WHERE request_id = %s
        ORDER BY decision_seq DESC
        LIMIT 1
        """
    ).format(sql.Identifier(schema))
    found = _rows(statement, [request_id])
    return found[0] if found else None


def _agent_stages(*, sim_run_id: str, request_id: str) -> list[LifecycleStage]:
    """업무 키로 이어진 확정 앞 구간.

    🔴 **부서 판정을 여기서 다시 세지 않는다.** 마스터가 그 실행에 적어 둔 `end_code` 와
       판단 기록을 옮길 뿐이다 — 재무·물류 판정의 주인은 각 부서이고, 그 실행의 계획에
       이미 남아 있다.
    """
    run = _master_run(sim_run_id=sim_run_id, request_id=request_id)
    if run is None:
        #  업무 키는 있는데 그 실행이 이 축에 없다 — 지어내지 않고 없다고 말한다.
        return [
            LifecycleStage(
                stage=name,
                status="MISSING",
                reference=request_id,
                detail="업무 키는 실려 있으나 이 실행에서 해당 판매 사이클 기록을 찾지 못했습니다.",
            )
            for name in (
                "candidate",
                "finance_validation",
                "logistics_validation",
                "master_decision",
            )
        ]
    run_ref = str(run["run_id"])
    evidence = [f"master_agent_runs/{run_ref}", f"request/{request_id}"]
    end_code = str(run["end_code"])
    stages = [
        LifecycleStage(
            stage="candidate",
            status="DONE",
            reference=request_id,
            occurred_at=run["created_at"],
            detail=f"판매 사이클 종료 코드 {end_code}",
            evidence=evidence,
        ),
        #  ★ 부서 판정은 그 실행의 계획에 남아 있다. 여기서는 **어디를 보면 되는지**만
        #    가리킨다 — 판정을 옮겨 적으면 두 곳이 서로 다른 말을 하게 된다.
        LifecycleStage(
            stage="finance_validation",
            status="DONE",
            reference=run_ref,
            occurred_at=run["created_at"],
            detail="재무 판정은 이 실행의 재무 회신이 정본입니다 (실행 이력 탭).",
            evidence=evidence,
        ),
        LifecycleStage(
            stage="logistics_validation",
            status="DONE",
            reference=run_ref,
            occurred_at=run["created_at"],
            detail="물류 판정은 이 실행의 물류 회신이 정본입니다 (실행 이력 탭).",
            evidence=evidence,
        ),
    ]
    decision = _master_decision(request_id=request_id)
    stages.append(
        LifecycleStage(
            stage="master_decision",
            status="DONE" if decision else "MISSING",
            reference=None if decision is None else str(decision["decision_id"]),
            occurred_at=None if decision is None else decision["created_at"],
            detail=(
                f"{decision['decision']} · {decision['scenario_label']}"
                f" · {decision['decided_by']}"
                if decision
                else "이 업무 키에 붙은 승인 기록이 없습니다."
            ),
            evidence=evidence,
        )
    )
    return stages


def get_console_sale_lifecycle(
    *, sim_run_id: str, sale_id: str, as_of: date
) -> ConsoleSaleLifecycle | None:
    """판매 한 건의 흐름. 없는 판매면 `None` 이다."""
    sale = _sale(sim_run_id=sim_run_id, sale_id=sale_id)
    if sale is None:
        return None

    business_key = sale["source_order_id"]
    linked = isinstance(business_key, str) and business_key.startswith(_REQUEST_PREFIX)
    stages: list[LifecycleStage] = (
        _agent_stages(sim_run_id=sim_run_id, request_id=str(business_key))
        if linked
        else [
            LifecycleStage(stage=name, status="BLOCKED", detail=_UNLINKED)
            for name in (
                "candidate",
                "finance_validation",
                "logistics_validation",
                "master_decision",
            )
        ]
    )

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

    #  확정 이후 구간만 본다 — 확정 앞 구간의 상태는 `agent_lineage` 가 따로 말한다.
    confirmed = [
        stage
        for stage in stages
        if stage.stage in {"sale", "reservation", "outbound", "receivable"}
    ]
    return ConsoleSaleLifecycle(
        sale_id=str(sale["sale_id"]),
        sim_run_id=sim_run_id,
        as_of=as_of,
        confirmed_lineage=(
            "LIVE" if all(stage.status in {"DONE", "OPEN"} for stage in confirmed) else "PARTIAL"
        ),
        agent_lineage="LIVE" if linked else "BLOCKED",
        stages=stages,
    )
