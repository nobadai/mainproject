"""Finance Sales Core Phase 2 — 판매 원가 기준 합성.

★ 이 파일이 지키는 것은 **중복 계상 차단과 근거 계보**다.
    · 재고원가에 이미 포함된 직접비는 두 번 더해지지 않는다
    · 권위 있는 재고원가가 없으면 0이 아니라 "계산할 수 없음"이다
    · 같은 구성요소가 두 번 오면 조용히 하나를 고르지 않고 거절한다
    · 더해진 모든 숫자는 source_ref 를 남긴다
  어느 재고/매입 원가가 정본인지는 아직 계약이 없다 — 여기서 고르지 않는다.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.finance.sales_models import InventoryCostBasis, VerifiedDirectCost
from app.finance.tools import build_sales_calculation_facts, compose_sales_cost_basis


def _inventory(
    amount: str = "700000",
    *,
    included: tuple[str, ...] = (),
    source_ref: str = "INV-LOT:L-001",
    grade: str = "OFFICIAL",
    method: str = "ACTUAL",
) -> InventoryCostBasis:
    return InventoryCostBasis(
        amount_krw=Decimal(amount),
        cost_method=method,
        included_components=included,
        source_ref=source_ref,
        evidence_grade=grade,
    )


def _direct(
    component: str,
    amount: str,
    *,
    source_ref: str | None = None,
    grade: str = "OFFICIAL",
    method: str = "ACTUAL",
) -> VerifiedDirectCost:
    return VerifiedDirectCost(
        component=component,
        amount_krw=Decimal(amount),
        cost_method=method,
        source_ref=source_ref or f"COST:{component}",
        evidence_grade=grade,
    )


# ---------------------------------------------------------------------------
# 합성
# ---------------------------------------------------------------------------


def test_inventory_basis_alone_is_the_cost_basis():
    basis = compose_sales_cost_basis(inventory_cost_basis=_inventory("700000"))

    assert basis is not None
    assert basis.amount_krw == Decimal(700000)
    assert basis.inventory_amount_krw == Decimal(700000)
    assert basis.added_direct_costs == ()
    assert basis.source_refs == ("INV-LOT:L-001",)


def test_one_verified_direct_cost_is_added():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000"),
        direct_costs=[_direct("outbound_transport", "23000")],
    )

    assert basis is not None
    assert basis.amount_krw == Decimal(723000)
    assert basis.included_components == ("outbound_transport",)


def test_multiple_verified_direct_costs_are_all_added():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000"),
        direct_costs=[
            _direct("outbound_transport", "23000"),
            _direct("outbound_handling", "4500"),
        ],
    )

    assert basis is not None
    assert basis.amount_krw == Decimal(727500)
    assert len(basis.added_direct_costs) == 2


def test_direct_cost_already_inside_inventory_basis_is_not_counted_twice():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000", included=("outbound_transport",)),
        direct_costs=[
            _direct("outbound_transport", "23000"),
            _direct("outbound_handling", "4500"),
        ],
    )

    assert basis is not None
    # 운송비는 이미 재고원가 안에 있다 — 더하지 않는다.
    assert basis.amount_krw == Decimal(704500)
    assert basis.already_included_components == ("outbound_transport",)
    assert [cost.component for cost in basis.added_direct_costs] == ["outbound_handling"]
    # 그리고 그 사실이 조용히 사라지지 않는다.
    assert basis.included_components == ("outbound_transport", "outbound_handling")


def test_zero_valued_direct_cost_is_preserved_as_zero_not_dropped():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000"),
        direct_costs=[_direct("outbound_handling", "0")],
    )

    assert basis is not None
    assert basis.amount_krw == Decimal(700000)
    # 0원은 "없음"이 아니다 — 검증된 0으로 계보에 남는다.
    assert basis.added_direct_costs[0].amount_krw == Decimal(0)
    assert "COST:outbound_handling" in basis.source_refs


def test_zero_inventory_basis_is_a_value_not_a_missing_input():
    basis = compose_sales_cost_basis(inventory_cost_basis=_inventory("0"))

    assert basis is not None
    assert basis.amount_krw == Decimal(0)


# ---------------------------------------------------------------------------
# 입력 방어
# ---------------------------------------------------------------------------


def test_negative_inventory_amount_is_rejected():
    with pytest.raises(ValidationError):
        _inventory("-1")


def test_negative_direct_cost_is_rejected():
    with pytest.raises(ValidationError):
        _direct("outbound_transport", "-1")


def test_boolean_amount_is_rejected_like_other_finance_schemas():
    with pytest.raises(ValidationError):
        InventoryCostBasis(
            amount_krw=True,  # type: ignore[arg-type]
            cost_method="ACTUAL",
            source_ref="INV-LOT:L-001",
            evidence_grade="OFFICIAL",
        )


def test_source_ref_is_required_on_every_cost_component():
    with pytest.raises(ValidationError):
        InventoryCostBasis(
            amount_krw=Decimal(1),
            cost_method="ACTUAL",
            source_ref="",
            evidence_grade="OFFICIAL",
        )


def test_unknown_evidence_grade_is_rejected_instead_of_inventing_vocabulary():
    with pytest.raises(ValidationError):
        _inventory("700000", grade="ACTUAL")


# ---------------------------------------------------------------------------
# 원가 산출방식 — 근거 등급과 다른 축이다
# ---------------------------------------------------------------------------


def test_cost_method_and_evidence_grade_are_independent_axes():
    # 실제원가인데 근거는 공식 — 모순이 아니라 가장 흔한 조합이다.
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000", method="ACTUAL", grade="OFFICIAL")
    )

    assert basis is not None
    assert basis.inventory_cost_method == "ACTUAL"
    assert basis.inventory_evidence_grade == "OFFICIAL"


def test_standard_cost_method_can_carry_a_weaker_evidence_grade():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000", method="STANDARD", grade="ASSUMED")
    )

    assert basis is not None
    assert basis.inventory_cost_method == "STANDARD"
    assert basis.inventory_evidence_grade == "ASSUMED"


def test_evidence_grade_value_is_not_accepted_as_a_cost_method():
    with pytest.raises(ValidationError):
        _inventory("700000", method="OFFICIAL")


def test_unknown_cost_method_is_preserved_and_never_becomes_zero():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000", method="UNKNOWN")
    )

    assert basis is not None
    # 판정(이 값을 써도 되는가)은 Rule 계층 몫이다 — 계산은 사실을 그대로 나른다.
    assert basis.inventory_cost_method == "UNKNOWN"
    assert basis.amount_krw == Decimal(700000)


def test_direct_cost_keeps_its_own_cost_method():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000", method="ACTUAL"),
        direct_costs=[_direct("outbound_transport", "23000", method="STANDARD")],
    )

    assert basis is not None
    assert basis.inventory_cost_method == "ACTUAL"
    assert basis.added_direct_costs[0].cost_method == "STANDARD"


def test_missing_authoritative_inventory_basis_is_not_converted_to_zero():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=None,
        direct_costs=[_direct("outbound_transport", "23000")],
    )

    # 직접비만으로 원가 기준을 만들지 않는다. 0으로도 만들지 않는다.
    assert basis is None


def test_duplicate_direct_cost_component_fails_closed():
    with pytest.raises(ValueError):
        compose_sales_cost_basis(
            inventory_cost_basis=_inventory("700000"),
            direct_costs=[
                _direct("outbound_transport", "23000"),
                _direct("outbound_transport", "31000"),
            ],
        )


def test_duplicate_is_rejected_even_when_inventory_basis_is_missing():
    # 재고원가가 없다는 이유로 잘못된 입력이 조용히 통과하지 않는다.
    with pytest.raises(ValueError):
        compose_sales_cost_basis(
            inventory_cost_basis=None,
            direct_costs=[
                _direct("outbound_transport", "23000"),
                _direct("outbound_transport", "31000"),
            ],
        )


# ---------------------------------------------------------------------------
# 계보 · 정밀도
# ---------------------------------------------------------------------------


def test_every_included_numeric_component_keeps_its_source_ref():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000", source_ref="INV-LOT:L-007"),
        direct_costs=[
            _direct("outbound_transport", "23000", source_ref="DLV:D-11"),
            _direct("outbound_handling", "4500", source_ref="DLV:D-12"),
        ],
    )

    assert basis is not None
    assert basis.source_refs == ("INV-LOT:L-007", "DLV:D-11", "DLV:D-12")
    assert basis.inventory_source_ref == "INV-LOT:L-007"
    assert basis.inventory_evidence_grade == "OFFICIAL"


def test_direct_cost_evidence_grade_survives_composition():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000"),
        direct_costs=[_direct("outbound_transport", "23000", grade="SIM_FIXED")],
    )

    assert basis is not None
    # 등급을 섞거나 낮추지 않는다 — 구성요소마다 그대로 남는다.
    assert basis.added_direct_costs[0].evidence_grade == "SIM_FIXED"


def test_decimal_precision_survives_composition():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000.123456"),
        direct_costs=[_direct("outbound_transport", "23000.654321")],
    )

    assert basis is not None
    assert basis.amount_krw == Decimal("723000.777777")


# ---------------------------------------------------------------------------
# Phase 1 과의 접합 — 원가 기준이 없으면 마진도 없다
# ---------------------------------------------------------------------------


def test_composed_basis_feeds_phase_one_margin_calculation():
    basis = compose_sales_cost_basis(
        inventory_cost_basis=_inventory("700000"),
        direct_costs=[_direct("outbound_transport", "23000")],
    )
    assert basis is not None

    facts = build_sales_calculation_facts(
        quantity_kg=Decimal("120.5"),
        unit_price_krw=Decimal(8000),
        sales_cost_basis_krw=basis.amount_krw,
    )

    assert facts["contribution_margin_krw"] == Decimal(241000)
    assert facts["contribution_margin_rate"] == Decimal("0.25")


def test_absent_basis_leaves_margin_uncomputed_rather_than_zero_cost():
    basis = compose_sales_cost_basis(inventory_cost_basis=None)

    facts = build_sales_calculation_facts(
        quantity_kg=Decimal("120.5"),
        unit_price_krw=Decimal(8000),
        sales_cost_basis_krw=basis.amount_krw if basis is not None else None,
    )

    assert facts["contribution_margin_krw"] is None
    assert facts["contribution_margin_rate"] is None
