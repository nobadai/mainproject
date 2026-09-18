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
from typing import Any

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
from app.logistics.console_service import (
    get_fefo_candidates_by_item,
    get_inbound_console,
    get_inventory_console,
    get_outbound_console,
    load_console_runtime,
)
from app.logistics.db import get_connection
from app.logistics.historical_repository import (
    onhand_total_by_day,
    reservation_state_at,
    runtime_coverage_at,
    snapshot_days_between,
)
from app.logistics.inbound_schedules import (  # noqa: F401  아래 주석대로 밖에 여는 이름이다
    schedule_view_scope as read_scope,
)
from app.logistics.monitoring.exceptions import live_exceptions_at, resolved_exceptions_on
from app.logistics.monitoring.schemas import DetectionRecord, ExceptionRow
from app.logistics.schemas import (
    ConsoleInboundResponse,
    ConsoleInventoryResponse,
    ConsoleOutboundResponse,
    ConsoleReservation,
)

log = logging.getLogger(__name__)

#  🔵 **`read_scope()` — 한 화면이 물류를 두 갈래로 읽을 때 감싸는 범위** (2026-09-17).
#
#     `build()` 와 `dashboard_stock()` 은 서로를 모르므로 같은 `(실행, 기준일)` 일정
#     조회를 **각자 한 번씩** 보낸다 (실측 0.145s + 0.139s). 이 범위 안에서는 처음
#     한 번만 읽는다. **읽기 전용 한 판에만 쓴다** — 규칙과 경고는 저쪽 docstring 에.
#
#     ★ 물류 안쪽(`app.logistics.*`)을 대시보드가 직접 임포트하지 않게 **여기로만**
#       연다. 이 파일이 물류를 읽는 유일한 길이라는 규율(파일 머리)을 그대로 지킨다.

PANES = ("summary", "stock", "inbound", "outbound")

#: 발표 화면이 그리는 품목. 🔴 **재고 축을 좁히는 것이 아니라 «보여 줄 칸» 을 고르는
#: 것이다.** `app/contracts/core.py` 가 *"재고 축은 자유 문자열 — 좁히지 않는다"* 고
#: 못박았고 `read_service` 도 «계약 밖 품목이라고 재고를 숨기지 않는다» 로 짜여
#: 있다. 그 둘은 그대로 두고 **표시 범위만** 여기서 건다 (#675 · 발표 화면 결정).
#:
#: ⚠️ **창고 사용량은 이 필터보다 앞선다** — 그날 실재한 모든 Lot 의 합이다
#:   (`read_service`: "창고 점유는 화면 필터보다 앞선다"). 그래서 품목 카드의
#:   현재고 합과 창고 사용량이 갈릴 수 있고, 그 사실을 카드의 `Note` 에 적는다.
_SCREEN_ITEMS = frozenset(ITEMS)

