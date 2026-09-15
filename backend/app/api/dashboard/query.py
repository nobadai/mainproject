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
from typing import Any

from app.api.calendar import build_axis
from app.api.dashboard.schema import DashboardTab
from app.api.finance import query as finance_q
from app.api.forecast import query as forecast_q
from app.api.logistics import query as logistics_q
from app.api.primitives import Badge, Column, Note, Stat, Table
from app.api.purchase import query as purchase_q
from app.api.sales import query as sales_q
from app.api.shown_run import SHOWN_SIM_RUN_ID
from app.contracts.core import ITEMS

#: 재무 선택 상태 키 → 같은 화면 현금 그래프 계열 이름(`finance_q.dashboard_cash`).
#: ★ 「운영 여유」 칸과 그래프 선이 **같은 말**로 기준을 밝히게 한다. 모르는 키면
#:   재무 탭이 준 상태 이름(`fi.states[].label`)을 그대로 쓴다.
_BASIS = {"base": "대출 제외", "loan": "대출 포함"}


def _pending(plans) -> int:
    return sum(1 for p in plans if p.pending)


def _plan_item(key: str) -> str | None:
    """안의 품목. 🔴 매입 `Plan` 스키마에 품목 칸이 없다 (2026-09-14 확인).

    매입 `_plan()` 이 `key=f"{item} · {label}"` 로 품목을 이름 앞에 넣는다. 그 앞자리를
    **계약 품목(`ITEMS`)과 맞춰** 읽는다 — 이름을 코드에 박지 않고, 계약 밖이면 공란.
    매입 스키마에 품목 칸이 서는 날 이 함수를 그 칸 읽기로 바꾼다.
    """
    return next((item for item in ITEMS if key.startswith(f"{item} · ")), None)


def _buffer_stat(fi):
    """재무 「운영 여유」 에 **어느 기준인가** 를 붙인다. 값은 건드리지 않는다."""
    if not fi.stats:
        return None
    stat = fi.stats[0]
    label = next((s.label for s in fi.states if s.key == fi.selected), None)
    basis = _BASIS.get(fi.selected, label)
    if not basis:
        return stat
    return stat.model_copy(update={"label": f"{stat.label} · {basis}"})


def _현재고(lg: Any) -> Stat:
    """물류 탭에서 **현재고 한 칸**만 꺼낸다.

    🔴 **자리로 집지 않는다** (종전 `panes[0].stats[0]`). #675 에서 물류 탭 맨 앞이
       「한눈에 보기」로 바뀌자 대시보드가 **문제 건수를 재고라고** 내보냈다 — 자리는
       화면 사정으로 움직이고 열쇠는 안 움직인다.
    """
    재고칸 = next((p for p in lg.panes if p.key == "stock"), None)
    if 재고칸 is not None and 재고칸.stats:
        return 재고칸.stats[0]
    #  ★ 못 찾으면 «모른다» 로 낸다 — 다른 칸을 재고인 척 올리지 않는다.
    return Stat(label="현재고 합계", value="—", detail="물류 탭에서 못 읽었습니다",
                tone="warn", raw=None)


def build(as_of: date) -> DashboardTab:
    axis = build_axis(as_of)
    n = len(axis.days)
    at = axis.as_of_index

    fc = forecast_q.build(as_of, "배추")
    #  ★ 매입은 축을 안 주면 모든 실행을 섞는다. 다른 네 탭과 같은 실행을 넘긴다
    #    (`app/api/shown_run.py` 한 자리).
    pu = purchase_q.build(as_of, sim_run_id=SHOWN_SIM_RUN_ID)
    fi = finance_q.build(as_of, "base")
    lg = logistics_q.build(as_of, "stock")
    sl = sales_q.build(as_of)

    cabbage = next(c for c in fc.cards if c.item == "배추")
    cards = {c.item: c for c in fc.cards}
    pending = _pending(pu.plans)
    today = axis.days[at] if 0 <= at < n else None

    #  ★ 두 그래프는 **주인 부서가 만듭니다.** 여기서 만들면 같은 값을 두 군데서
    #    계산하게 되고, 실제로 갈라졌습니다 — 요약은 재고 4,550kg 인데 그래프
    #    끝은 14,600kg 이었습니다. 이제 둘 다 물류에서 나옵니다.
    cash = finance_q.dashboard_cash(axis)
    stock = logistics_q.dashboard_stock(n, at, as_of)

    return DashboardTab(
        axis=axis,
        badges=[
            #  ★ 날짜축이 가진 사실만 적는다. 배치 시각·개장 처리 결과는 이 응답에
            #    없으므로 적지 않는다 (예전 고정 문구를 뺐다 · 2026-09-14).
            *([] if today is None else [
                Badge(text=("장 열림" if today.market_open else "휴장"),
                      tone=("good" if today.market_open else "neutral")),
            ]),
            Badge(text=(f"승인 대기 {pending}건" if pending else "오늘 승인 완료"),
                  tone=("warn" if pending else "good")),
        ],
        stats=[
            #  ★ «내일» 이라고 쓰지 않는다. 리드타임 1~2 는 모델이 아니라
            #    어제값이 나가므로, 카드는 **모델이 낸 첫 날**을 싣는다.
            Stat(label=f"{cabbage.item} {cabbage.grade} · {cabbage.target_date[5:]} 예측",
                 value=f"{cabbage.predicted:,}", unit="원/kg",
                 detail=f"구간 {cabbage.lower:,}–{cabbage.upper:,} · 폭 {cabbage.ci_width}",
                 tone="info", raw=cabbage.predicted),
            *([s] if (s := _buffer_stat(fi)) is not None else []),
            _현재고(lg),
            Stat(label="매입 승인 대기", value=str(pending), unit="건",
                 detail=(", ".join(p.key for p in pu.plans) + f" · {len(pu.plans)}안"
                         if pu.plans else "오늘 낸 안 없음"),
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
                    "item": item,
                    "ml": (None if (card := cards.get(item)) is None
                           else f"{card.predicted:,}"),
                    "plan": f"{p.key}안",
                    "unit_qty": f"{p.unit_price:,} × {p.qty_kg:,.0f}",
                    "amount": f"{p.amount_krw:,}",
                    "state": ("승인 대기" if p.pending else "후보"),
                }
                for p in pu.plans
                for item in [_plan_item(p.key)]
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
