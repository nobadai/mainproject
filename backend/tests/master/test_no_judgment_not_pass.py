"""판정하지 않은 것을 **통과로 세지 않는다.**

2026-09-02 · #173.

🔴 `_acceptable` 이 통과를 **부정형**으로 정했다.

```python
return all(v.get("business_status") != "reject" for v in verdicts.values())
```

*"기각이 아니면 통과"* 이므로 **어휘가 늘 때마다 새 값이 통과 쪽으로 샜다.**
2026-09-02 에 실제로 하나 늘었다 — 재무가 `SALES_VALIDATION` 을 내면서
`READY + skipped`(`INPUT_INCOMPLETE`) 가 생겼고, 재무 코드가 마스터가 어떻게 읽을지
까지 적어 뒀다: *"마스터는 재무가 정상 판정한 것으로 읽는다."*

★ **이 파일이 잠그는 주장은 하나다** — 판정 안 한 것이 *"검토 통과"* 로 사람에게
  올라가지 않는다.

⚠️ **그 다음에 무엇을 하는가는 `runtime_status` 가 가른다** (물류·재무 회신).
  부서 쪽 사정이면 매입을 다시 불러도 같고, `READY` 면 제안이 바뀌면 채워질 수 있다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master.answer import agent_label
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

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _advisor(*, verdict: str = "ok", runtime: str = "READY", reasoning: str = ""):
    """경계는 늘 정상으로 내고 **판정 단계에서만** 상태를 바꾼다.

    ★ 이렇게 해야 밴드가 서고 매입이 불린다. 경계에서 `runtime` 을 떨어뜨리면
      `band_is_formed` 가 먼저 잡아 E4 로 끝나 — **판정 게이트를 못 본다.**
      물류 스냅샷 조회 실패가 실제로 이 모양이다 (판정 때 터진다).

    ⚠️ **`RUNTIME_NOT_READY` 에는 `missing_data` 가 반드시 있어야 한다** (M-1 §5.1).
      없으면 봉투가 계약 위반으로 죽고, `_invoke` 가 그것을 `ERROR` 회신으로 바꿔
      **두 갈래를 가르는 검사가 둘 다 `ERROR` 를 보게 된다.** 처음 이 파일을 쓸 때
      실제로 그렇게 통과할 뻔했다 — 스텁을 우회하지 않고 고친다.
    """
    not_ready = runtime == "RUNTIME_NOT_READY"

    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        pre = request.mode == "PRE_PURCHASE"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY" if pre else runtime,  # type: ignore[arg-type]
            business_status="ok" if pre else verdict,  # type: ignore[arg-type]
            payload={"cap": 1} if pre else {},
            reasoning="" if pre else reasoning,
            missing_data=() if pre or not not_ready else ("logistics_snapshot",),
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


class _Purchaser:
    """부른 횟수를 센다. `scenarios=False` 면 **낼 안이 없다**고 답한다."""

    def __init__(self, *, scenarios: bool = True) -> None:
        self.calls = 0
        self.scenarios = scenarios

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.calls += 1
        run_id = f"PURCHASE-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="purchase",
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            # 🔴 매입 실제 동작 그대로다 (`purchase_agent/adapter.py:892`) —
            #    안이 없으면 `READY + skipped` 를 보낸다.
            business_status="ok" if self.scenarios else "skipped",
            payload={"scenarios": list(SCN)} if self.scenarios else {},
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent="purchase"
        )
        return reply, meta


def _run(*, purchaser: _Purchaser | None = None, **over: Any):
    purchaser = purchaser or _Purchaser()
    ports: dict[str, Any] = {
        "finance": _advisor(),
        "inventory": _advisor(),
        "purchase": purchaser,
    }
    ports.update({k: v for k, v in over.items() if k in ("finance", "inventory")})
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    flow = ProcurementFlow(runner, item="배추")
    return flow.run(), purchaser


def _acceptable(verdicts: dict[str, Any]) -> bool:
    return ProcurementFlow._acceptable(None, (), verdicts, ())  # type: ignore[arg-type]


# ── ① 통과 판정이 허용목록이다 ──────────────────────────────────────────────


def test_판정을_안_낸_부서가_있으면_통과가_아니다():
    """`skipped` 는 *"조건부로 괜찮다"* 가 아니라 **판정을 안 낸 것**이다."""
    assert _acceptable({"finance": {"business_status": "skipped"}}) is False


def test_어휘_밖의_값도_통과가_아니다():
    """모르는 값을 통과로 읽느니 통과가 아니라고 읽는다 (#162 에서 드러낸 fail-open)."""
    assert _acceptable({"inventory": {"business_status": "FAIL"}}) is False


def test_빈_값도_통과가_아니다():
    """칸이 아예 비어 오는 것도 판정이 아니다 — `None` 과 빈 문자열 둘 다."""
    assert _acceptable({"finance": {"business_status": None}}) is False
    assert _acceptable({"finance": {}}) is False


def test_ok_와_conditional_은_통과다():
    """🔴 **회귀** — `conditional` 이 막히면 계약 §3.4 가 깨진다.

    마스터는 최적안을 고르는 자리가 아니다. 조언자 하나가 `conditional` 을 내도
    사람이 보고 정한다. **이 검사가 허용목록을 너무 좁히는 것을 막는다.**
    """
    assert _acceptable({"finance": {"business_status": "ok"}}) is True
    assert _acceptable({"finance": {"business_status": "conditional"}}) is True
    assert (
        _acceptable(
            {
                "finance": {"business_status": "ok"},
                "inventory": {"business_status": "conditional"},
            }
        )
        is True
    )


def test_기각은_여전히_통과가_아니다():
    """🔴 **회귀** — 원래 막던 것을 계속 막는다."""
    assert _acceptable({"inventory": {"business_status": "reject"}}) is False


# ── ② 그 다음에 무엇을 하나 — runtime_status 가 가른다 ──────────────────────


def test_부서_쪽_사정이면_매입을_다시_부르지_않는다():
    """판정할 사실이 없는 것은 **새 안을 줘도 같다.** 재호출은 예산만 태운다.

    `#159` 에서 잘못된 `trigger` 하나로 LLM 6회를 태운 것과 같은 종류다.
    """
    outcome, purchaser = _run(
        inventory=_advisor(verdict="skipped", runtime="ERROR", reasoning="스냅샷 조회 실패")
    )

    assert purchaser.calls == 1, "다시 부르면 안 된다"
    assert outcome.end_code == "E2_HELD"


def test_READY_인_판정_없음은_재호출_경로에_남는다():
    """🔴 **범위를 좁힌 자리다** (재무 회신 2026-09-02).

    `INPUT_INCOMPLETE` 은 *"제안에 사실이 빠진 것"* 이라 **재무 고장이 아니다.**
    매입이 새 안을 내면 채워질 수 있으므로 여기서 끝내면 안 된다 —
    **고쳐질 수 있는 것을 안 고치고 끝내는 셈**이 된다.
    """
    outcome, purchaser = _run(finance=_advisor(verdict="skipped", runtime="READY"))

    assert purchaser.calls == 2, "제안이 바뀌면 채워질 수 있으므로 다시 부른다"
    assert outcome.end_code == "E3_REJECTED", "재호출을 다 쓴 것이지 보류가 아니다"


def test_기각은_재호출된다():
    """🔴 **회귀** — 판정이 난 기각은 매입이 고칠 수 있다."""
    _, purchaser = _run(inventory=_advisor(verdict="reject", reasoning="창고가 없다"))

    assert purchaser.calls == 2


# ── ③ 사유 문장이 갈래를 담는다 ─────────────────────────────────────────────


def test_사유에_부서와_상태_짝이_적힌다():
    """`skipped` 하나로는 안 갈린다 — 갈래는 `runtime_status` 가 가른다 (물류 지적)."""
    outcome, _ = _run(inventory=_advisor(verdict="skipped", runtime="ERROR"))

    assert agent_label("inventory") in outcome.reason
    assert "ERROR" in outcome.reason
    assert "skipped" in outcome.reason


def test_두_갈래의_문장이_서로_다르다():
    """🔴 *"입력 없음"* 으로만 적으면 **재시도 가치가 있는 실패를 포기하게 된다.**

    물류가 그 사실을 사유에 직접 적어 보낸다 (`logistics/adapter.py:1385`) —
    *"데이터 부재가 아니라 재시도 가치가 있는 실패다."*
    """
    error, _ = _run(inventory=_advisor(verdict="skipped", runtime="ERROR"))
    not_ready, _ = _run(inventory=_advisor(verdict="skipped", runtime="RUNTIME_NOT_READY"))

    assert error.reason != not_ready.reason, "두 갈래가 같은 문장이면 갈린 뜻이 사라진다"
    assert "ERROR" in error.reason
    assert "RUNTIME_NOT_READY" in not_ready.reason


def test_판정과_사유가_응답에_그대로_실린다():
    """보류로 끝나도 **왜인지를 아는 칸**은 다 나간다 — 안 나온 날일수록 필요하다."""
    outcome, _ = _run(
        inventory=_advisor(verdict="skipped", runtime="ERROR", reasoning="스냅샷 조회 실패")
    )

    assert outcome.verdicts["inventory"]["reasoning"] == "스냅샷 조회 실패"
    assert outcome.verdicts["inventory"]["runtime_status"] == "ERROR"


# ── ④ 제안자 자리의 skipped 는 뜻이 다르다 ──────────────────────────────────


def test_매입이_낼_안이_없다고_답한_것은_판정_없음이_아니다():
    """🔴 **회귀 — 이 검사가 이번 판의 안전장치다.**

    매입도 안이 없으면 `READY + skipped` 를 보낸다 (`adapter.py:892`). 같은 값이지만
    **제안자 자리라 뜻이 다르다** — *"낼 안이 없다"* 는 정상 답이다.

    `contributes_to_band` 에 판정 조건을 더하려다 이걸 발견했다. 그렇게 고쳤으면
    `if not scenarios` 보다 앞선 분기가 먼저 잡아 **E4("매입 미가동")** 로 갔을 것이다 —
    매입은 돌았는데 화면에는 안 돌았다고 나간다.
    """
    outcome, purchaser = _run(purchaser=_Purchaser(scenarios=False))

    assert purchaser.calls == 1
    assert outcome.end_code == "E2_HELD"
    assert outcome.end_code != "E4_NOT_STARTED", "매입은 돌았다"
    assert agent_label("inventory") not in outcome.reason, "조언자 탓으로 적으면 안 된다"


# ── ⑤ 통과하는 날은 그대로 통과한다 ─────────────────────────────────────────


def test_전원_ok_면_사람에게_올라간다():
    """🔴 **회귀** — 정상 경로를 막지 않았는지 본다."""
    outcome, purchaser = _run()

    assert outcome.end_code == "E1_APPROVED"
    assert purchaser.calls == 1
