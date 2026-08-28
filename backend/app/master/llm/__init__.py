"""마스터 LLM 계층 — 의도 분류.

마스터 역할 7가지 중 ①(요청 해석)이다. ⑥(사용자 응답 생성)은 아직 없다.

★ **LLM 은 발화문을 타입으로 바꾸는 데까지만 한다.** 호출 순서·재호출 판단은 규칙이
  소유한다 — LLM 이 순서를 정하면 재현성·회송 상한·승인 정지가 동시에 흔들린다
  (정의서 §3 · 8/26 회의).
"""

from app.master.llm.schemas import (
    Intent,
    IntentAction,
    IntentResult,
    LLMStatus,
)

__all__ = ["Intent", "IntentAction", "IntentResult", "LLMStatus"]
