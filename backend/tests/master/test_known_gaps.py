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

from app.master.answer import facts_from_status
from app.master.llm.runtime import _clarification
from app.master.llm.schemas import Intent
from datetime import date

from app.master.plan import ExecutionPlan
from app.master.status_flow import StatusOutcome

_UNKNOWN = Intent(action="UNKNOWN", confidence="HIGH")


# ── 되묻는 말이 없는 이유를 이름으로 말한다 ──────────────────────────────


def test_가격_질문에는_가격_자리가_없다고_말한다():
    """*"못 알아들었습니다"* 만 적으면 사람은 **자기가 말을 잘못했다고 생각하고**
    표현을 바꿔 다시 묻는다. 그래도 안 된다 — 없는 것이기 때문이다."""
    말 = _clarification(_UNKNOWN, "오늘 배추 가격얼마야?")

    assert "가격" in 말
    assert "없습니다" in 말
    assert "재무 조회는 회사 자금" in 말, "왜 재무가 답이 아닌지까지 적어야 다시 안 묻는다"


def test_시세_단가도_같은_말을_받는다():
    for 발화 in ("배추 시세 알려줘", "단가 어떻게 돼", "오늘 시가 얼마"):
        assert "가격" in _clarification(_UNKNOWN, 발화)


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
