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

🔴 **읽기에 실패하면 «오류» 라고 적습니다. 예시 숫자로 바꾸지 않습니다.**

  종전에는 어떤 예외든 잡아 예시값(현재고 14,600kg)으로 되돌아갔습니다. 그래서
  DB 가 죽은 날도, 시뮬레이션이 아직 안 걸어간 2028년을 물어본 날도 화면에는
  **그럴듯한 실적 숫자**가 떴습니다. `Source.status` 가 셋을 가릅니다.

  ```text
  OK       읽었고 값이 있다
  NO_DATA  읽었는데 그 실행의 기록 구간 밖이다   ★ 0 이 아니라 «모른다»
  ERROR    읽다가 실패했다                       ★ 숫자를 지어내지 않는다
  ```

🔴 **부르는 조회 다섯이 같은 `(sim_run_id, as_of)` 축에 섭니다.**

    get_inventory_console (sim_run_id, as_of)   items · lots · capacity
    get_inbound_console   (sim_run_id, as_of)   in_transit · receipts · arrival_summary
    get_outbound_console  (sim_run_id, as_of)   reservations
    live_exceptions_at    (sim_run_id, as_of)   그날 살아 있던 물류 문제
    resolved_exceptions_on(sim_run_id, as_of)   그날 닫힌 물류 문제

  ⚠️ 그래도 **모든 칸이 그날 값인 것은 아닙니다.** 되살릴 정본이 아직 없는 축이
     남아 있고(판매가능량이 빼는 예약 축 · 용량 한도 정책),
     응답의 `*_time_basis` 가 그것을 말합니다. 그 사실을 pane 의 `Note` 에
     적습니다 — 안 적으면 "날짜가 안 먹네" 라는 의심을 그대로 받습니다.

🔴 **`None` 은 0 이 아닙니다.** `available_qty_kg` 는 못 읽은 축이 있으면
  `None` 입니다. 0 으로 바꾸면 «팔 게 없다» 는 거짓말이 됩니다 — 이 탭이 맨 위에
  걸어 둔 `principle` 이 바로 그 이야기입니다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from http import HTTPStatus

import psycopg

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
    SourceStatus,
    Stat,
    Table,
)
from app.api.shown_run import SHOWN_SIM_RUN_ID
from app.contracts.core import ITEMS
from app.logistics.agent.exceptions import live_exceptions_at, resolved_exceptions_on
from app.logistics.agent.schemas import ExceptionRow
from app.logistics.console_schemas import (
    ConsoleInboundResponse,
    ConsoleInventoryResponse,
    ConsoleOutboundResponse,
    ConsoleReservation,
)
from app.logistics.console_service import (
    get_inbound_console,
    get_inventory_console,
    get_outbound_console,
    get_reservation_fefo_console,
)
from app.logistics.db import get_connection
from app.logistics.historical_repository import (
    onhand_total_by_day,
    runtime_coverage_at,
    snapshot_days_between,
)

log = logging.getLogger(__name__)

PANES = ("summary", "stock", "inbound", "outbound")

#: 발표 화면이 그리는 품목. 🔴 **재고 축을 좁히는 것이 아니라 «보여 줄 칸» 을 고르는
#: 것이다.** `app/contracts/core.py` 가 *"재고 축은 자유 문자열 — 좁히지 않는다"* 고
#: 못박았고 `console_service` 도 «계약 밖 품목이라고 재고를 숨기지 않는다» 로 짜여
#: 있다. 그 둘은 그대로 두고 **표시 범위만** 여기서 건다 (#675 · 발표 화면 결정).
#:
#: ⚠️ **창고 사용량은 이 필터보다 앞선다** — 그날 실재한 모든 Lot 의 합이다
#:   (`console_service`: "창고 점유는 화면 필터보다 앞선다"). 그래서 품목 카드의
#:   현재고 합과 창고 사용량이 갈릴 수 있고, 그 사실을 카드의 `Note` 에 적는다.
_SCREEN_ITEMS = frozenset(ITEMS)

