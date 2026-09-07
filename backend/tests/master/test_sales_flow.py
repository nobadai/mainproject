"""판매 Flow 골격 — 어휘 · 라우팅 · 되먹임 · 종료 코드.

매입 `test_flow.py` 와 **같은 것을 재지 않는다.** 판매가 매입과 갈리는 자리만 고정한다.

```text
① Capability 어휘가 판매 것과 한 벌인가      두 벌이 되면 라우팅이 조용히 갈린다
② 못 부르는 요구가 "안 왔다" 로 보이는가      조용히 건너뛰면 "검증됐다" 로 읽힌다
③ 밴드가 없는가                              물류가 못 답해도 시작은 한다
④ 통과 후보가 있으면 되먹임하지 않는가        C-1 — 걸면 볼 수 있던 안이 사라진다
⑤ 예산 소진이 탈락으로 접히지 않는가          매입과 일부러 다른 자리다
```

🔴 **①이 이 파일의 이유 중 하나다.** 마스터는 `app.sales.schemas` 를 런타임에 읽지
  않으므로(조정자가 부서 스키마에 묶이지 않으려고), 어휘가 갈려도 **런타임에는 아무
  소리가 안 난다.** 갈린 날 생기는 일은 예외가 아니라 *"그 요구는 라우팅이 없다"* 이고,
  그건 화면에서 정상 동작과 구별되지 않는다. 여기서만 양쪽을 import 해 대조한다.
"""

from __future__ import annotations

from datetime import date
from typing import get_args

from app.contracts.core import EndCode, SuggestedAdjustment
from app.master import (
    AgentRegistry,
    AgentReply,
    AgentRequest,
    CallBudget,
    ExecutionContext,
    ExecutionMetadata,
    MasterRunner,
)
from app.master.envelope import (
    CAPABILITY_ROUTING,
    Capability,
    agent_allowed_modes,
    route_capability,
)
from app.master.sales_flow import (
    MAX_FEEDBACK_ATTEMPTS,
    SALES_BUDGET,
    SalesEndCode,
    SalesFlow,
    sales_call_budget,
)

AS_OF = date(2026, 9, 6)


def ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="SREQ-1",
        as_of=AS_OF,
        trigger="USER_REQUEST",
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


def adjustment(dept: str = "finance") -> SuggestedAdjustment:
    """부서가 낸 **권위 있는 대안** 하나."""
    return SuggestedAdjustment(
        dept=dept,
        axis="amount",
        target_value=18000000.0,
        unit="KRW",
        reason="마진이 안 선다",
        ref_ids=("FIN-1",),
    )


def scenario(scenario_id: str, *capabilities: str) -> dict:
    """판매 후보 하나 — `required_validations` 는 **후보 단위**다 (설계 정정 ①)."""
    return {"scenario_id": scenario_id, "required_validations": list(capabilities)}


# ── 가짜 포트 ────────────────────────────────────────────────────────────────


def logistics(runtime: str = "READY", adjustments: tuple = ()):
    """물류 — `PRE_SALES` 초기 컨텍스트."""

    def port(request: AgentRequest):
        if runtime != "READY":
            reply = _reply(
                request,
                runtime_status=runtime,
                business_status="skipped",
                missing_data=("sellable_stock",),
                reasoning="판매가능 재고 조회 실패",
            )
        else:
            reply = _reply(
                request,
                payload={"sellable": "yes"},
                suggested_adjustments=adjustments,
            )
        return reply, _meta(request, reply)

    return port


def seller(rounds: list[list[dict]], capture: list | None = None, runtime: str = "READY"):
    """판매 — 회차마다 다른 후보를 낸다. 마지막 회차 목록이 이후에 반복된다."""
    seen = {"n": 0}

    def port(request: AgentRequest):
        index = min(seen["n"], len(rounds) - 1)
        seen["n"] += 1
        if capture is not None:
            capture.append(dict(request.payload))
        if runtime != "READY":
            reply = _reply(
                request,
                runtime_status=runtime,
                business_status="skipped",
                missing_data=("partner_credit",),
                reasoning="거래처 정보를 못 읽었다",
            )
        else:
            reply = _reply(request, payload={"scenarios": list(rounds[index]), "situation": "다"})
        return reply, _meta(request, reply)

    return port


