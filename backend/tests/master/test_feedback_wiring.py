"""매입 재호출이 **1회차와 다른 입력으로** 돈다.

2026-09-02 · #169 · 되먹임 계약 v0.2 §8 배선 1단계.

🔴 **매입 Q4 지적** — `constraints` 가 루프 밖에서 만들어지고 안 바뀌므로
2회차가 1회차와 완전히 같은 입력으로 돌았다. *"트리거를 넓혀도 같은 입력을 더
자주 돌릴 뿐"* 이었다.

사람이 화면에서 보는 것은 *"매입 재호출 2회에도 통과안 없음"* 이고, 읽는 사람은
**"고쳐 보라고 두 번 시켰는데 못 고쳤구나"** 로 읽었다.

★ **슬롯을 둘로 나눈다** (매입 제안 · 재무 동의).

```text
prior_feedback   사용자 조건   실행 단위 · 자연어 · 매입이 해석
adjustments      검증 되먹임   회차 단위 · 봉투 표준형 · 해석 불필요
```

  한 슬롯에 `source` 로 갈랐던 v0.1 은 **payload 의 타입이 `source` 값에 딸려 가서**
  계약이 아니라 관례가 됐다.

⚠️ **매입이 아직 안 읽는다.** 이 파일이 잠그는 것은 *"보냈는가"* 이지
*"반영됐는가"* 가 아니다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master.budget import CallBudget
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionContext,
    ExecutionMetadata,
)
from app.master.flow import ProcurementFlow
from app.master.runner import AgentRegistry, MasterRunner
from app.master.verifier import VerificationResult
from app.orchestrator.contracts_core import SuggestedAdjustment

AS_OF = date(2025, 12, 31)

SCN = [{"scenario_id": "SCN-1", "total_amount_krw": 30000000}]


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="REQ-20251231-0001",
        as_of=AS_OF,
        trigger="USER_REQUEST",
        policy_version="v1.3",
    )


def _adj(dept: str, axis: str, value: float, unit: str, reason: str = "사유"):
    return SuggestedAdjustment(
        dept=dept,  # type: ignore[arg-type]
        axis=axis,  # type: ignore[arg-type]
        target_value=value,
        unit=unit,
        reason=reason,
        ref_ids=("REF-1",),
    )


def _advisor(
    *,
    verdict: str = "ok",
    reasoning: str = "",
    adjustments: tuple[SuggestedAdjustment, ...] = (),
):
    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        run_id = f"{request.agent.upper()}-{request.mode[:3]}-{request.call_seq}"
        pre = request.mode == "PRE_PURCHASE"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok" if pre else verdict,  # type: ignore[arg-type]
            payload={"cap": 1} if pre else {},
            reasoning="" if pre else reasoning,
            suggested_adjustments=() if pre else adjustments,
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


class _Purchaser:
    """부른 payload 를 회차별로 기록한다."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        self.payloads.append(dict(request.payload))
        run_id = f"PURCHASE-{request.call_seq}"
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent="purchase",
            mode=request.mode,
            run_id=run_id,
            runtime_status="READY",
            business_status="ok",
            payload={"scenarios": list(SCN)},
        )
        meta = ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent="purchase"
        )
        return reply, meta


def _run(*, rejecting: bool = True, prior_feedback: dict[str, Any] | None = None, **over: Any):
    """`rejecting` 이면 조언자가 `reject` 를 내 재호출이 일어난다."""
    purchaser = _Purchaser()
    ports: dict[str, Any] = {
        "finance": _advisor(),
        "inventory": _advisor(verdict="reject" if rejecting else "ok", reasoning="창고가 없다"),
        "purchase": purchaser,
    }
    ports.update({k: v for k, v in over.items() if k in ("finance", "inventory")})
    registry = AgentRegistry()
    for name, port in ports.items():
        registry.register(name, port)
    runner = MasterRunner(_ctx(), registry, CallBudget(limit=12))
    flow = ProcurementFlow(
        runner,
        verifier=over.get("verifier"),
        item="피마늘",
        prior_feedback=prior_feedback,
    )
    return flow.run(), purchaser


# ── ① 2회차가 1회차와 다르다 ────────────────────────────────────────────────


