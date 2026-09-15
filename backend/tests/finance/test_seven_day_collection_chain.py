"""7일 결제 채권이 **기존 걷기 수금 구조**를 따라 8일째 들어오고 여신이 풀리는가.

★ 새 자동 수금을 만들지 않는다. 마스터의 `seed_collection_events` (SIM_FIXED · 결제기일이
  지난 미수 채권 → 수금 사건)를 **고치지 않고 그대로 부르고**, 재무의
  `build_collection_transition` 으로 그 사건을 반영한다.

```text
Day 1 (01-15)  판매 확정 · 결제일수 7 → 만기 01-22 · 미수 3,000,000 · 남은 여신 7,000,000
Day 7 (01-21)  만기 전 → 수금 사건 0건 · 미수 그대로
Day 8 (01-22)  만기 도래 → 수금 사건 1건 (누적 target = 원금)
               재무 반영 → 미수 0 · 현금 +3,000,000 · 남은 여신 10,000,000
```

⚠️ 가짜 커서는 마스터 SQL 을 **흉내 내지 않는다.** 그 SQL 에 만기 조건과 미수 조건이 실제로
  들어 있는지 먼저 확인하고, 확인한 조건만 그대로 적용한다. 조건이 바뀌면 이 검사가 먼저 깨진다.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.finance.collection import build_collection_transition
from app.finance.sales_validation import PartnerReceivable
from app.finance.tools import calculate_available_credit, summarize_partner_receivables
from app.master.collection_seed import seed_collection_events

SIM = "SIM-7DAY-CHAIN"
LIMIT = Decimal(10_000_000)
SALE_DATE = date(2026, 1, 15)
PAYMENT_DAYS = 7


class _Cursor:
    def __init__(self, ledger: dict[str, dict], events: list[dict]):
        self.ledger = ledger
        self.events = events
        self.rowcount = 0
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        text = query.as_string(None)
        if text.startswith("SELECT"):
            assert "due_date <= %s" in text
            assert "outstanding_amount_krw > 0" in text
            sim_run_id, as_of = params
            self._rows = [
                {
                    "receivable_id": row["receivable_id"],
                    "due_date": row["due_date"],
                    "original_amount_krw": row["original_amount_krw"],
                    "received_amount_krw": row["received_amount_krw"],
                }
                for row in self.ledger.values()
                if row["sim_run_id"] == sim_run_id
                and row["due_date"] <= as_of
                and row["outstanding_amount_krw"] > 0
            ]
            return
        assert text.startswith("INSERT")
        sim_run_id, financing_mode, collection_date, receivable_id, target, _note = params
        key = (sim_run_id, collection_date, receivable_id)
        if any(
            (e["sim_run_id"], e["collection_date"], e["receivable_id"]) == key for e in self.events
        ):
            self.rowcount = 0
            return
        self.events.append(
            {
                "sim_run_id": sim_run_id,
                "financing_mode": financing_mode,
                "collection_date": collection_date,
                "receivable_id": receivable_id,
                "target_received_total_krw": target,
            }
        )
        self.rowcount = 1

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, ledger, events):
        self.ledger = ledger
        self.events = events

    def cursor(self):
        return _Cursor(self.ledger, self.events)


def _available(ledger: dict[str, dict], as_of: date) -> tuple[Decimal, Decimal]:
    facts = summarize_partner_receivables(
        partner_id="KIMCHI_FACTORY_001",
        as_of=as_of,
        receivables=[
            PartnerReceivable(
                receivable_id=row["receivable_id"],
                due_date=row["due_date"],
                outstanding_amount_krw=row["outstanding_amount_krw"],
                status=row["status"],
                source_ref=row["receivable_id"],
            )
            for row in ledger.values()
        ],
    )
    return facts.current_ar_krw, calculate_available_credit(
        credit_limit_krw=LIMIT, current_partner_ar_krw=facts.current_ar_krw
    )


def test_a_seven_day_receivable_is_collected_on_day_eight_and_frees_the_credit():
    due = SALE_DATE + timedelta(days=PAYMENT_DAYS)
    ledger = {
        "AR-SALE-7": {
            "receivable_id": "AR-SALE-7",
            "sim_run_id": SIM,
            "due_date": due,
            "original_amount_krw": Decimal(3_000_000),
            "received_amount_krw": Decimal(0),
            "outstanding_amount_krw": Decimal(3_000_000),
            "status": "OPEN",
        }
    }
    state = {
        "finance_state_id": "FIN-7DAY",
        "sim_run_id": SIM,
        "current_cash_krw": Decimal(2_000_000),
        "receivables_krw": Decimal(3_000_000),
    }
    events: list[dict] = []
    conn = _Connection(ledger, events)

    # Day 1 — 판매 확정 직후
    assert due == date(2026, 1, 22)
    assert _available(ledger, SALE_DATE) == (Decimal(3_000_000), Decimal(7_000_000))

    # Day 7 — 만기 전: 수금 사건이 서지 않는다 (기일 전 자동 수금 없음)
    before_due = seed_collection_events(
        conn, sim_run_id=SIM, financing_mode="LOAN_BASELINE", as_of=due - timedelta(days=1)
    )
    assert (before_due.created, before_due.skipped) == (0, 0)
    assert _available(ledger, due - timedelta(days=1)) == (Decimal(3_000_000), Decimal(7_000_000))

    # Day 8 — 만기 도래: 기존 마스터 구조가 사건을 세운다
    on_due = seed_collection_events(conn, sim_run_id=SIM, financing_mode="LOAN_BASELINE", as_of=due)
    assert on_due.created == 1
    assert events[0]["collection_date"] == due
    assert events[0]["target_received_total_krw"] == Decimal(3_000_000)

    # 재무 반영 — 미수 감소 · 현금 증가 · 여신 복구
    plan = build_collection_transition(
        ledger["AR-SALE-7"], state, target_received_total_krw=events[0]["target_received_total_krw"]
    )
    ledger["AR-SALE-7"].update(
        received_amount_krw=plan.target_received_total_krw,
        outstanding_amount_krw=plan.next_outstanding_amount_krw,
        status=plan.next_status,
    )

    assert plan.delta_received_krw == Decimal(3_000_000)
    assert plan.next_current_cash_krw == Decimal(5_000_000)
    assert plan.next_receivables_krw == Decimal(0)
    assert _available(ledger, due) == (Decimal(0), LIMIT)

    # 같은 날 다시 걸어도 두 번 받지 않는다
    again = seed_collection_events(conn, sim_run_id=SIM, financing_mode="LOAN_BASELINE", as_of=due)
    assert again.created == 0


def test_the_thirty_day_baseline_is_still_open_on_day_eight():
    """비교 기준: 같은 판매가 30일 결제면 8일째에는 여신이 안 풀린다."""
    ledger = {
        "AR-SALE-30": {
            "receivable_id": "AR-SALE-30",
            "sim_run_id": SIM,
            "due_date": SALE_DATE + timedelta(days=30),
            "original_amount_krw": Decimal(3_000_000),
            "received_amount_krw": Decimal(0),
            "outstanding_amount_krw": Decimal(3_000_000),
            "status": "OPEN",
        }
    }
    events: list[dict] = []
    day_eight = SALE_DATE + timedelta(days=PAYMENT_DAYS)

    result = seed_collection_events(
        _Connection(ledger, events), sim_run_id=SIM, financing_mode="LOAN_BASELINE", as_of=day_eight
    )

    assert result.created == 0
    assert _available(ledger, day_eight) == (Decimal(3_000_000), Decimal(7_000_000))