#: 우선도 어휘. DB 값을 사람 말로만 바꾼다 — 등급을 새로 만들지 않는다.
_SEVERITY = {"CRITICAL": "매우 높음", "HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}

#: 문제 어휘. `detect.py` 가 여는 코드 둘뿐이다.
_EXCEPTION_LABEL = {
    "FRESHNESS_PRESSURE": "신선도 확인 필요",
    "CAPACITY_PRESSURE": "창고 여유 확인 필요",
}

#  ── 사용자 표시 사전 ─────────────────────────────────────────────────────
#
#  ★ **표시만 바꾼다.** 여기 있는 것은 전부 물류가 이미 판정해 준 값을 사람 말로 옮기는
#    사전이다. 숫자를 보고 상태를 다시 매기지 않는다 — 그 판정의 주인은 도메인이다.
#
#  ★ **키는 실제 계약값이다.** 각 사전에 정본 위치를 적어 둔다. 계약에 없는 상태를
#    지어내지 않고, 계약이 늘면 여기도 같이 는다.

#  🔴 **회전(turnover)은 이 화면에 안 적는다** (#812).
#
#     종전에는 Lot 표에 「회전 잔여」·「회전 상태」 두 칸이 있었고 내부 어휘를 그대로
#     옮긴 사전(`_TURNOVER_LABEL`: 정상 · 판매 우선 검토 · 회전목표 초과)이 여기 있었다.
#     신선도 잔여 옆에 비슷한 숫자가 하나 더 서서, 사용자는 **둘 중 무엇을 봐야 하는지**
#     알 수 없었다 — 회전은 회사 내부 관리 지표이고 「회전목표 초과」는 판매불가가
#     아니라, 화면에서 둘을 나란히 두면 없는 위험처럼 읽힌다.
#
#     ⚠️ **계산도 정책도 그대로다.** `turnover.py` · `item_turnover_policies` ·
#        Agent 판단(`query/tools.LotFact` · `status_query`) 어느 것도 안 건드렸고,
#        `ConsoleInventoryLot.remaining_turnover_days` · `turnover_status` 도 응답에
#        그대로 실린다. 빠진 것은 **이 표가 그 둘을 그리는 일** 하나뿐이다.
#
#     ★ 회전에서 나온 **업무 신호**는 남는다 — 「우선 출고 대상」 · 「폐기 검토 대상」
#       (`_lot_action`)은 Backend 가 이미 낸 판정이고, 화면이 새로 만든 것이 아니다.

#: 예약 상태. 정본 `app/logistics/outbound.py::ReservationStatus`.
#:
#: 🔴 **«할당» 이 무엇의 할당인지 적는다** (#812). 종전 「예약」·「일부 할당」은 실패나
#:    보류처럼 읽혔다 — `RESERVED` 는 *"수량은 확보됐고 어느 Lot 에서 낼지만 아직"* 이라
#:    정상 진행 상태다. 계약값(`ReservationStatus`)은 그대로 두고 **표시만** 바꾼다.
#:
#: ```text
#: RESERVED             수량 확보 완료 · Lot 은 아직
#: PARTIALLY_ALLOCATED  일부만 Lot 이 정해졌다
#: ALLOCATED            전량 Lot 이 정해졌다 (아직 나간 것은 아니다)
#: ```
_RESERVATION_LABEL = {
    "RESERVED": "수량 확보 · Lot 미배정",
    "PARTIALLY_ALLOCATED": "일부 Lot 배정",
    "ALLOCATED": "Lot 배정 완료",
    "RELEASED": "예약 해제",
    "CANCELLED": "취소",
}

#: 도착 건 진행 상태. 정본 `app/logistics/receipts.py::ReceiptStatus`.
#: ★ 사용자가 볼 흐름은 **입고 예정 → 창고 도착 → 검수 → 재고 반영** 넷이다.
_RECEIPT_LABEL = {
    "ARRIVED": "창고 도착",
    "INSPECTING": "검수 중",
    "INSPECTED": "검수 완료",
    "PUTAWAY_DONE": "재고 반영 완료",
    "CLOSED": "종료",
}

#: 검수 판정. 정본 `app/logistics/inspections.py::InspectionVerdict`.
_VERDICT_LABEL = {"PASS": "합격", "HOLD": "보류", "REJECT": "거절"}


def _receipt_progress(row: Any) -> tuple[str, str]:
    """도착한 물량 한 줄의 **처리 상태**와 **재고 처리**. 🔴 판정을 새로 만들지 않는다.

    ```text
    창고 도착        검수 전                      재고: 검수 대기
    검수 완료        검수는 끝 · 재고 아직        재고: 반영 대기
    처리 완료        stock_applied               재고: 재고 반영 완료
    처리 완료        settled_without_stock (#805) 재고: 반영할 재고 없음
    ```

    🔴 **«입고 처리 완료» 와 «재고 반영 완료» 는 다른 사실이다** (#805). 수용할 것이
       0 이라 재고를 안 만들고 끝난 입고도 **처리는 끝난 것**이다 — 「아직」으로 적으면
       도착 요약에서는 빠진 건이 이 표에서만 영영 밀린 것처럼 보인다.

    🔴 **실패라고 적지 않는다.** 만들 재고가 없던 것이지 처리가 실패한 것이 아니다.

    ⚠️ **`accepted_qty_kg == 0` 으로 여기서 다시 판정하지 않는다.** 그 규칙의 주인은
       `inbound_schedules.settled_without_stock` 하나다. 그 값이 `None`(그날 일정을
       못 읽음)이면 둘을 가릴 수 없으므로 «—» 로 둔다 — 넘겨짚지 않는다.
    """
    if row.stock_applied:
        return "처리 완료", "재고 반영 완료"
    if row.settled_without_stock:
        return "처리 완료", "반영할 재고 없음"
    if row.receipt_status == "ARRIVED":
        return "창고 도착", "검수 대기"
    #  검수는 끝났는데 재고가 없다 — 「반영 대기」인지 「반영할 재고 없음」인지는
    #  일정이 낸 사실로만 갈린다. 못 읽었으면 가리지 않는다.
    if row.settled_without_stock is None:
        return _label(_RECEIPT_LABEL, row.receipt_status) or "—", "—"
    return "검수 완료", "반영 대기"


def _label(table: dict[str, str], value: Any) -> str | None:
    """사전에 있으면 사람 말로, 없으면 «—». **내부 코드를 그대로 내보내지 않는다.**"""
    if value is None or value == "":
        return None
    return table.get(str(value), "—")


def _lot_names(lots: Any) -> dict[str, str]:
    """Lot 하나하나의 **사용자 표시명.** 🔴 **raw `lot_id` 를 쪼개지 않는다.**

    구조화 칸(`item_name` · `received_at`)으로만 만들고, 같은 품목·같은 입고일 Lot 이
    여럿일 때만 `lot_id` 정렬로 **안정된 순번**을 덧붙인다.

    ```text
    양파 · 08-12 입고
    무 · 08-28 입고 · #1
    무 · 08-28 입고 · #2
    ```
    """
    groups: dict[tuple[str, Any], list[Any]] = {}
    for lot in lots:
        groups.setdefault((lot.item_name or lot.item_id, lot.received_at), []).append(lot)
    out: dict[str, str] = {}
    for (item, received), members in groups.items():
        head = f"{item} · {_md(received)} 입고" if received is not None else str(item)
        ordered = sorted(members, key=lambda one: one.lot_id)
        for index, lot in enumerate(ordered, start=1):
            out[lot.lot_id] = head if len(ordered) == 1 else f"{head} · #{index}"
    return out


def _lot_action(sell_priority: bool, disposal_candidate: bool) -> str:
    """Lot 관리 조치. 🔴 **boolean 두 칸을 사용자가 조합하게 하지 않는다.**

    ★ 이미 내려온 두 칸만 본다. 신선도 잔여 일수를 보고 둘 중 무엇도 다시 판단하지
      않는다 — 판정의 주인은 `turnover` 다.
    """
    if sell_priority and disposal_candidate:
        return "우선 출고 · 폐기 검토 대상"
    if sell_priority:
        return "우선 출고 대상"
    if disposal_candidate:
        return "폐기 검토 대상"
    return "특이사항 없음"


def _resv_name(row: ConsoleReservation) -> str:
    """예약 한 건의 **사용자 표시명.** raw `reservation_id` · `sale_id` 를 안 쓴다.

    🔴 **납기가 없으면 아무 말도 안 붙인다** (#812).

    ```text
    종전   무 · 납기 미정      «아직 안 정했다» 로 읽힌다 — 담당자가 정할 일이 아니다
    고쳤다 무 · 납기 정보 없음  뜻은 맞지만 **모든 줄에 똑같이** 붙어 아무것도 안 가른다
    지금   무                  없는 칸을 말하지 않는다
    ```

       `due_date` 는 Sales ↔ Logistics 계약에 납기 칸이 없어 **구조적으로 늘 비어 있다**
       (`03_출고관리.md` §O-1: *"계약에 없어 항상 NULL(결함 아님 · #423)"* · 실측 0/495).
       늘 같은 값인 꼬리표는 구분에도 못 쓰고 정보도 아니다 — 여섯 줄에 여섯 번 적힐 뿐이다.

    ★ **계약이 납기를 주기 시작하면 저절로 붙는다.** 지운 것은 «없다» 는 말이지
      날짜를 적는 길이 아니다.
    """
    item = row.item_name or row.item_id
    due = _md(row.due_date)
    return f"{item} · {due} 납기" if due else str(item)


def _t(cols: list[tuple], rows: list[dict], **kw) -> Table:
    """표를 짧게 쓰기 위한 도우미.

    쪽지는 `(키, 이름, 정렬)` 셋이고, **네 번째로 너비**를 줄 수 있다.

    ```text
    ("item", "품목", "left")           너비는 균등 배분
    ("item", "품목", "left", "14%")    이 표에서 이 칸은 14%
    ```

    🔴 **너비는 표마다 정한다** (#812). 칸이 넷인 표와 일곱인 표에 같은 규칙을 걸 수
       없다 — 그 사실을 `Column.width` 주석에 적어 뒀다.
    """
    return Table(
        columns=[
            Column(
                key=쪽지[0],
                label=쪽지[1],
                align=쪽지[2],
                mono=(쪽지[2] == "right" or 쪽지[0] in ("id", "lot")),
                width=쪽지[3] if len(쪽지) > 3 else None,
            )
            for 쪽지 in cols
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


#: 그날 우선도를 증명할 수 없을 때 칸에 붙이는 말.
#:
#: 🔴 **«기준일 당시 우선도 확인 불가» 라고 적지 않는다** (#812). 구현 사정(감지 이력이
#:    그날 이하로 없다)이 그대로 드러나 사용자가 «내가 뭘 잘못 골랐나» 로 읽는다.
#:    결과만 짧게 적고, **왜 그런지는 카드 footer 가 한 번 말한다.**
_SEVERITY_UNKNOWN = "우선도 정보 없음"


def _severity_at(row: ExceptionRow, as_of: date) -> tuple[str, str | None]:
    """그날 우선도. 🔴 **미래 값을 과거 화면으로 흘리지 않는다.**

    `touch_exception` 이 `severity` 를 **덮어쓴다** (`SET severity = …`) — 그래서 지금
    행의 `severity` 는 마지막 감지값(캐시)이지 과거값이 아니다. LOG-AGENT-005 로
    `detection_history` 가 «그날 severity» 를 쌓으므로, 그 이력에서 그날 값을 복원한다.

    ```text
    detection_history 에 as_of <= 기준일 원소 있음  → 그중 max(as_of) 원소의 severity
                                                       🔴 배열 순서를 믿지 않는다 — 날짜로 고른다
    이력 있으나 기준일 이하 감지 없음               → «—» (그날 우선도 증명 불가)
    이력 없음(옛 행 · [])                          → 기존 fallback:
        last_detected_as_of <= as_of   지금 값이 그날 값이다
        그 밖                          «—»
    ```

    :returns: `(보일 말, 아래 붙일 한 줄)`.
    """
    chosen: DetectionRecord | None = None
    for record in row.detection_history:
        if record.as_of <= as_of and (chosen is None or record.as_of > chosen.as_of):
            chosen = record
    if chosen is not None:
        return _SEVERITY.get(chosen.severity, chosen.severity), None
    if row.detection_history:
        # 이력은 있으나 기준일 이하 감지가 없다 — 그날 우선도를 증명할 수 없다.
        return "—", _SEVERITY_UNKNOWN
    # 이력 없는 옛 행(적용 전 생성) — 기존 규칙 그대로.
    detected = row.last_detected_as_of
    if detected is None or detected > as_of:
        return "—", _SEVERITY_UNKNOWN
    return _SEVERITY.get(row.severity, row.severity), None


def _summary_pane(
    inv: ConsoleInventoryResponse,
    inb: ConsoleInboundResponse,
    ob: ConsoleOutboundResponse,
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
    newly_opened = [row for row in live if row.opened_as_of == as_of]
    carried_over = [row for row in live if row.opened_as_of < as_of]
    capacity_issues = [row for row in live if row.code == "CAPACITY_PRESSURE"]

    #  Lot → 사용자 표시명. 🔴 **못 찾으면 지어내지 않는다.**
    lot_name = _lot_names(inv.lots)

    def subject_name(row: ExceptionRow) -> str:
        """문제의 대상을 사람 말로. **raw Lot ID 를 화면에 싣지 않는다.**

        `detect.py` 가 여는 대상은 둘뿐이다 — `LOT` 과 `WAREHOUSE`.
        Lot 이름을 못 찾는 경우(그날 잔량이 0 이라 목록에 없는 Lot)는
        **품목을 지어내지 않고** 「Lot 정보 없음」으로 둔다.
        """
        if row.subject_type == "WAREHOUSE":
            return "창고 전체"
        return lot_name.get(row.subject_id) or "Lot 정보 없음"

    def issue_row(row: ExceptionRow, state: str) -> dict[str, str | float | int | None]:
        severity, hint = _severity_at(row, as_of)
        last_seen = row.last_detected_as_of
        return {
            "kind": _EXCEPTION_LABEL.get(row.code, row.code),
            "subject": subject_name(row),
            #  ★ 못 증명할 때는 «— (긴 설명)» 대신 **짧은 결과 한 마디만** 적는다.
            #    이유는 카드 footer 가 한 번 말한다 (#812).
            "sev": severity if hint is None else hint,
            "opened": _md(row.opened_as_of),
            "seen": _md(last_seen) if last_seen is not None and last_seen <= as_of else "—",
            "state": state,
        }

    issue_rows = [
        *[issue_row(row, "신규") for row in newly_opened],
        *[issue_row(row, "지속 중") for row in carried_over],
        *[issue_row(row, "해소됨") for row in resolved],
    ]

    unconfirmed_note = (
        None
        if not uncertainties
        else Note(
            tone="warn",
            text=(f"**목록이 확정되지 않았습니다** — 닫힌 날을 못 댄 문제 "
                  f"{len(uncertainties)}건이 있어 이 건수를 단정하지 않습니다."),
        )
    )

    cap = inv.capacity
    guaranteed = cap.guaranteed_capacity_kg
    usage_pct = (
        None
        if guaranteed is None or guaranteed <= 0
        else float(cap.used_capacity_kg / guaranteed * 100)
    )
    headroom = None if guaranteed is None else guaranteed - cap.used_capacity_kg

    #  ★ **업무 숫자를 맨 앞에 둔다** — 사용자가 먼저 볼 것은 재고이지 점검 건수가 아니다.
    #    값은 전부 이미 읽어 온 read model 것이고 여기서 새로 세지 않는다.
    shown_items = [it for it in inv.items if _on_screen(it.item_name)]
    on_hand = sum((it.on_hand_qty_kg for it in shown_items), Decimal(0))
    available = _sum([it.available_qty_kg for it in shown_items])
    attention_lots = sum(
        it.sell_priority_lot_count + it.disposal_candidate_lot_count for it in shown_items
    )
    open_issues = len(newly_opened) + len(carried_over)
    working = [r for r in ob.reservations if _on_screen(r.item_name) and _still_working(r)]

    return Pane(
        key="summary",
        label="한눈에 보기",
        stats=[
            Stat(label="현재고", value=_kg(on_hand), unit="kg",
                 detail=f"기준일 창고 보유량 · 품목 {len(shown_items)}종",
                 tone="good" if on_hand > 0 else "warn", raw=_raw(on_hand)),
            Stat(label="판매가능량",
                 value=_kg(available) if available is not None else "—",
                 unit="kg" if available is not None else None,
                 detail=("예약을 뺀 팔 수 있는 양" if available is not None
                         else "못 읽은 축이 있습니다 — 0 이 아닙니다"),
                 tone="good" if available else "warn", raw=_raw(available)),
            Stat(label="창고 여유", value=_kg(headroom) if headroom is not None else "—",
                 unit="kg" if headroom is not None else None,
                 detail=(f"보장 용량의 {usage_pct:.1f}% 사용" if usage_pct is not None
                         else "보장 용량을 못 읽어 계산하지 않습니다"),
                 tone="good" if headroom is not None and headroom > 0 else "warn",
                 raw=_raw(headroom)),
            Stat(label="확인할 문제", value=f"{open_issues:,}", unit="건",
                 detail=(f"새로 열림 {len(newly_opened)}건 · 이어짐 {len(carried_over)}건"
                         + (f" · 창고 여유 {len(capacity_issues)}건" if capacity_issues else "")),
                 tone="bad" if open_issues else "good", raw=float(open_issues)),
        ],
        cards=[
            Card(
                #  🔴 «지금» 이라고 적지 않는다 (#812) — 이 화면은 과거 기준일도 연다.
                #     기준일은 화면 맨 위가 한 번 말한다.
                key="progress", title="진행 중인 일",
                subtitle="들어올 물량 · 내보낼 예약 · 먼저 봐야 할 재고입니다",
                source_ref="inbound_schedules · inventory_reservations · inventory_lots",
                stats=[
                    #  🔴 「입고 예정」 칸을 뺐다 (#812) — 이 실행은 도착 전 상태를
                    #     남기지 않아 어느 기준일에도 값이 없다. 이유는 `_inbound_pane`
                    #     머리말에 적어 뒀다.
                    Stat(label="처리 중 예약", value=f"{len(working):,}", unit="건",
                         detail="아직 내보낼 일이 남은 예약",
                         tone="warn" if working else "good",
                         raw=float(len(working))),
                    Stat(label="신선도 관리 대상", value=f"{attention_lots:,}", unit="Lot",
                         detail="우선 출고하거나 폐기를 검토할 Lot",
                         tone="warn" if attention_lots else "good", raw=float(attention_lots)),
                    Stat(label="기준일에 해소", value=f"{len(resolved):,}", unit="건",
                         detail="조건이 없어져 닫힌 문제",
                         tone="good", raw=float(len(resolved))),
                ],
            ),
            Card(
                key="exceptions", title="확인할 일",
                #  ★ **안내를 subtitle 로 접어 넣었다** (#812). 재무·판매 카드는 설명을
                #    제목 옆 한 구절로 적고 색 박스를 안 쓴다 — 같은 관습을 따른다.
                #    `lead` 는 **진짜 경고**(목록이 확정되지 않음)일 때만 뜬다.
                subtitle="찾아서 적어 두기까지가 자동입니다 — 대응은 담당자가 정합니다",
                source_ref="logistics_exceptions",
                lead=unconfirmed_note,
                table=_t(
                    [("kind", "이상 유형", "left"), ("subject", "대상", "left"),
                     ("sev", "우선도", "left"), ("opened", "최초 감지", "left"),
                     ("seen", "최근 확인", "left"), ("state", "상태", "left")],
                    issue_rows,
                    empty_text="이 기준일에 열려 있거나 해소된 물류 문제가 없습니다",
                ),
                #  ★ 표 칸은 결과만 적고(「우선도 정보 없음」) **이유는 여기서 한 번** 말한다.
                #    🔴 «어느 점검이 찾았는지 장부에 안 남아 나누어 세지 않는다» 는
                #       걷기 내부 사정이라 뺐다 — 담당자가 할 일을 바꾸지 않는다.
                footer="기준일 뒤에 본 값은 그날 값으로 쓰지 않아 «—» 로 둡니다.",
            ),
            Card(
                key="capacity", title="창고 수용 여유",
                subtitle="창고에 얼마나 더 받을 수 있는지입니다",
                source_ref="inventory_moves · agent_policy_config",
                stats=[
                    Stat(label="창고 사용량", value=_kg(cap.used_capacity_kg), unit="kg",
                         detail="기준일 창고 실물 합계",
                         raw=_raw(cap.used_capacity_kg)),
                    #  🔴 **이 둘만 «현재값» 이다** (#812). 용량 정책 표에 유효일 칸이
                    #     없어 기준일로 되살릴 수 없다 (`capacity_basis =
                    #     CURRENT_ACTIVE_POLICY`). 화면 맨 위가 «기준일 시점 값» 이라고
                    #     말하므로, **다른 축인 칸은 그 자리에서 직접 말해야 한다.**
                    Stat(label="보장 용량",
                         value=_kg(guaranteed) if guaranteed is not None else "—",
                         unit="kg" if guaranteed is not None else None,
                         detail="현재값",
                         raw=_raw(guaranteed)),
                    Stat(label="최대 수용량",
                         value=_kg(cap.burst_capacity_kg) if cap.burst_capacity_kg else "—",
                         unit="kg" if cap.burst_capacity_kg else None,
                         detail="현재값", raw=_raw(cap.burst_capacity_kg)),
                    Stat(label="추가 수용 가능량",
                         value=_kg(headroom) if headroom is not None else "—",
                         unit="kg" if headroom is not None else None,
                         detail=(f"보장 용량의 {usage_pct:.1f}% 사용" if usage_pct is not None
                                 else "보장 용량을 못 읽어 계산하지 않습니다"),
                         tone="good" if headroom is not None and headroom > 0 else "warn",
                         raw=_raw(headroom)),
                ],
            ),
            Card(
                key="items", title="품목별 재고 현황",
                subtitle=" · ".join(ITEMS),
                source_ref="inventory_lots · inventory_reservations",
                #  🔴 종전 문구는 «판매가능량 · 예약량은 지금 기준» 이라고 적었는데
                #     #760 이후로는 **넷 다 기준일 축**이다 (`_MIXED_AXIS_NOTE` 와
                #     `console_service` 머리말이 정본). 같은 화면에서 두 설명이
                #     서로 어긋나 있었다.
                #  🔴 **안내문을 통째로 뺐다** (#812). 종전 두 문장은 각각
                #     «기준일 시점 값입니다»(화면 맨 위가 이미 말한다)와 «창고 사용량은
                #     화면에 안 그리는 품목까지 포함한 합계라 다를 수 있습니다» 였다.
                #     뒤엣것은 **화면 품목 필터라는 내부 사정**이라, 사용자가 이 표를
                #     읽거나 무엇을 할지 정하는 데 아무것도 보태지 않는다.
                table=_t(
                    [("item", "품목", "left"), ("onhand", "현재고", "right"),
                     ("avail", "판매가능량", "right"), ("resv", "예약량", "right"),
                     #  ★ 「미할당 예약량」 → 예약·출고 탭과 같은 어휘로 맞춘다 (#812).
                     ("left", "Lot 미배정", "right"),
                     ("expired", "폐기 검토 수량", "right"), ("risk", "먼저 볼 Lot", "right")],
                    [
                        {
                            "item": it.item_name,
                            "onhand": _kg_cell(it.on_hand_qty_kg),
                            "avail": _kg_cell(it.available_qty_kg),
                            "resv": _kg_cell(it.reserved_qty_kg),
                            "left": _kg_cell(it.unallocated_reserved_qty_kg),
                            "expired": _kg_cell(it.expired_qty_kg),
                            "risk": it.sell_priority_lot_count + it.expired_lot_count,
                        }
                        for it in shown_items
                    ],
                    empty_text="기준일에 그릴 품목이 없습니다",
                ),
                #  ★ 칸 이름으로 이미 읽히는 말은 적지 않는다 (#812). 남긴 둘은 **오해를
                #    막는 것**이다 — 「—」를 0 으로 읽는 것과, 「먼저 볼 Lot」을 위 문제
                #    건수와 같은 지표로 읽는 것.
                footer=("판매가능량이 «—» 면 못 읽었다는 뜻입니다 — 0 이 아닙니다. "
                        "먼저 볼 Lot 은 위 문제 건수와 다른 지표입니다."),
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

    #  🔴 현재고 합계 밑에 적던 «입고 예정 N kg» 을 뺐다 (#812) — 이 실행은 도착 전
    #     상태를 남기지 않아 늘 0 kg 이었다. 이유는 `_inbound_pane` 머리말에 있다.
    #  ★ 표시 순서: 폐기 검토 → 우선 출고 → 신선도 잔여 적은 순 → 입고일 오래된 순.
    #    🔴 **표시 순서일 뿐 업무 판정이 아니다** — 값은 read model 것 그대로다.
    #    `None` 신선도는 맨 뒤로 보낸다. 모르는 값을 «가장 급한 것» 으로 올리지 않는다.
    shown_lots = sorted(
        (lo for lo in inv.lots if _on_screen(lo.item_name)),
        key=lambda lo: (
            not lo.disposal_candidate,
            not lo.sell_priority,
            (1, 0) if lo.remaining_freshness_days is None else (0, lo.remaining_freshness_days),
            lo.received_at,
            lo.lot_id,
        ),
    )

    if available is None:
        avail_detail = f"못 읽은 축이 있습니다 — {inv.available_qty_unresolved_reason}"
    else:
        #  ★ **예약·할당 축도 이제 기준일 값이다 (#760).** 그날 Lot(`lot_state_at`)과
        #    그날 예약(`reservation_state_at`)으로 세운 스냅샷을 정본에 먹인다.
        avail_detail = "예약을 뺀 팔 수 있는 양"

    return Pane(
        key="stock",
        label="재고 · 신선도",
        stats=[
            Stat(
                label="현재고 합계", value=_kg(on_hand, 1), unit="kg",
                detail=f"품목 {len(inv.items)}종",
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
                label="활성 예약 수량", value=_kg(reserved, 1), unit="kg",
                detail=(f"활성 예약 {sum(it.active_reservation_count for it in inv.items)}건이 "
                        "요구한 수량입니다"),
                tone="warn" if reserved > 0 else "neutral", raw=_raw(reserved),
            ),
            Stat(
                label="폐기 검토 대상", value=f"{disposal:,}", unit="Lot",
                detail=f"우선 출고 대상 {sell_priority} Lot",
                tone="bad" if disposal > 0 else "good", raw=float(disposal),
            ),
        ],
        cards=[
            Card(
                key="reservation", title="처리 중 예약",
                subtitle="아직 내보낼 일이 남은 예약만 그립니다",
                source_ref="inventory_reservations · inventory_allocations",
                #  🔴 **절차 그림을 뺐다** (#812 · 입고 탭과 같은 이유).
                #     `현재고 → 예약으로 수량 확보 → Lot 배정 → 출고 → 현재고 감소` 는
                #     업무 절차 소개라 날짜를 바꿔도 안 바뀐다. 담당자가 할 일을 정하는
                #     값이 아니라 아래 표가 주인이고, 절차 정본은 `03_출고관리.md` 다.
                #  🔴 Reservation ID · Sale 참조는 본문에 싣지 않는다 — 값은 응답에 그대로 있다.
                #  🔴 **「납기일」 칸을 뺐다** (#812). Sales ↔ Logistics 계약에 납기 칸이
                #     없어 `due_date` 는 구조적으로 늘 비어 있고(#423 · 실측 495건 전부),
                #     전 줄이 «—» 인 칸은 어떤 판단에도 쓸 수 없다. **숨긴 것이 아니라
                #     없는 값이다** — 그 사실을 아래 footer 가 한 번 말한다. 계약이 납기를
                #     주기 시작하면 이 칸을 되돌린다.
                table=_t(
                    [("item", "품목", "left"),
                     ("need", "요구량", "right"), ("resv", "확보량", "right"),
                     ("alloc", "Lot 배정", "right"), ("left", "Lot 미배정", "right"),
                     ("state", "진행 상태", "left")],
                    [
                        {
                            "item": r.item_name or r.item_id,
                            "need": _kg_cell(r.required_qty_kg),
                            "resv": _kg_cell(r.reserved_qty_kg),
                            "alloc": _kg_cell(r.allocated_qty_kg),
                            "left": _kg_cell(r.unallocated_qty_kg),
                            "state": _label(_RESERVATION_LABEL, r.status),
                        }
                        for r in ob.reservations
                        if _on_screen(r.item_name) and _still_working(r)
                    ],
                    empty_text="처리 중인 예약이 없습니다",
                ),
                footer="Lot 미배정은 아직 어느 Lot 에서 낼지 안 정한 몫입니다 — 부족이 아닙니다.",
            ),
            Card(
                key="lots", title="Lot 별 신선도",
                subtitle="먼저 처리해야 할 Lot 을 위에 둡니다",
                source_ref="inventory_moves · inventory_lots · item_storage_policies",
                #  🔴 raw Lot ID · 내부 Zone 코드 · `ACTIVE` 는 싣지 않는다. Zone 은 공식
                #     표시명이 저장소에 없어(정본은 `item_storage_policies.storage_zone`
                #     코드뿐) 임의 해석 대신 칸을 뺀다 — 내부 코드보다 정보 없음이 낫다.
                table=_t(
                    #  ★ 신선도 하나만 남긴다 (#812) — 비슷한 숫자를 둘 세우지 않는다.
                    #  🔴 **「Lot」 칸을 뺐다** (#812). 그 값은 `배추 · 03-17 입고` 라
                    #     **품목 칸과 입고일 칸을 합친 것**이었고, 그 둘이 바로 옆에
                    #     각각 서 있었다 — 같은 사실을 한 줄에 세 번 적고 있었다.
                    #     쪼갤 수 있는 값은 쪼갠 칸으로 둔다.
                    [("item", "품목", "left"), ("grade", "등급", "left"),
                     ("qty", "잔량", "right"), ("received", "입고일", "left"),
                     ("fresh", "신선도 잔여", "right"), ("action", "필요한 조치", "left")],
                    [
                        {
                            "item": lo.item_name or lo.item_id,
                            #  ★ 등급은 **넘겨짚지 않는다.** DB NULL 은 "미확정" 이다.
                            "grade": lo.grade or "등급 미확정",
                            "qty": _kg_cell(lo.remaining_qty_kg),
                            "received": _md(lo.received_at),
                            "fresh": _days(lo.remaining_freshness_days),
                            "action": _lot_action(lo.sell_priority, lo.disposal_candidate),
                        }
                        for lo in shown_lots
                    ],
                    empty_text="이 날짜에 남아 있는 Lot 이 없습니다",
                ),
                footer="급한 Lot 이 위에 옵니다 — 폐기 검토 · 우선 출고 · 신선도 잔여 순입니다.",
            ),
            #  🔴 **「재고 처리 원칙」 카드를 통째로 없앴다** (#812). 다섯 줄 전부가
            #     «이 화면을 어떻게 읽어야 하나» 였고, 그중 셋은 다른 자리가 이미
            #     말하고 있었다 — 시간축은 화면 맨 위 한 줄, 예약이 재고를 안 줄인다는
            #     것은 위 예약 카드, 조치의 주인이 서버라는 것은 개발 쪽 규율이다.
            #     **표가 스스로 읽히지 않아 설명을 붙여야 한다면 고칠 곳은 표다.**
        ],
    )


#: 입고 내역처럼 **이력이 계속 쌓이는 표**에 한 번에 펼치는 최대 줄 수.
#: 🔴 실측 290행이 8개월치로 나와 «지금 할 일» 이 묻혔다. 자르되 **몇 건을 덜 폈는지
#:    footer 에 적는다** — 숨기는 것과 접는 것은 다르다.
_MAX_HISTORY_ROWS = 20


def _positive(value: Any) -> bool:
    """**값이 있고 0 보다 큰가.** 🔴 `None`(모름)은 «있다» 로 친다 — 0 이 아니다."""
    return value is None or value > 0


def _receipt_columns(receipts: Any) -> list[tuple[str, str, str]]:
    """도착 표의 칸을 **그날 값에 맞춰** 고른다.

    🔴 **전 줄이 같은 값인 칸은 세우지 않는다** (#812). 실측(2026-03-24)에서 아홉 칸 중
       다섯이 상수였다 — 보류 0 · 거절 0 · 검수 「합격」 · 처리 「처리 완료」 그리고
       주문이 수용과 같은 숫자였다. 읽을 것은 세 칸(도착일 · 품목 · 수용)뿐인데
       눈은 아홉 칸을 훑는다.

    🔴 **숨기는 것과 다르다.** 「입고 예정」이나 「납기」처럼 *구조적으로 못 채우는*
       값이 아니라, **그날 마침 0 이었을 뿐** 언제든 생길 수 있는 값이다 (검수가
       HOLD·REJECT 를 내면 그날 칸이 그대로 선다). 그래서 지우지 않고 **그날의 사실로
       고른다** — 칸이 떴다는 것 자체가 «볼 것이 있다» 는 신호가 된다.

    ⚠️ **`None` 은 0 이 아니다.** 못 읽은 값이 한 줄이라도 있으면 그 칸은 세운다 —
       모르는 것을 «없었다» 로 접으면 그 사실이 화면에서 사라진다.
    """
    #  ★ **이 표는 너비를 안 정한다 — 균등이 맞다** (#812).
    #
    #    칸이 상황에 따라 4~9개로 변하는 표라 고정 너비를 박으면 칸 수가 바뀔 때마다
    #    어긋난다. 실제로 `9% · 9% · 11% · 나머지` 로 줘 봤더니 마지막 칸이 71% 를
    #    가져가 **값이 앞쪽 29% 에 몰렸다** (실측). 균등 배분(칸 넷 → 274·266·282px)이
    #    이 표에는 이미 고르다.
    cols: list[tuple] = [("arrive", "도착일", "left"), ("item", "품목", "left")]
    #  ★ 주문과 수용이 한 줄도 안 갈리면 같은 숫자를 두 번 적는 것이다.
    if any(r.ordered_qty_kg != r.accepted_qty_kg for r in receipts):
        cols.append(("ord", "주문", "right"))
    cols.append(("acc", "수용", "right"))
    if any(_positive(r.hold_qty_kg) for r in receipts):
        cols.append(("hold", "보류", "right"))
    if any(_positive(r.rejected_qty_kg) for r in receipts):
        cols.append(("rej", "거절", "right"))
    if any(r.inspection_verdict != "PASS" for r in receipts):
        cols.append(("verdict", "검수 결과", "left"))
    #  ★ 「처리 상태」와 「재고 처리」는 완료의 두 축이다 (#805). 진행 중인 건이 하나도
    #    없으면 앞 칸은 전 줄 «처리 완료» 라 아무것도 안 가른다 — 뒤 칸만 남긴다.
    if any(_receipt_progress(r)[0] != "처리 완료" for r in receipts):
        cols.append(("state", "처리 상태", "left"))
    cols.append(("applied", "재고 처리", "left"))
    return cols


def _arrival_alerts(summary: Any) -> list[Stat]:
    """도착 처리가 **막혀 있을 때만** 뜨는 칸.

    🔴 **0 을 자리 채우기로 그리지 않는다** (#812). 이 둘은 «지금 손봐야 할 일이 있다»
       는 경보이지 상시 지표가 아니다 — 늘 `0건` 으로 떠 있으면 진짜 1 이 된 날에도
       눈에 안 띈다. 값이 생기면 그때 나타난다.

    ⚠️ **없앤 것이 아니라 조건부다.** 도착 전 상태 목록이 비어 있는 동안에는 둘 다
       구조적으로 0 이고(위 머리말), 그 목록이 채워지는 날 이 칸들이 그대로 돌아온다.
    """
    alerts: list[Stat] = []
    if summary.blocked_count:
        alerts.append(
            Stat(label="처리 보류", value=f"{summary.blocked_count:,}", unit="건",
                 detail="입고 처리에 필요한 정보가 없습니다",
                 tone="warn", raw=float(summary.blocked_count))
        )
    if summary.unresolved_count:
        alerts.append(
            Stat(label="확인 필요", value=f"{summary.unresolved_count:,}", unit="건",
                 detail="도착 여부를 판정할 근거가 없습니다",
                 tone="warn", raw=float(summary.unresolved_count))
        )
    return alerts


def _receipt_by_item(receipts: Any) -> list[dict[str, str | float | int | None]]:
    """도착한 물량을 **품목별로** 묶는다.

    ★ 새 판정이 아니라 **합계**다 (#812). 아래 표는 도착 건을 한 줄씩 늘어놓아 그날
      무엇이 얼마나 들어왔는지는 291줄을 눈으로 더해야 알 수 있었다. 같은 값을 품목
      축으로 한 번 더 보여 준다 — 원본 표는 그대로 있다.

    🔴 **`None` 은 0 이 아니다.** 수용량을 못 읽은 건이 섞이면 그 품목 합계는 «—» 다 —
       아는 것만 더해서 아는 척하지 않는다 (`_sum` 과 같은 규율).
    """
    묶음: dict[str, list[Any]] = {}
    for r in receipts:
        묶음.setdefault(r.item_name or r.item_id, []).append(r)
    rows: list[dict[str, str | float | int | None]] = []
    #  ★ 많이 들어온 품목을 위에 둔다. 합계를 못 낸 품목은 맨 뒤다.
    for name, 것들 in 묶음.items():
        수용 = _sum([r.accepted_qty_kg for r in 것들])
        rows.append(
            {
                "item": name,
                "count": len(것들),
                "acc": _kg_cell(수용),
                "_sort": float(수용) if 수용 is not None else -1.0,
            }
        )
    rows.sort(key=lambda row: -float(row["_sort"] or 0))
    for row in rows:
        del row["_sort"]
    return rows


def _receipt_progress_counts(receipts: Any) -> list[tuple[str, int, str, str]]:
    """도착한 건을 **재고 처리 결과별로** 센다.

    🔴 **판정을 새로 만들지 않는다.** 아래 표의 「재고 처리」 칸과 **같은 함수**
       (`_receipt_progress`)에게 물어 그 결과를 세기만 한다 — 다른 식으로 세면
       위 숫자와 아래 표가 갈린다.

    ⚠️ 완료의 형태가 둘이다 (#805). 「반영할 재고 없음」도 **정상 완료**라 경고색을
       쓰지 않는다 — 수용할 것이 0 이었을 뿐 실패가 아니다.
    """
    #  ★ 순서가 곧 표시 순서다. `_receipt_progress` 가 내는 어휘 그대로 쓴다.
    쓸것 = [
        ("재고 반영 완료", "재고가 선 입고", "good"),
        ("반영할 재고 없음", "수용할 물량이 없어 끝난 입고", "neutral"),
    ]
    센것 = {label: 0 for label, _, _ in 쓸것}
    처리중 = 0
    for r in receipts:
        _, applied = _receipt_progress(r)
        if applied in 센것:
            센것[applied] += 1
        else:
            처리중 += 1
    out = [
        (label, 센것[label], detail, tone)
        for label, detail, tone in 쓸것
        if 센것[label]
    ]
    #  ★ 아직 안 끝난 건은 **0 이어도 적는다** — 「할 일 없음」이 사용자가 볼 값이다.
    out.append(
        ("처리 중", 처리중, "아직 두 완료 어디에도 안 닿은 건", "warn" if 처리중 else "good")
    )
    return out


def _inbound_pane(inb: ConsoleInboundResponse, as_of: date) -> Pane:
    """입고 · 검수. **보여 주는 기간은 그 달 1일 ~ 기준일이다.**

    🔴 **누계를 «도착 건수» 라고 적지 않는다** (#812).

    `receipt_state_at` 의 WHERE 는 `arrived_at <= as_of` 하나뿐이라 **아래쪽 경계가
    없다.** 그래서 이 화면은 실행 첫날부터의 누계를 적고 있었다 — 실측:

    ```text
    도착 291건 · 151,921 kg   =  2026-01-06 ~ 2026-09-01   ← 8개월 누계
    기준일                       2026-09-14
    ```

    한 달도 월초부터도 아닌 «처음부터 전부» 라, 어제 열어도 오늘 열어도 같은 숫자였고
    화면 어디에도 **언제부터인지 안 적혀 있었다.** 재고 담당자가 «오늘 할 일» 을 정하는
    데 쓸 수 없는 값이다.

    ★ **그 달 1일부터 센다.** 월 실적과 대조하기 쉽고, 월초에도 「마지막 입고」가
      며칠 전인지 따로 말해 주므로 빈 화면이 되지 않는다.

    ⚠️ **응답에서 지운 것이 아니다.** `inb.receipts` 에는 기준일까지 전부 그대로 실려
       있고, 여기서는 **그릴 것만 고른다** — 화면 품목 필터(`_on_screen`)와 같은 결이다.

    🔴 **「입고 예정」(운송 중) 을 화면에서 뺐다** (#812).

    이 축(`inb.in_transit`)은 *"입고 일정에 올라 있고 아직 창고에 도착하지 않은 건"*
    인데, 이 저장소의 걷기는 **일정 행을 도착일에 만든다** (`created_as_of` =
    `expected_arrival_date` · 실측 291/495 전부 간격 0). 그래서 일정이 있는데 Receipt 가
    없는 날이 **하루도 없고**, 이 목록은 어느 기준일에도 늘 비어 있다.

    ```text
    화면이 적던 말        «이 기준일에 들어올 예정인 입고가 없습니다»
    실제 사실             «이 실행은 도착 전 상태를 남기지 않는다»
    ```

    앞엣말은 **0 건을 확인했다는 주장**이라 사실과 다르다. 카드 · 흐름 단계 · 요약
    숫자 셋을 다 뺀 이유가 그것이다 — 값을 숨긴 것이 아니라 **없는 값을 있는 척하지
    않는 것**이다.

    ⚠️ **도메인은 그대로다.** `inbound_schedules.in_transit_from` · `ConsoleInboundResponse
       .in_transit` · Agent 경로 어느 것도 안 건드렸고, 응답에도 그대로 실린다.
       `created_as_of` 를 고치는 일은 걷기 쪽이고 이번 이슈 범위 밖이다.

    ⚠️ **못 읽은 경우(`None`)가 조용히 사라지지 않는다.** 그 사실은 도착 요약의
       「확인 필요」(`unresolved_count`) 가 이미 자기 숫자로 말한다.
    """
    summary = inb.arrival_summary
    월초 = as_of.replace(day=1)
    기간 = f"{_md(월초)} ~ {_md(as_of)}"
    #  ★ 이 달에 도착한 것만 센다. 아래 표 · 품목별 · 요약 숫자가 **같은 모집단**이다.
    이달 = [r for r in inb.receipts if r.arrived_at >= 월초]
    수용합계 = _sum([r.accepted_qty_kg for r in 이달])
    #  ★ **마지막 입고는 기간 밖에서도 찾는다** (#812). 이 달에 한 건도 없으면 「0건」만
    #    남아 화면이 «왜 비었는지» 를 안 말한다 — 실측 09-14 기준 마지막 입고가 09-01 로
    #    13일 전이었고, 그 사실이 0 보다 중요하다.
    최근도착 = max((r.arrived_at for r in inb.receipts), default=None)

    #  ★ 아직 처리가 안 끝난 건을 **맨 위로** 올리고, 그다음 최근 도착 순이다.
    #    🔴 잘라내도 «아직 할 일» 은 안 잘린다 — 그것이 이 정렬의 이유다.
    #    판정은 하지 않는다. 재고가 섰거나(`stock_applied`) 수용 0 으로 끝난
    #    (`settled_without_stock` · #805) 건은 둘 다 **끝난 것**이다.
    receipts = sorted(
        (r for r in 이달 if _on_screen(r.item_name)),
        key=lambda r: (
            bool(r.stock_applied or r.settled_without_stock),
            -r.arrived_at.toordinal(),
        ),
    )
    shown_receipts = receipts[:_MAX_HISTORY_ROWS]
    hidden_receipts = len(receipts) - len(shown_receipts)
    receipt_rows: list[dict[str, str | float | int | None]] = []
    for r in shown_receipts:
        #  🔴 «처리 완료» 와 «재고 반영 완료» 를 한 칸에 담지 않는다 (#805).
        state, applied = _receipt_progress(r)
        receipt_rows.append(
            {
                "arrive": _md(r.arrived_at),
                "item": r.item_name or r.item_id,
                "ord": _kg_cell(r.ordered_qty_kg),
                "acc": _kg_cell(r.accepted_qty_kg),
                "hold": _kg_cell(r.hold_qty_kg),
                "rej": _kg_cell(r.rejected_qty_kg),
                "verdict": _label(_VERDICT_LABEL, r.inspection_verdict),
                "state": state,
                "applied": applied,
            }
        )

    return Pane(
        key="inbound",
        label="입고 · 검수",
        #  🔴 **움직이는 숫자만 세운다** (#812).
        #
        #     종전 넷(도착 예정 · 도착 지연 · 처리 보류 · 확인 필요)은 전부 «도착 전
        #     상태 목록» 에서 세는데 그 목록이 구조적으로 늘 비어 있다(위 머리말).
        #     그래서 어느 기준일을 열어도 `0건 · 0건 · 0건 · 0건` 이었다 — 네 칸이
        #     자리만 차지하고 **아무것도 알려 주지 않았다.**
        #
        #     대신 그날 실제로 도착한 건(`inb.receipts` · 실측 495행)을 센다. 판정은
        #     새로 만들지 않고 **아래 표와 같은 주인**(`_receipt_progress`)에게 묻는다 —
        #     그래야 위 숫자와 아래 표가 어긋날 자리가 없다.
        stats=[
            *_arrival_alerts(summary),
            #  🔴 **기간을 숫자 옆에 적는다** (#812). 종전에는 «기준일까지» 라고만 적어
            #     8개월 누계가 그날 실적처럼 읽혔다.
            Stat(label="이 달 도착", value=f"{len(이달):,}", unit="건",
                 detail=기간, tone="neutral", raw=float(len(이달))),
            #  ★ **건수만으로는 얼마가 들어왔는지 모른다** (#812). 같은 1건이 17kg 일
            #    수도 1,435kg 일 수도 있다. 합계는 표에 이미 있는 수용량을 더한 것이고,
            #    못 읽은 건이 섞이면 «—» 다 — 0 으로 메우지 않는다.
            Stat(label="수용 합계",
                 value=_kg(수용합계) if 수용합계 is not None else "—",
                 unit="kg" if 수용합계 is not None else None,
                 detail=("검수를 통과해 재고가 된 양" if 수용합계 is not None
                         else "못 읽은 건이 있습니다 — 0 이 아닙니다"),
                 tone="neutral", raw=_raw(수용합계)),
            Stat(label="마지막 입고",
                 value=_md(최근도착) or "—",
                 detail=(f"{(as_of - 최근도착).days}일 전" if 최근도착 is not None
                         else "이 실행에 도착 기록이 없습니다"),
                 #  ★ 기간 밖 값이라 «이 달» 이 0 건이어도 여기는 차 있다.
                 tone="warn" if 최근도착 is not None and (as_of - 최근도착).days > 7
                 else "neutral",
                 raw=None),
            *(
                Stat(label=label, value=f"{count:,}", unit="건",
                     detail=detail, tone=tone, raw=float(count))
                for label, count, detail, tone in _receipt_progress_counts(이달)
            ),
        ],
        cards=[
            #  🔴 **「입고 처리 흐름」 카드를 없앴다** (#812).
            #
            #     `실매입 확정 → 창고 도착 → 검수 → 입고 처리 완료` 는 **업무 절차
            #     소개**이지 그날의 사실이 아니다. 날짜를 바꿔도 안 바뀌고, 담당자가
            #     보고 할 일을 정하는 값도 아니다 — 매일 여는 화면의 첫 칸을 차지할
            #     이유가 없다.
            #
            #     ⚠️ 절차 자체는 그대로다. 정본은 `docs/logistics/services/01_입고관리.md`
            #        이고, 화면에는 그 절차가 **낸 결과**(도착 · 검수 · 재고 처리)가 아래
            #        표로 이미 서 있다.
            #  ★ **한 줄씩 늘어놓은 표 위에 «무엇이 얼마나» 를 먼저 둔다** (#812).
            #    아래 표는 도착 건을 시간순으로 적어, 그날 무엇이 많이 들어왔는지 알려면
            #    291줄을 눈으로 더해야 했다. 같은 값의 품목 축 합계다 — 새 사실이 아니다.
            Card(
                key="by_item", title="품목별 입고",
                subtitle=f"{기간} 에 들어온 양입니다",
                table=_t(
                    [("item", "품목", "left"), ("count", "건수", "right"),
                     ("acc", "수용", "right")],
                    _receipt_by_item(이달),
                    empty_text="이 달에 도착한 물량이 없습니다",
                ),
            ),
            Card(
                key="receipt", title="입고 처리 현황",
                subtitle=f"{기간} · 검수와 재고 처리 상태입니다",
                #  🔴 `arrival_schedule` 은 **표가 아니라 계약 필드명**이었다.
                #     실제 출처는 이 둘이다.
                source_ref="inbound_receipts · inbound_inspections",
                #  🔴 Receipt ID 는 본문에 싣지 않는다 — 사용자가 읽을 값이 아니다.
                table=_t(
                    _receipt_columns(shown_receipts),
                    receipt_rows,
                    empty_text="이 날짜까지 창고에 도착한 물량이 없습니다",
                ),
                #  ★ 접은 건수는 **반드시 남긴다** — 숨기는 것과 접는 것은 다르다.
                footer=(
                    "아직 안 끝난 건이 위에 옵니다."
                    + (f" 최근 {len(shown_receipts)}건만 폈고 나머지 "
                       f"{hidden_receipts:,}건은 접었습니다."
                       if hidden_receipts > 0 else "")
                ),
            ),
        ],
    )


def _outbound_pane(ob: ConsoleOutboundResponse, inv: ConsoleInventoryResponse) -> Pane:
    """예약 · 출고.

    ★ **기준일을 안 받는다** (#812). 후보를 «지금» 재고에서 구하던 때는 이 함수가
      `as_of` 를 들고 다니며 후보 이름과 신선도를 그 날짜로 맞춰야 했다. 이제 재료가
      전부 그날 축으로 들어오므로(`inv.lots` · `ob.reservations`) 날짜를 쥘 이유가 없고,
      **쥐지 않으면 섞을 수도 없다.**
    """
    #  ★ FEFO 후보는 **예약 한 건마다 그린다** — 예약이 없으면 그릴 대상도 없다.
    #    예약이 없을 때 Lot 을 신선도순으로 늘어놓아 «후보» 라고 부르지 않는다 —
    #    그건 서비스에 없는 계산을 화면이 새로 만드는 것이다 (#415).
    fefo_rows: list[dict] = []
    #  ★ 화면이 그리는 품목만 · 그날 아직 일이 남은 예약만 — 범위 밖 품목이나 이미 다
    #    나간 예약에 FEFO 를 물을 이유가 없다.
    item_reservations = [r for r in ob.reservations if _on_screen(r.item_name)]
    shown_reservations = [r for r in item_reservations if _still_working(r)]
    settled_count = len(item_reservations) - len(shown_reservations)
    #  🔴 **FEFO 는 «아직 Lot 을 안 고른 몫» 이 있는 예약에만 그린다.** 목표량이 0 이면
    #     `allocate_reserved_stock_fefo` 도 아무것도 안 하므로 후보를 구할 이유가 없다.
    #     예약 164건에 164번 묻던 것이 대시보드 8.6초의 태반이었다 (마스터 실측 2026-09-15).
    #  🔴 **묻는 것은 품목마다 한 번이다** (2026-09-15). 후보는 예약과 무관한 값이라
    #     (`console_service.get_fefo_candidates_by_item`) 같은 품목 예약 여덟 건이 같은 답을
    #     여덟 번 받던 자리다. 예약이 없으면 **묻지도 않는다.**
    #  🔴 **넘기는 예약은 «그린 것» 이 아니라 그날 **전부** 다** (#812). 남의 예약이
    #     잡아 둔 몫도 그 Lot 에서 빠져야 가용량이 부풀지 않는다.
    fefo_targets = [r for r in shown_reservations if r.unallocated_qty_kg > 0]
    candidates_by_item = (
        get_fefo_candidates_by_item(
            lots=inv.lots,
            reservations=ob.reservations,
            item_ids=[r.item_id for r in fefo_targets],
        )
        if fefo_targets
        else {}
    )
    #  🔴 **후보는 품목당 한 벌이다 — 예약마다 다시 그리지 않는다** (#812).
    #
    #     종전에는 예약 한 건마다 그 품목의 후보 전부를 다시 적었다. 무 예약이 다섯이면
    #     같은 후보 일곱 줄이 **다섯 번** 나와 56줄이 됐는데, 실제 사실은 19개뿐이었다.
    #
    #     ```text
    #     무 · 772 kg · 08-19 입고 · 가용 125 kg    ┐
    #     무 · 463 kg · 08-19 입고 · 가용 125 kg    ├ 같은 Lot 하나다
    #     무 ·  43 kg · 08-19 입고 · 가용 125 kg    ┘
    #     ```
    #
    #     🔴 **읽는 사람이 더하게 된다.** 세 줄을 보면 375 kg 가 있는 것 같지만 실물은
    #        125 kg 한 덩어리다 — 가용량은 예약마다 따로 있는 값이 아니라 **그 Lot 에서
    #        아직 아무 할당에도 안 묶인 몫** 하나다 (`ConsoleFefoCandidate` 주석).
    #
    #     ★ 예약별 수치(요구 · 확보 · Lot 배정 · 미배정)는 「재고 · 신선도」 탭의
    #       「처리 중 예약」 표가 이미 예약 축으로 보여 준다. 여기는 **Lot 축**이다.
    #
    #  🔴 raw `lot_id` 를 표에 싣지 않는다 — Lot 은 **입고일**로 가른다.
    #
    #  ★ **카드를 품목마다 나눈다** (#812). 한 표에 몰면 품목 칸에 「배추」가 일곱 번,
    #    「무」가 일곱 번 찍히고 순위가 중간에서 1 로 되돌아간다 — 19줄이 한 덩어리라
    #    어디까지가 한 품목인지 눈으로 세야 했다. 후보는 원래 품목마다 독립이므로
    #    카드도 그렇게 나누면 **품목 칸과 순위 설명이 둘 다 필요 없어진다.**
    미배정합계: dict[str, Decimal] = {}
    품목이름: dict[str, str] = {}
    for resv in fefo_targets:
        미배정합계[resv.item_id] = (
            미배정합계.get(resv.item_id, Decimal(0)) + resv.unallocated_qty_kg
        )
        품목이름.setdefault(resv.item_id, resv.item_name or resv.item_id)

    #  ★ 배정할 몫이 많은 품목을 위에 둔다 — 먼저 손봐야 할 것이 먼저 보인다.
    순서 = sorted(
        (item_id for item_id in candidates_by_item if candidates_by_item[item_id]),
        key=lambda item_id: -float(미배정합계.get(item_id, Decimal(0))),
    )
    fefo_cards: list[Card] = [
        Card(
            key=f"fefo-{item_id}",
            title=f"{품목이름.get(item_id, item_id)} 출고 후보",
            #  ★ 배정해야 할 양은 **제 카드 머리에** 한 번 적는다. 줄마다 되풀이하던
            #    「Lot 미배정」 칸이 그 자리였다.
            subtitle=(f"Lot 미배정 {_kg(미배정합계.get(item_id, Decimal(0)))} kg · "
                      "위에서부터 먼저 내보냅니다"),
            source_ref="inventory_reservations · inventory_lots",
            table=_t(
                [("received", "입고일", "left"), ("grade", "등급", "left"),
                 ("qty", "가용", "right"), ("fresh", "신선도 잔여", "right"),
                 ("rank", "순위", "right")],
                [
                    {
                        "received": _md(cand.received_at),
                        "grade": cand.grade or "등급 미확정",
                        "qty": _kg_cell(cand.available_qty_kg),
                        "fresh": _days(cand.remaining_freshness_days),
                        "rank": rank,
                    }
                    for rank, cand in enumerate(candidates_by_item[item_id], start=1)
                ],
                empty_text="추천할 출고 후보가 없습니다",
            ),
        )
        for item_id in 순서
    ]
    fefo_rows = [row for card in fefo_cards if card.table for row in card.table.rows]

    return Pane(
        key="outbound",
        label="예약 · 출고",
        stats=[
            Stat(label="예약", value=f"{len(shown_reservations):,}", unit="건",
                 detail=(f"전량 출고·해제된 {settled_count}건은 뺐습니다"
                         if settled_count else None),
                 tone="good" if shown_reservations else "neutral",
                 raw=float(len(shown_reservations))),
            Stat(label="Lot 미배정", value=f"{len(fefo_targets):,}", unit="건",
                 detail="아직 어느 Lot 에서 낼지 안 정한 예약",
                 tone="warn" if fefo_targets else "neutral",
                 raw=float(len(fefo_targets))),
            #  ★ **«후보 건수» 가 아니라 «후보 Lot 수» 다** — 예약마다 세던 때는 같은
            #    Lot 이 여러 번 세어져 숫자가 부풀었다 (실측 19 → 56).
            #  🔴 **«자동 배정» 처럼 보이면 안 된다** — 도메인 계약도 «추천만 한다 —
            #     고르지도 쓰지도 않는다» 이다 (`get_fefo_candidates_by_item`). 카드가
            #     품목마다 서므로 그 말은 **여기 한 번만** 적는다.
            Stat(label="출고 후보 Lot", value=f"{len(fefo_rows):,}", unit="개",
                 detail="추천입니다 — 자동으로 배정되지 않습니다",
                 tone="neutral", raw=float(len(fefo_rows))),
        ],
        #  ★ 그릴 후보가 하나도 없어도 **카드는 선다** — 빈 화면이 «왜 없는지» 를
        #    말해야 한다. 예약이 없으면 후보도 없다는 것이 그 답이다.
        cards=fefo_cards or [
            Card(
                key="fefo", title="신선도 우선 출고 후보",
                subtitle="신선도가 먼저 소진되는 Lot 부터 추천합니다",
                source_ref="inventory_reservations · inventory_lots",
                table=_t(
                    [("received", "입고일", "left"), ("grade", "등급", "left"),
                     ("qty", "가용", "right"), ("fresh", "신선도 잔여", "right"),
                     ("rank", "순위", "right")],
                    [],
                    empty_text="Lot 을 정해야 할 예약이 없습니다",
                ),
            ),
        ],
    )


#  ── 읽기 결과 ────────────────────────────────────────────────────────────
#
#  🔴 **실패해도 화면은 뜨지만, 숫자를 지어내지 않는다.** 종전에는 이 자리에
#     예시 재고(14,600kg)가 있었고 그것이 실적처럼 나갔다.


#: 화면 맨 위 안내. 🔴 **없앴다** (#812).
#:
#:    종전 문구: *"이 화면의 수치는 고른 기준일 시점 값입니다. 보고가 없는 날은 0 이
#:    아니라 공란입니다 — 둘은 다릅니다."*
#:
#:    두 문장 다 화면에 둘 이유가 없었다.
#:
#: ```text
#: «기준일 시점 값»   화면 맨 위 기준일 줄(`DataBasis`)이 이미 말한다
#: «공란 ≠ 0»        칸이 «—» 로 직접 보여 주는 사실이다 — 문장으로 얹을 것이 아니다
#: ```
#:
#:    ⚠️ **규율이 사라진 것이 아니다.** `None` 을 0 으로 안 쓰는 것은 `_kg_cell` ·
#:       `_days` · `_sum` 이 코드로 지키고, 화면은 `cellText` 가 «—» 로 그린다.
#:       설명문은 그 규율의 **자랑**이었지 실행이 아니었다.
#:
#:    ★ Current 축인 칸(용량 한도 정책 둘)은 자기 자리에서 「현재값」이라고 직접 말한다.
_PRINCIPLE = None


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
        #  🔴 **커넥션은 한 판에 하나다** (2026-09-15). 종전에는 조회마다 · FEFO 예약마다
        #     새로 열어 한 판에 23개 · 388 ms 였다 (원격 DB · 연결당 14~22 ms). 읽기만
        #     하므로 `with` 종료의 commit 은 아무것도 안 바꾼다.
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
                            f"logistics_runtime_fixture 없음 · 보고 있는 실행: {run}"
                            f" · 기준일: {as_of}"
                            f" (열린 구간 {coverage.first_as_of}~{coverage.last_as_of})"
                        ),
                    ),
                    http_status=HTTPStatus.OK,
                )
            #  ★ Runtime 읽기(Current 축)는 **한 판에 한 번**이다 — 재고 콘솔(판매가능량)과
            #    입고 콘솔(운송 중 · 도착 처리 대상)이 같은 한 벌을 나눠 쓴다. 따로 읽으면
            #    같은 fixture · 일정 질의가 두 번씩 나간다 (실측 2026-09-15 · 일정 5번 421 ms).
            runtime = load_console_runtime(conn=conn, sim_run_id=run, as_of=as_of)
            #  ★ 그날 예약(Historical)도 **한 판에 한 번** 읽는다 (#760). 재고 콘솔의
            #    예약 3칸·판매가능량과 출고 콘솔의 예약 목록이 같은 한 벌을 나눠 쓴다 —
            #    종전에는 출고 콘솔만 `reservation_state_at` 을 부르고 재고 3칸은
            #    «지금 status» 를 세어 한 화면에 두 시간축이 섞였다.
            reservations = reservation_state_at(conn, sim_run_id=run, as_of=as_of)
            inv = get_inventory_console(
                conn=conn, sim_run_id=run, as_of=as_of, runtime=runtime, reservations=reservations
            )
            inb = get_inbound_console(conn=conn, sim_run_id=run, as_of=as_of, runtime=runtime)
            ob = get_outbound_console(
                conn=conn, sim_run_id=run, as_of=as_of, reservations=reservations
            )
            #  🔴 **문제 장부도 같은 `(sim_run_id, as_of)` 축이다.** 다른 실행의 문제를
            #     섞지 않고 그날 뒤에 열린 문제도 싣지 않는다 — 그 두 규칙의 주인은
            #     `live_exceptions_at` 하나다. 그날 닫힌 행은 저 함수가 안 내므로
            #     `resolved_exceptions_on` 이 나머지 반쪽을 가져온다.
            live = live_exceptions_at(conn, sim_run_id=run, as_of=as_of)
            resolved = resolved_exceptions_on(conn, sim_run_id=run, as_of=as_of)
            panes = [
                _summary_pane(inv, inb, ob, live.rows, resolved, live.uncertainties, as_of),
                _stock_pane(inv, inb, ob),
                _inbound_pane(inb, as_of),
                _outbound_pane(ob, inv),
            ]
    except Exception as error:  #  DB 미연결 · 표 없음 · 원장/계보 무결성 다 잡는다
        log.exception("물류 값을 못 읽었습니다")
        http_status = _http_status_for_error(error)
        retryable = http_status == HTTPStatus.SERVICE_UNAVAILABLE
        retry_text = "잠시 뒤 다시 열어 보세요." if retryable else ""
        return LogisticsTabResult(
            tab=_empty_tab(
                status="ERROR",
                note=Note(
                    tone="bad",
                    text=(f"**값을 못 읽었습니다** (`{type(error).__name__}`). "
                          f"예시 숫자로 대신하지 않습니다 — 이 화면에는 지금 사실이 "
                          f"없습니다. {retry_text}").strip(),
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
        open_days = snapshot_days_between(
            conn, sim_run_id=SHOWN_SIM_RUN_ID, start=start, end=as_of
        )
    data: list[float | None] = [None] * n
    for index in range(at + 1):
        day = start + timedelta(days=index)
        # 🔴 열린 날에만 숫자를 적는다. 안 연 날의 0 은 «없다» 가 아니라 «모른다» 다.
        if day in open_days and day in series:
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
        with get_connection() as conn:
            runtime = load_console_runtime(conn=conn, sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
            inb = get_inbound_console(
                conn=conn, sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of, runtime=runtime
            )
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
            text=("원장을 날마다 더해 도출한 값입니다. **다음날 부터 이 실행이 "
                  "돌지 않은 날은 공란**입니다."),
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
