"""거래처 여신 현황 read model — **얼마까지 더 팔 수 있고, 언제 풀리는가.**

★ 재무 화면에는 채권 목록과 연체 구간은 있었지만, 사용자가 판매 전에 묻는 질문에
  답하는 자리가 없었다.

```text
여신한도는 얼마인가              partner_credit_limits (그날 유효한 한 행)
지금 미수금은 얼마인가            receivables + master_collection_events (기준일 복원)
얼마까지 더 팔 수 있는가          한도 − 미수금
언제쯤 여신이 풀리는가            미수 채권의 계약상 결제 예정일
```

🔴 **계산을 새로 짓지 않는다.** 판정 경로와 같은 함수를 부른다.

```text
여신한도        db.load_partner_credit_limit            (0원 ≠ 없음)
미수·연체 집계   tools.summarize_partner_receivables     (미회수 상태의 정의가 한 곳)
가용 여신       tools.calculate_available_credit        (음수를 0으로 깎지 않는다)
사용률          tools.calculate_credit_utilization_rate (한도 0원이면 None)
```

🔴 **채권은 기준일 시점으로 복원해 읽는다** (`receivable_history`). 저장된 수금 칸을
   그대로 읽으면 과거 화면에 미래 수금이 실려 여신이 실제보다 넉넉해 보인다.

🔴 **수금 예정은 예정이다.** 여기서 채권을 줄이거나 현금을 늘리지 않는다. 실제 감소는
   수금 사건으로만 일어난다. 그리고 **한도를 되돌려 주는 로직은 없다** — 여신은
   미수금이 실제로 줄어야만 풀린다.
"""

from datetime import date
from decimal import Decimal

from psycopg import sql
from pydantic import BaseModel, ConfigDict

from app.finance.db import fetch_all, get_db_schema, load_partner_credit_limit
from app.finance.receivable_history import history_columns, history_join, projected_status
from app.finance.sales_validation import PartnerReceivable
from app.finance.tools import (
    calculate_available_credit,
    calculate_credit_utilization_rate,
    summarize_partner_receivables,
)

#: 화면에 펼칠 수금 예정 수. 전부가 아니라 «다음 몇 번» 이 궁금한 자리다.
UPCOMING_COLLECTION_LIMIT = 5


class ConsoleCreditCollection(BaseModel):
    """계약상 결제 예정 한 건과, **그 돈이 들어온다면** 남는 여신."""

    model_config = ConfigDict(extra="forbid")

    due_date: date
    amount_krw: Decimal
    #: 결제 예정일이 기준일보다 앞선다 — 이미 받았어야 할 돈이다.
    overdue: bool
    #: 이 건까지 예정대로 들어온다면 남는 여신. 한도가 없으면 `None` 이다.
    available_credit_after_krw: Decimal | None


class ConsolePartnerCredit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_id: str
    partner_name: str | None
    #: 거래처 계약상 결제일수. 모르면 `None` 이다 (0일 결제와 다르다).
    payment_days: int | None
    #: 그날 유효한 여신한도. **`None` 은 한도가 정해지지 않았다는 뜻이고 0원이 아니다.**
    credit_limit_krw: Decimal | None
    credit_limit_evidence_grade: str | None
    current_ar_krw: Decimal
    overdue_ar_krw: Decimal
    open_receivable_count: int
    available_credit_krw: Decimal | None
    credit_utilization_rate: Decimal | None
    upcoming_collections: list[ConsoleCreditCollection]


class ConsoleCreditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sim_run_id: str
    as_of: date
    partners: list[ConsolePartnerCredit]


def load_credit_partner_rows(*, sim_run_id: str, as_of: date) -> list[dict[str, object]]:
    """여신을 보여 줄 거래처. **한도가 서 있거나, 이 실행에서 판 적이 있는 고객이다.**"""
    schema = sql.Identifier(get_db_schema())
    query = sql.SQL(
        """
        SELECT p.partner_id, p.partner_name, p.sales_collection_days,
               (
                   SELECT l.evidence_grade
                   FROM {schema}.partner_credit_limits l
                   WHERE l.partner_id = p.partner_id
                     AND l.is_active
                     AND l.effective_from <= %s
                     AND (l.effective_to IS NULL OR l.effective_to >= %s)
                   ORDER BY l.effective_from DESC
                   LIMIT 1
               ) AS credit_limit_evidence_grade
        FROM {schema}.partners p
        WHERE EXISTS (
                  SELECT 1 FROM {schema}.partner_credit_limits l
                  WHERE l.partner_id = p.partner_id
                    AND l.is_active
                    AND l.effective_from <= %s
                    AND (l.effective_to IS NULL OR l.effective_to >= %s)
              )
           OR EXISTS (
                  SELECT 1 FROM {schema}.sales s
                  WHERE s.sim_run_id = %s
                    AND s.customer_partner_id = p.partner_id
                    AND s.sale_date <= %s
              )
        ORDER BY p.partner_id
        """
    ).format(schema=schema)
    return fetch_all(query, [as_of, as_of, as_of, as_of, sim_run_id, as_of])