def financier(verdicts: dict[str, str] | None = None, adjustments: tuple = ()):
    """재무 — `SALES_VALIDATION`. **후보 하나씩** 받는다 (설계 정정 ②)."""

    def port(request: AgentRequest):
        scenario_id = str(request.payload.get("scenario_id") or "")
        status = (verdicts or {}).get(scenario_id, "ok")
        reply = _reply(
            request,
            business_status=status,
            reasoning=f"{scenario_id} 판정",
            suggested_adjustments=adjustments if status == "reject" else (),
        )
        return reply, _meta(request, reply)

    return port


def flow(
    budget: int = SALES_BUDGET, max_attempts: int = MAX_FEEDBACK_ATTEMPTS, **ports
) -> SalesFlow:
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(ctx(), registry, CallBudget(limit=budget))
    return SalesFlow(runner, max_feedback_attempts=max_attempts)


def happy(**over) -> SalesFlow:
    ports = {
        "inventory": logistics(),
        "sales": seller([[scenario("SCN-1", "FINANCIAL_VALIDATION")]]),
        "finance": financier(),
    }
    ports.update({k: v for k, v in over.items() if k in ("inventory", "sales", "finance")})
    kw = {k: v for k, v in over.items() if k not in ("inventory", "sales", "finance")}
    return flow(**kw, **ports)


# ---------------------------------------------------------------------------
# ① Capability 어휘 — 마스터 것과 판매 것이 한 벌인가
# ---------------------------------------------------------------------------


def test_마스터_capability_어휘가_판매_것과_같다():
    """🔴 **런타임에는 이것이 갈려도 아무 소리가 안 난다.**

    마스터는 `app.sales.schemas` 를 import 하지 않는다 — 조정자가 부서 스키마에 묶이면
    부서가 자기 파일을 고치는 날 마스터가 같이 깨진다 (재무 `ApprovedCommitmentFacts`
    를 Protocol 로 받은 것과 같은 이유). 그 대가로 **어휘가 두 벌**이 되므로, 갈린 날을
    잡는 자리가 여기밖에 없다.

    ★ 테스트에서는 양쪽을 읽어도 된다 — 런타임 의존이 아니다.
    """
    from app.sales.schemas import SalesCapability

    assert set(get_args(Capability)) == set(get_args(SalesCapability))


def test_라우팅표가_capability_를_하나도_빠뜨리지_않는다():
    """빠지면 `route_capability` 가 `None` 을 내고 **못 부르는 것으로 읽힌다.**

    부를 수 있는데 표에 안 적어 못 부르는 것과, 정말 부를 대상이 없는 것은 다르다.
    표를 어휘 전체로 강제해 **빠뜨림이 `None` 으로 위장하지 못하게** 한다.
    """
    assert set(CAPABILITY_ROUTING) == set(get_args(Capability))


def test_매입_추가공급은_부를_대상이_없다():
    """🔴 매입 호출 단위(batch / ONE_BY_ONE) 미회신 — **`None` 이 그 사실이다.**

    값을 채우면 마스터가 매입 대신 호출 단위를 정하는 것이 된다.
    """
    assert CAPABILITY_ROUTING["ADDITIONAL_SUPPLY_CONTEXT"] is None


def test_라우팅_대상이_그_모드를_실제로_받는다():
    """표에 적힌 `(agent, mode)` 가 봉투에서 거부되면 **호출 순간에 터진다.**

    표는 손으로 쓰는 자리라 `("finance", "SCENARIO_VALIDATION")` 같은 오타가 조용히 산다.
    """
    for capability, route in CAPABILITY_ROUTING.items():
        if route is None:
            continue
        agent, mode = route
        assert mode in agent_allowed_modes(agent), capability


def test_모르는_capability_는_터지지_않고_None_이다():
    """어휘가 갈린 날 `KeyError` 로 사이클을 죽이지 않는다 — 봉투 규칙과 같은 자리."""
    assert route_capability("WHAT_IS_THIS") is None