#: 우선도 어휘. DB 값을 사람 말로만 바꾼다 — 등급을 새로 만들지 않는다.
_SEVERITY = {"CRITICAL": "매우 높음", "HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}

#: 문제 어휘. `detect.py` 가 여는 코드 둘뿐이다.
_EXCEPTION_LABEL = {
    "FRESHNESS_PRESSURE": "신선도 압박",
    "CAPACITY_PRESSURE": "용량 압박",
}


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


def _on_screen(item_name: str | None) -> bool:
    """발표 화면이 그리는 품목인가.

    🔴 **이름을 못 읽으면 숨기지 않는다.** 분류를 못 한 것과 «범위 밖» 은 다른
       사실이고, 못 분류한 재고를 조용히 지우면 그 재고는 아무 데도 안 남는다.
    """
    return item_name is None or item_name in _SCREEN_ITEMS


def _still_working(r: ConsoleReservation) -> bool:
    """그날 **아직 일이 남은** 예약인가 — 발표 화면이 그리는 모집단이다.

    🔴 **«완료» 를 할당 0 · 미할당 0 두 칸만으로 짐작하지 않는다** (#675 지시 §10).
       그날 유도한 상태(`status`)와 출고된 할당(`SHIPPED`)을 같이 본다.

    ```text
    RESERVED · PARTIALLY_ALLOCATED        아직 Lot 을 다 못 골랐다        → 남았다
    잡고 있는 양(allocated + unallocated) > 0                            → 남았다
    ALLOCATED 이고 잡은 양 0 · SHIPPED 할당 있음   전량 출고가 끝났다      → 끝났다
    RELEASED · CANCELLED                   놓아줬다                       → 끝났다
    ```

    ⚠️ **숨기는 것이 아니다.** 뺀 건수를 카드 footer 에 적는다 — Historical 은 그대로다.
    """
    if r.status in ("RESERVED", "PARTIALLY_ALLOCATED"):
        return True
    if r.status in ("RELEASED", "CANCELLED"):
        return False
    if r.allocated_qty_kg > 0 or r.unallocated_qty_kg > 0:
        return True
    return not any(a.status == "SHIPPED" for a in r.allocations)


def _severity_at(row: ExceptionRow, as_of: date) -> tuple[str, str | None]:
    """그날 우선도. 🔴 **미래 값을 과거 화면으로 흘리지 않는다.**

    `touch_exception` 이 `severity` 를 **덮어쓴다** (`SET severity = …`). 그래서 마지막
    갱신이 `as_of` 뒤였다면 지금 행의 우선도는 **그날 우선도가 아니다.** 가르는 자는
    부르는 쪽이라고 `live_exceptions_at` 이 적어 뒀고, 화면에서는 이 함수가 그 일을 한다.

    ```text
    last_detected_as_of <= as_of   그날 뒤로 안 만졌다 → 지금 값이 그날 값이다
    그 밖                          증명 못 한다 → «—»
    ```

    :returns: `(보일 말, 아래 붙일 한 줄)`.
    """
    detected = row.last_detected_as_of
    if detected is None or detected > as_of:
        return "—", "기준일 당시 우선도 확인 불가"
    return _SEVERITY.get(row.severity, row.severity), None


def _summary_pane(
    inv: ConsoleInventoryResponse,
    live: tuple[ExceptionRow, ...],
    resolved: tuple[ExceptionRow, ...],
    uncertainties: tuple[str, ...],
    as_of: date,
) -> Pane:
    """한눈에 보기. **점검이 장부에 남긴 것 → 창고 여유 → 품목별** 순서다.

    🔴 **«오늘» 이라고 적지 않는다.** 이 화면은 과거 `as_of` 도 연다 — 실제 오늘과
       요청받은 기준일이 다를 수 있다.

    🔴 **입고 후 / 출고 후로 건수를 나누지 않는다.** `logistics_exceptions` 에 어느
       점검이 만졌는지 적는 칸이 없고, 걷기의 점검 결과(`InspectionOut`)는 어느 표에도
       안 남는다. 나누면 근거 없는 숫자가 된다 (#675 · 실측 2026-09-15).

    ⚠️ **Lot 위험 수와 Exception 수는 다른 지표다.** 만료된 Lot 은 새 문제로 다시
       열지 않으므로, 재고·신선도 탭의 «폐기 검토» 를 여기 건수에 더하지 않는다.
    """
    열린것 = [row for row in live if row.opened_as_of == as_of]
    이어진것 = [row for row in live if row.opened_as_of < as_of]
    용량압박 = [row for row in live if row.code == "CAPACITY_PRESSURE"]

    #  Lot → 품목. 🔴 **못 찾으면 비워 둔다** — 지어내지 않는다.
    lot_item = {lot.lot_id: lot.item_name for lot in inv.lots}

    def 문제_행(row: ExceptionRow, 상태: str) -> dict[str, str | float | int | None]:
        우선도, 단서 = _severity_at(row, as_of)
        품목 = lot_item.get(row.subject_id)
        본날 = row.last_detected_as_of
        return {
            "kind": _EXCEPTION_LABEL.get(row.code, row.code),
            "subject": f"{품목} · {row.subject_id}" if 품목 else row.subject_id,
            "sev": 우선도 if 단서 is None else f"{우선도} ({단서})",
            "opened": _md(row.opened_as_of),
            "seen": _md(본날) if 본날 is not None and 본날 <= as_of else "—",
            "state": 상태,
        }

    문제_표 = [
        *[문제_행(row, "신규") for row in 열린것],
        *[문제_행(row, "지속 중") for row in 이어진것],
        *[문제_행(row, "해소됨") for row in resolved],
    ]

    미확인 = (
        None
        if not uncertainties
        else Note(
            tone="warn",
            text=(f"**목록이 확정되지 않았습니다** — 닫힌 날을 못 댄 문제 "
                  f"{len(uncertainties)}건이 있어 이 건수를 단정하지 않습니다."),
        )
    )

    cap = inv.capacity
    보장 = cap.guaranteed_capacity_kg
    사용률 = None if 보장 is None or 보장 <= 0 else float(cap.used_capacity_kg / 보장 * 100)
    여유 = None if 보장 is None else 보장 - cap.used_capacity_kg

    return Pane(
        key="summary",
        label="한눈에 보기",
        stats=[
            Stat(label="신규", value=f"{len(열린것):,}", unit="건",
                 detail=f"기준일 {as_of} 에 새로 열린 문제",
                 tone="bad" if 열린것 else "good", raw=float(len(열린것))),
            Stat(label="지속 중", value=f"{len(이어진것):,}", unit="건",
                 detail="그 전에 열려 아직 해소되지 않은 문제",
                 tone="warn" if 이어진것 else "good", raw=float(len(이어진것))),
            Stat(label="해소", value=f"{len(resolved):,}", unit="건",
                 detail="기준일에 조건이 없어져 닫힌 문제",
                 tone="good", raw=float(len(resolved))),
            Stat(label="용량 압박", value=f"{len(용량압박):,}", unit="건",
                 detail="창고 자리 부족으로 열린 문제",
                 tone="bad" if 용량압박 else "good", raw=float(len(용량압박))),
        ],
        cards=[
            Card(
                key="exceptions", title="기준일 물류 이상 현황",
                subtitle="입고 후와 출고 후 점검이 장부에 남긴 문제입니다",
                source_ref="logistics_exceptions",
                lead=미확인 or Note(
                    tone="info",
                    text=("물류 점검은 걷기에서 **입고 후와 출고 후** 각각 한 번씩 돕니다. "
                          "어느 점검이 찾았는지는 장부에 남지 않아 **나누어 세지 않습니다.** "
                          "감지와 기록까지가 자동이고, 대응 여부는 담당자가 정합니다."),
                ),
                table=_t(
                    [("kind", "이상 유형", "left"), ("subject", "대상", "left"),
                     ("sev", "우선도", "left"), ("opened", "최초 감지", "left"),
                     ("seen", "최근 확인", "left"), ("state", "상태", "left")],
                    문제_표,
                    empty_text="기준일에 열려 있거나 해소된 물류 문제가 없습니다",
                ),
                footer=("우선도 · 최근 확인은 마지막 갱신이 기준일 뒤면 «—» 로 둡니다 — "
                        "그날 값을 증명할 수 없기 때문입니다."),
            ),
            Card(
                key="capacity", title="창고 수용 여유",
                subtitle="압박 건수는 위 장부에서, 아래 수치는 재고·정책 정본에서 옵니다",
                source_ref="inventory_moves · agent_policy_config",
                stats=[
                    Stat(label="현재 사용량", value=_kg(cap.used_capacity_kg), unit="kg",
                         detail="기준일 원장 기준 · 화면 품목 필터보다 앞섭니다",
                         raw=_raw(cap.used_capacity_kg)),
                    Stat(label="보장 용량", value=_kg(보장) if 보장 is not None else "—",
                         unit="kg" if 보장 is not None else None,
                         detail="지금 활성 정책 값입니다 (기준일 정책 이력이 아닙니다)",
                         raw=_raw(보장)),
                    Stat(label="최대 수용량",
                         value=_kg(cap.burst_capacity_kg) if cap.burst_capacity_kg else "—",
                         unit="kg" if cap.burst_capacity_kg else None,
                         detail="지금 활성 정책 값입니다", raw=_raw(cap.burst_capacity_kg)),
                    Stat(label="추가 수용 가능량", value=_kg(여유) if 여유 is not None else "—",
                         unit="kg" if 여유 is not None else None,
                         detail=(f"보장 용량의 {사용률:.1f}% 사용" if 사용률 is not None
                                 else "보장 용량을 못 읽어 계산하지 않습니다"),
                         tone="good" if 여유 is not None and 여유 > 0 else "warn",
                         raw=_raw(여유)),
                ],
            ),
            Card(
                key="items", title="품목별 재고 현황",
                subtitle=f"발표 화면은 {' · '.join(ITEMS)} 를 그립니다",
                source_ref="inventory_lots · inventory_reservations",
                lead=Note(
                    tone="info",
                    text=("**현재고는 기준일 원장 값이고, 판매가능량 · 예약량은 «지금» "
                          "기준입니다.** 시간축이 달라 이 값들을 서로 빼거나 더하지 "
                          "않습니다. 창고 사용량은 화면에 안 그리는 품목까지 포함한 "
                          "실물 합계입니다."),
                ),
                table=_t(
                    [("item", "품목", "left"), ("onhand", "현재고", "right"),
                     ("avail", "판매가능량", "right"), ("resv", "예약량", "right"),
                     ("expired", "만료 수량", "right"), ("risk", "신선도 위험 Lot", "right")],
                    [
                        {
                            "item": it.item_name,
                            "onhand": _kg_cell(it.on_hand_qty_kg),
                            "avail": _kg_cell(it.available_qty_kg),
                            "resv": _kg_cell(it.reserved_qty_kg),
                            "expired": _kg_cell(it.expired_qty_kg),
                            "risk": it.sell_priority_lot_count + it.expired_lot_count,
                        }
                        for it in inv.items
                        if _on_screen(it.item_name)
                    ],
                    empty_text="기준일에 그릴 품목이 없습니다",
                ),
                footer=("판매가능량이 «—» 면 못 읽은 축이 있다는 뜻입니다 — 0 이 아닙니다. "
                        "신선도 위험 Lot 은 우선판매 신호와 만료 Lot 을 함께 센 수이고, "
                        "위 이상 건수와 같은 지표가 아닙니다."),
            ),
        ],
    )

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
        #  ★ **«기준일 값» 이라고 적지 않는다.** 이 숫자가 빼는 예약·할당 축은
        #    Runtime 축의 지금 값이다 (`available_qty_time_basis`).
        avail_detail = "예약 · 할당 · 신선도 반영 서버 계산값 (예약 축은 «지금» 기준)"

    return Pane(
        key="stock",
        label="재고 · 신선도",
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
                        if _on_screen(r.item_name) and _still_working(r)
                    ],
                    empty_text="확보된 재고가 없습니다",
                ),
            ),
            Card(
                key="lots", title="Lot 상태",
                subtitle="기준일 원장 잔량 + turnover 계산 결과",
                source_ref="inventory_moves · inventory_lots · item_turnover_policies",
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
                        if _on_screen(lo.item_name)
                    ],
                    empty_text="이 날짜에 살아 있는 Lot 이 없습니다",
                ),
                footer="신선도 잔여가 음수인 Lot 은 폐기 검토 대상입니다.",
            ),
            Card(
                key="principle", title="재고 처리 원칙",
                lead=_MIXED_AXIS_NOTE,
                bullets=[
                    "예약과 할당은 재고를 줄이지 않습니다 — 실출고 때 줄어듭니다",
                    "판매가능량은 화면이 계산하지 않습니다. 서버 값을 그대로 씁니다",
                    "등급 미확정 Lot 은 «미확정» 으로 적습니다 — 특으로 넘겨짚지 않습니다",
                    "현재고 · Lot 잔량은 원장(inventory_moves)을 기준일까지 더한 값입니다",
                    "Lot 상태는 저장된 값이 아니라 그날 사실에서 유도합니다",
                ],
            ),
        ],
    )


