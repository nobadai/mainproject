"""⑥ 사용자 응답 생성 — **문장만 쓰게 하고, 숫자는 거부한다.**

①(의도 분류, `runtime.py`)과 프로바이더·설정을 공유하고 **검증만 다르다.**

```text
AnswerFacts (부서 이름만) → [LLM] → 문장 → 검사 3종 → answer.render_answer 가 얹음
                                         └ 걸리면 재시도 1회 → 그래도면 문장 없이 간다
```

★ **검사 넷이 전부 "지어낸 것"을 막는다 — 숫자 · 평가 · 없는 결손 · 없는 부서.**
  의도 분류는 출력이 닫힌 열거라 타입이 막아 줬지만, 여기는 자유 문장이라 타입이 못
  막는다. 그래서 *"틀리게 쓰지 마"* 가 아니라 **"쓰지 마"** 로 잠근다 — 판정 기준이
  이분법이라 애매한 경우가 없다.

  🔴 **평가 금지가 셋 중 가장 늦게 붙었고, 가장 중요하다.** 값을 숨겼더니 모델이
  *"현금 상황이 다소 어려운 편입니다"* 라고 썼다 — 현금 압박이 `LOW` 인 날이었다.
  근거를 안 주면 지어내고, 주면 숫자를 옮겨 적는다. **평가할 일 자체를 빼는 것**이
  둘 다 피하는 유일한 길이다.

★ **실패가 답을 막지 않는다.** 검증에 걸리든 서버가 죽든 `narrative=None` 으로 돌아가고,
  `answer.py` 가 만든 사실 줄만으로 답이 나간다. ①은 실패하면 되물어야 했지만
  (분류를 못 하면 실행할 수 없으므로) **⑥은 실패해도 답할 수 있다.**

★ **적을수록 낫다.** `TOO_LONG` 을 둔 것은 길이 제한이 아니라, 모델이 길게 쓸수록
  사실 줄과 어긋나는 말을 지어낼 자리가 늘기 때문이다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from app.master.answer import AnswerFacts
from app.master.llm.runtime import (
    LLMSettings,
    TextProvider,
    build_provider,
    get_llm_settings,
)
from app.master.llm.schemas import LLMStatus, Narrative, NarrativeResult

#: 숫자 하나라도 있으면 거부한다. `runtime._DIGITS` 와 같은 뜻이지만 **쓰임이 반대다** —
#: 거기서는 "발화문에 없던 숫자"만 걸렀고, 여기서는 **모든 숫자**를 거른다.
_DIGITS = re.compile(r"\d")

#: 사람이 읽는 앞머리라 한 문장이면 충분하다. 길수록 지어낼 자리가 는다.
_MAX_CHARS = 120

#: 🔴 **값을 못 본 모델이 상태를 평가하는 것**을 막는다.
#:
#: 실측에서 현금 압박 `LOW` · 가용현금이 최소현금의 2.5배인 상황에 모델이
#: *"현재 현금 상황이 다소 어려운 편입니다"* 라고 썼다. 값을 안 보여준 것이 원인인데,
#: 보여주면 이번엔 숫자를 옮겨 적는다. **그래서 평가 자체를 금지한다** — 판단은
#: 규칙(`_END_HEADLINE`)과 부서가 하고, 문장은 그것을 옮기기만 한다.
#:
#: ⚠️ 이 목록은 **완전하지 않다.** 본체는 프롬프트이고, 여기 있는 것은 실측에서
#: 실제로 나온 표현을 잠근 것이다. 새로 나오면 더한다.
_EVALUATIVE = (
    "넉넉",
    "부족",
    "어려",
    "여유롭",
    "안정",
    "위험",
    "우려",
    "충분",
    "빠듯",
    "좋습",
    "나쁩",
    "양호",
    "심각",
    "괜찮",
)

#: 🔴 **다 답했는데 "못 봤다" 고 쓰는 것**을 막는다.
#:
#: 이것도 실측이다 — 물류가 답한 상황에서 *"창고 여유와 보관 로트 정보는 확인되지
#: 않았습니다"* 라고 썼다. 정확히 반대였다. `gaps` 가 비어 있을 때만 검사한다.
_NEGATIVE = ("못 ", "못했", "못한", "않았", "않은", "없었", "없습", "불가", "실패")

#: 🔴 **묻지도 않은 부서를 문장에 넣는 것**을 막는다.
#:
#: 실측 — 물류 하나만 물은 요청에 *"재무 및 물류 부서는 확인되지 않았습니다"* 라고
#: 썼다. 재무는 이 요청에 등장한 적이 없다. 부서 이름은 **닫힌 목록**이라
#: 프롬프트에 없던 이름이 문장에 나오면 지어낸 것이 확실하다.
_AGENT_WORDS = ("재무", "물류", "매입")

SYSTEM_PROMPT = """당신은 햇들농산 매입 의사결정 시스템의 응답 문장 작성자다.
아래에 주어진 결론과 부서 목록을 **한 문장으로 옮겨 적는** 것이 당신의 일이다.

