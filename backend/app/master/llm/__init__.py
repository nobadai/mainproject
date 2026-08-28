"""마스터 LLM 계층 — 의도 분류(①)와 사용자 응답 생성(⑥).

★ **둘의 안전장치가 다르다.** ①은 출력이 전부 닫힌 열거라 **타입이 막고**, ⑥은 자유
  문장이라 타입이 못 막아 **숫자를 쓰면 거부**한다 (숫자는 규칙이 만든다).

★ **LLM 은 발화문을 타입으로 바꾸는 데까지만 한다.** 호출 순서·재호출 판단은 규칙이
  소유한다 — LLM 이 순서를 정하면 재현성·회송 상한·승인 정지가 동시에 흔들린다
  (정의서 §3 · 8/26 회의).
"""

from app.master.llm.schemas import (
    Intent,
    IntentAction,
    IntentResult,
    LLMStatus,
    Narrative,
    NarrativeResult,
)

__all__ = [
    "Intent",
    "IntentAction",
    "IntentResult",
    "LLMStatus",
    "Narrative",
    "NarrativeResult",
]
