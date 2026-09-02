"""닫힌 집합을 **누가 만든 값인지로 갈라** 강제한다.

2026-09-02 매입 실측에서 나왔다. 하네스가 `trigger="MANUAL"` 을 넣었는데
**아무 데서도 안 걸렸다.**

```text
잘못된 trigger → 봉투 검증 통과 → LLM 6회 · 8.6초 소모 → DB CHECK 에서 터짐
               → "재무 검토 기록을 저장하지 못해 결과를 확정하지 못했습니다"
```

**매입은 그 문장을 보고 재무 문제로 오진했다.** 전날 #156 으로 사유를 화면까지
나르게 만든 참이라, **배관이 고쳐진 만큼 틀린 사유도 더 멀리 갔다.**

★ 갈림은 *누가 채우는 값인가* 다.

```text
마스터가 만드는 값   예외로 막는다        trigger · request_id · policy_version · mode
부서가 채우는 값     findings 로 돌린다   business_status · runtime_status · llm_status
```

뒤엣것을 예외로 막으면 **부서 하나의 실수가 사이클을 죽인다** — 봉투 모듈
docstring 이 명시적으로 금하는 것이 그것이다.
"""

from __future__ import annotations

from datetime import date
from typing import get_args

import pytest

from app.master.envelope import (
    LLM_STATUSES,
    RUNTIME_STATUSES,
    TRIGGERS,
    VERDICTS,
    AgentReply,
    ExecutionContext,
    ExecutionMetadata,
    LLMStatus,
    Trigger,
    check_vocabulary,
    validate_reply,
)
from app.orchestrator.contracts_core import (
    ContractViolation,
    RuntimeStatus,
    Verdict,
)
from tests.master.test_envelope import reply, req

AS_OF = date(2026, 8, 26)


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


# ── ① 마스터가 만드는 값은 입구에서 막는다 ──────────────────────────────────


def test_유효하지_않은_trigger_는_입구에서_막힌다():
    """🔴 매입이 실제로 밟은 경로다. 부서를 부르기 전에 죽어야 한다."""
    with pytest.raises(ContractViolation, match="trigger"):
        ExecutionContext(request_id="REQ-1", as_of=AS_OF, trigger="MANUAL", policy_version="v1.3")


def test_사유에_받은_값과_허용_집합이_둘_다_있다():
    """`mode` 를 막는 문장과 같은 모양이다 — 무엇이 왔고 무엇이 되는지."""
    with pytest.raises(ContractViolation) as e:
        ExecutionContext(request_id="REQ-1", as_of=AS_OF, trigger="MANUAL", policy_version="v1.3")

    말 = str(e.value)
    assert "MANUAL" in 말, "무엇이 왔는지가 없으면 고칠 값을 모른다"
    assert "ML_COMPLETE" in 말 and "USER_REQUEST" in 말, "무엇이 되는지가 없다"


def test_유효한_trigger_둘은_그대로_통과한다():
    """막는 것을 넣다가 정상 관통을 죽이면 안 된다."""
    for trigger in ("ML_COMPLETE", "USER_REQUEST"):
        ctx = ExecutionContext(
            request_id="REQ-1", as_of=AS_OF, trigger=trigger, policy_version="v1.3"
        )
        assert ctx.trigger == trigger


def test_빈_trigger_도_막힌다():
    with pytest.raises(ContractViolation, match="trigger"):
        ExecutionContext(request_id="REQ-1", as_of=AS_OF, trigger="", policy_version="v1.3")


# ── ② 부서가 채우는 값은 findings 로 돌린다 ─────────────────────────────────


def test_모르는_판정값은_예외가_아니라_findings_다():
    """예외로 막으면 **부서 하나의 실수가 사이클을 죽인다.**"""
    r = reply(business_status="FAIL")  # 만들어지는 것 자체는 막지 않는다

    assert "E-VOCAB-BUSINESS-STATUS" in _codes(check_vocabulary(r))


def test_모르는_판정을_담은_회신도_만들어진다():
    """`ContractViolation` 이 아니라는 것을 따로 잠근다."""
    r = reply(business_status="FAIL")

    assert r.business_status == "FAIL", "값을 고치지도 버리지도 않는다"


def test_모르는_실행상태도_findings_다():
    r = reply(runtime_status="TIMEOUT", business_status="skipped")

    assert "E-VOCAB-RUNTIME-STATUS" in _codes(check_vocabulary(r))


