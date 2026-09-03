"""
flow.py — 매입 의사결정 Flow (정의서 v2.2 §3.4)

    ① 재무·물류 PRE_PURCHASE        → 실행 가능 경계 수집
    ② 마스터가 매입 Input 구성       → 해석·재계산하지 않는다 (§3.2.2)
    ③ 매입 GENERATE_SCENARIOS       → 시나리오 2~3개
    ④ 재무·물류 SCENARIO_VALIDATION  → 시나리오별 판정
    ⑤ 검증 Tool                     → 정합성·누락·충돌
    ⑥ 취합 → 필요 시 매입 재호출 (예산 내) → 사용자 제시

★ **순서는 결정론이다** (이슈 설계 원칙 ③).
  의도 분류에는 LLM 을 쓰지만 여기는 규칙이다. 같은 입력에 같은 실행 계획이 나와야
  백테스트가 성립한다. LLM 이 순서를 정하면 재현성·회송 상한·승인 정지가 동시에 흔들린다.

★ **ML 예측·확정주문·정책값은 마스터가 실어 준다** (§3.2.5 의 명시적 예외).

  처음에는 "매입이 자기 Tool 로 읽는다"로 구현했으나 **매입 파트 지적으로 뒤집었다.**
  ML 은 매입의 도메인이 아니다 — 매입이 직접 읽으면 §1.2-9(자기 도메인만 조회)를 어긴다.
  그렇다고 §4.1 의 "해당 에이전트에게 요청"도 성립하지 않는다. **ML 은 호출 구조 밖의
  독립 실행이라 부를 대상 자체가 없다.** 판매 Rule(확정주문)과 경영 정책값도 같다.

  따라서 이 셋은 마스터가 실어 준다. 대신 **look-ahead 방어가 조립 시점으로 옮겨오므로**
  마스터가 `as_of` 대조를 한다 (§1.2-6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from app.contracts.core import EndCode, Evidence, ItemCode, SuggestedAdjustment
from app.master.answer import agent_label
from app.master.budget import BudgetExhausted
from app.master.envelope import AgentName, AgentReply, Mode
from app.master.plan import ExecutionPlan
from app.master.runner import MasterRunner
from app.master.verifier import VerificationContext, VerificationResult

_HAS_TIMEZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")
"""ISO 8601 오프셋이 붙었는가. `2026-09-04T06:00:00+09:00` · `...Z` 는 통과."""

_PASSING_VERDICTS: frozenset[str] = frozenset({"ok", "conditional"})
"""사람에게 올려도 되는 판정. **허용목록이다 — 부정형이 아니다.**