def _inbound_pane(inb: ConsoleInboundResponse) -> Pane:
    summary = inb.arrival_summary
    transit = inb.in_transit

    return Pane(
        key="inbound",
        label="입고 · 검수",
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
                        if _on_screen(r.item_name)
                    ],
                    empty_text="이 날짜까지 도착한 Receipt 이 없습니다",
                ),
                footer="재고가 되는 것은 주문 수량이 아니라 **합격 수량**입니다.",
            ),
        ],
    )


def _outbound_pane(ob: ConsoleOutboundResponse, as_of: date) -> Pane:
    #  ★ FEFO 는 **예약 한 건마다** 묻는다. 예약이 없으면 물어볼 대상도 없다.
    #    예약이 없을 때 Lot 을 신선도순으로 늘어놓아 «후보» 라고 부르지 않는다 —
    #    그건 서비스에 없는 계산을 화면이 새로 만드는 것이다 (#415).
    fefo_rows: list[dict] = []
    #  ★ 화면이 그리는 품목만 · 그날 아직 일이 남은 예약만 — 범위 밖 품목이나 이미 다
    #    나간 예약에 FEFO 를 물을 이유가 없다.
    품목_예약 = [r for r in ob.reservations if _on_screen(r.item_name)]
    화면_예약 = [r for r in 품목_예약 if _still_working(r)]
    끝난_예약 = len(품목_예약) - len(화면_예약)
    #  🔴 **FEFO 는 «아직 Lot 을 안 고른 몫» 이 있는 예약에만 묻는다.** 목표량이 0 이면
    #     `allocate_reserved_stock_fefo` 도 아무것도 안 하므로 후보를 구할 이유가 없다.
    #     예약 164건에 164번 묻던 것이 대시보드 8.6초의 태반이었다 (마스터 실측 2026-09-15).
    for resv in 화면_예약:
        if resv.unallocated_qty_kg <= 0:
            continue
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
        label="예약 · 출고",
        stats=[
            Stat(label="예약", value=f"{len(화면_예약):,}", unit="건",
                 detail=(f"전량 출고·해제된 {끝난_예약}건은 뺐습니다" if 끝난_예약 else None),
                 tone="good" if 화면_예약 else "neutral",
                 raw=float(len(화면_예약))),
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
                          "예약이 없으면 후보도 없습니다. **예약 · 할당 목록은 기준일 "
                          "값입니다** — 예약 해제일(released_as_of) · 할당 결정 시각 · "
                          "출고 Move 로 그날 상태를 되살립니다. 🔴 **후보의 «가용» 은 "
                          "«지금» 재고입니다** — Lot 잔량에서 살아 있는 할당을 뺀 값이라 "
                          "기준일로 되살리지 않습니다."),
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