def load_partner_receivables_as_of(
    *, sim_run_id: str, as_of: date, partner_id: str
) -> list[PartnerReceivable]:
    """거래처 채권을 **기준일 시점으로 복원해** Finance 사실로 옮긴다.

    ★ 판매일과 발행일을 둘 다 기준일로 막는다 (`db._partner_receivable_rows` 와 같은
      이유). 상태는 저장값이 아니라 복원한 금액에서 다시 읽는다.
    """
    schema = get_db_schema()
    query = (
        sql.SQL(
            """
        SELECT r.receivable_id, r.due_date, r.original_amount_krw,
        """
        )
        + history_columns()
        + sql.SQL(
            """
        FROM {}.receivables r
        JOIN {}.sales s ON s.sale_id = r.sale_id AND s.sim_run_id = r.sim_run_id
        """
        ).format(sql.Identifier(schema), sql.Identifier(schema))
        + history_join(schema)
        + sql.SQL(
            """
        WHERE r.sim_run_id = %s
          AND s.customer_partner_id = %s
          AND r.issued_date <= %s
          AND s.sale_date <= %s
        ORDER BY r.due_date ASC, r.receivable_id ASC
        """
        )
    )
    #  ⚠️ `%s` 는 넷이다 — LATERAL 의 기준일이 WHERE 보다 **먼저** 온다.
    receivables: list[PartnerReceivable] = []
    for raw in fetch_all(query, [as_of, sim_run_id, partner_id, as_of, as_of]):
        original = Decimal(str(raw["original_amount_krw"]))
        received = Decimal(str(raw["received_amount_krw"]))
        outstanding = Decimal(str(raw["outstanding_amount_krw"]))
        receivables.append(
            PartnerReceivable(
                receivable_id=str(raw["receivable_id"]),
                due_date=raw["due_date"],
                # 🔴 초과 수금이 기록돼도 미수를 음수로 만들지 않는다 — 상태가 COLLECTED 다.
                outstanding_amount_krw=max(outstanding, Decimal(0)),
                status=projected_status(original_amount_krw=original, received_amount_krw=received),
                source_ref=str(raw["receivable_id"]),
            )
        )
    return receivables


def get_console_credit(*, sim_run_id: str, as_of: date) -> ConsoleCreditResponse:
    """거래처별 여신 현황. **판정하지 않는다 — 판매 판정은 재무 검증이 한다.**"""
    partners: list[ConsolePartnerCredit] = []
    for raw in load_credit_partner_rows(sim_run_id=sim_run_id, as_of=as_of):
        partner_id = str(raw["partner_id"])
        facts = summarize_partner_receivables(
            partner_id=partner_id,
            as_of=as_of,
            receivables=load_partner_receivables_as_of(
                sim_run_id=sim_run_id, as_of=as_of, partner_id=partner_id
            ),
        )
        limit = load_partner_credit_limit(as_of=as_of, partner_id=partner_id)
        available = (
            None
            if limit is None
            else calculate_available_credit(
                credit_limit_krw=limit, current_partner_ar_krw=facts.current_ar_krw
            )
        )
        utilization = (
            None
            if limit is None
            else calculate_credit_utilization_rate(
                current_partner_ar_krw=facts.current_ar_krw, credit_limit_krw=limit
            )
        )
        upcoming: list[ConsoleCreditCollection] = []
        freed = Decimal(0)
        for due in facts.open_receivable_schedule[:UPCOMING_COLLECTION_LIMIT]:
            freed += due.outstanding_amount_krw
            upcoming.append(
                ConsoleCreditCollection(
                    due_date=due.due_date,
                    amount_krw=due.outstanding_amount_krw,
                    overdue=due.due_date < as_of,
                    available_credit_after_krw=None if available is None else available + freed,
                )
            )
        days = raw.get("sales_collection_days")
        partners.append(
            ConsolePartnerCredit(
                partner_id=partner_id,
                partner_name=None if raw.get("partner_name") is None else str(raw["partner_name"]),
                payment_days=days if isinstance(days, int) and not isinstance(days, bool) else None,
                credit_limit_krw=limit,
                credit_limit_evidence_grade=(
                    None
                    if raw.get("credit_limit_evidence_grade") is None
                    else str(raw["credit_limit_evidence_grade"])
                ),
                current_ar_krw=facts.current_ar_krw,
                overdue_ar_krw=facts.overdue_ar_krw,
                open_receivable_count=facts.open_receivable_count,
                available_credit_krw=available,
                credit_utilization_rate=utilization,
                upcoming_collections=upcoming,
            )
        )
    return ConsoleCreditResponse(sim_run_id=sim_run_id, as_of=as_of, partners=partners)
