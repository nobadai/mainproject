"""
checks_finance_cycle_b.py — 재무 S2 조언 (담당: 이채훈)

═══════════════════════════════════════════════════════════════════════
 정의서 §3.1 — S2 에서 재무가 내는 것

     재무 → 현금 회수 시급도 · 채널별 회수 조건 평가

 ★ **사이클 A 와 성격이 다르다.**
   A 에서 재무는 `cap_amount_krw` 라는 하드 상한을 낸다 — "이 금액을 넘지 마라".
   B 에서는 상한이 아니라 **선호 신호**다 — "회수가 급하니 결제 빠른 채널을 골라라".

   판매를 금액으로 막을 근거가 없다. 재고를 파는 것은 현금을 **늘리는** 행위이므로
   금액 하드 제약을 걸면 방향이 반대가 된다. 그래서 두 검사 모두 **소프트**다.

 ★ CheckResult 계약상 재무는 cap_amount_krw 외의 밴드 필드를 채울 수 없다(§3.6.6).
   소프트 경고는 밴드를 채우지 않으므로 계약과 충돌하지 않는다.
   severity 로 S3 의 후보 순위에 영향을 준다.
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.contracts.core import (
    CheckResult,
    Evidence,
    ItemCode,
    compute_sales_cash_priority,
)

DEPT = "finance"
CYCLE = "B"

# 회수 시급도 → 소프트 경고 등급 (§3.6.9)
_PRIORITY_SEVERITY = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}


def check_cash_recovery_priority(
    allocation_qty: Mapping[ItemCode, float],
    ctx: Mapping[str, Any],
) -> CheckResult:
    """
    현금 회수 시급도 — `sales_cash_priority` (§7.4.1)

    ★ 영업이 쓰는 값은 T0 의 `base_cash_priority` 가 **아니다.**
      H1 에서 대규모 선매입이 승인되면 그만큼 미래 현금이 묶이므로 판매 시점의
      시급도가 T0 시점과 달라진다. 두 값을 같은 이름으로 덮어쓰지 않고
      `basis` 를 함께 기록한다 — 어느 기준으로 판단했는지가 남아야
      백테스트 후 "그날 왜 급하게 팔았나"에 답할 수 있다.

    산출은 `contracts_core.compute_sales_cash_priority()` 하나뿐이다.
    이 검사는 그 결과를 CheckResult 로 감싸 S2 회신 형식에 맞출 뿐이다 —
    룰을 두 벌 짜지 않는다(§6.4).

    ctx 필요 키
        cycle_b_state       : CycleBState   ← S3 가 overlay 를 넘겨준다
        base_cash_priority  : str           ← T0 스냅샷 값
    """
    state = ctx["cycle_b_state"]
    result = compute_sales_cash_priority(state, ctx["base_cash_priority"])
    priority, basis = result["cash_priority"], result["basis"]

    urgent = priority == "HIGH"
    return CheckResult(
        check_id="check_cash_recovery_priority",
        dept=DEPT,
        cycle=CYCLE,
        verdict="conditional" if urgent else "ok",
        kind="soft",
        severity=_PRIORITY_SEVERITY.get(priority, "MEDIUM"),
        reason=(
            f"현금 회수 시급도 {priority} (기준 {basis}) — 결제 주기가 짧은 채널을 우선 배분할 것"
            if urgent
            else f"현금 회수 시급도 {priority} (기준 {basis})"
        ),
        evidences=(
            Evidence(
                claim=f"sales_cash_priority={priority}",
                source="finance",
                ref_ids=(state.provenance().get("snapshot_id") or "T0",),
                value=1.0 if urgent else 0.0,
                unit="flag",
                evidence_grade="OFFICIAL",
            ),
        ),
        evidence_grade="OFFICIAL",
        source_ref="finance.sales_cash_priority",
    )


def check_channel_settlement_terms(
    allocation_qty: Mapping[ItemCode, float],
    ctx: Mapping[str, Any],
) -> CheckResult:
    """
    채널별 회수 조건 평가 — 배분 가중평균 회수일이 투영 기간을 넘는가

        가중평균 회수일 = Σ(채널 배분액 × 채널 결제일수) / Σ 배분액

    ★ 소프트인 이유. 회수가 늦은 채널이라도 파는 것이 안 파는 것보다 낫다.
      하드로 걸면 결제 30일 채널(김치공장)이 통째로 막혀 확정 납품 의무를
      못 지키게 된다 — 계약 위반이 현금 지연보다 비싸다.

    ctx 필요 키
        channel_settlement_days : {channel: int}   ✅ 페르소나 거래처 settlement.days
        horizon_days            : int              ✅ T0 스냅샷 재무 투영 기간
        legs                    : [ChannelLeg]     ← S1 후보의 채널별 배분

    ⏳ 임계(투영 기간 대비 몇 %에서 경고할지)는 백테스트 후 보정한다 (§3.6.9).
    """
    raise NotImplementedError(
        "채널별 결제일수는 페르소나에 있으나 경고 임계가 미정 — 백테스트 후 보정(§3.6.9). "
        "시그니처는 확정이므로 러너 등록은 지금 가능하다."
    )


FINANCE_CYCLE_B_CHECKS = {
    "check_cash_recovery_priority": check_cash_recovery_priority,  # ✅ 구현됨
    "check_channel_settlement_terms": check_channel_settlement_terms,  # ⏳ 임계 미정
}
