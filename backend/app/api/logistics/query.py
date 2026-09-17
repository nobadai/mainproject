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

#: 회전 Signal. 정본 `app/logistics/turnover.py::TurnoverStatus`.
#: ⚠️ **신선도 상태가 아니라 회전 상태다.** `STORAGE_TARGET_EXCEEDED` 는 회사 내부
#:    회전목표 초과일 뿐 **판매불가가 아니다** — 「만료」로 옮기면 없는 판정을 만든다.
_TURNOVER_LABEL = {
    "NORMAL": "정상",
    "SELL_PRIORITY": "판매 우선 검토",
    "STORAGE_TARGET_EXCEEDED": "회전목표 초과",
}

#: 예약 상태. 정본 `app/logistics/outbound.py::ReservationStatus`.
_RESERVATION_LABEL = {
    "RESERVED": "예약",
    "PARTIALLY_ALLOCATED": "일부 할당",
    "ALLOCATED": "할당 완료",
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
    """예약 한 건의 **사용자 표시명.** raw `reservation_id` · `sale_id` 를 안 쓴다."""
    item = row.item_name or row.item_id
    due = _md(row.due_date)
    return f"{item} · {due} 납기" if due else f"{item} · 납기 미정"


def _candidate_names(item: str, candidates: Any) -> list[str]:
    """FEFO 후보 Lot 의 **사용자 표시명.** 🔴 **raw `lot_id` 를 쪼개지 않는다.**

    ⚠️ **기준일 Lot 목록으로 이름을 못 찾는다.** 후보는 «지금» 재고 축이라
       (`recommend_fefo_candidates`) 기준일 Lot 목록에 없는 Lot 이 섞인다 — 실제로
       그렇게 나와 Lot 칸이 통째로 «—» 였다. 그래서 후보가 **자기 칸으로 들고 온**
       `received_at` 과 그 예약의 품목으로 이름을 만든다. `_lot_names` 와 같은 규칙이다.
    """
    seen: dict[Any, int] = {}
    total: dict[Any, int] = {}
    for cand in candidates:
        total[cand.received_at] = total.get(cand.received_at, 0) + 1
    out: list[str] = []
    for cand in candidates:
        received = _md(cand.received_at)
        head = f"{item} · {received} 입고" if received else item
        if total[cand.received_at] > 1:
            seen[cand.received_at] = seen.get(cand.received_at, 0) + 1
            head = f"{head} · #{seen[cand.received_at]}"
        out.append(head)
    return out


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
        return "—", "기준일 당시 우선도 확인 불가"
    # 이력 없는 옛 행(적용 전 생성) — 기존 규칙 그대로.
    detected = row.last_detected_as_of
    if detected is None or detected > as_of:
        return "—", "기준일 당시 우선도 확인 불가"
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
            "sev": severity if hint is None else f"{severity} ({hint})",
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
    #  🔴 입고 예정은 «못 읽음»(None)과 «0 건 확인»([])이 다른 값이다.
    pending = inb.in_transit
    pending_kg = None if pending is None else sum((t.quantity_kg for t in pending), Decimal(0))

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
                key="progress", title="지금 진행 중인 일",
                subtitle="들어올 물량 · 내보낼 예약 · 먼저 봐야 할 재고입니다",
                source_ref="inbound_schedules · inventory_reservations · inventory_lots",
                stats=[
                    Stat(label="입고 예정",
                         value=_kg(pending_kg) if pending_kg is not None else "—",
                         unit="kg" if pending_kg is not None else None,
                         detail=(f"{len(pending)}건 · 아직 창고에 오지 않았습니다"
                                 if pending is not None
                                 else "이 날짜의 입고 예정 목록을 확인하지 못했습니다"),
                         tone="info" if pending else "neutral", raw=_raw(pending_kg)),
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
                key="exceptions", title="지금 확인할 일",
                subtitle="입고 후와 출고 후 점검이 장부에 남긴 문제입니다",
                source_ref="logistics_exceptions",
                lead=unconfirmed_note or Note(
                    tone="info",
                    text=("물류 점검은 걷기에서 **입고 후와 출고 후** 각각 한 번씩 돕니다. "
                          "어느 점검이 찾았는지는 장부에 남지 않아 **나누어 세지 않습니다.** "
                          "감지와 기록까지가 자동이고, 대응 여부는 담당자가 정합니다."),
                ),
                table=_t(
                    [("kind", "이상 유형", "left"), ("subject", "대상", "left"),
                     ("sev", "우선도", "left"), ("opened", "최초 감지", "left"),
                     ("seen", "최근 확인", "left"), ("state", "상태", "left")],
                    issue_rows,
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
                    Stat(label="보장 용량",
                         value=_kg(guaranteed) if guaranteed is not None else "—",
                         unit="kg" if guaranteed is not None else None,
                         detail="지금 활성 정책 값입니다 (기준일 정책 이력이 아닙니다)",
                         raw=_raw(guaranteed)),
                    Stat(label="최대 수용량",
                         value=_kg(cap.burst_capacity_kg) if cap.burst_capacity_kg else "—",
                         unit="kg" if cap.burst_capacity_kg else None,
                         detail="지금 활성 정책 값입니다", raw=_raw(cap.burst_capacity_kg)),
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
                subtitle=f"발표 화면은 {' · '.join(ITEMS)} 를 그립니다",
                source_ref="inventory_lots · inventory_reservations",
                #  🔴 종전 문구는 «판매가능량 · 예약량은 지금 기준» 이라고 적었는데
                #     #760 이후로는 **넷 다 기준일 축**이다 (`_MIXED_AXIS_NOTE` 와
                #     `console_service` 머리말이 정본). 같은 화면에서 두 설명이
                #     서로 어긋나 있었다.
                lead=Note(
                    tone="info",
                    text=("이 표의 네 수치는 모두 **기준일 시점** 값입니다. "
                          "창고 사용량은 화면에 안 그리는 품목까지 포함한 실물 "
                          "합계라 품목별 현재고 합과 다를 수 있습니다."),
                ),
                table=_t(
                    [("item", "품목", "left"), ("onhand", "현재고", "right"),
                     ("avail", "판매가능량", "right"), ("resv", "예약량", "right"),
                     ("left", "미할당 예약량", "right"),
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
                footer=("판매가능량이 «—» 면 못 읽은 축이 있다는 뜻입니다 — 0 이 아닙니다. "
                        "예약량은 예약이 요구한 수량이고, 그중 아직 Lot 을 고르지 않은 몫이 "
                        "미할당 예약량입니다. 먼저 볼 Lot 은 우선 출고 · 폐기 검토 대상을 "
                        "함께 센 수이고, 위 문제 건수와 같은 지표가 아닙니다."),
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
        transit_text = "입고 예정 — 못 읽었습니다"
    elif first_eta:
        transit_text = f"입고 예정 {_kg(transit_kg)} kg · {_md(first_eta)} 도착 예정"
    else:
        transit_text = f"입고 예정 {_kg(transit_kg)} kg"

    #  ★ 표시 순서: 폐기 검토 → 우선 출고 → 신선도 잔여 적은 순 → 입고일 오래된 순.
    #    🔴 **표시 순서일 뿐 업무 판정이 아니다** — 값은 read model 것 그대로다.
    #    `None` 신선도는 맨 뒤로 보낸다. 모르는 값을 «가장 급한 것» 으로 올리지 않는다.
    lot_name = _lot_names(inv.lots)
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
        avail_detail = "예약 · 할당 · 신선도 반영 서버 계산값 (기준일 축)"

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
                flow=["현재고", "예약으로 수량 확보", "Lot 할당", "출고", "현재고 감소"],
                lead=Note(
                    tone="info",
                    text=("예약과 할당은 재고를 **바로 줄이지 않습니다.** 실제 재고 감소는 "
                          "출고 시점에 일어납니다. **판매가능량**은 서버가 예약 · 할당 · "
                          "Lot 상태 · 신선도를 반영해 계산한 값입니다."),
                ),
                #  🔴 Reservation ID · Sale 참조는 본문에 싣지 않는다 — 값은 응답에 그대로 있다.
                table=_t(
                    [("item", "품목", "left"),
                     ("need", "요구량", "right"), ("resv", "예약량", "right"),
                     ("alloc", "Lot 할당", "right"), ("left", "미할당 잔여", "right"),
                     ("due", "납기일", "left"), ("state", "처리 상태", "left")],
                    [
                        {
                            "item": r.item_name or r.item_id,
                            "need": _kg_cell(r.required_qty_kg),
                            "resv": _kg_cell(r.reserved_qty_kg),
                            "alloc": _kg_cell(r.allocated_qty_kg),
                            "left": _kg_cell(r.unallocated_qty_kg),
                            "due": _md(r.due_date),
                            "state": _label(_RESERVATION_LABEL, r.status),
                        }
                        for r in ob.reservations
                        if _on_screen(r.item_name) and _still_working(r)
                    ],
                    empty_text="처리 중인 예약이 없습니다",
                ),
            ),
            Card(
                key="lots", title="Lot 별 신선도 · 회전",
                subtitle="먼저 처리해야 할 Lot 을 위에 둡니다",
                source_ref="inventory_moves · inventory_lots · item_turnover_policies",
                #  🔴 raw Lot ID · 내부 Zone 코드 · `ACTIVE` 는 싣지 않는다. Zone 은 공식
                #     표시명이 저장소에 없어(정본은 `item_storage_policies.storage_zone`
                #     코드뿐) 임의 해석 대신 칸을 뺀다 — 내부 코드보다 정보 없음이 낫다.
                table=_t(
                    [("lot", "Lot", "left"), ("item", "품목", "left"), ("grade", "등급", "left"),
                     ("qty", "잔량", "right"), ("received", "입고일", "left"),
                     ("fresh", "신선도 잔여", "right"), ("turn", "회전 잔여", "right"),
                     ("signal", "회전 상태", "left"), ("action", "관리 조치", "left")],
                    [
                        {
                            "lot": lot_name.get(lo.lot_id, "—"),
                            "item": lo.item_name or lo.item_id,
                            #  ★ 등급은 **넘겨짚지 않는다.** DB NULL 은 "미확정" 이다.
                            "grade": lo.grade or "등급 미확정",
                            "qty": _kg_cell(lo.remaining_qty_kg),
                            "received": _md(lo.received_at),
                            "fresh": _days(lo.remaining_freshness_days),
                            "turn": _days(lo.remaining_turnover_days),
                            "signal": _label(_TURNOVER_LABEL, lo.turnover_status),
                            "action": _lot_action(lo.sell_priority, lo.disposal_candidate),
                        }
                        for lo in shown_lots
                    ],
                    empty_text="이 날짜에 남아 있는 Lot 이 없습니다",
                ),
                footer=("급한 Lot 이 위에 옵니다 — 폐기 검토 · 우선 출고 · 신선도 잔여가 "
                        "적은 순입니다. 관리 조치는 서버가 낸 판정을 옮긴 것이고 화면이 "
                        "신선도 숫자를 보고 다시 정하지 않습니다."),
            ),
            Card(
                key="principle", title="재고 처리 원칙",
                lead=_MIXED_AXIS_NOTE,
                bullets=[
                    "예약과 할당은 재고를 줄이지 않습니다 — 실제 출고 때 줄어듭니다",
                    "판매가능량은 화면이 계산하지 않습니다. 서버 값을 그대로 씁니다",
                    "등급 미확정 Lot 은 «미확정» 으로 적습니다 — 특으로 넘겨짚지 않습니다",
                    "현재고 · Lot 잔량은 입출고 기록을 기준일까지 더한 값입니다",
                    "회전 상태와 관리 조치는 그날 사실에서 서버가 낸 판정입니다",
                ],
            ),
        ],
    )


#: 입고 내역처럼 **이력이 계속 쌓이는 표**에 한 번에 펼치는 최대 줄 수.
#: 🔴 실측 290행이 8개월치로 나와 «지금 할 일» 이 묻혔다. 자르되 **몇 건을 덜 폈는지
#:    footer 에 적는다** — 숨기는 것과 접는 것은 다르다.
_MAX_HISTORY_ROWS = 20


def _inbound_pane(inb: ConsoleInboundResponse, as_of: date) -> Pane:
    summary = inb.arrival_summary
    transit = inb.in_transit

    #  ★ 아직 처리가 안 끝난 건을 **맨 위로** 올리고, 그다음 최근 도착 순이다.
    #    🔴 잘라내도 «아직 할 일» 은 안 잘린다 — 그것이 이 정렬의 이유다.
    #    판정은 하지 않는다. 재고가 섰거나(`stock_applied`) 수용 0 으로 끝난
    #    (`settled_without_stock` · #805) 건은 둘 다 **끝난 것**이다.
    receipts = sorted(
        (r for r in inb.receipts if _on_screen(r.item_name)),
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
        stats=[
            #  🔴 «오늘» 이라고 적지 않는다 — 이 화면은 과거 기준일도 연다.
            Stat(label="도착 예정", value=f"{summary.due_count:,}", unit="건",
                 detail=("기준일에 받을 수 있는 입고" if summary.due_count
                         else "예정된 입고 없음"),
                 tone="good" if summary.due_count else "neutral",
                 raw=float(summary.due_count)),
            Stat(label="도착 지연", value=f"{summary.overdue_count:,}", unit="건",
                 detail=("예정일이 지났는데 안 들어온 것" if summary.overdue_count
                         else "지연된 입고 없음"),
                 tone="bad" if summary.overdue_count else "good",
                 raw=float(summary.overdue_count)),
            Stat(label="처리 보류", value=f"{summary.blocked_count:,}", unit="건",
                 detail="입고 처리에 필요한 정보 확인 필요",
                 tone="warn" if summary.blocked_count else "neutral",
                 raw=float(summary.blocked_count)),
            Stat(label="확인 필요", value=f"{summary.unresolved_count:,}", unit="건",
                 detail="입고 상태 확인에 필요한 정보 부족",
                 tone="warn" if summary.unresolved_count else "neutral",
                 raw=float(summary.unresolved_count)),
        ],
        cards=[
            Card(
                key="flow", title="입고 처리 흐름",
                #  ⚠️ 내부 이름은 `in_transit` 이지만 차량 운송을 추적하는 값이 아니다 —
                #     「입고 일정에 올라 있고 아직 도착하지 않은 건」이라 그렇게 적는다.
                flow=["실매입 확정", "입고 예정", "창고 도착", "검수", "입고 처리 완료"],
                #  🔴 흐름의 끝을 «재고 반영» 으로 못박지 않는다 (#805) — 수용할 것이
                #     0 이면 재고를 안 만들고도 입고 처리는 끝난다.
                lead=Note(
                    tone="info",
                    text=("**창고 도착과 재고 반영은 다릅니다.** 검수를 통과한 수용 수량만 "
                          "재고가 됩니다. 수용할 물량이 없으면 재고는 안 생기지만 "
                          "**입고 처리는 끝난 것**입니다."),
                ),
            ),
            Card(
                key="transit", title="입고 예정",
                subtitle="아직 창고에 도착하지 않았습니다 — 재고가 아닙니다",
                source_ref="확정 매입 · 도착 예정 축",
                lead=(
                    None if transit is not None else Note(
                        tone="warn",
                        text=("이 날짜의 입고 예정 목록을 **확인하지 못했습니다** — "
                              "0 건이라는 뜻이 아닙니다."),
                    )
                ),
                #  🔴 `inbound_id` · `purchase_id` 는 본문에 싣지 않는다.
                table=_t(
                    [("item", "품목", "left"), ("qty", "수량", "right"),
                     ("eta", "예정 도착일", "left"), ("state", "현재 상태", "left")],
                    [
                        {
                            "item": t.item,
                            "qty": _kg_cell(t.quantity_kg),
                            "eta": _md(t.expected_arrival_date),
                            #  ★ 도착 «자격» 판정(진행 가능 · 처리 보류)은 위 카드 숫자가
                            #    주인이다. 여기서는 **예정일이 지났나** 하나만 적는다 —
                            #    아직 도착 안 했다는 사실은 이 목록의 모집단이 보장한다.
                            "state": (
                                "—" if t.expected_arrival_date is None
                                else "도착 예정" if t.expected_arrival_date > as_of
                                else "도착 지연"
                            ),
                        }
                        for t in (transit or [])
                    ],
                    empty_text=(
                        "현재 입고 예정 없음"
                        if transit is not None
                        else "이 날짜의 입고 예정 목록을 확인하지 못했습니다 — 0 이 아닙니다"
                    ),
                ),
            ),
            Card(
                key="receipt", title="입고 처리 현황",
                subtitle="창고에 도착한 물량의 검수와 재고 처리 상태입니다",
                #  🔴 `arrival_schedule` 은 **표가 아니라 계약 필드명**이었다.
                #     실제 출처는 이 둘이다.
                source_ref="inbound_receipts · inbound_inspections",
                #  🔴 Receipt ID 는 본문에 싣지 않는다 — 사용자가 읽을 값이 아니다.
                table=_t(
                    [("arrive", "도착일", "left"), ("item", "품목", "left"),
                     ("ord", "주문", "right"), ("acc", "수용", "right"),
                     ("hold", "보류", "right"), ("rej", "거절", "right"),
                     ("verdict", "검수 결과", "left"),
                     ("state", "처리 상태", "left"), ("applied", "재고 처리", "left")],
                    receipt_rows,
                    empty_text="이 날짜까지 창고에 도착한 물량이 없습니다",
                ),
                footer=(
                    "재고가 되는 것은 주문 수량이 아니라 **합격 수량**입니다. "
                    "아직 재고에 안 잡힌 건을 위에 두고 최근 도착 순으로 적습니다."
                    + (f" 여기에는 최근 {len(shown_receipts)}건만 폈습니다 — "
                       f"기준일까지 도착한 나머지 {hidden_receipts:,}건은 접었습니다."
                       if hidden_receipts > 0 else "")
                ),
            ),
        ],
    )


def _outbound_pane(
    ob: ConsoleOutboundResponse, as_of: date, *, conn: Any, sim_run_id: str
) -> Pane:
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
    #     여덟 번 받고 커넥션도 여덟 번 열던 자리다. 예약이 없으면 **묻지도 않는다.**
    fefo_targets = [r for r in shown_reservations if r.unallocated_qty_kg > 0]
    candidates_by_item = (
        get_fefo_candidates_by_item(
            conn=conn, sim_run_id=sim_run_id,
            item_ids=[r.item_id for r in fefo_targets], as_of=as_of,
        )
        if fefo_targets
        else {}
    )
    #  🔴 raw `lot_id` 를 표에 싣지 않는다 — 후보가 들고 온 구조화 칸으로 이름을 만든다.
    for resv in fefo_targets:
        candidates = list(candidates_by_item.get(resv.item_id, ()))
        names = _candidate_names(resv.item_name or resv.item_id, candidates)
        for rank, (cand, name) in enumerate(zip(candidates, names, strict=True), start=1):
            fefo_rows.append(
                {
                    "resv": _resv_name(resv),
                    #  ★ 같은 품목 예약이 여럿이면 예약 이름만으로는 안 갈린다 —
                    #    그 예약이 아직 못 채운 몫을 같이 적어 줄을 구분한다.
                    "need": _kg_cell(resv.unallocated_qty_kg),
                    "lot": name,
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
            Stat(label="예약", value=f"{len(shown_reservations):,}", unit="건",
                 detail=(f"전량 출고·해제된 {settled_count}건은 뺐습니다"
                         if settled_count else None),
                 tone="good" if shown_reservations else "neutral",
                 raw=float(len(shown_reservations))),
            Stat(label="출고 후보", value=f"{len(fefo_rows):,}", unit="건",
                 detail="예약이 있어야 후보가 나옵니다",
                 tone="neutral", raw=float(len(fefo_rows))),
        ],
        cards=[
            Card(
                key="fefo", title="신선도 우선 출고 후보",
                subtitle="먼저 상하는 것을 먼저 내보냅니다 (FEFO 기준)",
                source_ref="inventory_reservations · inventory_lots",
                #  🔴 **«자동 배정» 처럼 보이면 안 된다** — 도메인 계약도 «추천만 한다 —
                #     고르지도 쓰지도 않는다» 이다 (`get_fefo_candidates_by_item`).
                lead=Note(
                    tone="info",
                    text=("이 목록은 **추천일 뿐 자동으로 배정되지 않습니다.** 예약이 "
                          "없으면 후보도 없습니다. 예약 · 할당은 기준일 시점 값이지만, "
                          "🔴 **후보의 «가용» 은 «지금» 재고입니다** — Lot 잔량에서 살아 "
                          "있는 할당을 뺀 값이라 기준일로 되살리지 않습니다."),
                ),
                table=_t(
                    [("resv", "예약", "left"), ("need", "미할당", "right"),
                     ("lot", "Lot", "left"),
                     ("grade", "등급", "left"), ("qty", "가용", "right"),
                     ("fresh", "신선도 잔여", "right"), ("rank", "순서", "right")],
                    fefo_rows,
                    empty_text="신선도 우선 출고 후보 없음",
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

#: **그날 값이 아닌** 칸은 이제 Zone 자리 수 하나뿐이다 — 되살릴 정본(자리 정원
#: 유효일)이 없어서다. 판매가능량·예약 3칸은 #760(LOG-HIST-002)으로 기준일 축이 됐다.
#: 화면이 그 사실을 읽고 적는다.
_MIXED_AXIS_NOTE = Note(
    tone="info",
    text=("**이 화면의 수치는 기준일 시점 값입니다** — 현재고 · Lot · 신선도 · 회전 · "
          "입고 내역 · 예약 · 할당 · 판매가능량 모두. 딱 하나, **창고 자리 수만 «지금» "
          "값입니다** — 자리 정원에는 유효일이 없어 기준일로 되살릴 수 없습니다."),
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
                _outbound_pane(ob, as_of, conn=conn, sim_run_id=run),
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
