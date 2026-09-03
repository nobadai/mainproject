"""
fixtures.py v0.2 — 그래프 회귀 테스트용 6케이스

v0.1 → v0.2
  · 시나리오에 split_plan · sourcing_plan 추가 (삼중 일치 검증 대상)
  · IDENTITY_KEPT     신설 — 클리핑 후에도 삼중 일치가 유지되는가 (B1)
  · SPLIT_DIVERSITY   신설 — 총량 동일 + 분할 상이가 붕괴로 오판되지 않는가 (B2)
  · T0Snapshot 에 v0.2 8필드 반영

⚠ DEMAND 값 주의 — 현재 값은 정의서의 **금액 비중**을 수량으로 옮긴 근사치다.
  실제 수량 비중이 확보되면 교체할 것. 건고추 사례(수량 3.2% / 금액 33.4%)가
  보여주듯 두 기준은 10배까지 갈릴 수 있다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.contracts.core import (
    ITEMS,
    CheckResult,
    Evidence,
    FinanceSnapshot,
    InventoryLot,
    MinimalScenario,
    SourcingLot,
    SplitLeg,
    T0Snapshot,
    T2Reply,
    gate_variant_axes,
)

AS_OF = date(2023, 3, 15)  # ← 플레이스홀더. sim_start_date 확정 시 교체.

#: 픽스처 품목은 **계약에서 가져온다**. 여기서 따로 세면 계약이 바뀐 날 조용히
#: 어긋나고, 그때 깨지는 것은 계약이 아니라 픽스처다.
FIXTURE_ITEMS = ITEMS

PRICE_BASE = {"배추": 1500.0, "무": 900.0, "양파": 1100.0}
PRICE_HIGH = {"배추": 1800.0, "무": 900.0, "양파": 1100.0}
DEMAND = {"배추": 737.5, "무": 84.5, "양파": 11.8}  # 합 833.8 kg/일

# 금액 비중 (정의서 §7.0) — mix 게이팅의 정본
#
# ⚠️ 합이 1.0 이 아니라 0.918 이다. 피마늘 몫 0.082 를 뺐고 **정규화하지 않았다** —
#   정의서에 있는 값을 그대로 두는 쪽이 지어낸 값보다 낫다. 게이팅은
#   ``max(...) < ITEM_CONCENTRATION_THRESHOLD`` 로 최대값만 보므로(0.812 vs 0.70)
#   정규화해도(배추 0.884) 결과가 같다.
MIX_AMOUNT = {"배추": 0.812, "무": 0.093, "양파": 0.013}

# 등급 배분 (특 20% / 상 60% / 중 20%) — 단가는 상 등급 대비 ±10%
GRADE_SPLIT = {"특": 0.2, "상": 0.6, "중": 0.2}
GRADE_MULT = {"특": 1.10, "상": 1.00, "중": 0.90}


def _ev(claim, ref, value, unit, grade="ASSUMED", detail="") -> Evidence:
    return Evidence(claim, "tool_calc", (ref,), value, unit, grade, detail)


def make_snapshot(as_of: date = AS_OF, cash: float = 40_000_000.0, run_seq: int = 1) -> T0Snapshot:
    fin = FinanceSnapshot(
        cash_balance_krw=cash,
        salary_due_krw=12_941_280.0,
        debt_service_due_krw=94_000.0,
        minimum_operating_cash_krw=5_000_000.0,
        receivable_incoming_krw=20_000_000.0,
        projected_cash_min_krw=cash * 0.3,
        credit_headroom_krw=200_000_000.0 - 26_433_125.0,
        horizon_days=30,
    )
    return T0Snapshot(
        as_of=as_of,
        run_seq=run_seq,
        forecasts=(),
        spot_price_krw_per_kg=dict(PRICE_BASE),
        inventory_available_kg={i: 200.0 for i in FIXTURE_ITEMS},
        warehouse_free_kg=17_600.0,
        confirmed_orders_kg=dict(DEMAND),
        finance=fin,
        budget_envelope_krw=fin.purchasable_krw,
        # ── v0.2 ────────────────────────────────────────────────
        price_basis="AUCTION",  # N11 종결 — 경락가 확정
        contract_price_basis="AUCTION",
        item_mix_ratio_amount=dict(MIX_AMOUNT),
        item_mix_ratio_qty={i: DEMAND[i] / sum(DEMAND.values()) for i in FIXTURE_ITEMS},
        allowed_variant_axes=gate_variant_axes(MIX_AMOUNT, split_entry_ok=True),
        snapshot_id=f"SNAP-{as_of}-{run_seq}",
        inbound_lead_days=2,  # N4 미확정 — 플레이스홀더
        lots=tuple(InventoryLot(f"lot_{i}", i, 200.0, 30) for i in FIXTURE_ITEMS),
        confirmed_occupancy_by_date={as_of + timedelta(days=d): 3_000.0 for d in range(8)},
        contract_price_krw_per_kg={"배추": 2293.11},
        margin_defense_floor_rate=0.267,  # 거치 구간
        grade_unit_price={
            (i, g): PRICE_BASE[i] * m for i in FIXTURE_ITEMS for g, m in GRADE_MULT.items()
        },
    )


def _sourcing(qty: dict[str, float], price: dict[str, float]) -> tuple[SourcingLot, ...]:
    lots = []
    for i, q in qty.items():
        for g, share in GRADE_SPLIT.items():
            lots.append(
                SourcingLot(
                    item=i,
                    grade=g,
                    qty_kg=round(q * share, 3),
                    unit_price_krw_per_kg=price[i] * GRADE_MULT[g],
                    ref_ids=(f"SRC-FC-{i}-{g}",),
                )
            )
    return tuple(lots)


def _weighted_price(price: dict[str, float]) -> dict[str, float]:
    """등급 가중평균 — sourcing 에서 파생되는 품목 단가."""
    w = sum(GRADE_SPLIT[g] * GRADE_MULT[g] for g in GRADE_SPLIT)
    return {i: p * w for i, p in price.items()}


def make_scenarios(
    cover_days=(2, 5, 12), price=PRICE_BASE, splits=None, as_of: date = AS_OF
) -> list[MinimalScenario]:
    """충환님 A안: 매입수량 = 확정수요 × 커버일수 D, D ∈ [2, 18]."""
    labels = {2: "보수", 5: "기준", 12: "공격"}
    wp = _weighted_price(price)
    out = []
    for d in cover_days:
        qty = {i: round(DEMAND[i] * d, 1) for i in FIXTURE_ITEMS}
        legs = (splits or {}).get(d)
        if legs is None:
            legs = (SplitLeg(0, dict(qty), as_of + timedelta(days=2)),)
        out.append(
            MinimalScenario(
                scenario_id=f"SCN-D{d:02d}",
                strategy_type="quantity",
                stance=labels.get(d, f"D{d}"),
                qty_kg=qty,
                unit_price_krw_per_kg=wp,
                split_plan=legs,
                sourcing_plan=_sourcing(qty, price),
                price_basis="AUCTION",
                rationale=f"커버일수 D={d}",
            )
        )
    return out


def make_split_variants(d: int = 6, price=PRICE_BASE) -> list[MinimalScenario]:
    """총량 동일 · 분할 상이 2안 — B2 검증용."""
    wp = _weighted_price(price)
    qty = {i: round(DEMAND[i] * d, 1) for i in FIXTURE_ITEMS}
    half = {i: round(v / 2, 1) for i, v in qty.items()}
    variants = [
        ("SCN-T1", "기준", (SplitLeg(0, dict(qty), AS_OF + timedelta(days=2)),)),
        (
            "SCN-T2",
            "보수",
            (
                SplitLeg(0, dict(half), AS_OF + timedelta(days=2)),
                SplitLeg(3, dict(half), AS_OF + timedelta(days=5)),
            ),
        ),
    ]
    return [
        MinimalScenario(
            sid, "timing", st, qty, wp, legs, _sourcing(qty, price), "AUCTION", f"D={d} / {st}"
        )
        for sid, st, legs in variants
    ]


# ---------------------------------------------------------------------------
# 부서 회신 빌더
# ---------------------------------------------------------------------------


def sales_reply(floor_mult: float = 1.0, as_of: date = AS_OF) -> T2Reply:
    floor = {i: round(DEMAND[i] * floor_mult, 1) for i in FIXTURE_ITEMS}
    return T2Reply(
        "sales",
        as_of,
        (
            CheckResult(
                "check_confirmed_demand_total",
                "sales",
                "ok",
                "hard",
                "확정수요",
                (_ev("확정수요 총량", "SRC-SALES-001", sum(floor.values()), "kg"),),
                floor_kg=floor,
                source_ref="persona.거래처.daily_demand_kg",
            ),
        ),
        reasoning="김치공장 확정주문 기준 최소 매입량",
    )


def inventory_reply(cap_total: float = 17_600.0, as_of: date = AS_OF) -> T2Reply:
    return T2Reply(
        "inventory",
        as_of,
        (
            CheckResult(
                "check_warehouse_capacity",
                "inventory",
                "conditional",
                "hard",
                "버스트 상한 22 PLT",
                (
                    _ev(
                        "버스트 상한", "SRC-WH-001", cap_total, "kg", "SIM_FIXED", "5회차 승인 예정"
                    ),
                ),
                cap_total_kg=cap_total,
                evidence_grade="SIM_FIXED",
                source_ref="persona.창고.burst_capacity_plt",
            ),
        ),
        reasoning="내일 입고 반영 실질 가용",
    )


def finance_reply(cap_amount: float, as_of: date = AS_OF) -> T2Reply:
    return T2Reply(
        "finance",
        as_of,
        (
            CheckResult(
                "check_projected_cash_min",
                "finance",
                "conditional",
                "hard",
                "가용자금",
                (_ev("가용자금", "SRC-FIN-001", cap_amount, "krw", "OFFICIAL"),),
                cap_amount_krw=cap_amount,
                evidence_grade="OFFICIAL",
                source_ref="finance_account.cash_balance",
            ),
        ),
        reasoning="급여·원리금 차감 후 매입 가능액",
    )


def margin_warning(as_of: date = AS_OF) -> CheckResult:
    """N8 — 하드(납기)와 반대 방향으로 동시에 뜨는 소프트 경고."""
    return CheckResult(
        "warn_margin_floor",
        "sales",
        "conditional",
        "soft",
        "배추 1,800원/kg > 역마진 임계 1,622원/kg (거치)",
        (_ev("역마진 임계", "SRC-CM-배추", 1622.0, "krw_per_kg"),),
        source_ref="persona.손익분기CM",
    )


def _std_replies(cap_total=17_600.0, cap_amount=30_000_000.0):
    return {
        "sales": sales_reply(),
        "inventory": inventory_reply(cap_total),
        "finance": finance_reply(cap_amount),
    }


# ---------------------------------------------------------------------------
# 6케이스
# ---------------------------------------------------------------------------

CASES: dict[str, dict[str, Any]] = {
    "NORMAL": {
        "snapshot": make_snapshot(),
        "scenarios": make_scenarios(),
        "replies": _std_replies(),
        "expect": "정상 — 밴드 안, 2안 이상 생존, Critic FAIL 회송 1회",
    },
    "DEADLOCK_CASH": {
        "snapshot": make_snapshot(cash=3_000_000.0),
        "scenarios": make_scenarios(),
        "replies": _std_replies(cap_amount=500_000.0),
        "expect": "DEADLOCK_CASH — 납기는 있는데 살 돈이 없다. T1 회송 금지, LLM 0회.",
    },
    "VARIANT_COLLAPSED": {
        "snapshot": make_snapshot(),
        "scenarios": make_scenarios(),
        "replies": _std_replies(cap_total=1_000.0),
        "expect": "붕괴 + 구조적 협소(여유 9.2%) → 회송 생략(B5), 단일안 + [보류] 폴백",
    },
    "ADVERSE_MARGIN": {
        "snapshot": make_snapshot(),
        "scenarios": make_scenarios(price=PRICE_HIGH),
        "replies": {
            "sales": T2Reply(
                "sales",
                AS_OF,
                sales_reply().checks + (margin_warning(),),
                reasoning="납기는 지켜야 하나 역마진 구간",
            ),
            "inventory": inventory_reply(),
            "finance": finance_reply(30_000_000.0),
        },
        "expect": "N8 — 하드 통과 + 소프트 경고 동시. 자동 해결 금지, H1 3안.",
    },
    # ── v0.2 신설 ────────────────────────────────────────────────
    "IDENTITY_KEPT": {
        "snapshot": make_snapshot(),
        "scenarios": make_scenarios(),
        "replies": _std_replies(cap_total=4_000.0),
        "expect": "B1 — 클리핑되어도 삼중 일치가 유지되고 Critic 이 PASS 해야 한다",
    },
    "SPLIT_DIVERSITY": {
        "snapshot": make_snapshot(),
        "scenarios": make_split_variants(),
        "replies": _std_replies(),
        "expect": "B2 — 총량 동일 + 분할 상이는 붕괴가 아니다 (collapsed=False)",
    },
}
