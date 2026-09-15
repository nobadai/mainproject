"""매입 Flow — 순서 · 종료 코드 · 부분 실패 · 재호출.

정의서 §3.4 의 순서가 **결정론**으로 도는지, 그리고 각 실패 상황이 정의된 종료 코드로
떨어지는지 고정한다.
"""

from __future__ import annotations

from dataclasses import replace
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


def purchaser(scenarios=None, runtime: str = "READY", reason: str = "", **top_level):
    """``top_level`` 은 제안 최상위 판정부 (situation·allowed_axes 등) 를 흉내낸다."""

    def port(request: AgentRequest):
        reply = _reply(
            request,
            runtime_status=runtime,
            business_status="ok" if runtime == "READY" else "skipped",
            payload={
                "scenarios": list(scenarios if scenarios is not None else SCN),
                **top_level,
            },
            reasoning=reason,
            missing_data=() if runtime != "RUNTIME_NOT_READY" else ("ml_forecast",),
        )
        return reply, _meta(request, reply)

    return port


def _registry(**ports) -> AgentRegistry:
    reg = AgentRegistry()
    for name, port in ports.items():
        reg.register(name, port)
    return reg


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


def test_판정부가_응답까지_온다():
    """#73 ③ — situation·allowed_axes 가 scenarios 옆에서 유실되던 회귀.

    프론트 판정 헤더가 소비하므로 **scenarios 를 뺀 제안 최상위 전부**가
    `judgment` 로 실려야 한다. 키를 화이트리스트로 고르지 않는다.
    """
    out = happy(
        purchase=purchaser(
            situation="uncertain",
            allowed_axes=["quantity", "timing"],
            confidence={"level": "medium"},
        )
    ).run()
    assert out.end_code == "E1_APPROVED"
    assert out.judgment["situation"] == "uncertain"
    assert out.judgment["allowed_axes"] == ["quantity", "timing"]
    assert out.judgment["confidence"] == {"level": "medium"}
    assert "scenarios" not in out.judgment  # 중복 적재 금지 — 시나리오는 자기 자리에


def test_안이_없어도_판정부는_남는다():
    """E2 에서도 no_proposal_reason 등 "왜 안이 없는지"가 응답에 남아야 한다."""
    out = happy(
        purchase=purchaser(scenarios=[], situation="stable", no_proposal_reason="밴드 소진")
    ).run()
    assert out.end_code == "E2_HELD"
    assert out.judgment["situation"] == "stable"
    assert out.judgment["no_proposal_reason"] == "밴드 소진"


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

    def verifier(scenarios, constraints, verdicts, plan, context=None):
        calls.append(len(scenarios))
        return VerificationResult(("E-IDENTITY",) if len(calls) == 1 else ())

    out = happy(verifier=verifier).run()
    assert out.end_code == "E1_APPROVED"
    assert out.purchase_attempts == 2
    assert not out.verification_skipped


def test_검증_발견이_안_풀리면_E3():
    out = happy(verifier=lambda s, c, v, p, ctx=None: VerificationResult(("E-IDENTITY",))).run()
    assert out.end_code == "E3_REJECTED"
    assert out.findings == ("E-IDENTITY",)


