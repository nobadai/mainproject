"""
ports.py — 에이전트 호출 접점과 레지스트리 (마스터 본체 1/3)

마스터는 **에이전트를 부르고, 어떤 Tool 을 쓸지는 에이전트가 정한다** (정의서 §2.3 2단 호출).
따라서 마스터가 아는 것은 `AgentPort` 하나뿐이다 — 그 안에서 무엇이 일어나는지 모른다.

    AgentPort = (AgentRequest) -> (AgentReply, ExecutionMetadata)

★ 실패를 예외가 아니라 **값**으로 다룬다 (정의서 §7.1 · M-1 §5).
  에이전트 호출이 터져도 마스터는 계속 판단할 수 있어야 한다. 예외를 위로 던지면
  **에이전트 하나의 실패가 사이클 전체를 죽인다.**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.master.envelope import (
    AgentName,
    AgentReply,
    AgentRequest,
    ExecutionMetadata,
)


class MasterError(Exception):
    """마스터 본체의 오류. 도메인 계약 위반(`ContractViolation`)과 구분한다."""


class AgentNotRegistered(MasterError):
    pass


@runtime_checkable
class AgentPort(Protocol):
    """도메인 에이전트 호출 접점.

    ★ 구현체는 **자기 도메인 Tool 을 선택·호출**한 뒤 결과를 봉투에 담아 돌려준다.
      마스터는 그 선택에 관여하지 않는다.
    """

    def __call__(self, request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]: ...


class AgentRegistry:
    """이름 → 포트. 마스터가 부를 수 있는 대상의 전부다."""

    def __init__(self) -> None:
        self._ports: dict[AgentName, AgentPort] = {}

    def register(self, agent: AgentName, port: AgentPort) -> None:
        self._ports[agent] = port

    def get(self, agent: AgentName) -> AgentPort:
        try:
            return self._ports[agent]
        except KeyError:
            raise AgentNotRegistered(
                f"{agent} 에이전트가 등록되지 않았다. 등록된 것: {sorted(self._ports)}"
            ) from None

    def has(self, agent: AgentName) -> bool:
        return agent in self._ports

    @property
    def registered(self) -> tuple[AgentName, ...]:
        return tuple(sorted(self._ports))


# ---------------------------------------------------------------------------
# 호출 실패 → 회신 값으로 변환
# ---------------------------------------------------------------------------


def error_reply(request: AgentRequest, reason: str) -> AgentReply:
    """호출이 터졌을 때 마스터가 대신 만드는 회신.

    ★ `ERROR` 를 쓴다 — `RUNTIME_NOT_READY` 가 아니다.
      둘 다 밴드에 기여하지 않으므로 **fail-safe 는 동일**하지만, 갈리는 것은 재시도다.
      실행이 실패한 것(예외·타임아웃)은 다시 불러 볼 가치가 있고,
      입력이 없어서 못 낸 답은 다시 불러도 같다 (M-1 §5.1).

    > 이슈 초안에는 "타임아웃 → RUNTIME_NOT_READY" 로 적었으나 구현하며 바꿨다.
      타임아웃은 **입력이 없는 상태가 아니라 실행 실패**이고, 어느 쪽이든 밴드 기여는
      막히므로 fail-safe 를 잃지 않는다. 재시도 여지를 남기는 편이 낫다.
    """
    return AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=request.agent,
        mode=request.mode,
        run_id=f"{request.agent.upper()}-ERR-{request.call_seq}",
        runtime_status="ERROR",
        business_status="skipped",
        reasoning=reason,
    )


def empty_metadata(request: AgentRequest, run_id: str) -> ExecutionMetadata:
    """호출이 터져 메타데이터를 못 받았을 때의 빈 기록.

    **빈 채로라도 남긴다.** 실행 계획에 구멍이 생기면 "부르긴 했는데 기록이 없다"와
    "아예 안 불렀다"를 구분할 수 없다 (§1.2-11).
    """
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent=request.agent,
    )
