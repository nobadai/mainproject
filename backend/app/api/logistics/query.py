"""재고 · 물류 탭 — 값을 읽어오는 곳.

╔══════════════════════════════════════════════════════════════════════════╗
║  ★ 물류 파트가 채우는 파일입니다. `build()` 안쪽만 바꾸면 됩니다.          ║
║                                                                          ║
║  지금은 **예시값**입니다 (`Source.filled = False`) — 화면에 「예시값」      ║
║  딱지가 붙습니다.                                                         ║
║                                                                          ║
║  카드를 더 넣고 싶으면 `Card(...)` 를 목록에 하나 더 넣으면 됩니다.        ║
║  **화면은 안 고쳐도 됩니다.**                                             ║
║                                                                          ║
║  읽을 곳: inventory_lots · inventory_reservations ·                       ║
║  inventory_allocations · arrival_schedule · zone_capacity.                ║
║  DB 는 `app/logistics/db.py` 를 그대로 쓰세요.                             ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from datetime import date

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

PANES = ("stock", "inbound", "warehouse", "outbound")

#: 창고 재고 (kg). **1번 칸은 물류가 안 보고한 날이라 공란이다 — 0 이 아니다.**
_ONHAND = [21400, None, 19800, 19800, 17200, 17200, 16900, 15100, 14600]
_PROJ = [14600, 26200, 25700, 25100]
_IN_TRANSIT = [None, None, None, None, None, None, None, 12000, 12000]
#: 마지막 실적 = 화면 위 요약 숫자. **한 군데서만 정한다.**
ONHAND_NOW = 14600


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


def _stock_pane() -> Pane:
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
                        {"id": "RSV-001", "item": "배추", "need": "500 kg", "resv": "500 kg",
                         "alloc": "300 kg", "left": "200 kg", "due": "01-08",
                         "state": "PARTIALLY_ALLOCATED"},
                        {"id": "RSV-002", "item": "무", "need": "200 kg", "resv": "200 kg",
                         "alloc": "200 kg", "left": "0 kg", "due": "01-08",
                         "state": "ALLOCATED"},
                    ],
                    empty_text="확보된 재고가 없습니다",
                ),
            ),
            Card(
                key="lots", title="Lot 상태", subtitle="Snapshot + turnover 계산 결과",
                source_ref="inventory_lots",
                table=_t(
                    [("lot", "Lot", "left"), ("item", "품목", "left"), ("grade", "등급", "left"),
                     ("qty", "잔량", "right"), ("fresh", "신선도 잔여", "right"),
                     ("turn", "회전 잔여", "right"), ("state", "Lot 상태", "left"),
                     ("signal", "회전 Signal", "left")],
                    [
                        {"lot": "LOT-B-001", "item": "배추", "grade": "등급 미확정",
                         "qty": "850 kg", "fresh": "4일", "turn": "3일",
                         "state": "ACTIVE", "signal": "SELL_PRIORITY"},
                        {"lot": "LOT-B-002", "item": "배추", "grade": "특",
                         "qty": "1,200 kg", "fresh": "7일", "turn": "6일",
                         "state": "ACTIVE", "signal": "NORMAL"},
                        {"lot": "LOT-M-001", "item": "무", "grade": "특",
                         "qty": "1,600 kg", "fresh": "8일", "turn": "6일",
                         "state": "ACTIVE", "signal": "NORMAL"},
                        {"lot": "LOT-Y-OLD", "item": "양파", "grade": "등급 미확정",
                         "qty": "900 kg", "fresh": "-1일", "turn": "-2일",
                         "state": "ACTIVE", "signal": "STORAGE_TARGET_EXCEEDED"},
                    ],
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


def _inbound_pane() -> Pane:
    return Pane(
        key="inbound", label="입고 처리",
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
                key="receipt", title="도착 · Receipt 현황", source_ref="arrival_schedule",
                table=_t(
                    [("id", "Receipt", "left"), ("item", "품목", "left"),
                     ("qty", "도착 수량", "right"), ("arrive", "도착일", "left"),
                     ("state", "상태", "left")],
                    [{"id": "RCPT-0107-01", "item": "배추", "qty": "3,587 kg",
                      "arrive": "01-07", "state": "INSPECTING"}],
                    empty_text="오늘 도착 예정이 없습니다",
                ),
            ),
        ],
    )


def _warehouse_pane() -> Pane:
    return Pane(
        key="warehouse", label="창고 배치",
        cards=[
            Card(
                key="zone", title="Zone Capacity", source_ref="zone_capacity",
                table=_t(
                    [("zone", "Zone", "left"), ("kind", "기능", "left"),
                     ("used", "사용", "right"), ("cap", "한도", "right"),
                     ("free", "여유", "right")],
                    [
                        {"zone": "Z-COLD-01", "kind": "냉장", "used": "4,550 kg",
                         "cap": "8,000 kg", "free": "3,450 kg"},
                        {"zone": "Z-DRY-01", "kind": "상온", "used": "0 kg",
                         "cap": "4,000 kg", "free": "4,000 kg"},
                    ],
                ),
                footer="날짜별 입고 여유는 승인된 물량을 뺀 값입니다 — 오늘 4,059kg.",
            ),
        ],
    )


def _outbound_pane() -> Pane:
    return Pane(
        key="outbound", label="출고 · 운송",
        cards=[
            Card(
                key="fefo", title="FEFO 후보",
                subtitle="먼저 상하는 것을 먼저 내보냅니다 (First Expired, First Out)",
                table=_t(
                    [("lot", "Lot", "left"), ("item", "품목", "left"),
                     ("qty", "잔량", "right"), ("fresh", "신선도 잔여", "right"),
                     ("rank", "순서", "right")],
                    [
                        {"lot": "LOT-Y-OLD", "item": "양파", "qty": "900 kg",
                         "fresh": "-1일", "rank": 1},
                        {"lot": "LOT-B-001", "item": "배추", "qty": "850 kg",
                         "fresh": "4일", "rank": 2},
                    ],
                ),
            ),
        ],
    )


def build(as_of: date, pane: str) -> LogisticsTab:
    return LogisticsTab(
        panes=[_stock_pane(), _inbound_pane(), _warehouse_pane(), _outbound_pane()],
        selected=pane,
        principle=Note(
            tone="neutral",
            text=("재고 수치는 **물류가 보고한 것만** 적습니다. 보고가 없는 날은 "
                  "0 이 아니라 **공란**입니다 — 둘은 다릅니다."),
        ),
        source=Source(
            filled=False, owner="물류",
            note="app/api/logistics/query.py 의 build() 를 채우면 실제 값이 됩니다",
        ),
    )


def dashboard_stock(n: int, at: int) -> Chart:
    """대시보드에 얹을 재고 그래프.

    ★ **대시보드가 아니라 여기서 만듭니다.** 요약 숫자(`ONHAND_NOW`)와 같은
      곳에서 나와야 둘이 안 갈라집니다. 실제로 갈라졌던 적이 있습니다 —
      요약은 4,550kg 인데 그래프 끝은 14,600kg 이었습니다.
    """
    def pad(head: list) -> list:
        return list(head) + [None] * (n - len(head))

    tail = [None] * at + list(_PROJ) + [None] * max(0, n - at - len(_PROJ))
    return Chart(
        label="창고 재고", y_min=0, y_max=40000,
        y_ticks=[10000, 20000, 30000, 40000],
        # ★ kg 로 그리고 톤으로 적는다. y_unit 만으로는 «10,000t» 이 된다
        y_labels=["10t", "20t", "30t", "40t"],
        series=[
            Series(name="보유", data=pad(_ONHAND), tone="good", end_dot=True),
            Series(name="추정 보유", data=tail[:n], tone="good", dashed=True),
            Series(name="운송 중", data=pad(_IN_TRANSIT), tone="good",
                   dashed=True, opacity=0.5),
        ],
        markers=[Marker(index=6, value=16900, label="폐기 300", tone="bad")],
        note=Note(
            tone="warn",
            text=("★ 둘째 칸은 **0 이 아니라 공란**입니다 — 물류가 그날을 보고하지 "
                  "않았습니다. 0 으로 그리면 «재고가 없었다» 는 거짓말이 됩니다."),
        ),
    )
