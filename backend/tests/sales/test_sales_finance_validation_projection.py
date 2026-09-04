"""Sales Scenario → 재무 검증 요청 projection.

★ 이 파일이 지키는 것은 **모르는 값을 만들지 않는 것**이다.
    · Sales 가 권위 있게 아는 값만 옮긴다
    · 소유자가 없는 항목(payment_terms_type · source_ref)은 unresolved 로 남는다
    · evidence_refs[0] 을 source_ref 로 고르지 않는다
    · delivery_date 를 회수일 기준점으로 단정하지 않는다
    · 0 과 NULL 을 가른다
    · Sales 가 Finance 를 직접 부르지 않는다
"""

from datetime import date
from decimal import Decimal

from app.sales.finance_validation import (
    OWNED_BUT_OPTIONAL_FINANCE_FIELDS,
    build_financial_validation_batch,
    build_financial_validation_request,
)
from app.sales.schemas import SalesScenario, ScenarioSupply


def _scenario(**over) -> SalesScenario:
    base = {
        "scenario_id": "SALES-001-A",
        "scenario_type": "BALANCED",
        "objective": "BALANCE",
        "business_mode": "SPOT_SALES",
        "item": "red_pepper",
        "partner_id": "P-100",
        "quantity_kg": Decimal(100),
        "unit_price_krw": Decimal(10000),
        "sales_amount_krw": Decimal(1000000),
        "payment_days": 30,
        "delivery_date": date(2026, 1, 5),
        "supply": ScenarioSupply(confirmed_quantity_kg=Decimal(100)),
        "evidence_refs": ["EV-1", "EV-2"],
    }
    base.update(over)
    return SalesScenario(**base)


# ---------------------------------------------------------------------------
# 아는 값만 옮긴다
# ---------------------------------------------------------------------------


def test_known_facts_are_projected_under_the_finance_names():
    payload, _ = build_financial_validation_request(_scenario())

    assert payload["scenario_id"] == "SALES-001-A"
    assert payload["partner_id"] == "P-100"
    assert payload["item"] == "red_pepper"
    assert payload["quantity_kg"] == "100"
    assert payload["unit_price_krw"] == "10000"
    assert payload["payment_days"] == 30


def test_sales_amount_is_renamed_not_recalculated():
    # Sales 가 이미 소유한 금액을 그대로 옮긴다. 재무가 재계산해 대조하는 값이다.
    payload, _ = build_financial_validation_request(
        _scenario(quantity_kg=Decimal(3), unit_price_krw=Decimal(7), sales_amount_krw=Decimal(21))
    )

    assert payload["reported_sales_amount_krw"] == "21"


def test_projection_does_not_recompute_a_mismatched_amount():
    """금액이 어긋나 있어도 Sales 가 고쳐서 넘기지 않는다 — 대조는 재무 몫이다."""
    payload, _ = build_financial_validation_request(
        _scenario(quantity_kg=Decimal(100), unit_price_krw=Decimal(10000),
                  sales_amount_krw=Decimal(999))
    )

    assert payload["reported_sales_amount_krw"] == "999"


def test_decimal_precision_is_not_lost_to_float():
    payload, _ = build_financial_validation_request(
        _scenario(unit_price_krw=Decimal("10000.123456"))
    )

    assert payload["unit_price_krw"] == "10000.123456"


# ---------------------------------------------------------------------------
# 소유자 없는 항목 — 발명 금지
# ---------------------------------------------------------------------------


def test_payment_terms_type_is_never_invented():
    """🔴 `payment_days` 가 있다고 SINGLE 로 단정하지 않는다."""
    payload, unresolved = build_financial_validation_request(_scenario(payment_days=30))

    assert "payment_terms_type" not in payload
    assert "payment_terms_type" in unresolved


def test_source_ref_is_not_taken_from_the_first_evidence_ref():
    """🔴 배열 첫 번째를 고르는 것은 근거가 아니라 우연이다."""
    payload, unresolved = build_financial_validation_request(
        _scenario(evidence_refs=["EV-1", "EV-2", "EV-3"])
    )

    assert "source_ref" not in payload
    assert "source_ref" in unresolved
    assert "EV-1" not in payload.values()


def test_both_unowned_fields_are_always_reported():
    _, unresolved = build_financial_validation_request(_scenario())

    for field in OWNED_BUT_OPTIONAL_FINANCE_FIELDS:
        assert field in unresolved, field


