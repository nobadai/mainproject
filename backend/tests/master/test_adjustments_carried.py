"""부서가 낸 조정안이 **개수가 아니라 내용으로** 화면까지 간다.

2026-09-02. 물류 회신을 코드로 대조하다 나온 구멍이다 — 물류 잘못이 아니라 마스터
쪽이었다.

```text
전   "suggested_adjustments": len(reply.suggested_adjustments)   개수만
후   부서가 보낸 표준형 객체를 그대로 나른다
```

🔴 **사실이 아주 사라진 것은 아니어서 더 위험했다.** 같은 내용이
`verdicts[].payload` 에 부서 원시형으로 남아 있어서, *"값이 있으니 되겠지"* 로
넘어가면 **마스터가 남의 payload 를 파게 된다** — 그것이 §3.2.2 위반이고,
표준형은 그 해석을 안 하려고 만든 자리다.

★ 되먹임 계약 §3.2 의 `constraint` 가 바로 이 배열이라, 개수만 남기면 되먹임을
  붙이는 순간 나를 값이 없다.

★ `replans`(9/2 오전) · `evidences`(9/2 오전) 에 이은 **세 번째**다. 부서가 보내 준
  것을 마스터가 받는 자리에서 잃는 패턴.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contracts.core import SuggestedAdjustment
from app.master.answer import facts_from_procurement
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
    agent_dept,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.service import _to_response

AS_OF = date(2025, 12, 31)

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _adj(
    dept: str, axis: str, value: float, unit: str, reason: str = "사유"
) -> SuggestedAdjustment:
    return SuggestedAdjustment(
        dept=dept,  # type: ignore[arg-type]
        axis=axis,  # type: ignore[arg-type]
        target_value=value,
        unit=unit,
        reason=reason,
        ref_ids=("REF-SNAP-1",),
    )


def _advisor(
    *,
    validation_adjustments: tuple[SuggestedAdjustment, ...] = (),
    pre_purchase_adjustments: tuple[SuggestedAdjustment, ...] = (),
    verdict: str = "ok",
):
    """조정안을 실어 보내는 조언자."""

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        if request.mode == "PRE_PURCHASE":
            reply = AgentReply(
                request_id=request.context.request_id,
                as_of=request.context.as_of,
                agent=request.agent,
                mode=request.mode,
                run_id=run_id,
                runtime_status="READY",
                business_status="ok",
                payload={"cap": 1},
                suggested_adjustments=pre_purchase_adjustments,
            )
        else:
            reply = AgentReply(
                request_id=request.context.request_id,
                as_of=request.context.as_of,
                agent=request.agent,
                mode=request.mode,
                run_id=run_id,
                runtime_status="READY",
                business_status=verdict,  # type: ignore[arg-type]
                suggested_adjustments=validation_adjustments,
                needs_followup=bool(validation_adjustments),
            )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _purchaser(scenarios: list[dict[str, Any]] | None = None):
    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"PURCHASE-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="purchase",
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload={"scenarios": list(SCN if scenarios is None else scenarios)},
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent="purchase"
        )
        return reply, meta

    return port


def _flow(**ports: Any) -> ProcurementFlow:
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    return ProcurementFlow(runner, verifier=None, item="피마늘")


def _run(**over: Any):
    ports: dict[str, Any] = {
        "finance": _advisor(),
        "inventory": _advisor(),
        "purchase": _purchaser(),
    }
    ports.update(over)
    return _flow(**ports).run()


# ── ① 객체가 남는다 ─────────────────────────────────────────────────────────


def test_조정안이_개수가_아니라_객체로_남는다():
    """🔴 되먹임 §3.2 의 `constraint` 가 이 배열이다 — 개수로는 못 만든다."""
    run = _run(
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "quantity", 7120.0, "kg"),),
            verdict="conditional",
        )
    )

    assert len(run.adjustments) == 1
    got = run.adjustments[0]
    assert got.dept == "inventory"
    assert got.axis == "quantity"
    assert got.target_value == 7120.0


def test_개수를_두_곳에서_관리하지_않는다():
    """`verdicts` 에 개수를 남겨 두면 같은 사실의 주인이 둘이 된다."""
    run = _run(
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "timing", 3.0, "d"),), verdict="conditional"
        )
    )

    assert "suggested_adjustments" not in run.verdicts["inventory"]


def test_마스터가_값을_고치지_않는다():
    """§3.2.2 — 부서가 낸 것 그대로다. 단위도 사유도 참조도."""
    original = _adj("inventory", "timing", 3.0, "d", "도착일을 2026-01-03 로 조정 제안")
    run = _run(inventory=_advisor(validation_adjustments=(original,), verdict="conditional"))

    assert run.adjustments[0] == original


def test_순서를_바꾸지_않는다():
    """정렬하면 그것이 우선순위로 읽힌다 — 근거와 같은 이유다."""
    first = _adj("inventory", "quantity", 9000.0, "kg", "첫째")
    second = _adj("inventory", "timing", 1.0, "d", "둘째")
    run = _run(inventory=_advisor(validation_adjustments=(first, second), verdict="conditional"))

    assert [a.reason for a in run.adjustments] == ["첫째", "둘째"]


def test_응답_변환도_순서를_안_바꾼다():
    """🔴 **결과만 보고 끝내면 변이가 안 걸린다.**

    지난주 근거(`_evidences_out`)에서 똑같이 밟았다 — `outcome` 만 검사하고 응답
    변환은 안 봐서, 거기서 정렬해도 초록불이었다. **층마다 잠근다.**

    값이 큰 것부터/작은 것부터 어느 쪽으로 정렬해도 걸리게 순서를 어긋나게 둔다.
    """
    first = _adj("inventory", "quantity", 9000.0, "kg", "첫째")
    second = _adj("inventory", "timing", 1.0, "d", "둘째")
    run = _run(inventory=_advisor(validation_adjustments=(first, second), verdict="conditional"))
    response = _to_response(_ctx(), run)

    assert [a.reason for a in response.adjustments] == ["첫째", "둘째"]
    assert [a.target_value for a in response.adjustments] == [9000.0, 1.0]


def test_두_부서가_내면_둘_다_남는다():
    run = _run(
        finance=_advisor(
            validation_adjustments=(_adj("finance", "amount", 18000000.0, "krw"),),
            verdict="conditional",
        ),
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "quantity", 7120.0, "kg"),),
            verdict="conditional",
        ),
    )

    assert {a.dept for a in run.adjustments} == {"finance", "inventory"}


# ── ② 안 온 날과 못 낸 날 ───────────────────────────────────────────────────


def test_0건이_정답인_날이_있다():
    """물류는 `reject` 안의 조정을 승격하지 않는다 (#121 · 2026-09-02 물류 확정).

    **0건을 실패로 읽지 않는다.** 빈 튜플이지 없는 칸이 아니다.
    """
    run = _run(inventory=_advisor(validation_adjustments=(), verdict="reject"))

    assert run.adjustments == ()


def test_경계_단계_조정안도_버리지_않는다():
    """지금은 오지 않지만 **온다면 버리지 않는다.**

    부서가 무엇을 보내도 되는지는 봉투가 정하지 마스터가 정하지 않는다.
    """
    run = _run(
        inventory=_advisor(pre_purchase_adjustments=(_adj("inventory", "quantity", 500.0, "kg"),))
    )

    assert [a.target_value for a in run.adjustments] == [500.0]


def test_안이_안_나온_날에도_싣는다():
    """*"무엇을 고쳐야 하나"* 가 필요한 날이 바로 안이 없는 날이다."""
    run = _run(
        purchase=_purchaser(scenarios=[]),
        inventory=_advisor(pre_purchase_adjustments=(_adj("inventory", "quantity", 500.0, "kg"),)),
    )

    assert run.end_code in ("E2_HELD", "E5_NO_FEASIBLE_PLAN")
    assert len(run.adjustments) == 1, "실패한 날 조정안을 버리면 고칠 단서가 사라진다"


# ── ③ 응답·이력까지 간다 ────────────────────────────────────────────────────


def test_응답_변환에서_사라지지_않는다():
    run = _run(
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "quantity", 7120.0, "kg", "창고 부족"),),
            verdict="conditional",
        )
    )
    response = _to_response(_ctx(), run)

    assert len(response.adjustments) == 1
    out = response.adjustments[0]
    assert (out.dept, out.axis) == ("inventory", "quantity")
    assert (out.target_value, out.unit) == (7120.0, "kg")
    assert out.reason == "창고 부족"
    assert out.ref_ids == ["REF-SNAP-1"]


def test_응답_변환이_값을_반올림하지_않는다():
    """화면과 원본이 다른 숫자를 말하면 안 된다.

    ★ 정수만 넣고 검사하면 반올림해도 안 걸린다 — **소수를 쓴다.**
    """
    run = _run(
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "quantity", 7120.5, "kg"),),
            verdict="conditional",
        )
    )
    response = _to_response(_ctx(), run)

    assert response.adjustments[0].target_value == 7120.5


def test_이력에_적재되는_모양에_들어간다():
    """`master_agent_runs.response_payload` 는 이 덤프 그대로다.

    🔴 화면 문구가 *"실행 이력에서 보십시오"* 라고 안내하던 그곳이다 - 전에는
      거기에 조정안 칸이 아예 없었다.
    """
    run = _run(
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "quantity", 7120.0, "kg"),),
            verdict="conditional",
        )
    )
    dumped = _to_response(_ctx(), run).model_dump(mode="json")

    assert dumped["adjustments"], "이력에 안 실리면 가서 봐도 없다"
    assert dumped["adjustments"][0]["target_value"] == 7120.0


# ── ④ 발화문 ────────────────────────────────────────────────────────────────


def test_발화문이_없는_곳을_가리키지_않는다():
    """전에는 `"({N}건) — 실행 이력에서 보십시오"` 였고 거기에 없었다."""
    run = _run(
        inventory=_advisor(
            validation_adjustments=(_adj("inventory", "quantity", 7120.0, "kg", "창고 부족"),),
            verdict="conditional",
        )
    )
    facts = facts_from_procurement(_to_response(_ctx(), run))
    적힌_것 = " ".join(facts.gaps)

    assert "실행 이력에서 보십시오" not in 적힌_것
    assert "7120kg" in 적힌_것
    assert "창고 부족" in 적힌_것


# ── ⑤ 어휘를 섞지 않는다 ────────────────────────────────────────────────────


def test_에이전트_이름과_부서_이름의_주인이_하나다():
    """🔴 지금 글자가 같다 — 그래서 그냥 비교해도 통하고, 그래서 위험하다.

    이번 주에 이름으로 재다 세 번 틀렸다 (`llm_attempts` · `runtime_status` ·
    회차 분할). 매핑의 주인을 하나 두고 거기를 거친다.
    """
    assert agent_dept("finance") == "finance"
    assert agent_dept("inventory") == "inventory"
    assert agent_dept("purchase") is None, "매입은 축 조정을 제안할 수 없다"
