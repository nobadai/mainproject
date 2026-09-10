"""판매 판단이 자기 Critic 을 **지나가는가** (Refs #150).

실측 2026-09-08 — 그 전까지 두 사이클이 이랬다.

```text
매입(A)   verifier → critic_bridge → run_critic_procurement   판단 안에서 돈다
판매(B)   run_critic_sales ← critic/router.py:54 에서만        HTTP 로만 불렸다
```

그래서 *"두 시나리오 다 critic 검증을 거친다"* 가 사실이 아니었다. 여기서 재는 것은
**경로가 섰는가**다 — 판정의 질이 아니다.

⚠️ **온전한 판정은 아직 안 난다.** `inventory_allocations` · `inventory_reservations`
  가 0행이라 배분·로트 재료가 없다 (물류 답 대기). 그 사실이 **통과로 접히지 않는지**를
  이 파일이 지킨다.

★ 검사는 전부 대역이다 — DB 를 안 탄다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import wiring
from app.master.critic.schemas import CriticSalesRequest, CriticVerdictOut
from app.master.critic.service import run_critic_procurement, run_critic_sales
from app.master.critic_bridge import CriticSkipped, _sales_replies_in, build_sales_request
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.schemas import SalesRunRequest
from app.master.service import run_sales
from app.master.verifier import (
    MasterVerifier,
    SalesVerificationContext,
    SalesVerifier,
)

평일 = date(2026, 9, 10)
품목 = "배추"


# ── 대역 ─────────────────────────────────────────────────────────────────────


def _verdict(**kw: Any) -> CriticVerdictOut:
    base: dict[str, Any] = {
        "cycle": "B",
        "as_of": 평일,
        "run_seq": 1,
        "scenario_id": "ALLOC-1",
        "runtime_status": "READY",
        "status": "PASS",
        "badge": "판정",
        "coverage": {"L4": (2, 4)},
        "coverage_ratio": (2, 4),
        "findings": [],
        "concerns": [],
        "skipped": [],
        "end_stage": None,
    }
    base.update(kw)
    return CriticVerdictOut(**base)


class _Spy:
    """Critic 대역. **몇 번 불렸는지**를 센다."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[CriticSalesRequest] = []
        self.raises = raises

    def __call__(self, req: CriticSalesRequest) -> CriticVerdictOut:
        self.calls.append(req)
        if self.raises is not None:
            raise self.raises
        return _verdict()


def _reply(request: AgentRequest, payload: dict[str, Any]) -> AgentReply:
    return AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=request.agent,
        mode=request.mode,
        run_id=f"{request.agent.upper()}-{request.call_seq}",
        runtime_status="READY",
        business_status="ok",
        payload=payload,
    )


def _port(payload: dict[str, Any]):
    def port(request: AgentRequest):
        reply = _reply(request, payload)
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


#: 물류 `PRE_SALES` 정본 payload — 주소도 이름도 물류 것이다
#: (`app/logistics/adapter.py` · PR #484).
#:
#: 🔴 **최상위 `inventory_by_item` · `lots` 가 없다.** 정본이 그렇다 —
#:   `sellable_supply` 한 겹 안이고 로트 칸 이름은 `lot_constraints` 다.
_공급 = {
    "sellable_supply": {
        "status": "READY",
        "inventory_by_item": [{"item": 품목, "available_qty_kg": 3000.0}],
        "lot_constraints": [
            {
                "lot_id": "LOT-1",
                "item": 품목,
                "available_qty_kg": 3000.0,
                "remaining_freshness_days": 9,
                "grade": None,
                "status": "AVAILABLE",
            }
        ],
    },
    "delivery_feasibility": {"daily_outbound_capacity_kg": 7636.72},
}


def _로트없는_공급() -> dict:
    """로트만 빈 정본. 🔴 **`sellable_supply` 를 통째로 지우지 않는다** — 그러면
    재고 축까지 같이 사라져 다른 이유로 서게 되고 검사가 무엇을 재는지 흐려진다."""
    supply = {**_공급["sellable_supply"], "lot_constraints": []}
    return {**_공급, "sellable_supply": supply}


