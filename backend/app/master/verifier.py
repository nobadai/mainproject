"""
verifier.py — 마스터가 직접 가진 검증 Tool (정의서 §3.7)

    ① 부서가 낸 답의 자격      ← 봉투 검증이 이미 호출마다 돈다 (runner.call)
    ② 마스터 자신의 계산 재검산  ← 결합·클리핑이 붙은 뒤
    ③ 합쳤을 때의 모순
    ④ 실행 계획 온전성 (M-16)   ← 여기서 전부 구현한다

★ **판정하지 못한 것을 판정했다고 말하지 않는다** (§3.7.6).
  도메인 payload 필드명이 확정되지 않은 검사는 `findings` 를 비우는 것이 아니라
  `skipped` 에 사유와 함께 남긴다. 비워 두면 **"검사했고 통과했다"로 읽힌다.**

★ **커버리지를 감추지 않는다** (§3.7.6).
  Critic 56검사를 붙였고(2026-08-27), **몇 개가 돌았는지를 `skipped` 에 적는다.**
  `findings: []` 만 보면 *"56검사를 통과했다"* 로 읽힌다 — 실제로는 부서 메타 미제출
  같은 이유로 절반이 안 돌 수 있고, 그 사실이 같이 보여야 한다.

★ 왜 ①이 여기 없는가
  `E-BIND-*` · `E-EVIDENCE-*` · `E-REASONING-*` 는 `MasterRunner.call()` 이 호출마다
  돌려 `ExecutionStep.finding_codes` 에 쌓는다. 여기서는 **그것이 남아 있는지**만 본다
  (`M16-ENVELOPE`) — 두 번 계산하지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from pydantic import ValidationError

from app.critic.schemas import CriticProcurementRequest, CriticVerdictOut
from app.critic.service import run_critic_procurement
from app.master.critic_bridge import CriticSkipped, build_request, fold
from app.master.envelope import ENVELOPE_META_KEYS, AgentName
from app.master.plan import ExecutionPlan
from app.orchestrator.contracts_core import Evidence


class CriticPort(Protocol):
    """Critic 진입점. 갈아 끼울 수 있게 두는 이유는 테스트가 아니라 **격리**다 —
    검증 Tool 이 도메인 구현에 직접 묶이면 Critic 이 바뀔 때 마스터가 흔들린다."""

    def __call__(self, req: CriticProcurementRequest) -> CriticVerdictOut: ...


@dataclass(frozen=True)
class VerificationContext:
    """검증이 **부서 판정을 넘어** 보려면 필요한 것.

    ★ `evidences` 가 여기 있는 이유 — `constraints` 는 payload 만 담는다. Critic 은
      cap 축마다 근거를 요구하므로(§1.2-5) 근거 없이 넘기면 **없는 것이 아니라 안 넘긴
      것인데 계약 위반으로 잡힌다.**
    """

    as_of: date
    item: str | None = None
    evidences: Mapping[AgentName, tuple[Evidence, ...]] = field(default_factory=dict)


def _scenarios_of(proposal: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = proposal.get("scenarios", ())
    if isinstance(raw, Mapping) or not isinstance(raw, Sequence):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _rows(value: Any) -> tuple[Mapping[str, Any], ...] | None:
    """매핑들의 배열인가. **아니면 `None`** — 빈 배열과 구분한다.

    빈 배열로 접으면 *"항목이 없다"* 와 *"키가 없다"* 가 같아 보인다. 앞은 계약 위반이고
    뒤는 아직 안 실린 것이라 처리가 다르다.
    """
    if value is None or isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        return None
    return tuple(item for item in value if isinstance(item, Mapping))


def _int_of(value: Any) -> int | None:
    """정수로 읽는다. `bool` 은 배제한다 — `True` 가 `1` 로 새면 검사가 조용히 통과한다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _sum_field(rows: Any, field: str) -> int | None:
    """항목들의 한 필드를 더한다. **하나라도 못 읽으면 `None`** — 부분합은 대조에 못 쓴다."""
    items = _rows(rows)
    if items is None:
        return None
    total = 0
    for item in items:
        value = _int_of(item.get(field))
        if value is None:
            return None
        total += value
    return total


def _sum_product(rows: Any, left: str, right: str) -> int | None:
    items = _rows(rows)
    if items is None:
        return None
    total = 0
    for item in items:
        a, b = _int_of(item.get(left)), _int_of(item.get(right))
        if a is None or b is None:
            return None
        total += a * b
    return total