#  ── 읽기 결과 ────────────────────────────────────────────────────────────
#
#  🔴 **실패해도 화면은 뜨지만, 숫자를 지어내지 않는다.** 종전에는 이 자리에
#     예시 재고(14,600kg)가 있었고 그것이 실적처럼 나갔다.


_PRINCIPLE = Note(
    tone="neutral",
    text=("재고 수치는 **물류가 보고한 것만** 적습니다. 보고가 없는 날은 "
          "0 이 아니라 **공란**입니다 — 둘은 다릅니다."),
)

#: **그날 값이 아닌** 칸들. 이유가 둘로 갈린다 — 되살릴 정본이 없거나(Zone 자리
#: 정원), 축을 일부러 «지금» 에 둔 것이거나(판매가능량 · `console_schemas`
#: `available_qty_time_basis` 주석). 화면이 그 사실을 읽고 적는다.
_MIXED_AXIS_NOTE = Note(
    tone="warn",
    text=("**판매가능량 · Zone 자리 수는 «지금» 값입니다.** 판매가능량은 «지금 더 팔 "
          "수 있나» 를 답하는 값이라 예약·할당의 지금 상태를 빼고, Zone 자리 정원에는 "
          "유효일이 없어 기준일로 되살릴 수 없습니다. 현재고 · Lot · 신선도 · 회전 · "
          "Receipt · Lot 자리 · 예약·할당 목록은 기준일 값입니다."),
)