#: 판매 후보 — 채널 배분이 실린 모양 (`app/sales/schemas.py` `SalesCandidate.allocation`).
_배분_있는_후보 = {
    "scenario_id": "ALLOC-1",
    "item": 품목,
    "required_validations": [],
    # 🔴 상업조건 둘이 없으면 후보가 presented 에 안 오른다 (2026-09-08 계약).
    "delivery_date": "2026-09-15",
    "payment_days": 30,
    "allocation": [
        {
            "channel": "KIMCHI_FACTORY",
            "qty_kg": 2000.0,
            "unit_price": 1200.0,
            "lot_ids": ["LOT-1"],
            "due_date": "2026-09-15",
        }
    ],
}

#: 오늘 DB 가 주는 모양 — 배분이 없다 (`inventory_allocations` 0행).
_배분_없는_후보 = {
    "scenario_id": "ALLOC-1",
    "item": 품목,
    "required_validations": [],
    "delivery_date": "2026-09-15",
    "payment_days": 30,
}


def _wire(scenario: dict[str, Any], supply: dict[str, Any] = _공급) -> None:
    wiring.reset()
    wiring.register("inventory", _port(dict(supply)))
    wiring.register("sales", _port({"scenarios": [scenario], "situation": "물량이 있다"}))
    wiring.register("finance", _port({"verdict": "ok"}))


def _request() -> SalesRunRequest:
    return SalesRunRequest(
        as_of=평일,
        policy_version="v1.3",
        business_mode="SPOT_SALES",
        item=품목,
    )


# ── ① 판단이 critic 을 실제로 부르는가 ───────────────────────────────────────


def test_판매_판단이_critic_을_한_번_부른다() -> None:
    """🔴 **경로가 서 있는가.** 대역 스파이의 호출 횟수로만 잰다."""
    _wire(_배분_있는_후보)
    spy = _Spy()

    response = run_sales(_request(), verifier=SalesVerifier(critic=spy))

    assert len(spy.calls) == 1, "판매 판단이 Critic B 를 안 불렀다"
    assert spy.calls[0].items == [품목]
    assert [a.allocation_id for a in spy.calls[0].allocations] == ["ALLOC-1"]
    assert [lot.lot_id for lot in spy.calls[0].lot_constraints] == ["LOT-1"]
    # 판정이 났으면 커버리지가 응답에 남는다 — `findings: []` 를 통과로 못 읽게 한다.
    assert any("커버리지" in s for s in response.skipped_checks)
    assert response.verification_skipped is False


def test_critic_판정이_응답에_실린다() -> None:
    """★ 매입이 싣는 모양 그대로 — findings · concerns · skipped_checks."""
    _wire(_배분_있는_후보)

    class _Finding(_Spy):
        def __call__(self, req: CriticSalesRequest) -> CriticVerdictOut:
            self.calls.append(req)
            return _verdict(
                status="FAIL",
                findings=[
                    {
                        "layer": "L4",
                        "check_id": "L4-7",
                        "detail": "on_hand 초과",
                        "dept": "inventory",
                        "route": None,
                    }
                ],
            )

    response = run_sales(_request(), verifier=SalesVerifier(critic=_Finding()))

    assert any("CRITIC/L4/L4-7" in f for f in response.findings)


# ── ② 재료가 없으면 「못 냈다」로 나간다 ──────────────────────────────────────


def test_배분이_없으면_못_냈다로_나가고_통과가_아니다() -> None:
    """🔴 **재료가 없는 것은 통과가 아니다** (§3.7.6).

    `inventory_allocations` 가 0행인 오늘의 모양이다. 조용히 지나가면 빈 `findings` 가
    *"Critic 을 지나 통과했다"* 로 읽힌다.
    """
    _wire(_배분_없는_후보)
    spy = _Spy()

    response = run_sales(_request(), verifier=SalesVerifier(critic=spy))

    assert spy.calls == [], "재료가 없는데 Critic 을 불렀다"
    assert response.findings == []
    assert any("배분안을 Critic 입력으로 옮기지 못했다" in s for s in response.skipped_checks)
    # **통과로 접히지 않았다** — 커버리지 문장이 없어야 한다. 있으면 판정이 난 것처럼 읽힌다.
    assert not any("커버리지" in s for s in response.skipped_checks)


