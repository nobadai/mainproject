"""
sales_flow.py — 판매 의사결정 Flow **골격** (판매 v1.7 · 마스터 설계 2026-09-06)

    ① mock 입력 검사                        섞였으면 아무것도 시작하지 않는다
    ② inventory / PRE_SALES                 판매가능·납기 초기 컨텍스트
    ③ sales / GENERATE_SALES_PROPOSAL       후보 여럿
    ④ 후보마다 required_validations 라우팅   CAPABILITY_ROUTING 으로 (agent, mode)
    ⑤ 통과 후보 ≥ 1 → 제시 · == 0 → 되먹임(최대 2회) → 그래도 0 이면 탈락

★ **매입 `flow.py` 를 고치지 않는다.** 뼈대는 닮았지만 층이 다르다 (D-3 합의).
  종료 코드도 예산도 되먹임 규칙도 따로 둔다 — 합치면 한쪽 사이클을 고칠 때마다
  다른 쪽이 흔들린다.

★ **판매와 매입이 갈리는 세 자리** (설계 §1).

  ```text
  누가 검증 대상을 정하나   매입: 마스터가 조언자를 정한다
                            판매: 후보가 capability 로 요구하고 마스터가 라우팅한다
  밴드                      매입: 조언자가 하나라도 빠지면 시작하지 않는다
                            판매: 밴드가 없다 — 컨텍스트가 없어도 시작은 한다
  부분 통과                 매입: 안이 전부 잘리면 끝
                            판매: 후보가 여럿이고 **부분 통과가 정상이다** (C-1)
  ```

🔴 **이번 조각에 없는 것.** 라우터·서비스 진입점(`run_sales()`)·이력 적재·어댑터
  배선·최종 재검증·승인 후 Write·개장 Gate 연결. 랭킹/추천 재계산은 **판매 소유라
  영원히 여기 없다** — 마스터는 순위를 다시 매기지 않는다 (판매 v1.7 §18).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.contracts.core import SuggestedAdjustment
from app.master.budget import BudgetExhausted, CallBudget
from app.master.envelope import (
    AgentName,
    AgentReply,
    Mode,
    route_capability,
)

# 🔴 **매입 flow 에서 **읽어만** 온다 — 베끼지 않는다.**
#
#   `_PASSING_VERDICTS` 는 *"사람에게 올려도 되는 봉투 판정"* 이고 그 사실의 주인은
#   한 곳이어야 한다. 손으로 복사하면 `#173` 이 고친 자리가 판매에서 되살아난다 —
#   어휘가 늘어난 날 한쪽만 늘어난다.
#
#   `_wire` 도 같다. `SuggestedAdjustment.split_date` 를 ISO 로 펴는 규칙이 두 벌이
#   되면, JSON 왕복 성질(`test_전선에_실은_것은_왕복해도_같다`)이 판매 경로에서만
#   조용히 깨진다.
#
# ★ **제자리는 봉투(또는 `app/contracts/core.py`)다.** 옮기려면 `flow.py` 를 고쳐야
#   하는데 이번 조각은 매입 파일을 건드리지 않기로 했다. 옮기는 날 여기 import 도
#   같이 따라간다.
from app.master.flow import _PASSING_VERDICTS, AgentFailure, SourcedEvidence, _wire
from app.master.plan import ExecutionPlan
from app.master.ports import AgentNotRegistered
from app.master.runner import MasterRunner

SalesEndCode = Literal[
    "SL1_PRESENTED",
    "SL2_NO_CANDIDATE",
    "SL3_ALL_REJECTED",
    "SL4_NOT_STARTED",
    "SL5_BUDGET_EXHAUSTED",
]
"""판매 사이클 종료 코드.

```text
SL1_PRESENTED         통과 후보 ≥ 1 — 사용자에게 제시한다 (탈락안 사유 동봉)
SL2_NO_CANDIDATE      판매가 안을 만들지 못했다 (missing_data / missing_capability)
SL3_ALL_REJECTED      안은 있었으나 전부 탈락 — 되먹임까지 끝났다
SL4_NOT_STARTED       시작하지 못했다 (어댑터 미등록 · mock 입력)
SL5_BUDGET_EXHAUSTED  예산 소진 — 판단이 끝나지 않았다
```