def _empty_pane(key: str, label: str, note: Note, stats: list[Stat] | None = None) -> Pane:
    """값이 없거나 못 읽은 pane. 🔴 **표를 예시 행으로 채우지 않는다.**"""
    return Pane(
        key=key,
        label=label,
        stats=stats or [],
        cards=[Card(key="state", title=label, lead=note)],
    )


def _empty_tab(*, status: SourceStatus, note: Note, source_note: str) -> LogisticsTab:
    """네 pane 을 비운 탭. `status` 가 «없음» 과 «실패» 를 가른다.

    ★ **재고 요약 칸은 남기되 값을 «—» 로 둔다.** 대시보드가 이 칸을 그대로 실어
      가는데(`lg.panes[0].stats[0]`), 칸을 없애면 대시보드가 통째로 죽는다.
      🔴 `raw=None` 이라 계산에도 안 섞인다 — 0 으로 메우지 않는 그 규율이다.

    ★ `filled=True` 다. 「예시값」 띠는 *"이 파트가 아직 실제 값에 안 붙었다"* 는
      뜻이고, 지금은 붙어 있는데 **그날 사실이 없거나 읽기가 실패한** 것이다.
      그 구분은 `status` 가 한다.
    """
    return LogisticsTab(
        panes=[
            _empty_pane(
                "summary",
                "한눈에 보기",
                note,
                [Stat(label="신규", value="—", detail=note.text, tone="warn", raw=None)],
            ),
            _empty_pane(
                "stock",
                "재고 · 신선도",
                note,
                [Stat(label="현재고 합계", value="—", detail=note.text, tone="warn", raw=None)],
            ),
            _empty_pane("inbound", "입고 · 검수", note),
            _empty_pane("outbound", "예약 · 출고", note),
        ],
        selected="summary",
        principle=_PRINCIPLE,
        source=Source(filled=True, owner="물류", note=source_note, status=status),
    )


