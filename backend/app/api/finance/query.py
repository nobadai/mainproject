"""재무 탭 — Finance Dashboard Service 값을 화면 부품으로 옮긴다."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.api.finance.schema import FinanceTab, FlowCell, StateOption
from app.api.primitives import Chart, Column, Marker, Note, Series, Source, Stat, Table
from app.finance.dashboard_service import get_finance_cashflow, get_finance_dashboard
from app.finance.schemas import FinanceClosingItem, FinanceDashboardResponse, FinanceStateView
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

STATES = ("base", "loan")
_STATE_TO_MODE = {"base": "BASE_NO_LOAN", "loan": "LOAN_BASELINE"}
_STATE_LABELS = {
    "base": "대출 없이 운영 · BASE_NO_LOAN",
    "loan": "대출 반영 · LOAN_BASELINE",
}

# 통합 대시보드에 얹을 현금 잔고 (백만원). 이 함수는 별도 화면에서 아직 사용한다.
_DASH_ACTUAL = [52.4, 52.1, 51.8, 51.8, 49.6, 49.6, 49.6, 47.9, 47.9]
_DASH_PROJ = [47.9, 40.6, 40.6, 35.7]
_DASH_FLOOR = 30.0


def build(as_of: date, state: str) -> FinanceTab:
    dash = get_finance_dashboard(sim_run_id=BURN_IN_SIM_RUN_ID, as_of=as_of)
    flow = get_finance_cashflow(sim_run_id=BURN_IN_SIM_RUN_ID, as_of=as_of, days=30)
    selected = _state_by_key(dash, state)
    receivables = dash.ledger_summary["receivables"]
    payables = dash.ledger_summary["payables"]
    receivable_detail = (
        f"매출채권 {receivables.count}건 · "
        f"받은 돈 {_won(receivables.received_amount_krw)}"
    )

    return FinanceTab(
        states=[
            StateOption(
                key=key,
                label=_STATE_LABELS[key],
                explain=_state_explain(_state_by_mode(dash, mode)),
            )
            for key, mode in _STATE_TO_MODE.items()
            if _state_by_mode(dash, mode) is not None
        ],
        selected=state,
        stats=[
            Stat(
                label="현금 잔액",
                value=_manwon(selected.current_cash_krw),
                unit="만원",
                detail=f"{as_of.isoformat()} {selected.financing_mode} 기준",
                tone="good" if selected.current_cash_krw >= 0 else "bad",
                raw=_raw(selected.current_cash_krw),
            ),
            Stat(
                label="최소 운영자금",
                value=_manwon(selected.minimum_operating_cash_krw),
                unit="만원",
                detail="finance_states.minimum_operating_cash_krw",
                raw=_raw(selected.minimum_operating_cash_krw),
            ),
            Stat(
                label="받을 돈",
                value=_manwon(receivables.outstanding_amount_krw),
                unit="만원",
                detail=receivable_detail,
                tone="warn" if receivables.outstanding_amount_krw > 0 else "good",
                raw=_raw(receivables.outstanding_amount_krw),
            ),
            Stat(
                label="부채",
                value=_manwon(selected.current_debt_krw),
                unit="만원",
                detail=f"{selected.financing_mode} current_debt_krw",
                tone="warn" if selected.current_debt_krw > 0 else "good",
                raw=_raw(selected.current_debt_krw),
            ),
        ],
        explain=Note(tone="neutral", text=_state_explain(selected)),
        read_only=Note(
            tone="info",
            text=(
                "**이 화면은 조회 전용입니다.** 에이전트 추천이나 시나리오 결과를 "
                "보여주지 않습니다. 저장된 값에서 «현금이 얼마 남았는지, 언제 나가고 "
                "언제 들어오는지»를 바로 확인하는 데 집중합니다."
            ),
        ),
        cash_chart=_cash_chart(flow.cashflow),
        flows=_flows(dash.cashflow_summary),
        balances=[
            Stat(
                label="받을 돈 총액",
                value=_manwon(receivables.original_amount_krw),
                unit="만원",
                detail=(
                    f"완료 {receivables.collected_count} · 일부 {receivables.partial_count} · "
                    f"예정 {receivables.open_count}"
                ),
                tone="warn" if receivables.outstanding_amount_krw > 0 else "good",
                raw=_raw(receivables.original_amount_krw),
            ),
            Stat(
                label="이미 받은 돈",
                value=_manwon(receivables.received_amount_krw),
                unit="만원",
                detail="receivables.received_amount_krw",
                raw=_raw(receivables.received_amount_krw),
            ),
            Stat(
                label="남은 매입대금",
                value=_manwon(payables.outstanding_amount_krw),
                unit="만원",
                detail=f"payables {payables.count}건",
                tone="warn" if payables.outstanding_amount_krw > 0 else "good",
                raw=_raw(payables.outstanding_amount_krw),
            ),
            Stat(
                label="장부 재고가치",
                value=_manwon(selected.inventory_book_value_krw),
                unit="만원",
                detail="finance_states.inventory_book_value_krw",
                raw=_raw(selected.inventory_book_value_krw),
            ),
        ],
        balances_note=Note(
            tone="good" if payables.outstanding_amount_krw == 0 else "warn",
            text=(
                "**현재 미지급 매입대금은 없습니다.** payables outstanding_amount 가 0원입니다."
                if payables.outstanding_amount_krw == 0
                else f"남은 매입대금은 {_won(payables.outstanding_amount_krw)}입니다."
            ),
        ),
        closings=_closings_table(dash.recent_closings),
        tables_read=["finance_states", "daily_closings", "receivables", "payables", "expenses"],
        source=Source(
            filled=True,
            owner="재무",
            note=(
                "Finance Dashboard · finance_states · daily_closings · receivables · "
                f"payables · expenses · {dash.meta.sim_run_id} · {dash.meta.as_of} · "
                f"{dash.meta.data_type}"
            ),
        ),
    )


def dashboard_cash(n: int, at: int) -> Chart:
    """대시보드에 얹을 현금 그래프. **재무가 만듭니다** — 대시보드가 아닙니다."""
    tail = [None] * at + list(_DASH_PROJ) + [None] * max(0, n - at - len(_DASH_PROJ))
    return Chart(
        label="현금 잔고",
        y_min=25,
        y_max=60,
        y_ticks=[30, 40, 50, 60],
        y_unit="M",
        series=[
            Series(
                name="최소 운영현금",
                data=[_DASH_FLOOR] * n,
                tone="bad",
                dashed=True,
                width=1,
                opacity=0.7,
            ),
            Series(
                name="실적",
                data=list(_DASH_ACTUAL) + [None] * (n - len(_DASH_ACTUAL)),
                tone="warn",
                end_dot=True,
            ),
            Series(name="추정", data=tail[:n], tone="warn", dashed=True),
        ],
        markers=[Marker(index=at + 1, value=40.6, label="지급 -7.3M", tone="warn")],
        note=Note(
            tone="neutral",
            text="점선부터는 **아직 안 일어난 일**입니다. 승인한 매입의 지급 예정이 반영됩니다.",
        ),
    )


def _state_by_key(dash: FinanceDashboardResponse, state: str) -> FinanceStateView:
    selected = _state_by_mode(dash, _STATE_TO_MODE[state])
    if selected is None:
        raise LookupError(f"Finance state was not found: {_STATE_TO_MODE[state]}")
    return selected


def _state_by_mode(
    dash: FinanceDashboardResponse, financing_mode: str
) -> FinanceStateView | None:
    return next(
        (state for state in dash.states if state.financing_mode == financing_mode),
        None,
    )


def _state_explain(state: FinanceStateView | None) -> str:
    if state is None:
        return "해당 재무 기준 상태가 DB에 없습니다."
    note = "" if state.note is None else f" {state.note}"
    return (
        f"DB에 저장된 {state.financing_mode} 상태입니다. 현금 잔액은 "
        f"{_won(state.current_cash_krw)}이고 최소 운영자금 기준 buffer는 "
        f"{_won(state.operating_cash_buffer_krw)}입니다.{note}"
    )


def _cash_chart(rows: list[FinanceClosingItem]) -> Chart:
    base = [_to_million(row.base_cash_balance_krw) for row in rows]
    loan = [_to_million(row.loan_cash_balance_krw) for row in rows]
    minimum = [_to_million(row.minimum_operating_cash_krw) for row in rows]
    values = [*base, *loan, *minimum]
    y_min, y_max = _chart_range(values)
    return Chart(
        label="일별 현금 잔액",
        y_min=y_min,
        y_max=y_max,
        y_ticks=_ticks(y_min, y_max),
        y_unit="M",
        series=[
            Series(name="대출 없이 운영", data=base, tone="info", width=2.2),
            Series(name="대출 반영", data=loan, tone="good", width=2.2),
            Series(name="최소 운영자금", data=minimum, tone="bad", width=1.3, dashed=True),
        ],
        note=Note(tone="neutral", text="daily_closings 기준 일별 마감 현금 잔액"),
        x_labels=_spread_labels([f"{row.close_date.month}/{row.close_date.day}" for row in rows]),
    )


def _flows(summary) -> list[FlowCell]:
    return [
        FlowCell(
            label="상품 매입으로 나간 돈",
            value=_manwon_with_unit(summary.purchase_cash_out_krw),
            term="purchase cash out",
        ),
        FlowCell(
            label="물류로 나간 돈",
            value=_manwon_with_unit(summary.logistics_cash_out_krw),
            term="logistics cash out",
        ),
        FlowCell(
            label="급여 · 이자로 나간 돈",
            value=_manwon_with_unit(summary.payroll_interest_cash_out_krw),
            term="payroll + interest",
        ),
        FlowCell(
            label="판매로 잡힌 금액",
            value=_manwon_with_unit(summary.sales_recognized_krw),
            term="sales recognized",
            tone="good",
        ),
        FlowCell(
            label="실제로 들어온 수금",
            value=_manwon_with_unit(summary.collection_cash_in_krw),
            term="collection cash in",
            tone="good" if summary.collection_cash_in_krw > 0 else "warn",
        ),
    ]


def _closings_table(rows: list[FinanceClosingItem]) -> Table:
    return Table(
        columns=[
            Column(key="d", label="날짜", mono=True),
            Column(key="buy", label="매입 지급", align="right", mono=True),
            Column(key="log", label="물류비", align="right", mono=True),
            Column(key="pay", label="급여 · 이자", align="right", mono=True),
            Column(key="sale", label="판매 인식", align="right", mono=True),
            Column(key="col", label="수금", align="right", mono=True),
            Column(key="base", label="BASE 현금", align="right", mono=True),
            Column(key="loan", label="대출 반영 현금", align="right", mono=True),
        ],
        rows=[
            {
                "d": row.close_date.isoformat(),
                "buy": _won(row.purchase_cash_out_krw),
                "log": _won(row.logistics_cash_out_krw),
                "pay": _won(row.payroll_interest_cash_out_krw),
                "sale": _won(row.sales_recognized_krw),
                "col": _won(row.collection_cash_in_krw),
                "base": _won(row.base_cash_balance_krw),
                "loan": _won(row.loan_cash_balance_krw),
            }
            for row in rows
        ],
        empty_text="이 기간에 일마감 데이터가 없습니다",
    )


def _won(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1)):,.0f}원"


def _manwon(value: Decimal) -> str:
    return f"{(value / Decimal(10000)).quantize(Decimal(1)):,.0f}"


def _manwon_with_unit(value: Decimal) -> str:
    if value == 0:
        return "0원"
    return f"{_manwon(value)}만원"


def _raw(value: Decimal) -> float:
    return float(value)


def _to_million(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float((value / Decimal(1_000_000)).quantize(Decimal("0.001")))


def _chart_range(values: list[float | None]) -> tuple[float, float]:
    real = [value for value in values if value is not None]
    if not real:
        return 0, 1
    low = min(real)
    high = max(real)
    padding = max((high - low) * 0.12, 1)
    return round(low - padding, 1), round(high + padding, 1)


def _ticks(start: float, end: float) -> list[float]:
    middle = round((start + end) / 2, 1)
    return [start, middle, end]


def _spread_labels(labels: list[str]) -> list[str]:
    if len(labels) <= 2:
        return labels
    visible = {0, len(labels) - 1}
    visible.update(range(6, len(labels) - 1, 7))
    return [label if index in visible else "" for index, label in enumerate(labels)]
