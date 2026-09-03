"""보낸 조정안이 **매입에 닿았는지**를 마스터가 대조한다.

2026-09-03 · 매입 `P-1` 답에서 나왔다.

🔴 **매입이 채우는데 마스터가 안 읽고 있었다.**

```text
app/purchase_agent/nodes/self_check.py:722
    "received_adjustments": len(state.get("adjustments") or [])
```

내가 여러 파트에 지적해 온 *"값을 실어 주고 안 쓴다"* 의 **정확한 반대편**이다.
조정안을 보내 놓고 **닿았는지를 아무도 안 봤다.**

⚠️ **이것은 "반영됐나" 가 아니라 "닿았나" 다.** 반영은 `applied_adjustments` 가
와야 알 수 있고 그 칸은 아직 없다 (매입 ①timing 에서 만든다). 셋이 다른 사실이다.

```text
그 지적이 매입에 갔는가     _exhausted_reason 이 소유한다
조정안이 닿았는가           이 파일이 잠근다
조정안이 반영됐는가         아직 알 수 없다
```
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contracts.core import SuggestedAdjustment
from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner

AS_OF = date(2025, 12, 31)
SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30_000_000}]


def _ctx() -> ExecutionContext:
    return ExecutionContext("REQ-20251231-0001", AS_OF, "USER_REQUEST", "v1.3")


def _adj(axis: str = "quantity", unit: str = "kg") -> SuggestedAdjustment:
    return SuggestedAdjustment(
        dept="inventory",
        axis=axis,  # type: ignore[arg-type]
        target_value=500.0,
        unit=unit,
        reason="사유",
        ref_ids=("REF-1",),
    )


def _advisor(
    adjustments: tuple[SuggestedAdjustment, ...],
    verdict: str,
    pre_adjustments: tuple[SuggestedAdjustment, ...] = (),
):
    """판정에서 조정안을 내는 조언자. **`reject` 여야 재호출이 일어난다.**

    ★ `pre_adjustments` 는 **경계 단계**에서 오는 조정안이다. 지금 그러는 부서는
      없지만 계약이 허용하고 `_collect_constraints:504` 가 버리지 않는다.
      **누적과 실려 나간 것이 갈리는 유일한 자리**라 여기서만 재현된다.
    """

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        common = {
            "request_id": request.context.request_id,
            "as_of": request.context.as_of,
            "agent": request.agent,
            "mode": request.mode,
            "run_id": run_id,
            "runtime_status": "READY",
        }
        if request.mode == "PRE_PURCHASE":
            reply = AgentReply(
                **common,
                business_status="ok",
                payload={"cap": 1},
                suggested_adjustments=pre_adjustments,
            )
        else:
            reply = AgentReply(
                **common,
                business_status=verdict,  # type: ignore[arg-type]
                suggested_adjustments=adjustments,
                needs_followup=bool(adjustments),
            )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    return port


def _purchaser(meta_by_call: dict[int, dict[str, Any]]):
    """회차별 `judgment.meta` 를 정하는 매입. **회차를 세어 답을 바꾼다.**"""
    calls: list[int] = []

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        calls.append(request.call_seq)
        run_id = f"PURCHASE-{request.call_seq}"
        payload: dict[str, Any] = {"scenarios": list(SCN)}
        meta = meta_by_call.get(len(calls))
        if meta is not None:
            payload["meta"] = meta
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="purchase",
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload=payload,
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent="purchase"
        )

    port.calls = calls  # type: ignore[attr-defined]
    return port


def _run(
    *,
    meta_by_call: dict[int, dict[str, Any]],
    verdict: str = "reject",
    pre_adjustments: tuple[SuggestedAdjustment, ...] = (),
):
    registry = AgentRegistry()
    registry.register("finance", _advisor((), "ok"))
    registry.register("inventory", _advisor((_adj(),), verdict, pre_adjustments))
    purchaser = _purchaser(meta_by_call)
    registry.register("purchase", purchaser)
    flow = ProcurementFlow(
        MasterRunner(_ctx(), registry, CallBudget(limit=12)), verifier=None, item="배추"
    )
    return flow.run(), purchaser


# ── ① 핵심 — 안 닿으면 드러난다 ────────────────────────────────────────────


def test_보낸_건수와_받은_건수가_다르면_드러난다():
    """🔴 **이 파일의 주장이다.** 전에는 아무 데서도 안 봤다."""
    outcome, purchaser = _run(meta_by_call={2: {"received_adjustments": 0}})

    assert len(purchaser.calls) == 2, "재호출이 일어나야 이 검사가 의미 있다"
    note = " ".join(outcome.concerns)
    assert "조정안 1건을 보냈는데 매입은 0건" in note, (
        f"안 닿은 것이 안 드러났다: {outcome.concerns}"
    )


def test_칸_자체가_없으면_모른다고_적는다():
    """🔴 **0 과 모름은 다르다** (§1.2-10).

    매입이 `received_adjustments` 를 아예 안 채우면 *"안 받았다"* 가 아니라
    **"닿았는지 알 수 없다"** 다. 앞으로 읽어 버리면 없는 문제를 만든다.
    """
    outcome, _ = _run(meta_by_call={2: {"feedback_attempt": 2}})

    note = " ".join(outcome.concerns)
    assert "알 수 없다" in note, f"모름을 0 으로 읽었다: {outcome.concerns}"
    assert "0건 받았다고" not in note


def test_수가_맞으면_아무것도_안_적는다():
    """대조군. 없으면 위 둘이 **항상 concern 을 내는 코드**로도 통과한다."""
    outcome, purchaser = _run(meta_by_call={2: {"received_adjustments": 1}})

    assert len(purchaser.calls) == 2
    assert not [c for c in outcome.concerns if "조정안" in c], (
        f"정상 전달에 concern 이 붙었다: {outcome.concerns}"
    )


def test_안_보낸_회차는_대조하지_않는다():
    """1회차에는 조정안이 안 실린다 — 그 회차를 *"0건 받았다"* 로 세면 오탐이다.

    ★ 조언자가 `ok` 를 내면 재호출이 없고, 1회차만 돌아 보낸 건수가 0 이다.
    """
    outcome, purchaser = _run(meta_by_call={1: {"received_adjustments": 0}}, verdict="ok")

    assert len(purchaser.calls) == 1, "재호출이 없어야 이 검사가 그 상황을 잰다"
    assert not [c for c in outcome.concerns if "조정안" in c], outcome.concerns


# ── ② 어느 층의 사실인지 ───────────────────────────────────────────────────


def test_닿았음과_반영됨을_뭉개지_않는다():
    """⚠️ 이 문장이 소유하는 것은 **"닿았나"** 하나다.

    매입이 1건 받았다고 적어도 그것을 **썼는지는 모른다** — `applied_adjustments`
    가 와야 안다. 문장이 그 이상을 말하면 안 된다.

    ★ `outcome.reason` 은 대상이 아니다. `_exhausted_reason` 의
      *"(반영 여부는 매입 회신에 달림)"* 은 **모른다고 밝히는** 정확한 문장이고,
      소유하는 사실이 다르다 (*"그 지적이 매입에 갔는가"*).
    """
    outcome, _ = _run(meta_by_call={2: {"received_adjustments": 0}})

    note = next(c for c in outcome.concerns if "조정안" in c)
    assert "반영" not in note, f"닿은 것을 반영으로 적었다: {note}"
    assert "빠진 것이 있다" in note, f"무슨 사실인지가 문장에 없다: {note}"


def test_findings_가_아니라_concerns_다():
    """매입을 다시 불러도 배선이 끊긴 사실은 그대로다 — 재시도할 것이 아니다.

    `04` 문서 §3.2 의 기준을 그대로 따른다.
    """
    outcome, _ = _run(meta_by_call={2: {"received_adjustments": 0}})

    assert any("조정안" in c for c in outcome.concerns)
    assert not any("조정안 1건을 보냈는데" in f for f in outcome.findings)


def test_누적이_아니라_이번에_실려_나간_것을_센다():
    """🔴 **경계 단계 조정안이 있으면 두 세는 법이 갈린다.**

    ```text
    self.suggested_adjustments   경계 + 판정을 누적한다
    payload["adjustments"]       feedback 이 있을 때만 실린다 (1회차에는 없다)
    ```

    누적을 세면 **1회차에 안 보낸 것을 보냈다고 세어** 없는 어긋남을 만든다.
    지금 경계에서 조정안을 내는 부서는 없지만 계약이 허용하고
    `_collect_constraints:504` 가 그것을 버리지 않는다.

    ★ 이 검사가 없으면 두 세는 법이 같은 값을 내서 **변이가 살아남는다**
      (실측 2026-09-03 — M6 이 그렇게 통과했다).
    """
    outcome, purchaser = _run(
        meta_by_call={1: {"received_adjustments": 0}},
        verdict="ok",
        pre_adjustments=(_adj(),),
    )

    assert len(purchaser.calls) == 1, "재호출이 없어야 1회차만 도는 상황이 된다"
    assert outcome.adjustments, "경계 조정안이 실제로 모여야 이 검사가 의미 있다"
    assert not [c for c in outcome.concerns if "조정안" in c], (
        f"1회차에 안 보낸 것을 보냈다고 셌다: {outcome.concerns}"
    )