def test_로트가_없으면_빈_목록으로_넘기지_않는다() -> None:
    """⚠️ `lot_constraints` 는 계약 기본값이 `[]` 라 그냥 통과한다 — 그러면 L4-7·L4-8 이
    *"검사할 로트가 없다"* 로 조용히 지나간다. 그 자리를 막는다."""
    with pytest.raises(CriticSkipped) as caught:
        build_sales_request(
            as_of=평일,
            item=품목,
            candidates=[_배분_있는_후보],
            supply_context={"payload": _로트없는_공급()},
        )

    assert "로트 제약을 Critic 입력으로 옮기지 못했다" in str(caught.value)


def test_정본_중첩_경로에서_재고와_로트를_읽는다() -> None:
    """🔴 **`payload.sellable_supply.*` 를 읽는다** (물류 PR #484 수신요청 §4).

    ```text
    inventory_by_item   → payload.sellable_supply.inventory_by_item   모양 같음
    lots                → payload.sellable_supply.lot_constraints     **이름도 다르다**
    ```

    전에는 payload **최상위**에서 저 둘을 읽었는데 정본에는 최상위에 그 둘이 없다 —
    그래서 판매 Critic 이 **조용히 빈손**이었다 (오류도 안 났다).
    """
    request = build_sales_request(
        as_of=평일, item=품목, candidates=[_배분_있는_후보], supply_context={"payload": _공급}
    )

    assert [lot.lot_id for lot in request.lot_constraints] == ["LOT-1"]
    assert request.replies[0].checks[0].cap_kg == {품목: 3000.0}


def test_구_평면_경로는_더_이상_안_읽는다() -> None:
    """🔴 **구·신 경로를 둘 다 읽지 않는다** (물류 §3 dual mapper 금지).

    둘 다 읽으면 물류가 주소를 바꾼 사실이 마스터 안에서 덮이고, 한 사실에 주소가
    둘이 된다. 구 평면 payload 는 **재료 없음으로 서야** 한다.
    """
    구_평면 = {
        "warehouse_free_kg": 5000.0,
        "inventory_by_item": [{"item": 품목, "available_qty_kg": 3000.0}],
        "lots": [
            {
                "lot_id": "LOT-1",
                "item": 품목,
                "available_qty_kg": 3000.0,
                "remaining_freshness_days": 9,
                "status": "AVAILABLE",
            }
        ],
    }

    with pytest.raises(CriticSkipped) as caught:
        build_sales_request(
            as_of=평일, item=품목, candidates=[_배분_있는_후보], supply_context={"payload": 구_평면}
        )

    assert "로트 제약을 Critic 입력으로 옮기지 못했다" in str(caught.value)


def test_창고_여유는_출고_여력으로_메우지_않는다() -> None:
    """★★ **없으면 없는 대로 둔다** (물류 §4).

    `PRE_SALES` 정본에 `warehouse_free_kg` 가 **없다.**
    `delivery_feasibility.daily_outbound_capacity_kg` 는 **출고 여력**이지 창고
    여유가 아니다 — 다른 사실을 같은 칸에 넣는 것이 물류가 금지한 「재조립」이다.

    ⚠️ 계약이 `float` 이라 *"모름"* 을 담을 칸이 없어 `0.0` 이 간다. 그 사실은
      `replies` 가 `cap_total_kg` 없이 만들어진 것으로 드러난다.
    """
    request = build_sales_request(
        as_of=평일, item=품목, candidates=[_배분_있는_후보], supply_context={"payload": _공급}
    )

    assert _공급["delivery_feasibility"]["daily_outbound_capacity_kg"] == 7636.72
    assert request.warehouse_free_kg == 0.0, "출고 여력을 창고 여유 칸에 넣지 않는다"
    assert request.replies[0].checks[0].cap_total_kg is None


