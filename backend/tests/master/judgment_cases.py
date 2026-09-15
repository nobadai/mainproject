"""판정 검증 라벨셋 — **"이 상황에서 그 주장이 맞았는가"** 의 정답 묶음.

멘토 지적(2026-08-28)에 대한 답이다.

> *"매입 에이전트가 구매해야 한다는 주장이 있는데 **그 주장이 맞는가** 에 대한 검증이
> 중요하다. 날씨·비용·물량 등의 **비교 검증셋**이 중요하다."*
> *"어떤 LLM 이든 검증하기 위한 **데이터 라벨링과 그 이유가 명확한지**"*

★ **지금까지의 검증과 종류가 다르다.**

```text
Critic 56검사 · 마스터 14검사   숫자가 맞나 · 근거가 붙었나 · 계약을 지켰나
이 라벨셋                      그래서 그 결론이 맞나
```

앞은 **형식**을 보고 뒤는 **내용**을 본다. 형식이 완벽한데 결론이 틀릴 수 있다 —
근거를 다 달고 창고보다 많이 사는 안이 그렇다.

★ **정답이 규칙으로 명확한 것만 라벨한다.** 창고·자금·수요 상한은 넘으면 **무조건**
  틀리다. 반면 *"오를 것 같으니 앞당겨 살까"* 는 **판단이지 정답이 아니다** —
  라벨을 붙이면 내 취향이 정답이 된다. 안 붙인 것은 §미라벨에 이유와 함께 적는다.

★ **`why` 가 필수 필드인 이유가 여기 있다.** 멘토가 *"이유가 명확한지"* 를 함께 물었다.
  정답만 있고 근거가 없으면 나중에 그 라벨이 맞는지 아무도 검증하지 못한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

AS_OF = date(2025, 12, 31)

#: 실측에서 뜬 값을 기준선으로 굳혔다 (2025-12-31 · 재무·물류 실 DB).
#: **테스트가 DB 를 타지 않게** 상수로 둔다 — 상황을 바꾸는 것이 목적이지 오늘 값을
#: 읽는 것이 목적이 아니다.
BASE_FINANCE: dict[str, Any] = {
    "as_of": AS_OF.isoformat(),
    "state_date": AS_OF.isoformat(),
    "available_cash": 31_993_913.77,
    "base_projected_cash_min": 31_993_913.77,
    "finance_cap_amount_krw": 31_854_627.0,
    "purchase_payment_days": 7,
    "critical_payment_dates": ["2026-01-10"],
    "margin_defense_floor_rate": 0.267,
    "payment_pressure": "LOW",
    "policy_version_used": "v1.3-PROVISIONAL",
}

BASE_INVENTORY: dict[str, Any] = {
    "as_of": AS_OF.isoformat(),
    "warehouse_free_kg": 7636,
    "rental_cap_kg": 0,
    "guaranteed_capacity_kg": 8000,
    "used_capacity_kg": 363,
    "inbound_lead_days": 2,
    "daily_inbound_capacity_kg": 5000,
    "inbound_transport_capacity_kg": 5000,
    "cap_by_date": {(AS_OF + timedelta(days=n)).isoformat(): 7636 for n in range(2, 20)},
    "lots": [
        {
            "lot_id": "LOT-BASE-BAECHU",
            "item": "배추",
            "available_qty_kg": 286,
            "remaining_freshness_days": 10,
            "grade": None,
            "status": "ACTIVE",
        }
    ],
    "policy_version_used": "v1.3-PROVISIONAL",
}


def forecast(price: int = 1650, *, drift: int = 0) -> dict[str, Any]:
    """18일치 예측. `drift` 로 하루당 변화를 준다 (양수면 상승장)."""
    return {
        "generated_at": f"{AS_OF.isoformat()}T06:00:00+09:00",
        "item": "배추",
        "unit": "원/kg",
        "current_price": price,
        "horizon_days": 18,
        "model_version": "label-fixture",
        "daily": [
            {
                "date": (AS_OF + timedelta(days=n)).isoformat(),
                "predicted": price + drift * n,
                "lower": price + drift * n - 50,
                "upper": price + drift * n + 50,
            }
            for n in range(1, 19)
        ],
    }


def orders(total_kg: float, *, days: tuple[int, ...] = (3, 8)) -> dict[str, Any]:
    per = round(total_kg / len(days), 1) if days else 0
    return {
        "as_of": AS_OF.isoformat(),
        "item": "배추",
        "orders": [
            {"sale_id": n, "qty_kg": per, "due_date": (AS_OF + timedelta(days=d)).isoformat()}
            for n, d in enumerate(days, start=1)
        ],
        "total_kg": total_kg,
    }


POLICY: dict[str, Any] = {"item_mix_ratio": {"배추": 0.76, "무": 0.16, "양파": 0.08}}


@dataclass(frozen=True)
class Outcome:
    """채점기가 보는 것. **Flow 결과에서 뽑은 사실만** 담는다."""

    end_code: str
    labels: tuple[str, ...]
    total_qty_kg: tuple[int, ...]
    amounts_krw: tuple[int, ...]

    @property
    def max_qty(self) -> int:
        return max(self.total_qty_kg, default=0)

    @property
    def max_amount(self) -> int:
        return max(self.amounts_krw, default=0)


@dataclass(frozen=True)
class JudgmentCase:
    """정답이 붙은 상황 하나."""

    name: str
    #: 🔴 **왜 이것이 정답인가.** 멘토가 *"이유가 명확한지"* 를 함께 물었다 —
    #: 근거 없는 라벨은 나중에 그 라벨이 맞는지 아무도 검증하지 못한다.
    why: str
    #: 이 라벨이 무엇을 재는가 (`물량` · `자금` · `수요` …). 멘토의 "비교 검증셋" 축.
    axis: str
    check: Callable[[Outcome], str | None]
    finance: Mapping[str, Any] = field(default_factory=dict)
    inventory: Mapping[str, Any] = field(default_factory=dict)
    confirmed_orders: Mapping[str, Any] | None = None
    forecast_override: Mapping[str, Any] | None = None


# ── 채점식 ──────────────────────────────────────────────────────────────


def no_plan_over(limit_kg: int):
    """상한을 넘지 않는다. **안이 없어도 통과한다** — 상한 0 인 경우가 그렇다."""

    def check(out: Outcome) -> str | None:
        if out.max_qty > limit_kg:
            return f"상한 {limit_kg:,}kg 인데 {out.max_qty:,}kg 를 제안했다"
        return None

    return check


def clipped_to(limit_kg: int):
    """🔴 **안이 나오면서** 상한 이하여야 한다.

    `no_plan_over` 만 쓰면 **안이 하나도 없을 때 자동으로 통과**한다 — 상한을 넘을
    기회가 없었을 뿐인데 *"상한을 지켰다"* 로 채점된다. 라벨셋이 공허해지는 가장
    흔한 길이라, **클리핑이 실제로 일어났는지**를 함께 요구한다.
    """

    def check(out: Outcome) -> str | None:
        if not out.labels:
            return "안이 하나도 없다 — 상한을 지킨 것이 아니라 지킬 기회가 없었다"
        if out.max_qty > limit_kg:
            return f"상한 {limit_kg:,}kg 인데 {out.max_qty:,}kg 를 제안했다"
        return None

    return check


def no_spend_over(limit_krw: int):
    def check(out: Outcome) -> str | None:
        if out.max_amount > limit_krw:
            return f"상한 {limit_krw:,}원 인데 {out.max_amount:,}원 를 제안했다"
        return None

    return check


def must_propose(out: Outcome) -> str | None:
    if out.end_code != "E1_APPROVED" or not out.labels:
        return f"막는 제약이 없는데 제안이 없다 ({out.end_code})"
    return None


# ── 라벨셋 ──────────────────────────────────────────────────────────────

CASES: tuple[JudgmentCase, ...] = (
    JudgmentCase(
        name="창고 여유가 0이면 살 곳이 없다",
        axis="물량",
        why=(
            "들일 곳이 없는데 사면 **갈 곳 없는 물건**이 생긴다. 창고 여유와 임차 상한이 "
            "둘 다 0 이므로 어떤 양도 들일 수 없다 — 이건 판단이 아니라 산술이다."
        ),
        inventory={
            "warehouse_free_kg": 0,
            "rental_cap_kg": 0,
            "cap_by_date": dict.fromkeys(BASE_INVENTORY["cap_by_date"], 0),
        },
        check=no_plan_over(0),
    ),
    JudgmentCase(
        name="자금 상한이 0이면 살 돈이 없다",
        axis="자금",
        why=(
            "지급 능력을 넘는 매입은 **실행할 수 없는 계획**이다. 재무가 낸 cap 이 0 이면 "
            "금액이 붙는 어떤 안도 성립하지 않는다."
        ),
        finance={"finance_cap_amount_krw": 0},
        check=no_spend_over(0),
    ),
    JudgmentCase(
        name="창고 여유가 수요보다 작으면 창고가 상한이다",
        axis="물량",
        why=(
            "수요가 10,000kg 라도 창고가 500kg 밖에 없으면 **살 수 있는 양은 500kg** 이다. "
            "수요를 따라가면 넘치고, 넘친 물건은 어디에도 못 둔다. "
            "이 케이스가 잡는 것은 *'수요를 근거로 창고를 넘는'* 실수다."
        ),
        inventory={
            "warehouse_free_kg": 500,
            "rental_cap_kg": 0,
            "cap_by_date": dict.fromkeys(BASE_INVENTORY["cap_by_date"], 500),
        },
        confirmed_orders=orders(10_000),
        check=clipped_to(500),
    ),
    JudgmentCase(
        name="확정 주문이 0이면 팔 곳이 없다",
        axis="수요",
        why=(
            "수요 없는 매입은 **재고로 남는다.** 확정 주문 총량이 0 이면 일평균 수요가 0 "
            "이고, 수요에서 파생되는 어떤 수량도 0 이어야 한다. "
            "🔶 다만 *'전략적 선매입'* 은 이 라벨의 대상이 아니다 — 1차 범위에 그 근거가 "
            "없으므로 0 을 정답으로 둔다."
        ),
        confirmed_orders=orders(0, days=()),
        check=no_plan_over(0),
    ),
    JudgmentCase(
        name="막는 제약이 없으면 제안이 나와야 한다",
        axis="과보수",
        why=(
            "창고·자금·수요 어느 것도 안 걸리는데 안이 없으면 **과보수**다. "
            "*'아무것도 안 산다'* 는 안전해 보이지만 그것도 틀린 판단일 수 있다 — "
            "이 라벨이 없으면 **전부 거절하는 시스템이 만점**을 받는다."
        ),
        inventory={
            "warehouse_free_kg": 7000,
            "cap_by_date": dict.fromkeys(BASE_INVENTORY["cap_by_date"], 7000),
        },
        confirmed_orders=orders(5_000),
        check=must_propose,
    ),
)


# ── 미라벨 — **정답을 못 붙인 것** ───────────────────────────────────────

UNLABELED: tuple[tuple[str, str], ...] = (
    (
        "날씨",
        (
            "데이터가 없다. 기상 소스가 붙어 있지 않아 *'비가 와서 출하가 줄 것'* 을 "
            "사실로 쓸 수 없다. **없는 것을 라벨하면 그 라벨이 거짓이 된다.**"
        ),
    ),
    (
        "가격 타이밍 (비용)",
        (
            "*'오를 것 같으니 앞당겨 산다'* 는 **판단이지 정답이 아니다.** 예측 구간이 "
            "넓을수록 어느 쪽도 틀렸다고 할 수 없고, 라벨을 붙이면 **내 취향이 정답**이 "
            "된다. 사후 실측(그날 이후 실제 가격)과 대조해야 정답이 생기는데, "
            "재무·물류 상태가 2025-12 한 달치뿐이라 백테스트를 돌릴 구간이 없다."
        ),
    ),
    (
        "마진",
        (
            "계약 단가(`contract_price`)가 DB 에 없다. 마진을 계산할 수 없으므로 "
            "*'남는 장사인가'* 를 라벨할 수 없다 (`inputs.load_policy_values` 참조)."
        ),
    ),
)
