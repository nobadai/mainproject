"""재무 탭 — 값을 읽어오는 곳.

╔══════════════════════════════════════════════════════════════════════════╗
║  ★ 재무 파트가 채우는 파일입니다. `build()` 안쪽만 바꾸면 됩니다.          ║
║                                                                          ║
║  지금은 **예시값**입니다 (`Source.filled = False`) — 화면에 「예시값」      ║
║  딱지가 붙습니다. 실제 값으로 바꾸고 `filled=True` 로 두면 사라집니다.      ║
║                                                                          ║
║  읽을 곳: finance_states · daily_closings · receivables · payables ·      ║
║  expenses. DB 는 `app/finance/db.py` 를 그대로 쓰세요.                     ║
║                                                                          ║
║  ★ **조회만 하세요.** 이 탭은 쓰기가 없습니다. 상태 전이는 승인            ║
║  트랜잭션(`apply_approval`) 안에서만 일어납니다.                          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from datetime import date

from app.api.finance.schema import FinanceTab, FlowCell, StateOption
from app.api.primitives import Chart, Column, Marker, Note, Series, Source, Stat, Table

STATES = ("base", "loan")

_BASE = [58.1986, 54.0240, 54.0240, 49.8176, 49.8176, 45.6112, 45.6112, 41.5725, 41.5725,
         37.6040, 37.6040, 33.6559, 33.6559, 29.7453, 29.7453, 25.8269, 25.8269, 21.9085,
         21.9085, 17.9901, 17.9901, 14.2798, 14.2798, 10.5695, 10.5695, 6.8685, 6.8685,
         3.3253, 3.3253, -13.2782]
_LOAN = [103.4707, 99.2961, 99.2961, 95.0897, 95.0897, 90.8833, 90.8833, 86.8446, 86.8446,
         82.8761, 82.8761, 78.9280, 78.9280, 75.0174, 75.0174, 71.0990, 71.0990, 67.1806,
         67.1806, 63.2622, 63.2622, 59.5519, 59.5519, 55.8416, 55.8416, 52.1406, 52.1406,
         48.5974, 48.5974, 31.9939]
_MIN_CASH = 15.90264

#: 대시보드에 얹을 현금 잔고 (백만원). as_of 까지가 실적, 그 뒤는 추정.
_DASH_ACTUAL = [52.4, 52.1, 51.8, 51.8, 49.6, 49.6, 49.6, 47.9, 47.9]
_DASH_PROJ = [47.9, 40.6, 40.6, 35.7]
#: 지켜야 하는 현금 기준선 (백만원).
_DASH_FLOOR = 30.0

_STATE_TEXT = {
    "base": {
        "cash": "-1,328", "cash_tone": "bad", "cash_sub": "12/31 BASE_NO_LOAN 기준",
        "debt": "0", "debt_unit": "원", "debt_sub": "대출을 반영하지 않은 상태",
        "explain": ("대출을 반영하지 않은 12월 말 상태입니다. 현금 잔액이 -1,328만원으로 "
                    "내려가 최소 운영자금 1,590만원보다 낮습니다."),
    },
    "loan": {
        "cash": "3,199", "cash_tone": "good", "cash_sub": "12/31 LOAN_BASELINE 기준",
        "debt": "4,527", "debt_unit": "만원", "debt_sub": "DB에 저장된 대출 반영 상태",
        "explain": ("DB에 저장된 LOAN_BASELINE 상태입니다. 현금 잔액은 3,199만원이고 "
                    "부채 4,527만원이 함께 기록되어 있습니다. 이 값은 ‘대출 승인 결과’가 "
                    "아니라 저장된 재무 기준 상태입니다."),
    },
}


def build(as_of: date, state: str) -> FinanceTab:
    s = _STATE_TEXT[state]
    n = len(_BASE)

    return FinanceTab(
        states=[
            StateOption(key="base", label="대출 없이 운영 · BASE_NO_LOAN",
                        explain=_STATE_TEXT["base"]["explain"]),
            StateOption(key="loan", label="대출 반영 · LOAN_BASELINE",
                        explain=_STATE_TEXT["loan"]["explain"]),
        ],
        selected=state,
        stats=[
            Stat(label="현금 잔액", value=s["cash"], unit="만원", detail=s["cash_sub"],
                 tone=s["cash_tone"]),
            Stat(label="최소 운영자금", value="1,590", unit="만원",
                 detail="운영을 위해 지켜야 하는 현금 기준선", raw=1590),
            Stat(label="받을 돈", value="7,305", unit="만원",
                 detail="매출채권 15건 · 아직 수금 0원", tone="warn", raw=7305),
            Stat(label="부채", value=s["debt"], unit=s["debt_unit"], detail=s["debt_sub"]),
        ],
        explain=Note(tone="neutral", text=s["explain"]),
        read_only=Note(
            tone="info",
            text=("**이 화면은 조회 전용입니다.** 에이전트 추천이나 시나리오 결과를 "
                  "보여주지 않습니다. 저장된 값에서 «현금이 얼마 남았는지, 언제 나가고 "
                  "언제 들어오는지»를 바로 확인하는 데 집중합니다."),
        ),
        cash_chart=Chart(
            label="12월 일별 현금 잔액", y_min=-20, y_max=110,
            y_ticks=[-20, 0, 20, 40, 60, 80, 100], y_unit="M",
            series=[
                Series(name="대출 없이 운영", data=list(_BASE), tone="info", width=2.2),
                Series(name="대출 반영", data=list(_LOAN), tone="good", width=2.2),
                Series(name="최소 운영자금", data=[_MIN_CASH] * n, tone="bad",
                       width=1.3, dashed=True),
            ],
            note=Note(tone="neutral", text="daily_closings · 2025-12-02 ~ 2025-12-31"),
            # 30칸을 다 적으면 겹친다. 다섯 개만 적는다.
            x_labels=[
                {0: "12/02", 6: "12/08", 13: "12/15", 20: "12/22", 29: "12/31"}.get(i, "")
                for i in range(len(_BASE))
            ],
        ),
        flows=[
            FlowCell(label="상품 매입으로 나간 돈", value="5,628만원", term="purchase cash out"),
            FlowCell(label="물류로 나간 돈", value="296만원", term="logistics cash out"),
            FlowCell(label="급여 · 이자로 나간 돈", value="1,304만원", term="payroll + interest"),
            FlowCell(label="판매로 잡힌 금액", value="7,305만원", term="sales recognized",
                     tone="good"),
            FlowCell(label="실제로 들어온 수금", value="0원", term="collection cash in",
                     tone="warn"),
        ],
        balances=[
            Stat(label="받을 돈 총액", value="7,305", unit="만원",
                 detail="receivables 15건 · 모두 OPEN", tone="warn", raw=7305),
            Stat(label="이미 받은 돈", value="0", unit="원", detail="12월 말 기준 아직 회수 전",
                 raw=0),
            Stat(label="남은 매입대금", value="0", unit="원",
                 detail="payables 16건 모두 정산 완료", tone="good", raw=0),
            Stat(label="장부 재고가치", value="80", unit="만원",
                 detail="12/31 accounting inventory cost", raw=80),
        ],
        balances_note=Note(
            tone="good",
            text=("**현재 미지급 매입대금은 없습니다.** payables 16건은 모두 SETTLED 이고 "
                  "outstanding_amount 가 0원입니다. 그래서 경고 대신 «정산 완료» 로 "
                  "보이는 것이 더 명확합니다."),
        ),
        closings=Table(
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
                {"d": "2025-12-31", "buy": "337만원", "log": "20만원", "pay": "1,304만원",
                 "sale": "446만원", "col": "0원", "base": "-1,328만원", "loan": "3,199만원"},
                {"d": "2025-12-30", "buy": "0원", "log": "0원", "pay": "0원",
                 "sale": "0원", "col": "0원", "base": "333만원", "loan": "4,860만원"},
                {"d": "2025-12-29", "buy": "335만원", "log": "20만원", "pay": "0원",
                 "sale": "443만원", "col": "0원", "base": "333만원", "loan": "4,860만원"},
                {"d": "2025-12-28", "buy": "0원", "log": "0원", "pay": "0원",
                 "sale": "0원", "col": "0원", "base": "687만원", "loan": "5,214만원"},
                {"d": "2025-12-27", "buy": "350만원", "log": "20만원", "pay": "0원",
                 "sale": "463만원", "col": "0원", "base": "687만원", "loan": "5,214만원"},
            ],
        ),
        tables_read=["finance_states", "daily_closings", "receivables", "payables", "expenses"],
        source=Source(
            filled=False, owner="재무",
            note="app/api/finance/query.py 의 build() 를 채우면 실제 값이 됩니다",
        ),
    )


def dashboard_cash(n: int, at: int) -> Chart:
    """대시보드에 얹을 현금 그래프. **재무가 만듭니다** — 대시보드가 아닙니다."""
    tail = [None] * at + list(_DASH_PROJ) + [None] * max(0, n - at - len(_DASH_PROJ))
    return Chart(
        label="현금 잔고", y_min=25, y_max=60, y_ticks=[30, 40, 50, 60], y_unit="M",
        series=[
            Series(name="최소 운영현금", data=[_DASH_FLOOR] * n, tone="bad",
                   dashed=True, width=1, opacity=0.7),
            Series(name="실적", data=list(_DASH_ACTUAL) + [None] * (n - len(_DASH_ACTUAL)),
                   tone="warn", end_dot=True),
            Series(name="추정", data=tail[:n], tone="warn", dashed=True),
        ],
        markers=[Marker(index=at + 1, value=40.6, label="지급 -7.3M", tone="warn")],
        note=Note(
            tone="neutral",
            text="점선부터는 **아직 안 일어난 일**입니다. 승인한 매입의 지급 예정이 반영됩니다.",
        ),
    )
