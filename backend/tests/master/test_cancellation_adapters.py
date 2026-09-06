"""취소 어댑터 둘 — **재무·물류가 실제로 물린다** (2026-09-06 · 2차).

★ 1차(`test_cancellation.py`)가 **경계**를 쟀다면 여기는 **양쪽 끝**을 잰다.

```text
마스터 Protocol   cancel(conn, *, commitment, cancelled_on, target_state_date, purchase_ids)
재무              cancel_finance_payables(conn, *, purchase_ids, as_of, target_state_date)
물류              withdraw_inventory(conn, *, sim_run_id, as_of, inbound_ids, source_ref)
```

🔴 **이름이 옮겨지는 자리가 두 곳이고 둘 다 틀리기 쉽다.**

```text
cancelled_on → as_of        commitment.as_of(승인일)를 넣으면 과거 상태를 고친다
approval_id  → inbound_id   한 글자만 달라도 아무것도 못 걷는데 조용히 성공한다
```
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.logistics.cancellation import (
    LogisticsCancellationAdapter,
    inbound_ids_of,
    withdraw_inventory,
)
from app.master.commitment import ApprovedCommitment, ArrivalLeg
from app.master.finance_cancellation import FinanceCancellationAdapter

APPROVED_ON = date(2026, 1, 5)
CANCELLED_ON = date(2026, 1, 7)
TARGET = date(2026, 1, 8)


def _commitment(*, legs: int = 1) -> ApprovedCommitment:
    schedule = tuple(
        ArrivalLeg(
            seq=n,
            item="배추",
            qty_kg=1000.0 * n,
            purchase_date=APPROVED_ON,
            arrival_date=date(2026, 1, 7),
        )
        for n in range(1, legs + 1)
    )
    return ApprovedCommitment(
        approval_id="H1-REQ-20260105-0001-1",
        request_id="REQ-20260105-0001",
        item="배추",
        as_of=APPROVED_ON,
        scenario_label="기본",
        total_qty_kg=sum(leg.qty_kg for leg in schedule) or 0.0,
        total_amount_krw=1_000_000.0,
        inbound_lead_days=2,
        arrival_schedule=schedule,
    )


# ── ① 물류 — inbound_id 가 넣는 쪽과 같아야 한다 ──────────────────────────


def test_걷는_id_가_넣는_id_와_같다():
    """🔴 **한 글자만 달라도 아무것도 못 걷는데 조용히 성공한다.**

    ★ 넣는 쪽(`transition.build_next_inventory`)과 빼는 쪽(`inbound_ids_of`)을 여기서
      직접 대조한다 — 두 규칙이 갈리는 날 이 검사가 운다.
    """
    from app.logistics.transition import build_next_inventory

    commitment = _commitment(legs=2)
    넣은것 = [item.inbound_id for item in build_next_inventory(commitment)]

    assert list(inbound_ids_of(commitment)) == 넣은것


# ── ② 물류 — 걷는다. 남의 것은 안 건드린다 ────────────────────────────────


class _가짜커서:
    def __init__(self, row: tuple[Any, Any] | None) -> None:
        self._row = row
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 1

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        self.executed.append((str(query), params))

    def fetchone(self) -> Any:
        return self._row


class _가짜커넥션:
    def __init__(self, row: tuple[Any, Any] | None) -> None:
        self.cur = _가짜커서(row)

    def cursor(self) -> Any:
        return self.cur


def _row(in_transit: list[dict], confirmed: list[dict]) -> tuple[Any, Any]:
    return (in_transit, confirmed)


def _쓴값(conn: _가짜커넥션) -> tuple[Any, str, Any, str]:
    """UPDATE 에 실린 두 목록과 두 상태."""
    _, params = conn.cur.executed[-1]
    return params[0].obj, params[1], params[2].obj, params[3]


MINE = {"inbound_id": "INB-H1-REQ-20260105-0001-1-1", "item": "배추", "quantity_kg": "1000.0"}
OTHER = {"inbound_id": "INB-H1-OTHER-9", "item": "무", "quantity_kg": "500.0"}


def test_내_승인분만_걷는다():
    """🔴 **목록을 통째로 새로 쓰지 않는다** — 남의 승인분이 사라진다."""
    conn = _가짜커넥션(_row([MINE, OTHER], [MINE, OTHER]))

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE["inbound_id"]], source_ref="X"
    )

    in_transit, in_status, confirmed, conf_status = _쓴값(conn)
    assert in_transit == [OTHER]
    assert confirmed == [OTHER]
    assert in_status == "CONFIRMED"
    assert conf_status == "CONFIRMED"
    assert removed == 2, "두 목록에서 하나씩 빠져야 한다"


def test_다_걷히면_CONFIRMED_ZERO_다():
    """🔴 **`UNRESOLVED` 가 아니다.** 취소는 *"확인했고 이제 없다"* 이지
    *"모른다"* 가 아니다."""
    conn = _가짜커넥션(_row([MINE], [MINE]))

    withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE["inbound_id"]], source_ref="X"
    )

    _, in_status, confirmed, conf_status = _쓴값(conn)
    assert confirmed == []
    assert in_status == "CONFIRMED_ZERO"
    assert conf_status == "CONFIRMED_ZERO"


def test_이미_걷힌_뒤_재시도는_0이다():
    """★ 재무 `#302` 의 *"retry no-op"* 과 같은 모양이다."""
    conn = _가짜커넥션(_row([OTHER], [OTHER]))

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE["inbound_id"]], source_ref="X"
    )

    assert removed == 0


def test_inbound_id_가_없는_항목은_안_건드린다():
    """★ 물류가 다른 경로로 넣은 것일 수 있다 — **마스터가 만들지 않은 것을 지우지
    않는다.**"""
    익명 = {"item": "양파", "quantity_kg": "100.0"}
    conn = _가짜커넥션(_row([MINE, 익명], [익명]))

    withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE["inbound_id"]], source_ref="X"
    )

    in_transit, _, confirmed, _ = _쓴값(conn)
    assert in_transit == [익명]
    assert confirmed == [익명]


def test_걷을_것이_없으면_DB_를_안_친다():
    """★ 회차 일정이 없던 약정도 승인은 살아 있다."""
    conn = _가짜커넥션(_row([MINE], [MINE]))

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[], source_ref="X"
    )

    assert removed == 0
    assert not conn.cur.executed


def test_그날_행이_없으면_만들지_않는다():
    from app.logistics.transition import LogisticsFixtureMissing

    conn = _가짜커넥션(None)

    with pytest.raises(LogisticsFixtureMissing):
        withdraw_inventory(
            conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=["INB-X"], source_ref="X"
        )


def test_행을_잠그고_읽는다():
    """🔴 `FOR UPDATE` 가 없으면 두 취소가 같은 옛 목록을 읽고 마지막이 이긴다."""
    conn = _가짜커넥션(_row([MINE], [MINE]))

    withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE["inbound_id"]], source_ref="X"
    )

    select_sql, _ = conn.cur.executed[0]
    assert "FOR UPDATE" in select_sql


# ── ③ 물류 어댑터 — 취소일이 아니라 상태일 행에서 걷는다 ──────────────────


def test_물류가_target_state_date_행에서_걷는다():
    """🔴 **승인이 쓴 행이 아니다.** 승인 01-05 → 01-06 행에 적었고, 취소 01-07 →
    01-08 행에서 걷는다. 그 사이 날들은 **그대로 둔다** — 그때는 실제로 오는 중이었다.
    """
    conn = _가짜커넥션(_row([MINE], [MINE]))

    LogisticsCancellationAdapter(sim_run_id="SIM-1").cancel(
        conn,
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={1: "PUR-X-S1"},
    )

    _, params = conn.cur.executed[0]
    assert params[1] == TARGET, f"상태일 행이 아니라 {params[1]} 을 잠갔다"
    assert params[1] != APPROVED_ON


def test_물류_source_ref_가_취소임을_말한다():
    conn = _가짜커넥션(_row([MINE], [MINE]))

    LogisticsCancellationAdapter(sim_run_id="SIM-1").cancel(
        conn,
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={},
    )

    _, params = conn.cur.executed[-1]
    assert "MASTER-CANCEL" in params[4]
    assert CANCELLED_ON.isoformat() in params[4]


# ── ④ 재무 어댑터 — 이름 하나를 옮긴다 ────────────────────────────────────


def test_재무에_취소일을_as_of_로_넘긴다(monkeypatch: pytest.MonkeyPatch):
    """🔴 **`commitment.as_of`(승인일)를 넣으면 과거 상태를 고친다.**

    재무가 직접 짚어 준 자리다 (회신 2026-09-06 §4) —
    *"`ApprovedCommitment.as_of` 는 원 승인일이므로 취소일로 사용하지 않습니다."*
    """
    받은것: dict[str, Any] = {}

    def 가짜(conn: Any, **kw: Any) -> None:
        받은것.update(kw)

    monkeypatch.setattr("app.master.finance_cancellation.cancel_finance_payables", 가짜)

    FinanceCancellationAdapter().cancel(
        object(),
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={1: "PUR-A-S1"},
    )

    assert 받은것["as_of"] == CANCELLED_ON
    assert 받은것["as_of"] != APPROVED_ON
    assert 받은것["target_state_date"] == TARGET


def test_재무에_회차_순서대로_id_를_넘긴다(monkeypatch: pytest.MonkeyPatch):
    """★ 재무는 `Sequence[str]` 를 받고 `seq` 를 안 본다 — **순서가 유일한 단서**다."""
    받은것: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.master.finance_cancellation.cancel_finance_payables",
        lambda conn, **kw: 받은것.update(kw),
    )

    FinanceCancellationAdapter().cancel(
        object(),
        commitment=_commitment(legs=2),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={2: "PUR-A-S2", 1: "PUR-A-S1"},
    )

    assert 받은것["purchase_ids"] == ["PUR-A-S1", "PUR-A-S2"]


def test_회차가_없으면_재무를_안_부른다(monkeypatch: pytest.MonkeyPatch):
    """★ 빈 목록을 넘기면 재무가 *"요청 집합이 비었다"* 를 판단할 자리를 만들게 된다."""
    불렸나 = []
    monkeypatch.setattr(
        "app.master.finance_cancellation.cancel_finance_payables",
        lambda conn, **kw: 불렸나.append(kw),
    )

    FinanceCancellationAdapter().cancel(
        object(),
        commitment=_commitment(legs=0),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={},
    )

    assert not 불렸나


# ── ⑤ 배선 — 둘 다 등록된다 ───────────────────────────────────────────────


def test_main_이_두_파트를_다_등록한다():
    """🔴 **하나만 등록되면 `undo_approval` 이 `NOT_APPLIED` 로 접힌다.**

    ⚠️ 전역 등록소를 읽지 않는다 — 다른 검사가 `reset()` 을 하면 순서에 따라 답이
      달라진다. **배선 원문**을 읽어 두 줄이 다 있는지 본다.
    """
    import pathlib

    import app.main

    원문 = pathlib.Path(app.main.__file__).read_text(encoding="utf-8")

    assert 'register_cancellation("finance"' in 원문
    assert 'register_cancellation("logistics"' in 원문
    assert "BURN_IN_SIM_RUN_ID" in 원문.split("register_cancellation(\"logistics\"")[1][:120]
