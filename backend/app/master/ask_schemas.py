"""`/master/ask` 입출력 — 발화문 입구.

★ `schemas.py`(매입 Flow 계약)와 분리한다. 발화문 입구는 **화면 요구가 가장 자주
  바뀌는 자리**라, 여기가 흔들려도 에이전트 계약이 따라 흔들리면 안 된다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.master.envelope import AgentName
from app.master.llm.schemas import Intent, LLMStatus
from app.master.status_flow import StatusCode

#: 이 요청이 실제로 무엇을 했나.
#:
#: **분류와 실행을 구분하는 것이 이 필드의 전부다.** `CLASSIFIED_ONLY` 는 "알아들었지만
#: 아직 아무것도 안 했다"이고, 그 상태로 200 을 돌려주는 것이 정상 경로다.
AskOutcome = Literal[
    "CLASSIFIED_ONLY",  # 확인이 필요해 실행하지 않음
    "STATUS_ANSWERED",  # 조회를 돌려 답을 담음
    "NEEDS_CLARIFICATION",  # 못 알아들음 — 되묻는다
]


class AskRequest(BaseModel):
    """발화문 하나."""

    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(min_length=1, max_length=2000)
    as_of: date
    policy_version: str = Field(min_length=1)
    request_id: str | None = None
    budget: int = Field(default=12, ge=1, le=50)


class AskExecuteRequest(BaseModel):
    """사용자가 **확인한 의도**를 그대로 돌려보내 실행한다.

    ★ 발화문을 다시 분류하지 않는다. 재분류하면 사용자가 확인한 것과 다른 것이 돌 수
      있다 — 확인의 뜻이 사라진다. **본 것을 실행한다.**
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    as_of: date
    policy_version: str = Field(min_length=1)
    request_id: str | None = None
    budget: int = Field(default=12, ge=1, le=50)


class StatusAnswer(BaseModel):
    """조회 결과. **못 답한 부서를 감추지 않는다.**"""

    model_config = ConfigDict(extra="forbid")

    status_code: StatusCode
    reason: str
    answers: dict[AgentName, dict[str, Any]] = {}
    unavailable: list[AgentName] = []
    #: 입력이 없어 못 답한 것 — 다시 물어도 같다.
    missing_data: dict[AgentName, list[str]] = {}
    #: 호출이 터진 것 — 다시 불러 볼 값어치가 있다. `missing_data` 와 나눠 둔다.
    errors: dict[AgentName, str] = {}


class AnswerOut(BaseModel):
    """사람이 읽는 답. 마스터 역할 ⑥.

    ★ `text` 는 **규칙이 만든 사실 줄 + (있으면) LLM 문장**이다. `narrative` 가 비어도
      `text` 는 완결돼 있다 — LLM 이 답의 뼈대가 아니기 때문이다.

    ★ `status.answers` 를 지우지 않는다. 이건 **사람이 읽는 표현**이고, 화면·다른
      시스템이 쓰는 것은 여전히 구조화된 `status` 다.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    #: LLM 이 쓴 앞머리. 없으면 규칙이 만든 줄만으로 답한 것이다.
    narrative: str | None = None
    llm_status: LLMStatus
    llm_attempts: int = 0
    llm_fallback_used: bool = False


class AskResponse(BaseModel):
    """분류 결과 + (실행했다면) 그 결과."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    as_of: date
    outcome: AskOutcome

    intent: Intent
    #: 되물을 말. 확인이 필요하거나 못 알아들었을 때만 채운다.
    clarification: str | None = None
    #: 확인 후 실행하려면 이 의도를 `/master/ask/execute` 로 그대로 보낸다.
    confirm_required: bool = False

    status: StatusAnswer | None = None
    #: 사람이 읽는 답 (⑥). 실행한 경우에만 채운다 — 되묻는 경우는 `clarification` 이다.
    answer: AnswerOut | None = None

    #: ★ 아래 다섯은 **①(의도 분류)의 상태다.** ⑥의 상태는 `answer` 안에 따로 있다 —
    #: 한 요청에 LLM 호출이 둘이라 한 칸에 담으면 어느 쪽이 죽었는지 알 수 없다.
    llm_status: LLMStatus
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_attempts: int = 0
    llm_fallback_used: bool = False

    #: 실행하지 않은 이유. `CLASSIFIED_ONLY` 일 때 사람이 읽는다.
    note: str | None = None
