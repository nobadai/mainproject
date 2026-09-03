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
    """테스트 전용 — 등록을 비운다.

    🔴 **이것만으로는 되돌릴 수 없다** (2026-09-03 실측).

      등록은 `app/main.py` 가 **import 시점에 한 번** 한다. 여기서 비우면 그 모듈은
      이미 import 돼 있어 **다시 등록되지 않는다** — 그 프로세스에서 영영 빈 채로
      남는다.

      실제로 새어 나가고 있었다.

      ```text
      pytest tests/master tests/finance/test_finance_api.py   → 1 failed
      pytest tests/finance/test_finance_api.py tests/master   → 통과
      ```

      전체 스위트는 알파벳순이라 재무가 먼저 돌아 안 걸렸다. **부분 실행에서만
      깨지는 것이라 아무도 못 봤다.**

    ★ **부르기 전에 `snapshot()` 을 뜨고 끝나면 `restore()` 한다.** 루트
      `tests/conftest.py` 가 모든 테스트에 그것을 걸어 두므로, 이 함수를 그냥 불러도
      그 테스트 밖으로는 안 샌다.
    """
    global _REGISTRY
    _REGISTRY = AgentRegistry()


def snapshot() -> dict[AgentName, AgentPort]:
    """지금 등록 상태. **되돌리기 위한 것**이지 읽어서 판단할 값이 아니다."""
    return {name: _REGISTRY.get(name) for name in _REGISTRY.registered}


def restore(saved: dict[AgentName, AgentPort]) -> None:
    """`snapshot()` 뜬 상태로 되돌린다. **지금 등록된 것은 버린다.**

    ★ 합치지 않는다. 테스트가 남긴 등록이 섞여 나가면 되돌린 것이 아니다.
    """
    reset()
    for name, port in saved.items():
        register(name, port)
