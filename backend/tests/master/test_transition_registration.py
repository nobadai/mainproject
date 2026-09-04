"""전이 어댑터가 **실제로 등록되는가** (C 형태 ⑦ 배선).

🔴 **`register_transition` 호출이 0건이었다.** 재무 어댑터도 물류 어댑터도 서 있는데
   등록하는 줄이 어디에도 없어서, `apply_approval` 이 매 승인마다
   *"상태전이 미등록"* 으로 돌아섰다 — 사람이 승인해도 장부가 안 바뀌었다.

★ **가짜 어댑터로는 이 자리를 못 잰다.** `test_transition_boundary.py` 는 대역을 직접
  등록해 순서를 재므로, 배선이 통째로 빠져 있어도 초록불이다. 여기서는 `app.main` 이
  import 시점에 등록한 **실제 구현**으로 잰다.

🔴 **DB 는 부르지 않는다.** 재무 build 가 읽는 두 함수만 대역으로 갈아 끼우고, write 는
   가짜 커넥션이 받는다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Self

import pytest

import app.main  # noqa: F401  — import 시점에 두 전이를 등록한다. 이 검사의 전제다
from app.finance import transition as finance_transition
from app.logistics.transition import LogisticsTransitionAdapter
from app.master import transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

AS_OF = date(2025, 12, 31)
TARGET_STATE_DATE = AS_OF + timedelta(days=1)


def _commitment() -> ApprovedCommitment:
    return ApprovedCommitment(
        approval_id="H1-REQ-1-1",
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=44.0,
        total_amount_krw=228800.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=44.0,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
                payment_due_date=AS_OF,
            ),
        ),
        inbound_lead_days=2.0,
    )


class 가짜커서:
    """읽기 둘만 답하고 나머지 SQL 은 **파라미터째로 기록한다.**

    ★ **물류 fixture 읽기가 둘째다.** `logistics/transition.py` 의 `persist_inventory`
      는 `confirmed_inbound` 를 덮지 않고 **더하려고** 기존 목록을 먼저 읽는다 —
      여기서 빈손을 주면 그 행이 없다는 뜻이 되어 전이가 `FAILED` 로 선다.
      **그 병합은 임시 조치다** (물류 모듈 docstring 참조) — 걷어낼 때 이 가지도
      같이 걷는다.
    """

    def __init__(self, executed: list[tuple[str, Any]]) -> None:
        self.rowcount = 1
        self._executed = executed
        self._row: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self._executed.append((text, params))
        if "confirmed_inbound_json" in text and "SELECT" in text:
            # ★ 이미 확정된 입고가 없는 그날 행이다 — 확인했고 0 건.
            self._row = {"confirmed_inbound_json": []}
        elif "FROM" in text and "items" in text:
            self._row = {"item_id": "ITEM-BAECHU"}
        else:
            self._row = None

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class 가짜커넥션:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> 가짜커서:
        return 가짜커서(self.executed)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def 재무_읽기를_대역으로(monkeypatch: pytest.MonkeyPatch) -> None:
    """재무 build 가 읽는 두 자리만 갈아 끼운다. **DB 를 열지 않는다.**"""

    class _정책:
        purchase_payment_days = 0

    monkeypatch.setattr(
        finance_transition,
        "load_finance_state_row",
        lambda as_of: {
            "state_date": AS_OF,
            "sim_run_id": BURN_IN_SIM_RUN_ID,
            "financing_mode": "LOAN_BASELINE",
            "finance_state_id": "FIN-BURNIN-DAY30",
            "unsettled_purchase_payables_krw": Decimal(0),
        },
    )
    monkeypatch.setattr(finance_transition, "get_active_finance_policy", lambda: _정책())


# ── ⑥ 등록이 서 있다 ────────────────────────────────────────────────────


def test_두_전이가_다_등록되어_있다() -> None:
    """⑥ 🔴 `missing()` 이 **빈 튜플**이다 — 하나라도 비면 승인이 장부를 못 바꾼다."""
    assert transition.missing() == (), (
        "전이가 미등록이다 — app/main.py 의 register_transition 두 줄을 확인하라"
    )


def test_물류_자리에_물류_어댑터가_앉아_있다() -> None:
    """★ 이름만 채운 것이 아니라 **물류가 소유한 구현**이 앉아야 한다."""
    registered = transition.registered()

    assert isinstance(registered["logistics"], LogisticsTransitionAdapter)
    assert isinstance(registered["finance"], finance_transition.FinanceTransitionAdapter)


def test_물류_어댑터가_마스터가_소유한_sim_run_id_를_받았다() -> None:
    """🔴 실행 정체성은 **마스터가 정한다.** 물류 모듈에 상수로 박으면 실행이 둘이
    되는 날 물류 코드를 고쳐야 한다.

    ★ 값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID` 하나다 — 매입 원장
      (`ledger.sim_run_id_for`)이 가리키는 것과 같은 값이어야 재무 채무·매입 원장·
      재고 예정이 한 실행에 앉는다.
    """
    from app.master import ledger

    adapter = transition.registered()["logistics"]

    assert adapter._sim_run_id == BURN_IN_SIM_RUN_ID
    assert adapter._sim_run_id == ledger.sim_run_id_for(_commitment())


# ── ⑦ 등록된 실제 구현으로 승인 한 건이 통과한다 ────────────────────────


def test_승인이_두_파트를_다_거쳐_한_번_커밋한다(재무_읽기를_대역으로) -> None:
    """⑦ **대역이 아닌 실제 등록**으로 `apply_approval` 을 끝까지 돌린다.

    ★ 여기까지 와야 *"어댑터가 있다"* 가 *"승인이 장부를 바꾼다"* 가 된다.
    """
    conn = 가짜커넥션()

    out = transition.apply_approval(_commitment(), connect=lambda: conn)

    assert out.status == "APPLIED", out.reason
    assert out.parts == ["finance", "logistics"]
    assert conn.commits == 1, "커밋은 세 write 가 끝난 뒤 한 번이다"
    assert conn.rollbacks == 0
    assert conn.closed == 1


def test_세_장부가_한_커넥션으로_다_쓰인다(재무_읽기를_대역으로) -> None:
    """★ 매입 원장 · 재무 채무 · 물류 입고 예정 셋이 다 나가야 한다."""
    conn = 가짜커넥션()

    transition.apply_approval(_commitment(), connect=lambda: conn)

    문장 = [text for text, _ in conn.executed]
    assert any("INSERT INTO" in t and "purchases" in t for t in 문장), "매입 원장이 안 나갔다"
    assert any("INSERT INTO" in t and "payables" in t for t in 문장), "재무 채무가 안 나갔다"
    assert any("logistics_runtime_fixture" in t for t in 문장), "물류 입고 예정이 안 나갔다"


def test_물류_write_가_상태가_설_날의_행을_고른다(재무_읽기를_대역으로) -> None:
    """🔴 **하루 어긋나면 재무 상태와 다른 날에 앉는다.**

    재무는 `target_state_date` 로 `finance_states` 를 세운다. 물류가 승인일 행을
    고치면 두 장부가 다른 날의 사실이 되고, 어느 쪽이 그날인지 아무도 말해 주지 않는다.
    """
    conn = 가짜커넥션()

    transition.apply_approval(_commitment(), connect=lambda: conn)

    물류 = [params for text, params in conn.executed if "logistics_runtime_fixture" in text]
    # ★ 읽기 하나 · 쓰기 하나다 — 물류가 `confirmed_inbound` 를 덮지 않고 더하려고
    #   기존 목록을 먼저 읽는다. **둘이 같은 날 행을 가리켜야 한다.**
    assert len(물류) == 2
    for params in 물류:
        assert BURN_IN_SIM_RUN_ID in params
        assert TARGET_STATE_DATE in params
        assert AS_OF not in params, "승인일 행을 짚었다 — 재무 상태와 하루 어긋난다"
