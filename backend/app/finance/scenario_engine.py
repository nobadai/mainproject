"""레거시 결정론 Scenario 실행의 공개 표면.

구현은 `app.finance.legacy.scenario_engine` 이 가진다.
"""

from app.finance.legacy.scenario_engine import (
    run_finance_procurement_scenario,
    run_finance_sales_scenario,
)

__all__ = ["run_finance_procurement_scenario", "run_finance_sales_scenario"]