# ---------------------------------------------------------------------------
# ② 종료 코드 — 매입과 층이 다르다 (D-3)
# ---------------------------------------------------------------------------


def test_판매_종료코드는_매입_것과_한_글자도_겹치지_않는다():
    """겹치면 이력에서 **어느 사이클의 종료인지** 를 payload 로 되짚어야 한다."""
    assert set(get_args(SalesEndCode)) & set(get_args(EndCode)) == set()


def test_예산_소진은_탈락으로_접히지_않는다():
    """🔴 매입은 `E3_REJECTED` 로 접는다 (`flow.py:378-380`). **판매는 안 접는다.**

    *"다 봤는데 안 된다"* 와 *"다 못 봤다"* 를 같은 코드로 만들면, 사용자는 앞으로 읽고
    조건을 바꾼다 — 실제로는 같은 조건으로 다시 돌리는 것이 맞다.
    """
    out = happy(budget=2).run()  # inventory 1 + sales 1 → finance 에서 끊긴다

    assert out.end_code == "SL5_BUDGET_EXHAUSTED"
    assert not out.presentable


# ---------------------------------------------------------------------------
# ③ 정상 경로 — 통과 후보가 있으면 제시한다
# ---------------------------------------------------------------------------


def test_통과_후보가_있으면_제시한다():
    out = happy().run()

    assert out.end_code == "SL1_PRESENTED"
    assert out.presentable
    assert [c.scenario_id for c in out.presented] == ["SCN-1"]


def test_통과_후보가_있으면_되먹임하지_않는다():
    """🔴 C-1 — 전체 되먹임을 걸면 **통과했던 안까지 바뀌어** 볼 수 있던 안이 사라진다.

    더 나은 안은 사용자가 `RERUN_WITH_CONDITION` 으로 요청한다 (C-2).
    """
    out = happy(
        sales=seller(
            [
                [
                    scenario("SCN-1", "FINANCIAL_VALIDATION"),
                    scenario("SCN-2", "FINANCIAL_VALIDATION"),
                ]
            ]
        ),
        finance=financier({"SCN-2": "reject"}, adjustments=(adjustment(),)),
    ).run()

    assert out.end_code == "SL1_PRESENTED"
    assert out.feedback_attempts == 0
    assert out.plan.call_count("sales", "GENERATE_SALES_PROPOSAL") == 1


def test_탈락안_사유가_함께_나간다():
    """부분 통과가 정상이다 — 떨어진 안도 **왜 떨어졌는지와 함께** 화면에 남는다."""
    out = happy(
        sales=seller(
            [
                [
                    scenario("SCN-1", "FINANCIAL_VALIDATION"),
                    scenario("SCN-2", "FINANCIAL_VALIDATION"),
                ]
            ]
        ),
        finance=financier({"SCN-2": "reject"}),
    ).run()

    assert [c.scenario_id for c in out.rejected] == ["SCN-2"]
    assert "SCN-2 판정" in out.rejected[0].detail


def test_조건부_판정은_통과에_남는다():
    """마스터는 최적안을 고르는 자리가 아니다 — `conditional` 은 사람이 보고 정한다."""
    out = happy(finance=financier({"SCN-1": "conditional"})).run()

    assert out.end_code == "SL1_PRESENTED"


def test_판정을_안_낸_것은_통과가_아니다():
    """`skipped` 는 *"조건부로 괜찮다"* 가 아니라 **판정을 안 낸 것**이다 (#173)."""
    out = happy(finance=financier({"SCN-1": "skipped"})).run()

    assert out.end_code == "SL3_ALL_REJECTED"


# ---------------------------------------------------------------------------
# ④ 밴드가 없다 — 물류가 못 답해도 시작은 한다
# ---------------------------------------------------------------------------