def test_모르는_llm_status_도_findings_다():
    """이 값이 낡으면 *"모델이 죽은 날"* 과 *"안 쓴 날"* 이 화면에서 같아 보인다."""
    r = reply()
    meta = ExecutionMetadata(
        run_id=r.run_id, request_id=r.request_id, agent="finance", llm_status="RETRIED"
    )

    assert "E-VOCAB-LLM-STATUS" in _codes(validate_reply(req(), r, meta))


def test_정상_회신에는_어휘_지적이_없다():
    """지적이 늘 붙으면 아무도 안 읽는다."""
    r = reply()
    meta = ExecutionMetadata(run_id=r.run_id, request_id=r.request_id, agent="finance")

    codes = _codes(validate_reply(req(), r, meta))
    assert not {c for c in codes if c.startswith("E-VOCAB-")}


def test_어휘_지적이_봉투_검증_전체에_실린다():
    """`check_vocabulary` 만 통과하고 `validate_reply` 가 안 부르면 아무 데도 안 남는다."""
    r = reply(business_status="FAIL")

    assert "E-VOCAB-BUSINESS-STATUS" in _codes(validate_reply(req(), r))


# ── ③ 왜 이 지적이 필요한가 ─────────────────────────────────────────────────


def test_어휘_밖의_값은_통과로_읽힌다는_것을_고정한다():
    """🔴 **이것이 fail-open 이다.**

    마스터는 `business_status != "reject"` 로 통과를 정한다
    (`flow._acceptable`). 어휘 밖의 값은 *"reject 가 아니다"* 라 **그냥 통과한다.**

    ★ 이 테스트는 지금 동작을 **고치는 것이 아니라 드러낸다.** 통과 판정을
      fail-closed 로 바꿀지는 별건이고, 그때까지 이 지적이 유일한 경보다.
    """
    from app.master.flow import ProcurementFlow

    acceptable = ProcurementFlow._acceptable
    verdicts = {"inventory": {"business_status": "FAIL"}}

    assert acceptable(None, (), verdicts, ()) is True, "지금은 통과한다"
    assert "E-VOCAB-BUSINESS-STATUS" in _codes(check_vocabulary(reply(business_status="FAIL")))


def test_지적_문장이_부서_탓으로만_읽히지_않는다():
    """마스터 어휘가 낡았을 수도 있다 — 고칠 곳이 서로 다르다."""
    finding = check_vocabulary(reply(business_status="FAIL"))[0]

    assert "낡" in finding.detail, "둘 중 하나가 낡았다는 것이 안 보인다"


# ── ④ 집합의 주인이 하나다 ──────────────────────────────────────────────────


def test_어휘_집합을_손으로_복사하지_않았다():
    """두 벌로 만들면 어휘를 늘린 날 한쪽만 늘어난다.

    ★ 값을 나열해 비교하지 않고 **정의에서 읽은 것과 같은지**를 본다.
    """
    assert TRIGGERS == frozenset(get_args(Trigger))
    assert RUNTIME_STATUSES == frozenset(get_args(RuntimeStatus))
    assert VERDICTS == frozenset(get_args(Verdict))
    assert LLM_STATUSES == frozenset(get_args(LLMStatus))


def test_집합이_비어_있지_않다():
    """`get_args` 가 빈 튜플을 주면 **모든 값이 통과한다** — 검사가 사라진 줄 모른다."""
    for name, s in (
        ("TRIGGERS", TRIGGERS),
        ("RUNTIME_STATUSES", RUNTIME_STATUSES),
        ("VERDICTS", VERDICTS),
        ("LLM_STATUSES", LLM_STATUSES),
    ):
        assert s, f"{name} 가 비었다 — 검사가 무력해진다"


def test_AgentReply_는_모르는_값을_받아_준다():
    """②의 반대편 — 부서 값을 봉투가 예외로 막지 않는다는 것을 못 박는다.

    이걸 안 잠그면 다음 사람이 *"어휘를 강제하자"* 며 `__post_init__` 에 넣는다.
    """
    r = AgentReply(
        request_id="REQ-1",
        as_of=AS_OF,
        agent="inventory",
        mode="SCENARIO_VALIDATION",
        run_id="INV-1",
        runtime_status="READY",
        business_status="REVIEW_REQUIRED",
    )

    assert r.business_status == "REVIEW_REQUIRED"
