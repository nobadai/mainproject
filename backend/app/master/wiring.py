"""
wiring.py — 프로세스 전역 에이전트 레지스트리

마스터가 부를 수 있는 대상은 **런타임에 등록된 것뿐**이다. 각 파트가 어댑터를 만들면
여기에 등록하고, 마스터는 등록된 이름만 호출한다.

★ 어댑터가 아직 없는 것은 **오류가 아니라 상태**다.
  M-1 payload 확정 전이라 물류·매입 어댑터가 없는데, 그걸 예외로 다루면 API 가
  500 을 낸다. 실제로는 **"오늘 그 부서가 돌지 않는다"** 와 같은 상황이므로
  `E4_NOT_STARTED` 로 다뤄야 한다 (정의서 §5.3).

    AgentRegistry.get()   미등록 → 예외      ← 마스터 배선 실수
    이 모듈의 사전 점검     미등록 → 목록 반환  ← 아직 안 만든 것
"""

from __future__ import annotations

from app.master.envelope import AgentName
from app.master.flow import ADVISORS
from app.master.ports import AgentPort, AgentRegistry

_REGISTRY = AgentRegistry()

REQUIRED_FOR_PROCUREMENT: tuple[AgentName, ...] = (*ADVISORS, "purchase")


def register(agent: AgentName, port: AgentPort) -> None:
    """어댑터를 등록한다. 각 파트 모듈이 임포트 시점에 부른다."""
    _REGISTRY.register(agent, port)


def registry() -> AgentRegistry:
    return _REGISTRY


def missing(required: tuple[AgentName, ...] = REQUIRED_FOR_PROCUREMENT) -> tuple[AgentName, ...]:
    """아직 어댑터가 없는 에이전트."""
    return tuple(a for a in required if not _REGISTRY.has(a))


def reset() -> None:
    """테스트 전용 — 등록을 비운다."""
    global _REGISTRY
    _REGISTRY = AgentRegistry()
