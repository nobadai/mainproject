"""대시보드 「오늘의 매입 제안」 — **승인된 안을 「후보」라고 쓰지 않는다** (2026-09-16).

🔴 **고치기 전 실측** (`dev@1df31f8` · 공용 DB · `SIM-CHECK-HOLIDAY-0916` · 2026-04-13)::

    GET /api/dashboard?as_of=2026-04-13  →  purchase.rows
      양파  610 × 6    3,660     "승인 대기"   ← 맞다 (아직 결정 없음)
      배추  574 × 491  281,834   "후보"        🔴 승인 + 실매입 기록 완료
      무    403 × 196   78,988   "후보"        🔴 승인 + 실매입 기록 완료

    같은 응답의 「이번 주 확정 매입액」 은 353,988 원인데 그 값은 실매입 기록값
    275,000(배추) + 78,988(무) 이다 — **한 화면 안에서 두 숫자가 다른 사실을 말했다.**

`pending` 이 거짓이라는 것은 「결정이 났다」는 뜻인데 화면에는 「후보」가 찍혔다. 사람이
읽으면 사실과 정반대다.

★ 여기서 재는 것 넷::

    ① 결정 없는 안            「후보」
    ② 승인만 된 안            「승인됨」
    ③ 승인 + 실매입 기록       「매입 기록됨」 이고 **금액이 기록값**
    ④ 그때 `단가 × 수량` 도 기록값이고 **단가가 정수**
    ⑤ 기록이 없는 안은 금액이 **제안값 그대로**

🔴 **실 DB 에 안 닿는다.** 대시보드가 부르는 다섯 탭 `build()` · 두 그래프 · 실매입 기록
   읽기를 전부 대역으로 세우고, 「받은 값을 어떻게 읽어 어떤 낱말로 쓰는가」만 본다.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.api.dashboard import query as dashboard_query
from app.api.forecast.schema import ItemCard
from app.api.primitives import Chart, Source, Stat
from app.contracts.core import ITEMS
from app.master.purchase_record_repository import RecordedTotals

AS_OF = date(2026, 4, 13)

#: 배추 카드는 대시보드 첫 칸이 쓰므로 늘 둔다.
CABBAGE = "배추"
#: 실측이 잡은 두 품목 그대로 — 셋 다 계약 품목이다.
RADISH = next(i for i in ITEMS if i not in {CABBAGE})
ONION = next(i for i in ITEMS if i not in {CABBAGE, RADISH})

#: 실측값. 배추는 **제안 281,834 · 기록 275,000** 으로 갈라 둔다 — 화면이 어느 쪽을
#: 집는지 값으로 갈리게 하려는 것이다 (같은 값이면 집는 자리를 못 잰다).
제안_수량, 제안_단가 = 574.0, 491
제안_금액 = 281_834
기록_수량, 기록_단가 = 500.0, 550
기록_금액 = 275_000


def _card(item: str) -> ItemCard:
    return ItemCard(item=item, grade="특", target_date="2026-04-15", predicted=1_000,
                    lower=990, upper=1_010, ci_width=0.1, review=False,
                    use_recommended=True)


def _plan(item: str, label: str, *, approved: bool) -> SimpleNamespace:
    """매입 탭이 주는 안 하나. `key` 형식은 매입이 정한 것이다 (`f"{item} · {label}"`)."""
    return SimpleNamespace(
        key=f"{item} · {label}", unit_price=제안_단가, qty_kg=제안_수량,
        amount_krw=제안_금액, max_price=제안_단가 + 100,
        pending=not approved, approved=approved,
    )


def _chart() -> Chart:
    return Chart(label="대역", y_min=0, y_max=1, y_ticks=[0, 1], series=[])


def _source(owner: str) -> Source:
    return Source(filled=True, owner=owner)


@pytest.fixture
def screen(monkeypatch):
    """대시보드가 부르는 것을 전부 대역으로 세운다. `plans` · `records` 는 검사가 채운다."""
    state = {
        "plans": [],
        "records": {},
    }

    monkeypatch.setattr(
        dashboard_query.forecast_q, "build",
        lambda as_of, item: SimpleNamespace(
            cards=[_card(i) for i in (CABBAGE, RADISH, ONION)], source=_source("ML")),
    )
    monkeypatch.setattr(
        dashboard_query.purchase_q, "build",
        lambda as_of, sim_run_id=None: SimpleNamespace(
            plans=state["plans"], source=_source("매입")),
    )
    monkeypatch.setattr(
        dashboard_query.finance_q, "build",
        lambda as_of, s: SimpleNamespace(
            stats=[Stat(label="운영 여유", value="1", raw=1)],
            states=[SimpleNamespace(key="base", label="대출 제외")],
            selected="base", source=_source("재무")),
    )
    monkeypatch.setattr(
        dashboard_query.logistics_q, "build",
        lambda as_of, pane: SimpleNamespace(
            panes=[SimpleNamespace(key="stock", stats=[Stat(label="재고", value="1", raw=1)])],
            source=_source("물류")),
    )
    monkeypatch.setattr(
        dashboard_query.sales_q, "build",
        lambda as_of: SimpleNamespace(stats=[Stat(label="판매", value="1", raw=1)],
                                      source=_source("판매")),
    )
    monkeypatch.setattr(dashboard_query.finance_q, "dashboard_cash", lambda axis: _chart())
    monkeypatch.setattr(dashboard_query.logistics_q, "dashboard_stock",
                        lambda n, at, as_of: _chart())
    #  🔴 실매입 기록도 대역이다 — 표가 아니라 **읽어 온 값을 어떻게 쓰는가**를 잰다.
    monkeypatch.setattr(dashboard_query, "recorded_totals_by_plan",
                        lambda **_: state["records"])
    return state


def _row(screen, item: str, label: str = "기본", *, approved: bool, recorded: bool):
    screen["plans"] = [_plan(item, label, approved=approved)]
    screen["records"] = (
        {(item, label): RecordedTotals(qty_kg=기록_수량, amount_krw=기록_금액,
                                       unit_price=기록_단가)}
        if recorded
        else {}
    )
    return dashboard_query.build(AS_OF).purchase.rows[0]


# ══════════════════════════════════════════════════════════════════════════
#  ① ~ ③  상태 어휘가 실제 상태를 가린다
# ══════════════════════════════════════════════════════════════════════════

def test_결정이_없으면_후보다(screen):
    """양파 줄이 이랬다 — 이 줄만 고치기 전에도 맞았다."""
    assert _row(screen, ONION, approved=False, recorded=False)["state"] == "후보"


def test_승인만_된_안은_승인됨이다(screen):
    """🔴 고치기 전 이 자리에 「후보」가 찍혔다 — **결정이 났는데 안 났다고 말했다.**"""
    row = _row(screen, RADISH, approved=True, recorded=False)
    assert row["state"] == "승인됨"
    assert row["state"] != "후보", "결정이 난 안을 「후보」라고 쓰면 사실과 정반대다"


def test_승인하고_실매입까지_기록했으면_매입_기록됨이고_금액이_기록값이다(screen):
    """③ — 상태가 「매입 기록됨」이면 사람이 보는 값은 **실제로 산 값**이어야 한다.

    같은 화면의 「확정 매입액」이 이미 기록값으로 서 있다 (실측 353,988 = 275,000 + 78,988).
    """
    row = _row(screen, CABBAGE, approved=True, recorded=True)
    assert row["state"] == "매입 기록됨"
    assert row["amount"] == f"{기록_금액:,}"
    assert row["amount"] != f"{제안_금액:,}", "제안값은 지나간 값이다"


def test_승인됨과_매입_기록됨은_다른_낱말이다(screen):
    """둘 다 `approved` 가 참이다 — 참/거짓 하나로는 **못 가른다.**"""
    approved = _row(screen, RADISH, approved=True, recorded=False)["state"]
    recorded = _row(screen, CABBAGE, approved=True, recorded=True)["state"]
    assert approved != recorded


# ══════════════════════════════════════════════════════════════════════════
#  ④  단가 × 수량도 기록값이다
# ══════════════════════════════════════════════════════════════════════════

def test_기록이_있으면_단가_곱하기_수량도_기록값이고_단가가_정수다(screen):
    """🔴 **금액 칸과 같은 사실이어야 한다** — 한 줄 안에서 어긋나면 더 나쁘다."""
    row = _row(screen, CABBAGE, approved=True, recorded=True)

    unit_text, qty_text = (part.strip() for part in row["unit_qty"].split("×"))
    unit = int(unit_text.replace(",", ""))
    qty = float(qty_text.replace(",", ""))

    assert unit_text == f"{기록_단가:,}", "단가가 기록값이어야 한다"
    assert qty == 기록_수량, "수량도 기록값이어야 한다"
    assert unit * qty == 기록_금액, "단가 × 수량 이 금액 칸과 같아야 한다"
    assert float(unit).is_integer()


def test_단가가_정수로_안_떨어지면_지어내지_않는다(screen):
    """⚠️ 반올림해 보이면 `단가 × 수량` 이 금액 칸과 어긋난다 — 모르면 모른다고 쓴다."""
    screen["plans"] = [_plan(CABBAGE, "기본", approved=True)]
    screen["records"] = {
        (CABBAGE, "기본"): RecordedTotals(qty_kg=300.0, amount_krw=271_000, unit_price=None)
    }

    row = dashboard_query.build(AS_OF).purchase.rows[0]

    assert row["unit_qty"].startswith("—"), row["unit_qty"]
    assert row["amount"] == "271,000", "금액은 사람이 실제로 낸 돈 그대로다"


# ══════════════════════════════════════════════════════════════════════════
#  ⑤  기록이 없으면 안의 값 그대로다
# ══════════════════════════════════════════════════════════════════════════

def test_기록이_없는_안은_금액이_제안값_그대로다(screen):
    """★ 승인만 된 안까지 기록값으로 바꾸면 **없는 사실을 만드는 것**이다."""
    row = _row(screen, RADISH, approved=True, recorded=False)
    assert row["amount"] == f"{제안_금액:,}"
    assert row["unit_qty"] == f"{제안_단가:,} × {제안_수량:,.0f}"


def test_다른_안의_기록을_이_안에_붙이지_않는다(screen):
    """열쇠는 `(품목, 안 이름)` 이다. 품목만 맞는다고 붙이면 **엉뚱한 값**이 선다."""
    screen["plans"] = [_plan(CABBAGE, "보수", approved=True)]
    screen["records"] = {
        (CABBAGE, "공격"): RecordedTotals(qty_kg=기록_수량, amount_krw=기록_금액,
                                          unit_price=기록_단가)
    }

    row = dashboard_query.build(AS_OF).purchase.rows[0]

    assert row["state"] == "승인됨"
    assert row["amount"] == f"{제안_금액:,}"


# ══════════════════════════════════════════════════════════════════════════
#  어휘를 닫아 둔다
# ══════════════════════════════════════════════════════════════════════════

def test_화면에_상태_코드를_쓰지_않는다(screen):
    """🔴 `APPROVED` · `AWAITING_PURCHASE_RECORD` 는 API 안쪽 어휘다 — 사람 말만 쓴다."""
    screen["plans"] = [
        _plan(ONION, "기본", approved=False),
        _plan(RADISH, "기본", approved=True),
        _plan(CABBAGE, "기본", approved=True),
    ]
    screen["records"] = {
        (CABBAGE, "기본"): RecordedTotals(qty_kg=기록_수량, amount_krw=기록_금액,
                                          unit_price=기록_단가)
    }

    states = [r["state"] for r in dashboard_query.build(AS_OF).purchase.rows]

    assert states == ["후보", "승인됨", "매입 기록됨"]
    assert set(states) <= set(dashboard_query.PLAN_STATES)


def test_상태_어휘는_네_낱말이다():
    """낱말을 늘리면 같은 사실을 화면마다 다른 이름으로 부르게 된다."""
    assert dashboard_query.PLAN_STATES == ("후보", "승인됨", "매입 기록됨", "반려")
