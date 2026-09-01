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
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.master.budget import BudgetExhausted
from app.master.envelope import AgentName, AgentReply
from app.master.plan import ExecutionPlan
from app.master.runner import MasterRunner
from app.master.verifier import VerificationContext, VerificationResult
from app.orchestrator.contracts_core import EndCode, Evidence, ItemCode

_HAS_TIMEZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")
"""ISO 8601 오프셋이 붙었는가. `2026-09-04T06:00:00+09:00` · `...Z` 는 통과."""

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
class ProcurementOutcome:
    """Flow 한 번의 결과. **무엇을 못 했는지도 담는다.**"""

    end_code: EndCode
    reason: str
    plan: ExecutionPlan

    scenarios: tuple[Mapping[str, Any], ...] = ()
    judgment: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[AgentName, Mapping[str, Any]] = field(default_factory=dict)
    verdicts: Mapping[AgentName, Mapping[str, Any]] = field(default_factory=dict)

    blocked_by: tuple[AgentName, ...] = ()
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
        max_purchase_attempts: int = 2,
        item: ItemCode | None = None,
        forecast: Mapping[str, Any] | None = None,
        confirmed_orders: Mapping[str, Any] | None = None,
        policy_values: Mapping[str, Any] | None = None,
        prior_feedback: Mapping[str, Any] | None = None,
    ) -> None:
        self.runner = runner
        self.verifier = verifier
        self.advisors = advisors
        self.max_purchase_attempts = max_purchase_attempts
        self.item = item
        self.constraint_evidences: dict[AgentName, tuple[Evidence, ...]] = {}
        self.forecast = forecast
        self.confirmed_orders = confirmed_orders
        self.policy_values = policy_values
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
            return self._outcome(
                "E4_NOT_STARTED",
                f"경계를 내지 못한 에이전트: {', '.join(blocked)}",
                constraints=constraints,
                blocked_by=blocked,
            )

        attempts = 0
        scenarios: tuple[Mapping[str, Any], ...] = ()
        proposal: Mapping[str, Any] = {}
        judgment: Mapping[str, Any] = {}
        verdicts: dict[AgentName, Mapping[str, Any]] = {}
        verification = VerificationResult()

        while attempts < self.max_purchase_attempts:
            attempts += 1
            purchase = self.runner.call(
                "purchase", "GENERATE_SCENARIOS", self._purchase_input(constraints)
            )
            proposal = dict(purchase.payload)
            scenarios = _scenarios_of(purchase)
            judgment = _judgment_of(purchase)

            if not purchase.contributes_to_band:
                return self._outcome(
                    "E4_NOT_STARTED",
                    f"매입 에이전트 미가동: {purchase.reasoning or purchase.runtime_status}",
                    judgment=judgment,
                    constraints=constraints,
                    blocked_by=("purchase",),
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

            if attempts >= self.max_purchase_attempts:
                return self._outcome(
                    "E3_REJECTED",
                    f"매입 재호출 {attempts} 회에도 통과안 없음",
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
        for agent in self.advisors:
            reply = self.runner.call(agent, "PRE_PURCHASE")
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
                reply = self.runner.call(agent, "PRE_PURCHASE")
            if reply.contributes_to_band:
                out[agent] = dict(reply.payload)
                self.constraint_evidences[agent] = reply.evidences
        return out

    def _purchase_input(self, constraints: Mapping[AgentName, Mapping[str, Any]]) -> dict[str, Any]:
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
        if self.prior_feedback is not None:
            # ★ **사용자의 말 그대로 나른다.** 조건을 숫자로 바꿔 제약에 꽂으면 마스터가
            #   부서 판단을 덮어쓰는 것이 된다 — 해석은 매입이 한다 (§3.2.2).
            payload["prior_feedback"] = dict(self.prior_feedback)
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
                "suggested_adjustments": len(reply.suggested_adjustments),
                "needs_followup": reply.needs_followup,
                # 🔴 **판정을 못 냈을 때 유일하게 이유를 아는 칸이다.** 이것이 없으면
                #   화면이 *"물류가 못 답했다"* 까지만 말하고 왜인지는 아무 데도 안 남는다
                #   — 물류의 기준일 불일치 fail-closed 가 그런 모양이다 (2026-08-31 회신).
                "reasoning": reply.reasoning,
            }
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
          `reject` 가 있거나 검증 발견이 있으면 매입을 다시 부른다.
        """
        if findings:
            return False
        return all(v.get("business_status") != "reject" for v in verdicts.values())

    def _outcome(self, end_code: EndCode, reason: str, **kw: Any) -> ProcurementOutcome:
        plan: ExecutionPlan = self.runner.plan
        return ProcurementOutcome(
            end_code=end_code,
            reason=reason,
            plan=plan,
            verification_skipped=self.verifier is None,
            **kw,
        )


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
