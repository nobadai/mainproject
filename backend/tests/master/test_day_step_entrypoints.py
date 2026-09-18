"""🔴 하루를 넘기는 단계 **일곱이 전부 손으로 부를 자리가 있다** (2026-09-18).

```text
개장 → 전이 재시도 → 입고 → 채권 → 수금 → [판단] → 출고 → 마감
        ^^^^^^^^^^                              ^^^^
        이 둘만 라우트가 없었다
```

★ 없으면 **어제 누른 것이 오늘 안 움직인다** — 어제 적은 실매입가가 「입고 처리 중」에서
  안 넘어가고(전이 재시도), 어제 승인한 판매가 오늘 안 나간다(출고). 화면에서 하루를
  넘기는 사람에게는 그 둘이 빠진 하루가 **조용히 반쪽**이다.

🔴 **부르는 함수가 걷기와 같아야 한다.** 갈리면 손으로 넘긴 하루와 걷기가 다른 코드를
   지나고, 그 갈림은 백테스트가 못 잡는다 (`test_closing.py` 가 마감에 같은 잠금을 건다).
"""

from __future__ import annotations

import inspect as _inspect
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.master import outbound_flow, pending_transition
from app.master.outbound_flow import OutboundOut, SaleItemOutcome
from app.master.pending_transition import RetriedTransition, RetryOut

AS_OF = date(2026, 9, 15)
축 = "SIM-TEST-DAY-STEP"


@pytest.fixture
def client() -> TestClient:
    import app.main

    return TestClient(app.main.app)


def test_전이_재시도와_출고에도_진입점이_있다() -> None:
    """🔴 **없으면 하루를 넘기는 사람이 그 둘을 건너뛴다.**"""
    from app.master.router import router

    paths = {getattr(r, "path", "") for r in router.routes}
    있는_날_경로 = sorted(p for p in paths if "days" in p)

    assert "/master/days/{as_of}/retry-transitions" in paths, 있는_날_경로
    assert "/master/days/{as_of}/ship" in paths, 있는_날_경로


def test_두_진입점이_부르는_함수가_하루_실행이_부르는_함수와_같다() -> None:
    """🔴 **둘이 갈리면 손으로 부른 결과와 걷기 결과가 다른 코드를 지난다.**"""
    from app.master import router as router_module
    from app.master import scheduler

    전이_경로 = _inspect.getsource(router_module.master_retry_pending_transitions)
    출고_경로 = _inspect.getsource(router_module.master_ship_due_sales)
    assert "run_retry_pending_transitions(as_of, sim_run_id=_walk_axis(sim_run_id))" in 전이_경로
    assert "run_ship_due_sales(as_of, sim_run_id=_walk_axis(sim_run_id))" in 출고_경로

    재시도 = pending_transition.retry_pending_transitions
    assert router_module.run_retry_pending_transitions is 재시도
    assert router_module.run_ship_due_sales is outbound_flow.ship_due_sales

    걷기 = _inspect.signature(scheduler.run_scheduled_day).parameters
    assert 걷기["retry_fn"].default is pending_transition.retry_pending_transitions
    assert 걷기["outbound_fn"].default is outbound_flow.ship_due_sales


def test_전이_재시도_응답이_서버가_낸_사유를_그대로_싣는다(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """★ 화면이 「몇 번째에서 멈췄나」를 적으려면 **사유가 본문에 있어야** 한다."""
    from app.master import router as router_module

    낸값 = RetryOut(
        status="RAN",
        reason="미적용 1건을 다시 세웠다",
        retried=(
            RetriedTransition(
                request_id="REQ-1",
                decision_seq=1,
                as_of=AS_OF,
                outcome="APPLIED",
            ),
        ),
    )
    monkeypatch.setattr(
        router_module, "run_retry_pending_transitions", lambda as_of, **kwargs: 낸값
    )

    resp = client.post(
        f"/master/days/{AS_OF.isoformat()}/retry-transitions", params={"sim_run_id": 축}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "RAN"
    assert resp.json()["reason"] == "미적용 1건을 다시 세웠다"
    assert resp.json()["retried"][0]["outcome"] == "APPLIED"


def test_출고_응답이_서버가_낸_사유를_그대로_싣는다(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """★ `SHORT` 는 사업 결과라 품목별로 실린다 — 화면이 덮으면 그 사실이 사라진다."""
    from decimal import Decimal

    from app.master import router as router_module

    낸값 = OutboundOut(
        as_of=AS_OF,
        status="RAN",
        reason="나갈 것 1건을 내보냈다",
        items=(
            SaleItemOutcome(
                sale_id="SALE-1",
                sale_item_id="SALE-1-1",
                reservation_id="RES-1",
                status="SHORT",
                required_qty_kg=Decimal(100),
                shipped_qty_kg=Decimal(0),
                reason="확보 0kg",
            ),
        ),
    )
    monkeypatch.setattr(router_module, "run_ship_due_sales", lambda as_of, **kwargs: 낸값)

    resp = client.post(f"/master/days/{AS_OF.isoformat()}/ship", params={"sim_run_id": 축})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "RAN"
    assert resp.json()["reason"] == "나갈 것 1건을 내보냈다"
    assert resp.json()["items"][0]["status"] == "SHORT"


@pytest.mark.parametrize("tail", ["retry-transitions", "ship"])
def test_두_진입점도_축_없이는_장부를_안_바꾼다(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, tail: str
) -> None:
    """🔴 다섯 형제와 **같은 가드**다 — 축이 없으면 요청 자체를 거절한다."""
    from app.master import router as router_module

    불린: list[Any] = []
    for attr in ("run_retry_pending_transitions", "run_ship_due_sales"):
        monkeypatch.setattr(
            router_module, attr, lambda as_of, **kwargs: 불린.append(kwargs) or None
        )

    없음 = client.post(f"/master/days/{AS_OF.isoformat()}/{tail}")
    빈값 = client.post(f"/master/days/{AS_OF.isoformat()}/{tail}", params={"sim_run_id": "  "})

    assert 없음.status_code == 422, 없음.text
    assert 빈값.status_code == 400, 빈값.text
    assert 불린 == [], "거부했는데 장부 함수를 불렀다"