🔴 **매입 `EndCode`(E1~E5) 에 값을 더하지 않는다** (D-3 합의). 층이 다르다. 한 어휘에
  두 사이클을 담으면 `E2_HELD` 가 *"매입 보류"* 와 *"판매 보류"* 를 동시에 뜻하게 되고,
  화면과 이력이 어느 사이클의 종료인지를 payload 로 되짚어야 한다.

🔴 **예산 소진을 `SL3` 으로 접지 않는다 — 매입과 일부러 다르다.**

  매입은 `BudgetExhausted` 를 `E3_REJECTED` 로 바꾼다 (`flow.py:378-380`). 그러면
  **"다 봤는데 안 된다"** 와 **"다 못 봤다"** 가 **같은 코드**가 된다.

  매입 쪽을 지금 고칠 일은 아니지만, **새로 만드는 어휘를 같은 모양으로 만들 이유는
  없다.** 판매는 사용자에게 후보를 직접 보여주는 경로라, 못 본 것을 거절로 적으면
  화면이 거짓말을 한다 — 사용자는 *"이 조건으로는 안 된다"* 로 읽고 조건을 바꾸는데,
  실제로는 **판단이 끝나지 않은 것**이라 같은 조건으로 다시 돌리는 것이 맞다.
"""

MAX_FEEDBACK_ATTEMPTS = 2
"""되먹임 상한. **이 값의 소유자는 마스터다** (매입 `MAX_PURCHASE_ATTEMPTS` 와 같은 이유).

되먹임은 조정 행위이므로 조정자가 소유한다. 제안자는 자기 안을 몇 번 다시 만들지
정하지 않는다 — 그건 예산과 종료 코드를 쥔 쪽의 판단이다 (§1.2-12).

★ **되먹임 회차(`feedback_attempt`)는 호출 순번(`call_seq`)과 다르다** (C-5).
  `S-2`(ERROR 1회 재시도)가 끼면 `call_seq` 만 오르고 회차는 그대로다. 두 수를 한
  칸에 담으면 *"두 번 고쳐 보라고 시켰다"* 와 *"한 번 시켰는데 어댑터가 한 번
  터졌다"* 가 이력에서 같아 보인다.

★ 2 인 근거는 매입과 같다 — 상한이 아니라 **호출 예산이 실제 제동**이고, 회차는
  *"고칠 기회를 몇 번 주나"* 다. 더 나은 안은 사용자가 `RERUN_WITH_CONDITION` 으로
  요청한다 (C-2).
"""

SALES_BUDGET = 16
"""판매 사이클 기본 호출 예산.

```text
후보 3 · 되먹임 2회 최악 경우
  inventory PRE_SALES              1
  sales GENERATE_SALES_PROPOSAL    3   (최초 1 + 되먹임 2)
  finance SALES_VALIDATION         9   (후보 3 × 회차 3)
  ────────────────────────────────────
                                  13
+2  후보 범위·날짜가 바뀌어 물류를 다시 부르는 경우 (판매 v1.7 §5)
+1  S-2 (ERROR 1회 재시도) 여유
────
                                  16
```

🔴 **매입 기본값 12 (`schemas.py` `ProcurementRunRequest.budget`) 를 건드리지
  않는다.** 사이클이 다르면 예산도 다르다. 매입 값을 16 으로 올리면 매입이 안 쓰는
  4 회분 상한이 매입 쪽에서 풀린다.

🔴 **재무 `SALES_VALIDATION` 이 한 번에 한 후보만 받는다** (`app/finance/capabilities/
  sales.py` `parse_sales_validation_input` — `scenario_id`·`quantity_kg` 가 전부 단수).
  배열을 안 받으므로 **후보 3개면 재무를 3번 부른다.** 매입에 물어 둔 batch /
  ONE_BY_ONE 질문이 판매 쪽에서는 이미 ONE_BY_ONE 으로 정해진 셈이고, 재무가 그렇게
  구현한 것을 마스터가 바꿀 수 없다.

⚠️ **예산이 후보 수에 끌려간다.** 판매가 후보를 하나 더 내면 회차당 1 씩 더 붙는다.
  매입 라우팅(`ADDITIONAL_SUPPLY_CONTEXT`)이 열리면 후보당 1 이 또 붙으므로 **그때
  다시 센다.**

★ **`+2` 와 `+1` 은 아직 쓰이지 않는다.** 물류 재조회(S-1 범위 이탈)도 ERROR 재시도
  (S-2)도 이 골격에는 배선이 없다 — 예산에 자리를 잡아 둔 것과 배선한 것은 다르다.