🔴 전에는 `business_status != "reject"` 로 정했다. *"기각이 아니면 통과"* 이므로
  **어휘가 늘 때마다 새 값이 전부 통과 쪽으로 샜다.** 통과 조건을 세는 것이 아니라
  실패 하나를 빼는 구조였다 (#173).

  2026-09-02 에 실제로 하나 늘었다 — 재무가 `SALES_VALIDATION` 을 내면서
  `READY + skipped`(`INPUT_INCOMPLETE`) 가 생겼다. 재무 코드가 마스터가 어떻게
  읽을지까지 적어 뒀다: *"마스터는 재무가 정상 판정한 것으로 읽는다."*

★ `conditional` 은 통과에 남는다. 마스터는 최적안을 고르는 자리가 아니고 사람이
  보고 정한다 (계약 §3.4). `skipped` 는 다르다 — **판정을 안 낸 것**이지
  *"조건부로 괜찮다"* 가 아니다.
"""

_JUDGED_VERDICTS: frozenset[str] = _PASSING_VERDICTS | {"reject"}
"""**부서가 판정을 낸** 값. 여기 없으면 판정 자체가 없는 것이다.

`reject` 는 통과는 아니지만 **판정이다** — 매입을 다시 부르면 고쳐질 수 있다.
`skipped` 와 어휘 밖 값은 판정이 아니다. 그중 **부서 쪽 사정인 것**만
`_dept_blocked` 가 잡는다 — `READY` 인 것은 제안이 바뀌면 채워질 수 있다.
"""

_FORECAST_ENVELOPE_KEYS: tuple[str, ...] = (
    "generated_at",
    "model_version",
    "horizon_days",
    "unit",
    "price_basis",
    "size_class",
    "grade",
)
"""ML 봉투에서 **품목 블록으로 내려보내는** 필드 (ML 규격 v0.3 §1).

★ `price_basis` · `size_class` · `grade` 가 여기 있는 이유가 중요하다.
  매입의 상승률은 **분자를 ML 예측에서, 분모를 시세 실측에서** 가져온다. 둘이 다른
  시리즈면 규격 차이를 가격 변동으로 읽고, **숫자는 멀쩡히 나오며 에러도 안 난다.**
  봉투에만 두고 안 내려보내면 매입이 대조할 값 자체를 못 받는다.
"""

ADVISORS: tuple[AgentName, ...] = ("finance", "inventory")
"""1차 조언자. 영업은 구성에서 빠졌고 판매는 2차 MVP 다 (정의서 §2.1)."""

MAX_PURCHASE_ATTEMPTS = 2
"""매입 재호출 상한. **이 값의 소유자는 마스터다.**

재시도는 조정 행위이므로 조정자가 소유한다. 부서는 자기 안을 몇 번 다시 만들지 정하지
않는다 — 그건 예산과 종료 코드를 쥔 쪽의 판단이다 (§1.2-12).

🔴 **같은 수가 매입 `constraints.yaml` 의 `feedback.attempt_max` 에도 있다.**
  그쪽은 IO명세 §1 이 요구한 **인용 선언**이고 매입 코드는 아직 읽지 않는다 (그 파일
  주석에 그렇게 적혀 있다). 인용이 원본과 갈리면 *"2회까지"* 가 두 뜻을 갖는데,
  **갈려도 에러가 안 난다** — `tests/master/test_retry_cap_ownership.py` 가 대조한다.

★ **마스터는 그 YAML 을 런타임에 읽지 않는다.** 읽으면 마스터가 부서 설정을 배우는
  것이 되고, 남의 스키마를 해석하지 않는다는 자리와 어긋난다 (물류 `scenario_results`
  를 마스터가 안 펴는 것과 같다). **대조는 테스트에서만 한다.**
"""


class VerifierPort(Protocol):
    """마스터가 직접 가진 검증 Tool (정의서 §3.7.1).

    ★ 주입하지 않으면 **검증을 건너뛴 것이 결과에 드러난다** — 통과로 치지 않는다.
      "검사하지 못한 것을 검사했다고 말하지 않는다"(설계서 §8).
    """

    def __call__(
        self,
        proposal: Mapping[str, Any],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        plan: ExecutionPlan,
        context: VerificationContext | None = None,
    ) -> VerificationResult: ...

    """★ 시나리오 배열이 아니라 **제안 전체**를 받는다.

    `allowed_axes` · `situation` · `confidence` 가 `scenarios[]` 안이 아니라 제안
    최상위에 있어서다 (2026-08-27 매입 스키마). 배열만 넘기면 그 판정을 못 본다."""


@dataclass(frozen=True)
class SourcedEvidence:
    """부서가 낸 근거 하나 + **누가 어느 모드에서 냈는가.**

    ★ `Evidence` 자체에는 부서도 모드도 없다. 봉투가 그 문맥을 들고 있기 때문이다 -
      회신이 누구 것인지는 `AgentReply.agent` 가 안다. 응답으로 나갈 때는 그 문맥이
      사라지므로 여기서 붙여 준다.

    ★ **값은 손대지 않는다.** `evidence` 는 부서가 낸 것 그대로다.
    """

    agent: AgentName
    mode: str
    evidence: Evidence


@dataclass(frozen=True)
class AgentFailure:
    """기여하지 못한 부서 하나 — **이름만이 아니라 사유까지.**

    🔴 전에는 이름만 실었다. `"경계를 내지 못한 에이전트: finance"` 를 받은 사람이
      할 수 있는 것은 *"다시 돌려 본다"* 뿐이었고, 그건 조사가 아니라 추측이다
      (재현성 측정 2026-09-02 · 6회 중 2회 실패 사유를 이력만으로는 못 봄).

    ★ **같은 파일 안에서 매입은 이미 사유를 실었다** (`_run` 의 매입 미가동 분기).
      새 규칙이 아니라 **대칭을 맞추는 것**이다.

    ★ **마스터는 해석하지 않는다** (§3.2.2). 부서가 쓴 문장을 그대로 옮긴다.
    """

    agent: AgentName

    #: `ERROR` · `RUNTIME_NOT_READY`, 그리고 **아예 안 불린 경우** `NOT_CALLED`.
    #: 마지막 것은 `RuntimeStatus` 가 아니다 — 회신이 없었다는 뜻이라 회신의 상태로는
    #: 적을 수 없다. "안 부른 것과 못 부른 것은 다르다" 를 여기서도 지킨다.
    runtime_status: str

    reasoning: str = ""
    missing_data: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        """사람이 읽는 한 줄. **여기서만 만든다.**

        사유 문장과 화면 표시를 각자 조립하면 둘이 갈린다 - 근거를 검증과 화면이
        같은 객체로 보게 한 것과 같은 이유다.
        """
        parts = [self.reasoning.strip() or self.runtime_status]
        if self.missing_data:
            parts.append(f"없는 입력: {', '.join(self.missing_data)}")
        return " / ".join(parts)


@dataclass(frozen=True)
class ProcurementOutcome:
    """Flow 한 번의 결과. **무엇을 못 했는지도 담는다.**"""

    end_code: EndCode
    reason: str
    plan: ExecutionPlan

    scenarios: tuple[Mapping[str, Any], ...] = ()
    judgment: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[AgentName, Mapping[str, Any]] = field(default_factory=dict)
    verdicts: Mapping[AgentName, Mapping[str, Any]] = field(default_factory=dict)

    #: 🔴 **부서가 낸 근거.** 값이 어디서 왔는지를 사람이 볼 수 있게 나른다 (2026-09-02).
    #:
    #: 전에는 `_collect_constraints` 가 모아 **검증에만** 넘기고 응답에서는 끊겼다.
    #: 화면은 "재무 상한 2,000만원" 은 보여주는데 그 숫자의 출처는 못 보여줬다 -
    #: 사유 문장(`verdicts[].reasoning`)은 사람이 쓴 설명이지 출처가 아니다.
    #:
    #: ★ **마스터는 고르지도 요약하지도 않는다.** `constraints`·`verdicts` 를 나르는
    #:   것과 같다 - 고르는 것이 곧 판단이다 (§3.2.2).
    #:
    #: ★ **모드를 함께 담는다.** 경계 근거(`PRE_PURCHASE`)와 판정 근거
    #:   (`SCENARIO_VALIDATION`)는 답하는 질문이 다르다 - 앞은 "상한이 왜 그 값인가",
    #:   뒤는 "이 안이 왜 ok 인가". 한 칸에 섞으면 화면이 둘을 구별하지 못한다.
    evidences: tuple[SourcedEvidence, ...] = ()

    #: 🔴 **부서가 낸 조정안 - 봉투 표준형 그대로** (2026-09-02).
    #:
    #: 전에는 `_validate` 가 `len()` 만 담고 객체를 버렸다. 사실이 아주 사라진 것은
    #: 아니어서(부서 원시형이 `verdicts[].payload` 에 남는다) 더 위험했다 -
    #: **"값이 있으니 되겠지" 로 넘어가면 마스터가 남의 payload 를 파게 된다.**
    #: 표준형은 그 해석을 안 하려고 만든 자리인데 그 자리를 비워 두고 있었다.
    #:
    #: ★ **감싸지 않는다.** `SuggestedAdjustment` 는 `dept` 를 스스로 들고 있어
    #:   `SourcedEvidence` 같은 껍데기가 필요 없다. 되먹임 계약 §3.2 의 `constraint`
    #:   가 부서를 가로지르는 평평한 배열이라 모양도 1:1 로 맞는다.
    #:
    #: ★ **개수를 따로 담지 않는다.** 세는 쪽이 센다 - 같은 사실의 주인을 둘로
    #:   만들지 않는다 (`evidences` 와 같다).
    adjustments: tuple[SuggestedAdjustment, ...] = ()

    blocked_by: tuple[AgentName, ...] = ()

    #: 🔴 **막은 부서가 왜 막았는가** (2026-09-02). `blocked_by` 는 이름만 든다.
    #:
    #: `reason` 문장에도 같은 내용이 들어가지만 문장은 사람이 읽는 것이고, 이쪽은
    #: 화면이 부서별로 펼치기 위한 것이다 — `AdvisorVerdicts` 가 판정 사유를 펴는
    #: 자리와 같다.
    blocked_failures: tuple[AgentFailure, ...] = ()

    findings: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    skipped_checks: tuple[str, ...] = ()
    verification_skipped: bool = False
    purchase_attempts: int = 0

    @property
    def presentable(self) -> bool:
        """사용자에게 선택지를 올릴 수 있는가."""
        return self.end_code == "E1_APPROVED" and bool(self.scenarios)

    @property
    def single_option(self) -> bool:
        """선택지가 하나뿐인가.

        §1.2-7 은 2개 이상을 요구하지만 §5.2 가 단일안 예외를 둔다.
        **사용자에게 보여줄지 자체가 미결(M-5)** 이라 여기서는 사실만 드러낸다.
        """
        return len(self.scenarios) == 1


class ProcurementFlow:
    """매입 Flow 실행기.

    ★ 요청마다 새로 만든다 — `MasterRunner` 가 요청 단위이기 때문이다.
    """

    def __init__(
        self,
        runner: MasterRunner,
        verifier: VerifierPort | None = None,
        advisors: tuple[AgentName, ...] = ADVISORS,
        max_purchase_attempts: int = MAX_PURCHASE_ATTEMPTS,
        item: ItemCode | None = None,
        forecast: Mapping[str, Any] | None = None,
        confirmed_orders: Mapping[str, Any] | None = None,
        policy_values: Mapping[str, Any] | None = None,
        prior_feedback: Mapping[str, Any] | None = None,
        input_sources: Mapping[str, str] | None = None,
        approved_commitments: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.runner = runner
        self.verifier = verifier
        self.advisors = advisors
        self.max_purchase_attempts = max_purchase_attempts
        self.item = item
        self.constraint_evidences: dict[AgentName, tuple[Evidence, ...]] = {}
        #: 화면까지 나갈 근거. `constraint_evidences` 와 **원본이 같다** -
        #: 검증이 본 것과 화면이 보는 것이 갈리면 "검증은 통과했는데 근거는 다른 값" 이 된다.
        self.sourced_evidences: list[SourcedEvidence] = []
        #: 부서가 낸 조정안. **마스터는 고르지도 정렬하지도 않는다** - 온 차례 그대로.
        #: 🔴 **실어 준 값이 어디서 왔는가** (2026-09-03 · 판매 요청 · 매입 A-1).
        #:
        #:   마스터는 등급을 안다 — `MEASURED` · `DERIVED` · **`MOCK`** · `MISSING`.
        #:   그런데 부서에게는 **값만** 보냈다. 응답 `input_sources` 로 화면에는
        #:   가는데 payload 에는 안 갔다.
        #:
        #: ⚠️ **부서가 스스로 조심하는 수밖에 없는 상태**였고, 그건 계약이 아니라
        #:   습관이다. 매입 `#190` 이 *"`ci_width_threshold` 는 mock 시연값"* 이라
        #:   적었는데, 정작 매입은 자기가 받은 예측이 mock 인지 payload 로 몰랐다.
        #:
        #: ★ **생성자 인자다.** 실행 시작에 정해지고 안 바뀐다 —
        #:   `forecast`·`confirmed_orders` 와 같은 성격이고, 루프에서 누적되는
        #:   `suggested_adjustments` 와 다르다 (매입 지적 2026-09-03).
        self.input_sources: Mapping[str, str] = dict(input_sources or {})

        self.suggested_adjustments: list[SuggestedAdjustment] = []

        #: 조정안 전달 대조에서 어긋난 것. **정상이면 비어 있다** — 대조군이 조용해야
        #: 어긋남이 눈에 띈다.
        self.delivery_notes: list[str] = []
        self.forecast = forecast
        self.confirmed_orders = confirmed_orders
        self.policy_values = policy_values
        #: 🔴 **어제까지 승인된 확정 입고 약정** (#185). 부서 경계 호출에 실어 보낸다.
        #:
        #: 전에는 마스터가 약정을 만들어 저장까지 해 놓고 **다음 실행에 안 실었다.**
        #: 어제 승인한 매입이 오늘 창고에 없는 것처럼 됐다 — 만들어 놓고 안 보내는
        #: 것은 값을 보내고 안 쓰는 것과 같은 병의 반대편이다.
        #:
        #: ★ **마스터가 해석하지 않는다** (§3.2.2). 약정을 그대로 나르고, 그것을
        #:   창고 점유로 볼지 현금 유출로 볼지는 받는 부서가 정한다.
        self.approved_commitments = tuple(approved_commitments)
        self.prior_feedback = prior_feedback

    # ── 진입점 ──────────────────────────────────────────────────

    def run(self, has_unmet_obligation: bool = False) -> ProcurementOutcome:
        """끝까지 돌린다.

        `has_unmet_obligation` 은 **판매 Rule 이 주는 사실**이다 (1차는 B2B 계약 납품량).
        마스터가 계산하지 않고 받아서 E5 판정에만 쓴다.
        """
        try:
            return self._run(has_unmet_obligation)
        except BudgetExhausted as exc:
            # 예산 소진은 위로 새지 않는다 — 종료 코드로 바꾼다 (§1.2-12)
            return self._outcome("E3_REJECTED", f"호출 예산 소진: {exc}")

    def _run(self, has_unmet_obligation: bool) -> ProcurementOutcome:
        constraints = self._collect_constraints()

        if not self.runner.band_is_formed(self.advisors):
            blocked = self.runner.blocking_agents(self.advisors)
            failures = self._failures_of(blocked, "PRE_PURCHASE")
            return self._outcome(
                "E4_NOT_STARTED",
                _blocked_reason(failures),
                constraints=constraints,
                blocked_by=blocked,
                blocked_failures=failures,
            )

        attempts = 0
        scenarios: tuple[Mapping[str, Any], ...] = ()
        proposal: Mapping[str, Any] = {}
        judgment: Mapping[str, Any] = {}
        verdicts: dict[AgentName, Mapping[str, Any]] = {}
        verification = VerificationResult()

        #: 다음 회차에 실을 되먹임. **1회차는 None** — 되먹임은 1회차 산출물에서 나온다.
        feedback: Mapping[str, Any] | None = None

        while attempts < self.max_purchase_attempts:
            attempts += 1
            # ★ **보낸 건수를 여기서 센다.** payload 를 만든 자리에서 세지 않으면
            #   나중에 `self.suggested_adjustments` 를 다시 세게 되는데, 그것은
            #   *"이 회차에 실제로 실려 나간 것"* 이 아니라 **누적된 것**이다.
            #   1회차에는 `feedback` 이 없어 안 실린다 — 그 차이가 사라진다.
            purchase_payload = self._purchase_input(constraints, feedback)
            sent_adjustments = len(purchase_payload.get("adjustments") or ())
            purchase = self.runner.call("purchase", "GENERATE_SCENARIOS", purchase_payload)
            proposal = dict(purchase.payload)
            scenarios = _scenarios_of(purchase)
            judgment = _judgment_of(purchase)

            # 🔴 **보낸 것과 받았다고 적힌 것을 대조한다** (2026-09-03).
            #   매입이 `meta.received_adjustments` 를 채우는데 마스터가 안 읽고 있었다 —
            #   내가 여러 파트에 지적한 *"값을 실어 주고 안 쓴다"* 의 **반대편**이다.
            delivery = self._adjustment_delivery(attempts, sent_adjustments, judgment)
            if delivery:
                self.delivery_notes.append(delivery)

            # 🔴 **제안자 근거도 모은다** (2026-09-02 · 매입 실측으로 발견).
            #
            #   근거를 모으는 곳이 `_collect_constraints` 와 `_validate` 둘뿐이었고
            #   **둘 다 `self.advisors` 를 돌았다.** 매입은 조언자 목록에 없으니
            #   구조적으로 빠졌다 — 실측에서 근거 63건 중 매입이 0건이었다
            #   (inventory 44 · finance 19).
            #
            #   **"왜 이 수량인가" 를 아는 쪽의 근거가 화면에 하나도 없었다.**
            #   멘토 지시("매입 시나리오 근거를 보이게")의 주어가 매입인데 매입만
            #   빠져 있었다.
            #
            # ★ **`ADVISORS` 에 매입을 넣지 않는다.** 그 목록은 *"경계를 내야 밴드가
            #   선다"* 는 뜻이라 매입을 넣으면 `band_is_formed` · `blocking_agents` 가
            #   전부 틀린다. 근거 수집만 조언자 목록에서 떼어낸다.
            #
            # ★ **기여 여부로 거르지 않는다.** 바로 아래 E4 분기가 같은 회신의
            #   `judgment` 를 이미 싣는다 — 근거만 버리면 *"판정은 보이는데 그 근거는
            #   없는"* 화면이 된다. `_validate` 도 거르지 않는다
            #   (`_collect_constraints` 만 거르는데, 그쪽은 안 쓰인 경계값의 근거라
            #   뜻이 다르다).
            #
            # ★ **재호출이면 두 회차가 다 남는다.** 같은 주장이 두 번 보이는 것이
            #   맞다 — 매입을 두 번 불렀다는 사실이 근거에도 남아야 한다.
            #
            # ★ **조정안은 안 모은다.** 매입은 축 조정을 제안할 권한이 없고
            #   (`_AGENT_DEPT` 에 없어 봉투가 `ContractViolation` 으로 막는다),
            #   여기서 모으면 *"올 수도 있다"* 로 읽힌다. 제안자와 조언자의 차이다.
            self.sourced_evidences.extend(
                SourcedEvidence("purchase", "GENERATE_SCENARIOS", ev) for ev in purchase.evidences
            )

            if not purchase.contributes_to_band:
                # 사유는 전부터 실었다. 바뀐 것은 **`missing_data` 도 같이 나른다**는
                # 것과, 조언자 쪽과 **같은 자리(`blocked_failures`)** 에 담는다는 것이다.
                purchase_failures = self._failures_of(("purchase",), "GENERATE_SCENARIOS")
                return self._outcome(
                    "E4_NOT_STARTED",
                    f"매입 에이전트 미가동: {purchase_failures[0].detail}",
                    judgment=judgment,
                    constraints=constraints,
                    blocked_by=("purchase",),
                    blocked_failures=purchase_failures,
                    purchase_attempts=attempts,
                )

            if not scenarios:
                # `no_proposal_reason` 이 judgment 에 실려 "왜 안이 없는지"도 응답에 남는다
                return self._outcome(
                    "E5_NO_FEASIBLE_PLAN" if has_unmet_obligation else "E2_HELD",
                    purchase.reasoning or "실행 가능한 매입안이 없다",
                    judgment=judgment,
                    constraints=constraints,
                    purchase_attempts=attempts,
                )

            verdicts = self._validate(proposal, scenarios)
            verification = self._verify(proposal, constraints, verdicts)

            if self._acceptable(scenarios, verdicts, verification.findings):
                break

            # 🔴 **판정을 못 받은 것은 재호출로 안 고쳐진다** (#173).
            #
            #   `_acceptable` 이 거짓인 이유가 갈린다. `reject`·검증 지적은 매입이
            #   안을 바꾸면 풀릴 수 있지만, **부서 쪽 사정으로 판정이 없는 것**은
            #   새 안을 줘도 같은 답이 온다.
            #
            # ⚠️ `READY` 인 판정 없음은 여기서 안 잡는다 — 재무 `INPUT_INCOMPLETE`
            #   처럼 **제안이 바뀌면 채워질 수 있는 것**이라 재호출 경로에 남긴다.
            #
            # ★ **`_acceptable` 뒤에 둔다.** 통과 여부를 정하는 자리는 하나여야 한다
            #   (`_PASSING_VERDICTS`). 여기는 *"통과가 아닌 다음에 무엇을 하나"* 만
            #   가른다 — 두 판단을 한 함수에 넣으면 둘 다 흐려진다.
            blocked = self._dept_blocked(verdicts)
            if blocked:
                return self._outcome(
                    "E2_HELD",
                    self._unjudged_reason(blocked, verdicts),
                    scenarios=scenarios,
                    judgment=judgment,
                    constraints=constraints,
                    verdicts=verdicts,
                    findings=verification.findings,
                    concerns=verification.concerns,
                    skipped_checks=verification.skipped,
                    purchase_attempts=attempts,
                )

            # 🔴 **다음 회차를 위한 되먹임.** 여기가 없어서 2회차가 1회차와 같은 입력으로
            #   돌았다 (매입 Q4 지적 2026-09-02). `_exhausted_reason` 이 그 사실을
            #   문장으로 내보내고 있었는데, **적어 둔 것과 고친 것은 다르다.**
            #
            # ★ **이 자리에서 만든다.** 1회차 산출물이 다 나온 뒤이고 다음 호출 전이라,
            #   `verdicts`·`findings`·조정안이 전부 이 회차의 것이다. 루프 밖에서
            #   만들면 어느 회차의 것인지가 흐려진다.
            feedback = self._feedback(attempts + 1, verdicts, verification)

            if attempts >= self.max_purchase_attempts:
                return self._outcome(
                    "E3_REJECTED",
                    self._exhausted_reason(attempts, verdicts, verification.findings),
                    scenarios=scenarios,
                    judgment=judgment,
                    constraints=constraints,
                    verdicts=verdicts,
                    findings=verification.findings,
                    concerns=verification.concerns,
                    skipped_checks=verification.skipped,
                    purchase_attempts=attempts,
                )

        return self._outcome(
            "E1_APPROVED",
            "사용자 선택 대기",
            scenarios=scenarios,
            judgment=judgment,
            constraints=constraints,
            verdicts=verdicts,
            findings=verification.findings,
            concerns=verification.concerns,
            skipped_checks=verification.skipped,
            purchase_attempts=attempts,
        )

    # ── 단계 ────────────────────────────────────────────────────

    def _collect_constraints(self) -> dict[AgentName, Mapping[str, Any]]:
        """① 재무·물류에게 실행 가능 경계를 받는다.

        ★ **근거도 같이 남긴다.** 전에는 `payload` 만 들고 있었는데, Critic 은 cap 축
          마다 근거를 요구한다(§1.2-5). 근거를 안 넘기면 *"없는 것"* 이 아니라
          **"안 넘긴 것"** 인데 계약 위반으로 잡힌다.
        """
        out: dict[AgentName, Mapping[str, Any]] = {}
        self.constraint_evidences = {}
        self.sourced_evidences = []
        self.suggested_adjustments = []
        self.delivery_notes = []
        for agent in self.advisors:
            reply = self.runner.call(agent, "PRE_PURCHASE", self._boundary_input())
            if not reply.contributes_to_band and self.runner.retryable(agent, "PRE_PURCHASE"):
                # 🔴 **한 번만 다시 부른다.** `ERROR` 는 어댑터가 터진 것이라 다시 부르면
                #   달라질 수 있다 — `RUNTIME_NOT_READY` 는 입력이 없어서 못 낸 답이라
                #   다시 불러도 같고, `retryable` 이 그 둘을 갈라 준다 (M-1 §5.1).
                #
                # ★ **루프가 아니다.** 결정론 고장(어댑터 스키마 오류 같은 것)은 몇 번을
                #   불러도 같으므로, 두 번째까지만 쓰고 예산을 더 태우지 않는다.
                #
                # ★ **실패를 감추지 않는다 — 오히려 드러낸다.** 실행 계획에 같은 단계가
                #   두 줄로 남아 *"한 번 실패"* 가 아니라 **"다시 불렀는데도 안 됐다"** 가
                #   된다. 사람이 보는 문장이 달라진다.
                reply = self.runner.call(agent, "PRE_PURCHASE", self._boundary_input())
            if reply.contributes_to_band:
                out[agent] = dict(reply.payload)
                self.constraint_evidences[agent] = reply.evidences
                # ★ 검증에 넘기는 것과 **같은 객체**를 화면 쪽에도 담는다.
                #   두 곳에서 각자 모으면 갈린다.
                self.sourced_evidences.extend(
                    SourcedEvidence(agent, "PRE_PURCHASE", ev) for ev in reply.evidences
                )
                # 경계 단계에서 조정안이 오는 일은 지금 없지만, **온다면 버리지 않는다.**
                # 부서가 무엇을 보내도 되는지는 봉투가 정하지 마스터가 정하지 않는다.
                self.suggested_adjustments.extend(reply.suggested_adjustments)
        return out

    def _boundary_input(self) -> dict[str, Any] | None:
        """① 경계 호출에 실어 보내는 것.

        🔴 **어제까지 승인된 약정을 싣는다** (#185). 물류는 그것을 미래 입고로 보고
          창고 여유를 다시 계산할 수 있고, 재무는 현금 유출로 볼 수 있다 — **어느
          쪽으로 볼지는 부서가 정한다.**

        ★ **없으면 칸을 안 만든다.** 빈 배열을 보내면 받는 쪽이 *"어제 승인이 없었다"*
          와 *"마스터가 안 보낸다"* 를 구별할 수 없다 (§1.2-10).

        ⚠️ **받는 쪽은 아직 없다.** 되먹임 `adjustments` 때와 같은 순서로, 보내는
          쪽을 먼저 만든다 — 버려지는 상태는 *"안 왔다"* 로 보이지 조용히 틀리지
          않는다 (매입 2026-09-03).
        """
        if not self.approved_commitments:
            return None
        return {"approved_commitments": [dict(c) for c in self.approved_commitments]}

    def _feedback(
        self,
        next_attempt: int,
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        verification: VerificationResult,
    ) -> dict[str, Any]:
        """다음 회차에 실을 되먹임 (계약 v0.2 §3.3).

        ★ **`reason` 은 인용도 요약도 아니다.** 무엇이 방아쇠였나를 세어 적는다.
          `findings` 중 하나를 골라 옮기면 **고르는 것이 곧 판단**이 된다 (§3.2.2).
          원문은 `findings` 와 `verdict_reasons` 에 통째로 있다.

        ★ **`verdicts` 는 봉투 어휘 그대로.** 마스터가 표기를 바꾸지 않는다.
          `adjustments` 가 0건일 때 그 0의 뜻을 가르는 유일한 칸이다 —
          `reject`(구제 불가) · `conditional` · `ok`(조정 불필요) · `skipped`(검토 안 함)
          가 전부 빈 배열로 보이기 때문이다 (재무·물류 지적 2026-09-02).

        ★ **시각·난수·외부조회를 넣지 않는다** (§3.4). 되먹임은 1회차 산출물에서만
          나와야 같은 입력에 같은 2회차가 나온다.
        """
        return {
            "attempt": next_attempt,
            "reason": _feedback_reason(verdicts, verification.findings),
            # 아래 셋은 부서·검증이 낸 원문 그대로다. 마스터가 안 고친다.
            "findings": list(verification.findings),
            "verdict_reasons": {
                agent: str(v.get("reasoning") or "") for agent, v in verdicts.items()
            },
            "verdicts": {
                agent: str(v.get("business_status") or "") for agent, v in verdicts.items()
            },
        }

    def _purchase_input(
        self,
        constraints: Mapping[AgentName, Mapping[str, Any]],
        feedback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """② 받은 것을 **묶기만** 한다.

        ★ 해석하거나 재계산하지 않는다 (§3.2.2). 값의 타당성은 검증 Tool 이 본다.
          마스터가 여기서 손대면 **부서 판단을 조정자가 덮어쓰는** 것이 된다.

        ★ 예외 셋(ML 예측·확정주문·정책값)은 마스터가 싣되 **`as_of` 대조는 한다.**
          직접 조회 시절 매입이 테스트로 강제하던 look-ahead 방어가 조립 시점으로 옮겨왔다.
          누수는 에러를 내지 않고 손익만 좋아지므로 여기서 막지 않으면 아무도 모른다.
        """
        payload: dict[str, Any] = {"constraints": dict(constraints)}
        if self.item is not None:
            payload["item"] = self.item
        if self.forecast is not None and self._forecast_is_clean():
            unwrapped = self._forecast_for_item()
            if unwrapped is not None:
                payload["forecast"] = unwrapped
        if self.confirmed_orders is not None:
            payload["confirmed_orders"] = dict(self.confirmed_orders)
        if self.policy_values is not None:
            payload["policy_values"] = dict(self.policy_values)
        if self.input_sources:
            # ★ **응답 `input_sources` 와 같은 모양이다** (매입 A-1 답 · ㄴ).
            #   화면과 payload 가 같은 이름을 써야, 부서가 나중에 화면에서 본 것과
            #   대조할 때 이름이 안 갈린다.
            #
            # ⚠️ **forecast 블록 안에 넣지 않는다.** `_FORECAST_ENVELOPE_KEYS` 는
            #   *"ML 봉투에서 내려보내는 필드"* 라, ML 이 안 보낸 키를 얹으면 받는
            #   쪽이 **"ML 이 준 것"** 으로 읽는다 (매입이 ㄱ 을 반대한 이유).
            payload["input_sources"] = dict(self.input_sources)
        if self.prior_feedback is not None:
            # ★ **사용자의 말 그대로 나른다.** 조건을 숫자로 바꿔 제약에 꽂으면 마스터가
            #   부서 판단을 덮어쓰는 것이 된다 — 해석은 매입이 한다 (§3.2.2).
            payload["prior_feedback"] = dict(self.prior_feedback)
        if feedback is not None:
            # 🔴 **사용자 조건과 섞지 않는다** (계약 v0.2 §2 · 매입 제안).
            #
            #   수명   사용자 조건은 실행 단위, 되먹임은 회차 단위
            #   모양   자연어 하나 vs 구조화 배열
            #   권위   사람 → 제안자 vs 조언자 → 제안자
            #
            #   한 슬롯에 `source` 로 갈랐던 v0.1 은 **payload 의 타입이 source 값에
            #   딸려 가서** 계약이 아니라 관례가 됐다.
            #
            # ★ **조정안은 부서가 낸 표준형 그대로.** 고르지도 정렬하지도 병합하지도
            #   않는다 — 같은 축이 둘 이상이어도 그대로 나른다 (매입·재무 합의).
            #   dataclass 를 dict 로 펴기만 한다 (모양을 바꾸는 것이 아니다).
            payload["adjustments"] = [_wire(a) for a in self.suggested_adjustments]
            payload["feedback_context"] = dict(feedback)
        return payload

    def _forecast_for_item(self) -> dict[str, Any] | None:
        """4품목 봉투에서 **이 품목 블록만** 꺼내 매입이 읽는 평면 모양으로 편다.

        ML 은 하루 한 번 4품목을 한 봉투로 보내고(ML 규격 §8-4 · 매입 동의), 매입은
        **품목 하나씩** 돈다. 그 사이를 잇는 것이 조립 책임이라 마스터 자리다 (§3.2.2).

        ★ **값을 만들지 않는다.** 봉투 공통 필드를 블록에 얹고 이름만 바꾼다.
          품목별 예측치를 여기서 고르거나 합치면 마스터가 ML 판단을 덮어쓰게 된다.

        ★ 블록이 봉투를 이긴다. 같은 이름이 양쪽에 있으면 **품목별 값이 더 구체적**이다.

        되돌리는 값이 `None` 이면 **싣지 않는다** — 매입이 `missing_data: ["forecast"]`
        로 `RUNTIME_NOT_READY` 를 내고 그 사실이 이력에 남는다. 빈 dict 를 실으면
        *"받았는데 비어 있다"* 가 되어 못 받은 것과 구분되지 않는다 (§1.2-10).
        """
        forecast = self.forecast
        if forecast is None:
            return None

        items = forecast.get("items")
        if not isinstance(items, Mapping):
            # 평면 봉투 — 품목 축이 없는 현행 모양이다. 그대로 넘긴다.
            return dict(forecast)

        if self.item is None:
            return None  # 어느 품목인지 모르는 채로 4품목 봉투를 넘길 수는 없다
        block = items.get(self.item)
        if not isinstance(block, Mapping):
            return None  # 이 품목의 예측이 안 왔다

        out = {key: forecast[key] for key in _FORECAST_ENVELOPE_KEYS if key in forecast}
        out.update(block)
        out["item"] = self.item
        return out

    def _forecast_is_clean(self) -> bool:
        """예측 생성 시각이 `as_of` 이후면 싣지 않는다.

        오염된 입력으로 시나리오를 만들면 **백테스트 손익만 좋아진다.**
        싣지 않으면 매입이 `RUNTIME_NOT_READY` 를 내고, 그 사실이 이력에 남는다.

        ★ **타임존이 없으면 싣지 않는다** (2026-08-27 매입 요청 반영).
          앞 10자만 비교하므로 오프셋이 없으면 `2026-09-04T23:00` 이 KST 로 09-05 인지
          UTC 로 09-04 인지 갈리지 않는다 — **이 검사 자체가 성립하지 않는다.**
          매입도 수신 시 거부하지만, 여기서 막으면 매입 호출 한 번을 아낀다.
        """
        generated = (self.forecast or {}).get("generated_at")
        if not isinstance(generated, str):
            return True  # 시점 필드가 없으면 판단하지 않는다 — 매입이 수신 시 재검증한다
        if not _HAS_TIMEZONE.search(generated):
            return False
        return generated[:10] <= self.runner.context.as_of.isoformat()

    def _validate(
        self, proposal: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]]
    ) -> dict[AgentName, Mapping[str, Any]]:
        """④ 각 조언자가 시나리오를 자기 관점에서 본다.

        ★ **시나리오 배열이 아니라 제안 전체를 넘긴다.**

          전에는 `{"scenarios": [...]}` 만 보냈다. 그런데 물류는 도착일을 계산하려면
          `meta.as_of` · `meta.item` 이 필요하고, 그 둘은 시나리오 안이 아니라 **제안
          최상위**에 있다. 검증 Tool 이 `allowed_axes` 를 못 보던 것과 **같은 종류의
          누락**이다 — 배열만 넘기면 그 판정을 못 본다.

          `scenarios` 는 제안 안에 그대로 있으므로 기존 소비자(재무)는 안 바뀐다.
        """
        payload = {**proposal, "scenarios": list(scenarios)}
        out: dict[AgentName, Mapping[str, Any]] = {}
        for agent in self.advisors:
            reply = self.runner.call(agent, "SCENARIO_VALIDATION", payload)
            out[agent] = {
                "business_status": reply.business_status,
                "runtime_status": reply.runtime_status,
                "payload": dict(reply.payload),
                "needs_followup": reply.needs_followup,
                # 🔴 **판정을 못 냈을 때 유일하게 이유를 아는 칸이다.** 이것이 없으면
                #   화면이 *"물류가 못 답했다"* 까지만 말하고 왜인지는 아무 데도 안 남는다
                #   — 물류의 기준일 불일치 fail-closed 가 그런 모양이다 (2026-08-31 회신).
                "reasoning": reply.reasoning,
            }
            # 🔴 **판정 근거도 버리지 않는다** (2026-09-02). 전에는 여기서 payload 만
            #   꺼내고 evidences 를 통째로 흘렸다 - 부서가 보내 준 것을 마스터가
            #   버리는 모양이고, `replans` 에서 고친 것과 같은 종류다.
            #
            #   경계 근거와 답하는 질문이 다르다 - 저쪽은 "상한이 왜 그 값인가",
            #   여기는 "이 안이 왜 ok 인가". 그래서 모드를 붙여 구별한다.
            self.sourced_evidences.extend(
                SourcedEvidence(agent, "SCENARIO_VALIDATION", ev) for ev in reply.evidences
            )
            # 🔴 **개수가 아니라 객체를 담는다** (2026-09-02). 되먹임 계약 §3.2 의
            #   `constraint` 가 바로 이 배열이라, 개수만 남기면 되먹임을 붙이는 순간
            #   나를 값이 없다. `replans` · `evidences` 와 같은 종류의 누락이었다.
            self.suggested_adjustments.extend(reply.suggested_adjustments)
        return out

    def _verify(
        self,
        proposal: Mapping[str, Any],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
    ) -> VerificationResult:
        """⑤ 마스터가 가진 검증 Tool. 주입 전에는 건너뛴 사실이 결과에 남는다.

        ★ 검증 Tool 은 **실행 계획도 본다** — ④ 실행 계획 온전성(M-16)이 그것만 읽는다.
          시나리오·경계·판정만으로는 "필요한 에이전트를 다 불렀나"를 알 수 없다.
        """
        if self.verifier is None:
            return VerificationResult()
        context = VerificationContext(
            as_of=self.runner.context.as_of,
            item=self.item,
            evidences=dict(self.constraint_evidences),
            # 부서가 남긴 관측을 실행 계획에서 그대로 꺼내 나른다. 마스터는 내용을
            # 해석하지 않는다 — 해석은 `critic_bridge` 가 Critic 어휘로 옮길 때뿐이다.
            observations=self._boundary_observations(),
        )
        return self.verifier(proposal, constraints, verdicts, self.runner.plan, context)

    def _boundary_observations(self) -> dict[AgentName, tuple[str, ...]]:
        """조언자 회신에 딸린 부서 관측. **기여한 회신만** 담는다.

        ★ 경계(`PRE_PURCHASE`)와 시나리오 판정(`SCENARIO_VALIDATION`)을 **둘 다** 나른다.
          Critic 의 권한 검사(`E-AUTHORITY`)는 부서가 무엇을 산출했는지를 보는데, 재무는
          두 mode 에서 서로 다른 것을 산출한다 — 경계만 나르면 시나리오 산출 필드는
          아무도 못 본다.

        ★ 마스터는 **읽지 않고 나른다.** 내용을 해석하는 곳은 `critic_bridge` 가 부서
          관측을 Critic 어휘로 옮길 때뿐이다.
        """
        out: dict[AgentName, tuple[str, ...]] = {}
        for agent in ADVISORS:
            items: list[str] = []
            for mode in ("PRE_PURCHASE", "SCENARIO_VALIDATION"):
                step = self.runner.plan.last(agent, mode)
                if step is not None and step.contributed and step.observations:
                    items.extend(step.observations)
            if items:
                out[agent] = tuple(items)
        return out

    # ── 판단 ────────────────────────────────────────────────────

    def _acceptable(
        self,
        scenarios: Sequence[Mapping[str, Any]],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        findings: Sequence[str],
    ) -> bool:
        """사용자에게 올릴 만한가.

        ★ **전원 통과를 요구하지 않는다.** 조언자 하나가 `conditional` 을 내도 사람이
          보고 정할 수 있다 — 마스터는 최적안을 고르는 자리가 아니다 (§3.4).

        ★ **허용목록으로 정한다** (`_PASSING_VERDICTS`). 통과를 *"reject 가 아닌 것"*
          으로 정하면 어휘가 늘 때마다 새 값이 통과로 샌다 (#173).

        🔴 **여기가 거짓인 이유는 둘로 갈린다.** 무엇을 할지가 다르다.

            reject · 검증 지적   판정은 났고 통과가 아니다   → 매입을 다시 부른다
            그 밖의 값           판정 자체가 없다            → `_dept_blocked` 가 가른다
        """
        if findings:
            return False
        return all(
            str(v.get("business_status") or "") in _PASSING_VERDICTS for v in verdicts.values()
        )

    def _dept_blocked(
        self, verdicts: Mapping[AgentName, Mapping[str, Any]]
    ) -> tuple[AgentName, ...]:
        """**부서 쪽 사정으로** 판정이 없는 조언자. 온 차례 그대로 돌려준다.

        걸리는 조건이 둘이고 **둘 다 만족해야** 한다.

            ① 판정을 안 냈다        `business_status` 가 `_JUDGED_VERDICTS` 밖
            ② 부서 쪽 사정이다      `runtime_status != "READY"`

        🔴 **②가 없으면 안 된다** (물류·재무 회신 2026-09-02). 판정 없음은
          `runtime_status` 가 갈라 준다.

            RUNTIME_NOT_READY   판정할 사실이 없다   매입이 새 안을 줘도 같다
            ERROR               실행이 실패했다      매입이 새 안을 줘도 같다
            READY               **호출자가 보낸 입력이 부족하다**
                                → 🔴 **새 제안이면 달라질 수 있다.** 여기서 잡으면
                                  고쳐질 수 있는 것을 안 고치고 끝낸다

          재무 `INPUT_INCOMPLETE` 가 바로 ③이다 — *"제안에 사실이 빠진 것은 재무
          고장이 아니다"* (`finance/application/orchestration.py:153`). 제안이 바뀌면
          채워질 수 있으므로 **기존 재호출 경로에 그대로 둔다.**

        ★ 여기 걸리면 매입을 다시 불러도 같은 답이 온다. 재호출은 호출 예산과 LLM 만
          태운다 — `#159` 에서 그렇게 6회를 태웠다.

        ★ **어휘 밖 값도 ①에 해당한다.** `#162` 가 `E-VOCAB-BUSINESS-STATUS` 로
          드러내기는 했지만 그 지적은 실행 계획으로 가고 `_acceptable` 은
          `verification.findings` 만 본다 — **지적이 그 게이트에 안 닿았다.**
          다만 ② 를 같이 요구하므로, `READY` 인 모르는 값은 여기서 끝내지 않고
          재호출로 보낸다. **통과로 읽히지 않는 것**이 이 이슈의 주장이다.

        ⚠️ **조언자만 본다.** 같은 `skipped` 라도 제안자 자리에서는 뜻이 다르다 —
          매입은 *"낼 안이 없다"* 를 `READY + skipped` 로 보내고(`adapter.py:892`),
          그것은 `if not scenarios` 가 E2/E5 로 받는 정상 경로다. 여기서 같이 잡으면
          **매입이 제대로 답한 것을 "판정 없음" 으로 바꿔 버린다.**
        """
        return tuple(
            agent
            for agent in self.advisors
            if agent in verdicts
            and str(verdicts[agent].get("business_status") or "") not in _JUDGED_VERDICTS
            and str(verdicts[agent].get("runtime_status") or "") != "READY"
        )

    def _unjudged_reason(
        self,
        unjudged: tuple[AgentName, ...],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
    ) -> str:
        """판정을 못 받아 못 올린다는 **사람이 읽는 사유.**

        🔴 **`runtime_status` 를 같이 적는다** (물류 지적 2026-09-02). `skipped`
          하나로는 갈리지 않는다 — 갈래는 `runtime_status` 가 가른다.

            RUNTIME_NOT_READY   판정할 사실이 없다        다시 불러도 같다
            ERROR               실행이 실패했다           **재시도 가치가 있다**
            READY               호출자가 보낸 입력이 부족  부른 쪽이 고친다

          이 구분 없이 *"판정 없음"* 으로만 적으면 물류 스냅샷 조회 실패처럼
          **재시도 가치가 있는 실패를 "입력 없음" 으로 처리하게 된다**
          (`logistics/adapter.py:1385` 가 그 사실을 사유에 직접 적어 보낸다).

        ★ **왜인지는 안 쓴다.** 부서가 보낸 `reasoning` 이 이미 응답에 실려 있고,
          여기서 한 번 더 요약하면 같은 사실의 주인이 둘이 된다 (§3.2.2).
        """
        parts = [
            f"{agent_label(agent)}"
            f"({verdicts[agent].get('runtime_status') or '?'}"
            f"/{verdicts[agent].get('business_status') or '?'})"
            for agent in unjudged
        ]
        return f"판정을 받지 못해 올리지 않는다 — {' · '.join(parts)}"

    def _adjustment_delivery(
        self, attempt: int, sent: int, judgment: Mapping[str, Any]
    ) -> str:
        """보낸 조정안과 **매입이 받았다고 적은 수**를 대조한다. 맞으면 빈 문자열이다.

        🔴 **매입이 채우는데 마스터가 안 읽고 있었다.**

        ```text
        app/purchase_agent/nodes/self_check.py:722
            "received_adjustments": len(state.get("adjustments") or [])
        ```

        내가 여러 파트에 지적해 온 *"값을 실어 주고 안 쓴다"* 의 **정확한 반대편**이다.
        조정안을 보내 놓고 **닿았는지를 아무도 안 봤다.**

        ★ **맞으면 아무것도 안 적는다.** 정상 경로가 시끄러우면 어긋남이 안 보인다.

        ★ **`findings` 가 아니라 `concerns` 다.** 매입을 다시 불러도 배선이 끊긴
          사실은 그대로다 — 사람이 볼 것이지 재시도할 것이 아니다 (`04` §3.2).

        ⚠️ **이것은 "반영됐나" 가 아니라 "닿았나" 다.** 반영은 매입이
          `applied_adjustments` 를 회신해야 알 수 있고 그 칸은 아직 없다
          (매입 ①timing 에서 만든다). 두 사실을 한 문장으로 뭉개지 않는다.
        """
        if not sent:
            return ""  # 안 보낸 회차는 대조할 것이 없다 (1회차가 늘 그렇다)

        meta = judgment.get("meta")
        if not isinstance(meta, Mapping) or "received_adjustments" not in meta:
            return (
                f"{attempt}회차: 조정안 {sent}건을 보냈는데 매입 회신에 "
                f"received_adjustments 가 없다 — 닿았는지 알 수 없다"
            )

        received = meta["received_adjustments"]
        if received != sent:
            return (
                f"{attempt}회차: 조정안 {sent}건을 보냈는데 매입은 {received}건 "
                f"받았다고 적었다 — 전선에서 빠진 것이 있다"
            )
        return ""

    def _exhausted_reason(
        self,
        attempts: int,
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        findings: Sequence[str],
    ) -> str:
        """재호출을 다 쓰고도 통과안이 없을 때 **사람이 읽는 사유.**

        🔴 **다시 부른 것과 이유를 주고 다시 부른 것은 다른 문장이다.**
          전에는 *"매입 재호출 2 회에도 통과안 없음"* 만 적었다. 사람은 그것을
          *"고쳐 보라고 두 번 시켰는데 못 고쳤구나"* 로 읽는다. 실제로는 **같은 입력으로
          두 번 돌렸다** — `_purchase_input` 이 루프 밖 값만 읽어서 무엇을 고쳐야 하는지가
          매입에 안 간다.

        ★ **되먹임을 배선하지 않은 것은 선택이다. 안 한 것을 한 것처럼 읽히게 두는 것은
          선택이 아니다.**

        🟢 **전달 대조는 여기서 안 한다** (2026-09-03). 예고했던 *"전달 건수와 반영
          건수를 적는 쪽으로 바뀐다"* 를 다시 갈랐다 — 셋이 다른 사실이다.

        ```text
        그 지적이 매입에 갔는가        이 문장이 소유한다
        조정안이 닿았는가             _adjustment_delivery 가 concerns 로 낸다
        조정안이 반영됐는가           applied_adjustments 가 와야 안다 (아직 없다)
        ```

          한 문장이 셋을 말하면 어느 것이 틀렸는지 못 가린다.

        ★ **왜 못 냈는지는 안 쓴다.** 그건 `findings` 와 `verdicts` 가 이미 응답에
          싣고 있고, 여기서 한 번 더 요약하면 같은 사실의 주인이 둘이 된다.
          이 문장이 소유하는 것은 **"그 지적이 매입에 갔는가"** 하나다.
        """
        head = f"매입 재호출 {attempts} 회에도 통과안 없음"
        sent = self._sent_to_purchase(verdicts, findings)
        if not sent:
            return head
        # ⚠️ **보낸 것과 반영된 것은 다르다.** 매입이 `applied_adjustments` 를 회신하기
        #   전까지 마스터가 아는 것은 "보냈다" 까지다. 문장이 그 이상을 말하면 안 된다.
        return f"{head} — 매입에 전달한 것: {', '.join(sent)}(반영 여부는 매입 회신에 달림)"

    def _sent_to_purchase(
        self,
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        findings: Sequence[str],
    ) -> tuple[str, ...]:
        """재호출을 유발한 근거 중 **재호출 payload 에 실어 보낸 것.**

        🔴 **전에는 이 함수가 `_unsent_to_purchase` 였다** — 같은 것을 세면서 *"안
          실린다"* 고 말했다. 되먹임을 배선한 지금은 실린다 (계약 v0.2 · #169).
          세는 대상은 그대로고 **문장의 뜻이 뒤집혔다.**

        `_acceptable` 이 거짓이 되는 길이 둘뿐이므로(검증 지적 · 부서 기각) 둘만 본다.
        둘 다 없으면 `_acceptable` 이 참이라 이 자리에 오지 않지만, 빈 튜플을 돌려
        **머리말만 남기는 쪽**으로 둔다 — 없는 것을 있다고 적지 않는다.

        ★ **조정안 건수는 안 센다.** 여기가 소유하는 사실은 *"재호출을 유발한 것이
          매입에 갔는가"* 이고, 조정안은 그 유발자가 아니라 **함께 실어 보낸 값**이다.
        """
        out: list[str] = []
        if findings:
            out.append(f"검증 지적 {len(findings)}건")
        rejected = tuple(
            agent_label(agent)
            for agent, verdict in verdicts.items()
            if verdict.get("business_status") == "reject"
        )
        if rejected:
            out.append(f"{'·'.join(rejected)} 기각 사유")
        return tuple(out)

    def _failures_of(self, agents: tuple[AgentName, ...], mode: Mode) -> tuple[AgentFailure, ...]:
        """기여하지 못한 부서의 **사유를 실행 계획에서 꺼낸다.**

        ★ **회신이 아니라 계획을 본다.** `_collect_constraints` 는 실패한 회신을
          `contributes_to_band` 로 걸러 버리므로 그 자리에는 이미 값이 없다. 계획에는
          남아 있고, `retryable` 도 같은 이유로 계획을 본다 —
          *"무슨 일이 일어났나"* 의 단일 출처를 하나로 둔다.
        """
        out: list[AgentFailure] = []
        for agent in agents:
            step = self.runner.plan.last(agent, mode)
            if step is None:
                # 회신 자체가 없다. `RuntimeStatus` 로 적을 수 없는 상태다.
                out.append(AgentFailure(agent, "NOT_CALLED"))
                continue
            out.append(
                AgentFailure(
                    agent,
                    step.runtime_status,
                    reasoning=step.reasoning,
                    missing_data=tuple(step.missing_data),
                )
            )
        return tuple(out)

    def _outcome(self, end_code: EndCode, reason: str, **kw: Any) -> ProcurementOutcome:
        plan: ExecutionPlan = self.runner.plan
        # ★ **모든 종료 코드에서 싣는다.** 배관이 끊긴 날이야말로 그 사실이 필요하다 —
        #   E1 로 끝나도 조정안이 안 닿았으면 그 통과는 되먹임과 무관하게 난 것이다.
        if self.delivery_notes:
            kw["concerns"] = (*kw.get("concerns", ()), *self.delivery_notes)
        return ProcurementOutcome(
            end_code=end_code,
            reason=reason,
            plan=plan,
            verification_skipped=self.verifier is None,
            # ★ **모든 종료 코드에서 싣는다.** 안이 안 나온 날(E2·E3)이야말로
            #   "무슨 근거로 그렇게 됐나" 가 필요하다 - 성공한 날만 근거를 보여주면
            #   정작 설명이 필요한 날에 화면이 침묵한다.
            evidences=tuple(self.sourced_evidences),
            # ★ 근거와 같은 이유로 **모든 종료 코드에서 싣는다.** 안이 안 나온 날
            #   ("무엇을 고쳐야 하나")이야말로 조정안이 필요한 날이다.
            adjustments=tuple(self.suggested_adjustments),
            **kw,
        )


def _wire(adjustment: SuggestedAdjustment) -> dict[str, Any]:
    """봉투 표준형을 **전선에 실을 수 있는 모양**으로 편다 (#175).

    🔴 `asdict` 는 `date` 를 그대로 둔다. 그 dict 를 `json.dumps` 에 넣으면
      **TypeError 로 죽는다** — *"Object of type date is not JSON serializable"*.

      지금 안 터지는 이유가 더 나쁘다. 물류 어댑터가 `split_date` 를 표준형에
      **안 옮겨서**(`logistics/adapter.py:1122`) 늘 `None` 이라 통과한다.
      **물류가 칸을 채우는 순간 터진다** — 지금 그 작업 중이다 (매입 지적 2026-09-03).

    ★ **dataclass 의 타입은 안 바꾼다.** `split_date: date | None` 은 객체 안에서
      비교·연산이 되는 것이 맞다. **전선에 실을 때만** ISO 문자열로 편다.
      화면 쪽(pydantic)은 이미 알아서 한다 — 여기만 손으로 해야 하는 자리다.

    ★ **정규화는 보내는 쪽이 한다.** 매입은 *"받아서 바꾸는 쪽이 자연스럽다"* 고
      했지만, `asdict` 로 편 것이 마스터라 마스터가 책임진다. 받는 쪽이 여럿이 되면
      **각자 변환해 같은 사실의 주인이 여럿**이 된다.

    ★ **튜플도 목록으로 편다.** `asdict` 는 튜플을 그대로 두는데 JSON 을 한 번
      왕복하면 목록이 된다 — **같은 칸이 경로에 따라 두 모양**이 되고, 받는 쪽이
      `== [...]` 로 비교하면 in-process 에서만 조용히 어긋난다.

      기준은 하나다. **여기서 나간 dict 는 JSON 왕복을 거쳐도 같아야 한다**
      (`test_전선에_실은_것은_왕복해도_같다`). 칸마다 세지 않고 이 성질로 잠근다.
    """
    out: dict[str, Any] = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in asdict(adjustment).items()
    }
    if adjustment.split_date is not None:
        out["split_date"] = adjustment.split_date.isoformat()
    return out


def _feedback_reason(
    verdicts: Mapping[AgentName, Mapping[str, Any]],
    findings: Sequence[str],
) -> str:
    """되먹임을 켠 방아쇠를 **세어 적는다.**

    ★ **고르지 않는다.** `findings` 중 하나를 옮기면 *"이것이 대표다"* 라는 판단이
      생긴다. 원문은 `findings` · `verdict_reasons` 에 통째로 실려 있으므로 여기는
      **무엇이 방아쇠였나**만 말한다.

    ★ `_unsent_to_purchase` 와 세는 것이 같다 — `_acceptable` 이 거짓이 되는 길이
      둘뿐이라(검증 지적 · 부서 기각) 둘만 본다. 저쪽은 *"안 보낸 것"* 을 세고
      이쪽은 *"보내는 이유"* 를 센다. 배선이 끝나면 저쪽이 없어질 자리다.
    """
    parts: list[str] = []
    if findings:
        parts.append(f"검증 지적 {len(findings)}건")
    rejected = tuple(
        agent_label(agent)
        for agent, verdict in verdicts.items()
        if verdict.get("business_status") == "reject"
    )
    if rejected:
        parts.append(f"{'·'.join(rejected)} 기각")
    return f"재호출 사유: {' · '.join(parts)}" if parts else "재호출 사유: 미상"


def _blocked_reason(failures: tuple[AgentFailure, ...]) -> str:
    """E4 사유 한 줄. **누구인지 다음에 왜인지가 온다.**

    ★ 앞머리(`경계를 내지 못한 에이전트: `)와 부서 이름은 그대로 둔다 - 읽던 사람과
      이것을 찾던 코드가 있다. 뒤에 사유를 붙일 뿐이다.
    """
    if not failures:
        return "경계를 내지 못한 에이전트: (목록 없음)"
    return "경계를 내지 못한 에이전트: " + ", ".join(f"{f.agent}({f.detail})" for f in failures)


def _scenarios_of(reply: AgentReply) -> tuple[Mapping[str, Any], ...]:
    raw = reply.payload.get("scenarios", ())
    if isinstance(raw, Mapping) or not isinstance(raw, Sequence):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _judgment_of(reply: AgentReply) -> Mapping[str, Any]:
    """`scenarios` 를 뺀 제안 최상위 — `situation`·`allowed_axes`·`confidence` 판정부.

    ★ 키를 **고르지 않는다.** 화이트리스트로 뽑으면 매입이 판정 필드를 추가할 때마다
      마스터를 고쳐야 하고, 빠뜨린 키는 §3.7.6 의 "커버리지를 감춘" 상태가 된다.
      시나리오 배열만 빼고 전부 옮긴다 — 검증 Tool 에 배열 대신 제안 전체를 넘기게
      된 것과 같은 교훈이다 (2026-08-27 매입 스키마 · 프론트 판정 헤더가 소비).
    """
    return {k: v for k, v in reply.payload.items() if k != "scenarios"}
