"""대시보드 — 다섯 부서 값을 날짜 축에 놓기만 한다.

╔══════════════════════════════════════════════════════════════════════════╗
║  ★ **여기서 숫자를 만들지 마세요.**                                        ║
║                                                                          ║
║  같은 값을 두 군데서 계산하면 언젠가 갈라집니다. 그러면 어느 쪽이 맞는지    ║
║  아무도 모릅니다. 그래서 이 파일은 다른 탭의 `build()` 를 불러 **골라       ║
║  담기만** 합니다.                                                         ║
║                                                                          ║
║  값이 없으면 **공란**으로 둡니다 — 0 으로 채우지 않습니다.                 ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from datetime import date

from app.api.calendar import build_axis
from app.api.dashboard.schema import DashboardTab
from app.api.finance import query as finance_q
from app.api.forecast import query as forecast_q
from app.api.logistics import query as logistics_q
from app.api.primitives import Badge, Column, Note, Stat, Table
from app.api.purchase import query as purchase_q
from app.api.sales import query as sales_q


def _pending(plans) -> int:
    return sum(1 for p in plans if p.pending)


def build(as_of: date) -> DashboardTab:
    axis = build_axis(as_of)
    n = len(axis.days)
    at = axis.as_of_index

    fc = forecast_q.build(as_of, "배추")
    pu = purchase_q.build(as_of)
    fi = finance_q.build(as_of, "base")
    lg = logistics_q.build(as_of, "stock")
    sl = sales_q.build(as_of)

    cabbage = next(c for c in fc.cards if c.item == "배추")
    pending = _pending(pu.plans)

    #  ★ 두 그래프는 **주인 부서가 만듭니다.** 여기서 만들면 같은 값을 두 군데서
    #    계산하게 되고, 실제로 갈라졌습니다 — 요약은 재고 4,550kg 인데 그래프
    #    끝은 14,600kg 이었습니다. 이제 둘 다 물류에서 나옵니다.
    cash = finance_q.dashboard_cash(n, at)
    stock = logistics_q.dashboard_stock(n, at)

    return DashboardTab(
        axis=axis,
        badges=[
            Badge(text="장 열림 · ML 배치 06:10", tone="good"),
            Badge(text="open_day 완료 · 전일 승계", tone="info"),
            Badge(text=(f"승인 대기 {pending}건" if pending else "오늘 승인 완료"),
                  tone=("warn" if pending else "good")),
        ],
        stats=[
            Stat(label=f"{cabbage.item} {cabbage.grade} · 내일 예측",
                 value=f"{cabbage.predicted:,}", unit="원/kg",
                 detail=f"구간 {cabbage.lower:,}–{cabbage.upper:,} · 폭 {cabbage.ci_width}",
                 tone="info", raw=cabbage.predicted),
            fi.stats[0],
            lg.panes[0].stats[0],
            Stat(label="매입 승인 대기", value=str(pending), unit="건",
                 detail=" · ".join(p.key for p in pu.plans) + " 두 안",
                 tone=("warn" if pending else "good"), raw=pending),
            sl.stats[0],
        ],
        forecast_cards=fc.cards,
        purchase=Table(
            columns=[
                Column(key="item", label="품목"),
                Column(key="ml", label="ML 특", align="right", mono=True),
                Column(key="plan", label="안"),
                Column(key="unit_qty", label="단가 × 수량", align="right", mono=True),
                Column(key="amount", label="금액", align="right", mono=True),
                Column(key="state", label="상태"),
            ],
            rows=[
                {
                    "item": "배추", "ml": f"{cabbage.predicted:,}", "plan": f"{p.key}안",
                    "unit_qty": f"{p.unit_price:,} × {p.qty_kg:,.0f}",
                    "amount": f"{p.amount_krw:,}",
                    "state": ("승인 대기" if p.pending else "후보"),
                }
                for p in pu.plans
            ],
            empty_text="오늘 낸 매입안이 없습니다",
        ),
        purchase_note=Note(
            tone="neutral",
            text=("상한가(이보다 비싸면 안 산다)는 안마다 다릅니다 — "
                  + " · ".join(f"{p.key} {p.max_price:,}" for p in pu.plans)
                  + " 원/kg. 매입 화면에서 근거와 함께 봅니다."),
        ),
        cash_chart=cash,
        stock_chart=stock,
        sources=[fc.source, pu.source, fi.source, lg.source, sl.source],
    )