@dataclass(frozen=True)
class LogisticsTabResult:
    """탭 한 판과 **그 판이 나가야 할 HTTP 코드.**

    🔴 **읽기 실패를 `200 OK` 로 내보내지 않는다.** 본문에 `status="ERROR"` 를 적어도
       HTTP 가 200 이면 그 응답은 **성공으로 캐시되고 성공으로 집계되고 성공으로
       재시도되지 않는다.** 계약은 그 반대다.

    ```text
    OK · NO_DATA        200   읽었다. 값이 있거나, 그날이 없다
    ERROR · DB 접속 실패 503   지금은 못 읽는다 — 다시 오면 될 수 있다
    ERROR · 그 밖        500   이 요청은 여기서 깨졌다
    ```

    ★ **본문은 그대로 `LogisticsTab` 이다.** 오류라고 `{"detail": …}` 로 바꾸지
      않는다 — 화면은 `Source.status` 와 pane 문구를 읽어 «왜» 를 보여 줘야 한다.
    """

    tab: LogisticsTab
    http_status: int


#: DB 에 **닿지 못한** 실패. 값이 틀린 것이 아니라 지금 못 읽는 상태라 503 이다.
#:
#: 🔴 `psycopg.OperationalError` 하나만 여기 둔다. 그 밑에 `ProgrammingError`(SQL 잘못) ·
#:    `IntegrityError` 는 **우리 코드가 깨진 것**이라 503 으로 재시도를 권하면 안 된다.
_DB_UNAVAILABLE: tuple[type[BaseException], ...] = (psycopg.OperationalError,)


def _http_status_for_error(error: BaseException) -> int:
    return (
        HTTPStatus.SERVICE_UNAVAILABLE
        if isinstance(error, _DB_UNAVAILABLE)
        else HTTPStatus.INTERNAL_SERVER_ERROR
    )


