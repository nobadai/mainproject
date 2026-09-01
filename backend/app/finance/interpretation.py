"""레거시 응답 해설 보강의 공개 표면.

구현은 `app.finance.legacy.interpretation` 이 가진다.
"""

from app.finance.legacy.interpretation import build_finance_context, enrich_finance_response

__all__ = ["build_finance_context", "enrich_finance_response"]
