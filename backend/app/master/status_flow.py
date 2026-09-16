"""상태 조회 Flow — `STATUS_QUERY` 만 도는 경로.

*"오늘 재무 상태 알려줘"* 처럼 **묻기만 하는** 요청이다. 회의 3.1 의
*"사용자가 물으면 그것만 답한다"* 가 이 Flow 다.

★ **매입 Flow 와 다른 점 셋.**

```text
밴드를 만들지 않는다      조회는 제약이 아니다. band_is_formed 를 안 본다
매입을 부르지 않는다      시나리오를 만들 이유가 없다
검증 Tool 을 안 돌린다    검사할 제안이 없다 — 억지로 돌리면 skipped 만 늘어난다
```

★ **그래도 예산은 센다.** 조회도 에이전트 왕복이다. 예산 밖에 두면 *"조회를 100번
  하면 공짜"* 가 된다.

★ **종료 코드(E1~E5)를 쓰지 않는다.** 저 다섯은 매입 의사결정의 어휘라 조회에 붙이면
  뜻이 무너진다 — `E1_APPROVED`("통과안이 있다")를 상태 조회 결과에 쓸 수는 없다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.master.budget import BudgetExhausted
from app.master.envelope import AgentName, agent_allowed_modes
from app.master.plan import ExecutionPlan
from app.master.runner import MasterRunner

#: 조회 결과 상태. 매입의 `EndCode` 와 **섞지 않는다.**
StatusCode = Literal[
    "S1_ANSWERED",  # 물어본 부서가 전부 답했다
    "S2_PARTIAL",  # 일부만 답했다 — 못 답한 부서를 밝힌다
    "S3_UNAVAILABLE",  # 아무도 못 답했다
]


@dataclass(frozen=True)
class StatusOutcome:
    """조회 한 번의 결과. **무엇을 못 봤는지도 담는다.**"""

    status_code: StatusCode
    reason: str
    plan: ExecutionPlan

    answers: Mapping[AgentName, Mapping[str, Any]] = field(default_factory=dict)
    #: 물었는데 못 답한 부서. 조용히 빼지 않는다 — 빈 답과 못 받은 답은 다르다.
    unavailable: tuple[AgentName, ...] = ()
    #: 각 부서가 "무엇이 없어서" 못 답했는지 (`RUNTIME_NOT_READY`).
    missing_data: Mapping[AgentName, tuple[str, ...]] = field(default_factory=dict)
    #: 호출이 **터진** 부서와 사유 (`ERROR`).
    #:
    #: ★ `missing_data` 와 나눈다. 둘 다 밴드에 기여하지 않아 fail-safe 는 같지만
    #:   **재시도 가치가 다르다** — 실행 실패(예외·타임아웃)는 다시 불러 볼 값어치가
    #:   있고, 입력이 없어서 못 낸 답은 다시 불러도 같다 (`ports.error_reply` 주석).
    #:   한 칸에 담으면 어댑터가 터진 날과 값이 없던 날이 이력에서 같아 보인다.
    errors: Mapping[AgentName, str] = field(default_factory=dict)

    @property
    def runtime_status(self) -> str:
        """적재용. **아무도 못 답한 경우만 미가동이다.**

        일부라도 답했으면 돌긴 돈 날이다 — 매입 Flow 에서 `E4` 만
        `RUNTIME_NOT_READY` 인 것과 같은 구분이다.
        """
        return "RUNTIME_NOT_READY" if self.status_code == "S3_UNAVAILABLE" else "READY"


class StatusFlow:
    """조회 실행기. 요청마다 새로 만든다 (`MasterRunner` 가 요청 단위)."""

    def __init__(
        self,
        runner: MasterRunner,
        agents: tuple[AgentName, ...],
        *,
        question: str | None = None,
        item: str | None = None,
    ) -> None:
        self.runner = runner
        self.agents = agents
        #: 사용자 발화 원문과 의도의 품목. 싣는 자리가 다르다 (`_payload_for`).
        #:
        #: ```text
        #: ml         question + item      원문과 품목을 둘 다 받는다
        #: inventory  question 만          품목은 안 싣는다 (아래 이유)
        #: 나머지     빈 payload           수신 계약을 바꾸지 않는다
        #: ```
        #:
        #: 🔴 **물류에 품목을 싣지 않는 이유.** 품목 해석의 주인은 물류 하나다. 마스터가
        #:   분류한 품목을 같이 보내면 주인이 둘이 되고, 마스터 쪽이 먼저 좁혀 버린다 —
        #:   실측(2026-09-16 · 실 서버 · gemini-3.6-flash)에서 *"배추랑 양파 재고 얼마나
        #:   남았어?"* 의 분류 결과가 `item="배추"` 하나였다. 그대로 실어 보내면 양파가
        #:   떨어진다. 원문만 보내면 물류가 둘 다 읽는다.
        self.question = question
        self.item = item

    def run(self) -> StatusOutcome:
        try:
            return self._run()
        except BudgetExhausted as exc:
            # 조회도 예산을 센다. 소진은 예외가 아니라 결과로 접는다 (§1.2-12).
            return StatusOutcome(
                status_code="S3_UNAVAILABLE",
                reason=f"호출 예산 소진: {exc}",
                plan=self.runner.plan,
                unavailable=self.agents,
            )

    def _run(self) -> StatusOutcome:
        if not self.agents:
            return StatusOutcome(
                status_code="S3_UNAVAILABLE",
                reason="물어볼 부서가 지정되지 않았다.",
                plan=self.runner.plan,
            )

        answers: dict[AgentName, Mapping[str, Any]] = {}
        unavailable: list[AgentName] = []
        missing: dict[AgentName, tuple[str, ...]] = {}
        errors: dict[AgentName, str] = {}

        for agent in self.agents:
            if "STATUS_QUERY" not in agent_allowed_modes(agent):
                # 계약이 안 받는 mode 를 부르면 봉투가 터진다. 부르기 전에 접는다.
                unavailable.append(agent)
                missing[agent] = ("STATUS_QUERY_NOT_SUPPORTED",)
                continue

            payload = self._payload_for(agent)
            if payload is None:
                # ML 은 질문 원문이 있어야 답한다. 없는 질문을 지어내지 않고 접는다.
                unavailable.append(agent)
                missing[agent] = ("질문 원문",)
                continue

            reply = self.runner.call(agent, "STATUS_QUERY", payload)
            if reply.runtime_status == "READY":
                answers[agent] = dict(reply.payload)
                continue

            unavailable.append(agent)
            if reply.runtime_status == "ERROR":
                # 터진 것은 사유를 그대로 올린다. 안 올리면 어댑터 버그가
                # "부서가 못 답했다"와 구분되지 않는다.
                errors[agent] = reply.reasoning or "호출이 실패했다 (사유 미기재)"
            elif reply.missing_data:
                missing[agent] = tuple(reply.missing_data)

        return StatusOutcome(
            status_code=_code(answered=len(answers), asked=len(self.agents)),
            reason=_reason(answers, unavailable),
            plan=self.runner.plan,
            answers=answers,
            unavailable=tuple(unavailable),
            missing_data=missing,
            errors=errors,
        )

    def _payload_for(self, agent: AgentName) -> dict[str, Any] | None:
        """부서마다 싣는 조회 입력. **질문 원문은 ML 과 물류가 받는다.**

        ★ ML 은 `{"question": 원문, "item": 품목}` 이다. 품목이 없으면 키를 뺀다
          (비워 보내면 *"품목이 없다"* 가 아니라 *"빈 품목"* 을 물은 것이 된다).
          🔴 ML 은 질문 원문이 없으면 `None` 이다 — 부르지 않는다.
        ★ 물류(`inventory`)는 `{"question": 원문}` 이다. **품목은 안 싣는다** —
          품목 해석의 주인은 물류 하나다 (`__init__` 주석의 실측이 그 이유다).
          🔴 **원문이 없어도 부른다.** 빈 payload 로 부르는 기존 호출이 그대로 살아
          있어야 한다 — 바로가기 버튼처럼 발화문 없이 오는 경로가 그 길이다.
          여기서 `None` 을 돌려주면 그 경로가 통째로 `unavailable` 이 된다.
        ★ 나머지 부서는 지금처럼 빈 payload 다 — 수신 계약을 바꾸지 않는다.
        """
        question = (self.question or "").strip()
        if agent == "ml":
            if not question:
                return None
            payload: dict[str, Any] = {"question": question}
            if self.item:
                payload["item"] = self.item
            return payload
        if agent == "inventory" and question:
            return {"question": question}
        return {}


def _code(*, answered: int, asked: int) -> StatusCode:
    if answered == 0:
        return "S3_UNAVAILABLE"
    if answered < asked:
        return "S2_PARTIAL"
    return "S1_ANSWERED"


def _reason(answers: Mapping[AgentName, Mapping[str, Any]], unavailable: list[AgentName]) -> str:
    if not answers:
        return f"물어본 부서가 답하지 못했다: {', '.join(unavailable)}"
    if unavailable:
        return f"{', '.join(answers)} 는 답했고 {', '.join(unavailable)} 는 답하지 못했다."
    return f"{', '.join(answers)} 상태를 조회했다."