def test_두_번_부른다():
    """앞으로 오는 검사들이 무의미해지지 않게 전제부터 고정한다."""
    _, purchaser = _run()

    assert len(purchaser.payloads) == 2


def test_1회차는_되먹임을_안_싣는다():
    """되먹임은 1회차 산출물에서 나온다 — 1회차에는 있을 수가 없다."""
    _, purchaser = _run()
    first = purchaser.payloads[0]

    assert "adjustments" not in first
    assert "feedback_context" not in first


def test_2회차는_되먹임을_싣는다():
    """🔴 이것이 없어서 같은 입력으로 두 번 돌았다."""
    _, purchaser = _run(
        inventory=_advisor(
            verdict="reject",
            reasoning="창고가 없다",
            adjustments=(_adj("inventory", "quantity", 7120.0, "kg"),),
        )
    )
    second = purchaser.payloads[1]

    assert second["adjustments"], "조정안이 안 갔다"
    assert second["feedback_context"]["attempt"] == 2


def test_1회차와_2회차가_다르다():
    """이 파일 전체가 잠그는 한 문장."""
    _, purchaser = _run()

    assert purchaser.payloads[0] != purchaser.payloads[1]


def test_되먹임_말고는_같다():
    """막는 것을 넣다가 나머지 입력이 흔들리면 안 된다."""
    _, purchaser = _run()
    first, second = purchaser.payloads
    바뀐_키 = {k for k in second if k not in first}

    assert 바뀐_키 == {"adjustments", "feedback_context"}
    assert all(first[k] == second[k] for k in first)


# ── ② 슬롯을 섞지 않는다 ────────────────────────────────────────────────────


def test_사용자_조건이_2회차에도_살아_있다():
    """🔴 한 슬롯이면 검증 되먹임이 사용자 조건을 덮는다.

    사람이 건 조건은 **실행 단위**라 회차가 바뀌어도 유효하다.
    """
    조건 = {"condition_text": "예산 2천만원으로 낮춰서", "condition_seq": 3}
    _, purchaser = _run(prior_feedback=조건)

    for payload in purchaser.payloads:
        assert payload["prior_feedback"] == 조건


def test_회차를_세는_이름이_슬롯마다_다르다():
    """🔴 **슬롯을 나눠 놓고 이름을 안 갈랐던 자리다** (#178 · 매입 실측 2026-09-03).

    ```text
    prior_feedback["condition_seq"]   사람이 조건을 건 회차
    feedback_context["attempt"]       매입 재호출 회차
    ```

    매입이 `state["feedback"].get("attempt", 0)` 으로 되먹임 회차를 찾다가 늘 0을
    받았다 — **틀린 값을 읽은 것이 아니라 다른 개념을 같은 이름으로 찾고 있었다.**

    ★ 이 검사가 잠그는 것은 개명 자체가 아니라 **다시 합쳐지지 않는 것**이다.
      한 이름으로 되돌아가면 같은 실수가 그대로 반복된다.
    """
    _, purchaser = _run(prior_feedback={"condition_text": "예산 2천만원으로", "condition_seq": 3})
    second = purchaser.payloads[1]

    assert second["prior_feedback"]["condition_seq"] == 3
    assert second["feedback_context"]["attempt"] == 2, "되먹임 회차는 조건 회차와 다르다"
    assert "attempt" not in second["prior_feedback"], "사용자 조건 슬롯은 attempt 를 안 쓴다"
    assert "condition_seq" not in second["feedback_context"], "되먹임 슬롯도 서로 안 쓴다"


def test_사용자_조건과_되먹임이_다른_칸에_있다():
    """수명·모양·권위가 달라 한 칸에 두면 받는 쪽이 타입을 분기해야 한다."""
    _, purchaser = _run(prior_feedback={"condition_text": "예산 2천만원으로"})
    second = purchaser.payloads[1]

    assert "condition_text" in second["prior_feedback"]
    assert "condition_text" not in second["feedback_context"]
    assert isinstance(second["adjustments"], list)


# ── ③ 마스터가 값을 손대지 않는다 ───────────────────────────────────────────