def test_검증은_제안전체와_경계와_판정과_계획을_받는다():
    seen = {}

    def verifier(proposal, constraints, verdicts, plan, context=None):
        # ★ 배열이 아니라 제안 전체다 — allowed_axes 가 최상위에 있다
        seen["scenarios"] = len(proposal["scenarios"])
        seen["top_level"] = sorted(proposal)
        seen["constraints"] = sorted(constraints)
        seen["verdicts"] = sorted(verdicts)
        seen["plan_steps"] = len(plan.steps)  # ④ M-16 이 읽는 것
        # ★ Critic 에 넘길 맥락 — as_of · 품목 · 조언자 근거
        seen["context_as_of"] = context.as_of.isoformat()
        seen["context_evidence_depts"] = sorted(context.evidences)
        return VerificationResult()

    happy(verifier=verifier).run()
    assert seen == {
        "scenarios": 2,
        "constraints": ["finance", "inventory"],
        "top_level": ["scenarios"],
        "verdicts": ["finance", "inventory"],
        "plan_steps": 5,
        "context_as_of": "2026-08-26",
        "context_evidence_depts": ["finance", "inventory"],
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

# ★ 타임존 필수 — 없으면 as_of 대조가 성립하지 않아 싣지 않는다 (매입 요청)
FORECAST = {"generated_at": "2026-08-26T06:00:00+09:00", "horizon_days": 18, "daily": []}


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
    future = {**FORECAST, "generated_at": "2026-08-27T06:00:00+09:00"}
    _flow_with(seen, forecast=future).run()
    assert "forecast" not in seen


def test_같은_날_생성은_싣는다():
    seen: dict = {}
    _flow_with(seen, forecast={**FORECAST, "generated_at": "2026-08-26T23:59:00+09:00"}).run()
    assert "forecast" in seen


def test_시점_필드가_없으면_판단하지_않는다():
    """매입이 수신 시 재검증한다 — 마스터가 임의로 막지 않는다."""
    seen: dict = {}
    _flow_with(seen, forecast={"horizon_days": 18}).run()
    assert "forecast" in seen


# ---------------------------------------------------------------------------
# 타임존 요구 (2026-08-27 매입 요청) — 위 세 건은 as_of 대조, 여기는 대조의 성립 조건
# ---------------------------------------------------------------------------


def test_타임존이_없으면_싣지_않는다():
    """★ 앞 10자만 비교하므로 오프셋이 없으면 **이 검사 자체가 성립하지 않는다.**

    `2026-08-26T23:59` 이 KST 로 08-26 인지 UTC 로 08-27 인지 갈리지 않는다.
    매입도 수신 시 거부하지만, 여기서 막으면 매입 호출 한 번을 아낀다.
    """
    seen: dict = {}
    _flow_with(seen, forecast={**FORECAST, "generated_at": "2026-08-26T06:00:00"}).run()
    assert "forecast" not in seen


def test_Z_표기도_타임존으로_인정한다():
    seen: dict = {}
    _flow_with(seen, forecast={**FORECAST, "generated_at": "2026-08-26T06:00:00Z"}).run()
    assert "forecast" in seen


# ---------------------------------------------------------------------------
# 품목 축 — ML 4품목 봉투 분해 (2026-08-27 결정 ⓑ)
#
# ML 은 하루 한 번 4품목을 한 봉투로 보내고 매입은 품목 하나씩 돈다.
# 그 사이를 마스터가 잇는다. **값을 만들지 않고 이름만 편다.**
# ---------------------------------------------------------------------------

_ENVELOPE = {
    "generated_at": "2026-08-26T06:00:00+09:00",
    "model_version": "lgbm-v1.2.0",
    "horizon_days": 18,
    "unit": "원/kg",
    "price_basis": "경락가",
    "size_class": "대표규격",
    "grade": "상",
    "items": {
        "배추": {"daily": [{"date": "2026-08-27", "predicted": 1671}]},
        "무": {"daily": [{"date": "2026-08-27", "predicted": 900}]},
    },
}


def test_품목을_주면_매입_Input_에_실린다():
    """매입 필수 4키 중 하나다 — 없으면 어댑터가 missing_data 로 막는다."""
    seen: dict = {}
    _flow_with(seen, item="배추").run()
    assert seen["item"] == "배추"


def test_4품목_봉투에서_그_품목만_꺼낸다():
    seen: dict = {}
    _flow_with(seen, item="배추", forecast=_ENVELOPE).run()
    forecast = seen["forecast"]
    assert forecast["daily"] == [{"date": "2026-08-27", "predicted": 1671}]
    assert forecast["item"] == "배추"
    assert "items" not in forecast  # 남의 품목이 따라 들어가지 않는다


def test_봉투_공통필드가_품목_블록으로_내려온다():
    """`price_basis`·`size_class`·`grade` 가 안 내려오면 매입이 대조할 값이 없다.

    상승률의 분자는 ML, 분모는 시세 실측이다. 시리즈가 어긋나면 **규격 차이를 가격
    변동으로 읽고 에러도 안 난다** — 매입이 거부하려면 이 셋을 받아야 한다.
    """
    seen: dict = {}
    _flow_with(seen, item="무", forecast=_ENVELOPE).run()
    forecast = seen["forecast"]
    assert forecast["price_basis"] == "경락가"
    assert forecast["size_class"] == "대표규격"
    assert forecast["grade"] == "상"
    assert forecast["model_version"] == "lgbm-v1.2.0"
    assert forecast["horizon_days"] == 18


def test_품목_블록이_봉투를_이긴다():
    envelope = {**_ENVELOPE, "items": {"배추": {"daily": [], "horizon_days": 7}}}
    seen: dict = {}
    _flow_with(seen, item="배추", forecast=envelope).run()
    assert seen["forecast"]["horizon_days"] == 7


def test_품목을_모르면_4품목_봉투를_싣지_않는다():
    """어느 품목인지 모르는 채로 넘기면 매입이 daily 를 못 찾는다.

    싣지 않으면 매입이 `missing_data: ["forecast"]` 를 내고 **그 사실이 이력에 남는다.**
    """
    seen: dict = {}
    _flow_with(seen, forecast=_ENVELOPE).run()
    assert "forecast" not in seen


def test_봉투에_없는_품목이면_싣지_않는다():
    """빈 dict 를 싣지 않는다 — 못 받은 것과 받았는데 빈 것은 다르다 (§1.2-10)."""
    seen: dict = {}
    _flow_with(seen, item="양파", forecast=_ENVELOPE).run()
    assert "forecast" not in seen


def test_평면_봉투는_그대로_넘긴다():
    """`items` 가 없는 현행 모양 — 품목 축 도입이 기존 경로를 깨지 않는다."""
    seen: dict = {}
    _flow_with(seen, item="배추", forecast=FORECAST).run()
    assert seen["forecast"]["horizon_days"] == 18
    assert "item" not in seen["forecast"]  # 평면 봉투에는 손대지 않는다


def test_품목_분해_전에_as_of_대조가_먼저다():
    """look-ahead 방어는 품목 축과 무관하게 봉투 층에서 끝난다."""
    seen: dict = {}
    future = {**_ENVELOPE, "generated_at": "2026-08-27T06:00:00+09:00"}
    _flow_with(seen, item="배추", forecast=future).run()
    assert "forecast" not in seen


# ---------------------------------------------------------------------------
# 부서 관측 운반 — 마스터는 읽지 않고 나른다
# ---------------------------------------------------------------------------


def test_부서_관측을_해석하지_않고_검증_맥락으로_나른다():
    """🔴 Critic 의 `E-AUTHORITY`·`E-GRADE-LEAK` 는 부서가 낸 DeptMeta 가 없으면
       아예 돌지 않는다. 마스터는 *"재무가 무엇을 읽었나"* 를 모르므로 **추측하지 않고**
       부서가 적어 보낸 관측을 그대로 검증 Tool 까지 옮긴다.
    """
    observation = '{"observation_type": "finance_dept_meta", "produced_fields": []}'

    def finance_port(request: AgentRequest):
        reply = _reply(request, payload={"finance_cap_amount_krw": 38_000_000})
        meta = _meta(request, reply)
        if request.mode == "PRE_PURCHASE":
            meta = replace(meta, observations=(observation,))
        return reply, meta

    seen: dict = {}

    def watching_verifier(proposal, constraints, verdicts, plan, context):
        seen["observations"] = dict(context.observations) if context else {}
        return VerificationResult()

    happy(finance=finance_port, verifier=watching_verifier).run()

    assert seen["observations"]["finance"] == (observation,)
    # 물류는 관측을 안 냈으므로 목록에 없다 — 빈 값을 지어내지 않는다.
    assert "inventory" not in seen["observations"]


def test_경계를_못_낸_부서의_관측은_나르지_않는다():
    """경계에 기여하지 못한 회신의 관측을 넘기면, 안 쓴 값이 검증 근거가 된다."""

    def finance_port(request: AgentRequest):
        reply = _reply(
            request,
            runtime_status="RUNTIME_NOT_READY",
            business_status="skipped",
            missing_data=("finance_state",),
        )
        meta = replace(_meta(request, reply), observations=("{}",))
        return reply, meta

    runner = MasterRunner(
        ctx(),
        _registry(finance=finance_port, inventory=advisor(), purchase=purchaser()),
        CallBudget(limit=12),
    )
    procurement = ProcurementFlow(runner)
    procurement.run()

    assert procurement._boundary_observations() == {}


def test_매입이_미가동이면_조언자_SCENARIO_VALIDATION_을_부르지_않는다():
    """★ 매입이 안을 못 냈으면 **검증할 시나리오가 없다.**

    그래도 부르면 조언자는 `RUNTIME_NOT_READY` payload 를 시나리오로 읽으려다
    ERROR 를 내고, 그 ERROR 가 *"재무가 고장났다"* 로 보인다 — 실제로는 매입이
    미가동인 것이다. 원인이 한 칸 밀려 기록되면 다음 사람이 엉뚱한 데를 판다.
    """
    called: list[tuple[str, str]] = []

    def watching(agent: str):
        def port(request: AgentRequest):
            called.append((agent, request.mode))
            return advisor()(request)

        return port

    out = happy(
        purchase=purchaser(runtime="RUNTIME_NOT_READY", reason="예측 없음"),
        finance=watching("finance"),
        inventory=watching("inventory"),
    ).run()

    assert out.end_code == "E4_NOT_STARTED"
    assert out.blocked_by == ("purchase",)
    # 경계는 받았지만 시나리오 판정은 아무도 부르지 않았다.
    assert ("finance", "PRE_PURCHASE") in called
    assert not [item for item in called if item[1] == "SCENARIO_VALIDATION"], called


def test_시나리오가_0개여도_SCENARIO_VALIDATION_을_부르지_않는다():
    """매입이 READY 로 답했지만 안이 없는 경우도 같다 — 검증할 것이 없다."""
    called: list[str] = []

    def watching(request: AgentRequest):
        called.append(request.mode)
        return advisor()(request)

    out = happy(
        purchase=purchaser(scenarios=[], reason="밴드 소진"), finance=watching
    ).run()

    assert out.end_code == "E2_HELD"
    assert "SCENARIO_VALIDATION" not in called