def test_collection_reference_date_is_not_guessed_from_delivery_date():
    payload, _ = build_financial_validation_request(_scenario(delivery_date=date(2026, 1, 5)))

    # 회수일 기준점이 납품일인지 아직 정해지지 않았다.
    assert "collection_reference_date" not in payload


def test_collection_reference_date_is_not_filled_with_a_date_at_all():
    payload, _ = build_financial_validation_request(_scenario())

    assert not any(isinstance(value, date) for value in payload.values())


# ---------------------------------------------------------------------------
# 0 != NULL
# ---------------------------------------------------------------------------


def test_zero_quantity_is_projected_as_a_value():
    payload, unresolved = build_financial_validation_request(
        _scenario(quantity_kg=Decimal(0), sales_amount_krw=Decimal(0))
    )

    assert payload["quantity_kg"] == "0"
    assert payload["reported_sales_amount_krw"] == "0"
    assert "quantity_kg" not in unresolved


def test_null_quantity_is_reported_as_unresolved_not_zero():
    payload, unresolved = build_financial_validation_request(_scenario(quantity_kg=None))

    assert "quantity_kg" not in payload
    assert "quantity_kg" in unresolved


def test_null_payment_days_is_not_projected_as_zero():
    payload, _ = build_financial_validation_request(_scenario(payment_days=None))

    assert "payment_days" not in payload


def test_zero_payment_days_is_projected():
    payload, _ = build_financial_validation_request(_scenario(payment_days=0))

    assert payload["payment_days"] == 0


def test_missing_partner_is_unresolved_not_blank():
    payload, unresolved = build_financial_validation_request(_scenario(partner_id=None))

    assert "partner_id" not in payload
    assert "partner_id" in unresolved


# ---------------------------------------------------------------------------
# 공급 — 조건부를 확정처럼 넘기지 않는다
# ---------------------------------------------------------------------------


def test_confirmed_supply_is_projected():
    payload, _ = build_financial_validation_request(
        _scenario(supply=ScenarioSupply(confirmed_quantity_kg=Decimal(80)))
    )

    assert payload["supply"] == {"confirmed_quantity_kg": "80"}


def test_required_additional_quantity_is_not_sent_as_conditional_supply():
    """🔴 '더 필요한 양' 과 '조건부로 확보 가능한 양' 은 다른 사실이다."""
    payload, _ = build_financial_validation_request(
        _scenario(
            supply=ScenarioSupply(
                confirmed_quantity_kg=Decimal(60),
                required_additional_quantity_kg=Decimal(40),
                additional_supply_required=True,
            )
        )
    )

    # 확보되지 않은 수량을 확보 가능한 것처럼 넘기지 않는다.
    assert payload["supply"] == {"confirmed_quantity_kg": "60"}
    assert "conditional_quantity_kg" not in payload["supply"]
    assert "40" not in str(payload)


def test_unknown_conditional_supply_is_left_absent_not_zero():
    """조건부 칸을 비워 두면 재무가 '모름' 으로 읽고 fail closed 한다.

    0 을 넣으면 *모르는 것*이 *조건부 물량 없음*이라는 사실로 바뀌어, 확정 재고원가가
    제안 전체를 덮는 것을 막는 방어가 풀린다.
    """
    payload, _ = build_financial_validation_request(
        _scenario(
            supply=ScenarioSupply(
                confirmed_quantity_kg=Decimal(60), additional_supply_required=True
            )
        )
    )

    assert "conditional_quantity_kg" not in payload["supply"]


def test_confirmed_quantity_survives_even_when_more_supply_is_needed():
    """🔴 예전에는 추가 공급이 필요하면 supply 블록을 통째로 빼서 확정 물량까지 잃었다."""
    payload, _ = build_financial_validation_request(
        _scenario(
            supply=ScenarioSupply(
                confirmed_quantity_kg=Decimal(60),
                required_additional_quantity_kg=Decimal(40),
                additional_supply_required=True,
            )
        )
    )

    assert payload["supply"]["confirmed_quantity_kg"] == "60"