def _day_gap(start: Any, end: Any) -> int | None:
    """`YYYY-MM-DD` 두 개의 일수 차이. calendar day 다 — 영업일 보정 없음 (N5)."""
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except (ValueError, TypeError):
        return None


# 매입이 밝힌 판정 필드 (2026-08-27 회신). 없으면 그 검사는 skipped 다.
_ALLOWED_AXES = "allowed_axes"
_SPLIT_PLAN = "split_plan"
_TIMING = "timing"

# 분할 매입 지급 일정 — 매입 §3.2 제안 · 재무 회신으로 필드 확정 (2026-08-27)
_PAYMENT_SCHEDULE = "payment_schedule"
_SOURCING_PLAN = "sourcing_plan"


@dataclass(frozen=True)
class VerificationResult:
    """세 갈래로 나눠 돌려준다.

    | | 무엇 | 마스터 행동 |
    |---|---|---|
    | `findings` | **매입이 다시 만들면 달라질 수 있는 것** | 재호출 |
    | `concerns` | 사실이지만 재호출로 안 고쳐지는 것 | 보고만 |
    | `skipped` | 판정하지 못한 것 | 커버리지에 노출 |

    ★ **`concerns` 를 나눈 이유는 배선하고 나서 드러났다.**
      처음에는 전부 `findings` 였다. 그러자 **재무 회신의 봉투 위반 때문에 매입을 다시
      부르고 있었다** — 매입이 몇 번을 다시 만들어도 재무의 `E-EVIDENCE-MISSING` 은
      그대로다. 호출 예산만 태우고 `E3_REJECTED` 로 끝난다.

      **재호출은 "다시 부르면 달라질 수 있는 것"에만 쓴다.** 남의 계약 위반과 마스터
      자신의 배선 문제는 보고 대상이지 재시도 대상이 아니다.

    ★ `concerns` 는 숨기는 자리가 아니다. 응답에 그대로 나가고, 사람이 본다
      (§3.4 "마스터는 최적안을 고르지 않는다").
    """

    findings: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """**재호출을 유발하지 않는가.** `concerns` 는 여기에 안 든다."""
        return not self.findings


