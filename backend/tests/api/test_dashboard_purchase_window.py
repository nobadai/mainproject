"""대시보드가 매입 탭에 **도착일을 안 읽게** 한다 — 그러고도 화면이 그대로다 (2026-09-16).

무엇을 하는가
────────────────────────────────────────────────────────────────────────────
대시보드는 매입 탭의 `build()` 를 통째로 재사용하는데, 거기서 실제로 읽는 것은
**`pu.plans` 와 `pu.source` 둘뿐**이다. 도착일 조회(`arrivals`)가 먹이는 곳은 매입 탭의
확정 매입 표 하나이고, 그 왕복이 실측 `829.6ms` 로 이 화면의 단일 최대였다.

그래서 `window_days=0` 을 넘긴다 — **좁히는 것이 아니라 안 읽는 것**이다 (`#740` 의 인자).

무엇을 재는가
────────────────────────────────────────────────────────────────────────────
.. code-block:: text

    ① 창을 0 으로 두면 매입 탭 자신은 **실제로 달라진다**
       (안 그러면 아래 ②가 아무것도 안 재는 검사가 된다 — 규칙 8)
    ② 그런데 대시보드의 통계 · 배지 · 표 · 글은 **한 글자도 안 달라진다**

🔴 **①이 없으면 ②는 빈 검사다.** 창 인자를 지워도 통과하기 때문이다. 그래서 먼저
   «달라진다» 를 세우고, 그 위에서 «그래도 대시보드는 같다» 를 잰다.

🔴 **실 DB 에 안 닿는다.** 매입의 `_read` 와 나머지 네 탭 · 두 그래프 · 실매입 기록을
   전부 대역으로 세운다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.dashboard import query as dashboard_query
from app.api.forecast.schema import ItemCard
from app.api.primitives import Chart, Source, Stat
from app.api.purchase import query as purchase_query
from app.contracts.core import ITEMS

AS_OF = date(2026, 4, 13)
AXIS = "SIM-CHECK-HOLIDAY-0916"
ITEM = "배추"
#: 🔵 `as_of` 당일이라 창 `1` 이면 들어오고 `0` 이면 빠진다 — 그 경계가 ①을 세운다.
BUY_DATE = AS_OF
#: 도착일이 `as_of` 뒤라 「확정 입고 예정」에 실제로 잡힌다.
ARRIVE = "2026-04-15"
AMOUNT = 281_834


def _scenario() -> dict[str, Any]:
    return {
        "label": "기본",
        "coverage_days": 2,
        "strategy_type": "quantity",
        "total_qty_kg": 574,
        "total_amount_krw": AMOUNT,
        "max_price": 1_000,
        "cut_unit_price": 950,
        "sourcing_plan": [{"grade": "특", "grade_unit_price": 491, "qty_kg": 574}],
        "split_plan": [{"buy_date": BUY_DATE.isoformat(), "qty_kg": 574,
                        "expected_arrival_date": ARRIVE}],
        "rationale": [],
        "risks": [],
    }


def _data() -> dict[str, Any]:
    return {
        "runs": [{
            "run_id": "0f2a1c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b",
            "request_id": "REQ-A",
            "item": ITEM,
            "end_code": "E1_APPROVED",
            "runtime_status": "READY",
            "created_at": datetime(2026, 4, 13, 1, 0, tzinfo=UTC),
            "sim_run_id": AXIS,
            "payload": {"scenarios": [_scenario()]},
        }],
        "buys": [{
            "purchase_id": "PUR-A",
            "purchase_date": BUY_DATE,
            "payment_due_date": BUY_DATE,
            "settlement_status": "UNSETTLED",
            "sim_run_id": AXIS,
            "item_id": "ITEM-BAECHU",
            "grade": "특",
            "quantity_kg": Decimal(574),
            "unit_price_krw_per_kg": Decimal(491),
            "line_amount_krw": Decimal(AMOUNT),
        }],
        "decisions": [],
        "items": {"ITEM-BAECHU": ITEM},
        "arrivals": [{
            "as_of": BUY_DATE,
            "item": ITEM,
            "sim_run_id": AXIS,
            "scenarios": [_scenario()],
        }],
    }


def _read(_as_of: date, *, window_days: int | None = None) -> dict[str, Any]:
    """`_read` 대역. **창 규칙은 진짜와 같은 자리에서 돈다.**

    ★ 날짜 고르기를 여기서 흉내 내는 대신 진짜와 같은 식을 쓴다 — 식이 갈리면 이 검사가
      실제와 다른 세상을 재게 된다.
    """
    data = _data()
    all_dates = sorted({row["purchase_date"] for row in data["buys"]})
    dates = all_dates if window_days is None else [
        d for d in all_dates if 0 <= (AS_OF - d).days < window_days
    ]
    data["arrivals"] = [r for r in data["arrivals"] if r["as_of"] in dates]
    data["arrivals_complete"] = dates == all_dates
    return data


def _card(item: str) -> ItemCard:
    return ItemCard(item=item, grade="특", target_date="2026-04-15", predicted=1_000,
                    lower=990, upper=1_010, ci_width=0.1, review=False,
                    use_recommended=True)


def _chart() -> Chart:
    return Chart(label="대역", y_min=0, y_max=1, y_ticks=[0, 1], series=[])


def _source(owner: str) -> Source:
    return Source(filled=True, owner=owner)


@pytest.fixture
def screen(monkeypatch):
    """매입만 **진짜**로 돌리고 나머지는 대역. 매입의 DB 자리만 갈아 끼운다."""
    monkeypatch.setattr(purchase_query, "_read", _read)
    monkeypatch.setattr(purchase_query, "recorded_totals_by_plan", lambda **_: {})
    monkeypatch.setattr(dashboard_query, "recorded_totals_by_plan", lambda **_: {})
    monkeypatch.setattr(
        dashboard_query.forecast_q, "build",
        lambda as_of, item: SimpleNamespace(
            cards=[_card(i) for i in ITEMS], source=_source("ML")),
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
    return monkeypatch


def _inbound(tab) -> Stat:
    return next(s for s in tab.stats if s.label == "확정 입고 예정")


# ══════════════════════════════════════════════════════════════════════════
#  ①  매입 탭 자신은 실제로 달라진다 — 그래야 ②가 무엇을 재는 검사가 된다
# ══════════════════════════════════════════════════════════════════════════

def test_창을_0_으로_두면_매입_탭이_실제로_달라진다(screen):
    """🔴 이것이 안 달라지면 아래 «대시보드가 같다» 는 **아무것도 안 재는 검사**다."""
    전부 = purchase_query.build(AS_OF, AXIS)
    창0 = purchase_query.build(AS_OF, AXIS, window_days=0)

    assert _inbound(전부).value != _inbound(창0).value
    assert 전부.committed_note.text != 창0.committed_note.text


def test_창을_0_으로_두면_입고_예정을_0_이_아니라_미결로_낸다(screen):
    """★ 「확정된 0」 과 「안 읽었다」 는 다른 사실이다 (`#740` 이 세운 규율)."""
    칸 = _inbound(purchase_query.build(AS_OF, AXIS, window_days=0))

    assert 칸.value == "—"
    assert 칸.raw is None


# ══════════════════════════════════════════════════════════════════════════
#  ②  그런데 대시보드는 한 글자도 안 달라진다
# ══════════════════════════════════════════════════════════════════════════

def _dashboard_with(screen, window_days: int | None):
    """대시보드가 매입에 넘기는 창을 **강제로** 바꿔 세운다."""
    진짜 = purchase_query.build
    screen.setattr(
        dashboard_query.purchase_q, "build",
        lambda as_of, sim_run_id=None, **_: 진짜(
            as_of, sim_run_id, window_days=window_days),
    )
    return dashboard_query.build(AS_OF)


def test_창을_0_으로_둬도_대시보드_응답이_그대로다(screen):
    """🔴 대시보드가 그 칸을 실제로 쓰고 있었다면 여기서 운다 — 그때는 창을 되돌린다."""
    전부 = _dashboard_with(screen, None)
    창0 = _dashboard_with(screen, 0)

    assert 창0.model_dump() == 전부.model_dump()


def test_대시보드_매입_표와_통계가_그대로다(screen):
    """쪼개서도 본다 — 통째 비교가 깨질 때 **어디가** 달라졌는지 말해 주려는 것이다."""
    전부 = _dashboard_with(screen, None)
    창0 = _dashboard_with(screen, 0)

    assert [s.model_dump() for s in 창0.stats] == [s.model_dump() for s in 전부.stats]
    assert 창0.purchase.rows == 전부.purchase.rows
    assert 창0.purchase_note.text == 전부.purchase_note.text
    assert [b.model_dump() for b in 창0.badges] == [b.model_dump() for b in 전부.badges]


def test_대시보드가_보는_매입_출처도_그대로다(screen):
    """`source` 는 대시보드가 실제로 읽는 둘 중 하나다 — 창이 그것을 건드리면 안 된다."""
    전부 = _dashboard_with(screen, None)
    창0 = _dashboard_with(screen, 0)

    def 매입출처(tab):
        return next(s for s in tab.sources if s.owner == "매입")

    assert 매입출처(창0).model_dump() == 매입출처(전부).model_dump()
