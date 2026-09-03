"""Sales Scenario → 재무 검증 요청 projection.

이 파일이 하는 것
    **번역뿐이다.** Sales 가 이미 권위 있게 소유한 값을 재무가 읽는 이름으로 옮긴다.

여기 **없는 것**
    새 숫자 · 업무 판단 · 재무 호출
    → 숫자는 `proposal` 이 소유하고, 호출은 마스터가 소유한다.

★ **Sales 는 Finance 를 직접 부르지 않는다.** 이 모듈은 마스터가 그대로 나를 수 있는
  payload 를 만들 뿐이고, `app.finance` 를 import 하지 않는다. 두 Agent 를 실행
  계층에서 붙이면 마스터가 중개할 자리가 사라진다.

★ **모르는 값을 만들지 않는다.** 재무가 요구하지만 Sales 가 권위 있게 알지 못하는
  항목은 payload 에 넣지 않고 `unresolved` 로 돌려준다. 그 항목들은 재무 쪽에서
  `INPUT_INCOMPLETE` 로 드러나야 하고, 여기서 그럴듯한 기본값으로 메우면 그 사실이
  영영 보이지 않게 된다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, NamedTuple

from app.sales.schemas import SalesScenario

#: 재무 판매 검증이 값 없이는 계산을 시작할 수 없는 항목.
#: 재무 내부 계약(`SalesValidationInput`)의 필수 칸과 같은 이름을 쓴다 —
#: 이름을 새로 지으면 받는 쪽이 두 어휘를 맞춰 봐야 한다.
FINANCE_REQUIRED_FIELDS: tuple[str, ...] = (
    "scenario_id",
    "partner_id",
    "item",
    "quantity_kg",
    "unit_price_krw",
    "reported_sales_amount_krw",
    "payment_terms_type",
    "source_ref",
)

#: Sales 가 소유하지만 **값이 없을 수 있는** 재무 필수 항목.
#:
#: ★ 소유권은 정해졌다 (2026-09-03 계약 확정).
#:   · payment_terms_type — 사용자/계약이 말해 준 사실이며 Scenario 가 보유한다.
#:     `payment_days` 가 있다고 SINGLE 을 만들지 않는다.
#:   · source_ref — Scenario 상업조건의 직접 출발점. `evidence_refs[0]` 로 고르지 않는다.
#:
#: 🔴 소유자가 정해졌다고 값이 늘 있는 것은 아니다. 사용자가 말하지 않았거나 마스터가
#:   아직 ref 를 넘기지 않으면 `None` 이고, 그때는 재무가 `INPUT_INCOMPLETE` 로 닫는다.
#:   그것이 정상 동작이다 — 값을 만들어 채우지 않는다.
OWNED_BUT_OPTIONAL_FINANCE_FIELDS: tuple[str, ...] = ("payment_terms_type", "source_ref")


class FinanceValidationProjection(NamedTuple):
    """마스터가 나를 payload 와, Sales 가 채울 수 없던 항목."""

    payload: dict[str, Any]
    unresolved: tuple[str, ...]


def build_financial_validation_request(
    scenario: SalesScenario,
) -> FinanceValidationProjection:
    """Scenario 한 건을 재무 검증 payload 로 옮긴다.

    ★ `sales_amount_krw` → `reported_sales_amount_krw` 는 **이름만 바꾸는 것**이다.
      Sales 가 이미 결정론적으로 계산해 소유한 금액을 재무가 재계산해 대조하라고
      넘긴다 — 여기서 다시 계산하지 않는다.

    ★ `delivery_date` 를 `collection_reference_date` 로 옮기지 않는다. 회수일 기준점이
      납품일인지 송장일인지 계약일인지 아직 정해지지 않았다. 하나를 고르면 그것이
      곧 결정이 된다.
    """
    payload: dict[str, Any] = {"item": scenario.item, "scenario_id": scenario.scenario_id}
    unresolved: list[str] = []

    if scenario.partner_id is not None:
        payload["partner_id"] = scenario.partner_id
    else:
        unresolved.append("partner_id")

    # 0 은 값이고 None 은 모름이다 — 둘을 같은 자리에서 가르지 않는다.
    for source, target in (
        ("quantity_kg", "quantity_kg"),
        ("unit_price_krw", "unit_price_krw"),
        ("sales_amount_krw", "reported_sales_amount_krw"),
    ):
        value = getattr(scenario, source)
        if value is None:
            unresolved.append(target)
        else:
            payload[target] = _plain(value)

    # 결제일수는 재무에서도 선택 항목이다. 없으면 넣지 않는다 — null 을 0 으로 바꾸면
    # "모름" 이 "당일 회수" 가 된다.
    if scenario.payment_days is not None:
        payload["payment_days"] = scenario.payment_days

    # 소유는 Sales 지만 값이 없을 수 있다. 있으면 넘기고 없으면 unresolved 로 남긴다.
    for field in OWNED_BUT_OPTIONAL_FINANCE_FIELDS:
        value = getattr(scenario, field)
        if value is None:
            unresolved.append(field)
        else:
            payload[field] = value

    supply = _supply(scenario)
    if supply is not None:
        payload["supply"] = supply

    return FinanceValidationProjection(payload, tuple(unresolved))


def _supply(scenario: SalesScenario) -> dict[str, Any] | None:
    """확정 공급만 옮기고, 조건부 공급은 **모르는 채로** 둔다.

    ★ `required_additional_quantity_kg` 를 재무의 `conditional_quantity_kg` 로 옮기지
      않는다. 앞의 것은 *더 필요한 양*이고 뒤의 것은 *조건부로 확보 가능하다고 확인된
      양*이다 — 아직 아무도 확보해 주지 않은 수량을 확보 가능한 것처럼 넘기게 된다.

    ★ 조건부 칸을 아예 넣지 않는다. 재무는 그 부재를 "모름" 으로 읽고 확정 재고원가가
      제안 전체를 덮지 못하게 fail closed 한다. 0 을 넣으면 *모르는 것*이 *조건부 물량
      없음*이라는 사실로 바뀌어 그 방어가 풀린다.

    ★ 그래서 추가 공급이 필요할 때도 **확정 물량은 그대로 넘긴다.** 예전처럼 공급
      블록을 통째로 빼면 재무가 아는 확정 수량까지 함께 사라졌다.
    """
    confirmed = scenario.supply.confirmed_quantity_kg
    if confirmed is None:
        # 재무 계약이 확정 수량을 필수로 요구한다. 모르는 채로 블록을 만들 수 없으므로
        # 통째로 생략하고, 재무는 `supply=None` 을 **모름** 으로 읽어 fail closed 한다.
        return None
    supply: dict[str, Any] = {"confirmed_quantity_kg": _plain(confirmed)}
    conditional = scenario.supply.conditional_quantity_kg
    if conditional is not None:
        # Purchase 가 실제로 확인해 준 값만 실린다. 0 도 사실이라 그대로 나른다.
        supply["conditional_quantity_kg"] = _plain(conditional)
        if scenario.supply.dependency_ref is not None:
            supply["dependency_ref"] = scenario.supply.dependency_ref
    return supply


def _plain(value: Decimal) -> str:
    """Decimal 을 문자열로 옮긴다 — float 로 바꾸면 자리수가 조용히 어긋난다."""
    return str(value)


def build_financial_validation_batch(
    scenarios: list[SalesScenario],
) -> FinanceValidationProjection:
    """1~3안을 재무 batch payload 로 옮긴다.

    ★ 개수를 여기서 자르지 않는다. 몇 안을 낼지는 제안 생성이 정하는 것이고, 개수
      계약을 어겼는지는 재무가 자기 계약으로 판정한다 — 여기서 조용히 잘라내면
      사용자가 본 안과 검증된 안이 달라진다.
    """
    projections = [build_financial_validation_request(scenario) for scenario in scenarios]
    unresolved: list[str] = []
    for projection in projections:
        for field in projection.unresolved:
            if field not in unresolved:
                unresolved.append(field)
    return FinanceValidationProjection(
        {"scenarios": [projection.payload for projection in projections]},
        tuple(unresolved),
    )
