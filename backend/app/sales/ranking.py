"""Sales-local deterministic candidate ranking.

LLM은 이 모듈이 고른 ID를 설명할 뿐 바꿀 수 없다. 임계값 정책이 없으므로
profit near-tie는 정확히 같은 값일 때만 성립한다.
"""

from decimal import Decimal

from app.sales.schemas import SalesScenario

_STATUS_ORDER = {"EXECUTABLE": 0, "CONDITIONAL": 1}
_FINANCE_ORDER = {"PASS": 0, "REVIEW_REQUIRED": 1, None: 2, "FAIL": 3}
_SEVERE = {"SEVERE", "CRITICAL"}
_SELL_PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, None: 3}


def rank_scenarios(scenarios: list[SalesScenario]) -> list[SalesScenario]:
    """실행 가능한 후보만 권위 순서로 정렬한다."""
    eligible = [scenario for scenario in scenarios if scenario.status in _STATUS_ORDER]
    return sorted(eligible, key=_rank_key)


def recommended_scenario_id(scenarios: list[SalesScenario]) -> str | None:
    ranked = rank_scenarios(scenarios)
    return ranked[0].scenario_id if ranked else None


def _rank_key(scenario: SalesScenario) -> tuple[object, ...]:
    obligation = 0 if scenario.business_mode == "CONTRACT_FULFILLMENT" else 1
    severe = 1 if scenario.authoritative_inventory_risk_severity in _SEVERE else 0
    profit_missing = scenario.contribution_margin_krw is None
    profit = scenario.contribution_margin_krw or Decimal(0)
    # 정책 threshold가 없으므로 이 값은 정확한 이익 동점 뒤에서만 영향을 준다.
    freshness = _remaining_freshness(scenario)
    return (
        obligation,
        _STATUS_ORDER[scenario.status],
        _FINANCE_ORDER[scenario.finance_verdict],
        severe,
        profit_missing,
        -profit,
        scenario.depends_on_projected_inflow is not False,
        scenario.scenario_projected_cash_min is None,
        -(scenario.scenario_projected_cash_min or Decimal(0)),
        _SELL_PRIORITY_ORDER.get(scenario.sell_priority, 3),
        freshness is None,
        freshness or 0,
        len(scenario.execution_dependencies),
        not scenario.ml_support_used,
        -(scenario.sales_amount_krw or Decimal(0)),
        -(scenario.quantity_kg or Decimal(0)),
        scenario.scenario_id,
    )


def _remaining_freshness(scenario: SalesScenario) -> int | None:
    if scenario.remaining_freshness_days is not None:
        return scenario.remaining_freshness_days
    values: list[int] = []
    for reply in scenario.domain_replies:
        if reply.source_agent != "logistics":
            continue
        value = reply.payload.get("remaining_freshness_days")
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    return min(values) if values else None


def remove_dominated_scenarios(
    scenarios: list[SalesScenario],
) -> tuple[list[SalesScenario], dict[str, list[str]]]:
    """상업조건이 같은 후보 중 모든 권위 축에서 열등한 후보만 제외한다."""
    kept: list[SalesScenario] = []
    excluded: dict[str, list[str]] = {}
    for candidate in scenarios:
        dominator = next(
            (
                other
                for other in scenarios
                if other is not candidate
                and _same_commercial_terms(candidate, other)
                and _dominates(other, candidate)
            ),
            None,
        )
        if dominator is None:
            kept.append(candidate)
        else:
            excluded[candidate.scenario_id] = [f"DOMINATED_BY:{dominator.scenario_id}"]
    return kept, excluded


def _same_commercial_terms(a: SalesScenario, b: SalesScenario) -> bool:
    return (
        a.quantity_kg,
        a.unit_price_krw,
        a.delivery_date,
        a.payment_days,
        a.payment_terms_type,
        a.contract_term_days,
    ) == (
        b.quantity_kg,
        b.unit_price_krw,
        b.delivery_date,
        b.payment_days,
        b.payment_terms_type,
        b.contract_term_days,
    )


def _dominates(a: SalesScenario, b: SalesScenario) -> bool:
    status_a = _STATUS_ORDER.get(a.status, 99)
    status_b = _STATUS_ORDER.get(b.status, 99)
    profit_a = a.contribution_margin_krw
    profit_b = b.contribution_margin_krw
    profit_not_worse = profit_b is None or (profit_a is not None and profit_a >= profit_b)
    dependencies_not_worse = set(a.execution_dependencies) <= set(b.execution_dependencies)
    strictly_better = (
        status_a < status_b
        or (profit_a is not None and (profit_b is None or profit_a > profit_b))
        or set(a.execution_dependencies) < set(b.execution_dependencies)
    )
    return (
        status_a <= status_b
        and profit_not_worse
        and dependencies_not_worse
        and strictly_better
        and not b.authoritative_inventory_risk_severity
        and not (
            b.supply.conditional_quantity_kg is not None and b.supply.conditional_quantity_kg > 0
        )
    )