def test_물류가_못_답해도_판매를_부른다():
    """🔴 매입 `band_is_formed` 를 쓰지 않는다.

    매입은 조언자가 하나라도 빠지면 시작조차 안 하지만, 판매는 물류가 없으면 **후보의
    질이 떨어질 뿐** 시작은 된다 (설계 §1-2).
    """
    out = happy(inventory=logistics(runtime="RUNTIME_NOT_READY")).run()

    assert out.plan.call_count("sales", "GENERATE_SALES_PROPOSAL") == 1
    assert out.end_code == "SL1_PRESENTED"


def test_물류가_못_답한_사실이_결과에_남는다():
    """멈추지 않는 것과 **없던 일로 하는 것**은 다르다 (§1.2-10)."""
    out = happy(inventory=logistics(runtime="ERROR")).run()

    assert out.context_failure is not None
    assert out.context_failure.agent == "inventory"
    assert "판매가능 재고 조회 실패" in out.context_failure.detail


def test_물류가_못_답하면_컨텍스트_칸을_안_만든다():
    """빈 값을 실으면 판매가 *"팔 수 있는 게 없다고 했다"* 로 읽는다 (§1.2-10)."""
    보낸것: list[dict] = []
    happy(
        inventory=logistics(runtime="RUNTIME_NOT_READY"),
        sales=seller([[scenario("SCN-1")]], capture=보낸것),
    ).run()

    assert "supply_context" not in 보낸것[0]


def test_물류가_답하면_컨텍스트를_실어_보낸다():
    보낸것: list[dict] = []
    happy(sales=seller([[scenario("SCN-1")]], capture=보낸것)).run()

    assert 보낸것[0]["supply_context"]["payload"] == {"sellable": "yes"}


# ---------------------------------------------------------------------------
# ⑤ 라우팅 — 후보 단위 · S-1 재사용 · 못 부르는 요구
# ---------------------------------------------------------------------------


def test_재무는_후보마다_한_번씩_불린다():
    """🔴 재무 `SALES_VALIDATION` 은 배열을 안 받는다 (설계 정정 ②).

    `parse_sales_validation_input` 이 읽는 것이 전부 단수라 **후보 3개면 3번**이다.
    이 사실이 판매 예산 16 의 절반을 만든다.
    """
    out = happy(
        sales=seller(
            [
                [
                    scenario("SCN-1", "FINANCIAL_VALIDATION"),
                    scenario("SCN-2", "FINANCIAL_VALIDATION"),
                    scenario("SCN-3", "FINANCIAL_VALIDATION"),
                ]
            ]
        )
    ).run()

    assert out.plan.call_count("finance", "SALES_VALIDATION") == 3


def test_물류_컨텍스트는_후보마다_다시_부르지_않는다():
    """S-1 기여 호출 재사용 — 같은 `as_of`, 같은 요청 안이라 다시 불러도 같다."""
    out = happy(
        sales=seller(
            [
                [
                    scenario("SCN-1", "SELLABLE_SUPPLY_CONTEXT"),
                    scenario("SCN-2", "DELIVERY_FEASIBILITY_CONTEXT"),
                ]
            ]
        )
    ).run()

    assert out.plan.call_count("inventory", "PRE_SALES") == 1
    assert out.end_code == "SL1_PRESENTED"


def test_부를_대상이_없는_요구는_결과에_실린다():
    """🔴 조용히 건너뛰면 *"검증됐다"* 로 읽힌다 — **"안 왔다" 로 보여야 한다.**"""
    out = happy(sales=seller([[scenario("SCN-1", "ADDITIONAL_SUPPLY_CONTEXT")]])).run()

    assert out.unroutable_capabilities == ("ADDITIONAL_SUPPLY_CONTEXT",)


def test_부를_대상이_없는_요구를_한_후보는_통과가_아니다():
    """다른 요구가 전부 통과해도 미해결로 둔다 — 못 물어본 것을 물어봤다고 하지 않는다."""
    out = happy(
        sales=seller([[scenario("SCN-1", "FINANCIAL_VALIDATION", "ADDITIONAL_SUPPLY_CONTEXT")]])
    ).run()

    assert out.presented == ()
    assert out.end_code == "SL3_ALL_REJECTED"
    assert "ADDITIONAL_SUPPLY_CONTEXT" in out.rejected[0].detail