def test_zero_confirmed_supply_is_a_value_not_absence():
    payload, _ = build_financial_validation_request(
        _scenario(supply=ScenarioSupply(confirmed_quantity_kg=Decimal(0)))
    )

    assert payload["supply"] == {"confirmed_quantity_kg": "0"}


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def test_batch_keeps_every_scenario_and_its_identity():
    scenarios = [
        _scenario(scenario_id="SALES-001-A"),
        _scenario(scenario_id="SALES-001-B", quantity_kg=Decimal(250)),
    ]

    payload, _ = build_financial_validation_batch(scenarios)

    assert [item["scenario_id"] for item in payload["scenarios"]] == [
        "SALES-001-A",
        "SALES-001-B",
    ]
    assert payload["scenarios"][1]["quantity_kg"] == "250"


def test_batch_does_not_silently_truncate_scenarios():
    scenarios = [_scenario(scenario_id=f"SALES-001-{n}") for n in "ABCD"]

    payload, _ = build_financial_validation_batch(scenarios)

    # 사용자가 본 안과 검증에 나가는 안이 달라지면 안 된다. 개수 판정은 재무 몫이다.
    assert len(payload["scenarios"]) == 4


def test_batch_unresolved_fields_are_deduplicated():
    payload, unresolved = build_financial_validation_batch(
        [_scenario(scenario_id="A"), _scenario(scenario_id="B")]
    )
    del payload

    assert unresolved.count("payment_terms_type") == 1


# ---------------------------------------------------------------------------
# Agent 경계
# ---------------------------------------------------------------------------


def _imported_modules(module) -> set[str]:
    """실제 import 문만 본다 — 설명 문장에 이름이 나온 것과 구분한다."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_sales_projection_does_not_import_finance():
    """Sales 는 Finance 를 직접 부르지 않는다 — 마스터가 중개한다."""
    import app.sales.finance_validation as module

    imported = _imported_modules(module)

    assert not any(name.startswith("app.finance") for name in imported), imported
    # 다른 도메인 실행 계층도 마찬가지다.
    for domain in ("app.purchase_agent", "app.logistics", "app.master"):
        assert not any(name.startswith(domain) for name in imported), domain


# ---------------------------------------------------------------------------
# 계약 확정 이후 — 소유가 정해진 값은 실제로 넘어간다
# ---------------------------------------------------------------------------


def test_payment_terms_type_is_projected_when_sales_owns_it():
    payload, unresolved = build_financial_validation_request(
        _scenario(payment_terms_type="INSTALLMENT")
    )

    assert payload["payment_terms_type"] == "INSTALLMENT"
    assert "payment_terms_type" not in unresolved


def test_source_ref_is_projected_when_sales_owns_it():
    payload, unresolved = build_financial_validation_request(
        _scenario(source_ref="CONTRACT:C-1")
    )

    assert payload["source_ref"] == "CONTRACT:C-1"
    assert "source_ref" not in unresolved


def test_owned_but_absent_values_stay_unresolved_not_invented():
    """소유자가 정해졌다고 값이 늘 있는 것은 아니다 — 없으면 없는 채로 보고한다."""
    payload, unresolved = build_financial_validation_request(
        _scenario(payment_terms_type=None, source_ref=None)
    )

    assert "payment_terms_type" not in payload
    assert "source_ref" not in payload
    assert "payment_terms_type" in unresolved
    assert "source_ref" in unresolved


def test_conditional_supply_is_projected_with_its_dependency_ref():
    payload, _ = build_financial_validation_request(
        _scenario(
            supply=ScenarioSupply(
                confirmed_quantity_kg=Decimal(3000),
                conditional_quantity_kg=Decimal(1500),
                dependency_ref="PUR-1",
            )
        )
    )

    assert payload["supply"] == {
        "confirmed_quantity_kg": "3000",
        "conditional_quantity_kg": "1500",
        "dependency_ref": "PUR-1",
    }


def test_explicit_zero_conditional_supply_is_projected_as_zero():
    payload, _ = build_financial_validation_request(
        _scenario(
            supply=ScenarioSupply(
                confirmed_quantity_kg=Decimal(3000),
                conditional_quantity_kg=Decimal(0),
                dependency_ref="PUR-1",
            )
        )
    )

    # 0 은 사실이다 — 생략하면 '모름' 이 되어 뜻이 달라진다.
    assert payload["supply"]["conditional_quantity_kg"] == "0"


def test_unknown_confirmed_supply_omits_the_whole_block():
    payload, _ = build_financial_validation_request(
        _scenario(supply=ScenarioSupply(confirmed_quantity_kg=None))
    )

    # 재무는 supply 부재를 '모름' 으로 읽고 fail closed 한다.
    assert "supply" not in payload
