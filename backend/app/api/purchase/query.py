"""매입 탭 — 값을 읽어오는 곳.

╔══════════════════════════════════════════════════════════════════════════╗
║  ★ 매입 파트가 채우는 파일입니다. 아래 `build()` 안쪽만 바꾸면 됩니다.      ║
║                                                                          ║
║  지금은 **예시값**입니다 (`Source.filled = False`). 그래서 화면에          ║
║  「예시값」 딱지가 붙습니다. 실제 값으로 바꾸고 `filled=True` 로 두면       ║
║  딱지가 사라집니다.                                                       ║
║                                                                          ║
║  화면은 안 고쳐도 됩니다 — 이 함수가 돌려주는 **모양**만 지키면 됩니다     ║
║  (`schema.py`).                                                          ║
║                                                                          ║
║  읽을 곳: `app/master/*`(승인·확정) · `app/purchase_agent/*`(제안)         ║
║  DB 는 `app/finance/db.py` 처럼 부서 것을 쓰세요.                          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from datetime import date

from app.api.primitives import Column, Note, Source, Stat, Table
from app.api.purchase.schema import Plan, PurchaseTab, Reason

_LEG_COLS = [
    Column(key="leg", label="회차"),
    Column(key="buy", label="사는 날", mono=True),
    Column(key="qty", label="수량", align="right", mono=True),
    Column(key="arrive", label="도착", mono=True),
]
_PAY_COLS = [
    Column(key="leg", label="회차"),
    Column(key="buy", label="사는 날", mono=True),
    Column(key="pay", label="내는 날", mono=True),
    Column(key="amount", label="금액", align="right", mono=True),
]

#: 근거 여섯 줄은 두 안이 같다 — 같은 하루를 보고 만든 안이라 그렇다.
_REASONS = [
    Reason(source="예측", text="D+14 예측 −2.8%, 신뢰구간 폭 63.9%", ref="FC-2026-01-06"),
    Reason(source="시세", text="가락 2026-01-05 경락가 925원/kg 등 1개 등급", ref="MQ-가락-0105"),
    Reason(source="주문", text="확정주문 10,042.2kg → 일평균 717kg", ref="SO-2026-01-06"),
    Reason(source="재고", text="가용 286.92kg (로트 LOT-KIMCHI-015)", ref="INV-0106"),
    Reason(source="현금", text="재무 매입 상한 13,057,049원까지 매입 가능", ref="CASH-0106"),
    Reason(source="창고", text="날짜별 입고 여유 4,059kg — 어제 승인분 3,587kg 이 01-07 에 옵니다",
           ref="CAP-0106", carried=True),
]

_RISKS = [
    "기존 로트 LOT-KIMCHI-015 잔여신선도 4일 — 새로 사는 물량이 이 로트를 밀어내지 않는지",
    "기준등급 ‘상’이 당일 시세에 없어 ‘특’(854원/kg)으로 배정했다",
    "문서 1종을 찾았으나 참조 가능한 발간물 0건 — 문서 근거 없이 구성된 안이다",
]


def _plan(key: str, coverage: str, qty: float, amount: int, cap: int, *, pending: bool) -> Plan:
    return Plan(
        key=key, coverage=coverage, knob="수량으로 조절",
        qty_kg=qty, amount_krw=amount, unit_price=854, grade="특", max_price=cap,
        legs=Table(columns=_LEG_COLS, rows=[
            {"leg": 1, "buy": "01-06", "qty": f"{qty:,.0f} kg", "arrive": "01-08"},
        ]),
        payments=Table(columns=_PAY_COLS, rows=[
            {"leg": 1, "buy": "01-06", "pay": "01-13", "amount": f"{amount:,} 원"},
        ]),
        reasons=list(_REASONS), risks=list(_RISKS), pending=pending,
    )


def build(as_of: date) -> PurchaseTab:
    plans = [
        _plan("보수", "2일치", 1435, 1_225_745, 992, pending=False),
        _plan("기본", "5일치", 3587, 3_063_298, 1_007, pending=True),
    ]
    return PurchaseTab(
        stats=[
            Stat(label="오늘 제안", value="2", unit="안", detail="보수 · 기본 · 차단 0건", raw=2),
            Stat(label="승인 대기", value="1", unit="건", detail="사람이 고르면 H1 확정",
                 tone="warn", raw=1),
            Stat(label="이번 주 확정 매입액", value="3,063,298", unit="원",
                 detail="H1 commitment 합계", tone="warn", raw=3_063_298),
            Stat(label="확정 입고 예정", value="3,587", unit="kg",
                 detail="2026-01-07 도착 · 날짜별 여유 4,059kg", tone="good", raw=3587),
        ],
        plans=plans,
        plans_note=Note(
            tone="neutral",
            text=(
                "**공격안이 왜 없나** — 예측 구간이 넓은 날은 길게 사는 안을 만들지 "
                "않습니다. 실데이터 504조합을 전부 재봤는데 구간이 좁은 날이 한 번도 "
                "없어서, 지금은 늘 두 안입니다. 구간이 좁아지면 세 안이 됩니다."
            ),
        ),
        committed=Table(
            columns=[
                Column(key="approval", label="승인", mono=True),
                Column(key="leg", label="회차"),
                Column(key="buy", label="사는 날", mono=True),
                Column(key="arrive", label="도착", mono=True),
                Column(key="qty", label="수량", align="right", mono=True),
                Column(key="unit", label="단가", align="right", mono=True),
                Column(key="amount", label="금액", align="right", mono=True),
                Column(key="pay", label="지급", mono=True),
            ],
            rows=[{
                "approval": "H1-THRU-20260105", "leg": 1, "buy": "01-05", "arrive": "01-07",
                "qty": "3,587 kg", "unit": "854", "amount": "3,063,298", "pay": "01-12",
            }],
            empty_text="아직 확정된 매입이 없습니다 — 위에서 안을 고르면 여기에 생깁니다",
        ),
        committed_note=Note(
            tone="neutral",
            text=(
                "승인 전에는 이 표가 비어 있습니다. 위에서 내는 것은 후보이고, "
                "사람이 골라야 확정이 됩니다."
            ),
        ),
        source=Source(
            filled=False, owner="매입",
            note="app/api/purchase/query.py 의 build() 를 채우면 실제 값이 됩니다",
        ),
    )
