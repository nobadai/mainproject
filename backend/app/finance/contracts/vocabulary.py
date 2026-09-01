"""Finance 계약이 공유하는 닫힌 어휘."""

from typing import Literal

FinalVerdict = Literal["PASS", "REVIEW_REQUIRED", "FAIL"]
RuntimeStatus = Literal["READY", "RUNTIME_NOT_READY", "ERROR"]
CashPriority = Literal["LOW", "MEDIUM", "HIGH"]
FinanceCycle = Literal["PROCUREMENT", "SALES"]
CashEventDirection = Literal["INFLOW", "OUTFLOW"]
CashEventType = Literal[
    "PURCHASE_PAYABLE",
    "COMMITTED_OUTFLOW",
    "RECEIVABLE",
    "PAYROLL",
    "DEBT_SERVICE",
    "EXTRA_PURCHASE",
    "H1_PURCHASE_PAYMENT",
]

