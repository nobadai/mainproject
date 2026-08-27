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
  Critic 56검사가 아직 이 경로에 붙지 않았다는 사실 자체를 `skipped` 로 노출한다.
  붙지 않은 것을 조용히 두면 시연에서 "검증이 돈다"가 사실이 아니게 된다.

★ 왜 ①이 여기 없는가
  `E-BIND-*` · `E-EVIDENCE-*` · `E-REASONING-*` 는 `MasterRunner.call()` 이 호출마다
  돌려 `ExecutionStep.finding_codes` 에 쌓는다. 여기서는 **그것이 남아 있는지**만 본다
  (`M16-ENVELOPE`) — 두 번 계산하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.master.envelope import AgentName
from app.master.plan import ExecutionPlan

# 매입이 밝힌 판정 필드 (2026-08-27 회신). 없으면 그 검사는 skipped 다.
_ALLOWED_AXES = "allowed_axes"
_SPLIT_PLAN = "split_plan"
_TIMING = "timing"


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

    def __init__(self, required_advisors: tuple[AgentName, ...] = ("finance", "inventory")):
        self.required_advisors = required_advisors

    def __call__(
        self,
        scenarios: Sequence[Mapping[str, Any]],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        plan: ExecutionPlan,
    ) -> VerificationResult:
        findings: list[str] = []
        concerns: list[str] = []
        skipped: list[str] = []

        self._check_plan_integrity(plan, scenarios, findings, concerns)
        self._check_timing_gate(scenarios, findings, skipped)
        self._declare_uncovered(skipped)

        return VerificationResult(tuple(findings), tuple(concerns), tuple(skipped))

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
        scenarios: Sequence[Mapping[str, Any]],
        out: list[str],
        skipped: list[str],
    ) -> None:
        """타이밍 축이 닫혔는데 분할이 있나.

        ★ `strategy_type` 이 아니라 `allowed_axes` 로 잡는다 (2026-08-27 매입 지정).
          `strategy_type` 은 그 시나리오가 어느 축을 **썼는지**이고, `allowed_axes` 는
          그날 어느 축이 **열렸는지**다. 게이트는 후자다.

        하나의 신뢰도 판정이 개수·허용 축·분할 진입 셋을 동시에 정하므로(§4.2.2),
        **축이 닫혔는데 분할이 있으면 그 판정이 지켜지지 않은 것이다.**
        """
        checked = 0
        for idx, scenario in enumerate(scenarios):
            axes = scenario.get(_ALLOWED_AXES)
            split = scenario.get(_SPLIT_PLAN)
            if axes is None or split is None:
                continue
            checked += 1
            if _TIMING in axes:
                continue
            rounds = len(split) if isinstance(split, Sequence) else 0
            if rounds > 1:
                out.append(
                    f"L-TIMING-GATE: scenarios[{idx}] 는 timing 축이 닫혔는데 "
                    f"분할 {rounds} 회차다"
                )

        if scenarios and checked == 0:
            skipped.append(
                f"L-TIMING-GATE: 시나리오에 {_ALLOWED_AXES}·{_SPLIT_PLAN} 이 없어 미검사"
            )

    # ── 커버리지 정직성 (§3.7.6) ────────────────────────────────

    def _declare_uncovered(self, skipped: list[str]) -> None:
        """아직 이 경로에 붙지 않은 검사를 드러낸다.

        ★ 이 줄이 없으면 `findings: []` 가 **"56검사를 통과했다"로 읽힌다.**
          붙지 않은 것을 조용히 두는 것이 커버리지를 감추는 가장 흔한 방식이다.
        """
        skipped.append(
            "Critic L0~L5 (56검사): 마스터 경로 미배선 — "
            "도메인 payload 필드명 미확정(물류 미제출 · 매입 키 표 미수령)"
        )
        skipped.append("②마스터 계산 재검산: 결합·클리핑 Tool 이 Flow 에 붙은 뒤 가능")
