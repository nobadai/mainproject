"""판정 채점 — **매입의 주장이 그 상황에서 맞았는가** (MT-11).

★ **부서 경계는 가짜, 매입은 진짜다.** 상황(창고 여유·자금 상한·확정 주문)은 내가
  정하고, 그 상황에서 **실제 매입 에이전트가 무슨 주장을 하는지** 채점한다.
  매입까지 가짜로 두면 내 가짜를 채점하는 셈이라 아무것도 검증하지 못한다.

★ **검증 Tool 을 끄고 돈다.** 여기서 보는 것은 *"매입의 결론이 맞나"* 이지
  *"근거 형식이 맞나"* 가 아니다. 후자는 Critic 56검사와 마스터 14검사가 이미 본다.

★ **DB 를 타지 않는다.** 기준선을 상수로 굳혔다 (`judgment_cases.BASE_*`).

⚠️ **매입 LLM 이 켜져 있어도 결과가 흔들리면 안 되는 것만 본다** — 종료 코드와
  수량·금액이다. 문장·근거 표현은 채점하지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.ports import AgentRegistry
from app.master.runner import MasterRunner
from app.purchase_agent.adapter import purchase_port
from tests.master.judgment_cases import (
    AS_OF,
    BASE_FINANCE,
    BASE_INVENTORY,
    CASES,
    POLICY,
    UNLABELED,
    JudgmentCase,
    Outcome,
    forecast,
    orders,
)


def advisor_port(payload: dict[str, Any]):
    """상황을 그대로 답하는 부서. **판정(`SCENARIO_VALIDATION`)은 통과시킨다.**

    부서 판정까지 상황에 넣으면 채점 대상이 둘이 된다 — 여기서 재는 것은 매입이다.
    """

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        pre = request.mode == "PRE_PURCHASE"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            payload=payload if pre else {"verdict": "ok"},
        )
        meta = ExecutionMetadata(
            run_id=reply.run_id,
            request_id=request.context.request_id,
            agent=request.agent,
            used_tools=("fixture",),
            tool_order=(1,),
        )
        return reply, meta

    return port


def run_case(case: JudgmentCase) -> Outcome:
    """상황을 세우고 **진짜 매입**을 돌린다."""
    registry = AgentRegistry()
    registry.register("finance", advisor_port({**BASE_FINANCE, **case.finance}))
    registry.register("inventory", advisor_port({**BASE_INVENTORY, **case.inventory}))
    registry.register("purchase", purchase_port)

    context = ExecutionContext(
        request_id=f"REQ-LABEL-{abs(hash(case.name)) % 10000:04d}",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )
    outcome = ProcurementFlow(
        MasterRunner(context, registry, CallBudget(limit=12)),
        verifier=None,  # 결론을 보는 자리다 — 근거 형식은 Critic 이 본다
        item="배추",
        forecast=case.forecast_override or forecast(),
        confirmed_orders=case.confirmed_orders or orders(3_000),
        policy_values=POLICY,
    ).run()

    return Outcome(
        end_code=outcome.end_code,
        labels=tuple(str(s.get("label", "")) for s in outcome.scenarios),
        total_qty_kg=tuple(int(s.get("total_qty_kg") or 0) for s in outcome.scenarios),
        amounts_krw=tuple(int(s.get("total_amount_krw") or 0) for s in outcome.scenarios),
    )


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_상황마다_주장이_맞는가(case: JudgmentCase):
    """🔴 **형식이 아니라 결론을 채점한다.**

    근거를 다 달고 창고보다 많이 사는 안은 Critic 을 통과한다 — 형식이 완벽하기
    때문이다. 그걸 잡는 자리가 여기다.
    """
    out = run_case(case)
    failure = case.check(out)

    assert failure is None, (
        f"[{case.axis}] {case.name}\n"
        f"  → {failure}\n"
        f"  정답 근거: {case.why}\n"
        f"  실제: {out.end_code} · 안 {list(zip(out.labels, out.total_qty_kg, strict=False))}"
    )


def test_모든_라벨에_근거가_붙어_있다():
    """멘토가 *"데이터 라벨링과 **그 이유가 명확한지**"* 를 함께 물었다.

    정답만 있고 근거가 없으면 **나중에 그 라벨이 맞는지 아무도 검증하지 못한다.**
    라벨셋이 늘어날 때 근거 없이 붙는 것을 막는다.
    """
    for case in CASES:
        assert case.why.strip(), f"{case.name} 에 정답 근거가 없다"
        assert case.axis.strip(), f"{case.name} 에 검증 축이 없다"


def test_못_라벨한_것을_감추지_않는다():
    """🔴 **없는 것을 라벨하면 그 라벨이 거짓이 된다.**

    멘토가 예시로 든 셋 중 **날씨는 데이터가 없고 가격 타이밍은 정답이 없다.**
    라벨셋이 *"비교 검증셋을 다 만들었다"* 로 읽히지 않도록 사유와 함께 남긴다
    (§3.7.6 — 못 한 것을 한 척하지 않는다).
    """
    topics = {name for name, _ in UNLABELED}

    assert "날씨" in topics
    assert all(reason.strip() for _, reason in UNLABELED)
    # 라벨된 축과 못 라벨한 축이 겹치지 않는다
    assert topics.isdisjoint({c.axis for c in CASES})


def test_채점기가_공허하게_통과하지_않는다():
    """🔴 **안이 하나도 없으면 "상한을 안 넘었다" 가 자동으로 참이 된다.**

    라벨셋이 죽는 가장 흔한 길이다 — 전부 `E2_HELD` 로 떨어지는 시스템이 만점을
    받는다. 최소 한 케이스는 **실제로 안을 내면서** 상한을 지켜야 한다.
    """
    clipping = next(c for c in CASES if "창고가 상한" in c.name)
    out = run_case(clipping)

    assert out.labels, "클리핑 케이스가 안을 내지 못했다 — 채점이 공허하다"
    assert out.max_qty == 500, f"창고 상한 500kg 에 정확히 붙어야 한다 (실제 {out.max_qty})"
    # 수요 10,000kg 를 따라갔다면 20배를 샀을 것이다
    assert out.max_qty < 10_000


def test_채점식은_틀린_결과를_실제로_잡는다():
    """테스트의 테스트. **통과만 확인하면 채점기가 죽어도 모른다.**"""
    from tests.master.judgment_cases import clipped_to, must_propose, no_plan_over

    over = Outcome(end_code="E1_APPROVED", labels=("공격",), total_qty_kg=(9_000,), amounts_krw=())
    assert no_plan_over(500)(over) is not None
    assert clipped_to(500)(over) is not None

    empty = Outcome(end_code="E2_HELD", labels=(), total_qty_kg=(), amounts_krw=())
    assert clipped_to(500)(empty) is not None  # 안이 없으면 잡는다
    assert must_propose(empty) is not None
