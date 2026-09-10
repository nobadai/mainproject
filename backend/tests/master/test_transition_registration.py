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
from app.finance.day_open import FinanceDayOpening
from app.logistics.day_open import LogisticsDayOpening
from app.logistics.transition import LogisticsTransitionAdapter
from app.master import day_open, transition
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


#: 물류가 `_record_schedules` 에서 읽는 매입 줄 하나. **승인이 방금 만든 그 줄이다.**
#
# 🔴 **여기서 매입 줄을 지어내는 것이 아니다.** 같은 트랜잭션 안에서
#    `persist_purchases` 가 이미 `purchase_items` 를 썼고, 물류는 그것을
#    `purchase_id` 로 되읽는다 (`app/master/transition.py` 의 호출 순서 주석).
#    가짜 커넥션은 방금 쓴 것을 기억하지 않으므로, 그 한 줄을 여기서 답한다.
매입줄 = {
    "purchase_item_id": "PI-BURNIN-1",
    "item_id": "ITEM-BAECHU",
    "grade": None,
    "quantity_kg": Decimal("44.0"),
    "unit_price_krw_per_kg": Decimal(5200),
}


class 가짜커서:
    """읽기 넷만 답하고 나머지 SQL 은 **파라미터째로 기록한다.**

    ★ **답하는 자리가 바뀌었다** (물류 `#484` · W3-3 · 2026-09-10).

    ```text
    ① ~2026-09-09   confirmed_inbound_json 을 SELECT 로 읽어 병합했다 (임시 조치)
    ② 2026-09-10~   fixture 행은 **잠그기만** 하고(SELECT fixture_id … FOR UPDATE),
                    업무 사실은 신규 inbound_schedules 에 적는다
    ```

    ⚠️ **재는 것은 그대로다** — *"세 장부가 한 커넥션으로 한 번에 쓰이는가"*. 여기서
      바뀐 것은 대역이 답해야 하는 **질문의 목록**뿐이다.
    """

    def __init__(self, executed: list[tuple[str, Any]]) -> None:
        self.rowcount = 1
        self._executed = executed
        self._row: dict[str, Any] | None = None
        self._rows: list[Any] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self._executed.append((text, params))
        self._row = None
        self._rows = []
        if "logistics_runtime_fixture" in text and "SELECT" in text:
            # ★ 그날 fixture 행은 **있다.** 물류는 그 행을 잠그기만 하고, 없으면
            #   `LogisticsFixtureMissing` 으로 멈춘다 — 만들지 않는 것이 계약이다.
            self._row = {"fixture_id": "FX-BURNIN-1"}
        elif "purchase_items" in text and "SELECT" in text:
            # ★ 승인이 같은 트랜잭션에서 방금 쓴 매입 줄이다. **한 줄이다** —
            #   둘을 주면 물류가 `PurchaseDetailAmbiguous` 로 멈춘다.
            self._rows = [매입줄]
        elif "inbound_schedules" in text and "SELECT" in text:
            # ★ 아직 그 입고 일정이 없다 — 이번 승인이 처음 적는다. 빈 목록이 사실이다.
            self._rows = []
        elif "FROM" in text and "items" in text:
            self._row = {"item_id": "ITEM-BAECHU"}

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

    def fetchall(self) -> list[Any]:
        """목록으로 답하는 조회들. **빈 목록도 확인된 사실이다.**

        🔴 **`#458` 로 재무 전이가 재고 장부가를 원장에서 파생하기 시작했다**
          (`finance/db.load_inventory_snapshot_as_of`). 그 전에는 `finance_state` 에
          실린 값을 그대로 옮겼다 — *"수량 정본은 이동 원장이고 현재 잔량은 과거
          계산에 쓰지 않는다"* 가 그 함수의 규율이다.

        ★ **여기서 로트를 지어내지 않는다.** 이 검사가 재는 것은 *"세 장부가 한
          커넥션으로 한 번에 쓰이는가"* 이지 재고 평가가 아니다. 로트를 넣으면
          이 검사가 재고 계산까지 떠안게 되고, 그 계산이 바뀌는 날 여기가 빨개진다.

        ⚠️ 빈 목록은 *"그날까지 입고된 로트가 없다"* 는 **확인된 사실**이다.
        """
        return self._rows


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