def build_result(as_of: date, pane: str) -> LogisticsTabResult:
    """재고·물류 탭 한 판 + HTTP 코드. **네 조회가 같은 `(sim_run_id, as_of)` 축에 선다.**

    🔴 **실패를 예시값으로 바꾸지 않는다.** 예외를 통째로 잡는 것은 그대로다 —
       무슨 일이 나든 화면은 떠야 하기 때문이다. 바뀐 것은 **그때 무엇을 내려
       보내는가**다: 예시 숫자가 아니라 빈 화면 · `status="ERROR"` · 503/500 이다.

    🔴 **`NO_DATA` 는 그날 Runtime Snapshot 이 없다는 뜻이다.**
       물리 사실(Lot · Move · Receipt)의 최소~최대 구간으로 판정하지 않는다 —
       그 판정은 **안 연 날을 열렸다고 하고(실측 31일) 열린 날을 모른다고 한다
       (실측 245일).** 입·출고가 0 건인 정상 하루는 물리 사실을 아예 안 남긴다.
    """
    run = SHOWN_SIM_RUN_ID
    try:
        with get_connection() as conn:
            coverage = runtime_coverage_at(conn, sim_run_id=run, as_of=as_of)
        if not coverage.has_snapshot:
            return LogisticsTabResult(
                tab=_empty_tab(
                    status="NO_DATA",
                    note=Note(
                        tone="warn",
                        text=(f"**{as_of} 은 이 실행이 연 날이 아닙니다** — 그날 "
                              "Runtime Snapshot 이 없습니다. 0 이 아니라 "
                              "**아직 모르는 날**입니다. "
                              f"이 실행이 연 날: {coverage.first_as_of} ~ "
                              f"{coverage.last_as_of}."),
                    ),
                    source_note=(
                        f"logistics_runtime_fixture 없음 · 보고 있는 실행: {run} · 기준일: {as_of}"
                        f" (열린 구간 {coverage.first_as_of}~{coverage.last_as_of})"
                    ),
                ),
                http_status=HTTPStatus.OK,
            )
        inv = get_inventory_console(sim_run_id=run, as_of=as_of)
        inb = get_inbound_console(sim_run_id=run, as_of=as_of)
        ob = get_outbound_console(sim_run_id=run, as_of=as_of)
        #  🔴 **문제 장부도 같은 `(sim_run_id, as_of)` 축이다.** 다른 실행의 문제를
        #     섞지 않고 그날 뒤에 열린 문제도 싣지 않는다 — 그 두 규칙의 주인은
        #     `live_exceptions_at` 하나다. 그날 닫힌 행은 저 함수가 안 내므로
        #     `resolved_exceptions_on` 이 나머지 반쪽을 가져온다.
        with get_connection() as conn:
            live = live_exceptions_at(conn, sim_run_id=run, as_of=as_of)
            resolved = resolved_exceptions_on(conn, sim_run_id=run, as_of=as_of)
        panes = [
            _summary_pane(inv, live.rows, resolved, live.uncertainties, as_of),
            _stock_pane(inv, inb, ob),
            _inbound_pane(inb),
            _outbound_pane(ob, as_of),
        ]
    except Exception as error:  #  DB 미연결 · 표 없음 · 원장/계보 무결성 다 잡는다
        log.exception("물류 값을 못 읽었습니다")
        http_status = _http_status_for_error(error)
        다시 = http_status == HTTPStatus.SERVICE_UNAVAILABLE
        재시도 = "잠시 뒤 다시 열어 보세요." if 다시 else ""
        return LogisticsTabResult(
            tab=_empty_tab(
                status="ERROR",
                note=Note(
                    tone="bad",
                    text=(f"**값을 못 읽었습니다** (`{type(error).__name__}`). "
                          f"예시 숫자로 대신하지 않습니다 — 이 화면에는 지금 사실이 "
                          f"없습니다. {재시도}").strip(),
                ),
                source_note=(
                    f"읽기 실패 ({type(error).__name__}) · 보고 있는 실행: {run} · 기준일: {as_of}"
                ),
            ),
            http_status=http_status,
        )

    return LogisticsTabResult(
        tab=LogisticsTab(
            panes=panes,
            selected=pane,
            principle=_PRINCIPLE,
            source=Source(
                filled=True,
                owner="물류",
                status="OK",
                note=(
                    "inventory_moves · inventory_lots · inbound_receipts · "
                    "inbound_inspections · inventory_reservations · "
                    f"logistics_exceptions · 보고 있는 실행: {run} · 기준일: {as_of}"
                ),
            ),
        ),
        http_status=HTTPStatus.OK,
    )


def build(as_of: date, pane: str) -> LogisticsTab:
    """탭 본문만. **대시보드가 쓰는 진입점이다** (HTTP 코드가 필요 없다).

    ★ 계산은 `build_result` 하나가 한다 — 같은 판을 두 벌 만들지 않는다.
    """
    return build_result(as_of, pane).tab


def _ceiling(value: float) -> float:
    """눈금 꼭대기. 1 · 2 · 2.5 · 5 계단으로 올린다."""
    if value <= 0:
        return 100.0
    base = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5):
        if value <= step * base:
            return step * base
    return 10 * base


