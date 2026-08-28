"""⑥ 사용자 응답 생성 — **실 모델** 채점 (`-m llm` 으로 돈다).

```bash
uv run pytest tests/master/test_answer_llm.py -m llm -v
```

★ **이 파일이 재는 것은 문장 품질이 아니라 규칙 준수다.** "잘 쓴 문장"은 채점할 수
  없지만 **"숫자를 안 썼는가"** 는 잴 수 있다. ①의 채점표(`test_intent_llm.py`)와
  같은 이유로 둔다 — 프롬프트를 고칠 때 성적이 떨어지는지 보려는 것이다.

★ 실패해도 답이 나가는 설계라 **`FALLBACK` 을 허용한다.** 다만 그때도 사실 줄의
  숫자는 반드시 그대로 있어야 한다 — 그것이 이 설계의 요지다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.master.answer import facts_from_status, render_answer
from app.master.llm.answer_runtime import get_narrative_service
from app.master.plan import ExecutionPlan
from app.master.status_flow import StatusOutcome

pytestmark = pytest.mark.llm


def outcome(**kw) -> StatusOutcome:
    base = {
        "status_code": "S1_ANSWERED",
        "reason": "...",
        "plan": ExecutionPlan(request_id="REQ-LLM", as_of=date(2025, 12, 31)),
        "answers": {
            "finance": {
                "available_cash": 31993913.0,
                "minimum_cash_balance_krw": 12941280.0,
                "payment_pressure": "LOW",
            },
            "inventory": {"warehouse_free_kg": 7636.72, "lot_count": 4},
        },
    }
    base.update(kw)
    return StatusOutcome(**base)


@pytest.fixture(scope="module")
def narrator():
    return get_narrative_service()


CASES = {
    "둘 다 답한 경우": outcome(),
    "한 부서만 답한 경우": outcome(
        status_code="S2_PARTIAL",
        answers={"inventory": {"warehouse_free_kg": 7636.72}},
        unavailable=("finance",),
        missing_data={"finance": ("finance_state",)},
    ),
    "아무도 못 답한 경우": outcome(
        status_code="S3_UNAVAILABLE",
        answers={},
        unavailable=("finance", "inventory"),
        errors={"finance": "커넥션 실패"},
    ),
}


@pytest.mark.parametrize(("name", "case"), CASES.items(), ids=list(CASES))
def test_문장에_숫자가_없다(narrator, name, case):
    """**이것 하나가 이 파일의 본체다.**"""
    result = narrator.write(facts_from_status(case))

    assert result.llm_status in {"SUCCESS", "FALLBACK"}, (
        f"모델을 부르지 못했다 ({result.llm_status}) — 프로바이더 설정을 확인하라"
    )
    if result.narrative is None:
        return  # 문장을 못 얻어도 답은 나간다 — 아래 테스트가 그것을 잠근다
    assert not any(ch.isdigit() for ch in result.narrative), (
        f"[{name}] 문장에 숫자가 들어갔다: {result.narrative!r} · 시도 {result.llm_attempts}회"
    )


def test_문장이_있든_없든_사실_줄의_숫자는_그대로다(narrator):
    facts = facts_from_status(outcome())
    text = render_answer(facts, narrator.write(facts).narrative)

    assert "31,993,913원" in text
    assert "7,636.7kg" in text
    assert "4건" in text