def test_두_하루넘김이_다_등록되어_있다() -> None:
    """🔴 **다른 등록소다.** 전이가 다 서 있어도 하루 넘김이 비면 승인 없는 날 다음이 막힌다.

    ⚠️ 재무를 켜기 전에 `database/finance/finance_state_daily_unique.sql` 이 실 DB 에
       먼저 서야 한다. `#285` 의 `ON CONFLICT (sim_run_id, financing_mode, state_date)`
       가 그 UNIQUE 를 가리키고, 없으면 승인 전이가 거기서 터진다 (2026-09-05 실측).
    """
    assert day_open.missing() == (), (
        "하루 넘김이 미등록이다 — app/main.py 의 register_day_opening 두 줄을 확인하라"
    )


def test_하루넘김_자리에_각_파트_구현이_앉아_있다() -> None:
    """★ 이름만 채운 것이 아니라 **그 파트가 소유한 구현**이 앉아야 한다."""
    registered = day_open.registered()

    assert isinstance(registered["logistics"], LogisticsDayOpening)
    assert isinstance(registered["finance"], FinanceDayOpening)


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
    # ② **하나로 바뀌었다** (물류 `#484` · 2026-09-10). 전에는 `apply_approval` 이
    #    트랜잭션 밖에서 개장 정본을 한 번 더 읽어(`opened_days_after`) 같은 대역이
    #    두 번 닫혔다. 전파가 없어져 그 읽기가 사라졌다.
    #    지키는 것은 **write 가 한 트랜잭션**이고, 위 두 줄이 그것을 잰다.
    assert conn.closed == 1


def test_세_장부가_한_커넥션으로_다_쓰인다(재무_읽기를_대역으로) -> None:
    """★ 매입 원장 · 재무 채무 · 물류 입고 예정 셋이 다 나가야 한다."""
    conn = 가짜커넥션()

    transition.apply_approval(_commitment(), connect=lambda: conn)

    문장 = [text for text, _ in conn.executed]
    assert any("INSERT INTO" in t and "purchases" in t for t in 문장), "매입 원장이 안 나갔다"
    assert any("INSERT INTO" in t and "payables" in t for t in 문장), "재무 채무가 안 나갔다"
    assert any("logistics_runtime_fixture" in t for t in 문장), "물류 fixture 머리말이 안 섰다"
    # 🔴 **입고 예정의 정본은 이제 여기다** (물류 `#484` · W3-3 · 2026-09-10).
    #    fixture 행에는 표시 둘만 남았다 — 그것만 보면 *"업무 사실이 나갔다"* 를
    #    머리말로 재는 셈이 된다.
    assert any("INSERT INTO" in t and "inbound_schedules" in t for t in 문장), (
        "물류 입고 예정이 안 나갔다"
    )


def test_물류_write_가_상태가_설_날의_행을_고른다(재무_읽기를_대역으로) -> None:
    """🔴 **하루 어긋나면 재무 상태와 다른 날에 앉는다.**

    재무는 `target_state_date` 로 `finance_states` 를 세운다. 물류가 승인일 행을
    고치면 두 장부가 다른 날의 사실이 되고, 어느 쪽이 그날인지 아무도 말해 주지 않는다.
    """
    conn = 가짜커넥션()

    transition.apply_approval(_commitment(), connect=lambda: conn)

    물류 = [params for text, params in conn.executed if "logistics_runtime_fixture" in text]
    # ★ 읽기 하나 · 쓰기 하나다 — 물류가 그 행을 **잠그고**(FOR UPDATE) 고친다.
    #   ① 재는 것은 그대로다. 전에는 `confirmed_inbound` 목록을 병합하려고 읽었고
    #   (임시 조치), `#484` 뒤로는 잠그기만 한다. **둘이 같은 날 행을 가리켜야 한다.**
    assert len(물류) == 2
    for params in 물류:
        assert BURN_IN_SIM_RUN_ID in params
        assert TARGET_STATE_DATE in params
        assert AS_OF not in params, "승인일 행을 짚었다 — 재무 상태와 하루 어긋난다"