def test_조정안을_고르지도_병합하지도_않는다():
    """같은 축이 둘 이상이어도 그대로 나른다 (매입·재무 합의)."""
    첫째 = _adj("inventory", "quantity", 9000.0, "kg", "첫째")
    둘째 = _adj("inventory", "quantity", 457.0, "kg", "둘째")
    _, purchaser = _run(
        inventory=_advisor(verdict="reject", reasoning="창고", adjustments=(첫째, 둘째))
    )
    실린_것 = purchaser.payloads[1]["adjustments"]

    assert len(실린_것) == 2, "합쳤거나 골랐다"
    assert [a["reason"] for a in 실린_것] == ["첫째", "둘째"], "순서를 바꿨다"
    assert [a["target_value"] for a in 실린_것] == [9000.0, 457.0], "정렬했다"


def test_조정안_값을_고치지_않는다():
    원본 = _adj("inventory", "timing", 3.0, "d", "도착일을 밀어라")
    _, purchaser = _run(inventory=_advisor(verdict="reject", reasoning="창고", adjustments=(원본,)))
    실린_것 = purchaser.payloads[1]["adjustments"][0]

    assert 실린_것["dept"] == "inventory"
    assert 실린_것["axis"] == "timing"
    assert 실린_것["target_value"] == 3.0
    assert 실린_것["unit"] == "d"
    assert 실린_것["reason"] == "도착일을 밀어라"


def test_사유는_고르지_않고_센다():
    """`findings` 중 하나를 옮기면 *"이것이 대표다"* 라는 판단이 생긴다 (§3.2.2)."""
    _, purchaser = _run(
        verifier=lambda s, c, v, p, ctx=None: VerificationResult(("E-IDENTITY", "E-TIMING"))
    )
    context = purchaser.payloads[1]["feedback_context"]

    assert "검증 지적 2건" in context["reason"]
    assert "E-IDENTITY" not in context["reason"], "원문을 사유에 옮겼다"
    assert context["findings"] == ["E-IDENTITY", "E-TIMING"], "원문은 여기 통째로 있다"


def test_부서_사유는_원문_그대로_간다():
    _, purchaser = _run(inventory=_advisor(verdict="reject", reasoning="창고 구역이 잠겼다"))
    context = purchaser.payloads[1]["feedback_context"]

    assert context["verdict_reasons"]["inventory"] == "창고 구역이 잠겼다"


# ── ④ verdicts — 0건의 뜻을 가른다 ──────────────────────────────────────────


def test_판정을_기계_값으로_싣는다():
    """`adjustments` 가 0건일 때 그 0의 뜻을 가르는 유일한 칸이다."""
    _, purchaser = _run()
    context = purchaser.payloads[1]["feedback_context"]

    assert context["verdicts"] == {"finance": "ok", "inventory": "reject"}


def test_조정안_0건과_판정을_함께_읽을_수_있다():
    """reject + 0건 = 구제 불가 / ok + 0건 = 조정 불필요 — 같은 빈 배열이 정반대다."""
    _, purchaser = _run()
    second = purchaser.payloads[1]

    assert second["adjustments"] == []
    assert second["feedback_context"]["verdicts"]["inventory"] == "reject"


def test_봉투_어휘를_그대로_옮긴다():
    """`skipped` 도 판정 값이다 — 마스터가 표기를 바꾸지 않는다 (재무 지적)."""
    _, purchaser = _run(inventory=_advisor(verdict="skipped"), rejecting=False)
    # skipped 는 reject 가 아니라 통과하므로 재호출이 없다 — 판정만 확인한다
    assert purchaser.payloads

    _, p2 = _run(
        finance=_advisor(verdict="skipped"),
        inventory=_advisor(verdict="reject", reasoning="창고"),
    )
    assert p2.payloads[1]["feedback_context"]["verdicts"]["finance"] == "skipped"


# ── ⑤ 재현성 ────────────────────────────────────────────────────────────────


def test_같은_입력이면_같은_2회차가_나온다():
    """되먹임에 시각·난수가 들어가면 백테스트가 성립하지 않는다 (§3.4)."""
    _, first = _run()
    _, second = _run()

    assert first.payloads[1] == second.payloads[1]
