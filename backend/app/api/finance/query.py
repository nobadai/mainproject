"""재무 탭 — 저장된 재무 화면 값을 화면 부품으로 옮긴다."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.api.finance.schema import FinanceTab, FlowCell, StateOption
from app.api.primitives import Card, Chart, Column, Marker, Note, Series, Source, Stat, Table
from app.finance.dashboard import get_finance_cashflow, get_finance_dashboard
from app.finance.schemas import FinanceClosingItem, FinanceDashboardResponse, FinanceStateView
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

STATES = ("base", "loan")
_STATE_TO_MODE = {"base": "BASE_NO_LOAN", "loan": "LOAN_BASELINE"}
_STATE_LABELS = {
    "base": "대출 없이 운영",
    "loan": "대출 반영",
}

# 통합 대시보드에 얹을 현금 잔고 (백만원). 이 함수는 별도 화면에서 아직 사용한다.
_DASH_ACTUAL = [52.4, 52.1, 51.8, 51.8, 49.6, 49.6, 49.6, 47.9, 47.9]
_DASH_PROJ = [47.9, 40.6, 40.6, 35.7]
_DASH_FLOOR = 30.0


def build(as_of: date, state: str) -> FinanceTab:
    dash = get_finance_dashboard(sim_run_id=BURN_IN_SIM_RUN_ID, as_of=as_of)
    flow = get_finance_cashflow(sim_run_id=BURN_IN_SIM_RUN_ID, as_of=as_of, days=30)
    selected_key, selected = _select_state(dash, state)
    state_as_of = None if selected is None else selected.state_date
    latest_closing_as_of = max((row.close_date for row in dash.recent_closings), default=None)
    receivables = dash.ledger_summary["receivables"]
    payables = dash.ledger_summary["payables"]
    receivable_detail = (
        f"남은 받을 돈 {receivables.count}건 · "
        f"이미 받은 돈 {_won(receivables.received_amount_krw)}"
    )

    return FinanceTab(
        has_data=selected is not None,
        states=_state_options(dash),
        selected=selected_key,
        requested_as_of=as_of.isoformat(),
        state_as_of=None if state_as_of is None else state_as_of.isoformat(),
        latest_closing_as_of=None
        if latest_closing_as_of is None
        else latest_closing_as_of.isoformat(),
        stats=[] if selected is None else [
            Stat(
                label="운영 여유",
                value=_manwon(selected.operating_cash_buffer_krw),
                unit="만원",
                detail=_buffer_detail(selected.operating_cash_buffer_krw),
                tone=_buffer_tone(selected.operating_cash_buffer_krw),
                raw=_raw(selected.operating_cash_buffer_krw),
            ),
            Stat(
                label="현재 보유 현금",
                value=_manwon(selected.current_cash_krw),
                unit="만원",
                detail=_date_detail(as_of, state_as_of),
                tone="good" if selected.current_cash_krw >= 0 else "bad",
                raw=_raw(selected.current_cash_krw),
            ),
            Stat(
                label="아직 못 받은 판매대금",
                value=_manwon(receivables.outstanding_amount_krw),
                unit="만원",
                detail=receivable_detail,
                tone="warn" if receivables.outstanding_amount_krw > 0 else "good",
                raw=_raw(receivables.outstanding_amount_krw),
            ),
            Stat(
                label="현재 차입 잔액",
                value=_manwon(selected.current_debt_krw),
                unit="만원",
                detail="현재 남아 있는 차입금",
                tone="warn" if selected.current_debt_krw > 0 else "good",
                raw=_raw(selected.current_debt_krw),
            ),
        ],
        action_card=None if selected is None else _action_card(dash, selected),
        state_indicator=_state_indicator(dash.states),
        state_cards=_state_cards(dash.states) if len(dash.states) > 1 else [],
        explain=Note(tone="neutral", text=_state_explain(selected)),
        read_only=Note(
            tone="info",
            text=_freshness_note(as_of, state_as_of, latest_closing_as_of),
        ),
        cash_chart=None if selected is None or not flow.cashflow else _cash_chart(flow.cashflow),
        flows=[] if selected is None else _flows(dash.cashflow_summary),
        balances=[] if selected is None else [
            Stat(
                label="아직 받을 돈",
                value=_manwon(receivables.outstanding_amount_krw),
                unit="만원",
                detail=(
                    f"완료 {receivables.collected_count} · 일부 {receivables.partial_count} · "
                    f"예정 {receivables.open_count}"
                ),
                tone="warn" if receivables.outstanding_amount_krw > 0 else "good",
                raw=_raw(receivables.outstanding_amount_krw),
            ),
            Stat(
                label="이미 받은 돈",
                value=_manwon(receivables.received_amount_krw),
                unit="만원",
                detail="현재까지 받은 판매대금",
                raw=_raw(receivables.received_amount_krw),
            ),
            Stat(
                label="아직 지급할 매입대금",
                value=_manwon(payables.outstanding_amount_krw),
                unit="만원",
                detail=f"아직 지급하지 않은 매입대금 {payables.count}건",
                tone="warn" if payables.outstanding_amount_krw > 0 else "good",
                raw=_raw(payables.outstanding_amount_krw),
            ),
            Stat(
                label="판매대금 총액",
                value=_manwon(receivables.original_amount_krw),
                unit="만원",
                detail="확정된 판매대금 원금",
                raw=_raw(receivables.original_amount_krw),
            ),
        ],
        balances_note=None if selected is None else Note(
            tone="good" if payables.outstanding_amount_krw == 0 else "warn",
            text=(
                "**현재 미지급 매입대금은 없습니다.** 정산할 매입대금이 남아 있지 않습니다."
                if payables.outstanding_amount_krw == 0
                else f"남은 매입대금은 {_won(payables.outstanding_amount_krw)}입니다."
            ),
        ),
        closings=(
            None
            if selected is None or not dash.recent_closings
            else _closings_table(dash.recent_closings)
        ),
        source=Source(
            filled=True,
            owner="재무",
            note="근거 · 재무 마감 / 수금·지급 장부",
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


def _select_state(
    dash: FinanceDashboardResponse, state: str
) -> tuple[str, FinanceStateView | None]:
    selected = _state_by_mode(dash, _STATE_TO_MODE[state])
    if selected is not None:
        return state, selected
    for fallback_key in STATES:
        fallback = _state_by_mode(dash, _STATE_TO_MODE[fallback_key])
        if fallback is not None:
            return fallback_key, fallback
    if dash.states:
        first = dash.states[0]
        return _key_for_mode(first.financing_mode), first
    return "", None


def _state_options(dash: FinanceDashboardResponse) -> list[StateOption]:
    return [
        StateOption(
            key=_key_for_mode(state.financing_mode),
            label=_label_for_mode(state.financing_mode),
            explain=_state_explain(state),
        )
        for state in dash.states
    ]


def _state_by_mode(
    dash: FinanceDashboardResponse, financing_mode: str
) -> FinanceStateView | None:
    return next(
        (state for state in dash.states if state.financing_mode == financing_mode),
        None,
    )


def _state_explain(state: FinanceStateView | None) -> str:
    if state is None:
        return (
            "이 날짜에는 아직 재무 기록이 없습니다."
            " 재무 데이터가 저장된 이후 날짜를 선택해 주세요."
        )
    return _buffer_summary(state.operating_cash_buffer_krw)


def _state_indicator(states: list[FinanceStateView]) -> str | None:
    if len(states) != 1:
        return None
    return f"현재 재무 기준 · {_label_for_mode(states[0].financing_mode)}"


def _action_card(dash: FinanceDashboardResponse, state: FinanceStateView) -> Card:
    receivables = dash.ledger_summary["receivables"]
    payables = dash.ledger_summary["payables"]
    stats = [
        Stat(
            label="운영자금 부족" if state.operating_cash_buffer_krw < 0 else "운영자금 여유",
            value=_manwon(abs(state.operating_cash_buffer_krw)),
            unit="만원",
            detail=_buffer_detail(state.operating_cash_buffer_krw),
            tone=_buffer_tone(state.operating_cash_buffer_krw),
        ),
        Stat(
            label="연체 미수금",
            value=_manwon(receivables.overdue_amount_krw),
            unit="만원",
            detail="수금 예정일이 지난 판매대금",
            tone="bad" if receivables.overdue_amount_krw > 0 else "good",
        ),
        Stat(
            label="남은 매입대금",
            value=_manwon(payables.outstanding_amount_krw),
            unit="만원",
            detail="아직 지급하지 않은 매입대금",
            tone="warn" if payables.outstanding_amount_krw > 0 else "good",
        ),
    ]
    next_due = min(
        (item.due_date for item in dash.receivables if item.outstanding_amount_krw > 0),
        default=None,
    )
    if next_due is not None:
        stats.append(
            Stat(
                label="다음 수금 예정",
                value=next_due.isoformat(),
                detail="아직 받을 돈이 있는 가장 가까운 예정일",
                tone="info",
            )
        )
    return Card(
        key="actions",
        title="지금 확인할 자금",
        source_ref="재무 마감 · 수금·지급 장부",
        stats=stats,
    )


def _state_cards(states: list[FinanceStateView]) -> list[Card]:
    return [
        Card(
            key=_key_for_mode(state.financing_mode),
            title=_label_for_mode(state.financing_mode),
            subtitle=f"재무 상태 기준일 {state.state_date.isoformat()}",
            source_ref="재무 마감",
            lead=Note(
                tone=_buffer_tone(state.operating_cash_buffer_krw),
                text=_buffer_summary(state.operating_cash_buffer_krw),
            ),
            stats=[
                Stat(
                    label="현금 잔액",
                    value=_manwon(state.current_cash_krw),
                    unit="만원",
                    raw=_raw(state.current_cash_krw),
                ),
                Stat(
                    label="최소 운영자금",
                    value=_manwon(state.minimum_operating_cash_krw),
                    unit="만원",
                    raw=_raw(state.minimum_operating_cash_krw),
                ),
                Stat(
                    label="운영자금 여유",
                    value=_manwon(state.operating_cash_buffer_krw),
                    unit="만원",
                    tone=_buffer_tone(state.operating_cash_buffer_krw),
                    raw=_raw(state.operating_cash_buffer_krw),
                ),
                Stat(
                    label="현재 차입 잔액",
                    value=_manwon(state.current_debt_krw),
                    unit="만원",
                    tone="warn" if state.current_debt_krw > 0 else "good",
                    raw=_raw(state.current_debt_krw),
                ),
            ],
        )
        for state in states
    ]


def _key_for_mode(financing_mode: str) -> str:
    for key, mode in _STATE_TO_MODE.items():
        if mode == financing_mode:
            return key
    return financing_mode


def _label_for_mode(financing_mode: str) -> str:
    if financing_mode == "LOAN_BASELINE":
        return "대출 반영"
    if financing_mode == "BASE_NO_LOAN":
        return "대출 없이 운영"
    return "저장된 재무 상태"


def _buffer_tone(value: Decimal) -> str:
    if value > 0:
        return "good"
    if value == 0:
        return "warn"
    return "bad"


def _date_detail(requested: date, actual: date | None) -> str:
    if actual is None:
        return f"조회 기준일 {requested.isoformat()}"
    if requested == actual:
        return f"조회 기준일 {requested.isoformat()}"
    return f"조회 기준일 {requested.isoformat()} · 재무 상태 기준일 {actual.isoformat()}"


def _freshness_note(
    requested: date, state_as_of: date | None, closing_as_of: date | None
) -> str:
    if state_as_of is None or state_as_of == requested:
        parts = [f"기준일 · {requested.isoformat()}"]
    else:
        parts = [
            f"조회일 {requested.isoformat()}",
            f"선택한 날짜의 재무 상태가 없어 {state_as_of.isoformat()} 최신 재무 상태를 표시합니다",
        ]
    if closing_as_of is not None and closing_as_of not in {requested, state_as_of}:
        parts.append(f"최근 일마감 {closing_as_of.isoformat()}")
    parts.append("조회 전용")
    return " · ".join(parts)


def _cash_chart(rows: list[FinanceClosingItem]) -> Chart:
    base = [_to_manwon(row.base_cash_balance_krw) for row in rows]
    loan = [_to_manwon(row.loan_cash_balance_krw) for row in rows]
    minimum = [_to_manwon(row.minimum_operating_cash_krw) for row in rows]
    values = [*base, *loan, *minimum]
    y_min, y_max = _chart_range(values)
    return Chart(
        label="일별 현금 잔액",
        y_min=y_min,
        y_max=y_max,
        y_ticks=_ticks(y_min, y_max),
        y_unit="만원",
        series=[
            Series(name="현재 자금만 사용", data=base, tone="info", width=2.2),
            Series(name="대출 포함", data=loan, tone="good", width=2.2),
            Series(name="최소 유지해야 할 현금", data=minimum, tone="bad", width=1.3, dashed=True),
        ],
        note=Note(tone="neutral", text="현금이 최소 운영자금 아래로 내려가면 주의가 필요합니다."),
        x_labels=_spread_labels([f"{row.close_date.month}/{row.close_date.day}" for row in rows]),
    )


def _flows(summary) -> list[FlowCell]:
    return [
        FlowCell(
            label="상품 매입으로 나간 돈",
            value=_manwon_with_unit(summary.purchase_cash_out_krw),
            group="out",
        ),
        FlowCell(
            label="물류로 나간 돈",
            value=_manwon_with_unit(summary.logistics_cash_out_krw),
            group="out",
        ),
        FlowCell(
            label="급여 · 이자로 나간 돈",
            value=_manwon_with_unit(summary.payroll_interest_cash_out_krw),
            group="out",
        ),
        FlowCell(
            label="판매로 잡힌 금액",
            value=_manwon_with_unit(summary.sales_recognized_krw),
            tone="good",
            group="in",
        ),
        FlowCell(
            label="실제로 들어온 수금",
            value=_manwon_with_unit(summary.collection_cash_in_krw),
            tone="good" if summary.collection_cash_in_krw > 0 else "warn",
            group="in",
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
            Column(key="base", label="대출 없음 현금", align="right", mono=True),
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


def _to_manwon(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float((value / Decimal(10_000)).quantize(Decimal("0.1")))


def _buffer_summary(value: Decimal) -> str:
    amount = _manwon(abs(value))
    if value > 0:
        return f"최소 운영자금보다 {amount}만원 여유가 있습니다."
    if value < 0:
        return f"최소 운영자금보다 {amount}만원 부족합니다."
    return "현재 보유 현금이 최소 운영자금과 같습니다."


def _buffer_detail(value: Decimal) -> str:
    if value > 0:
        return "최소 운영자금보다 여유"
    if value < 0:
        return "최소 운영자금보다 부족"
    return "최소 운영자금과 같음"


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
