"""마스터 검증 Tool — M-16 실행 계획 온전성 · timing 게이트.

★ 픽스처는 **매입 실측 스키마**를 따른다 (2026-08-27 매입 §4 답변).
  `allowed_axes` 는 제안 최상위, `split_plan` 은 시나리오 안이다. 초안은 둘 다
  시나리오에서 찾다가 **검사가 영영 발화하지 않았다** — 그런 검사는 skipped 로도
  안 잡히고 "봤는데 문제없음"으로 읽힌다.
"""

from __future__ import annotations

from datetime import date

from app.master.plan import ExecutionPlan
from app.master.verifier import MasterVerifier

AS_OF = date(2026, 9, 4)


def plan() -> ExecutionPlan:
    return ExecutionPlan(request_id="REQ-1", as_of=AS_OF)


def scenario(rounds: int = 1) -> dict:
    return {
        "label": "보수",
        "strategy_type": "quantity",
        "total_qty_kg": 5000,
        # 매입 스키마상 split_plan 은 **최소 1** — 미적용이 빈 배열이 아니다
        "split_plan": [{"seq": i + 1, "date": "2026-09-05", "qty_kg": 1000} for i in range(rounds)],
    }


def proposal(axes: list[str] | None = None, rounds: int = 1) -> dict:
    out: dict = {"situation": "uncertain", "confidence": "medium", "scenarios": [scenario(rounds)]}
    if axes is not None:
        out["allowed_axes"] = axes
    return out


def run(p: dict):
    return MasterVerifier()(p, {}, {}, plan())


def gate(result) -> list[str]:
    return [f for f in result.findings if f.startswith("L-TIMING-GATE")]


# ---------------------------------------------------------------------------
# ③ timing 게이트
# ---------------------------------------------------------------------------


def test_timing_닫혔는데_분할이면_발화한다():
    assert gate(run(proposal(["quantity"], rounds=3)))


def test_분할이_1회차면_통과한다():
    """`split_plan` 은 최소 1이라 **경계가 `> 1`** 이다. 1을 잡으면 전부 걸린다."""
    assert not gate(run(proposal(["quantity"], rounds=1)))


def test_timing_이_열려_있으면_분할은_정상이다():
    assert not gate(run(proposal(["quantity", "timing"], rounds=3)))


def test_allowed_axes_가_없으면_통과가_아니라_미검사다():
    """★ 못 본 것을 통과로 치지 않는다 (§3.7.6)."""
    result = run(proposal(None, rounds=3))
    assert not gate(result)
    assert any(s.startswith("L-TIMING-GATE") for s in result.skipped)


def test_split_plan_이_없으면_그_시나리오만_미검사다():
    p = proposal(["quantity"], rounds=3)
    p["scenarios"].append({"label": "기본"})  # split_plan 없음
    result = run(p)
    assert gate(result)  # 0번은 여전히 걸린다
    assert any("scenarios[1]" in s for s in result.skipped)


# ---------------------------------------------------------------------------
# ④ 실행 계획 온전성 (M-16)
# ---------------------------------------------------------------------------


def test_조언자를_안_부르면_concern_이다():
    """★ finding 이 아니다 — 매입을 다시 불러도 마스터가 물류를 안 부른 사실은 안 바뀐다."""
    result = run(proposal(["quantity", "timing"]))
    assert not result.findings
    assert any("M16-AGENT-MISSING" in c for c in result.concerns)


def test_커버리지를_감추지_않는다():
    """`findings: []` 를 "56검사 통과"로 읽지 않게 못 본 것을 함께 낸다."""
    assert any("56검사" in s for s in run(proposal(["quantity", "timing"])).skipped)
