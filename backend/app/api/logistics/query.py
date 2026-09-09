"""재고 · 물류 탭 — 값을 읽어오는 곳.

╔══════════════════════════════════════════════════════════════════════════╗
║  ★ 물류 파트가 채우는 파일입니다. `build()` 안쪽만 바꾸면 됩니다.          ║
║                                                                          ║
║  이제 **실제 DB 값**을 읽습니다 (`Source.filled = True`).                 ║
║  읽는 길은 `app/logistics/console_service.py` 하나뿐입니다 —              ║
║  **SQL 을 여기서 새로 쓰지 않습니다** (#415). 같은 쿼리를 두 벌 두면       ║
║  언젠가 값이 갈라집니다.                                                  ║
║                                                                          ║
║  카드를 더 넣고 싶으면 `Card(...)` 를 목록에 하나 더 넣으면 됩니다.        ║
║  **화면은 안 고쳐도 됩니다** — 표의 칸은 백엔드가 내려줍니다.              ║
╚══════════════════════════════════════════════════════════════════════════╝

★ **읽기에 실패해도 화면은 뜹니다.** 그때는 예시값으로 되돌아가고 「예시값」
  딱지가 붙습니다 (매입·ML 과 같은 판단). 예외 종류를 골라 잡지 않습니다 —
  안 골라낸 하나 때문에 화면이 통째로 죽습니다.

🔴 **부르는 함수 넷의 시그니처가 서로 다릅니다.**

    get_inventory_console(sim_run_id, as_of)    items · lots · capacity
    get_inbound_console  (sim_run_id, as_of)    in_transit · receipts · arrival_summary
    get_warehouse_console(sim_run_id)           zones · lot_locations      ← as_of 없음
    get_outbound_console (sim_run_id, status)   reservations               ← as_of 없음

  뒤의 둘은 **기준일을 안 받습니다.** 날짜를 바꿔도 창고·출고는 안 변합니다.
  그 사실을 각 pane 의 `Note` 에 적습니다 — 안 적으면 "날짜가 안 먹네" 라는
  의심을 그대로 받습니다.

🔴 **`None` 은 0 이 아닙니다.** `available_qty_kg` 는 못 읽은 축이 있으면
  `None` 입니다. 0 으로 바꾸면 «팔 게 없다» 는 거짓말이 됩니다 — 이 탭이 맨 위에
  걸어 둔 `principle` 이 바로 그 이야기입니다.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from decimal import Decimal

from app.api.logistics.schema import LogisticsTab
from app.api.primitives import (
    Card,
    Chart,
    Column,
    Marker,
    Note,
    Pane,
    Series,
    Source,
    Stat,
    Table,
)
from app.logistics.console_schemas import (
    ConsoleInboundResponse,
    ConsoleInventoryResponse,
    ConsoleOutboundResponse,
    ConsoleWarehouseResponse,
)
from app.logistics.console_service import (
    get_inbound_console,
    get_inventory_console,
    get_inventory_moves_console,
    get_outbound_console,
    get_reservation_fefo_console,
    get_warehouse_console,
)
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

log = logging.getLogger(__name__)

PANES = ("stock", "inbound", "warehouse", "outbound")

#: 예시 재고 (kg). **읽기에 실패했을 때만** 쓴다 — 그때는 요약도 같이 예시값이라
#: 둘이 안 갈라진다. 1번 칸이 공란인 것은 «안 보고한 날» 이지 0 이 아니다.
_ONHAND = [21400, None, 19800, 19800, 17200, 17200, 16900, 15100, 14600]
_PROJ = [14600, 26200, 25700, 25100]
_IN_TRANSIT = [None, None, None, None, None, None, None, 12000, 12000]
#: 예시값 화면의 요약 숫자. **실측 경로에서는 안 쓴다.**
ONHAND_NOW = 14600

#: 화면에 적는 Zone 종류. DB 어휘를 사람 말로만 바꾼다 — 뜻을 더하지 않는다.
_ZONE_KIND = {"STORAGE_RACK": "보관 랙", "WORK_FLOOR": "작업 Floor"}
_PLACEMENT = {"PLACED": "배치됨", "UNPLACED": "미배치"}


def _t(cols: list[tuple[str, str, str]], rows: list[dict], **kw) -> Table:
    """표를 짧게 쓰기 위한 도우미. (키, 이름, 정렬) 세 쪽지로 칸을 만든다."""
    return Table(
        columns=[
            Column(key=k, label=lab, align=al, mono=(al == "right" or k in ("id", "lot")))
            for k, lab, al in cols
        ],
        rows=rows,
        **kw,
    )


#  ── 글자 만들기 ──────────────────────────────────────────────────────────
#
#  ★ **`None` 은 «—» 로 적는다.** 0 으로 적으면 «없었다» 가 되어 뜻이 바뀐다.


def _kg(value: Decimal | float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}"


def _kg_cell(value: Decimal | float | None, digits: int = 0) -> str | None:
    """표 칸. 공란은 `None` 으로 둬야 화면이 «모름» 으로 그린다."""
    return None if value is None else f"{_kg(value, digits)} kg"


def _raw(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _days(value: int | None) -> str | None:
    return None if value is None else f"{value}일"


def _md(value: date | None) -> str | None:
    return None if value is None else value.strftime("%m-%d")


def _sum(values: list[Decimal | None]) -> Decimal | None:
    """하나라도 모르면 합계도 모른다. **아는 것만 더해서 아는 척하지 않는다.**"""
    if any(v is None for v in values):
        return None
    return sum((v for v in values if v is not None), Decimal(0))


#  ── 실제 값 ──────────────────────────────────────────────────────────────


def _stock_pane(
    inv: ConsoleInventoryResponse,
    inb: ConsoleInboundResponse,
    ob: ConsoleOutboundResponse,
) -> Pane:
    on_hand = sum((it.on_hand_qty_kg for it in inv.items), Decimal(0))
    available = _sum([it.available_qty_kg for it in inv.items])
    reserved = sum((it.reserved_qty_kg for it in inv.items), Decimal(0))
    disposal = sum(it.disposal_candidate_lot_count for it in inv.items)
    sell_priority = sum(it.sell_priority_lot_count for it in inv.items)

    #  운송 중은 입고 쪽 사실이다. `None` 이면 «못 읽음» 이라 0 으로 적지 않는다.
    transit = inb.in_transit
    transit_kg = (
        None if transit is None else sum((t.quantity_kg for t in transit), Decimal(0))
    )
    first_eta = None
    if transit:
        etas = [t.expected_arrival_date for t in transit if t.expected_arrival_date]
        first_eta = min(etas) if etas else None

    if transit_kg is None:
        transit_text = "운송 중 — 못 읽었습니다"
    elif first_eta:
        transit_text = f"운송 중 {_kg(transit_kg)} · {_md(first_eta)} 도착 예정"
    else:
        transit_text = f"운송 중 {_kg(transit_kg)}"

    if available is None:
        avail_detail = f"못 읽은 축이 있습니다 — {inv.available_qty_unresolved_reason}"
    else:
        avail_detail = "예약 · 할당 · 신선도 반영 서버 계산값"

    return Pane(
        key="stock",
        label="재고 · 예약",
        stats=[
            Stat(
                label="현재고 합계", value=_kg(on_hand, 1), unit="kg",
                detail=transit_text,
                tone="good" if on_hand > 0 else "warn", raw=_raw(on_hand),
            ),
            Stat(
                label="판매가능량", value=_kg(available, 1) if available is not None else "—",
                unit="kg" if available is not None else None,
                detail=avail_detail,
                tone=("warn" if available is None else "good" if available > 0 else "warn"),
                raw=_raw(available),
            ),
            Stat(
                label="예약 총량", value=_kg(reserved, 1), unit="kg",
                detail=f"활성 예약 {sum(it.active_reservation_count for it in inv.items)}건",
                tone="warn" if reserved > 0 else "neutral", raw=_raw(reserved),
            ),
            Stat(
                label="폐기 검토", value=f"{disposal:,}", unit="Lot",
                detail=f"우선판매 신호 {sell_priority} Lot",
                tone="bad" if disposal > 0 else "good", raw=float(disposal),
            ),
        ],
        cards=[
            Card(
                key="reservation", title="재고 확보 · Reservation 현황",
                subtitle="예약 총량 안에 Lot 할당량이 포함됩니다",
                source_ref="inventory_reservations · inventory_allocations",
                flow=["현재고", "Reservation 으로 수량 확보", "Lot Allocation",
                      "실출고 SHIPPED", "원장 OUT · 현재고 감소"],
                lead=Note(
                    tone="info",
                    text=("예약과 할당은 재고를 **바로 줄이지 않습니다.** 실제 재고 감소는 "
                          "실출고 시점에 일어납니다. **판매가능량**은 서버가 예약 · 할당 · "
                          "Lot 상태 · 신선도를 반영해 계산한 값입니다."),
                ),
                table=_t(
                    [("id", "Reservation", "left"), ("item", "품목", "left"),
                     ("need", "요구량", "right"), ("resv", "예약량", "right"),
                     ("alloc", "Lot 할당", "right"), ("left", "미할당 잔여", "right"),
                     ("due", "출고기한", "left"), ("state", "상태", "left")],
                    [
                        {
                            "id": r.reservation_id,
                            "item": r.item_name or r.item_id,
                            "need": _kg_cell(r.required_qty_kg),
                            "resv": _kg_cell(r.reserved_qty_kg),
                            "alloc": _kg_cell(r.allocated_qty_kg),
                            "left": _kg_cell(r.unallocated_qty_kg),
                            "due": _md(r.due_date),
                            "state": r.status,
                        }
                        for r in ob.reservations
                    ],
                    empty_text="확보된 재고가 없습니다",
                ),
            ),
            Card(
                key="lots", title="Lot 상태", subtitle="Snapshot + turnover 계산 결과",
                source_ref="inventory_lots · item_turnover_policies",
                table=_t(
                    [("lot", "Lot", "left"), ("item", "품목", "left"), ("grade", "등급", "left"),
                     ("qty", "잔량", "right"), ("fresh", "신선도 잔여", "right"),
                     ("turn", "회전 잔여", "right"), ("state", "Lot 상태", "left"),
                     ("signal", "회전 Signal", "left")],
                    [
                        {
                            "lot": lo.lot_id,
                            "item": lo.item_name or lo.item_id,
                            #  ★ 등급은 **넘겨짚지 않는다.** DB NULL 은 "미확정" 이다.
                            "grade": lo.grade or "등급 미확정",
                            "qty": _kg_cell(lo.remaining_qty_kg),
                            "fresh": _days(lo.remaining_freshness_days),
                            "turn": _days(lo.remaining_turnover_days),
                            "state": lo.status,
                            "signal": lo.turnover_status,
                        }
                        for lo in inv.lots
                    ],
                    empty_text="이 날짜에 살아 있는 Lot 이 없습니다",
                ),
                footer="신선도 잔여가 음수인 Lot 은 폐기 검토 대상입니다.",
            ),
            Card(
                key="principle", title="재고 처리 원칙",
                bullets=[
                    "예약과 할당은 재고를 줄이지 않습니다 — 실출고 때 줄어듭니다",
                    "판매가능량은 화면이 계산하지 않습니다. 서버 값을 그대로 씁니다",
                    "등급 미확정 Lot 은 «미확정» 으로 적습니다 — 특으로 넘겨짚지 않습니다",
                ],
            ),
        ],
    )


def _inbound_pane(inb: ConsoleInboundResponse) -> Pane:
    summary = inb.arrival_summary
    transit = inb.in_transit

    return Pane(
        key="inbound",
        label="입고 처리",
        stats=[
            Stat(label="오늘 도착 예정", value=f"{summary.due_count:,}", unit="건",
                 tone="good" if summary.due_count else "neutral",
                 raw=float(summary.due_count)),
            Stat(label="연체", value=f"{summary.overdue_count:,}", unit="건",
                 detail="도착 예정일이 지났는데 안 들어온 것",
                 tone="bad" if summary.overdue_count else "good",
                 raw=float(summary.overdue_count)),
            Stat(label="막힘", value=f"{summary.blocked_count:,}", unit="건",
                 tone="warn" if summary.blocked_count else "neutral",
                 raw=float(summary.blocked_count)),
            Stat(label="판정 불가", value=f"{summary.unresolved_count:,}", unit="건",
                 detail=f"원천 상태 {summary.source_status}",
                 tone="warn" if summary.unresolved_count else "neutral",
                 raw=float(summary.unresolved_count)),
        ],
        cards=[
            Card(
                key="flow", title="입고 처리 흐름",
                flow=["매입 승인", "in_transit", "도착 Receipt", "검수", "입고 완료 · 원장 IN"],
                lead=Note(
                    tone="info",
                    text="**도착과 입고 완료는 다릅니다.** 검수를 통과해야 재고가 늘어납니다.",
                ),
            ),
            Card(
                key="transit", title="운송 중",
                subtitle="아직 도착하지 않았습니다 — 재고가 아닙니다",
                source_ref="확정 매입 · 도착 예정 축",
                lead=(
                    None if transit is not None else Note(
                        tone="warn",
                        text=("운송 중 목록을 **못 읽었습니다** — 비어 있는 것과 다릅니다. "
                              f"원천 상태 `{inb.in_transit_status}`."),
                    )
                ),
                table=_t(
                    [("id", "Inbound", "left"), ("item", "품목", "left"),
                     ("qty", "수량", "right"), ("eta", "도착 예정", "left")],
                    [
                        {
                            "id": t.inbound_id,
                            "item": t.item,
                            "qty": _kg_cell(t.quantity_kg),
                            "eta": _md(t.expected_arrival_date),
                        }
                        for t in (transit or [])
                    ],
                    empty_text=(
                        "운송 중인 물량이 없습니다"
                        if transit is not None
                        else "값을 못 읽었습니다 — 0 이 아닙니다"
                    ),
                ),
            ),
            Card(
                key="receipt", title="도착 · Receipt 현황",
                #  🔴 `arrival_schedule` 은 **표가 아니라 계약 필드명**이었다.
                #     실제 출처는 이 둘이다.
                source_ref="inbound_receipts · inbound_inspections",
                table=_t(
                    [("id", "Receipt", "left"), ("item", "품목", "left"),
                     ("ord", "주문", "right"), ("acc", "합격", "right"),
                     ("arrive", "도착일", "left"), ("state", "상태", "left"),
                     ("verdict", "검수", "left"), ("applied", "재고 반영", "left")],
                    [
                        {
                            "id": r.receipt_id,
                            "item": r.item_name or r.item_id,
                            "ord": _kg_cell(r.ordered_qty_kg),
                            "acc": _kg_cell(r.accepted_qty_kg),
                            "arrive": _md(r.arrived_at),
                            "state": r.receipt_status,
                            "verdict": r.inspection_verdict,
                            "applied": "반영됨" if r.stock_applied else "아직",
                        }
                        for r in inb.receipts
                    ],
                    empty_text="이 날짜까지 도착한 Receipt 이 없습니다",
                ),
                footer="재고가 되는 것은 주문 수량이 아니라 **합격 수량**입니다.",
            ),
        ],
    )


def _warehouse_pane(wh: ConsoleWarehouseResponse, inv: ConsoleInventoryResponse) -> Pane:
    cap = inv.capacity
    unplaced = [lo for lo in wh.lot_locations if lo.placement == "UNPLACED"]

    return Pane(
        key="warehouse",
        label="창고 배치",
        stats=[
            Stat(label="사용 중", value=_kg(cap.used_capacity_kg, 1), unit="kg",
                 detail="창고 전체 합계", tone="neutral", raw=_raw(cap.used_capacity_kg)),
            Stat(label="보장 용량", value=_kg(cap.guaranteed_capacity_kg), unit="kg",
                 detail="계약 baseline", tone="neutral",
                 raw=_raw(cap.guaranteed_capacity_kg)),
            Stat(label="최대 용량", value=_kg(cap.burst_capacity_kg), unit="kg",
                 detail="일시 초과 허용치", tone="neutral", raw=_raw(cap.burst_capacity_kg)),
            Stat(label="자리 못 잡은 Lot", value=f"{len(unplaced):,}", unit="Lot",
                 detail="입고됐지만 Pallet 자리가 없습니다",
                 tone="warn" if unplaced else "good", raw=float(len(unplaced))),
        ],
        cards=[
            Card(
                key="zone", title="Zone 점유",
                subtitle="Zone 의 물리 정본은 kg 이 아니라 Pallet 자리 수입니다",
                #  🔴 `zone_capacity` 는 **표가 아니라 계약 필드명**이었다.
                source_ref="warehouse_zones · storage_locations",
                lead=Note(
                    tone="info",
                    text=("**자리(Position)와 kg 를 섞지 않습니다.** 아래 표는 자리 수이고, "
                          "kg 한도는 Zone 별로 없이 **창고 전체 하나**뿐이라 "
                          "위 요약에 적었습니다."),
                ),
                table=_t(
                    [("zone", "Zone", "left"), ("kind", "종류", "left"),
                     ("used", "사용 자리", "right"), ("cap", "총 자리", "right"),
                     ("free", "빈 자리", "right")],
                    [
                        {
                            "zone": z.zone_name,
                            "kind": _ZONE_KIND.get(z.zone_kind, z.zone_kind),
                            "used": z.occupied_positions,
                            "cap": z.total_positions,
                            "free": z.free_positions,
                        }
                        for z in wh.zones
                    ],
                    empty_text="등록된 Zone 이 없습니다",
                ),
            ),
            Card(
                key="placement", title="Lot 배치",
                source_ref="pallets · storage_locations",
                table=_t(
                    [("lot", "Lot", "left"), ("item", "품목", "left"),
                     ("qty", "잔량", "right"), ("zone", "Zone", "left"),
                     ("loc", "자리", "left"), ("state", "배치", "left")],
                    [
                        {
                            "lot": lo.lot_id,
                            "item": lo.item_name or lo.item_id,
                            "qty": _kg_cell(lo.remaining_qty_kg),
                            "zone": lo.zone_id,
                            "loc": lo.location_id,
                            "state": _PLACEMENT.get(lo.placement, lo.placement),
                        }
                        for lo in wh.lot_locations
                    ],
                    empty_text="배치된 Lot 이 없습니다",
                ),
                footer="자리가 «미배치» 인 Lot 은 입고는 됐지만 Pallet 을 못 잡은 것입니다.",
            ),
        ],
    )


def _outbound_pane(ob: ConsoleOutboundResponse, as_of: date) -> Pane:
    #  ★ FEFO 는 **예약 한 건마다** 묻는다. 예약이 없으면 물어볼 대상도 없다.
    #    예약이 없을 때 Lot 을 신선도순으로 늘어놓아 «후보» 라고 부르지 않는다 —
    #    그건 서비스에 없는 계산을 화면이 새로 만드는 것이다 (#415).
    fefo_rows: list[dict] = []
    for resv in ob.reservations:
        fefo = get_reservation_fefo_console(reservation_id=resv.reservation_id, as_of=as_of)
        for rank, cand in enumerate(fefo.candidates, start=1):
            fefo_rows.append(
                {
                    "resv": resv.reservation_id,
                    "lot": cand.lot_id,
                    "grade": cand.grade or "등급 미확정",
                    "qty": _kg_cell(cand.available_qty_kg),
                    "fresh": _days(cand.remaining_freshness_days),
                    "rank": rank,
                }
            )

    return Pane(
        key="outbound",
        label="출고 · 운송",
        stats=[
            Stat(label="예약", value=f"{len(ob.reservations):,}", unit="건",
                 tone="good" if ob.reservations else "neutral",
                 raw=float(len(ob.reservations))),
            Stat(label="FEFO 후보", value=f"{len(fefo_rows):,}", unit="건",
                 detail="예약이 있어야 후보가 나옵니다",
                 tone="neutral", raw=float(len(fefo_rows))),
        ],
        cards=[
            Card(
                key="fefo", title="FEFO 후보",
                subtitle="먼저 상하는 것을 먼저 내보냅니다 (First Expired, First Out)",
                source_ref="inventory_reservations · inventory_lots",
                lead=Note(
                    tone="info",
                    text=("후보는 **고르는 것이 아닙니다** — 자동 Allocation 이 아닙니다. "
                          "예약이 없으면 후보도 없습니다."),
                ),
                table=_t(
                    [("resv", "Reservation", "left"), ("lot", "Lot", "left"),
                     ("grade", "등급", "left"), ("qty", "가용", "right"),
                     ("fresh", "신선도 잔여", "right"), ("rank", "순서", "right")],
                    fefo_rows,
                    empty_text="확보된 예약이 없어 후보가 없습니다",
                ),
            ),
        ],
    )


#  ── 예시값 (읽기 실패 시) ────────────────────────────────────────────────
#
#  ★ 실측 경로가 죽어도 **화면은 떠야** 한다. 대신 「예시값」 딱지가 붙는다.


def _demo_stock_pane() -> Pane:
    return Pane(
        key="stock", label="재고 · 예약",
        stats=[
            Stat(label="현재고 합계", value=f"{ONHAND_NOW:,}", unit="kg",
                 detail="운송 중 12,000 · 01-07 도착", tone="good", raw=ONHAND_NOW),
            Stat(label="판매가능량", value="2,950", unit="kg",
                 detail="예약 · 할당 · 신선도 반영 서버 계산값", tone="good", raw=2950),
            Stat(label="예약 총량", value="700", unit="kg",
                 detail="Lot 할당 500kg 포함", tone="warn", raw=700),
            Stat(label="폐기 검토", value="1", unit="Lot",
                 detail="양파 LOT-Y-OLD · 신선도 잔여 -1일", tone="bad", raw=1),
        ],
        cards=[
            Card(
                key="reservation", title="재고 확보 · Reservation 현황",
                subtitle="예약 총량 안에 Lot 할당량이 포함됩니다",
                source_ref="inventory_reservations · inventory_allocations",
                flow=["현재고", "Reservation 으로 수량 확보", "Lot Allocation",
                      "실출고 SHIPPED", "원장 OUT · 현재고 감소"],
                table=_t(
                    [("id", "Reservation", "left"), ("item", "품목", "left"),
                     ("resv", "예약량", "right"), ("state", "상태", "left")],
                    [
                        {"id": "RSV-001", "item": "배추", "resv": "500 kg",
                         "state": "PARTIALLY_ALLOCATED"},
                        {"id": "RSV-002", "item": "무", "resv": "200 kg",
                         "state": "ALLOCATED"},
                    ],
                    empty_text="확보된 재고가 없습니다",
                ),
            ),
        ],
    )


def _demo_pane(key: str, label: str) -> Pane:
    return Pane(key=key, label=label, cards=[
        Card(key="demo", title=label,
             lead=Note(tone="warn", text="**예시값입니다.** 실제 값을 못 읽었습니다.")),
    ])


_PRINCIPLE = Note(
    tone="neutral",
    text=("재고 수치는 **물류가 보고한 것만** 적습니다. 보고가 없는 날은 "
          "0 이 아니라 **공란**입니다 — 둘은 다릅니다."),
)


def _demo(reason: str) -> LogisticsTab:
    return LogisticsTab(
        panes=[
            _demo_stock_pane(),
            _demo_pane("inbound", "입고 처리"),
            _demo_pane("warehouse", "창고 배치"),
            _demo_pane("outbound", "출고 · 운송"),
        ],
        selected="stock",
        principle=_PRINCIPLE,
        source=Source(filled=False, owner="물류", note=reason),
    )


def build(as_of: date, pane: str) -> LogisticsTab:
    #  ★ 통째로 잡는 것이 맞다 — 여기서 무슨 일이 나든 **화면은 떠야** 하고
    #    대신 「예시값」 딱지가 붙는다. 예외 종류를 골라 잡으면 안 골라낸
    #    하나 때문에 화면이 통째로 죽는다 (매입·ML 이 같은 판단).
    run = BURN_IN_SIM_RUN_ID
    try:
        inv = get_inventory_console(sim_run_id=run, as_of=as_of)
        inb = get_inbound_console(sim_run_id=run, as_of=as_of)
        wh = get_warehouse_console(sim_run_id=run)
        ob = get_outbound_console(sim_run_id=run)
        panes = [
            _stock_pane(inv, inb, ob),
            _inbound_pane(inb),
            _warehouse_pane(wh, inv),
            _outbound_pane(ob, as_of),
        ]
    except Exception as error:  # noqa: BLE001  DB 미연결 · 표 없음 둘 다
        log.info("물류 값을 못 읽어 예시값을 씁니다: %s", error)
        return _demo(f"DB 를 못 읽었습니다 ({type(error).__name__})")

    return LogisticsTab(
        panes=panes,
        selected=pane,
        principle=_PRINCIPLE,
        source=Source(
            filled=True,
            owner="물류",
            note=(
                "inventory_lots · inventory_moves · inventory_reservations · "
                "inbound_receipts · warehouse_zones · storage_locations · "
                f"{run} · {as_of}"
            ),
        ),
    )


def _ceiling(value: float) -> float:
    """눈금 꼭대기. 1 · 2 · 2.5 · 5 계단으로 올린다."""
    if value <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5):
        if value <= step * base:
            return step * base
    return 10 * base


def _onhand_series(as_of: date, n: int, at: int) -> tuple[list[float | None], bool]:
    """원장을 거슬러 올라가 하루치 보유량을 편다.

    ★ **정본은 원장이다** (`inventory_lots.remaining_qty_kg` 컬럼 주석).

          on_hand(D-1) = on_hand(D) - IN(D) + OUT(D) + DISPOSE(D)

    🔴 `ADJUST` 는 **방향을 모른다.** `ADJUST_IN` · `ADJUST_OUT` 로 갈리기 전까지
      (`23_inventory_move_type_split.sql`) 부호를 넘겨짚을 수 없다. 창 안에 하나라도
      있으면 **거슬러 올라가기를 포기하고 오늘 칸만 남긴다** — 틀린 선을 그리느니
      공란이 낫다. 두 번째 반환값이 그 사실을 알린다.
    """
    run = BURN_IN_SIM_RUN_ID
    inv = get_inventory_console(sim_run_id=run, as_of=as_of)
    today = sum((it.on_hand_qty_kg for it in inv.items), Decimal(0))

    data: list[float | None] = [None] * n
    data[at] = float(today)

    moves = get_inventory_moves_console(
        sim_run_id=run,
        moved_from=as_of - timedelta(days=at),
        moved_to=as_of,
        limit=1000,
    )
    if any(m.move_type == "ADJUST" for m in moves.moves):
        return data, False

    net: dict[date, Decimal] = {}
    for m in moves.moves:
        sign = Decimal(1) if m.move_type == "IN" else Decimal(-1)
        net[m.moved_at] = net.get(m.moved_at, Decimal(0)) + sign * m.quantity_kg

    cursor = today
    for i in range(at - 1, -1, -1):
        cursor -= net.get(as_of - timedelta(days=at - i - 1), Decimal(0))
        data[i] = float(cursor)
    return data, True


def dashboard_stock(n: int, at: int, as_of: date) -> Chart:
    """대시보드에 얹을 재고 그래프.

    ★ **대시보드가 아니라 여기서 만듭니다.** 요약 숫자와 같은 곳에서 나와야
      둘이 안 갈라집니다. 실제로 갈라졌던 적이 있습니다 — 요약은 4,550kg 인데
      그래프 끝은 14,600kg 이었습니다. `tests/api/test_screen_api.py` 가 지킵니다.

    ★ **앞날은 그리지 않습니다.** 예전에는 `_PROJ` 로 «추정 보유» 점선을 그렸는데,
      그 값의 출처가 없었습니다. 없는 예측을 지어내지 않고 **공란**으로 둡니다.
      확정된 도착만 `markers` 로 얹습니다.
    """
    try:
        data, walked = _onhand_series(as_of, n, at)
        inb = get_inbound_console(sim_run_id=BURN_IN_SIM_RUN_ID, as_of=as_of)
    except Exception as error:  # noqa: BLE001  DB 미연결 · 표 없음 둘 다
        log.info("재고 그래프를 못 읽어 예시값을 씁니다: %s", error)
        return _demo_stock_chart(n, at)

    #  창 안에 도착이 잡힌 것만 표시한다. 밖의 것을 가장자리로 끌어오지 않는다.
    markers: list[Marker] = []
    for item in inb.in_transit or []:
        if item.expected_arrival_date is None:
            continue
        index = at + (item.expected_arrival_date - as_of).days
        if 0 <= index < n:
            markers.append(
                Marker(index=index, value=float(item.quantity_kg),
                       label=f"{item.item} 도착 {float(item.quantity_kg):,.0f}kg", tone="info")
            )

    top = _ceiling(max([v for v in data if v is not None] + [m.value for m in markers] + [1]))
    ticks = [top * q for q in (0.25, 0.5, 0.75, 1.0)]
    return Chart(
        label="창고 재고", y_min=0, y_max=top,
        y_ticks=ticks,
        #  ★ kg 로 그리고 kg 로 적는다. 예전에는 톤으로 적으려다 «10,000t» 이 됐다.
        y_labels=[f"{t:,.0f}kg" for t in ticks],
        series=[Series(name="보유", data=data, tone="good", end_dot=True)],
        markers=markers,
        note=Note(
            tone="neutral" if walked else "warn",
            text=(
                "원장(`inventory_moves`)을 거슬러 올라가 편 값입니다. **앞날은 공란**입니다 — "
                "확정된 도착만 점으로 얹습니다."
                if walked else
                "🔴 창 안에 방향을 모르는 `ADJUST` 가 있어 **과거를 거슬러 올라가지 "
                "않았습니다.** 오늘 칸만 실측입니다."
            ),
        ),
    )


def _demo_stock_chart(n: int, at: int) -> Chart:
    """읽기 실패용 예시 그래프. **요약도 같이 예시값으로 떨어지므로 둘은 여전히 맞는다.**"""
    def pad(head: list) -> list:
        return list(head) + [None] * (n - len(head))

    tail = [None] * at + list(_PROJ) + [None] * max(0, n - at - len(_PROJ))
    return Chart(
        label="창고 재고", y_min=0, y_max=40000,
        y_ticks=[10000, 20000, 30000, 40000],
        y_labels=["10t", "20t", "30t", "40t"],
        series=[
            Series(name="보유", data=pad(_ONHAND), tone="good", end_dot=True),
            Series(name="추정 보유", data=tail[:n], tone="good", dashed=True),
            Series(name="운송 중", data=pad(_IN_TRANSIT), tone="good",
                   dashed=True, opacity=0.5),
        ],
        markers=[Marker(index=6, value=16900, label="폐기 300", tone="bad")],
        note=Note(tone="warn", text="**예시값입니다.** 실제 값을 못 읽었습니다."),
    )
