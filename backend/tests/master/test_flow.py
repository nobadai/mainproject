"""매입 Flow — 순서 · 종료 코드 · 부분 실패 · 재호출.

정의서 §3.4 의 순서가 **결정론**으로 도는지, 그리고 각 실패 상황이 정의된 종료 코드로
떨어지는지 고정한다.
"""

from __future__ import annotations

from datetime import date

from app.master import (
    AgentRegistry,
    AgentReply,
    AgentRequest,
    CallBudget,
    ExecutionContext,
    ExecutionMetadata,
    MasterRunner,
)
from app.master.flow import ProcurementFlow
from app.master.verifier import VerificationResult

AS_OF = date(2026, 8, 26)

SCN = [
    {"scenario_id": "SCN-1", "total_amount_krw": 30000000},
    {"scenario_id": "SCN-2", "total_amount_krw": 38000000},
]


def ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-1",
        as_of=AS_OF,
        trigger="ML_COMPLETE",
        policy_version="v1.3-PROVISIONAL",
    )


def _reply(request: AgentRequest, **kw) -> AgentReply:
    base = {
        "request_id": request.context.request_id,
        "as_of": request.context.as_of,
        "agent": request.agent,
        "mode": request.mode,
        "run_id": f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}",
        "runtime_status": "READY",
        "business_status": "ok",
    }
    base.update(kw)
    return AgentReply(**base)


def _meta(request: AgentRequest, reply: AgentReply) -> ExecutionMetadata:
    return ExecutionMetadata(
        run_id=reply.run_id,
        request_id=request.context.request_id,
        agent=request.agent,
        used_tools=("tool_a",),
        tool_order=(1,),
    )


def advisor(
    *,
    pre_purchase: dict | None = None,
    validation_status: str = "ok",
    runtime: str = "READY",
    missing: tuple[str, ...] = (),
):
    """재무·물류 역할 포트."""

    def port(request: AgentRequest):
        if request.mode == "PRE_PURCHASE":
            if runtime != "READY":
                reply = _reply(
                    request,
                    runtime_status=runtime,
                    business_status="skipped",
                    missing_data=missing or ("x",),
                )
            else:
                reply = _reply(request, payload=pre_purchase or {"cap": 1})
        else:
            reply = _reply(request, business_status=validation_status)
        return reply, _meta(request, reply)

    return port


def purchaser(scenarios=None, runtime: str = "READY", reason: str = ""):
    def port(request: AgentRequest):
        reply = _reply(
            request,
            runtime_status=runtime,
            business_status="ok" if runtime == "READY" else "skipped",
            payload={"scenarios": list(scenarios if scenarios is not None else SCN)},
            reasoning=reason,
            missing_data=() if runtime != "RUNTIME_NOT_READY" else ("ml_forecast",),
        )
        return reply, _meta(request, reply)

    return port


def flow(budget: int = 12, verifier=None, max_attempts: int = 2, **ports) -> ProcurementFlow:
    reg = AgentRegistry()
    for name, port in ports.items():
        reg.register(name, port)
    runner = MasterRunner(ctx(), reg, CallBudget(limit=budget))
    return ProcurementFlow(runner, verifier=verifier, max_purchase_attempts=max_attempts)


def happy(**over) -> ProcurementFlow:
    ports = {
        "finance": advisor(),
        "inventory": advisor(),
        "purchase": purchaser(),
    }
    ports.update({k: v for k, v in over.items() if k in ("finance", "inventory", "purchase")})
    kw = {k: v for k, v in over.items() if k not in ("finance", "inventory", "purchase")}
    return flow(**kw, **ports)


# ---------------------------------------------------------------------------
# 정상 경로 — 순서가 §3.4 대로 도는가
# ---------------------------------------------------------------------------


def test_정상_경로는_E1():
    out = happy().run()
    assert out.end_code == "E1_APPROVED"
    assert out.presentable
    assert len(out.scenarios) == 2