def test_요구가_없는_후보는_검증_0건으로_표시된다():
    """마스터가 요구를 지어내지 않는다 (§3.2.2). 대신 **안 본 안이라는 사실**이 남는다."""
    out = happy(sales=seller([[scenario("SCN-1")]])).run()

    assert out.end_code == "SL1_PRESENTED"
    assert out.presented[0].unvalidated


def test_후보를_그대로_보낸다():
    """마스터가 골라 담으면 판매가 필드를 늘린 날 조용히 빠진다 (§3.2.2)."""
    후보 = {**scenario("SCN-1", "FINANCIAL_VALIDATION"), "quantity_kg": "1000"}
    본것: list[dict] = []

    def 재무(request: AgentRequest):
        본것.append(dict(request.payload))
        reply = _reply(request)
        return reply, _meta(request, reply)

    happy(sales=seller([[후보]]), finance=재무).run()

    assert 본것[0]["quantity_kg"] == "1000"


# ---------------------------------------------------------------------------
# ⑥ 되먹임 — 최대 2회 · 권위 있는 대안이 있을 때만
# ---------------------------------------------------------------------------


def test_통과_후보가_0이면_되먹임한다():
    보낸것: list[dict] = []
    out = happy(
        sales=seller([[scenario("SCN-1", "FINANCIAL_VALIDATION")]], capture=보낸것),
        finance=financier({"SCN-1": "reject"}, adjustments=(adjustment(),)),
    ).run()

    assert out.end_code == "SL3_ALL_REJECTED"
    assert out.feedback_attempts == MAX_FEEDBACK_ATTEMPTS
    assert 보낸것[1]["feedback_context"]["feedback_attempt"] == 1


def test_되먹임_상한을_넘기지_않는다():
    """상한이 2 면 판매 호출은 **최초 1 + 되먹임 2 = 3** 이다. 예산 16 이 이 수를 받친다."""
    out = happy(
        sales=seller([[scenario("SCN-1", "FINANCIAL_VALIDATION")]]),
        finance=financier({"SCN-1": "reject"}, adjustments=(adjustment(),)),
    ).run()

    assert MAX_FEEDBACK_ATTEMPTS == 2, "상한을 바꾸면 예산 산식(SALES_BUDGET)도 다시 센다"
    assert out.plan.call_count("sales", "GENERATE_SALES_PROPOSAL") == 3


def test_되먹임에_부서가_낸_대안이_실린다():
    """🔴 안 실으면 **같은 입력으로 다시 돌린다** — 매입이 `#169` 에서 고친 자리다."""
    보낸것: list[dict] = []
    happy(
        sales=seller([[scenario("SCN-1", "FINANCIAL_VALIDATION")]], capture=보낸것),
        finance=financier({"SCN-1": "reject"}, adjustments=(adjustment(),)),
    ).run()

    assert [a["axis"] for a in 보낸것[1]["adjustments"]] == ["amount"]
    assert 보낸것[1]["feedback_context"]["rejected"][0]["scenario_id"] == "SCN-1"


def test_권위_있는_대안이_없으면_되먹임하지_않는다():
    """🔴 C-2 — 실을 것이 없는데 다시 부르면 호출 예산과 LLM 만 태운다 (§1.2-12)."""
    out = happy(
        sales=seller([[scenario("SCN-1", "FINANCIAL_VALIDATION")]]),
        finance=financier({"SCN-1": "reject"}),
    ).run()

    assert out.end_code == "SL3_ALL_REJECTED"
    assert out.feedback_attempts == 0
    assert out.plan.call_count("sales", "GENERATE_SALES_PROPOSAL") == 1


def test_되먹임_뒤_통과하면_제시한다():
    out = happy(
        sales=seller(
            [
                [scenario("SCN-1", "FINANCIAL_VALIDATION")],
                [scenario("SCN-2", "FINANCIAL_VALIDATION")],
            ]
        ),
        finance=financier({"SCN-1": "reject"}, adjustments=(adjustment(),)),
    ).run()

    assert out.end_code == "SL1_PRESENTED"
    assert out.feedback_attempts == 1
    assert [c.scenario_id for c in out.presented] == ["SCN-2"]


