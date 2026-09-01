"""조언자가 `ERROR` 로 터지면 **한 번만** 다시 부른다.

🔴 **실측 2026-08-31 — 판단이 배선되지 않은 채로 오래 있었다.**
`runner.retryable` 과 `envelope.worth_retry` 가 정의와 테스트만 있고 **앱 안에
호출자가 0개**였다. 판단을 담아 둔 자리가 비어 있으면, 읽는 사람은 *"재시도가
되는 줄"* 안다.

```text
ERROR              어댑터가 터졌다        → 다시 부르면 달라질 수 있다   ← 재시도
RUNTIME_NOT_READY  입력이 없어 못 냈다    → 다시 불러도 같다             → 예산만 태운다
```

★ **성공 경로는 하나도 안 바뀐다.** `contributes_to_band` 가 참이면 재시도 판단
  자체를 안 한다 — 정상 관통의 호출 수는 그대로다.

★ **루프가 아니다.** 결정론 고장(어댑터 스키마 오류 같은 것)은 몇 번을 불러도
  같으므로 두 번째까지만 쓴다. 2026-09-01 재무 Gemini 400 이 그런 경우였다.
"""

from __future__ import annotations

from tests.master.test_flow import advisor, ctx, flow, purchaser

_PRE = "PRE_PURCHASE"


def _calls(plan, agent: str, mode: str = _PRE) -> int:
    return sum(1 for s in plan.steps if s.agent == agent and s.mode == mode)


def _flaky(*, fail_times: int):
    """`fail_times` 번은 `ERROR`, 그 뒤로는 정상."""
    state = {"n": 0}
    ok = advisor()
    bad = advisor(runtime="ERROR")

    def port(request):
        if request.mode == _PRE:
            state["n"] += 1
            if state["n"] <= fail_times:
                return bad(request)
        return ok(request)

    return port


def test_한_번_터지면_다시_부르고_이어서_돈다():
    run = flow(finance=_flaky(fail_times=1), inventory=advisor(), purchase=purchaser()).run()

    assert _calls(run.plan, "finance") == 2, "다시 부르지 않았다"
    assert run.end_code == "E1_APPROVED", f"재시도로 살아나야 한다: {run.reason}"


def test_두_번_터지면_포기한다_루프가_아니다():
    """🔴 결정론 고장은 몇 번을 불러도 같다 — 예산을 더 태우지 않는다."""
    run = flow(finance=_flaky(fail_times=9), inventory=advisor(), purchase=purchaser()).run()

    assert _calls(run.plan, "finance") == 2, "두 번을 넘겼다"
    assert run.end_code == "E4_NOT_STARTED"
    assert "finance" in run.reason


def test_입력이_없어_못_낸_답은_다시_안_부른다():
    """`RUNTIME_NOT_READY` 를 재시도하면 **호출 예산만 태운다.**"""
    run = flow(
        finance=advisor(runtime="RUNTIME_NOT_READY"), inventory=advisor(), purchase=purchaser()
    ).run()

    assert _calls(run.plan, "finance") == 1, "다시 부르면 안 된다"
    assert run.end_code == "E4_NOT_STARTED"


def test_성공_경로의_호출_수는_그대로다():
    """★ 이 변경이 **정상 관통을 건드리지 않는다**는 것이 안전의 근거다."""
    run = flow(finance=advisor(), inventory=advisor(), purchase=purchaser()).run()

    assert _calls(run.plan, "finance") == 1
    assert _calls(run.plan, "inventory") == 1
    assert run.end_code == "E1_APPROVED"


def test_다시_불렀다는_사실이_실행_계획에_남는다():
    """실패를 감추지 않는다 — **오히려 드러낸다.**

    같은 단계가 두 줄로 남아야 *"한 번 실패"* 가 아니라
    **"다시 불렀는데도 안 됐다"** 가 된다.
    """
    run = flow(finance=_flaky(fail_times=9), inventory=advisor(), purchase=purchaser()).run()

    finance_steps = [s for s in run.plan.steps if s.agent == "finance" and s.mode == _PRE]
    assert len(finance_steps) == 2
    assert [s.runtime_status for s in finance_steps] == ["ERROR", "ERROR"]
    assert [s.call_seq for s in finance_steps] == [1, 2], "회차가 구분돼야 한다"


def test_예산이_모자라면_재시도가_예산을_넘기지_않는다():
    """재시도도 예산을 센다 — 소진되면 `E3` 로 접힌다 (§1.2-12)."""
    run = flow(
        budget=1, finance=_flaky(fail_times=9), inventory=advisor(), purchase=purchaser()
    ).run()

    assert run.end_code == "E3_REJECTED"
    assert "예산" in run.reason


def test_runner_는_스스로_재시도하지_않는다():
    """★ `retryable` 은 **알려만 준다.** 정하는 것은 `flow` 다."""
    from app.master import AgentRegistry, CallBudget, MasterRunner

    reg = AgentRegistry()
    reg.register("finance", advisor(runtime="ERROR"))
    runner = MasterRunner(ctx(), reg, CallBudget(limit=12))

    runner.call("finance", _PRE)

    assert runner.retryable("finance", _PRE) is True
    assert _calls(runner.plan, "finance") == 1, "runner 가 스스로 다시 부르면 안 된다"
