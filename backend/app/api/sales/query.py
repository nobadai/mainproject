"""판매 탭 — Sales Dashboard Service 값을 화면 부품으로 옮긴다."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.api.primitives import Card, Chart, Column, Note, Series, Source, Stat, Table
from app.api.sales.schema import SalesTab
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.sales.dashboard_service import get_sales_dashboard


def build(as_of: date) -> SalesTab:
    dash = get_sales_dashboard(sim_run_id=BURN_IN_SIM_RUN_ID, as_of=as_of)
    summary = dash.summary
    quantity_detail = f"고객 {summary.customer_count}곳 · 총 {_kg(summary.total_sales_quantity_kg)}"
    receivable_detail = (
        f"수금 {_won(summary.received_amount_krw)} · "
        f"미수 {_won(summary.outstanding_receivables_krw)}"
    )

    return SalesTab(
        stats=[
            Stat(
                label="총 판매금액",
                value=_manwon(summary.total_sales_amount_krw),
                unit="만원",
                detail=f"{as_of.isoformat()} 기준 판매 {summary.sales_count}건",
                tone="info",
                raw=_raw(summary.total_sales_amount_krw),
            ),
            Stat(
                label="총 판매량",
                value=_ton(summary.total_sales_quantity_kg),
                unit="톤",
                detail=quantity_detail,
                tone="good",
                raw=_raw(summary.total_sales_quantity_kg),
            ),
            Stat(
                label="공헌이익",
                value=_manwon(summary.contribution_profit_krw),
                unit="만원",
                detail=f"매출에서 변동비를 뺀 금액 · {summary.contribution_margin_pct}%",
                tone="good",
                raw=_raw(summary.contribution_profit_krw),
            ),
            Stat(
                label="아직 받을 돈",
                value=_manwon(summary.outstanding_receivables_krw),
                unit="만원",
                detail=receivable_detail,
                tone="warn" if summary.outstanding_receivables_krw > 0 else "good",
                raw=_raw(summary.outstanding_receivables_krw),
            ),
        ],
        read_only=Note(
            tone="info",
            text=(
                "**이 화면은 조회 전용입니다.** 시나리오 실행 없이 저장된 판매 · 수금 "
                "결과만 봅니다 — «얼마 팔았는지, 언제 돈을 받는지»를 바로 확인하는 데 "
                "집중합니다."
            ),
        ),
        cards=[
            _summary_card(dash),
            _items_card(dash),
            _recent_sales_card(dash),
            _receivables_card(dash),
        ],
        source=Source(
            filled=True,
            owner="판매",
            note=(
                "Sales Dashboard · sales · sale_items · receivables · "
                f"{dash.meta.sim_run_id} · {dash.meta.as_of} · {dash.meta.data_type}"
            ),
        ),
    )


def _summary_card(dash) -> Card:
    collection = dash.collection_summary
    collected = collection.get("COLLECTED")
    partial = collection.get("PARTIAL")
    open_ = collection.get("OPEN")
    return Card(
        key="summary",
        title="이번 달 판매를 한눈에",
        subtitle="저장된 판매 Header와 수금 원장 집계",
        source_ref="sales · receivables",
        stats=[
            Stat(label="판매 처리", value=f"{dash.summary.sales_count}건", detail="sale_date 기준"),
            Stat(
                label="수금 상태",
                value=(
                    f"완료 {0 if collected is None else collected.count} · "
                    f"일부 {0 if partial is None else partial.count} · "
                    f"예정 {0 if open_ is None else open_.count}"
                ),
                detail=f"받은 돈 {_won(dash.summary.received_amount_krw)}",
                tone="info",
            ),
            Stat(
                label="공헌이익률",
                value=str(dash.summary.contribution_margin_pct),
                unit="%",
                detail="Dashboard Service 집계값",
                tone="good",
                raw=_raw(dash.summary.contribution_margin_pct),
            ),
        ],
    )


def _items_card(dash) -> Card:
    return Card(
        key="items",
        title="품목별 매출",
        subtitle="실제 판매 line 기준",
        source_ref="sale_items · items",
        table=Table(
            columns=[
                Column(key="item", label="품목"),
                Column(key="lines", label="판매 line", align="right", mono=True),
                Column(key="qty", label="판매량", align="right", mono=True),
                Column(key="amount", label="판매금액", align="right", mono=True),
                Column(key="profit", label="공헌이익", align="right", mono=True),
                Column(key="margin", label="이익률", align="right", mono=True),
                Column(key="unit", label="평균단가", align="right", mono=True),
            ],
            rows=[
                {
                    "item": item.item_name,
                    "lines": item.line_count,
                    "qty": _kg(item.total_quantity_kg),
                    "amount": _won(item.sales_amount_krw),
                    "profit": _won(item.contribution_profit_krw),
                    "margin": f"{item.contribution_margin_pct}%",
                    "unit": f"{_number(item.avg_unit_price_krw_per_kg)}원/kg",
                }
                for item in dash.items
            ],
            empty_text="이 기간에 판매 품목이 없습니다",
        ),
    )


def _recent_sales_card(dash) -> Card:
    return Card(
        key="recent",
        title="최근 판매 내역",
        source_ref="sales · partners · receivables",
        table=Table(
            columns=[
                Column(key="d", label="판매일", mono=True),
                Column(key="no", label="판매번호", mono=True),
                Column(key="partner", label="거래처"),
                Column(key="qty", label="판매량", align="right", mono=True),
                Column(key="amount", label="판매금액", align="right", mono=True),
                Column(key="margin", label="공헌이익", align="right", mono=True),
                Column(key="due", label="수금 예정일", mono=True),
                Column(key="state", label="수금 상태"),
                Column(key="outbound", label="출고 상태"),
            ],
            rows=[
                {
                    "d": sale.sale_date.isoformat(),
                    "no": sale.sale_id,
                    "partner": sale.partner_name,
                    "qty": _kg(sale.total_quantity_kg),
                    "amount": _won(sale.total_amount_krw),
                    "margin": _won(sale.contribution_profit_krw),
                    "due": sale.collection_due_date.isoformat(),
                    "state": sale.collection_status_label,
                    "outbound": sale.order_status,
                }
                for sale in dash.recent_sales
            ],
            empty_text="이 기간에 판매가 없습니다",
        ),
    )


def _receivables_card(dash) -> Card:
    by_due: dict[date, Decimal] = defaultdict(Decimal)
    for receivable in dash.receivables:
        if receivable.outstanding_amount_krw > 0:
            by_due[receivable.due_date] += receivable.outstanding_amount_krw
    due_dates = sorted(by_due)
    values = [_to_million(by_due[due_date]) for due_date in due_dates]
    y_max = _chart_max(values)
    return Card(
        key="ar",
        title="남은 수금 일정",
        subtitle="수금 완료분을 제외한 outstanding 기준",
        source_ref="receivables",
        chart=Chart(
            label="남은 수금 일정",
            y_min=0,
            y_max=y_max,
            y_ticks=_ticks(0, y_max),
            y_unit="M",
            series=[Series(name="남은 수금", data=values, tone="info")],
            x_labels=_spread_labels([f"{d.month}/{d.day}" for d in due_dates]),
            note=Note(
                tone="neutral",
                text=(
                    "수금 완료 채권은 제외하고, 일부 수금과 수금 예정 채권의 "
                    "**남은 금액**만 날짜별로 합산했습니다."
                ),
            ),
        ),
        table=Table(
            columns=[
                Column(key="due", label="수금 예정일", mono=True),
                Column(key="sale", label="판매번호", mono=True),
                Column(key="partner", label="거래처"),
                Column(key="original", label="원금", align="right", mono=True),
                Column(key="received", label="받은 돈", align="right", mono=True),
                Column(key="outstanding", label="남은 돈", align="right", mono=True),
                Column(key="status", label="상태"),
                Column(key="d_day", label="D-day", align="right", mono=True),
            ],
            rows=[
                {
                    "due": receivable.due_date.isoformat(),
                    "sale": receivable.sale_id,
                    "partner": receivable.partner_name,
                    "original": _won(receivable.original_amount_krw),
                    "received": _won(receivable.received_amount_krw),
                    "outstanding": _won(receivable.outstanding_amount_krw),
                    "status": receivable.display_status,
                    "d_day": None if receivable.d_day is None else receivable.d_day,
                }
                for receivable in dash.receivables
            ],
            empty_text="이 기간에 매출채권이 없습니다",
        ),
    )


def _won(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1)):,.0f}원"


def _manwon(value: Decimal) -> str:
    return f"{(value / Decimal(10000)).quantize(Decimal(1)):,.0f}"


def _kg(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1)):,.0f} kg"


def _ton(value: Decimal) -> str:
    return f"{(value / Decimal(1000)).quantize(Decimal('0.1')):,.1f}"


def _number(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1)):,.0f}"


def _raw(value: Decimal) -> float:
    return float(value)


def _to_million(value: Decimal) -> float:
    return float((value / Decimal(1_000_000)).quantize(Decimal("0.001")))


def _chart_max(values: list[float]) -> float:
    if not values:
        return 1
    return max(1, round(max(values) * 1.2, 1))


def _ticks(start: float, end: float) -> list[float]:
    middle = round((start + end) / 2, 1)
    return [start, middle, end]


def _spread_labels(labels: list[str]) -> list[str]:
    if len(labels) <= 2:
        return labels
    visible = {0, len(labels) - 1}
    visible.update(range(6, len(labels) - 1, 7))
    return [label if index in visible else "" for index, label in enumerate(labels)]
