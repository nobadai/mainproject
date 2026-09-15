"""신규 일별 Finance state를 만들 때 쓰는 결정론 ID 규칙."""

from __future__ import annotations

from datetime import date

__all__ = ["daily_finance_state_id"]


def daily_finance_state_id(*, sim_run_id: str, financing_mode: str, state_date: date) -> str:
    """transition과 명시적 day opening이 새 state를 만들 때 공유하는 ID다.

    이 함수는 CREATE identity 규칙이지 LOOKUP identity 규칙이 아니다. 이미 존재하는
    Finance state 조회의 정본 키는 ``(sim_run_id, financing_mode, state_date)``이며,
    ID 문자열을 조립해 존재성을 판단하지 않는다.
    """
    return f"FIN-DAY-{sim_run_id}-{financing_mode}-{state_date:%Y%m%d}"
