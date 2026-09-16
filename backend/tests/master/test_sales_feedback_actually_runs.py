"""되먹임이 **실제로 돈다.**

```text
1차 제안 → 재무 판정 FAIL + 금액 상한 대안 → 마스터 되먹임 → 판매 재계획
```

🔴 **실측에서 이 길은 한 번도 돈 적이 없다.** 판매 요청 9,937 건 전부
  `feedback_attempt == 0` 이었고, 이유는 재무 판매 검증이 조정안을 **항상 빈
  목록**으로 돌려줬기 때문이다. 마스터는 *"부서가 낸 대안이 없으면 다시 물어도
  같다"* 로 접는다 (`sales_flow._run`).

★ **재무가 낸 대안을 여기서 지어내지 않는다.** 실제 `build_sales_adjustments` 를
  불러 만든다 — 지어내면 이 검사는 마스터 배선만 재고, 정작 막혀 있던 자리는
  그대로 남는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contracts.core import SuggestedAdjustment
from app.finance.capabilities.sales import CREDIT_LIMIT_EXCEEDED, build_sales_adjustments
from app.finance.execution import _adjustment_from_dict
from app.master import AgentRegistry, CallBudget, ExecutionContext, MasterRunner
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.sales_flow import SalesFlow
from tests.master.logistics_pre_sales import PRE_SALES_PAYLOAD

AS_OF = date(2026, 9, 16)


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="SREQ-FB-1",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3-PROVISIONAL",
        sim_run_id="SIM-TEST",
    )


def _reply(request: AgentRequest, **kw) -> AgentReply:
    base: dict[str, Any] = {
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


def _meta(request: AgentRequest, reply: AgentReply) -> ExecutionMetadata:
    return ExecutionMetadata(
        run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
    )


def _scenario(scenario_id: str) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "required_validations": ["FINANCIAL_VALIDATION"],
        "delivery_date": "2026-09-18",
        "payment_days": 30,
    }


#: 재무 판매 검증 Tool 이 실제로 내는 모양. **여신이 모자란 안 하나.**
def _validation_payload(scenario_id: str) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "status": "EVALUATED",
        "finance_verdict": "FAIL",
        "reason_codes": [CREDIT_LIMIT_EXCEEDED],
        "max_finance_allowed_amount_krw": 6_000_000,
        "financial_summary": {"recalculated_sales_amount_krw": 14_500_000},
        "evidence_refs": ["RCV-1001"],
    }


def _finance_port(*, with_adjustment: bool = True):
    def port(request: AgentRequest):
        if request.mode == "PRE_SALES_FACTS":
            reply = _reply(request, payload={"available_cash": 31_000_000.0})
            return reply, _meta(request, reply)
        scenario_id = str(request.payload.get("scenario_id") or "")
        payload = _validation_payload(scenario_id)
        if not with_adjustment:
            # 계산할 수 없는 실패 — 상한을 못 센다.
            payload["max_finance_allowed_amount_krw"] = None
        adjustments: tuple[SuggestedAdjustment, ...] = tuple(
            _adjustment_from_dict(item) for item in build_sales_adjustments(payload)
        )
        reply = _reply(
            request,
            business_status="reject",
            payload=payload,
            suggested_adjustments=adjustments,
            reasoning=f"{scenario_id} 여신 초과",
        )
        return reply, _meta(request, reply)

    return port


def _sales_port(capture: list[dict[str, Any]]):
    def port(request: AgentRequest):
        capture.append(dict(request.payload))
        reply = _reply(request, payload={"scenarios": [_scenario("SCN-1")], "situation": "다"})
        return reply, _meta(request, reply)

    return port


def _logistics_port():
    def port(request: AgentRequest):
        reply = _reply(request, payload=PRE_SALES_PAYLOAD)
        return reply, _meta(request, reply)

    return port


def _run(*, with_adjustment: bool = True) -> tuple[Any, list[dict[str, Any]]]:
    보낸것: list[dict[str, Any]] = []
    registry = AgentRegistry()
    registry.register("inventory", _logistics_port())
    registry.register("sales", _sales_port(보낸것))
    registry.register("finance", _finance_port(with_adjustment=with_adjustment))
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=25))
    return SalesFlow(runner, user_request={"item": "배추", "partner_id": "CUST-1"}).run(), 보낸것


# ---------------------------------------------------------------------------


def test_재무_대안이_있으면_되먹임이_실제로_돈다():
    """🔴 `feedback_attempt > 0` — 실측에서 0 건이던 경로다."""
    outcome, 보낸것 = _run()

    assert outcome.feedback_attempts > 0, "재무가 대안을 냈는데도 되먹임이 안 돌았다"
    assert 보낸것[1]["feedback_attempt"] == 1
    assert len(보낸것) > 1, "판매를 한 번만 불렀다"


def test_되먹임_회차에_재무_회신_원본이_실린다():
    """마스터가 자기 문장을 지어 보내지 않는다 — 부서 회신을 통째로 나른다."""
    _outcome, 보낸것 = _run()

    replies = 보낸것[1]["feedback"]["domain_replies"]
    재무회신 = next(r for r in replies if r["source_agent"] == "finance")
    assert 재무회신["payload"]["reason_codes"] == [CREDIT_LIMIT_EXCEEDED]
    assert 재무회신["payload"]["suggested_adjustments"][0]["target_value"] == 6_000_000


def test_되먹임_회차의_전략_계획도_회신을_본다():
    """재계획에서 자세를 다시 고를 때 부서 회신이 실제로 손에 있어야 한다."""
    from app.sales.proposal import _all_feedback_replies
    from app.sales.schemas import SalesProposalInput

    _outcome, 보낸것 = _run()
    재계획 = SalesProposalInput.model_validate(
        {
            "business_mode": "SPOT_SALES",
            "user_request": {"item": "배추", "partner_id": "CUST-1", "requested_quantity_kg": 100},
            "is_refeed": True,
            "feedback_attempt": 보낸것[1]["feedback_attempt"],
            "feedback": 보낸것[1]["feedback"],
        }
    )

    assert _all_feedback_replies(재계획), "재계획이 부서 회신을 못 본다"


def test_상한을_못_세면_되먹임을_억지로_만들지_않는다():
    """★ 기존 경로가 그대로 끝난다 — 다시 물어도 같은 실패는 다시 묻지 않는다."""
    outcome, 보낸것 = _run(with_adjustment=False)

    assert outcome.feedback_attempts == 0
    assert len(보낸것) == 1
    assert outcome.end_code == "SL3_ALL_REJECTED"
