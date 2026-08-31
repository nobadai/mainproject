"""`/master/ask` 입출력 — 발화문 입구.

★ `schemas.py`(매입 Flow 계약)와 분리한다. 발화문 입구는 **화면 요구가 가장 자주
  바뀌는 자리**라, 여기가 흔들려도 에이전트 계약이 따라 흔들리면 안 된다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.master.decision import DecisionOut
from app.master.envelope import AgentName
from app.master.llm.schemas import Intent, LLMStatus
from app.master.schemas import ProcurementRunResponse
from app.master.status_flow import StatusCode

#: 이 요청이 실제로 무엇을 했나.
#:
#: **분류와 실행을 구분하는 것이 이 필드의 전부다.** `CLASSIFIED_ONLY` 는 "알아들었지만
#: 아직 아무것도 안 했다"이고, 그 상태로 200 을 돌려주는 것이 정상 경로다.
AskOutcome = Literal[
    "CLASSIFIED_ONLY",  # 확인이 필요해 실행하지 않음
    "STATUS_ANSWERED",  # 조회를 돌려 답을 담음
    "DECISION_RECORDED",  # 사람이 고른 안을 결정 이력에 적음
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

    #: 🔴 **결정 대상 실행의 업무 키.** `SELECT_SCENARIO` 에 필수다.
    #:
    #: **LLM 이 채울 수 없고 채워서도 안 된다.** *"기본안으로 진행해"* 라는 말에는
    #: **어느 실행의** 기본안인지가 없다. 화면은 방금 무엇을 보여줬는지 알고 있으므로
    #: 화면이 싣는다 — 서버가 "가장 최근 실행" 으로 추측하면 **엉뚱한 날의 안을
    #: 승인**할 수 있다.
    target_request_id: str | None = None

    #: 🔴 **화면이 보고 있던 실행의 이력 행 id** (2026-08-30 신설).
    #:
    #: `target_request_id` 는 **업무 키**라 한 키에 실행이 여러 행이면 어느 것인지
    #: 못 가린다 (실측: 한 키에 75행). 그 사이 재실행이 있었으면 **사람이 본 안과
    #: 다른 안이 승인된 것으로 남는다** — 라벨이 같아 눈에 안 띈다.
    #:
    #: 화면이 응답의 `history_run_id` 를 그대로 되돌려 주면 된다. 안 주면 서버가
    #: 최신 실행을 고르고, **그때는 경합이 남는다.**
    target_history_run_id: str | None = None

    #: 🔴 **승인자.** `SELECT_SCENARIO` 에 필수다.
    #:
    #: *"승인자가 없는 승인은 승인이 아니다"* (`decision.py`). **말로 골랐다고 승인자가
    #: 생기지는 않는다** — 발화문에는 신원이 없으므로 인증된 사용자를 화면이 싣는다.
    decided_by: str | None = None


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
    #: 적재된 결정. `DECISION_RECORDED` 일 때만 채운다.
    decision: DecisionOut | None = None
    #: 조건부 재요청으로 **다시 돈 실행.** `RERUN_WITH_CONDITION` 일 때만 채운다.
    #:
    #: ★ **없으면 고리가 끊긴다.** 사용자가 *"다시 해줘"* 라고 했으면 다음 동작은
    #:   **새로 나온 안 중 하나를 고르는 것**인데, 결정만 돌려주면 화면이 그 안을
    #:   그릴 수도 고를 수도 없다. 리포트 문장에는 있지만 문장에서 라벨을 긁어 쓰는 것은
    #:   화면이 서버 문장 형식에 묶이는 일이라 하지 않는다.
    run: ProcurementRunResponse | None = None
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