def test_호출_순서가_정의서_3_4_와_같다():
    out = happy().run()
    assert out.plan.signature == (
        ("finance", "PRE_PURCHASE", 1),
        ("inventory", "PRE_PURCHASE", 1),
        ("purchase", "GENERATE_SCENARIOS", 1),
        ("finance", "SCENARIO_VALIDATION", 1),
        ("inventory", "SCENARIO_VALIDATION", 1),
    )


def test_같은_입력에_같은_실행_계획():
    assert happy().run().plan.signature == happy().run().plan.signature


def test_경계를_묶어서만_넘긴다():
    """마스터는 받은 값을 해석·재계산하지 않는다 (§3.2.2)."""
    seen = {}

    def watching(request: AgentRequest):
        if request.mode == "GENERATE_SCENARIOS":
            seen.update(request.payload)
        return purchaser()(request)

    happy(
        finance=advisor(pre_purchase={"finance_cap_amount_krw": 38000000}), purchase=watching
    ).run()
    assert seen["constraints"]["finance"] == {"finance_cap_amount_krw": 38000000}


# ---------------------------------------------------------------------------
# 부분 실패 — 제약 하나가 빠진 시나리오를 만들지 않는다
# ---------------------------------------------------------------------------


def test_조언자_하나가_미가동이면_매입을_부르지_않는다():
    called = []

    def counting(request: AgentRequest):
        called.append(request.mode)
        return purchaser()(request)

    out = happy(
        inventory=advisor(runtime="RUNTIME_NOT_READY", missing=("warehouse_capacity",)),
        purchase=counting,
    ).run()

    assert out.end_code == "E4_NOT_STARTED"
    assert out.blocked_by == ("inventory",)
    assert called == []  # 매입 LLM 비용을 쓰지 않는다


def test_미가동_사유가_결과에_남는다():
    out = happy(inventory=advisor(runtime="ERROR")).run()
    assert "inventory" in out.reason


def test_매입_자신이_미가동이면_E4():
    out = happy(purchase=purchaser(runtime="RUNTIME_NOT_READY", reason="예측 없음")).run()
    assert out.end_code == "E4_NOT_STARTED"
    assert out.blocked_by == ("purchase",)


# ---------------------------------------------------------------------------
# 종료 코드
# ---------------------------------------------------------------------------


def test_시나리오_0개면_보류():
    out = happy(purchase=purchaser(scenarios=[], reason="밴드 소진")).run()
    assert out.end_code == "E2_HELD"
    assert out.reason == "밴드 소진"


def test_납품_의무가_있으면_같은_상황이_E5():
    """사지 않는 것과 못 지키는 것은 다르다 (§5.3)."""
    out = happy(purchase=purchaser(scenarios=[])).run(has_unmet_obligation=True)
    assert out.end_code == "E5_NO_FEASIBLE_PLAN"


def test_reject_가_계속되면_재호출_후_E3():
    out = happy(finance=advisor(validation_status="reject")).run()
    assert out.end_code == "E3_REJECTED"
    assert out.purchase_attempts == 2


def test_conditional_은_통과시킨다():
    """마스터는 최적안을 고르는 자리가 아니다 — 사람이 보고 정한다."""
    out = happy(finance=advisor(validation_status="conditional")).run()
    assert out.end_code == "E1_APPROVED"
    assert out.purchase_attempts == 1


def test_예산_소진은_E3_로_바뀐다():
    # 위로 새면 사이클이 죽는다 (§1.2-12)
    out = happy(budget=3).run()
    assert out.end_code == "E3_REJECTED"
    assert "예산" in out.reason


# ---------------------------------------------------------------------------
# 검증 Tool
# ---------------------------------------------------------------------------


def test_검증_미주입은_건너뛴_사실이_남는다():
    """검사하지 못한 것을 검사했다고 말하지 않는다 (설계서 §8)."""
    out = happy().run()
    assert out.verification_skipped
    assert out.findings == ()


