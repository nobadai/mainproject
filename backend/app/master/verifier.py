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
from datetime import date
from typing import Any

from app.master.envelope import AgentName
from app.master.plan import ExecutionPlan


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

    def __init__(self, required_advisors: tuple[AgentName, ...] = ("finance", "inventory")):
        self.required_advisors = required_advisors

    def __call__(
        self,
        proposal: Mapping[str, Any],
        constraints: Mapping[AgentName, Mapping[str, Any]],
        verdicts: Mapping[AgentName, Mapping[str, Any]],
        plan: ExecutionPlan,
    ) -> VerificationResult:
        """★ 시나리오 배열이 아니라 **제안 전체**를 받는다 (2026-08-27 매입 스키마 확인).

        `allowed_axes` · `situation` · `confidence` 는 `scenarios[]` 안이 아니라
        **제안 최상위**에 있다(`PurchaseProposal`). 배열만 받으면 그 판정들을 볼 수 없다.
        """
        scenarios = _scenarios_of(proposal)
        findings: list[str] = []
        concerns: list[str] = []
        skipped: list[str] = []

        self._check_plan_integrity(plan, scenarios, findings, concerns)
        self._check_timing_gate(proposal, scenarios, findings, skipped)
        self._check_scenario_identities(scenarios, findings, skipped)
        self._check_payment_schedule(scenarios, constraints, findings, skipped)
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
                out.append(
                    f"L-PAYSCHED-N5: {label} seq {seq} 의 지급 간격 D+{gap} ≠ D+{pay_days}"
                )

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
