"""재무 선행 사실(②')과 판매 반복 실행.

```text
① 순서      물류 → 재무 사실 → 판매 → 재무 판정   후보를 만들기 전에 사실이 온다
② 전달      판매 payload 에 finance_context 가 실제로 들어간다
③ 0 / NULL  0 은 사실, 없는 칸은 모름 — 마스터가 0 으로 메우지 않는다
④ as_of     사실 조회가 그 실행의 as_of 로 나간다
⑤ 반복      같은 거래처·품목·날짜라도 실행마다 새 이력이 append 된다
```

🔴 **②' 를 뒤로 옮기면 그것은 판정이지 사실이 아니다.** 판매가 수량과 가격을 정한
  **뒤에** 자금 상황을 받으면, 받은 값이 후보에 반영될 자리가 없다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.master import persistence, wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.schemas import SalesRunRequest
from app.master.service import run_sales
from tests.master.logistics_pre_sales import PRE_SALES_PAYLOAD

평일 = date(2026, 9, 10)
어제 = date(2026, 9, 9)

#: 재무가 내는 선행 사실의 모양. **0 과 없는 칸이 같이 들어 있다** — ③이 이것을 잰다.
FINANCE_FACTS: dict[str, Any] = {
    "as_of": 평일.isoformat(),
    "available_cash": 31_000_000.0,
    "payables_total_krw": 0.0,
    "payables_due_7d_krw": 0.0,
    "payables_due_30d_krw": 0.0,
    "receivables_total_krw": 7_000_000.0,
    "payment_pressure": "LOW",
    "partner_credit": {
        "partner_id": "CUST-1",
        "partner_receivable_krw": 0.0,
        # 🔴 `partner_credit_limit_krw` 가 **없다** — 한도가 안 선 거래처다.
    },
}


def _reply(request: AgentRequest, **kw) -> AgentReply:
    base = {
        "request_id": request.context.request_id,
        "as_of": request.context.as_of,
        "agent": request.agent,
        "mode": request.mode,
        "run_id": f"{request.agent.upper()}-{request.mode[:4]}-{request.call_seq}",
        "runtime_status": "READY",
        "business_status": "ok",
    }
    base.update(kw)
    return AgentReply(**base)


def _port(payload_by_mode: dict[str, dict[str, Any]], capture: list):
    def port(request: AgentRequest):
        capture.append((request.agent, request.mode, dict(request.payload)))
        reply = _reply(request, payload=payload_by_mode.get(request.mode, {}))
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


_SCENARIOS = {
    "scenarios": [
        {
            "scenario_id": "SCN-1",
            "required_validations": ["FINANCIAL_VALIDATION"],
            "delivery_date": "2026-09-17",
            "payment_days": 30,
        }
    ],
    "situation": "물량이 있다",
}


def _wire(*, finance_facts: dict[str, Any] | None = None, finance_ready: bool = True) -> list:
    called: list = []
    wiring.reset()
    wiring.register("inventory", _port({"PRE_SALES": PRE_SALES_PAYLOAD}, called))
    wiring.register("sales", _port({"GENERATE_SALES_PROPOSAL": _SCENARIOS}, called))

    facts = FINANCE_FACTS if finance_facts is None else finance_facts

    def finance(request: AgentRequest):
        called.append((request.agent, request.mode, dict(request.payload)))
        if request.mode == "PRE_SALES_FACTS" and not finance_ready:
            reply = _reply(
                request,
                runtime_status="RUNTIME_NOT_READY",
                business_status="skipped",
                missing_data=("finance_state",),
                reasoning="재무 상태를 못 읽었다",
                payload={},
            )
        elif request.mode == "PRE_SALES_FACTS":
            reply = _reply(request, payload=facts)
        else:
            reply = _reply(request, payload={"finance_verdict": "PASS"})
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    wiring.register("finance", finance)
    return called


def _request(**kw) -> SalesRunRequest:
    base = {
        "as_of": 평일,
        "policy_version": "v1.3",
        "business_mode": "SPOT_SALES",
        "item": "배추",
        "partner_id": "CUST-1",
        "requested_quantity_kg": 1000,
        "preferred_delivery_date": date(2026, 9, 17),
        "preferred_payment_days": 30,
    }
    base.update(kw)
    return SalesRunRequest(**base)


def _sales_payload(called: list) -> dict[str, Any]:
    return next(
        payload
        for agent, mode, payload in called
        if (agent, mode) == ("sales", "GENERATE_SALES_PROPOSAL")
    )


@pytest.fixture
def 적재를_지켜본다(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    counter = {"n": 0}

    def fake(**kwargs):
        counter["n"] += 1
        seen.append(kwargs)
        return f"RUN-FAKE-{counter['n']}"

    monkeypatch.setattr("app.master.persistence.try_save_run", fake)
    return seen


# ---------------------------------------------------------------------------
# ① 순서
# ---------------------------------------------------------------------------


def test_재무_사실이_판매_후보_생성보다_먼저_온다():
    """🔴 뒤로 가면 그것은 판정이지 사실이 아니다."""
    called = _wire()

    response = run_sales(_request())

    assert response.end_code == "SL1_PRESENTED", response.reason
    순서 = [(agent, mode) for agent, mode, _ in called]
    assert 순서[:3] == [
        ("inventory", "PRE_SALES"),
        ("finance", "PRE_SALES_FACTS"),
        ("sales", "GENERATE_SALES_PROPOSAL"),
    ]


def test_사실_조회에_거래처가_실린다():
    """재무가 채권·여신을 거래처 축으로 읽는다 — 안 실으면 여신 칸이 통째로 빈다."""
    called = _wire()

    run_sales(_request())

    payload = next(
        payload for agent, mode, payload in called if mode == "PRE_SALES_FACTS"
    )
    assert payload["partner_id"] == "CUST-1"
    assert payload["item"] == "배추"


# ---------------------------------------------------------------------------
# ② 전달
# ---------------------------------------------------------------------------


def test_판매_payload_에_finance_context_가_실린다():
    """그 칸은 스키마에 있었지만 **아무도 채우지 않았다** (실측 0건 / 9,937)."""
    called = _wire()

    run_sales(_request())

    assert _sales_payload(called)["finance_context"]["available_cash"] == 31_000_000.0


def test_재무가_못_답하면_칸을_안_만든다():
    """🔴 빈 매핑을 실으면 판매가 *"현금도 여신도 0"* 으로 읽는다 (§1.2-10)."""
    called = _wire(finance_ready=False)

    response = run_sales(_request())

    assert "finance_context" not in _sales_payload(called)
    # ★ 멈추지는 않는다 — 물류와 같은 태도다.
    assert response.end_code == "SL1_PRESENTED", response.reason


# ---------------------------------------------------------------------------
# ③ 0 / NULL
# ---------------------------------------------------------------------------


def test_0_은_사실이고_없는_칸은_모름이다():
    """마스터는 재무 payload 를 재조립하지 않는다 — 0 을 지우지도 채우지도 않는다."""
    called = _wire()

    run_sales(_request())

    context = _sales_payload(called)["finance_context"]
    assert context["payables_total_krw"] == 0.0, "0 원이라는 사실이 사라졌다"
    assert context["partner_credit"]["partner_receivable_krw"] == 0.0
    assert "partner_credit_limit_krw" not in context["partner_credit"], (
        "없는 여신한도를 마스터가 만들어 냈다"
    )


def test_전선에_실은_재무_사실은_왕복해도_같다():
    """튜플이 남으면 같은 칸이 경로에 따라 두 모양이 된다 (#175)."""
    import json

    called = _wire(
        finance_facts={**FINANCE_FACTS, "critical_payment_dates": ("2026-09-25",)}
    )

    run_sales(_request())

    context = _sales_payload(called)["finance_context"]
    assert json.loads(json.dumps(context, ensure_ascii=False)) == context


# ---------------------------------------------------------------------------
# ④ as_of
# ---------------------------------------------------------------------------


def test_사실_조회가_그_실행의_as_of_로_나간다():
    """과거 `as_of` 요청이 오늘 재무 상태를 받으면 백테스트가 성립하지 않는다."""
    called: list = []
    seen_as_of: list[date] = []
    wiring.reset()
    wiring.register("inventory", _port({"PRE_SALES": PRE_SALES_PAYLOAD}, called))
    wiring.register("sales", _port({"GENERATE_SALES_PROPOSAL": _SCENARIOS}, called))

    def finance(request: AgentRequest):
        if request.mode == "PRE_SALES_FACTS":
            seen_as_of.append(request.context.as_of)
        reply = _reply(request, payload=FINANCE_FACTS)
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    wiring.register("finance", finance)

    run_sales(_request(as_of=어제, preferred_delivery_date=date(2026, 9, 16)))

    assert seen_as_of == [어제]


# ---------------------------------------------------------------------------
# ⑤ 반복 실행
# ---------------------------------------------------------------------------


def test_같은_거래처_품목_날짜로_두_번_실행해도_둘_다_남는다(적재를_지켜본다):
    """🔴 사용자는 조건을 고쳐 다시 실행한다 — 두 번째가 막히면 안 된다.

    ★ 재검토·추가 판매도 같은 자리다. 판매안을 **여러 번 만드는 것**과 실판매를
      **여러 번 확정하는 것**은 다르다 (`confirm_sale` 경계는 그대로다).
    """
    _wire()

    첫번째 = run_sales(_request(requested_quantity_kg=500))
    두번째 = run_sales(_request(requested_quantity_kg=300))

    assert 첫번째.end_code == "SL1_PRESENTED", 첫번째.reason
    assert 두번째.end_code == "SL1_PRESENTED", 두번째.reason
    assert len(적재를_지켜본다) == 2, "두 번째 실행이 이력에 안 남았다"
    assert 첫번째.history_run_id != 두번째.history_run_id, "두 실행이 같은 이력 행에 앉았다"


def test_두_번째_실행이_첫_실행의_요청을_바꾸지_않는다(적재를_지켜본다):
    """이력은 append-only 다 — 새 상태를 반영하고 싶으면 새 run 을 만든다."""
    _wire()

    run_sales(_request(requested_quantity_kg=500))
    적재된_첫_요청 = dict(적재를_지켜본다[0]["request_payload"])

    run_sales(_request(requested_quantity_kg=300))

    assert 적재를_지켜본다[0]["request_payload"] == 적재된_첫_요청
    assert str(적재를_지켜본다[0]["request_payload"]["requested_quantity_kg"]) == "500"
    assert str(적재를_지켜본다[1]["request_payload"]["requested_quantity_kg"]) == "300"


def test_판매안을_여러_번_만들어도_실판매는_안_생긴다(적재를_지켜본다, monkeypatch):
    """🔴 proposal 실행 횟수와 confirmed sale 건수는 다른 사실이다.

    확정은 사용자가 고른 안 하나를 `decision` → 재검증 → `confirm_sale` 로 옮길 때만
    일어난다. 여기서는 그 경로를 **아무도 부르지 않는다**는 것을 본다.
    """
    확정 = []
    monkeypatch.setattr(
        "app.sales.persistence.confirm_sale",
        lambda *a, **kw: 확정.append((a, kw)),
    )
    _wire()

    run_sales(_request())
    run_sales(_request())
    run_sales(_request())

    assert len(적재를_지켜본다) == 3
    assert 확정 == [], "승인하지 않은 판매안이 실판매를 만들었다"


def test_사실_조회가_그_실행의_sim_run_id_축으로_나간다():
    """어느 실행의 장부인가는 마스터가 정한다 — 재무가 추측하면 남의 잔액을 읽는다."""
    called: list = []
    본_축: list[str] = []
    wiring.reset()
    wiring.register("inventory", _port({"PRE_SALES": PRE_SALES_PAYLOAD}, called))
    wiring.register("sales", _port({"GENERATE_SALES_PROPOSAL": _SCENARIOS}, called))

    def finance(request: AgentRequest):
        if request.mode == "PRE_SALES_FACTS":
            본_축.append(request.context.sim_run_id)
        reply = _reply(request, payload=FINANCE_FACTS)
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    wiring.register("finance", finance)

    run_sales(_request(sim_run_id="SIM-AXIS-1"))

    assert 본_축 == ["SIM-AXIS-1"]


def test_새_사용자_실행은_되먹임_회차_0_부터_시작한다(적재를_지켜본다):
    """🔴 Run B 가 Run A 의 `feedback_attempt` 를 이어받으면 안 된다 (§11).

    한 Run 안의 재계획과 사용자가 다시 부른 새 Run 은 다른 개념이다.
    """
    called = _wire()

    run_sales(_request(requested_quantity_kg=500))
    run_sales(_request(requested_quantity_kg=300))

    회차 = [
        payload["feedback_attempt"]
        for agent, mode, payload in called
        if (agent, mode) == ("sales", "GENERATE_SALES_PROPOSAL")
    ]
    assert 회차 == [0, 0], f"새 실행이 앞 실행의 회차를 이어받았다: {회차}"


def test_추가_판매는_그날_상태를_다시_읽는_새_run_이다(적재를_지켜본다):
    """★ 과거 Run 을 재사용하지 않는다 — 새 실행은 재고·재무를 **다시 묻는다.**"""
    called = _wire()

    run_sales(_request(requested_quantity_kg=500))
    앞선_호출 = len(called)
    run_sales(_request(requested_quantity_kg=200))

    새_호출 = [(agent, mode) for agent, mode, _ in called[앞선_호출:]]
    assert ("inventory", "PRE_SALES") in 새_호출
    assert ("finance", "PRE_SALES_FACTS") in 새_호출
    assert len(적재를_지켜본다) == 2


def test_같은_조건으로_다시_실행해도_새_run_이_선다(적재를_지켜본다):
    """§28 Case B — 사용자가 같은 조건으로 다시 비교하고 싶을 수 있다."""
    _wire()

    첫번째 = run_sales(_request())
    두번째 = run_sales(_request())

    assert 첫번째.history_run_id != 두번째.history_run_id
    assert len(적재를_지켜본다) == 2


def test_같은_request_id_재전송도_이력을_덮어쓰지_않는다(적재를_지켜본다):
    """★ **기존 idempotency 계약을 제거하지 않았다** — 애초에 없다.

    `master_agent_runs` 는 `run_id` 가 기본키이고 실행마다 새로 난다.
    같은 `request_id` 로 다시 불러도 **앞 행을 고치지 않고 새 행이 선다** —
    네트워크 재시도 보호를 여기서 새로 만들지도, 없는 것을 있는 척하지도 않는다.

    🔴 `partner_id + item + as_of` 로 1회 실행 제한을 만들지 않는다.
    """
    _wire()

    run_sales(_request(request_id="REQ-FIXED-0001", requested_quantity_kg=500))
    첫_요청 = dict(적재를_지켜본다[0]["request_payload"])
    run_sales(_request(request_id="REQ-FIXED-0001", requested_quantity_kg=300))

    assert len(적재를_지켜본다) == 2
    assert 적재를_지켜본다[0]["request_payload"] == 첫_요청
    assert 적재를_지켜본다[0]["request_id"] == 적재를_지켜본다[1]["request_id"]


def test_적재_함수는_갱신이_아니라_추가다():
    """★ 저장소 계약을 이름으로 잠근다 — `record_sales` 는 UPDATE 를 부르지 않는다."""
    import inspect

    source = inspect.getsource(persistence.record_sales)

    assert "try_save_run" in source
    assert "UPDATE" not in source.upper()
