"""마스터 에이전트 (정의서 v2.2 · 소유: 이현서).

`envelope` 는 마스터 ↔ 도메인 에이전트가 주고받는 **공용 계약**이다.
재무·물류·매입 파트가 전부 여기서 임포트한다 — M-1 공통 이벤트 규약 v0.2.

나머지 모듈은 **마스터 본체**다.

    ports    에이전트 호출 접점 · 레지스트리 · 실패를 값으로
    budget   호출 예산 강제 (정의서 §1.2-12)
    plan     실행 계획 기록 (정의서 §1.2-11)
    runner   위 셋을 묶은 호출 계층
"""

from app.master.budget import BudgetExhausted, CallBudget
from app.master.envelope import (
    SCHEMA_VERSION,
    AgentName,
    AgentReply,
    AgentRequest,
    EnvelopeFinding,
    ExecutionContext,
    ExecutionMetadata,
    LLMStatus,
    Mode,
    Trigger,
    agent_allowed_modes,
    validate_reply,
)
from app.master.plan import ExecutionPlan, ExecutionStep
from app.master.ports import (
    AgentNotRegistered,
    AgentPort,
    AgentRegistry,
    MasterError,
    empty_metadata,
    error_reply,
)
from app.master.runner import MasterRunner

__all__ = [
    "SCHEMA_VERSION",
    "AgentName",
    "AgentNotRegistered",
    "AgentPort",
    "AgentRegistry",
    "AgentReply",
    "AgentRequest",
    "BudgetExhausted",
    "CallBudget",
    "EnvelopeFinding",
    "ExecutionContext",
    "ExecutionMetadata",
    "ExecutionPlan",
    "ExecutionStep",
    "LLMStatus",
    "MasterError",
    "MasterRunner",
    "Mode",
    "Trigger",
    "agent_allowed_modes",
    "empty_metadata",
    "error_reply",
    "validate_reply",
]
