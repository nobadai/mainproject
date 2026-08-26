"""마스터 에이전트 (정의서 v2.2 · 소유: 이현서).

`envelope` 는 마스터 ↔ 도메인 에이전트가 주고받는 **공용 계약**이다.
재무·물류·매입 파트가 전부 여기서 임포트한다 — M-1 공통 이벤트 규약 v0.2.
"""

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

__all__ = [
    "SCHEMA_VERSION",
    "AgentName",
    "AgentReply",
    "AgentRequest",
    "EnvelopeFinding",
    "ExecutionContext",
    "ExecutionMetadata",
    "LLMStatus",
    "Mode",
    "Trigger",
    "agent_allowed_modes",
    "validate_reply",
]
