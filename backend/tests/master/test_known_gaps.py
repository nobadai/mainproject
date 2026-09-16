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

import json
from datetime import date

from app.master.answer import facts_from_status
from app.master.llm.runtime import (
    SYSTEM_PROMPT,
    IntentService,
    LLMSettings,
    _clarification,
    _known_gap,
)
from app.master.llm.schemas import Intent
from app.master.plan import ExecutionPlan
from app.master.status_flow import StatusOutcome

_UNKNOWN = Intent(action="UNKNOWN", confidence="HIGH")

_SETTINGS = LLMSettings(
    enabled=True,
    provider="fake",
    model="fake-model",
    base_url="",
    timeout_seconds=1.0,
    max_retries=1,
    max_output_tokens=512,
    effort=None,
)


class _FakeProvider:
    """네트워크를 타지 않는다 — 지어 둔 분류 결과를 그대로 돌려준다."""

    def __init__(self, response: str) -> None:
        self.response = response

    def generate(self, system: str, user: str, schema: dict) -> str:
        return self.response


def payload(**kw) -> str:
    base = {"action": "UNKNOWN", "agents": [], "confidence": "LOW"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def service(response: str) -> IntentService:
    return IntentService(_SETTINGS, _FakeProvider(response))


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


# ── 분류 지시문이 ML 이 답할 수 있는 것을 ml 로 보내는가 ────────────────


def _부서_항목(이름: str) -> str:
    """「부서 이름 (agents)」 절에서 한 부서 항목만 떼어낸다.

    지시문 전체에 낱말이 있는지 보면 **다른 부서 줄의 낱말이 섞여 세어진다**.
    항목은 다음 빈 줄까지다.
    """
    _, 있다, 뒤 = SYSTEM_PROMPT.partition(f"\n  {이름} ")
    assert 있다, f"부서 목록에 {이름} 항목이 없다"
    항목, _, _ = 뒤.partition("\n\n")
    return 항목


def test_ml_항목에_배치_갈래가_있다():
    """ML 이 배치·데이터 처리 갈래를 더했다 (`#746`).

    ★ 이것은 **글자 검사다.** 지시문은 프롬프트라서 낱말이 있다고 LLM 이 그렇게 가른다는
      보장은 없다 — 여기서 잴 수 있는 것은 *"지시문이 그 갈래를 아예 말하지 않는"* 회귀뿐이다.
      실제로 ml 로 가는지는 LLM 을 부르는 실측에서 본다.
    """
    항목 = _부서_항목("ml")

    for 낱말 in ("배치", "데이터 처리", "점검"):
        assert 낱말 in 항목, f"ml 항목에 {낱말} 이 없다 — 배치 질문이 ml 로 안 간다"


def test_ml_항목에_모델_성능_갈래가_있다():
    """ML 이 모델 성능·재학습 갈래를 더했다 (`#746`)."""
    항목 = _부서_항목("ml")

    for 낱말 in ("모델 성능", "재학습", "정확도"):
        assert 낱말 in 항목, f"ml 항목에 {낱말} 이 없다 — 성능 질문이 ml 로 안 간다"


def test_품목_이름이_없어도_ml_이라고_적혀_있다():
    """🔴 "오늘 가격 알려줘" 가 걸리던 자리다.

    품목 이름이 ml 을 가른다고 적혀 있으면 품목 없는 가격 질문이 ml 로 오지 않는다.
    ML 은 이제 품목 없이도 답한다.
    """
    assert "품목 이름이 없어도 ml" in SYSTEM_PROMPT
    assert "item 을 비운다" in _부서_항목("ml"), "품목이 없을 때 무엇을 하라는지까지 적는다"


def test_finance_경계는_회사_돈으로_또렷하다():
    """🔴 ml 을 넓히면서 **자금 질문이 ml 로 새면** 안 된다.

    「대금 · 지급 · 결제 = 회사 돈」 경계가 흐려지는지를 본다.
    """
    assert "대금" in _부서_항목("finance")
    assert "대금 · 지급 · 결제 = 회사 돈이다" in SYSTEM_PROMPT
    assert '"이번 주 대금 얼마 나가?"   → finance' in SYSTEM_PROMPT


def test_갈래가_섞여도_ml_하나로_보낸다():
    """가격과 배치를 같이 물으면 부서를 둘로 쪼개지 않는다 — 답하는 부서는 하나다."""
    assert "ml 하나로" in SYSTEM_PROMPT


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


# ── 제외 품목은 대상이 아니라고 말한다 ──────────────────────────────────
#
# 물류가 정했다 (「재고·물류 STATUS_QUERY 기능 정의서」 §3.2 · §3.3 · §16 · §18 · §19 ·
# 물류 확정 2026-09-16). 정상 품목은 배추·무·양파 셋이고 피마늘·건고추는 대상이 아니다.
#
# ★ **분류 결과를 지어 넣는다.** 제외 품목을 UNKNOWN 으로 보내는 것은 지시문이 하는 일이고
#   지시문은 LLM 이 읽는다 — 단위 검사로 못 잠근다. 여기서 재는 것은 *"UNKNOWN 으로 왔을 때
#   무슨 말이 나가는가"* 와 *"섞인 질문이 부서까지 가는가"* 다.


def test_피마늘_질문은_대상이_아니라고_말한다():
    말 = _clarification(_UNKNOWN, "피마늘 재고 얼마나 남았어?")

    assert "대상 품목이 아닙니다" in 말
    assert "알아듣지 못했습니다" not in 말, "못 알아들은 것이 아니라 대상이 아닌 것이다"


def test_건고추_질문도_대상이_아니라고_말한다():
    말 = _clarification(_UNKNOWN, "건고추 재고 알려줘")

    assert "대상 품목이 아닙니다" in 말
    assert "알아듣지 못했습니다" not in 말


def test_제외_품목이_없으면_빈자리로_이름_붙이지_않는다():
    """목록에 없는 말은 종전 안내로 간다 — 목록을 늘려 가며 맞히는 것이 아니다."""
    assert _known_gap("배추 재고 얼마나 남았어?") is None
    assert _known_gap("오늘 무 얼마나 사야 해?") is None


def test_섞인_질문은_부서까지_간다():
    """🔴 **전체 실패가 아니다.**

    원문이 물류에 실려 가므로(`#767`) 물류가 정상 품목을 답하고 제외 품목 안내를 붙인다.
    마스터가 여기서 거르면 **배추 재고까지 막힌다.**
    """
    result = service(
        payload(action="STATUS_QUERY", agents=["inventory"], item="배추", confidence="HIGH")
    ).classify("배추랑 피마늘 재고 알려줘")

    assert result.intent.action == "STATUS_QUERY"
    assert result.intent.agents == ["inventory"]
    assert result.needs_confirmation is False
    assert result.clarification is None, "제외 품목 안내가 정상 조회를 덮으면 안 된다"


def test_정상_품목_질문은_종전과_같다():
    """회귀 — 제외 품목 규칙이 배추 조회에 번지면 안 된다."""
    result = service(
        payload(action="STATUS_QUERY", agents=["inventory"], item="배추", confidence="HIGH")
    ).classify("배추 재고 얼마나 남았어?")

    assert result.intent.action == "STATUS_QUERY"
    assert result.intent.agents == ["inventory"]
    assert result.needs_confirmation is False
    assert result.clarification is None


def test_분류_지시문에도_제외_품목_규칙이_있다():
    """지시문과 되묻는 말이 **같은 사실**을 말하는지 대조한다."""
    assert "피마늘 · 건고추는 이 프로젝트의 대상 품목이 아니다" in SYSTEM_PROMPT
    assert '"피마늘 재고 얼마나 남았어?"  → UNKNOWN' in SYSTEM_PROMPT
    assert '"배추랑 피마늘 재고 알려줘"   → STATUS_QUERY' in SYSTEM_PROMPT


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
