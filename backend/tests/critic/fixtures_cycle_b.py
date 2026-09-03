"""
fixtures_cycle_b.py — 사이클 B 픽스처 (S1 후보 · S2 회신 · 4케이스)

fixtures.py(사이클 A)와 같은 규약이다. 실제 값이 아니라 **골격이 도는지** 보는 것이 목적이다.

★ 여기 값은 전부 플레이스홀더다. N17(공용 출고 능력)과 납품 소요일이 확정되면
  `_INVENTORY_CAPS` 와 `_DUE_DAYS` 를 페르소나 값으로 교체한다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from checks_finance_cycle_b import check_cash_recovery_priority
from fixtures import AS_OF, DEMAND, ITEMS4, PRICE_BASE

from app.contracts.core import (
    ChannelLeg,
    CheckResult,
    CycleBState,
    Evidence,
    MinimalAllocation,
    OutboundLeg,
    SalesFacts,
    T2Reply,
)

# ── 플레이스홀더 ──────────────────────────────────────────────────
SHARED_OUTBOUND_KG = 2_500.0  # ⏳ N17 — 하루 공용 출고 능력
_ON_HAND_KG = {i: 200.0 for i in ITEMS4}  # T0 스냅샷 inventory_available_kg 와 일치
CONTRACT_PRICE = {"배추": 2293.11, "무": 1400.0, "양파": 1600.0, "피마늘": 8000.0}

CHANNELS = {
    "KIMCHI_FACTORY_001": {"due_days": 2, "settlement_days": 30},
    "MEAL_SERVICE_001": {"due_days": 1, "settlement_days": 15},
    "SPOT": {"due_days": 0, "settlement_days": 0},
}


# ---------------------------------------------------------------------------
# S1 — 판매 배분 후보
# ---------------------------------------------------------------------------


def make_allocations(
    ratios: tuple[float, ...] = (0.3, 0.6, 1.0),
    as_of: date = AS_OF,
    on_hand: dict | None = None,
) -> list[MinimalAllocation]:
    """
    보유 재고의 몇 %를 오늘 내보낼지로 후보를 만든다.

    ★ 사이클 A 의 커버일수 D 와 대응하는 축이다. B 에는 strategy_type 축이 없으므로
      (분할 입고·등급 조달이 없다) 수량 하나로만 차별화된다.
    """
    stock = on_hand or _ON_HAND_KG
    labels = {0.3: "보유", 0.6: "균형", 1.0: "소진"}
    out: list[MinimalAllocation] = []
    for r in ratios:
        legs = tuple(
            ChannelLeg(
                channel="KIMCHI_FACTORY_001",
                item=i,
                qty_kg=round(stock[i] * r, 1),
                unit_price_krw_per_kg=CONTRACT_PRICE[i],
                lot_ids=(f"lot_{i}",),
                due_date=as_of + timedelta(days=CHANNELS["KIMCHI_FACTORY_001"]["due_days"]),
            )
            for i in stock
        )
        # 나머지는 HOLD — 출고가 아니다 (영업 IO 명세 §5)
        held = tuple(
            ChannelLeg(
                channel="HOLD",
                item=i,
                qty_kg=round(stock[i] * (1 - r), 1),
                unit_price_krw_per_kg=0.0,
                lot_ids=(f"lot_{i}",),
            )
            for i in stock
            if stock[i] * (1 - r) > 0
        )
        legs = legs + held

        contribution = sum(
            l.qty_kg * (l.unit_price_krw_per_kg - PRICE_BASE[l.item]) for l in legs if l.is_outbound
        )
        shipped = sum(l.qty_kg for l in legs if l.is_outbound)
        out.append(
            MinimalAllocation(
                allocation_id=f"ALO-{int(r * 100):03d}",
                strategy_type=labels.get(r, f"R{r}"),
                legs=legs,
                expected_contribution_krw=round(contribution, 1),
                rationale=f"보유 재고의 {r:.0%} 방출",
                # ★ 확정 납품분을 포함한 그날의 총 출고량. HOLD 는 빠진다.
                outbound_by_date=(OutboundLeg(as_of, round(shipped, 1)),),
                estimation_confidence="LOW",
            )
        )
    return out


def make_sales_facts(coverable_ratio: float = 1.0) -> SalesFacts:
    """
    영업의 사실 보고 (영업 IO 명세 §5).

    ★ 영업은 판정하지 않는다. 의무량과 충당 가능량이라는 사실만 낸다.
      E5 판정은 S3 가 한다.
    """
    return SalesFacts(
        confirmed_obligation_kg=dict(DEMAND),
        coverable_kg={i: round(v * coverable_ratio, 1) for i, v in DEMAND.items()},
        no_feasible_reason=None,
    )


# ---------------------------------------------------------------------------
# S2 — 부서 회신
# ---------------------------------------------------------------------------


def inventory_reply_b(
    shared_outbound_kg: float = SHARED_OUTBOUND_KG,
    cap_kg: dict | None = None,
    as_of: date = AS_OF,
    state: CycleBState | None = None,
) -> T2Reply:
    """
    재고 S2 — 공용 출고 능력(cap_total) + 품목별 상한(cap_kg)

    ★ 실물은 `checks_inventory_cycle_b.py` 의 두 함수가 낸다. 둘 다 값 대기 중이라
      여기서는 같은 **형태**의 CheckResult 를 직접 만든다.
      값이 확보되면 이 빌더를 그 함수 호출로 바꾸면 된다.

    ★ overlay 를 받아도 **on_hand 는 늘지 않는다** (§3.5).
      승인 매입은 in_transit 으로만 들어가므로 오늘 팔 수 있는 양은 그대로다.
      overlay 가 바꾸는 것은 날짜별 창고 여유이지 오늘의 출고 상한이 아니다.
    """
    caps = cap_kg or dict(_ON_HAND_KG)
    return T2Reply(
        "inventory",
        as_of,
        (
            CheckResult(
                "check_daily_shared_outbound",
                "inventory",
                "conditional",
                "hard",
                f"하루 공용 출고 능력 {shared_outbound_kg:,.0f}kg",
                (
                    Evidence(
                        "공용 출고 능력",
                        "inventory",
                        ("SRC-OUT-001",),
                        shared_outbound_kg,
                        "kg",
                        "SIM_FIXED",
                        "N17 대기 — 플레이스홀더",
                    ),
                ),
                cap_total_kg=shared_outbound_kg,
                cycle="B",
                evidence_grade="SIM_FIXED",
                source_ref="persona.물류.daily_outbound",
            ),
            CheckResult(
                "check_outbound_freshness_window",
                "inventory",
                "ok",
                "hard",
                "신선도 창 내 출고 가능량",
                tuple(
                    Evidence(f"{i} 출고 가능", "inventory", (f"lot_{i}",), v, "kg", "OFFICIAL")
                    for i, v in caps.items()
                ),
                cap_kg=caps,
                cycle="B",
                evidence_grade="OFFICIAL",
                source_ref="inventory_lots.on_hand",
            ),
        ),
        reasoning="공용 출고 능력과 신선도 창을 반영한 오늘의 출고 상한",
    )


def finance_reply_b(
    state: CycleBState,
    base_priority: str = "MEDIUM",
    as_of: date = AS_OF,
) -> T2Reply:
    """
    재무 S2 — 회수 시급도 (소프트)

    ★ 실물 함수(`check_cash_recovery_priority`)를 그대로 호출한다.
      이 검사는 값이 이미 확보돼 있어 스텁이 아니다.
    """
    chk = check_cash_recovery_priority(
        {}, {"cycle_b_state": state, "base_cash_priority": base_priority}
    )
    return T2Reply("finance", as_of, (chk,), reasoning="H1 승인분을 반영한 판매 시점 현금 시급도")


# ---------------------------------------------------------------------------
# 4케이스
# ---------------------------------------------------------------------------

CASES_B: dict[str, dict[str, Any]] = {
    "B_NORMAL": {
        "allocations": make_allocations(),
        "shared_outbound_kg": SHARED_OUTBOUND_KG,
        "expect": "정상 — 후보 3안이 전부 밴드 안. H2 로 2안 이상 올라간다",
    },
    "B_SHARED_BINDING": {
        "allocations": make_allocations(),
        "shared_outbound_kg": 400.0,
        "expect": "공용 출고 능력이 구속 — 품목별로는 통과인데 합치면 초과 (§3.7.4 검사 3번)",
    },
    "B_COLLAPSED": {
        "allocations": make_allocations(),
        "shared_outbound_kg": 100.0,
        "expect": "전 후보가 같은 값으로 수렴 — 단일안 + [보류] 폴백",
    },
    "B_NO_CAPACITY": {
        "allocations": make_allocations(),
        "shared_outbound_kg": 0.0,
        "expect": "출고 능력 0 → 전 후보 실행 불가 → S1 회송 → 판매 0 → 의무 미충족이면 E5",
    },
}
