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

import logging
from datetime import date
from typing import Any

from app.api import plan_state
from app.api.calendar import build_axis
from app.api.dashboard.schema import DashboardTab
from app.api.finance import query as finance_q
from app.api.forecast import query as forecast_q
from app.api.logistics import query as logistics_q
from app.api.primitives import Badge, Column, Note, Stat, Table
from app.api.purchase import query as purchase_q
from app.api.sales import query as sales_q
from app.api.shown_run import SHOWN_SIM_RUN_ID
from app.master.purchase_record_repository import RecordedTotals, recorded_totals_by_plan

log = logging.getLogger(__name__)

#: 재무 선택 상태 키 → 같은 화면 현금 그래프 계열 이름(`finance_q.dashboard_cash`).
#: ★ 「운영 여유」 칸과 그래프 선이 **같은 말**로 기준을 밝히게 한다. 모르는 키면
#:   재무 탭이 준 상태 이름(`fi.states[].label`)을 그대로 쓴다.
_BASIS = {"base": "대출 제외", "loan": "대출 포함"}

#: 🔴 **상태 어휘와 판정은 `app/api/plan_state.py` 가 소유한다** (2026-09-16).
#:
#:   매입 탭도 같은 낱말을 싣게 되면서 규칙이 두 화면의 것이 됐다. 두 벌로 짜면
#:   한쪽만 고치는 날 **같은 안이 화면마다 다른 상태**로 뜬다. 여기서는 그 이름을
#:   가져다 쓰기만 하고, 부르는 자리는 아래 `_state` 하나다.
PLAN_STATES = plan_state.PLAN_STATES

#: 모르는 값 한 글자. 재고 칸(`_현재고`)이 쓰는 것과 **같은 글자**다.
_UNKNOWN = "—"

#: 안 이름에서 품목·안 이름을 읽는 규칙도 같은 자리에서 온다.
_plan_item = plan_state.plan_item
_recorded = plan_state.recorded_for


def _pending(plans) -> int:
    return sum(1 for p in plans if p.pending)


def _state(plan: Any, recorded: RecordedTotals | None) -> str:
    """안의 상태를 사람 말로. **판정은 `plan_state.state_of` 가 한다.**"""
    return plan_state.state_of(approved=plan.approved, recorded=recorded)


def _records(as_of: date) -> dict[tuple[str, str], RecordedTotals]:
    """그날 · 보고 있는 실행의 **실매입 기록 합계**.

    🔴 **여기서 숫자를 만들지 않는다.** 표를 읽는 자리는 마스터 한 곳이고
       (`master/purchase_record_repository.recorded_totals_by_plan`) 이 함수는 그것을
       부르기만 한다. 같은 SELECT 를 화면 층에 한 벌 더 두면 PK 가 바뀌는 날 갈린다.

    ⚠️ 못 읽으면 **빈 표**다. 다섯 부서 탭이 저마다 DB 실패를 삼키고 「예시값」으로 뜨는
      것과 같은 태도 — 기록 하나 때문에 대시보드가 통째로 죽으면 안 된다.
    """
    try:
        return recorded_totals_by_plan(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
    except Exception as error:  # noqa: BLE001  DB 미연결 · 표 없음 둘 다
        log.info("실매입 기록을 못 읽어 안의 값을 그대로 보입니다: %s", error)
        return {}


def _unit_qty(plan: Any, recorded: RecordedTotals | None) -> str:
    """`단가 × 수량`. 🔴 기록이 있으면 **기록값**이다.

    ★ 이 표에는 상태 칸이 있고, 상태가 「매입 기록됨」이면 사람이 보는 값은 **실제로 산
      값**이어야 한다. 제안값은 지나간 값이고, 같은 화면의 「확정 매입액」이 이미 기록값으로
      서 있다 — 한 화면에서 두 숫자가 다른 사실을 말하면 안 된다.

    ⚠️ 단가가 정수로 안 떨어지면 「—」다. 반올림해 보이면 `단가 × 수량` 이 금액 칸과
      어긋나고, 그건 틀린 줄도 모르는 오류다.
    """
    if recorded is None:
        return f"{plan.unit_price:,} × {plan.qty_kg:,.0f}"
    unit = _UNKNOWN if recorded.unit_price is None else f"{recorded.unit_price:,}"
    return f"{unit} × {recorded.qty_kg:,.0f}"


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
    #
    #  🔵 **`window_days=0` — 도착일을 안 읽는다** (2026-09-16 · `#740` 의 인자).
    #     이 화면이 매입에서 읽는 것은 `pu.plans` · `pu.source` 둘뿐이다. 도착일
    #     조회(`arrivals`)가 먹이는 곳은 매입 탭의 확정 매입 표 하나이고 여기서는
    #     안 쓰는데, 그 왕복이 실측 `829.6ms` 로 이 화면의 단일 최대였다.
    #
    #  🔴 **좁히는 것이 아니라 안 읽는 것이다.** 그래서 `12` 가 아니라 `0` 이다.
    #     매입 탭 쪽은 그 사실을 「확정 입고 예정 —」 으로 말하는데, 이 화면은 그
    #     칸을 안 읽으므로 표시가 달라지지 않는다 —
    #     `tests/api/test_dashboard_purchase_window.py` 가 그 자리를 지킨다.
    pu = purchase_q.build(as_of, sim_run_id=SHOWN_SIM_RUN_ID, window_days=0)
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
    #  ★ 매입안과 **같은 실행 · 같은 날**의 실매입 기록. 열쇠는 `(품목, 안 이름)`.
    records = _records(as_of)

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
                    #  🔴 기록이 있으면 금액도 단가 × 수량도 **기록값**이다. 없으면
                    #     안의 값 그대로다 — 0 으로도 «—» 로도 바꾸지 않는다.
                    "unit_qty": _unit_qty(p, rec),
                    "amount": f"{(p.amount_krw if rec is None else rec.amount_krw):,}",
                    "state": _state(p, rec),
                }
                for p in pu.plans
                for item in [_plan_item(p.key)]
                for rec in [_recorded(records, p.key)]
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
