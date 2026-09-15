"""**없는 것을 없다고 말하는가** — 되묻는 말과 생존 확인 렌더.

실측에서 나온 두 장면을 고정한다.

```text
"오늘 배추 가격얼마야?"
  → 매입·재무를 부르고 [매입 능력 목록 + 재무 현금 잔고] 를 답으로 냈다
     가격은 한 줄도 없었다
```

★ **관계없는 숫자는 "모른다" 보다 나쁘다.** 물어본 사람이 그걸 답으로 읽는다.
"""

from __future__ import annotations

from datetime import date

from app.master.answer import facts_from_status
from app.master.llm.runtime import SYSTEM_PROMPT, _clarification
from app.master.llm.schemas import Intent
from app.master.plan import ExecutionPlan
from app.master.status_flow import StatusOutcome

_UNKNOWN = Intent(action="UNKNOWN", confidence="HIGH")


# ── 되묻는 말이 없는 이유를 이름으로 말한다 ──────────────────────────────


def test_가격_질문은_이제_빈자리가_아니다():
    """🔴 **가격·시세 항목을 뺐다** (2026-09-15).

    가격 예측(`ml`)이 상태 조회로 품목 가격에 답하게 되어, 되묻는 말이 *"가격을 조회하는
    자리는 아직 없습니다"* 라고 하면 **거짓말**이 된다. 분류 지시문의 UNKNOWN 가격 예시와
    같이 뺐다 — 한쪽만 남으면 지시문은 ml 로 보내는데 되묻는 말은 자리가 없다고 한다.
    """
    for 발화 in ("오늘 배추 가격얼마야?", "배추 시세 알려줘", "단가 어떻게 돼", "오늘 시가 얼마"):
        말 = _clarification(_UNKNOWN, 발화)
        assert "자리는 아직 없습니다" not in 말, 발화
        assert "알아듣지 못했습니다" in 말, 발화


def test_분류_지시문에도_가격_빈자리_예시가_없다():
    """지시문과 되묻는 말이 **같은 사실**을 말하는지 대조한다."""
    assert "품목 가격·시세를 답하는 부서는 없다" not in SYSTEM_PROMPT
    assert "  ml  " in SYSTEM_PROMPT


def test_이름_없는_것은_종전_안내로_간다():
    """목록을 늘려 가며 맞히는 것이 아니다 — 자주 묻는데 답이 없는 것만 이름을 준다."""
    말 = _clarification(_UNKNOWN, "그거 있잖아")

    assert "알아듣지 못했습니다" in 말
    assert "가격" not in 말


def test_발화문이_없으면_종전_안내다():
    assert "알아듣지 못했습니다" in _clarification(_UNKNOWN)


def test_되묻는_말은_UNKNOWN_에만_붙는다():
    """가격이라는 낱말이 있다고 다른 action 의 되물음을 덮으면 안 된다."""
    말 = _clarification(
        Intent(action="PROCUREMENT_RUN", item="배추", confidence="HIGH"),
        "배추 가격 보고 매입안 만들어줘",
    )

    assert "매입안을 새로 만들까요" in 말


# ── 생존 확인은 능력 목록으로 나가지 않는다 ──────────────────────────────


def _status(answers: dict) -> StatusOutcome:
    return StatusOutcome(
        status_code="S1_ANSWERED",
        reason="...",
        plan=ExecutionPlan(request_id="REQ-TEST", as_of=date(2025, 12, 31)),
        answers=answers,
    )


def test_매입_생존_확인은_사람이_읽는_한_줄로_나간다():
    """🔴 실측에서 이렇게 나왔다 —

    ```text
    매입 capabilities agent_version v1.1, supported_modes GENERATE_SCENARIOS,
    STATUS_QUERY, items 배추, 무, 피마늘 외 1건
    ```

    매입 잘못이 아니다. 매입의 `STATUS_QUERY` 는 설계상 생존 확인이고,
    **마스터가 셋을 똑같이 취급한 것**이 잘못이다.
    """
    facts = facts_from_status(
        _status(
            {
                "purchase": {
                    "capabilities": {
                        "agent_version": "v1.1",
                        "supported_modes": ["GENERATE_SCENARIOS", "STATUS_QUERY"],
                        "items": ["배추", "무", "피마늘", "양파"],
                    }
                }
            }
        )
    )

    적힌_것 = " ".join(f"{f.label} {f.value}" for f in facts.facts)
    assert "agent_version" not in 적힌_것
    assert "supported_modes" not in 적힌_것
    assert "v1.1" not in 적힌_것
    assert "요청을 받을 수 있는 상태" in 적힌_것
    assert "매입안 생성에서 나옵니다" in 적힌_것, "다음에 무엇을 하면 되는지까지 적는다"


def test_매입은_여전히_답한_부서로_세어진다():
    """감추는 것이 아니라 **뜻을 옮기는 것**이다 — 답한 사실은 남아야 한다."""
    facts = facts_from_status(_status({"purchase": {"capabilities": {"agent_version": "v1.1"}}}))

    assert "매입" in facts.answered
    assert len(facts.facts) == 1


def test_업무_값을_주는_부서는_그대로다():
    """재무·물류는 성격이 다르다 — 이 변경이 거기까지 번지면 안 된다."""
    facts = facts_from_status(_status({"finance": {"available_cash": 31993914}}))

    적힌_것 = " ".join(f"{f.label} {f.value}" for f in facts.facts)
    assert "가용 현금" in 적힌_것
    assert "31,993,914" in 적힌_것
