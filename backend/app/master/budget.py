"""
budget.py — 호출 예산 강제 (마스터 본체 2/3)

정의서 §1.2-12 — **마스터의 에이전트 호출 횟수에 상한을 두고 초과 시 코드가 끊는다.**

고정 순서 파이프라인에서는 호출 수가 상수였다(부서당 1회 · 루프 예산 2회). 마스터가
순서를 스스로 정하면 **그 상한이 사라져 비용이 예측 불가**가 된다.

★ 예산은 마스터가 쥔다. 에이전트에게 남은 예산을 알려주지 않는다 (M-1 v0.2 · 재무 합의).
  노출하면 "마지막이니 보수적으로" 같은 **예산 반응이 도메인 판단을 오염**시킨다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.master.envelope import AgentName, Mode
from app.master.ports import MasterError


class BudgetExhausted(MasterError):
    """예산 소진. **마스터 본체가 잡아 종료 코드로 바꾼다** — 위로 새면 안 된다."""

    def __init__(self, limit: int, attempted: str) -> None:
        super().__init__(
            f"호출 예산 {limit} 회를 모두 썼다 (시도: {attempted}). "
            "정의서 §1.2-12 — 코드가 끊는다."
        )
        self.limit = limit
        self.attempted = attempted


@dataclass
class CallBudget:
    """한 요청에서 쓸 수 있는 에이전트 호출 횟수.

    ★ 검증 Tool 호출은 세지 않는다.
      마스터가 자기 Tool 을 부르는 것은 도메인 왕복이 아니고, 비용도 다르다
      (검증은 코드 50 + LLM 6 이며 앞 레이어에서 걸리면 LLM 까지 가지도 않는다).
    """

    limit: int = 8
    _spent: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise MasterError(f"예산은 1 이상이다 (받음: {self.limit}).")

    @property
    def spent(self) -> int:
        return len(self._spent)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    @property
    def history(self) -> tuple[str, ...]:
        return tuple(self._spent)

    def consume(self, agent: AgentName, mode: Mode) -> None:
        """호출 **직전에** 부른다. 소진이면 끊는다."""
        label = f"{agent}:{mode}"
        if self.remaining <= 0:
            raise BudgetExhausted(self.limit, label)
        self._spent.append(label)