# ---------------------------------------------------------------------------
# ⑦ 시작 못 함 · 후보 없음
# ---------------------------------------------------------------------------


def test_mock_입력이면_아무도_안_부른다():
    """한 번이라도 부르면 그 회신이 이력에 남고 **"돌긴 돌았다"** 로 읽힌다."""
    registry = AgentRegistry()
    registry.register("inventory", logistics())
    runner = MasterRunner(ctx(), registry, sales_call_budget())
    out = SalesFlow(runner, mocked_inputs=("sellable_stock",)).run()

    assert out.end_code == "SL4_NOT_STARTED"
    assert out.plan.steps == []
    assert "sellable_stock" in out.reason


def test_어댑터가_없으면_SL4():
    """**미등록은 오류가 아니라 상태다** (§5.3). 배선 전에도 그것을 정확히 말한다."""
    runner = MasterRunner(ctx(), AgentRegistry(), sales_call_budget())
    out = SalesFlow(runner).run()

    assert out.end_code == "SL4_NOT_STARTED"
    assert "inventory" in out.reason


def test_후보가_없으면_SL2():
    out = happy(sales=seller([[]])).run()

    assert out.end_code == "SL2_NO_CANDIDATE"
    assert not out.presentable


def test_판매가_못_돌면_SL2_이고_사유가_실린다():
    """🔴 `SL4` 가 아니다 — `SL4` 는 *"부르기 전에 못 섰다"* 이고 여기는 부른 뒤다."""
    out = happy(sales=seller([[]], runtime="RUNTIME_NOT_READY")).run()

    assert out.end_code == "SL2_NO_CANDIDATE"
    assert "거래처 정보를 못 읽었다" in out.reason
    assert "partner_credit" in out.reason


# ---------------------------------------------------------------------------
# ⑧ 예산 — 사이클이 다르면 예산도 다르다
# ---------------------------------------------------------------------------


def test_판매_예산은_매입_기본값과_다르다():
    """🔴 매입 12 를 건드리지 않는다. 올리면 매입이 안 쓰는 상한이 매입 쪽에서 풀린다."""
    from app.master.schemas import ProcurementRunRequest

    assert SALES_BUDGET == 16
    assert ProcurementRunRequest.model_fields["budget"].default == 12


def test_최악_경우_호출수가_기본_예산_안에_든다():
    """산식을 주석이 아니라 **식으로** 잠근다 — 상한을 바꾸면 여기서 먼저 걸린다."""
    회차 = MAX_FEEDBACK_ATTEMPTS + 1
    후보 = 3
    최악 = 1 + 회차 + 후보 * 회차  # 물류 1 + 판매 회차 + 재무(후보 × 회차)

    assert 최악 == 13
    assert 최악 <= SALES_BUDGET


def test_판매_예산은_한_자리에서_받는다():
    """진입점이 `CallBudget(limit=16)` 을 각자 쓰면 그 순간 주인이 여럿이 된다."""
    assert sales_call_budget().limit == SALES_BUDGET


def test_최악_경우가_실제로_예산_안에서_끝난다():
    """수식만이 아니라 **돌려서** 확인한다 — 후보 3 · 되먹임 2회를 기본 예산으로."""
    out = flow(
        inventory=logistics(),
        sales=seller(
            [
                [
                    scenario("SCN-1", "FINANCIAL_VALIDATION"),
                    scenario("SCN-2", "FINANCIAL_VALIDATION"),
                    scenario("SCN-3", "FINANCIAL_VALIDATION"),
                ]
            ]
        ),
        finance=financier(
            {"SCN-1": "reject", "SCN-2": "reject", "SCN-3": "reject"},
            adjustments=(adjustment(),),
        ),
    ).run()

    assert out.end_code == "SL3_ALL_REJECTED"  # SL5 가 아니다 — 예산이 모자라지 않았다
    assert len(out.plan.steps) == 13
