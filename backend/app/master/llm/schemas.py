"""의도 분류의 LLM 계약.

**출력 스키마에 payload 도 숫자 칸도 자유 문자열 에이전트 이름도 없다 — 이것이
안전장치의 전부다.** 오케 `SelectionInterpretation`·매입 `GradeMixInterpretation` 이
쓴 방법 그대로다: 프롬프트로 "만들지 마"라고 부탁하는 대신 **만들 자리를 없앤다.**

LLM 이 돌려주는 건 **닫힌 열거에서 고른 값들**뿐이고, 실제 요청(`ProcurementRunRequest`)은
규칙이 조립한다.

```text
발화문 → [LLM] → Intent(닫힌 열거) → 규칙이 요청 조립 → 기존 run_procurement()
                                      └ flow.py 는 한 줄도 안 바뀐다
```

상태 4종(`LLMStatus`)은 `app/master/envelope.py` 와 **같은 어휘**다 — 팀 공용 AI 카드가
수정 없이 동작한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.master.envelope import AgentName

#: 봉투(`envelope.LLMStatus`)와 같은 4값. 새로 만들지 않는다.
LLMStatus = Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]

#: 마스터가 알아들을 수 있는 요청 종류. **이 목록 밖은 만들 수 없다.**
#:
#: `UNKNOWN` 을 열거에 둔 것이 핵심이다 — 없으면 LLM 이 애매한 발화를 가장 가까운
#: 것으로 밀어 넣는다. 모르겠다고 말할 자리를 줘야 되물을 수 있다.
IntentAction = Literal[
    "PROCUREMENT_RUN",
    "STATUS_QUERY",
    "RERUN_WITH_CONDITION",
    "SELECT_SCENARIO",
    "UNKNOWN",
]

#: 품목. 매입이 도는 4종으로 닫는다 — 없는 품목을 지어낼 자리가 없다.
ItemName = Literal["배추", "무", "양파", "피마늘"]

Confidence = Literal["HIGH", "MEDIUM", "LOW"]


class Intent(BaseModel):
    """LLM 의 응답. **고른 값들이 전부다.**

    ⚠️ `min_length` 같은 문자열 제약을 걸지 않는다. 이 모델의 `model_json_schema()` 가
    그대로 구조화 출력 API 에 실리는데 Anthropic 구조화 출력과 OpenAI strict 모드가
    **둘 다** 문자열 길이 제약을 지원하지 않는다. 빈 값 검사는 `runtime.validate_intent`
    가 한다 — 타입이 못 막는 건 검증기가 막는 원칙 그대로다.
    """

    model_config = ConfigDict(extra="forbid")

    action: IntentAction

    #: `STATUS_QUERY` 일 때만 채운다. 열거라 없는 에이전트를 부를 수 없다.
    agents: list[AgentName] = Field(default_factory=list)

    #: 이번 요청이 다루는 품목. 못 알아내면 비운다 — **추측해서 채우지 않는다.**
    #: 비면 규칙이 되묻는다(매입이 `missing_data: ["item"]` 을 내는 것보다 낫다).
    item: ItemName | None = None

    #: `SELECT_SCENARIO` 일 때 사용자가 지목한 안. 라벨이 실제로 제시된 것인지는
    #: **`decision_service` 가 그 실행의 응답과 대조**한다 — 여기서 막지 않는다.
    scenario_label: str | None = None

    #: `RERUN_WITH_CONDITION` 일 때 사용자가 붙인 조건. **사용자의 말 그대로** 옮긴다.
    #: 숫자를 지어내지 못하게 `runtime` 이 발화문과 대조한다.
    condition: str | None = None

    confidence: Confidence


class IntentResult(BaseModel):
    """서비스가 돌려주는 결과. 상태 필드는 팀 공통이다."""

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    llm_status: LLMStatus
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_attempts: int = Field(default=0, ge=0)
    llm_fallback_used: bool = False

    #: 실행 전에 사람에게 확인받아야 하는가.
    #:
    #: **오분류 비용이 비대칭이라 둔 장치다.** `STATUS_QUERY` 를 잘못 고르면 사용자가
    #: 다시 물으면 그만이지만, `PROCUREMENT_RUN` 을 잘못 고르면 호출 예산 12회와
    #: 매입 LLM 호출을 태운다.
    needs_confirmation: bool = False

    #: 되물을 말. `UNKNOWN` 이거나 확인이 필요할 때만 채운다.
    clarification: str | None = None