def test_검증_발견이_있으면_재호출한다():
    calls = []

    def verifier(scenarios, constraints, verdicts, plan):
        calls.append(len(scenarios))
        return VerificationResult(("E-IDENTITY",) if len(calls) == 1 else ())

    out = happy(verifier=verifier).run()
    assert out.end_code == "E1_APPROVED"
    assert out.purchase_attempts == 2
    assert not out.verification_skipped


def test_검증_발견이_안_풀리면_E3():
    out = happy(verifier=lambda s, c, v, p: VerificationResult(("E-IDENTITY",))).run()
    assert out.end_code == "E3_REJECTED"
    assert out.findings == ("E-IDENTITY",)


def test_검증은_시나리오와_경계와_판정을_함께_받는다():
    seen = {}

    def verifier(scenarios, constraints, verdicts, plan):
        seen["scenarios"] = len(scenarios)
        seen["constraints"] = sorted(constraints)
        seen["verdicts"] = sorted(verdicts)
        seen["plan_steps"] = len(plan.steps)   # ④ M-16 이 읽는 것
        return VerificationResult()

    happy(verifier=verifier).run()
    assert seen == {
        "scenarios": 2,
        "constraints": ["finance", "inventory"],
        "verdicts": ["finance", "inventory"],
        "plan_steps": 5,
    }


# ---------------------------------------------------------------------------
# 단일안 — 보여줄지는 미결(M-5)이라 사실만 드러낸다
# ---------------------------------------------------------------------------


def test_시나리오가_하나면_단일안으로_표시된다():
    out = happy(purchase=purchaser(scenarios=[SCN[0]])).run()
    assert out.end_code == "E1_APPROVED"
    assert out.single_option


def test_둘_이상이면_단일안이_아니다():
    assert not happy().run().single_option


# ---------------------------------------------------------------------------
# §3.2.5 예외 — ML 예측 · 확정주문 · 정책값은 마스터가 싣는다 (매입 파트 요청)
# ---------------------------------------------------------------------------

FORECAST = {"generated_at": "2026-08-26T06:00:00", "horizon_days": 18, "daily": []}


def _watch_purchase_input(seen: dict):
    def port(request: AgentRequest):
        if request.mode == "GENERATE_SCENARIOS":
            seen.clear()
            seen.update(request.payload)
        return purchaser()(request)

    return port


def _flow_with(seen: dict, **kw) -> ProcurementFlow:
    reg = AgentRegistry()
    reg.register("finance", advisor())
    reg.register("inventory", advisor())
    reg.register("purchase", _watch_purchase_input(seen))
    runner = MasterRunner(ctx(), reg, CallBudget(limit=12))
    return ProcurementFlow(runner, **kw)


def test_예측과_주문과_정책값이_매입_Input_에_실린다():
    seen: dict = {}
    _flow_with(
        seen,
        forecast=FORECAST,
        confirmed_orders={"total_kg": 5000},
        policy_values={"contract_price_krw": 1900},
    ).run()
    assert seen["forecast"]["horizon_days"] == 18
    assert seen["confirmed_orders"]["total_kg"] == 5000
    assert seen["policy_values"]["contract_price_krw"] == 1900


def test_안_주면_안_싣는다():
    seen: dict = {}
    _flow_with(seen).run()
    assert set(seen) == {"constraints"}


def test_예측_생성시각이_as_of_이후면_싣지_않는다():
    """look-ahead 는 에러를 내지 않고 손익만 좋아진다 (§1.2-6)."""
    seen: dict = {}
    future = {**FORECAST, "generated_at": "2026-08-27T06:00:00"}
    _flow_with(seen, forecast=future).run()
    assert "forecast" not in seen


def test_같은_날_생성은_싣는다():
    seen: dict = {}
    _flow_with(seen, forecast={**FORECAST, "generated_at": "2026-08-26T23:59:00"}).run()
    assert "forecast" in seen


def test_시점_필드가_없으면_판단하지_않는다():
    """매입이 수신 시 재검증한다 — 마스터가 임의로 막지 않는다."""
    seen: dict = {}
    _flow_with(seen, forecast={"horizon_days": 18}).run()
    assert "forecast" in seen