def _onhand_series(as_of: date, n: int, at: int) -> list[float | None]:
    """날짜축 칸마다의 창고 보유량. **원장 누계이고, 안 연 날은 공란이다.**

    ```text
    opening(start−1)  =  Σ(moved_at <  start)
    on_hand(D)        =  on_hand(D−1) + IN(D) − OUT(D) − DISPOSE(D)
    ```

    🔴 **현재 잔량을 앵커로 잡고 거슬러 올라가지 않는다.** 종전에는
       `Σ inventory_lots.remaining_qty_kg` 를 오늘 칸에 놓고 역산했는데, 그 앵커가
       **Current Cache** 라 모든 과거 칸이 같이 틀렸다 (네 기준일 전부 0 kg).

    🔴 **`limit` 으로 원장을 자르지 않는다.** 종전 `limit=1000` 은 잘린 줄이 하나만
       생겨도 선 전체를 조용히 틀어 놓는다. 이제 합은 DB 가 낸다.

    🔴 **Runtime Snapshot 이 없는 날은 `None` 이다 — 0 이 아니다.** 원장 누계는 첫
       사실 이전 날짜에도 숫자를 내고 그 값이 **0** 인데, 화면 계약에서

    ```text
    0      확인했고 재고가 없다
    공란   그날을 모른다
    ```

       는 다른 값이다. 안 가르면 **이 실행이 열지도 않은 날이 «재고 0kg» 선**으로
       그려진다 (실측: 창 시작이 씨앗 구간이면 실제로 그렇게 나왔다).

    ★ **앞날은 그리지 않는다** — `as_of` 뒤 칸은 `None` 이다. 확정된 도착만
      `markers` 로 얹는다.

    ⚠️ `ADJUST` 는 `historical_repository` 가 예외로 막는다. 방향을 모르는 이동을
       0 이나 `IN` 으로 넘겨짚어 그린 선은 틀렸다는 것조차 알려 주지 않는다.
    """
    start = as_of - timedelta(days=at)
    with get_connection() as conn:
        series = onhand_total_by_day(
            conn, sim_run_id=SHOWN_SIM_RUN_ID, start=start, end=as_of
        )
        열린_날 = snapshot_days_between(
            conn, sim_run_id=SHOWN_SIM_RUN_ID, start=start, end=as_of
        )
    data: list[float | None] = [None] * n
    for index in range(at + 1):
        day = start + timedelta(days=index)
        # 🔴 열린 날에만 숫자를 적는다. 안 연 날의 0 은 «없다» 가 아니라 «모른다» 다.
        if day in 열린_날 and day in series:
            data[index] = float(series[day])
    return data


def dashboard_stock(n: int, at: int, as_of: date) -> Chart:
    """대시보드에 얹을 재고 그래프.

    ★ **대시보드가 아니라 여기서 만듭니다.** 요약 숫자와 같은 곳에서 나와야
      둘이 안 갈라집니다. 실제로 갈라졌던 적이 있습니다 — 요약은 4,550kg 인데
      그래프 끝은 14,600kg 이었습니다.

    🔴 **못 읽으면 빈 그래프에 「오류」 라고 적습니다.** 종전에는 예시 선
       (`_ONHAND` · `_PROJ`)으로 되돌아갔고, 그 선은 실적처럼 보였습니다.

    🔴 **칸마다 그날이 열렸는지 따로 봅니다.** 선택한 `as_of` 하나만 보면 창 앞쪽의
       안 연 날들이 0kg 으로 그려집니다 (`_onhand_series` 참조).
    """
    try:
        with get_connection() as conn:
            coverage = runtime_coverage_at(
                conn, sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of
            )
        if not coverage.has_snapshot:
            return _empty_stock_chart(
                n,
                Note(
                    tone="warn",
                    text=(f"**{as_of} 은 이 실행이 연 날이 아닙니다** "
                          f"(열린 구간 {coverage.first_as_of} ~ {coverage.last_as_of})."),
                ),
            )
        data = _onhand_series(as_of, n, at)
        inb = get_inbound_console(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
    except Exception as error:  #  DB 미연결 · 표 없음 · 원장 이상 다 잡는다
        log.exception("재고 그래프를 못 읽었습니다")
        return _empty_stock_chart(
            n,
            Note(
                tone="bad",
                text=(f"**재고 추이를 못 읽었습니다** (`{type(error).__name__}`). "
                      "예시 선으로 대신하지 않습니다."),
            ),
        )

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
            tone="neutral",
            text=("원장(`inventory_moves`)을 날마다 더해 편 값입니다. **앞날과 이 실행이 "
                  "열지 않은 날은 공란**입니다 — 확정된 도착만 점으로 얹습니다."),
        ),
    )


def _empty_stock_chart(n: int, note: Note) -> Chart:
    """값이 없거나 못 읽은 그래프. 🔴 **선을 지어내지 않는다 — 전부 공란이다.**"""
    return Chart(
        label="창고 재고", y_min=0, y_max=100,
        y_ticks=[25, 50, 75, 100],
        y_labels=["25kg", "50kg", "75kg", "100kg"],
        series=[Series(name="보유", data=[None] * n, tone="good")],
        note=note,
    )