def test_창고_여유_0은_꽉_찼다는_뜻이_아니다() -> None:
    """🔴 **결측을 `0kg` 이라는 사실로 접지 않는다** (2026-09-10 물류 회신 · ㉡).

    ```text
    0kg          창고가 **실제로 꽉 찼다**
    PRE_SALES    그 사실을 **안 낸다** — 창고는 매입·입고 쪽 질문이다
    ```

    ★ 물류가 청한 *"PRE_SALES 경로에서는 창고 여유 검사를 미적용으로"* 는
      **`cap_total_kg` 칸이 아예 안 실리는 것**으로 선다.

    🔴 **키의 부재를 잰다. `== 0.0` 을 재면 안 된다** — 누가
      `check["cap_total_kg"] = cap_total or 0.0` 을 넣어도 그 단언은 초록이라
      **막으려던 바로 그것을 통과시킨다.**

    ⚠️ 위 `test_창고_여유는_출고_여력으로_메우지_않는다` 와 겹쳐 보이지만 **재는 층이
      다르다.** 저쪽은 계약 모델을 통과한 뒤의 값이고, 여기는 `_sales_replies_in` 이
      **만드는 dict 자체**다. 모델이 기본값을 채우기 시작하면 저쪽만으로는 못 잡는다.
    """
    replies = _sales_replies_in(_공급, 품목)

    check = replies[0]["checks"][0]
    assert "cap_kg" in check, "품목 가용재고는 실린다 — 이 검사가 빈손을 재는 것이 아니다"
    assert "cap_total_kg" not in check, (
        "PRE_SALES 에는 warehouse_free_kg 가 없다 — 칸을 만들면 «안 낸 것» 이 «꽉 찼다» 로 선다"
    )


def test_품목이_없으면_매입과_같은_낱말로_선다() -> None:
    """★ 새 낱말을 만들지 않았다 — 매입 `build_request` 와 같은 문장이다."""
    with pytest.raises(CriticSkipped) as caught:
        build_sales_request(
            as_of=평일, item=None, candidates=[_배분_있는_후보], supply_context={"payload": _공급}
        )

    assert "품목이 정해지지 않아" in str(caught.value)


# ── ③ critic 이 죽어도 판매 판단은 산다 ──────────────────────────────────────


def test_critic_예외가_판매_판단을_죽이지_않는다() -> None:
    """★ **검증 실패는 판매 판단의 실패가 아니다.** 다만 조용히 넘어가지도 않는다."""
    _wire(_배분_있는_후보)
    spy = _Spy(raises=RuntimeError("critic 내부 폭발"))

    response = run_sales(_request(), verifier=SalesVerifier(critic=spy))

    assert len(spy.calls) == 1
    assert response.end_code == "SL1_PRESENTED"
    assert any("CRITIC: 검증 Tool 이 돌지 못했다" in c for c in response.concerns)
    assert any("실행 중 오류로 미판정" in s for s in response.skipped_checks)
    # 🔴 세 값을 안 섞는다 — 실패는 판정도 아니고 재료 부족도 아니다.
    assert response.findings == []


def test_critic_을_안_붙이면_그_사실이_남는다() -> None:
    """🔴 `None` 은 *"Critic 을 안 돌렸다"* 는 뜻이다 — 통과가 아니다."""
    result = SalesVerifier(critic=None)(
        SalesVerificationContext(as_of=평일, item=품목, candidates=(_배분_있는_후보,))
    )

    assert result.findings == ()
    assert any("검증 Tool 에 주입되지 않음" in s for s in result.skipped)


# ── ④ 기본값과 매입 경로 ─────────────────────────────────────────────────────


def test_기본값은_run_critic_sales_자체다() -> None:
    """🔴 `None` 을 센티넬로 쓰지 않는다 — 그 낱말은 이미 *"안 돌렸다"* 를 뜻한다."""
    assert SalesVerifier().critic is run_critic_sales


def test_주입하지_않으면_기본_검증_Tool_이_붙는다() -> None:
    """★ 매입 `run_procurement` 와 같은 규율 — 끄려면 명시적으로 꺼야 한다."""
    _wire(_배분_없는_후보)

    response = run_sales(_request())

    # 기본 Tool 이 붙었으니 **못 냈다는 문장**이 남는다. 아무것도 안 붙었으면 빈다.
    assert response.skipped_checks, "기본 검증 Tool 이 안 붙어 아무 사실도 안 남았다"


def test_매입_경로는_안_변했다() -> None:
    """회귀 방어 — 매입 기본 Critic 은 그대로 `run_critic_procurement` 다."""
    assert MasterVerifier().critic is run_critic_procurement
