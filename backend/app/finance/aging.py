"""Deterministic receivable-aging vocabulary shared by Finance readers.

★ **This stays the Finance-facing name.**  Finance code and Finance tests keep
  importing `app.finance.aging` — that path is the canon they were written against.

🔴 **The implementation moved to `app/contracts/aging.py`.**  Sales needs the same
   rule for the 수금 screen, and a Sales module importing `app.finance` would bolt
   the two departments together at the execution layer (the boundary that
   `tests/finance/test_finance_sales_orchestration_boundary.py` guards).  Copying
   the rule into Sales was the other option and the worse one: two implementations
   of the same buckets, each correct on its own terms, disagreeing about the same
   receivable.  So the rule lives in the shared contract and Finance re-exports it.
"""

from app.contracts.aging import AgingBucket, classify_receivable_aging

__all__ = ["AgingBucket", "classify_receivable_aging"]
