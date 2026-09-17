"""발화문 입구 — 분류하고, 확인이 필요 없을 때만 실행하고, 사람 말로 답한다.

```text
발화문 → [LLM ①분류] → 확인 필요? ─예→ 되묻고 끝 (아무것도 안 돈다)
                                 └아니오→ 조회 실행 → [LLM ⑥문장] → 답
```

★ **LLM 이 둘이고 역할이 다르다.** ①이 죽으면 되물어야 하지만(분류를 못 하면 실행할
  수 없다), **⑥이 죽어도 답은 나간다** — 숫자는 규칙이 만들고 LLM 은 앞머리 문장만
  얹기 때문이다 (`answer.py`).

★ **`flow.py` 는 이 모듈을 모른다.** 발화문 해석은 Flow 바깥 일이고, Flow 는 타입이
  붙은 요청만 받는다 — 그래야 백테스트에서 Flow 를 그대로 돌릴 수 있다.

★ **실행하는 것은 조회뿐이다.** 매입 실행(`PROCUREMENT_RUN`)은 확인을 받은 뒤
  `/master/ask/execute` 로 온다. 오분류 비용이 비대칭이기 때문이다 — 조회를 잘못
  고르면 다시 물으면 그만이지만, 매입은 예산 12회와 매입 LLM 을 태운다.

★ **조회도 실행이력에 적재한다** (2026-09-02 배선).
  조회는 안을 만들지 않지만 **예산을 쓰고 부서를 부른다.** 안 남기면 그 호출이
  이력에서 사라지고, M-16(실행 계획 온전성)이 막으려는 것이 정확히
  "안 보이는 호출" 이다. 조회만 계속 돌린 날과 아무것도 안 한 날이 같아 보이면 안 된다.

  전에는 표가 못 받았다 — 옛 `orchestrator_agent_runs.cycle` 의 CHECK 에 `STATUS` 가
  없었고, 어휘를 고치려면 오케·Critic 행의 뜻까지 건드려야 했다. 마스터가 자기
  표(`master_agent_runs`)로 나오면서 그 장애물이 없어졌다.

⚠️ **조회와 매입이 같은 업무 키를 쓴다.** 둘 다 `make_request_id(as_of)` 로
  `REQ-20251231-0001` 을 만든다 — 순번 관리가 호출자 몫이라 화면이 안 주면 같아진다.

  그래서 **읽는 쪽이 `cycle` 을 밝힌다** (`get_run_by_request_id(..., cycle=...)`).
  안 밝히면 조회가 최신 행이 되는 날 **결정이 조회를 가리키고** 이력 화면이 조회를
  보여준다. 조회는 승인 대상이 아니다.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.api.shown_run import SHOWN_SIM_RUN_ID

# DOMAIN_ACTION은 기존 Domain read/write를 호출만 한다. Master에 SQL/재계산을 두지 않는다.
from app.finance.console_credit import get_console_credit
from app.finance.console_expenses import get_console_expenses
from app.finance.console_payables import get_console_payables
from app.finance.console_receivables import get_console_receivables
from app.finance.dashboard import get_finance_cashflow, get_finance_dashboard
from app.finance.router import (
    CashAdjustmentChange,
    CreditLimitChange,
    ExpenseCancel,
    ExpenseCreate,
    ExpenseSettle,
    ReceivableCollectionChange,
    cancel_operating_expense,
    create_cash_adjustment,
    create_operating_expense,
    record_receivable_collection,
    register_credit_limit,
    settle_operating_expense,
)
from app.master import persistence, wiring
from app.master.answer import (
    AnswerFacts,
    Fact,
    facts_from_decision,
    facts_from_procurement,
    facts_from_status,
    render_answer,
)
from app.master.ask_schemas import (
    AnswerOut,
    AskExecuteRequest,
    AskRequest,
    AskResponse,
    DomainActionAnswer,
    StatusAnswer,
)
from app.master.budget import CallBudget
from app.master.decision import DecisionIn, DecisionRejected
from app.master.decision_repository import link_follow_up
from app.master.decision_service import record_decision
from app.master.envelope import ExecutionContext
from app.master.llm.answer_runtime import NarrativeService, get_narrative_service
from app.master.llm.runtime import IntentService, get_intent_service
from app.master.llm.schemas import DomainSlots, Intent, IntentResult
from app.master.report import render_finance_chat_report, render_sales_chat_report
from app.master.runner import MasterRunner
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse, SalesRunRequest
from app.master.service import get_run_history, make_request_id, run_procurement, run_sales
from app.master.status_flow import StatusFlow, StatusOutcome
from app.sales.console_partners import get_console_partner_detail, get_console_partners
from app.sales.console_proposals import get_console_sales_proposals
from app.sales.router import add_partner_profile, edit_partner_profile

#: 확인 없이 바로 도는 종류. 조회뿐이다.
_AUTO_RUN = frozenset({"STATUS_QUERY"})

_DOMAIN_READ_ACTIONS = frozenset(
    {
        "FINANCE_SUMMARY_GET",
        "FINANCE_CREDIT_LIMIT_GET",
        "FINANCE_EXPENSE_LIST",
        "FINANCE_CASHFLOW_GET",
        "FINANCE_RECEIVABLES_GET",
        "FINANCE_PAYABLES_GET",
        "FINANCE_REPORT_GENERATE",
        "SALES_PROPOSALS_TODAY",
        "SALES_CONFIRMED_TODAY",
        "SALES_REPORT_GENERATE",
        "PARTNER_LIST",
        "PARTNER_DETAIL_GET",
    }
)
_DOMAIN_WRITE_ACTIONS = frozenset(
    {
        "FINANCE_CASH_ADJUSTMENT_CREATE",
        "FINANCE_CREDIT_LIMIT_UPSERT",
        "FINANCE_COLLECTION_CREATE",
        "FINANCE_EXPENSE_CREATE",
        "FINANCE_EXPENSE_SETTLE",
        "FINANCE_EXPENSE_CANCEL",
        "SALES_PROPOSAL_CREATE",
        "PARTNER_CREATE",
        "PARTNER_UPDATE",
    }
)
_DOMAIN_REQUIRED: dict[str, tuple[str, ...]] = {
    "FINANCE_CASH_ADJUSTMENT_CREATE": ("direction", "amount", "source_ref"),
    "FINANCE_CREDIT_LIMIT_GET": ("partner_ref",),
    "FINANCE_CREDIT_LIMIT_UPSERT": (
        "partner_ref",
        "credit_limit",
        "effective_from",
        "evidence_grade",
        "source_ref",
    ),
    "FINANCE_EXPENSE_CREATE": (
        "expense_date",
        "due_date",
        "expense_category",
        "amount",
        "evidence_id",
    ),
    "FINANCE_EXPENSE_SETTLE": ("expense_id", "paid_date"),
    "FINANCE_EXPENSE_CANCEL": ("expense_id",),
    "SALES_PROPOSAL_CREATE": ("partner_ref", "business_mode", "item", "requested_quantity_kg"),
    "PARTNER_CREATE": ("partner_id", "partner_name", "partner_type"),
    "PARTNER_DETAIL_GET": ("partner_ref",),
    "PARTNER_UPDATE": ("partner_ref",),
}


class _DomainClarification(ValueError):
    pass


def _slots(intent: Intent) -> DomainSlots:
    return intent.slots or DomainSlots()


def _slot(intent: Intent, name: str):
    if name == "item":
        return intent.item
    return getattr(_slots(intent), name, None)


def _missing_domain_slots(intent: Intent) -> list[str]:
    action = intent.domain_action
    if action is None:
        return ["domain_action"]
    missing = [
        name for name in _DOMAIN_REQUIRED.get(action, ()) if _slot(intent, name) in (None, "")
    ]

    if action == "FINANCE_COLLECTION_CREATE":
        slots = _slots(intent)
        if not slots.receivable_id and not slots.partner_ref:
            missing.append("receivable_id 또는 partner_ref")
        if not slots.collect_all and not slots.amount:
            missing.append("amount 또는 collect_all")

    if action == "PARTNER_UPDATE":
        slots = _slots(intent)
        editable = (
            slots.partner_name,
            slots.partner_type,
            slots.client_type,
            slots.factory_region,
            slots.factory_city,
            slots.factory_area,
            slots.sales_collection_days,
            slots.pricing_contract_type,
            slots.active,
            slots.note,
        )
        if all(value is None for value in editable):
            missing.append("수정할 거래처 정보")
    return missing


_SLOT_LABELS = {
    "partner_ref": "거래처",
    "credit_limit": "새 여신한도",
    "effective_from": "한도 적용 시작일",
    "evidence_grade": "한도 근거 등급(공식 계약/거래처 확인/시뮬레이션 고정)",
    "source_ref": "확인 자료",
    "direction": "입금/출금 구분",
    "amount": "금액",
    "expense_id": "비용 번호",
    "expense_category": "비용 분류",
    "expense_date": "비용 발생일",
    "due_date": "지급 예정일",
    "paid_date": "실제 지급일",
    "evidence_id": "비용 근거 번호",
    "business_mode": "판매 유형",
    "item": "품목",
    "requested_quantity_kg": "판매 요청 수량",
    "partner_id": "내부 거래처 코드",
    "partner_name": "거래처명",
    "partner_type": "거래처 유형",
    "receivable_id 또는 partner_ref": "받을 돈 번호 또는 거래처",
    "amount 또는 collect_all": "수금액 또는 전액 수금 여부",
    "수정할 거래처 정보": "수정할 거래처 정보",
}


def _missing_message(missing: list[str]) -> str:
    names = [_SLOT_LABELS.get(name, name) for name in missing]
    if len(names) == 1:
        return f"{names[0]}을 알려주세요."
    return "계속하려면 " + ", ".join(names) + "을 알려주세요."


def _dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value


def _won(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)):,.0f}원"
    except (InvalidOperation, ValueError, TypeError):
        return "—"


def _money(raw: str | None, *, field: str, allow_zero: bool = False) -> Decimal:
    """사용자가 말한 금액 표현만 deterministic하게 원으로 바꾼다."""
    if not raw:
        raise _DomainClarification(f"{field}을(를) 알려주세요.")
    text = raw.strip().replace(",", "").replace(" ", "")
    text = text.removesuffix("원")
    multipliers = (
        ("천만", Decimal(10_000_000)),
        ("백만", Decimal(1_000_000)),
        ("십만", Decimal(100_000)),
        ("억", Decimal(100_000_000)),
        ("만", Decimal(10_000)),
        ("천", Decimal(1_000)),
        ("백", Decimal(100)),
        ("십", Decimal(10)),
    )
    multiplier = Decimal(1)
    for suffix, factor in multipliers:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            multiplier = factor
            break
    if not re.fullmatch(r"\d+(?:\.\d+)?", text or ""):
        raise _DomainClarification(
            f"{field}을(를) 원 단위가 분명하게 다시 말씀해 주세요. 예: 300만원, 3000000원"
        )
    value = Decimal(text) * multiplier
    if value < 0 or (value == 0 and not allow_zero):
        rule = "0 이상" if allow_zero else "0보다 크게"
        raise _DomainClarification(f"{field}은(는) {rule} 알려주세요.")
    return value


def _integer(raw: str | None, *, field: str, allow_zero: bool = True) -> int | None:
    if raw is None:
        return None
    text = raw.strip().replace("일", "")
    if not text.isdigit():
        raise _DomainClarification(f"{field}을(를) 숫자로 알려주세요.")
    value = int(text)
    if value < 0 or (value == 0 and not allow_zero):
        raise _DomainClarification(f"{field} 값이 올바르지 않습니다.")
    return value


def _user_date(raw: str | None, *, as_of: date, field: str, required: bool = False) -> date | None:
    if raw is None:
        if required:
            raise _DomainClarification(f"{field}을(를) 알려주세요.")
        return None
    text = raw.strip()
    relative = {
        "오늘": as_of,
        "금일": as_of,
        "어제": as_of - timedelta(days=1),
        "내일": as_of + timedelta(days=1),
    }
    if text in relative:
        return relative[text]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if match:
        try:
            return date(as_of.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            pass
    raise _DomainClarification(
        f"{field}을(를) 날짜가 분명하게 다시 말씀해 주세요. 예: 오늘, 2026-09-17"
    )


def _period(intent: Intent, *, as_of: date) -> tuple[date, date]:
    slots = _slots(intent)
    if slots.start_date or slots.end_date:
        start = _user_date(slots.start_date, as_of=as_of, field="시작일") or as_of
        end = _user_date(slots.end_date, as_of=as_of, field="종료일") or as_of
        if start > end:
            raise _DomainClarification("시작일은 종료일보다 늦을 수 없습니다.")
        return start, min(end, as_of)

    period = slots.period or "TODAY"
    if period == "TODAY":
        return as_of, as_of
    if period == "YESTERDAY":
        day = as_of - timedelta(days=1)
        return day, day
    if period == "THIS_WEEK":
        return as_of - timedelta(days=as_of.weekday()), as_of
    if period == "LAST_WEEK":
        this_monday = as_of - timedelta(days=as_of.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if period == "THIS_MONTH":
        return as_of.replace(day=1), as_of
    if period == "LAST_7_DAYS":
        return as_of - timedelta(days=6), as_of
    if period == "LAST_30_DAYS":
        return as_of - timedelta(days=29), as_of
    if period == "LAST_3_MONTHS":
        month = as_of.month - 2
        year = as_of.year
        if month <= 0:
            month += 12
            year -= 1
        return as_of.replace(year=year, month=month, day=1), as_of
    if period == "LAST_YEAR":
        try:
            return as_of.replace(year=as_of.year - 1), as_of
        except ValueError:  # 2월 29일의 전년은 2월 28일
            return as_of.replace(year=as_of.year - 1, day=28), as_of
    raise _DomainClarification("기간을 확인해 주세요.")


def _has_report_period(intent: Intent) -> bool:
    """Report generation must not silently turn an omitted period into TODAY."""
    slots = _slots(intent)
    return bool(slots.period or slots.start_date or slots.end_date)


def _partner_id(intent: Intent, *, as_of: date) -> str:
    slots = _slots(intent)
    ref = (slots.partner_id or slots.partner_ref or "").strip()
    if not ref:
        raise _DomainClarification("거래처를 알려주세요.")
    rows = get_console_partners(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of, query=ref).rows
    exact = [
        row for row in rows if row.partner_id == ref or (row.partner_name or "").strip() == ref
    ]
    hits = exact or rows
    if not hits:
        raise _DomainClarification(f"'{ref}' 거래처를 찾지 못했습니다.")
    if len(hits) > 1:
        choices = " · ".join(
            f"{row.partner_name or '이름 없음'}({row.partner_id})" for row in hits[:5]
        )
        raise _DomainClarification(f"거래처가 여러 곳입니다: {choices}. 하나를 지정해 주세요.")
    return hits[0].partner_id


def _financing_mode(intent: Intent, *, as_of: date) -> str:
    slots = _slots(intent)
    if slots.financing_mode:
        return slots.financing_mode
    dashboard = get_finance_dashboard(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
    modes = list(dict.fromkeys(state.financing_mode for state in dashboard.states))
    if not modes:
        raise _DomainClarification("해당 기준일의 재무 장부가 준비되지 않았습니다.")
    if len(modes) > 1:
        labels = {"BASE_NO_LOAN": "대출 없이 운영", "LOAN_BASELINE": "대출 반영"}
        choices = " · ".join(labels.get(mode, mode) for mode in modes)
        raise _DomainClarification(f"어느 재무 장부에 기록할까요? {choices}")
    return modes[0]


def _find_receivable(intent: Intent, *, as_of: date) -> str:
    slots = _slots(intent)
    if slots.receivable_id:
        return slots.receivable_id
    partner_id = _partner_id(intent, as_of=as_of)
    rows = get_console_receivables(
        sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of, partner_id=partner_id
    ).rows
    open_rows = [row for row in rows if row.outstanding_amount_krw > 0]
    if not open_rows:
        raise _DomainClarification("해당 거래처에 남아 있는 받을 돈이 없습니다.")
    if len(open_rows) > 1:
        choices = " · ".join(
            f"{row.receivable_id}({_won(row.outstanding_amount_krw)})" for row in open_rows[:5]
        )
        raise _DomainClarification(f"받을 돈이 여러 건입니다: {choices}. 하나를 지정해 주세요.")
    return open_rows[0].receivable_id


def _domain_preview(intent: Intent, *, as_of: date) -> str:
    action = intent.domain_action or ""
    slots = _slots(intent)

    if action == "FINANCE_CASH_ADJUSTMENT_CREATE":
        direction = "입금" if slots.direction == "INFLOW" else "출금"
        amount = _money(slots.amount, field="금액")
        mode = _financing_mode(intent, as_of=as_of)
        return (
            f"{direction}을 기록합니다.\n- 금액: {_won(amount)}\n- 장부: {mode}\n"
            f"- 기준일: {as_of.isoformat()}\n- 확인 자료: {slots.source_ref}\n진행할까요?"
        )

    if action == "FINANCE_CREDIT_LIMIT_UPSERT":
        partner_id = _partner_id(intent, as_of=as_of)
        amount = _money(slots.credit_limit, field="새 여신한도", allow_zero=True)
        effective = _user_date(
            slots.effective_from, as_of=as_of, field="적용 시작일", required=True
        )
        credit = get_console_credit(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        current = next((row for row in credit.partners if row.partner_id == partner_id), None)
        return (
            f"{partner_id}의 여신한도를 변경합니다.\n"
            f"- 현재 한도: {_won(None if current is None else current.credit_limit_krw)}\n"
            f"- 새 한도: {_won(amount)}\n- 적용일: {effective}\n"
            f"- 근거 등급: {slots.evidence_grade}\n- 확인 자료: {slots.source_ref}\n진행할까요?"
        )

    if action == "FINANCE_COLLECTION_CREATE":
        receivable_id = _find_receivable(intent, as_of=as_of)
        mode = _financing_mode(intent, as_of=as_of)
        amount = "전액" if slots.collect_all else _won(_money(slots.amount, field="수금액"))
        return (
            f"수금을 기록합니다.\n- 받을 돈: {receivable_id}\n- 수금: {amount}\n"
            f"- 장부: {mode}\n- 확인 자료: {slots.source_ref}\n진행할까요?"
        )

    if action == "FINANCE_EXPENSE_CREATE":
        amount = _money(slots.amount, field="비용 금액")
        expense_date = _user_date(
            slots.expense_date, as_of=as_of, field="비용 발생일", required=True
        )
        due_date = _user_date(slots.due_date, as_of=as_of, field="지급 예정일", required=True)
        return (
            f"비용을 ACCRUED로 등록합니다.\n- 분류: {slots.expense_category}\n"
            f"- 금액: {_won(amount)}\n- 발생일: {expense_date}\n- 지급 예정일: {due_date}\n"
            f"- 근거: {slots.evidence_id}\n진행할까요?"
        )

    if action == "FINANCE_EXPENSE_SETTLE":
        paid = _user_date(slots.paid_date, as_of=as_of, field="실제 지급일", required=True)
        mode = _financing_mode(intent, as_of=as_of)
        return (
            f"{slots.expense_id} 비용을 지급 처리합니다.\n"
            f"- 지급일: {paid}\n- 장부: {mode}\n진행할까요?"
        )

    if action == "FINANCE_EXPENSE_CANCEL":
        return f"{slots.expense_id} 비용을 취소합니다. 현금은 바꾸지 않습니다.\n진행할까요?"

    if action == "SALES_PROPOSAL_CREATE":
        partner_id = _partner_id(intent, as_of=as_of)
        qty = _money(slots.requested_quantity_kg, field="판매 요청 수량")
        return (
            f"판매 후보를 생성합니다.\n- 거래처: {partner_id}\n- 품목: {intent.item}\n"
            f"- 수량: {qty}kg\n- 판매 유형: {slots.business_mode}\n진행할까요?"
        )

    if action == "PARTNER_CREATE":
        return (
            "새 거래처를 등록합니다.\n"
            f"- 코드: {slots.partner_id}\n- 이름: {slots.partner_name}\n"
            f"- 유형: {slots.partner_type}\n진행할까요?"
        )

    if action == "PARTNER_UPDATE":
        partner_id = _partner_id(intent, as_of=as_of)
        return (
            f"{partner_id} 거래처 기본정보를 수정합니다. 여신한도는 건드리지 않습니다.\n진행할까요?"
        )

    return "이 작업을 실행할까요?"


def _domain_read(
    intent: Intent, *, as_of: date, sim_run_id: str = SHOWN_SIM_RUN_ID
) -> DomainActionAnswer:
    action = intent.domain_action or ""
    slots = _slots(intent)

    if action == "FINANCE_SUMMARY_GET":
        value = get_finance_dashboard(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        lines = ["재무 현황입니다."]
        for state in value.states:
            lines.append(
                f"- {state.financing_mode}: 현재 현금 {_won(state.current_cash_krw)}"
                f" · 운영 여유 {_won(state.operating_cash_buffer_krw)}"
            )
        return DomainActionAnswer(
            domain="finance", action=action, text="\n".join(lines), data=_dump(value)
        )

    if action == "FINANCE_CREDIT_LIMIT_GET":
        partner_id = _partner_id(intent, as_of=as_of)
        value = get_console_credit(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        row = next((item for item in value.partners if item.partner_id == partner_id), None)
        if row is None:
            text = f"{partner_id}의 여신 기록이 없습니다."
            data = {"partner_id": partner_id, "credit": None}
        else:
            text = (
                f"{row.partner_name or row.partner_id} 여신 현황\n"
                f"- 한도: {_won(row.credit_limit_krw)}\n"
                f"- 현재 미수: {_won(row.current_ar_krw)}\n"
                f"- 가용 여신: {_won(row.available_credit_krw)}"
            )
            data = _dump(row)
        return DomainActionAnswer(domain="finance", action=action, text=text, data=data)

    if action == "FINANCE_EXPENSE_LIST":
        start, end = _period(intent, as_of=as_of)
        value = get_console_expenses(
            sim_run_id=SHOWN_SIM_RUN_ID,
            as_of=as_of,
            from_date=start,
            to_date=end,
            category=slots.expense_category,
        )
        summary = value.summary
        text = (
            f"비용 {len(value.rows)}건\n- 미지급: {_won(summary.accrued_krw)}\n"
            f"- 지급: {_won(summary.paid_krw)}\n- 취소: {_won(summary.cancelled_krw)}"
        )
        return DomainActionAnswer(domain="finance", action=action, text=text, data=_dump(value))

    if action == "FINANCE_CASHFLOW_GET":
        start, end = _period(intent, as_of=as_of)
        value = get_finance_cashflow(
            sim_run_id=SHOWN_SIM_RUN_ID,
            as_of=end,
            days=max(1, min(400, (end - start).days + 1)),
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=f"{start} ~ {end} 자금 흐름 {len(value.cashflow)}일 기록을 불러왔습니다.",
            data=_dump(value),
        )

    if action == "FINANCE_RECEIVABLES_GET":
        value = get_console_receivables(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=f"아직 받을 돈 {_won(value.summary.total_outstanding_krw)} · {len(value.rows)}건",
            data=_dump(value),
        )

    if action == "FINANCE_PAYABLES_GET":
        value = get_console_payables(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=(
                f"아직 지급할 매입대금 {_won(value.summary.total_outstanding_krw)}"
                f" · 연체 {_won(value.summary.overdue_krw)}"
            ),
            data=_dump(value),
        )

    if action == "FINANCE_REPORT_GENERATE":
        start, end = _period(intent, as_of=as_of)
        report = render_finance_chat_report(
            sim_run_id=sim_run_id, as_of=as_of, start_date=start, end_date=end
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=f"{start} ~ {end} 재무 보고서를 만들었습니다.",
            data=report,
            report_kind="FINANCE",
        )

    if action == "SALES_PROPOSALS_TODAY":
        value = get_console_sales_proposals(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        return DomainActionAnswer(
            domain="sales",
            action=action,
            text=(
                f"오늘 판매안: 제시 가능 {value.presentable_count} · "
                f"검토 필요 {value.review_required_count} · "
                f"판정 대기 {value.unresolved_count} · 확정 불가 {value.rejected_count}"
            ),
            data=_dump(value),
        )

    if action == "SALES_CONFIRMED_TODAY":
        value = get_console_sales_proposals(sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of)
        rows = [_dump(row) for row in value.rows if row.sale_status in {"CONFIRMED", "DELIVERED"}]
        return DomainActionAnswer(
            domain="sales",
            action=action,
            text=f"오늘 실제 확정된 판매는 {len(rows)}건입니다.",
            data={"sim_run_id": SHOWN_SIM_RUN_ID, "as_of": as_of.isoformat(), "rows": rows},
        )

    if action == "SALES_REPORT_GENERATE":
        start, end = _period(intent, as_of=as_of)
        report = render_sales_chat_report(
            sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of, start_date=start, end_date=end
        )
        return DomainActionAnswer(
            domain="sales",
            action=action,
            text=f"{start} ~ {end} 판매 보고서를 만들었습니다.",
            data=report,
            report_kind="SALES",
        )

    if action == "PARTNER_LIST":
        value = get_console_partners(
            sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of, query=slots.partner_ref
        )
        return DomainActionAnswer(
            domain="partner",
            action=action,
            text=f"거래처 {len(value.rows)}곳을 찾았습니다.",
            data=_dump(value),
        )

    if action == "PARTNER_DETAIL_GET":
        partner_id = _partner_id(intent, as_of=as_of)
        value = get_console_partner_detail(
            sim_run_id=SHOWN_SIM_RUN_ID, as_of=as_of, partner_id=partner_id
        )
        if value is None:
            raise LookupError("거래처를 찾지 못했습니다.")
        collection_days = value.basic.sales_collection_days
        collection_days_text = "—" if collection_days is None else str(collection_days)
        return DomainActionAnswer(
            domain="partner",
            action=action,
            text=(
                f"{value.basic.partner_name or value.basic.partner_id}\n"
                f"- 유형: {value.basic.partner_type or '—'}\n"
                "- 결제일수: "
                f"{collection_days_text}"
            ),
            data=_dump(value),
        )

    raise NotImplementedError(f"{action} 읽기 경로가 배선되지 않았다.")


def _domain_write(
    intent: Intent,
    *,
    as_of: date,
    policy_version: str,
    request_id: str,
    sim_run_id: str | None = None,
    actor: str,
    utterance: str | None,
) -> DomainActionAnswer:
    action = intent.domain_action or ""
    slots = _slots(intent)

    if action == "FINANCE_CASH_ADJUSTMENT_CREATE":
        mode = _financing_mode(intent, as_of=as_of)
        direction = slots.direction
        if direction is None:
            raise _DomainClarification("입금인지 출금인지 알려주세요.")
        category = slots.cash_category or (
            "OWNER_INJECTION" if direction == "INFLOW" else "OWNER_WITHDRAWAL"
        )
        result = create_cash_adjustment(
            CashAdjustmentChange(
                sim_run_id=SHOWN_SIM_RUN_ID,
                financing_mode=mode,
                adjustment_date=as_of,
                direction=direction,
                category=category,
                amount_krw=_money(slots.amount, field="금액"),
                source_ref=slots.source_ref or "",
                recorded_by=actor,
                note=slots.note,
            )
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=f"자금 변동을 기록했습니다. 현재 현금 {_won(result.get('current_cash_krw'))}",
            data=_dump(result),
        )

    if action == "FINANCE_CREDIT_LIMIT_UPSERT":
        partner_id = _partner_id(intent, as_of=as_of)
        result = register_credit_limit(
            CreditLimitChange(
                partner_id=partner_id,
                credit_limit_krw=_money(slots.credit_limit, field="새 여신한도", allow_zero=True),
                effective_from=_user_date(
                    slots.effective_from, as_of=as_of, field="적용 시작일", required=True
                ),
                evidence_grade=slots.evidence_grade,  # type: ignore[arg-type]
                source_ref=slots.source_ref or "",
                recorded_by=actor,
                note=slots.note,
            )
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=(
                f"{partner_id}의 여신한도를 "
                f"{_won(result.get('credit_limit_krw'))}으로 저장했습니다."
            ),
            data=_dump(result),
        )

    if action == "FINANCE_COLLECTION_CREATE":
        receivable_id = _find_receivable(intent, as_of=as_of)
        mode = _financing_mode(intent, as_of=as_of)
        result = record_receivable_collection(
            ReceivableCollectionChange(
                sim_run_id=SHOWN_SIM_RUN_ID,
                financing_mode=mode,
                collection_date=as_of,
                receivable_id=receivable_id,
                collect_all=bool(slots.collect_all),
                amount_krw=None if slots.collect_all else _money(slots.amount, field="수금액"),
                source_ref=slots.source_ref or "",
                recorded_by=actor,
                note=slots.note,
            )
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=(
                f"{receivable_id} 수금을 기록했습니다. 남은 받을 돈 "
                f"{_won(result.get('outstanding_amount_krw'))}"
            ),
            data=_dump(result),
        )

    if action == "FINANCE_EXPENSE_CREATE":
        result = create_operating_expense(
            ExpenseCreate(
                sim_run_id=SHOWN_SIM_RUN_ID,
                expense_date=_user_date(
                    slots.expense_date, as_of=as_of, field="비용 발생일", required=True
                ),
                due_date=_user_date(
                    slots.due_date, as_of=as_of, field="지급 예정일", required=True
                ),
                expense_category=slots.expense_category or "",
                amount_krw=_money(slots.amount, field="비용 금액"),
                evidence_id=slots.evidence_id or "",
                related_delivery_id=slots.related_delivery_id,
                is_fixed=bool(slots.is_fixed),
                note=slots.note,
            )
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=f"비용 {result.get('expense_id')}을 ACCRUED로 등록했습니다.",
            data=_dump(result),
        )

    if action == "FINANCE_EXPENSE_SETTLE":
        mode = _financing_mode(intent, as_of=as_of)
        result = settle_operating_expense(
            slots.expense_id or "",
            ExpenseSettle(
                sim_run_id=SHOWN_SIM_RUN_ID,
                financing_mode=mode,
                paid_date=_user_date(
                    slots.paid_date, as_of=as_of, field="실제 지급일", required=True
                ),
            ),
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=(
                f"{result.get('expense_id')} 비용을 지급했습니다. 현재 현금 "
                f"{_won(result.get('current_cash_krw'))}"
            ),
            data=_dump(result),
        )

    if action == "FINANCE_EXPENSE_CANCEL":
        result = cancel_operating_expense(
            slots.expense_id or "", ExpenseCancel(sim_run_id=SHOWN_SIM_RUN_ID)
        )
        return DomainActionAnswer(
            domain="finance",
            action=action,
            text=f"{result.get('expense_id')} 비용을 취소했습니다. 현금은 바뀌지 않습니다.",
            data=_dump(result),
        )

    if action == "SALES_PROPOSAL_CREATE":
        partner_id = _partner_id(intent, as_of=as_of)
        payload: dict[str, object] = {
            "as_of": as_of,
            "policy_version": policy_version,
            "request_id": request_id,
            "sim_run_id": SHOWN_SIM_RUN_ID,
            "business_mode": slots.business_mode,
            "partner_id": partner_id,
            "item": intent.item,
            "requested_quantity_kg": _money(slots.requested_quantity_kg, field="판매 요청 수량"),
            "user_request": utterance,
            "allow_additional_sourcing": bool(slots.allow_additional_sourcing),
        }
        if slots.preferred_unit_price_krw is not None:
            payload["preferred_unit_price_krw"] = _money(
                slots.preferred_unit_price_krw, field="희망 단가", allow_zero=True
            )
        if slots.preferred_delivery_date is not None:
            payload["preferred_delivery_date"] = _user_date(
                slots.preferred_delivery_date, as_of=as_of, field="납품 희망일", required=True
            )
        if slots.preferred_payment_days is not None:
            payload["preferred_payment_days"] = _integer(
                slots.preferred_payment_days, field="결제일수"
            )
        if slots.preferred_payment_terms_type is not None:
            payload["preferred_payment_terms_type"] = slots.preferred_payment_terms_type
        # 권위 있는 source_ref는 채팅이 임의 생성하지 않는다. 없으면 판매/재무가 미비로 남긴다.
        response = run_sales(SalesRunRequest.model_validate(payload))
        return DomainActionAnswer(
            domain="sales",
            action=action,
            text=f"판매 후보 생성을 실행했습니다. 결과: {response.end_code} · {response.reason}",
            data=_dump(response),
        )

    if action == "PARTNER_CREATE":
        body: dict[str, object] = {
            "partner_id": slots.partner_id,
            "partner_name": slots.partner_name,
            "partner_type": slots.partner_type,
            "active": True if slots.active is None else slots.active,
        }
        for key in (
            "client_type",
            "factory_region",
            "factory_city",
            "factory_area",
            "pricing_contract_type",
            "note",
        ):
            value = getattr(slots, key)
            if value is not None:
                body[key] = value
        if slots.sales_collection_days is not None:
            body["sales_collection_days"] = _integer(slots.sales_collection_days, field="결제일수")
        result = add_partner_profile(body)
        return DomainActionAnswer(
            domain="partner",
            action=action,
            text=f"{result.partner_name}({result.partner_id}) 거래처를 등록했습니다.",
            data=_dump(result),
        )

    if action == "PARTNER_UPDATE":
        partner_id = _partner_id(intent, as_of=as_of)
        body: dict[str, object] = {}
        for key in (
            "partner_name",
            "partner_type",
            "client_type",
            "factory_region",
            "factory_city",
            "factory_area",
            "pricing_contract_type",
            "active",
            "note",
        ):
            value = getattr(slots, key)
            if value is not None:
                body[key] = value
        if slots.sales_collection_days is not None:
            body["sales_collection_days"] = _integer(slots.sales_collection_days, field="결제일수")
        result = edit_partner_profile(partner_id, body)
        return DomainActionAnswer(
            domain="partner",
            action=action,
            text=f"{result.partner_name}({result.partner_id}) 거래처 정보를 수정했습니다.",
            data=_dump(result),
        )

    raise NotImplementedError(f"{action} 쓰기 경로가 배선되지 않았다.")


def _run_domain_action(
    intent: Intent,
    *,
    as_of: date,
    policy_version: str,
    request_id: str,
    sim_run_id: str | None = None,
    actor: str | None = None,
    utterance: str | None = None,
):
    action = intent.domain_action
    if action in _DOMAIN_READ_ACTIONS:
        return _domain_read(intent, as_of=as_of, sim_run_id=sim_run_id or SHOWN_SIM_RUN_ID)
    if action in _DOMAIN_WRITE_ACTIONS:
        if not actor:
            raise DecisionRejected(
                "기록할 사용자를 확인하지 못했습니다 — 로그인 사용자가 필요합니다."
            )
        return _domain_write(
            intent,
            as_of=as_of,
            policy_version=policy_version,
            request_id=request_id,
            actor=actor,
            utterance=utterance,
        )
    raise NotImplementedError(f"{action} DOMAIN_ACTION 경로가 배선되지 않았다.")


def _domain_answer_response(
    *, request_id: str, as_of: date, intent: Intent, result: DomainActionAnswer, outcome: str
) -> AskResponse:
    return AskResponse(
        request_id=request_id,
        as_of=as_of,
        outcome=outcome,  # type: ignore[arg-type]
        intent=intent,
        answer=AnswerOut(text=result.text, markdown=result.markdown, llm_status="SKIPPED_TEMPLATE"),
        domain_result=result,
        llm_status="SKIPPED_TEMPLATE",
        note=_shown_note(as_of),
    )


def _ask_domain_action(
    *, request_id: str, request: AskRequest, result: IntentResult
) -> AskResponse:
    intent = result.intent
    if intent.domain_action == "FINANCE_REPORT_GENERATE" and not _has_report_period(intent):
        return _response(
            request_id,
            request,
            result,
            outcome="NEEDS_CLARIFICATION",
            clarification="어느 기간의 재무 보고서를 생성할까요?",
            note="보고 기간이 정해지기 전에는 재무 보고서를 생성하지 않았다.",
        )
    missing = _missing_domain_slots(intent)
    if missing:
        return _response(
            request_id,
            request,
            result,
            outcome="NEEDS_CLARIFICATION",
            clarification=_missing_message(missing),
            note="필수 정보가 없어 실행하지 않았다.",
        )
    if intent.confidence != "HIGH":
        return _response(
            request_id,
            request,
            result,
            outcome="NEEDS_CLARIFICATION",
            clarification=(
                "요청을 한 가지 의미로 확정하지 못했습니다. 조금 더 구체적으로 말씀해 주세요."
            ),
            note="낮은 분류 신뢰도로 실행하지 않았다.",
        )
    try:
        if intent.domain_action in _DOMAIN_READ_ACTIONS:
            domain = _run_domain_action(
                intent,
                as_of=request.as_of,
                policy_version=request.policy_version,
                request_id=request_id,
                sim_run_id=request.sim_run_id,
                utterance=request.utterance,
            )
            assert isinstance(domain, DomainActionAnswer)
            response = _domain_answer_response(
                request_id=request_id,
                as_of=request.as_of,
                intent=intent,
                result=domain,
                outcome="DOMAIN_ACTION_ANSWERED",
            )
            # ① 분류의 상태는 보존한다. ⑥ narrative는 부르지 않았다.
            response.llm_status = result.llm_status
            response.llm_provider = result.llm_provider
            response.llm_model = result.llm_model
            response.llm_attempts = result.llm_attempts
            response.llm_fallback_used = result.llm_fallback_used
            return response

        if intent.domain_action in _DOMAIN_WRITE_ACTIONS:
            return _response(
                request_id,
                request,
                result,
                outcome="CLASSIFIED_ONLY",
                confirm_required=True,
                clarification=_domain_preview(intent, as_of=request.as_of),
                note="확인 전에는 장부를 바꾸지 않았다.",
            )
    except _DomainClarification as error:
        return _response(
            request_id,
            request,
            result,
            outcome="NEEDS_CLARIFICATION",
            clarification=str(error),
            note="정보가 하나로 정해지지 않아 실행하지 않았다.",
        )
    raise NotImplementedError(f"{intent.domain_action} DOMAIN_ACTION 경로가 배선되지 않았다.")


def ask(
    request: AskRequest,
    service: IntentService | None = None,
    narrator: NarrativeService | None = None,
) -> AskResponse:
    """발화문을 분류하고, 확인이 필요 없으면 조회까지 돌린 뒤 **사람 말로 답한다.**

    ★ `service`(①분류) · `narrator`(⑥응답)를 주지 않으면 `.env` 설정으로 만든다.
      테스트가 갈아 끼운다. **둘을 나눠 받는 이유는 역할마다 모델 등급이 달라질
      것이기 때문이다** — 분류는 소형이면 되고, 응답 문장도 마찬가지지만 판정 검증은
      아니다.
    """
    service = service or get_intent_service()
    request_id = request.request_id or make_request_id(request.as_of.isoformat())
    result = service.classify(request.utterance)
    if request.date_from or request.date_to:
        if request.date_from is None or request.date_to is None:
            raise ValueError("시작일과 종료일을 모두 선택해 주세요.")
        if request.date_from > request.date_to:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        intent = result.intent
        if intent.domain_action in {"FINANCE_REPORT_GENERATE", "SALES_REPORT_GENERATE"}:
            slots = _slots(intent).model_copy(update={
                "start_date": request.date_from.isoformat(),
                "end_date": request.date_to.isoformat(),
            })
            updated_intent = intent.model_copy(update={"slots": slots})
            result = result.model_copy(update={"intent": updated_intent})
    intent = result.intent

    if intent.action == "UNKNOWN":
        return _response(
            request_id,
            request,
            result,
            outcome="NEEDS_CLARIFICATION",
            note="발화문을 분류하지 못했다. 실행하지 않았다.",
        )

    if intent.action == "DOMAIN_ACTION":
        return _ask_domain_action(request_id=request_id, request=request, result=result)

    if result.needs_confirmation or intent.action not in _AUTO_RUN:
        return _response(
            request_id,
            request,
            result,
            outcome="CLASSIFIED_ONLY",
            confirm_required=True,
            note="확인 후 /master/ask/execute 로 같은 intent 를 보내면 실행한다.",
        )

    outcome = _run_status(
        request_id=request_id,
        as_of=request.as_of,
        policy_version=request.policy_version,
        budget=request.budget,
        intent=intent,
        question=request.utterance,
    )
    return _response(
        request_id,
        request,
        result,
        outcome="STATUS_ANSWERED",
        status=_to_answer(outcome),
        answer=_write_answer(facts_from_status(outcome), narrator),
        note=_shown_note(request.as_of),
    )


def execute(
    request: AskExecuteRequest,
    narrator: NarrativeService | None = None,
) -> AskResponse | ProcurementRunResponse:
    """사용자가 확인한 의도를 실행한다.

    ★ **발화문을 다시 분류하지 않는다.** 본 것을 실행한다.

    ★ 매입 실행은 기존 `run_procurement` 을 그대로 탄다 — 발화문 경로라고 다른 Flow 를
      두면 두 경로가 조용히 갈라진다 (구 백로그 B1-3 이 그 문제였다).
    """
    intent = request.intent
    request_id = request.request_id or make_request_id(request.as_of.isoformat())

    if intent.action == "DOMAIN_ACTION":
        missing = _missing_domain_slots(intent)
        if missing:
            raise DecisionRejected(_missing_message(missing))
        try:
            domain = _run_domain_action(
                intent,
                as_of=request.as_of,
                policy_version=request.policy_version,
                request_id=request_id,
                actor=request.actor,
                utterance=request.utterance,
            )
        except _DomainClarification as error:
            raise DecisionRejected(str(error)) from error
        if isinstance(domain, ProcurementRunResponse):
            return domain
        return _domain_answer_response(
            request_id=request_id,
            as_of=request.as_of,
            intent=intent,
            result=domain,
            outcome=(
                "DOMAIN_ACTION_EXECUTED"
                if intent.domain_action in _DOMAIN_WRITE_ACTIONS
                else "DOMAIN_ACTION_ANSWERED"
            ),
        )

    if intent.action == "STATUS_QUERY":
        outcome = _run_status(
            request_id=request_id,
            as_of=request.as_of,
            policy_version=request.policy_version,
            budget=request.budget,
            intent=intent,
            # 확인을 거친 조회는 발화문이 없다 — 화면이 원문을 되돌려 줄 때만 싣는다.
            question=request.utterance,
        )
        return AskResponse(
            request_id=request_id,
            as_of=request.as_of,
            outcome="STATUS_ANSWERED",
            intent=intent,
            status=_to_answer(outcome),
            answer=_write_answer(facts_from_status(outcome), narrator),
            # ①은 안 부른다 (이미 분류된 의도다). ⑥의 상태는 answer 안에 있다.
            llm_status="SKIPPED_TEMPLATE",
            note=_shown_note(request.as_of),
        )

    if intent.action == "PROCUREMENT_RUN":
        response = run_procurement(
            ProcurementRunRequest(
                as_of=request.as_of,
                policy_version=request.policy_version,
                request_id=request_id,
                item=intent.item,
                budget=request.budget,
                # 🔴 **화면이 보는 실행으로 판단한다.** 안 실으면 번인으로 떨어진다
                #   (service.py `given or BURN_IN_SIM_RUN_ID`).
                sim_run_id=SHOWN_SIM_RUN_ID,
            )
        )
        # ★ **여기에는 ⑥ 을 붙이지 않는다.** 매입 리포트의 머리말은 이미 완결된 판단
        #   문장이라(`"매입안을 제시합니다. 고르시면 진행합니다."`) LLM 이 얹으면
        #   **같은 말을 두 번** 한다. 실측에서 *"매입안을 제시합니다."* 가 그대로
        #   중복됐다. 더할 것이 없는 자리에 모델을 부르는 것은 비용과 위험만 는다.
        #
        #   조회·결정은 다르다 — 거기 머리말은 문장이 아니라 머리글이라 얹을 자리가 있다.
        return response

    if intent.action == "SELECT_SCENARIO":
        return _record_selection(request)

    if intent.action == "RERUN_WITH_CONDITION":
        return _record_rerun(request)

    if intent.action == "UNKNOWN":
        # **"아직 안 만들었다" 가 아니라 "실행할 것이 없다" 다.** 501 로 답하면 언젠가
        # 되는 것처럼 읽힌다 — `UNKNOWN` 은 분류에 실패했다는 뜻이라 영영 실행되지 않는다.
        raise DecisionRejected(
            "UNKNOWN 은 실행할 수 없다 — 무엇을 할지 정해지지 않았다. 다시 물어라."
        )

    # 종류가 늘어났는데 여기 배선을 안 한 경우. **조용히 통과시키지 않는다.**
    raise NotImplementedError(f"{intent.action} 실행 경로가 배선되지 않았다.")


# ── 내부 ────────────────────────────────────────────────────────────────


def _run_status(
    *,
    request_id: str,
    as_of: date,
    policy_version: str,
    budget: int,
    intent: Intent,
    question: str | None = None,
) -> StatusOutcome:
    """조회 Flow 를 돌린다. **어댑터 미등록도 결과로 접는다.**

    ★ **끝에서 이력에 적재한다** (2026-09-02). 조회는 안을 만들지 않지만 예산을 쓰고
      부서를 부른다 — 안 남기면 그 호출이 이력에서 사라진다. 적재 실패는 답을 막지
      않는다 (`try_save_run` 이 삼킨다).
    """
    started = time.perf_counter()
    context = ExecutionContext(
        request_id=request_id,
        as_of=as_of,
        trigger="USER_REQUEST",
        policy_version=policy_version,
        # ★ **조회는 화면이 보는 실행을 읽는다** (2026-09-14). 전에는 번인 상수라
        #   2025-12 한 달치 장부를 읽었고, 2026 날짜는 기준일을 바꿔도 늘 같은 물려받은
        #   상태가 나왔다. 화면 탭과 같은 한 자리(`app/api/shown_run.py`)를 가리킨다.
        sim_run_id=SHOWN_SIM_RUN_ID,
    )
    asked = tuple(intent.agents)
    missing = set(wiring.missing())
    registered = tuple(a for a in asked if a not in missing)

    runner = MasterRunner(context, wiring.registry(), CallBudget(limit=budget))
    # ★ 발화 원문은 ML 과 물류가 받고, 품목은 **ML 에만** 실린다
    #   (`StatusFlow._payload_for` · 물류는 원문을 직접 읽어 품목을 푼다).
    outcome = StatusFlow(runner, registered, question=question, item=intent.item).run()

    unregistered = tuple(a for a in asked if a in missing)
    if unregistered:
        # 미등록은 오류가 아니라 "그 부서가 오늘 돌지 않는다"와 같다 (§5.3).
        merged_missing = dict(outcome.missing_data)
        for agent in unregistered:
            merged_missing[agent] = ("ADAPTER_NOT_REGISTERED",)
        answered = len(outcome.answers)
        outcome = StatusOutcome(
            status_code="S3_UNAVAILABLE" if answered == 0 else "S2_PARTIAL",
            reason=(
                f"{', '.join(unregistered)} 어댑터가 등록되지 않았다."
                if answered == 0
                else f"{outcome.reason} {', '.join(unregistered)} 는 어댑터 미등록이다."
            ),
            plan=outcome.plan,
            answers=outcome.answers,
            unavailable=outcome.unavailable + unregistered,
            missing_data=merged_missing,
            errors=outcome.errors,
        )

    # ★ **미등록으로 접힌 결과를 적재한다.** 위에서 바로 돌려주면 "어댑터가 없어
    #   못 물어본 날" 이 이력에 안 남는다 - 안 부른 것과 못 부른 것은 다르다.
    persistence.record_status(
        request_id=request_id,
        as_of=as_of,
        policy_version=policy_version,
        intent=intent.model_dump(mode="json"),
        outcome=outcome,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        # 🔴 **읽기 축과 기록 축을 나눈다** (2026-09-14). 봉투 축은 읽을 장부이고,
        #   이 행은 걷기가 만든 행이 아니다. 정본 실행 축으로 적으면 그 실행의 이력
        #   (`count_runs_by_day` · 실행 목록의 최근 활동)에 조회가 섞인다. 칸이
        #   NULL 을 받으므로(`master_agent_runs_sim_run_id.sql`) «걷기 밖» 으로 적는다.
        sim_run_id=None,
    )
    return outcome


def _record_selection(request: AskExecuteRequest) -> AskResponse:
    """사용자가 **말로 고른 안**을 결정 이력에 적는다 (역할 ⑦ 앞의 사람 게이트).

    ★ **여기서 새로 검사하지 않는다.** 라벨이 그 실행에 실제로 있었나 · 지금 승인할 수
      있는 상태인가는 전부 `decision_service` 가 한다. 발화문 경로라고 검사를 따로 두면
      **두 경로의 승인 기준이 조용히 갈라진다** — 화면에서 누른 승인과 말로 한 승인이
      다른 규칙을 타면 안 된다.

    ★ **마스터 Flow 는 이 경로를 부를 수 없다.** `flow.py` 는 `decision` 계열을
      임포트하지 않는다 (8/26 회의 — 승인 게이트는 툴 목록 바깥).

    🔴 **말에 없는 둘을 화면이 싣는다.** 어느 실행인지(`target_request_id`)와 누가
      승인하는지(`decided_by`)는 발화문에 없다. 없으면 **추측하지 않고 거절한다.**
    """
    intent = request.intent
    if not request.target_request_id:
        raise DecisionRejected(
            "어느 실행의 안인지 지정되지 않았다 — target_request_id 가 필요하다. "
            "발화문에는 그 정보가 없으므로 화면이 실어야 한다."
        )
    if not request.decided_by:
        raise DecisionRejected(
            "승인자가 없다 — decided_by 가 필요하다. 승인자가 없는 승인은 승인이 아니다."
        )

    decision = record_decision(
        request.target_request_id,
        DecisionIn(
            decision="APPROVE",
            scenario_label=intent.scenario_label,
            decided_by=request.decided_by,
            # ★ 화면이 본 실행을 그대로 넘긴다 — 여기서 고르지 않는다.
            history_run_id=request.target_history_run_id,
            note="발화문 경로에서 선택",
        ),
    )
    return AskResponse(
        request_id=decision.request_id,
        as_of=request.as_of,
        outcome="DECISION_RECORDED",
        intent=intent,
        decision=decision,
        answer=_rule_answer(facts_from_decision(decision)),
        # ①도 ⑥도 안 부른다 — 이미 분류된 의도이고, 머리말이 이미 완결 문장이다.
        llm_status="SKIPPED_TEMPLATE",
    )


def _record_rerun(request: AskExecuteRequest) -> AskResponse:
    """조건을 붙인 재요청 — **적고 · 다시 돌리고 · 둘을 잇는다.**

    ```text
    REQUEST_CHANGE 적재 → 새 업무 키로 재실행 → follow_up_request_id 로 연결
    ```

    ★ **조건을 숫자로 해석하지 않는다.** *"예산 2천만원으로 낮춰서"* 를 재무 cap 으로
      꽂으면 마스터가 부서 판단을 덮어쓰는 것이다. 사용자의 말을 그대로
      `prior_feedback` 으로 매입에 넘기고, **해석은 매입이 한다** (§3.2.2).

    🔴 **지금 매입은 그 조건으로 안을 바꾸지 않는다.** `prior_feedback` 을
      `is_refeed` 메타로만 읽는다(`self_check.py`). 그래서 재실행 결과에
      **그 사실을 적어 내보낸다** — 안 적으면 사용자는 조건이 반영된 줄 안다.
      값을 실어 주고 안 쓰는 것을 매입에 지적해 놓고 같은 일을 조용히 할 수는 없다.

    ★ **품목은 원 실행에서 가져온다.** *"예산 줄여서 다시 해줘"* 에는 품목이 없다.
      발화문에 없는 것을 지어내지 않고 **직전 실행이 무엇이었는지**를 본다.
    """
    intent = request.intent
    if not request.target_request_id:
        raise DecisionRejected(
            "어느 실행에 대한 재요청인지 지정되지 않았다 — target_request_id 가 필요하다."
        )
    if not request.decided_by:
        raise DecisionRejected("요청자가 없다 — decided_by 가 필요하다.")
    if not intent.condition:
        raise DecisionRejected("조건이 비어 있다 — 조건 없는 재요청은 그냥 거절이다.")

    decision = record_decision(
        request.target_request_id,
        DecisionIn(
            decision="REQUEST_CHANGE",
            condition_text=intent.condition,
            decided_by=request.decided_by,
            history_run_id=request.target_history_run_id,
            note="발화문 경로에서 조건부 재요청",
        ),
    )

    follow_up_id = make_request_id(request.as_of.isoformat(), seq=decision.decision_seq + 1)
    rerun = run_procurement(
        ProcurementRunRequest(
            as_of=request.as_of,
            policy_version=request.policy_version,
            request_id=follow_up_id,
            item=intent.item or _item_of(request.target_request_id),
            budget=request.budget,
            sim_run_id=SHOWN_SIM_RUN_ID,
            prior_feedback={
                "condition_text": intent.condition,
                # 🔴 **`attempt` 가 아니다** (#178 · 매입 실측 2026-09-03).
                #
                #   슬롯을 둘로 나누면서(계약 v0.2 §2) **안의 키 이름은 안 갈랐다.**
                #   수명·모양·권위가 다르다고 슬롯을 나눠 놓고 같은 이름을 양쪽에 뒀다.
                #
                #     prior_feedback["condition_seq"]   사람이 조건을 건 회차   ← 여기
                #     feedback_context["attempt"]       매입 재호출 회차
                #
                #   매입이 `state["feedback"].get("attempt", 0)` 으로 되먹임 회차를
                #   찾다가 늘 0을 받았다 — 틀린 값을 읽은 것이 아니라 **다른 개념을
                #   같은 이름으로 찾고 있었다.**
                #
                # ★ **`attempt` 는 되먹임 쪽이 가진다.** 매입 `constraints.yaml` 의
                #   `attempt_max`(= `MAX_PURCHASE_ATTEMPTS` 인용)가 세는 것이 그쪽이라
                #   이름이 이미 그 뜻으로 쓰이고 있다. 옮기면 더 헷갈린다.
                "condition_seq": decision.decision_seq,
                "requested_by": request.decided_by,
                "origin_request_id": request.target_request_id,
            },
        )
    )
    linked = link_follow_up(decision_id=decision.decision_id, follow_up_request_id=follow_up_id)

    # ★ **답의 본체는 결정이 아니라 다시 만든 안이다.** 사용자가 *"다시 해줘"* 라고
    #   했으니 보고 싶은 것은 새 안이다 — 결정 기록은 그 위에 한 줄로 붙인다.
    passed_on = (
        f"조건 '{intent.condition}' 을 매입에 그대로 전달했습니다 — "
        "다만 매입은 아직 이 조건으로 안을 바꾸지 않습니다(재요청 표시로만 씁니다)"
    )
    unlinked = () if linked else ("이 결정에는 이미 후속 실행이 있어 링크를 잇지 않았습니다",)
    base = facts_from_procurement(rerun)
    facts = replace(
        base,
        facts=(
            *base.facts,
            Fact(label="조건 기록", value=f"{decision.decision_seq}회차 · {intent.condition}"),
            Fact(label="원 실행", value=request.target_request_id),
        ),
        gaps=(*base.gaps, passed_on, *unlinked),
    )
    rerun.report_text = render_answer(facts)
    return AskResponse(
        request_id=follow_up_id,
        as_of=request.as_of,
        outcome="DECISION_RECORDED",
        intent=intent,
        decision=decision.model_copy(update={"follow_up_request_id": follow_up_id}),
        run=rerun,
        answer=_rule_answer(facts),
        llm_status="SKIPPED_TEMPLATE",
    )


def _shown_note(as_of: date) -> str:
    """조회가 **어느 실행·기준일을 읽었나.** 재무 현금 그래프 문장과 같은 모양이다."""
    return f"보고 있는 실행: {SHOWN_SIM_RUN_ID} · 기준일: {as_of.isoformat()}"


def _item_of(request_id: str) -> str | None:
    """직전 실행의 품목. **없으면 비운다** — 지어내지 않는다."""
    try:
        history = get_run_history(request_id)
    except LookupError:
        return None
    item = (history.request_payload or {}).get("item")
    return item if isinstance(item, str) else None


def _rule_answer(facts: AnswerFacts) -> AnswerOut:
    """⑥ 없이 규칙만으로 만드는 답.

    ★ **머리말이 이미 완결된 판단 문장인 곳에는 ⑥ 을 얹지 않는다.**
      *"'기본' 안으로 진행합니다"* · *"매입안을 제시합니다"* 위에 한 문장을 더 쓰면
      **같은 말을 두 번** 한다(실측에서 그랬다). 조회만 머리말이 **머리글**
      (*"조회 결과 — 물류"*)이라 얹을 자리가 있다.
    """
    return AnswerOut(text=render_answer(facts), llm_status="SKIPPED_TEMPLATE")


def _write_answer(facts: AnswerFacts, narrator: NarrativeService | None) -> AnswerOut:
    """⑥ — 문장을 얹어 사람이 읽는 답을 만든다.

    ★ **문장 생성이 실패해도 답은 나간다.** `narrative=None` 이면 규칙이 만든 사실
      줄만으로 완결된다 — LLM 을 답의 뼈대로 쓰지 않는 것이 이 설계의 요지다.
    """
    if facts.markdown:
        # 🔴 **부서가 완결한 본문이 있으면 ⑥을 부르지 않는다.** 가격 예측의 마크다운은
        #   이미 사람에게 쓴 답이라, 문장을 얹으면 같은 말을 두 번 하거나 요약이 본문과
        #   어긋난다 — 매입 머리말에 ⑥을 안 붙이는 것과 같은 이유다.
        return AnswerOut(
            text=render_answer(facts),
            markdown=facts.markdown,
            llm_status="SKIPPED_TEMPLATE",
        )
    narrator = narrator or get_narrative_service()
    result = narrator.write(facts)
    return AnswerOut(
        text=render_answer(facts, result.narrative),
        narrative=result.narrative,
        llm_status=result.llm_status,
        llm_attempts=result.llm_attempts,
        llm_fallback_used=result.llm_fallback_used,
    )


def _to_answer(outcome: StatusOutcome) -> StatusAnswer:
    return StatusAnswer(
        status_code=outcome.status_code,
        reason=outcome.reason,
        answers={k: dict(v) for k, v in outcome.answers.items()},
        unavailable=list(outcome.unavailable),
        missing_data={k: list(v) for k, v in outcome.missing_data.items()},
        errors=dict(outcome.errors),
    )


def _response(
    request_id: str,
    request: AskRequest,
    result: IntentResult,
    *,
    outcome,
    confirm_required: bool = False,
    status: StatusAnswer | None = None,
    answer: AnswerOut | None = None,
    note: str | None = None,
    clarification: str | None = None,
    domain_result: DomainActionAnswer | None = None,
) -> AskResponse:
    return AskResponse(
        request_id=request_id,
        as_of=request.as_of,
        outcome=outcome,
        intent=result.intent,
        clarification=clarification if clarification is not None else result.clarification,
        confirm_required=confirm_required,
        status=status,
        answer=answer,
        domain_result=domain_result,
        llm_status=result.llm_status,
        llm_provider=result.llm_provider,
        llm_model=result.llm_model,
        llm_attempts=result.llm_attempts,
        llm_fallback_used=result.llm_fallback_used,
        note=note,
    )
