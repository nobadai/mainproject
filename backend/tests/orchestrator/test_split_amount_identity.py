"""삼중 일치의 **금액 변** — `total ↔ split` 이 비어 있었다.

`check_triple_identity` 가 v0.3 까지 본 것.

```text
수량:  total == Σ split == Σ sourcing      두 변
금액:  total_amount == Σ sourcing          한 변만
```

`SplitLeg` 에 금액 칸이 없어 `total ↔ split` 의 금액 변을 만들 수가 없었다.

🔴 **그 사이로 원장이 지나간다.** 매입이 회차별 금액을 보내면 마스터가 쓴다.

```text
purchases.total_amount_krw     NOT NULL    회차마다 한 행
purchase_items.line_amount_krw NOT NULL    회차·품목마다 한 줄
```

`Σ 회차금액 ≠ total_amount_krw` 여도 아무도 안 봤다.
재무 cap 검증을 통과한 안이 cap 을 넘는 원장을 만들 수 있다.

★ **금액은 전부 선택 필드다.** 매입이 아직 안 보내므로 오늘은 늘 비어 있고, 그때는
  검사가 통째로 건너뛴다 (`test_금액이_없으면_오늘과_같다`). 값이 오는 날부터 걸린다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.contracts.core import (
    Band,
    MinimalScenario,
    SourcingLot,
    SplitLeg,
    check_triple_identity,
)
from app.orchestrator.band import clip_scenario

AS_OF = date(2025, 12, 31)
D2 = AS_OF + timedelta(days=2)
D5 = AS_OF + timedelta(days=5)

PRICE = {"배추": 1000.0, "무": 500.0}


def _sourcing(qty: dict[str, float]) -> tuple[SourcingLot, ...]:
    """품목당 로트 하나. 단가는 PRICE 그대로 — 금액이 손으로 계산된다."""
    return tuple(
        SourcingLot(item=i, grade="상", qty_kg=q, unit_price_krw_per_kg=PRICE[i])
        for i, q in qty.items()
    )


def _amount(qty: dict[str, float]) -> dict[str, float]:
    return {i: q * PRICE[i] for i, q in qty.items()}


def _scenario(
    qty: dict[str, float],
    legs: tuple[SplitLeg, ...],
    sourcing: tuple[SourcingLot, ...] | None = None,
) -> MinimalScenario:
    return MinimalScenario(
        scenario_id="SCN-AMT",
        strategy_type="quantity",
        stance="기준",
        qty_kg=qty,
        unit_price_krw_per_kg=PRICE,
        split_plan=legs,
        sourcing_plan=_sourcing(qty) if sourcing is None else sourcing,
        rationale="금액 항등식 검사용",
    )


def _band(cap: dict[str, float], cap_total: float = 1e9, cap_amount: float = 1e12) -> Band:
    return Band(
        floor_kg={i: 0.0 for i in cap},
        cap_kg=cap,
        cap_total_kg=cap_total,
        cap_amount_krw=cap_amount,
        contributors={},
    )


# ---------------------------------------------------------------------------
# ① 금액이 없으면 오늘과 같다 — 이 파일에서 제일 중요한 판이다
# ---------------------------------------------------------------------------


def test_금액이_없으면_오늘과_같다() -> None:
    """`SplitLeg.amount_krw` 가 비면 금액 변 셋을 전부 건너뛴다.

    이 PR 이 기존 동작을 하나도 안 바꾼다는 것이 여기서 증명된다.
    """
    qty = {"배추": 100.0, "무": 200.0}
    legs = (SplitLeg(0, dict(qty), D2),)

    problems = check_triple_identity(qty, legs, _sourcing(qty), amount_krw=200_000.0)

    assert problems == []
    assert legs[0].amount_krw is None


def test_금액이_없으면_클리핑도_오늘과_같다() -> None:
    qty = {"배추": 100.0, "무": 200.0}
    result = clip_scenario(
        _scenario(qty, (SplitLeg(0, dict(qty), D2),)),
        _band({"배추": 50.0, "무": 200.0}),
    )

    assert result.identity_problems == ()
    assert all(leg.amount_krw is None for leg in result.clipped_split_plan)


# ---------------------------------------------------------------------------
# ② 총액 변
# ---------------------------------------------------------------------------


def test_총액과_Σsplit_이_어긋나면_잡는다() -> None:
    qty = {"배추": 100.0, "무": 200.0}
    legs = (SplitLeg(0, dict(qty), D2, {"배추": 100_000.0, "무": 50_000.0}),)

    problems = check_triple_identity(qty, legs, (), amount_krw=200_000.0)

    assert any("Σsplit" in p for p in problems), problems


def test_총액과_Σsplit_이_맞으면_통과한다() -> None:
    qty = {"배추": 100.0, "무": 200.0}
    legs = (SplitLeg(0, dict(qty), D2, _amount(qty)),)

    assert check_triple_identity(qty, legs, _sourcing(qty), amount_krw=200_000.0) == []


def test_총액_허용오차는_기존_금액_상수를_쓴다() -> None:
    """새 상수를 만들지 않는다 — IDENTITY_TOL_KRW(1.0) 하나뿐이다."""
    qty = {"배추": 100.0}
    legs = (SplitLeg(0, dict(qty), D2, {"배추": 100_000.5}),)

    assert check_triple_identity(qty, legs, (), amount_krw=100_000.0) == []
    assert check_triple_identity(qty, legs, (), amount_krw=99_998.0) != []


# ---------------------------------------------------------------------------
# ③ 품목별 변
# ---------------------------------------------------------------------------


def test_품목별_Σsplit_과_Σsourcing_이_어긋나면_잡는다() -> None:
    """총액은 맞는데 **품목 사이에서 옮겨 놓은** 경우를 총액 변은 못 본다."""
    qty = {"배추": 100.0, "무": 200.0}
    legs = (SplitLeg(0, dict(qty), D2, {"배추": 120_000.0, "무": 80_000.0}),)

    problems = check_triple_identity(qty, legs, _sourcing(qty), amount_krw=200_000.0)

    assert not any("total" in p for p in problems), problems
    assert any("[배추]" in p and "Σsourcing" in p for p in problems), problems
    assert any("[무]" in p and "Σsourcing" in p for p in problems), problems


def test_sourcing_이_없으면_품목별_변은_돌지_않는다() -> None:
    qty = {"배추": 100.0}
    legs = (SplitLeg(0, dict(qty), D2, {"배추": 100_000.0}),)

    assert check_triple_identity(qty, legs, (), amount_krw=100_000.0) == []


# ---------------------------------------------------------------------------
# ④ 부분 공급
# ---------------------------------------------------------------------------


def test_일부_회차만_금액이면_위반이다() -> None:
    qty = {"배추": 100.0}
    half = {"배추": 50.0}
    legs = (
        SplitLeg(0, dict(half), D2, {"배추": 50_000.0}),
        SplitLeg(3, dict(half), D5),
        SplitLeg(6, {"배추": 0.0}, D5),
    )

    problems = check_triple_identity(qty, legs, _sourcing(qty), amount_krw=100_000.0)

    assert problems == ["회차 금액이 3회차 중 1회차만 실려 있다 — 출처를 섞지 않는다"]


def test_부분_공급이면_총액이_맞아도_잡는다() -> None:
    """실린 회차만 더해 총액과 비교하면 **출처가 섞인 값**으로 판정하게 된다."""
    qty = {"배추": 100.0}
    half = {"배추": 50.0}
    legs = (
        SplitLeg(0, dict(half), D2, {"배추": 100_000.0}),
        SplitLeg(3, dict(half), D5),
    )

    problems = check_triple_identity(qty, legs, (), amount_krw=100_000.0)

    assert len(problems) == 1
    assert "출처를 섞지 않는다" in problems[0]


# ---------------------------------------------------------------------------
# ⑤⑥ 클리핑 — `_scale_split` 이 금액도 줄이는가
# ---------------------------------------------------------------------------


def test_클리핑_뒤에도_금액_항등식이_유지된다() -> None:
    qty = {"배추": 100.0, "무": 200.0}
    legs = (SplitLeg(0, dict(qty), D2, _amount(qty)),)

    result = clip_scenario(_scenario(qty, legs), _band({"배추": 50.0, "무": 100.0}))

    assert result.clipped_qty_kg == {"배추": 50.0, "무": 100.0}
    assert result.identity_problems == ()
    assert result.clipped_split_plan[0].amount_krw == {"배추": 50_000.0, "무": 50_000.0}


@pytest.mark.parametrize("cap", [{"배추": 40.0, "무": 200.0}, {"배추": 100.0, "무": 60.0}])
def test_품목별_배율이_다른_클리핑에서도_유지된다(cap: dict[str, float]) -> None:
    """🔴 **금액을 품목별 매핑으로 둔 이유가 여기서만 드러난다.**

    한 품목만 cap 에 걸리면 배율이 품목마다 다르다. 회차 금액이 스칼라였다면 어느
    배율로 줄여도 품목별 Σsplit 이 Σsourcing 과 어긋난다.
    """
    qty = {"배추": 100.0, "무": 200.0}
    legs = (
        SplitLeg(0, {"배추": 60.0, "무": 50.0}, D2, {"배추": 60_000.0, "무": 25_000.0}),
        SplitLeg(3, {"배추": 40.0, "무": 150.0}, D5, {"배추": 40_000.0, "무": 75_000.0}),
    )

    result = clip_scenario(_scenario(qty, legs), _band(cap))

    assert result.binding_constraints, "클리핑이 안 일어나면 이 판이 아무것도 안 본다"
    assert result.identity_problems == ()


def test_min_lot_되맞춤_뒤에도_금액_항등식이_유지된다() -> None:
    """`_scale_split` 은 `clip_scenario` 안에서 두 번 불린다 — 되맞춤 경로도 본다."""
    qty = {"배추": 100.0}
    legs = (SplitLeg(0, dict(qty), D2, {"배추": 100_000.0}),)
    sourcing = (
        SourcingLot(
            item="배추",
            grade="상",
            qty_kg=100.0,
            unit_price_krw_per_kg=1000.0,
            min_lot_kg=25.0,
        ),
    )

    result = clip_scenario(_scenario(qty, legs, sourcing), _band({"배추": 60.0}))

    assert result.lot_residual_kg > 0, "min_lot 내림이 없으면 되맞춤 경로를 안 탄다"
    assert result.identity_problems == ()
