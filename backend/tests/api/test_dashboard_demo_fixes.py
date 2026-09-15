"""통합 대시보드 시연 전 표시 오류 (2026-09-14 화면 점검).

🔴 **실 DB 에 닿지 않는다.** 대시보드가 부르는 다섯 탭 `build()` 와 두 그래프를
   대역으로 바꿔 «받은 값을 어디에 어떻게 놓는가» 만 본다.

```text
① 매입 표 품목·ML 가격이 안마다 그 안의 품목 것이다 (대역 두 품목)
② 「두 안」 고정 꼬리가 없다 · 실제 개수가 붙는다
③ 지어낸 배지 문구(배치 시각 · open_day)가 응답에도 소스에도 없다
④ 「운영 여유」 가 어느 기준(대출 포함/제외)인지 밝힌다 · 값은 그대로
```
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.dashboard import query as dashboard_query
from app.api.forecast.schema import ItemCard
from app.api.primitives import Chart, Source, Stat
from app.contracts.core import ITEMS

AS_OF = date(2026, 1, 26)
_QUERY = Path(dashboard_query.__file__)

#: 배추 카드는 대시보드 첫 칸이 쓰므로 늘 둔다. 표에는 **다른 두 품목**을 싣는다.
CABBAGE = "배추"
ITEM_A, ITEM_B = [i for i in ITEMS if i != CABBAGE][:2]
PRICE = {CABBAGE: 888, ITEM_A: 1_234, ITEM_B: 567}


def _card(item: str) -> ItemCard:
    p = PRICE[item]
    return ItemCard(item=item, grade="특", target_date="2026-01-28", predicted=p,
                    lower=p - 10, upper=p + 10, ci_width=0.1, review=False,
                    use_recommended=True)


def _plan(key: str, pending: bool = True) -> SimpleNamespace:
    return SimpleNamespace(key=key, unit_price=900, qty_kg=1000.0,
                           amount_krw=900_000, max_price=1_000, pending=pending)


def _chart() -> Chart:
    return Chart(label="대역", y_min=0, y_max=1, y_ticks=[0, 1], series=[])


def _source(owner: str) -> Source:
    return Source(filled=True, owner=owner)


def _finance(selected: str, states: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        stats=[Stat(label="운영 여유", value="1,234", unit="만원", detail="여유 있음",
                    raw=12_340_000)],
        states=[SimpleNamespace(key=k, label=lbl) for k, lbl in states],
        selected=selected,
        source=_source("재무"),
    )


@pytest.fixture
def stub(monkeypatch):
    plans = [
        _plan(f"{ITEM_A} · 공격"),
        _plan(f"{ITEM_B} · 기본"),
        _plan(f"{ITEM_A} · 보수", pending=False),
        _plan(f"{ITEM_B} · 보수"),
    ]
    state = {"finance": _finance("loan", [("loan", "대출 반영")])}

    monkeypatch.setattr(
        dashboard_query.forecast_q, "build",
        lambda as_of, item: SimpleNamespace(
            cards=[_card(CABBAGE), _card(ITEM_A), _card(ITEM_B)], source=_source("ML")),
    )
    monkeypatch.setattr(
        dashboard_query.purchase_q, "build",
        lambda as_of, sim_run_id=None: SimpleNamespace(plans=plans, source=_source("매입")),
    )
    monkeypatch.setattr(dashboard_query.finance_q, "build", lambda as_of, s: state["finance"])
    monkeypatch.setattr(
        dashboard_query.logistics_q, "build",
        lambda as_of, pane: SimpleNamespace(
            #  ★ 대시보드는 **열쇠로** 재고 칸을 집는다 (#675). 자리로 집던 때의
            #    대역이라 `key` 가 없었다.
            panes=[
                SimpleNamespace(key="stock", stats=[Stat(label="재고", value="1", raw=1)])
            ],
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
    return SimpleNamespace(plans=plans, state=state)


def test_매입_표_품목과_ML_가격은_안마다_그_안의_품목_것이다(stub):
    rows = dashboard_query.build(AS_OF).purchase.rows
    expected = [ITEM_A, ITEM_B, ITEM_A, ITEM_B]
    assert [r["item"] for r in rows] == expected
    assert [r["ml"] for r in rows] == [f"{PRICE[i]:,}" for i in expected]
    assert CABBAGE not in {r["item"] for r in rows}


def test_계약_밖_품목은_공란이다(stub):
    stub.plans[:] = [_plan("없는품목 · 기본")]
    row = dashboard_query.build(AS_OF).purchase.rows[0]
    assert row["item"] is None
    assert row["ml"] is None


def test_승인_대기_칸에_두_안_고정_꼬리가_없다(stub):
    tab = dashboard_query.build(AS_OF)
    stat = next(s for s in tab.stats if s.label == "매입 승인 대기")
    assert "두 안" not in stat.detail
    assert stat.detail.endswith(f"· {len(stub.plans)}안")
    assert "두 안" not in _QUERY.read_text(encoding="utf-8")


def test_지어낸_배지_문구가_없다(stub):
    tab = dashboard_query.build(AS_OF)
    texts = " ".join(b.text for b in tab.badges)
    source = _QUERY.read_text(encoding="utf-8")
    for fixed in ("06:10", "ML 배치", "open_day", "전일 승계"):
        assert fixed not in texts
        assert fixed not in source
    today = tab.axis.days[tab.axis.as_of_index]
    assert ("장 열림" if today.market_open else "휴장") in texts


@pytest.mark.parametrize(("selected", "basis"), [("loan", "대출 포함"), ("base", "대출 제외")])
def test_운영_여유는_기준을_밝히고_값은_그대로다(stub, selected, basis):
    stub.state["finance"] = _finance(selected, [(selected, "재무 이름")])
    tab = dashboard_query.build(AS_OF)
    stat = next(s for s in tab.stats if s.label.startswith("운영 여유"))
    assert basis in stat.label
    original = stub.state["finance"].stats[0]
    assert (stat.value, stat.raw, stat.detail) == (original.value, original.raw, original.detail)


def test_모르는_재무_키면_재무가_준_상태_이름을_쓴다(stub):
    stub.state["finance"] = _finance("other", [("other", "다른 기준")])
    tab = dashboard_query.build(AS_OF)
    stat = next(s for s in tab.stats if s.label.startswith("운영 여유"))
    assert "다른 기준" in stat.label