지켜야 할 것
- 숫자를 절대 쓰지 마라. 아라비아 숫자도 한글 수사도 쓰지 않는다.
  금액·수량·일수는 문장 아래 표로 이미 나가므로 문장에서 반복할 필요가 없다.
- **상태를 평가하지 마라.** 넉넉하다·부족하다·어렵다·안정적이다·위험하다 같은 말을
  쓰지 않는다. 당신에게는 값이 주어지지 않았으므로 판단할 근거가 없다.
- **권고하지 마라.** 사야 한다·기다려야 한다 같은 말을 쓰지 않는다. 결론은 이미
  정해져 주어진다.
- "답하지 못한 부서" 가 "없음" 이면 무언가 확인되지 않았다고 쓰지 마라.
- 한 문장, 100자 이내, 존댓말로 쓴다.

좋은 예
{"summary": "재무와 물류 상태를 확인했습니다."}
{"summary": "물류는 답했지만 재무는 확인하지 못했습니다."}
{"summary": "매입안을 준비했으니 확인해 주세요."}

나쁜 예
{"summary": "가용 현금이 3199만원입니다."}              ← 숫자를 썼다
{"summary": "자금은 넉넉한 편입니다."}                  ← 값을 못 봤으면서 평가했다
{"summary": "지금 매입하시는 것이 좋겠습니다."}          ← 권고를 지어냈다
{"summary": "일부 정보는 확인되지 않았습니다."}          ← 다 답했는데 못 봤다고 했다"""


def narrative_schema() -> dict[str, Any]:
    """구조화 출력용. `summary` 는 기본값이 없어 이미 `required` 다."""
    return Narrative.model_json_schema()


#: 거절 사유. ①의 `IntentIssue` 처럼 열거를 두지 않는다 — 여섯뿐이고, 이 모듈 밖으로
#: 나가지 않는다 (응답에는 `llm_status` 만 실린다).
_NOT_JSON = "NOT_JSON"
_EMPTY = "EMPTY"
_HAS_NUMBER = "HAS_NUMBER"
_TOO_LONG = "TOO_LONG"
_EVALUATED = "EVALUATED"
_INVENTED_GAP = "INVENTED_GAP"
_INVENTED_AGENT = "INVENTED_AGENT"

#: 교정 문구. ①에서 배운 것을 그대로 쓴다 — **무엇을 빼라고만 하면 모델이 답을 무른다.**
#: 그래서 뺄 것과 함께 **대신 쓸 말**을 준다.
_GUIDANCE: dict[str, str] = {
    _NOT_JSON: 'JSON 만 출력한다. {"summary": "..."} 형태다.',
    _EMPTY: "summary 를 비우지 마라. 한 문장이라도 쓴다.",
    _HAS_NUMBER: (
        "문장에서 숫자를 빼라. 금액·수량·일수는 문장 아래 표로 이미 나가므로 "
        "'상태를 확인했습니다' 처럼 옮겨 적기만 한다."
    ),
    _TOO_LONG: "한 문장으로 줄여라.",
    _EVALUATED: (
        "상태를 평가하지 마라 — 넉넉하다·부족하다·어렵다 같은 말을 빼고, "
        "'확인했습니다' 처럼 무엇을 했는지만 쓴다."
    ),
    _INVENTED_GAP: (
        "모든 부서가 답했다. 확인되지 않았다는 말을 빼고 '상태를 확인했습니다' 로 쓴다."
    ),
    _INVENTED_AGENT: "주어진 부서 이름만 쓴다. 목록에 없는 부서를 문장에 넣지 마라.",
}


class NarrativeRejected(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__(", ".join(issues))
        self.issues = issues


def validate_narrative(raw_output: str, facts: AnswerFacts | None = None) -> str:
    """문장 하나를 꺼내 검사한다.

    ★ **넷 다 "지어낸 것"을 막는 검사다** — 숫자 · 평가 · 없는 결손 · 없는 부서.
      문장을 잘 썼는지는 보지 않는다(잴 수 없다). 이 모듈이 지키는 것은 **사실과
      어긋나지 않는 것**뿐이다.

    ★ `facts` 를 주지 않으면 뒤의 둘을 건너뛴다 — 그 검사들은 "이번 요청에 무엇이
      있었나"를 알아야 판정할 수 있다.
    """
    try:
        narrative = Narrative.model_validate_json(raw_output)
    except ValidationError as error:
        raise NarrativeRejected([_NOT_JSON]) from error

    text = narrative.summary.strip()
    issues: list[str] = []
    if not text:
        issues.append(_EMPTY)
    if _DIGITS.search(text):
        issues.append(_HAS_NUMBER)
    if len(text) > _MAX_CHARS:
        issues.append(_TOO_LONG)
    if any(word in text for word in _EVALUATIVE):
        issues.append(_EVALUATED)
    if facts is not None:
        if not facts.gaps and any(word in text for word in _NEGATIVE):
            issues.append(_INVENTED_GAP)
        # ★ 대조 대상이 `to_prompt()` 인 것이 요점이다 — **모델이 볼 수 있었던 것**과
        #   맞춘다. 답한 부서·못 답한 부서·결론에 없던 이름이면 지어낸 것이다.
        seen = facts.to_prompt()
        if any(word in text and word not in seen for word in _AGENT_WORDS):
            issues.append(_INVENTED_AGENT)
    if issues:
        raise NarrativeRejected(issues)
    return text


class NarrativeService:
    """⑥ 실행기. **실패를 결과로 접는다** — 예외를 위로 올리지 않는다."""

    def __init__(self, settings: LLMSettings, provider: TextProvider) -> None:
        self.settings = settings
        self.provider = provider

    def write(self, facts: AnswerFacts) -> NarrativeResult:
        if not self.settings.enabled:
            return self._result(None, status="DISABLED", attempts=0, fallback=False)

        guidance: list[str] | None = None
        attempts = 0
        for _ in range(self.settings.max_retries + 1):
            attempts += 1
            try:
                raw = self.provider.generate(
                    SYSTEM_PROMPT,
                    _user_payload(facts, guidance),
                    narrative_schema(),
                )
                return self._result(
                    validate_narrative(raw, facts),
                    status="SUCCESS",
                    attempts=attempts,
                    fallback=False,
                )
            except NarrativeRejected as error:
                guidance = [_GUIDANCE[issue] for issue in error.issues]
            except Exception:  # noqa: BLE001 — 문장 실패가 답을 막으면 안 된다
                break
        return self._result(None, status="FALLBACK", attempts=attempts, fallback=True)

    def _result(
        self, narrative: str | None, *, status: LLMStatus, attempts: int, fallback: bool
    ) -> NarrativeResult:
        return NarrativeResult(
            narrative=narrative,
            llm_status=status,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model or None,
            llm_attempts=attempts,
            llm_fallback_used=fallback,
        )


def _user_payload(facts: AnswerFacts, guidance: list[str] | None) -> str:
    """★ **값도 항목 라벨도 넘기지 않는다.** 부서 이름과 결론뿐이다 (`to_prompt`)."""
    payload: dict[str, Any] = {"facts": facts.to_prompt()}
    if guidance:
        payload["correction"] = guidance
    return json.dumps(payload, ensure_ascii=False)


def get_narrative_service() -> NarrativeService:
    settings = get_llm_settings()
    return NarrativeService(settings, build_provider(settings))