class MasterVerifier:
    """마스터의 검증 Tool.

    ★ 제안자와 심판을 분리하는 것이 전부다.
      재무·물류는 *"내 기준으로 되나"* 를 답하고, 이쪽은 *"그 답이 규칙대로 나왔나 ·
      마스터가 제대로 합쳤나 · 합친 결과가 앞뒤가 맞나"* 를 본다.
    """

    def __init__(
        self,
        required_advisors: tuple[AgentName, ...] = ("finance", "inventory"),
        critic: CriticPort | None = run_critic_procurement,
    ):
        self.required_advisors = required_advisors
        self.critic = critic
        """Critic 56검사. `None` 이면 **돌리지 않은 사실이 `skipped` 에 남는다.**"""

    def __call__(
        self,
        proposal: Mapping[str, Any],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        plan: ExecutionPlan,
        context: VerificationContext | None = None,
    ) -> VerificationResult:
        """★ 시나리오 배열이 아니라 **제안 전체**를 받는다 (2026-08-27 매입 스키마 확인).

        `allowed_axes` · `situation` · `confidence` 는 `scenarios[]` 안이 아니라
        **제안 최상위**에 있다(`PurchaseProposal`). 배열만 받으면 그 판정들을 볼 수 없다.

        ★ `context` 는 Critic 에 넘길 때만 쓴다 — `as_of` · 품목 · 조언자 Evidence.
          주지 않으면 Critic 을 돌리지 않고 그 사실을 `skipped` 에 남긴다.
        """
        scenarios = _scenarios_of(proposal)
        findings: list[str] = []
        concerns: list[str] = []
        skipped: list[str] = []

        self._check_plan_integrity(plan, scenarios, findings, concerns)
        self._check_timing_gate(proposal, scenarios, findings, skipped)
        identity_findings = len(findings)
        self._check_scenario_identities(scenarios, findings, skipped)
        identity_broken = len(findings) > identity_findings
        self._check_payment_schedule(scenarios, constraints, findings, skipped)
        self._check_supplied_but_unused(scenarios, constraints, concerns)
        self._run_critic(
            proposal, constraints, context, identity_broken, findings, concerns, skipped
        )
        self._declare_uncovered(skipped)

        return VerificationResult(tuple(findings), tuple(concerns), tuple(skipped))

    # ── Critic 56검사 (§3.7.1) ──────────────────────────────────

    def _run_critic(
        self,
        proposal: Mapping[str, Any],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        context: VerificationContext | None,
        identity_broken: bool,
        findings: list[str],
        concerns: list[str],
        skipped: list[str],
    ) -> None:
        """★ **항등식이 깨졌으면 돌리지 않는다.**

        Critic 의 금액 축은 `qty × unit_price` 로 다시 만들어지고, 그 단가는
        `total_amount_krw / total_qty_kg` 에서 온다 — **두 값이 서로 맞을 때만** 뜻이
        있는 표현이다. 어긋난 숫자 위에서 돌린 56검사는 **그럴듯한 통과**를 만든다.
        그건 검증이 아니라 검증처럼 보이는 것이다.

        ★ Critic 이 던지는 예외를 위로 올리지 않는다. 검증 Tool 이 죽으면 Flow 가
          통째로 `ERROR` 가 되는데, **검증 실패는 매입 판단의 실패가 아니다.**
        """
        if self.critic is None:
            skipped.append("Critic L0~L5 (56검사): 검증 Tool 에 주입되지 않음")
            return
        if context is None:
            skipped.append("Critic L0~L5 (56검사): 실행 맥락 미전달 — as_of · 품목 · 근거 없음")
            return
        if identity_broken:
            skipped.append(
                "Critic L0~L5 (56검사): 시나리오 항등식이 깨져 돌리지 않았다 — "
                "어긋난 숫자 위의 판정은 통과해도 뜻이 없다"
            )
            return

        try:
            request = build_request(
                as_of=context.as_of,
                item=context.item,
                proposal=proposal,
                constraints=constraints,
                evidences=context.evidences,
            )
            verdict = self.critic(request)
        except CriticSkipped as exc:
            skipped.append(f"Critic L0~L5 (56검사): {exc}")
            return
        except ValidationError as exc:
            # ★ 입력이 Critic 계약에 안 맞는 것은 **검증 Tool 의 고장이 아니다.**
            #   매입이 허용 목록 밖 어휘를 내면 여기서 걸린다(예: strategy_type).
            #   어느 필드인지 적어 `skipped` 로 남긴다 — 통과로 치지 않는다.
            fields = " · ".join(
                ".".join(str(part) for part in err["loc"]) for err in exc.errors()[:5]
            )
            skipped.append(f"Critic L0~L5 (56검사): 입력이 Critic 계약에 맞지 않는다 — {fields}")
            return
        except Exception as exc:  # noqa: BLE001 — 검증이 죽어도 Flow 는 살아야 한다
            concerns.append(f"CRITIC: 검증 Tool 이 돌지 못했다 — {type(exc).__name__}: {exc}")
            skipped.append("Critic L0~L5 (56검사): 실행 중 오류로 미판정")
            return

        critic_findings, critic_concerns, critic_skipped = fold(verdict)
        findings.extend(critic_findings)
        concerns.extend(critic_concerns)
        skipped.extend(critic_skipped)

    # ── ④ 실행 계획 온전성 (M-16 · §3.7.4) ──────────────────────
    #
    # 마스터가 호출 순서를 스스로 정하면서 새로 필요해진 검사다.
    # 고정 순서 파이프라인에서는 코드가 순서를 보장했으므로 필요 없었다.

    def _check_plan_integrity(
        self,
        plan: ExecutionPlan,
        scenarios: Sequence[Mapping[str, Any]],
        out: list[str],
        concerns: list[str],
    ) -> None:
        """★ 여기서 나오는 것은 대부분 `concerns` 다.

        M-16 이 잡는 것은 **마스터 자신의 배선 문제**다. 매입을 다시 불러도 마스터가
        물류를 안 부른 사실은 바뀌지 않는다. 사람이 봐야 할 것이지 재시도할 것이 아니다.
        """
        # 필요한 조언자를 다 불렀나 — 하나라도 빠지면 그 부서의 상한이 무한대로 남는다
        for agent in self.required_advisors:
            if not plan.called(agent, "PRE_PURCHASE"):
                concerns.append(f"M16-AGENT-MISSING: {agent} 를 PRE_PURCHASE 로 부르지 않았다")

        # 순서 역전 — 경계를 받기 전에 시나리오를 만들면 제약 없는 안이 나온다
        first_purchase = next(
            (s.seq for s in plan.steps if s.agent == "purchase" and s.mode == "GENERATE_SCENARIOS"),
            None,
        )
        if first_purchase is not None:
            for agent in self.required_advisors:
                pre = next(
                    (s.seq for s in plan.steps if s.agent == agent and s.mode == "PRE_PURCHASE"),
                    None,
                )
                if pre is None or pre > first_purchase:
                    concerns.append(
                        f"M16-ORDER: 매입 호출(#{first_purchase})이 {agent} 경계보다 앞섰다"
                    )

        # 시나리오가 있는데 판정을 안 받았나
        if scenarios:
            for agent in self.required_advisors:
                if not plan.called(agent, "SCENARIO_VALIDATION"):
                    concerns.append(f"M16-VALIDATION-MISSING: {agent} 가 시나리오를 보지 않았다")

        # 봉투 검증이 남긴 것 — 여기서 다시 계산하지 않고 남아 있는지만 본다.
        #
        # ★ 누구의 위반인지로 갈린다.
        #   매입 것이면 다시 만들면 달라질 수 있으므로 재호출 대상이고,
        #   조언자 것이면 매입을 몇 번 불러도 그대로다 — 보고만 한다.
        seen: set[str] = set()
        for step in plan.steps:
            for code in step.finding_codes:
                line = f"M16-ENVELOPE: {step.agent}/{step.mode} 에 {code}"
                if line in seen:
                    continue  # 재호출로 같은 줄이 반복되면 읽는 사람만 피곤하다
                seen.add(line)
                (out if step.agent == "purchase" else concerns).append(line)

    # ── ③ 합쳤을 때의 모순 (§3.7.3) ─────────────────────────────

    def _check_timing_gate(
        self,
        proposal: Mapping[str, Any],
        scenarios: Sequence[Mapping[str, Any]],
        out: list[str],
        skipped: list[str],
    ) -> None:
        """타이밍 축이 닫혔는데 분할이 있나.

        ```text
        "timing" ∉ allowed_axes  AND  ∃ s: len(s.split_plan) > 1
        ```

        ★ **`allowed_axes` 는 제안 최상위, `split_plan` 은 시나리오 안이다.**
          초안은 둘 다 시나리오에서 찾았는데 `allowed_axes` 가 거기 없어
          **검사가 영영 발화하지 않았다.** 그런 검사는 `skipped` 로도 안 잡힌다 —
          "봤는데 문제없음"으로 읽힌다. (2026-08-27 매입 스키마로 확인)

        ★ `strategy_type` 으로 판정하지 않는다 (매입 지정). 축이 하나뿐인 날은 전 안이
          같은 축을 쓰므로 `strategy_type == "timing"` 이 안 나와도 분할은 존재할 수 있다.

        ★ `split_plan` 은 **최소 1**이다 (매입 스키마). 분할 미적용이 빈 배열이 아니라
          1회차 목록이므로 **경계는 `> 1`** 이다.
        """
        axes = proposal.get(_ALLOWED_AXES)
        if axes is None:
            if scenarios:
                skipped.append(f"L-TIMING-GATE: 제안에 {_ALLOWED_AXES} 가 없어 미검사")
            return
        if _TIMING in axes:
            return  # 축이 열려 있으면 분할은 정상이다

        for idx, scenario in enumerate(scenarios):
            split = scenario.get(_SPLIT_PLAN)
            if not isinstance(split, Sequence) or isinstance(split, (str, bytes)):
                skipped.append(f"L-TIMING-GATE: scenarios[{idx}] 에 {_SPLIT_PLAN} 이 없어 미검사")
                continue
            if len(split) > 1:
                out.append(
                    f"L-TIMING-GATE: scenarios[{idx}] 는 timing 축이 닫혔는데 "
                    f"분할 {len(split)} 회차다 (allowed_axes={list(axes)})"
                )

    # ── ② 마스터 계산 재검산 (§3.7.3-②) ─────────────────────────
    #
    # 매입이 §4-2 에서 항등식을 명시해 줘서 성립하게 됐다. 그전에는 "무엇과 무엇이
    # 같아야 하는가"가 계약에 없어 재검산할 대상 자체가 없었다.

    def _check_scenario_identities(
        self,
        scenarios: Sequence[Mapping[str, Any]],
        out: list[str],
        skipped: list[str],
    ) -> None:
        """시나리오 층 항등식.

        ```text
        total_qty_kg     == Σ split_plan[].qty_kg == Σ sourcing_plan[].qty_kg
        total_amount_krw == Σ(sourcing_plan[].qty_kg × grade_unit_price)
        ```

        ★ **자기검증이 아니다.** 매입도 같은 항등식을 강제한다고 했지만, 그 말을 믿는
          것과 확인하는 것은 다르다. 여기서는 **원시 항목에서 독립적으로 다시 더해**
          매입이 낸 합계와 대조한다 (§3.7.3-②).
        """
        for idx, scenario in enumerate(scenarios):
            label = f"scenarios[{idx}]"
            total_qty = _int_of(scenario.get("total_qty_kg"))
            total_amount = _int_of(scenario.get("total_amount_krw"))

            split_qty = _sum_field(scenario.get(_SPLIT_PLAN), "qty_kg")
            source_qty = _sum_field(scenario.get(_SOURCING_PLAN), "qty_kg")
            source_amount = _sum_product(scenario.get(_SOURCING_PLAN), "qty_kg", "grade_unit_price")

            for name, got, expected in (
                (f"{_SPLIT_PLAN} 수량 합", split_qty, total_qty),
                (f"{_SOURCING_PLAN} 수량 합", source_qty, total_qty),
            ):
                if got is None or expected is None:
                    skipped.append(f"L-IDENTITY-QTY: {label} 의 {name} 을 셀 수 없어 미검사")
                elif got != expected:
                    out.append(
                        f"L-IDENTITY-QTY: {label} 의 {name} {got:,} ≠ total_qty_kg {expected:,}"
                    )

            if source_amount is None or total_amount is None:
                skipped.append(f"L-IDENTITY-AMOUNT: {label} 의 등급별 금액을 셀 수 없어 미검사")
            elif source_amount != total_amount:
                out.append(
                    f"L-IDENTITY-AMOUNT: {label} 의 Σ(수량×등급단가) {source_amount:,} "
                    f"≠ total_amount_krw {total_amount:,}"
                )

    # ── ③ 분할 지급 일정 (매입 §3.2 · 재무 확정) ────────────────

    def _check_payment_schedule(
        self,
        scenarios: Sequence[Mapping[str, Any]],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        out: list[str],
        skipped: list[str],
    ) -> None:
        """분할 회차의 지급 일정이 시나리오와 맞물리는가.

        매입이 검증 대상 항등식 5개를 명시했다(§3.2). 그대로 옮긴다.

        ```text
        ① Σ qty_kg          == total_qty_kg
        ② Σ amount_krw      == total_amount_krw
        ③ purchase_date     == split_plan[i].date   (seq 대응)
        ④ payment_date      == purchase_date + N5
        ⑤ 분할이 아닌 시나리오에는 이 키가 없다
        ```

        ★ **N5 를 상수로 박지 않는다.** 재무 payload 의 `purchase_payment_days` 를 쓰고,
          없으면 ④를 `skipped` 로 남긴다. 7 을 박아 두면 정책이 바뀌어도 검사가
          옛 값으로 통과시킨다 — 검사가 거짓말하는 가장 흔한 방식이다.

        ★ ④는 **H1 확정 `payment_date` 가 있으면 건너뛴다** (재무: *"H1 값이
          authoritative"*). 지금 경로에는 H1 이 없지만, 붙었을 때 이 검사가 정상
          동작을 오류로 잡지 않게 미리 갈라 둔다.
        """
        pay_days = _int_of(constraints.get("finance", {}).get("purchase_payment_days"))

        for idx, scenario in enumerate(scenarios):
            label = f"scenarios[{idx}]"
            split = _rows(scenario.get(_SPLIT_PLAN))
            schedule = _rows(scenario.get(_PAYMENT_SCHEDULE))

            # ⑤ 분할이 아닌 안에는 이 키가 없어야 한다
            if schedule is not None and split is not None and len(split) <= 1:
                out.append(
                    f"L-PAYSCHED-UNEXPECTED: {label} 은 분할이 아닌데 "
                    f"{_PAYMENT_SCHEDULE} 가 있다 (split {len(split)} 회차)"
                )
                continue

            if schedule is None:
                if split is not None and len(split) > 1:
                    # 아직 안 실려 오는 신설 필드다. **통과로 치지 않는다.**
                    skipped.append(
                        f"L-PAYSCHED: {label} 은 분할 {len(split)} 회차인데 "
                        f"{_PAYMENT_SCHEDULE} 가 없어 미검사"
                    )
                continue

            self._payment_rows(label, scenario, split, schedule, pay_days, out, skipped)

    def _payment_rows(
        self,
        label: str,
        scenario: Mapping[str, Any],
        split: Sequence[Mapping[str, Any]] | None,
        schedule: Sequence[Mapping[str, Any]],
        pay_days: int | None,
        out: list[str],
        skipped: list[str],
    ) -> None:
        # ① 수량 합
        qty = _sum_field(schedule, "qty_kg")
        total_qty = _int_of(scenario.get("total_qty_kg"))
        if qty is not None and total_qty is not None and qty != total_qty:
            out.append(
                f"L-PAYSCHED-QTY: {label} 의 회차 수량 합 {qty:,} ≠ total_qty_kg {total_qty:,}"
            )

        # ② 금액 합 — 회차 금액은 오늘 단가 기준 추정이지만 **합은 총액과 같아야 한다**
        amount = _sum_field(schedule, "amount_krw")
        total_amount = _int_of(scenario.get("total_amount_krw"))
        if amount is not None and total_amount is not None and amount != total_amount:
            out.append(
                f"L-PAYSCHED-AMOUNT: {label} 의 회차 금액 합 {amount:,} "
                f"≠ total_amount_krw {total_amount:,}"
            )

        # ③ 매입일이 split_plan 과 seq 대응하는가
        if split is None:
            skipped.append(f"L-PAYSCHED-DATE: {label} 에 {_SPLIT_PLAN} 이 없어 대조 불가")
        elif len(split) != len(schedule):
            out.append(
                f"L-PAYSCHED-DATE: {label} 의 회차 수가 다르다 — "
                f"{_SPLIT_PLAN} {len(split)} vs {_PAYMENT_SCHEDULE} {len(schedule)}"
            )
        else:
            for row, plan_row in zip(schedule, split, strict=True):
                seq = row.get("seq")
                if row.get("purchase_date") != plan_row.get("date"):
                    out.append(
                        f"L-PAYSCHED-DATE: {label} seq {seq} 의 purchase_date "
                        f"{row.get('purchase_date')} ≠ {_SPLIT_PLAN} 의 {plan_row.get('date')}"
                    )

        # ④ 지급일 = 매입일 + N5
        for row in schedule:
            seq = row.get("seq")
            if row.get("h1_payment_date") or row.get("payment_date_authoritative"):
                skipped.append(f"L-PAYSCHED-N5: {label} seq {seq} 는 H1 확정 지급일이라 미검사")
                continue
            if pay_days is None:
                skipped.append(
                    f"L-PAYSCHED-N5: {label} seq {seq} — 재무 purchase_payment_days 가 없어 미검사"
                )
                continue
            gap = _day_gap(row.get("purchase_date"), row.get("payment_date"))
            if gap is None:
                skipped.append(f"L-PAYSCHED-N5: {label} seq {seq} 의 날짜를 읽을 수 없어 미검사")
            elif gap != pay_days:
                out.append(f"L-PAYSCHED-N5: {label} seq {seq} 의 지급 간격 D+{gap} ≠ D+{pay_days}")

        # 상한 — 재무가 STRESS Cashflow 로 쓴다. 수량 × 상한가여야 한다
        max_price = _int_of(scenario.get("max_price"))
        for row in schedule:
            declared = _int_of(row.get("amount_max_krw"))
            row_qty = _int_of(row.get("qty_kg"))
            if declared is None or row_qty is None or max_price is None:
                continue
            if declared != row_qty * max_price:
                out.append(
                    f"L-PAYSCHED-MAX: {label} seq {row.get('seq')} 의 amount_max_krw "
                    f"{declared:,} ≠ qty {row_qty:,} × max_price {max_price:,}"
                )

    # ── 커버리지 정직성 (§3.7.6) ────────────────────────────────

    # ── 실어 준 값을 미결이라 답하는가 ────────────────────────────

    #: 부서가 *"이 값이 없어서 못 했다"* 고 말할 때 쓰는 말.
    _UNRESOLVED_WORDS = ("미확정", "미결", "싣지 않았다", "받지 못")

    #: 한 문장 안에서만 본다. 문장이 바뀌면 다른 얘기다.
    _SENTENCE_END = re.compile(r"[.。\n;]")

    #: 🔴 **글자 수로 재지 않는다.** 처음엔 *"cap_by_date 검사는 inbound_lead_days(N4)
    #: 미확정으로 보류"* 에서 `cap_by_date` 까지 잡혀서 **12자 이내**로 좁혔는데,
    #: 그건 원인을 고친 것이 아니라 증상을 잘라 낸 것이었다. 매입이 8/31 에
    #: 지적했다 — *"저희가 쓰려던 문구가 12자를 넘어 안 울립니다"*.
    #:
    #: **검사가 남의 문장 길이를 정하면 안 된다.** 진짜 규칙은 이것이다:
    #: 키와 미결 어휘 사이에 **다른 실린 키가 끼어 있으면 그 키 얘기다.**
    #:
    #: ```text
    #: "cap_by_date 검사는 inbound_lead_days(N4) 미확정으로 보류"
    #:   cap_by_date        → 사이에 inbound_lead_days 가 있다        → 건너뛴다
    #:   inbound_lead_days  → 사이에 다른 키가 없다                    → 울린다
    #:
    #: "operational_limit_days는 받았으나 등급 어휘 미확정(#69)"
    #:   operational_limit_days → 사이에 다른 키가 없다                → 울린다 (13자여도)
    #: ```
    def _unresolved_here(self, text: str, key: str, others: Iterable[str]) -> bool:
        for match in re.finditer(re.escape(key), text):
            tail = text[match.end() :]
            stop = self._SENTENCE_END.search(tail)
            clause = tail[: stop.start()] if stop else tail
            spots = [clause.find(word) for word in self._UNRESOLVED_WORDS]
            spots = [i for i in spots if i >= 0]
            if not spots:
                continue
            between = clause[: min(spots)]
            if any(other != key and other in between for other in others):
                continue  # 그 키 얘기다 — 같은 원인을 두 번 보고하지 않는다
            return True
        return False

    #: 부서가 실은 것이지만 **값이 아니라 메타**라 대조 대상이 아닌 키.
    #:
    #: ★ **계약 쪽 목록을 그대로 읽는다** (`envelope.ENVELOPE_META_KEYS`). 전에는 같은
    #:   목록을 여기 따로 적어 뒀는데, `required_claims` 는 `soft_warnings` 에 근거를
    #:   요구하고 이 검사는 그것을 메타로 빼는 **정반대 상태**가 됐다 (실측 2026-08-30).
    #:   두 벌을 두면 언젠가 갈린다 — 갈렸다.
    _NOT_A_VALUE = ENVELOPE_META_KEYS

    def _supplied_keys(self, constraints: Mapping[AgentName, Mapping[str, Any]]) -> set[str]:
        """봉투에 **값이 실린** 키. 최상위와 **항목 배열 안 한 겹**까지 본다.

        🔴 **한 겹을 안 봐서 ②③ 을 놓칠 뻔했다 (실측 2026-08-31).** 매입이 읽는
        `operational_limit_days`·`medium_grade_factor` 는 최상위가 아니라
        `item_storage_policies[]` **안**에 있다. 최상위만 보면 `supplied` 에
        `item_storage_policies` 만 들어가고, 매입이 *"operational_limit_days 미확정"*
        이라고 적어도 **이 검사는 조용하다.**

        이 클래스 주석이 *"②③ 배선하면 그때 이 검사가 울리는 것이 옳다"* 고 예고해
        뒀는데, **울리지 않을 상태였다.** 예고가 검사를 대신하지 않는다.

        ★ **`None` 인 칸은 안 넣는다.** 로트 `grade` 가 그렇다 — 전부 `None` 이라
          *"실어 준 값"* 이 아니고, 매입이 *"grade 미확정"* 이라 말하면 그건
          **맞는 말이다.** 맞는 말을 지적으로 올리면 안 된다.

        ★ 한 겹만 판다. 더 깊이는 `required_claims` 와 같은 규율이다 —
          더 깊은 중첩의 규칙은 도메인이 정한다.
        """
        supplied: set[str] = set()
        for payload in constraints.values():
            for key, value in payload.items():
                if key in self._NOT_A_VALUE or value is None:
                    continue
                supplied.add(key)
                if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
                    continue
                for item in value:
                    if not isinstance(item, Mapping):
                        continue
                    supplied.update(
                        sub for sub, sub_value in item.items() if sub_value is not None
                    )
        return supplied

    def _check_supplied_but_unused(
        self,
        scenarios: tuple[Mapping[str, Any], ...],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        concerns: list[str],
    ) -> None:
        """🔴 **마스터가 실어 준 값을 부서가 "없다" 고 답하는가.**

        ★ **조정자만 볼 수 있는 종류다.** 각 부서는 자기 쪽만 본다 — 물류는 자기가
          보낸 것을 알고, 매입은 자기가 못 읽은 것을 안다. **둘을 나란히 놓는 것은
          마스터뿐**이고, 안 보면 아무도 안 본다.

        ★ **실측에서 나왔다 (2026-08-29).** `inbound_lead_days` 가 봉투에 `2.0` 으로
          실려 있는데 매입은 *"inbound_lead_days(N4) 미확정"* 으로 도착일 계산을
          보류했다 — 매입이 봉투 대신 자기 `constraints.yaml` 의 `pending` 을 본다.
          값이 있는데 안 쓰면 **더 보수적인 안이 나오고, 아무도 이유를 모른다.**

        ★ `findings` 가 아니라 `concerns` 다. **재호출해도 안 고쳐진다** — 배선을
          고쳐야 하는 일이라 사람이 봐야 한다 (§3.4).

        🔴 **증상을 잡지 원인을 못 찾는다.** 실측 ①(2026-08-29)의 원인이 **둘**이었는데
          이 검사는 하나만 봤다.

        ```text
        allocate_sourcing 이 pending 을 직접 읽어 pending_value() 우회   ← 잡았다
        어댑터가 N4 를 State 최상위에 안 올려 draft_plan 이 못 봄        ← 못 잡았다
        ```

          뒤엣것은 **올바른 함수를 쓰는데도 값을 못 보는** 상태다. 봉투 밖에서 값이
          어디로 흐르는지는 그 파트만 알고, 마스터는 *"미결이라 답했다"* 는 사실까지만
          본다. **원인 규명은 그 파트 몫이고, 이 검사는 물어볼 거리를 만들 뿐이다.**

        ★ **①은 닫혔다 (2026-08-30, `713e515` · dev `50ba70c` 반영).** 물류가
          `inbound_lead_days: 2.0` 을 실어 주고 매입이 그것으로 도착일을 계산한다.
          같은 관통에서 이 검사는 **0건**이고, 그 0 은 진짜다 — 키가 실제로 봉투에
          있는 것을 따로 확인했다. **실리지 않은 키로 낸 0 은 통과가 아니다.**

        🔴 **세 번째 원인이 곧 온다.** 매입이 `operational_limit_days`·
          `medium_grade_factor` 를 등급 배분에 배선하면(②③, 매입 2026-08-30 회신),
          두 값을 **쓰면서도** 결론이 안 난다 — 로트 `grade` 가 전부 `None` 이라
          기준 등급 로트를 특정할 수 없기 때문이다(#69, 실측으로 확인). 그때 이
          검사가 울리는 것은 옳지만, **원인은 물류도 매입도 아니고 등급 어휘 미확정**
          이다. 그래서 고지 문구가 원인을 하나로 단정하지 않는다.

        ★ **키 이름으로만 대조한다.** 이름이 다른 불일치는 여기서 못 잡는다
          (물류 `item_storage_policies[].operational_limit_days` vs 매입
          `lots[].shelf_life_days`). 별칭 표를 두면 어긋날 자리가 하나 더 생기고,
          그건 8/29 에 걷어낸 층이다 — **이름 합의는 팀이 할 일**이다.
        """
        supplied = self._supplied_keys(constraints)
        if not supplied:
            return

        seen: set[str] = set()
        for scenario in scenarios:
            for risk in scenario.get("risks") or ():
                text = str(risk)
                for key in supplied:
                    if key in seen:
                        continue
                    if self._unresolved_here(text, key, supplied):
                        seen.add(key)
                        concerns.append(
                            f"SUPPLIED-BUT-UNRESOLVED: '{key}' 는 봉투에 실려 있는데 "
                            f"매입이 미결로 답했다 — 원인은 최소 셋이고 마스터는 "
                            f"고르지 않는다: ① 봉투 대신 다른 곳을 본다 ② 올바른 "
                            f"함수를 쓰는데 값이 그 자리까지 안 온다 ③ 이 값은 "
                            f"쓰는데 다른 입력이 없어 결론이 안 난다 "
                            f"(사유: {text[:80]})"
                        )

    def _declare_uncovered(self, skipped: list[str]) -> None:
        """아직 이 경로에 붙지 않은 검사를 드러낸다.

        ★ 이 줄이 없으면 `findings: []` 가 **"56검사를 통과했다"로 읽힌다.**
          붙지 않은 것을 조용히 두는 것이 커버리지를 감추는 가장 흔한 방식이다.
        """
        skipped.append("②마스터 계산 재검산: 결합·클리핑 Tool 이 Flow 에 붙은 뒤 가능")