"""


def sales_call_budget(limit: int = SALES_BUDGET) -> CallBudget:
    """판매 사이클 `CallBudget`. **진입점이 여기를 거친다.**

    ★ 상수를 각자 읽어 `CallBudget(limit=16)` 을 만들면 그 순간 주인이 여럿이 된다.
      아직 진입점(`run_sales()`)이 없어 호출자는 테스트뿐이지만, **자리를 먼저 하나로
      둔다** — 나중에 만드는 쪽이 상수를 다시 옮겨 적지 않게.
    """
    return CallBudget(limit=limit)


INITIAL_CONTEXT_ROUTE: tuple[AgentName, Mode] = ("inventory", "PRE_SALES")
"""②에서 부르는 초기 컨텍스트. **라우팅표와 같은 값을 손으로 적지 않는다.**

S-1(기여 호출 재사용)은 *"판매가 요구한 capability 의 라우팅이 ②와 같은 곳을 가리키면
그 회신을 다시 쓴다"* 로 판정한다. capability 이름 목록을 따로 두면
`CAPABILITY_ROUTING` 이 바뀐 날 한쪽만 바뀐다 — 여기는 **경로 하나**만 안다.
"""


@dataclass(frozen=True)
class CandidateVerdict:
    """후보 하나에 대한 판정 — **무엇을 물었고 무엇이 왔는가.**

    ★ **후보 단위다** (설계 정정 ① · 2026-09-06). 판매 v1.7 §4 는
      `required_validations` 를 최상위 배열로 적었지만 구현은 시나리오별이다
      (`app/sales/schemas.py` `SalesScenario.required_validations`). 1안은 재무만,
      2안은 재무+매입 식으로 **후보마다 요구가 다를 수 있다.**

    ★ **통과/탈락을 필드로 들지 않는다.** `passed` 는 `validations` 와 `unroutable`
      에서 나오는 값이라 필드로 두면 같은 사실의 주인이 둘이 된다.
    """

    #: 판매가 낸 후보 그대로. **마스터는 고르지도 재계산하지도 않는다** (§3.2.2).
    scenario: Mapping[str, Any]

    #: capability → 그 검증의 회신. 키는 판매가 요구한 이름 그대로다.
    validations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    #: 🔴 **부를 대상이 없어 못 물어본 요구.** 조용히 버리지 않는다 (설계 §3).
    #: 이 칸이 비어 있지 않으면 그 후보는 **통과로 치지 않는다** — 미해결로 둔다.
    unroutable: tuple[str, ...] = ()

    @property
    def scenario_id(self) -> str:
        return str(self.scenario.get("scenario_id") or "")

    @property
    def unvalidated(self) -> bool:
        """요구한 검증이 **하나도 없었다.**

        🔴 이 후보는 통과로 나가지만 **아무도 안 본 안이다.** 마스터가 요구를 지어내지
          않기 때문이다 — 무엇이 필요한지는 제안자가 정한다 (§3.2.2). 대신 그 사실이
          여기 남아 화면이 *"검증 0건"* 을 말할 수 있다.
          *"검사하지 못한 것을 검사했다고 말하지 않는다"* (설계서 §8).
        """
        return not self.validations and not self.unroutable

    @property
    def passed(self) -> bool:
        """사용자에게 올려도 되는가.

        ★ **허용목록으로 정한다** (`_PASSING_VERDICTS`). *"reject 가 아니면 통과"* 로
          정하면 봉투 어휘가 늘 때마다 새 값이 통과 쪽으로 샌다 (#173).
        """
        if self.unroutable:
            return False
        return all(
            str(v.get("business_status") or "") in _PASSING_VERDICTS
            for v in self.validations.values()
        )

    @property
    def detail(self) -> str:
        """왜 탈락했나 — **사람이 읽는 한 줄. 여기서만 만든다.**

        ★ 마스터가 사유를 새로 쓰지 않는다. 부서가 보낸 `reasoning` 을 그대로 옮기고,
          없으면 상태값만 적는다 (§3.2.2 · `AgentFailure.detail` 과 같은 자리).
        """
        parts: list[str] = []
        if self.unroutable:
            parts.append(f"부를 대상이 없는 요구: {', '.join(self.unroutable)}")
        for capability, verdict in self.validations.items():
            business = str(verdict.get("business_status") or "?")
            if business in _PASSING_VERDICTS:
                continue
            runtime = str(verdict.get("runtime_status") or "?")
            head = f"{capability}({runtime}/{business})"
            why = str(verdict.get("reasoning") or "").strip()
            parts.append(f"{head}: {why}" if why else head)
        return " / ".join(parts) if parts else "통과"


@dataclass(frozen=True)
class SalesOutcome:
    """판매 Flow 한 번의 결과. **무엇을 못 했는지도 담는다.**

    ★ 매입 `ProcurementOutcome` 과 모양이 닮았지만 담는 것이 다르다. 매입은
      *"시나리오 배열 + 부서별 판정"* 이고 판매는 **후보마다 자기 판정을 들고 있다** —
      부분 통과가 정상이라 부서 축으로 접으면 어느 후보가 왜 떨어졌는지가 사라진다.
    """

    end_code: SalesEndCode
    reason: str
    plan: ExecutionPlan

    #: 통과·탈락을 **한 칸에** 담는다. 가르는 것은 아래 property 다 — 두 칸으로 두면
    #: 같은 후보가 양쪽에 들어가는 날을 아무도 못 막는다.
    candidates: tuple[CandidateVerdict, ...] = ()

    #: `scenarios` 를 뺀 제안 최상위 — 판매의 `situation`·`business_mode`·`self_check`.
    #: 매입 `_judgment_of` 와 같은 자리이고, **키를 고르지 않는다.**
    judgment: Mapping[str, Any] = field(default_factory=dict)

    #: ②에서 받은 초기 물류 컨텍스트. **못 받았으면 비어 있고** 그 사유는 아래 칸에 있다.
    supply_context: Mapping[str, Any] = field(default_factory=dict)

    #: 🔴 **물류가 컨텍스트를 못 냈다는 사실.** 판매는 밴드가 없어 여기서 멈추지 않지만,
    #: 멈추지 않는 것과 없던 일로 하는 것은 다르다 — 후보의 질이 왜 떨어졌는지를
    #: 나중에 읽는 사람이 볼 수 있어야 한다.
    context_failure: AgentFailure | None = None

    evidences: tuple[SourcedEvidence, ...] = ()
    adjustments: tuple[SuggestedAdjustment, ...] = ()

    #: 실제로 돈 되먹임 회차. **0 이면 되먹임하지 않았다** (통과 후보가 있었거나,
    #: 권위 있는 대안이 없어 다시 물어도 같았거나).
    feedback_attempts: int = 0

    @property
    def presented(self) -> tuple[CandidateVerdict, ...]:
        return tuple(c for c in self.candidates if c.passed)

    @property
    def rejected(self) -> tuple[CandidateVerdict, ...]:
        """탈락 후보. **SL1 에서도 비어 있지 않을 수 있다** — 사유를 동봉해 함께 낸다."""
        return tuple(c for c in self.candidates if not c.passed)

    @property
    def unroutable_capabilities(self) -> tuple[str, ...]:
        """이번 실행에서 **못 불러 본 요구**의 전부. 후보들 것을 모아 이름만 남긴다."""
        return tuple(sorted({cap for c in self.candidates for cap in c.unroutable}))

    @property
    def presentable(self) -> bool:
        return self.end_code == "SL1_PRESENTED" and bool(self.presented)


class SalesFlow:
    """판매 Flow 실행기.

    ★ 요청마다 새로 만든다 — `MasterRunner` 가 요청 단위이기 때문이다.
    """

    def __init__(
        self,
        runner: MasterRunner,
        user_request: Mapping[str, Any] | None = None,
        mocked_inputs: Sequence[str] = (),
        max_feedback_attempts: int = MAX_FEEDBACK_ATTEMPTS,
    ) -> None:
        self.runner = runner
        #: 사용자가 말한 조건 그대로. **숫자로 바꿔 제약에 꽂지 않는다** — 해석은
        #: 판매가 한다 (§3.2.2 · 매입 `prior_feedback` 과 같은 자리).
        self.user_request = user_request
        #: 🔴 **mock 에서 온 입력. 하나라도 있으면 실행을 세운다** (매입과 같은 태도).
        #: 경고와 차단은 다르다 — mock 으로 내린 결론은 실측으로 읽히면 안 되는 정도가
        #: 아니라 **아예 내리면 안 되는** 것이다.
        self.mocked_inputs: tuple[str, ...] = tuple(mocked_inputs)
        self.max_feedback_attempts = max_feedback_attempts

        #: ②의 회신을 담아 둔다. **S-1 재사용의 원본이다** — 같은 회신을 두 번 부르지
        #: 않는다는 것을 이 한 칸이 보증한다.
        self.supply_context: Mapping[str, Any] | None = None
        self.context_failure: AgentFailure | None = None

        self.sourced_evidences: list[SourcedEvidence] = []
        self.suggested_adjustments: list[SuggestedAdjustment] = []

    # ── 진입점 ──────────────────────────────────────────────────

    def run(self) -> SalesOutcome:
        """끝까지 돌린다.

        🔴 **예산 소진을 탈락으로 접지 않는다** (`SalesEndCode` 참조). 매입이
          `E3_REJECTED` 로 접는 자리에서 판매는 `SL5_BUDGET_EXHAUSTED` 로 남긴다.

        ★ **어댑터 미등록은 오류가 아니라 상태다** (§5.3). `AgentNotRegistered` 는
          `MasterRunner._invoke` 가 값으로 안 바꾸고 올리는 유일한 실패인데, 그것을
          여기서 종료 코드로 받는다 — 배선 전에도 이 Flow 가 *"시작하지 못했다"* 를
          정확히 말할 수 있어야 하기 때문이다.
        """
        try:
            return self._run()
        except BudgetExhausted as exc:
            return self._outcome("SL5_BUDGET_EXHAUSTED", f"호출 예산 소진: {exc}")
        except AgentNotRegistered as exc:
            return self._outcome("SL4_NOT_STARTED", f"에이전트 미등록: {exc}")

    def _run(self) -> SalesOutcome:
        # ① mock 이 섞이면 아무것도 시작하지 않는다 — 부르기 전에 선다.
        #   한 번이라도 부르면 그 회신이 이력에 남고, 나중에 읽는 사람이
        #   **"돌긴 돌았다"** 로 읽는다.
        if self.mocked_inputs:
            keys = " · ".join(self.mocked_inputs)
            return self._outcome(
                "SL4_NOT_STARTED",
                f"mock 입력으로는 판단하지 않는다: {keys}. "
                f"실 데이터를 못 읽은 것이므로 그 조회부터 고쳐야 한다",
            )

        # ② 초기 물류 컨텍스트.
        self._collect_supply_context()

        candidates: tuple[CandidateVerdict, ...] = ()
        judgment: Mapping[str, Any] = {}
        feedback: Mapping[str, Any] | None = None
        attempt = 0

        while True:
            # ③ 판매에게 후보를 받는다.
            proposal = self.runner.call(
                "sales", "GENERATE_SALES_PROPOSAL", self._proposal_input(feedback)
            )
            judgment = _judgment_of(proposal)
            self.sourced_evidences.extend(
                SourcedEvidence("sales", "GENERATE_SALES_PROPOSAL", ev) for ev in proposal.evidences
            )

            if not proposal.contributes_to_band:
                # 🔴 **`SL4` 가 아니라 `SL2` 다.** `SL4` 는 *"부르기 전에 못 섰다"* 이고,
                #   여기는 부른 뒤다. 사용자가 보는 사실은 **후보가 없다**는 것이고,
                #   왜인지는 부서가 쓴 문장 그대로 사유에 실린다.
                failure = self._failure_of("sales", "GENERATE_SALES_PROPOSAL")
                return self._outcome(
                    "SL2_NO_CANDIDATE",
                    f"판매 에이전트 미가동: {failure.detail}",
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            scenarios = _scenarios_of(proposal)
            if not scenarios:
                # ★ **되먹임 회차에서 후보가 사라져도 여기로 온다.** 그 회차가 소유하는
                #   사실은 *"안을 못 만들었다"* 이고, 앞 회차의 탈락안은 사유와 함께
                #   `candidates` 에 그대로 실려 나간다 — 둘 다 화면에 남는다.
                return self._outcome(
                    "SL2_NO_CANDIDATE",
                    _no_candidate_reason(proposal, attempt),
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            # ④ 후보마다 라우팅해 판정을 받는다.
            before = len(self.suggested_adjustments)
            candidates = tuple(self._judge(scenario) for scenario in scenarios)
            fresh_adjustments = len(self.suggested_adjustments) - before

            # ⑤ 통과 후보가 하나라도 있으면 **되먹임하지 않고 제시한다** (C-1).
            #
            #   🔴 전체 되먹임을 걸면 **통과했던 안까지 바뀌어** 사용자가 볼 수 있던
            #     안이 사라진다. 더 나은 안은 사용자가 `RERUN_WITH_CONDITION` 으로
            #     요청한다 (C-2) — 마스터가 최적안을 찾아 주는 자리가 아니다.
            if any(c.passed for c in candidates):
                return self._outcome(
                    "SL1_PRESENTED",
                    "사용자 선택 대기",
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            if attempt >= self.max_feedback_attempts:
                return self._outcome(
                    "SL3_ALL_REJECTED",
                    f"되먹임 {attempt} 회에도 통과 후보 없음",
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            if not fresh_adjustments:
                # 🔴 **권위 있는 대안이 없으면 다시 물어도 같다** (C-2).
                #   되먹임에 실을 것이 없는데 부르면 호출 예산과 LLM 만 태운다
                #   (§1.2-12 · 매입 `_dept_blocked` 와 같은 판단).
                return self._outcome(
                    "SL3_ALL_REJECTED",
                    "통과 후보가 없고 부서가 낸 대안도 없다 — 다시 물어도 같다",
                    candidates=candidates,
                    judgment=judgment,
                    feedback_attempts=attempt,
                )

            attempt += 1
            feedback = self._feedback(attempt, candidates)

    # ── 단계 ────────────────────────────────────────────────────

    def _collect_supply_context(self) -> None:
        """② 물류에게 판매가능·납기 컨텍스트를 받는다.

        🔴 **밴드를 세우지 않는다.** 매입은 조언자가 하나라도 빠지면
          `band_is_formed` 로 시작조차 안 하지만, 판매는 물류가 못 답해도 **시작은
          한다** — 시나리오의 질이 떨어질 뿐이다 (설계 §1-2).

          매입의 `band_is_formed` 를 여기 쓰면 두 가지가 동시에 틀린다. 그 함수는
          `PRE_PURCHASE` 만 보므로 `PRE_SALES` 회신은 **영원히 안 선 것으로 읽히고**,
          설령 고쳐 쓰더라도 판매에 없는 게이트를 만들게 된다.

        ★ **못 받았으면 그 사실을 남긴다.** 조용히 건너뛰면 나중에 읽는 사람이
          *"물류가 괜찮다고 했다"* 로 읽는다 (§1.2-10).
        """
        agent, mode = INITIAL_CONTEXT_ROUTE
        reply = self.runner.call(agent, mode, self._context_input())
        self.sourced_evidences.extend(SourcedEvidence(agent, mode, ev) for ev in reply.evidences)
        self.suggested_adjustments.extend(reply.suggested_adjustments)
        if reply.contributes_to_band:
            self.supply_context = _verdict_of(reply)
        else:
            # 🔴 회신 자체는 버리지 않는다. 후보가 `SELLABLE_SUPPLY_CONTEXT` 를
            #   요구하면 **이 못 받은 회신이 그대로 그 자리에 들어가** 탈락 사유가
            #   된다 — 다시 부르지 않는다 (`RUNTIME_NOT_READY` 는 다시 불러도 같다).
            self.supply_context = _verdict_of(reply)
            self.context_failure = self._failure_of(agent, mode)

    def _context_input(self) -> dict[str, Any]:
        """② 에 실어 보내는 것. **사용자 조건을 그대로 나른다.**

        ★ **없으면 칸을 안 만든다.** 빈 매핑을 보내면 받는 쪽이 *"조건이 없었다"* 와
          *"마스터가 안 보낸다"* 를 구별할 수 없다 (§1.2-10).
        """
        payload: dict[str, Any] = {}
        if self.user_request is not None:
            payload["user_request"] = dict(self.user_request)
        return payload

    def _proposal_input(self, feedback: Mapping[str, Any] | None) -> dict[str, Any]:
        """③ 에 실어 보내는 것. **묶기만 한다** (§3.2.2).

        ★ **물류가 못 답한 회차에는 `supply_context` 칸을 안 만든다.** 빈 값을 실으면
          판매가 *"물류가 팔 수 있는 게 없다고 했다"* 로 읽는다. 안 실으면 판매가
          `missing_capabilities` 로 그 사실을 낸다 — 그것이 §1.2-10 이 원하는 모양이다.
        """
        payload: dict[str, Any] = {}
        if self.user_request is not None:
            payload["user_request"] = dict(self.user_request)
        if self.context_failure is None and self.supply_context is not None:
            payload["supply_context"] = dict(self.supply_context)
        if feedback is not None:
            payload["feedback_context"] = dict(feedback)
            # ★ **부서가 낸 표준형 그대로.** 고르지도 정렬하지도 병합하지도 않는다 —
            #   같은 축이 둘 이상이어도 그대로 나른다 (매입·재무 합의).
            payload["adjustments"] = [_wire(a) for a in self.suggested_adjustments]
        return payload

    def _judge(self, scenario: Mapping[str, Any]) -> CandidateVerdict:
        """④ 후보 하나의 `required_validations` 를 라우팅해 판정을 모은다.

        🔴 **요청 단위가 아니라 후보 단위다** (설계 정정 ①). 재무 `SALES_VALIDATION`
          은 배열을 안 받으므로 **후보 3개면 재무를 3번 부른다** (설계 정정 ②).

        ★ **S-1 재사용.** 라우팅이 ②와 같은 곳을 가리키면 그 회신을 다시 쓴다 — 같은
          `as_of`, 같은 요청 안이라 다시 불러도 같다.

          ⚠️ **최종 재검증에서는 재사용이 금지다** (C-3). 그쪽의 목적은 *"그 사이
            바뀌었는가"* 라 재사용이 규칙 위반이다. 최종 재검증은 승인 엔드포인트
            작업이고 이 파일에 없다 — 붙이는 사람이 이 재사용을 그리로 옮기면 안 된다.

          ⚠️ 후보의 수량·날짜 범위가 초기 조회 범위를 벗어나면 재사용하지 않고 다시
            불러야 한다 (판매 v1.7 §5). **그 범위 대조는 아직 배선하지 않았다** —
            `SALES_BUDGET` 이 +2 로 자리만 잡아 두었다.
        """
        validations: dict[str, Mapping[str, Any]] = {}
        unroutable: list[str] = []

        for capability in _required_validations(scenario):
            route = route_capability(capability)
            if route is None:
                # 🔴 **조용히 건너뛰지 않는다.** 건너뛰면 *"검증됐다"* 로 읽힌다.
                unroutable.append(capability)
                continue
            if route == INITIAL_CONTEXT_ROUTE and self.supply_context is not None:
                validations[capability] = self.supply_context
                continue
            agent, mode = route
            # ★ 후보를 **그대로** 보낸다. 재무 `parse_sales_validation_input` 이 읽는
            #   `scenario_id`·`quantity_kg`·`supply` 가 전부 후보 최상위에 있다 —
            #   마스터가 골라 담으면 판매가 필드를 늘린 날 조용히 빠진다.
            reply = self.runner.call(agent, mode, dict(scenario))
            validations[capability] = _verdict_of(reply)
            self.sourced_evidences.extend(
                SourcedEvidence(agent, mode, ev) for ev in reply.evidences
            )
            self.suggested_adjustments.extend(reply.suggested_adjustments)

        return CandidateVerdict(
            scenario=dict(scenario),
            validations=validations,
            unroutable=tuple(unroutable),
        )

    def _feedback(self, attempt: int, candidates: Sequence[CandidateVerdict]) -> dict[str, Any]:
        """다음 회차에 실을 되먹임.

        ★ **회차 이름을 판매 어휘로 쓴다.** 판매 회신이 `feedback_attempt` 로 되받으므로
          (`SalesProposalReply.feedback_attempt`) 보내는 칸도 같은 이름이다. 매입은
          `attempt` 인데, 두 사이클이 서로 다른 부서와 말하므로 **받는 쪽 낱말에 맞춘다.**

        ★ **시각·난수·외부조회를 넣지 않는다** (§3.4). 되먹임이 앞 회차 산출물에서만
          나와야 같은 입력에 같은 다음 회차가 나온다.

        ★ **사유를 요약하지 않는다.** 후보별 사유 원문(`detail`)을 그대로 옮긴다 —
          고르는 것이 곧 판단이다 (§3.2.2).
        """
        return {
            "feedback_attempt": attempt,
            "reason": f"통과 후보 0건 — 탈락 {len(candidates)}건",
            "rejected": [
                {"scenario_id": c.scenario_id, "detail": c.detail}
                for c in candidates
                if not c.passed
            ],
            "unroutable_capabilities": sorted({cap for c in candidates for cap in c.unroutable}),
        }

    def _failure_of(self, agent: AgentName, mode: Mode) -> AgentFailure:
        """기여하지 못한 부서의 **사유를 실행 계획에서 꺼낸다.**

        ★ **회신이 아니라 계획을 본다** — 매입 `_failures_of` 와 같은 이유다.
          *"무슨 일이 일어났나"* 의 단일 출처를 하나로 둔다.
        """
        step = self.runner.plan.last(agent, mode)
        if step is None:
            # 회신 자체가 없다. `RuntimeStatus` 로는 적을 수 없는 상태다.
            return AgentFailure(agent, "NOT_CALLED")
        return AgentFailure(
            agent,
            step.runtime_status,
            reasoning=step.reasoning,
            missing_data=tuple(step.missing_data),
        )

    def _outcome(self, end_code: SalesEndCode, reason: str, **kw: Any) -> SalesOutcome:
        return SalesOutcome(
            end_code=end_code,
            reason=reason,
            plan=self.runner.plan,
            # ★ **모든 종료 코드에서 싣는다.** 후보가 안 나온 날이야말로 *"무슨 근거로
            #   그렇게 됐나"* 가 필요하다 — 성공한 날만 근거를 보여주면 정작 설명이
            #   필요한 날에 화면이 침묵한다 (매입과 같은 자리).
            evidences=tuple(self.sourced_evidences),
            adjustments=tuple(self.suggested_adjustments),
            supply_context=dict(self.supply_context or {}),
            context_failure=self.context_failure,
            **kw,
        )


# ---------------------------------------------------------------------------
# 회신 읽기 — 마스터는 **꺼내기만** 한다
# ---------------------------------------------------------------------------


def _verdict_of(reply: AgentReply) -> dict[str, Any]:
    """회신 하나를 후보 판정 칸에 담는 모양으로.

    ★ **`agent`·`mode` 를 같이 담는다.** capability → 부서 매핑은 마스터만 아는
      사실이라, 담지 않으면 화면이 *"FINANCIAL_VALIDATION 이 reject"* 까지만 알고
      **누가 그렇게 말했는지**를 모른다.
    """
    return {
        "agent": reply.agent,
        "mode": reply.mode,
        "business_status": reply.business_status,
        "runtime_status": reply.runtime_status,
        "payload": dict(reply.payload),
        "reasoning": reply.reasoning,
        "missing_data": tuple(reply.missing_data),
    }


def _required_validations(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    """후보가 요구한 capability. **순서를 지킨다** — 판매가 적은 차례 그대로 부른다.

    ★ 문자열이 아닌 것은 버린다. 어휘 밖 문자열은 **안 버린다** — 그것은
      `route_capability` 가 `None` 으로 받아 `unroutable` 에 남는다.
    """
    raw = scenario.get("required_validations")
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _scenarios_of(reply: AgentReply) -> tuple[Mapping[str, Any], ...]:
    raw = reply.payload.get("scenarios", ())
    if isinstance(raw, Mapping) or not isinstance(raw, Sequence):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _judgment_of(reply: AgentReply) -> Mapping[str, Any]:
    """`scenarios` 를 뺀 제안 최상위. **키를 고르지 않는다.**

    화이트리스트로 뽑으면 판매가 판정 필드를 추가할 때마다 마스터를 고쳐야 하고,
    빠뜨린 키는 §3.7.6 의 *"커버리지를 감춘"* 상태가 된다 (매입 `_judgment_of` 와 같다).
    """
    return {k: v for k, v in reply.payload.items() if k != "scenarios"}


def _no_candidate_reason(reply: AgentReply, attempt: int) -> str:
    """`SL2` 사유 한 줄. **판매가 쓴 문장을 그대로 쓰고, 없으면 봉투 칸을 적는다.**

    ★ 봉투에 이미 자리가 있는 두 칸(`missing_data`·`missing_capability`)만 읽는다.
      판매 payload 의 `missing_capabilities` 를 파면 마스터가 남의 스키마를 해석하는
      것이 된다 — 그 값은 `judgment` 에 통째로 실려 화면까지 간다.
    """
    parts: list[str] = [reply.reasoning.strip() or "실행 가능한 판매안이 없다"]
    if reply.missing_data:
        parts.append(f"없는 입력: {', '.join(reply.missing_data)}")
    if reply.missing_capability:
        parts.append(f"없는 capability: {', '.join(reply.missing_capability)}")
    if attempt:
        parts.append(f"되먹임 {attempt} 회차")
    return " / ".join(parts)


__all__ = [
    "INITIAL_CONTEXT_ROUTE",
    "MAX_FEEDBACK_ATTEMPTS",
    "SALES_BUDGET",
    "CandidateVerdict",
    "SalesEndCode",
    "SalesFlow",
    "SalesOutcome",
    "sales_call_budget",
]
