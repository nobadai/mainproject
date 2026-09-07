"""승인이 **이미 열린 다음 날들**에도 실린다.

🔴 **실측 2026-09-07** — `#343` 뒤에 새 승인을 냈는데 도착일에 입고가 안 됐다.

```text
2026-01-14   in_transit 2건   ← 전이가 여기 들어갔다 (target_state_date = 승인일 + 1)
2026-01-15   in_transit 1건   ← **도착일인데 새 것이 없다**
```

`in_transit` 은 **승인 ~ 도착 ~ 검수까지 여러 날에 걸쳐 유지되는 상태**이고(물류
`day_open.py`), 물류는 그것을 **하루 넘김의 carry-forward** 로 유지한다. 그런데 그
방식은 *"다음 날을 만들 때 전날에서 물려받는다"* 라서, **다음 날이 이미 열려 있으면
그 행은 이 승인을 모른 채 굳는다.**

★ **정방향에서는 안 생긴다** — 내일은 아직 없으니까. 다만 *"내일을 미리 열어 두고
  오늘 승인"* 은 있을 수 있는 순서이고, 아티팩트로 보고 덮으면 **그 순서가 실제로
  오는 날 도착분이 조용히 사라진다.**

---

🔴 **마스터가 물류 표를 읽지 않는다** (정의서 §3.2.5).

어느 날이 열렸는지는 `master_day_openings` 가 아는 **마스터 사실**이라 마스터가
답할 수 있다. 물류 fixture 를 뒤지지 않는다.

⚠️ **물류 코드도 안 고친다.** `InventoryTransition` 이 날짜를 행마다 들고 있고
  어댑터 `persist` 가 행마다 `persist_inventory` 를 부른다 — **묶음을 여러 개 주면
  되는 계약**이다. 그리고 `in_transit` 은 덮어쓰기가 아니라 **병합**이라 같은
  `inbound_id` 를 여러 날에 실어도 안전하다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Self

import pytest

from app.master import transition
from app.master.commitment import ApprovedCommitment, ArrivalLeg

AS_OF = date(2026, 1, 13)
다음날 = AS_OF + timedelta(days=1)


class _가짜커서:
    """`persist_purchases` 가 쓰는 최소 표면. **아무것도 안 한다.**"""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return

    def __init__(self) -> None:
        self._row: dict[str, str] | None = None
        self.rowcount = 1

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        # `items` 조회만 답한다 — `test_transition_boundary.py` 의 대역과 같은 모양이다.
        self._row = {"item_id": "ITEM-BAECHU"} if "FROM" in text and "items" in text else None

    def executemany(self, *_: Any, **__: Any) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class _가짜커넥션:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _가짜커서:
        return _가짜커서()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class _전이:
    """`build` 가 받은 날짜를 그대로 기록한다."""

    def __init__(self) -> None:
        self.dates: list[date] = []
        self.persisted: list[Any] = []

    def build(self, commitment: Any, *, target_state_date: date, **_: Any) -> tuple[Any, ...]:
        self.dates.append(target_state_date)
        return (f"row@{target_state_date}",)

    def persist(self, conn: Any, rows: Any) -> None:
        self.persisted.extend(rows if isinstance(rows, (list, tuple)) else [rows])


class _재무전이(_전이):
    def build(self, commitment: Any, *, target_state_date: date, **_: Any) -> Any:
        self.dates.append(target_state_date)
        return f"finance@{target_state_date}"

    def persist(self, conn: Any, row: Any) -> None:
        self.persisted.append(row)


def _commitment() -> ApprovedCommitment:
    return ApprovedCommitment(
        approval_id="H1-REQ-CARRY-1",
        request_id="REQ-CARRY",
        as_of=AS_OF,
        item="배추",
        scenario_label="기본",
        total_qty_kg=100.0,
        total_amount_krw=100000.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=100.0,
                arrival_date=AS_OF + timedelta(days=2),
                purchase_date=AS_OF,
                seq=1,
                payment_due_date=AS_OF,
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    before = dict(transition.registered())
    transition.reset()
    yield
    transition.reset()
    for part, impl in before.items():
        transition.register_transition(part, impl)


@pytest.fixture
def _배선() -> tuple[_재무전이, _전이]:
    재무, 물류 = _재무전이(), _전이()
    transition.register_transition("finance", 재무)
    transition.register_transition("logistics", 물류)
    return 재무, 물류


def _열린날(*days: date):
    def _fake(*, after: date, sim_run_id: str, connect: Any = None) -> tuple[date, ...]:
        return tuple(d for d in days if d > after)

    return _fake


# ---------------------------------------------------------------------------
# 1. 앞질러 열린 날이 없으면 — **아무것도 안 달라진다**
# ---------------------------------------------------------------------------


def test_정방향이면_다음날_하나뿐이다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ **비어 있는 것이 정상이다.** 내일은 아직 없다."""
    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날())

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.status == "APPLIED"
    assert 물류.dates == [다음날]
    assert out.carried_forward == []
    assert out.carried_forward_status == "OK", "읽었는데 못 읽은 것으로 나갔다"


# ---------------------------------------------------------------------------
# 2. 앞질러 열려 있으면 — **그 날들에도 싣는다**
# ---------------------------------------------------------------------------


def test_이미_열린_날들에도_같은_사실을_싣는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **이것이 없으면 도착일 행이 승인을 모른 채 굳는다.**"""
    _, 물류 = _배선
    나중 = [다음날 + timedelta(days=n) for n in (1, 2, 5)]
    monkeypatch.setattr(transition, "opened_days_after", _열린날(*나중))

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert 물류.dates == [다음날, *나중], "이미 열린 날에 안 실었다"
    assert out.carried_forward == 나중


def test_따라잡은_날을_결과에_적는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """★ **왜 하루가 여러 번 바뀌었는지**가 화면까지 가야 한다."""
    monkeypatch.setattr(transition, "opened_days_after", _열린날(다음날 + timedelta(days=1)))

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.carried_forward == [다음날 + timedelta(days=1)]


def test_묶음을_여러_개_주지_물류를_고치지_않는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """⚠️ 물류 `persist` 는 **행마다** `persist_inventory` 를 부른다 — 계약 그대로다."""
    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날(다음날 + timedelta(days=1)))

    transition.apply_approval(_commitment(), connect=_가짜커넥션)

    assert 물류.persisted == [f"row@{다음날}", f"row@{다음날 + timedelta(days=1)}"]


# ---------------------------------------------------------------------------
# 3. 재무는 안 건드린다
# ---------------------------------------------------------------------------


def test_재무는_다음날_하나만_받는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """🔴 **재무 상태의 다일 의미는 재무가 정한다.**

    `in_transit` 이 여러 날에 걸쳐 유지된다는 것은 **물류가 자기 파일에 적은 사실**
    이다. 재무 상태행도 같은 성격인지는 재무 몫이라, 마스터가 대신 넓히지 않는다 —
    `inspection_provider` 를 마스터가 안 고른 것과 같은 자리다.
    """
    재무, _ = _배선
    monkeypatch.setattr(transition, "opened_days_after", _열린날(다음날 + timedelta(days=1)))

    transition.apply_approval(_commitment(), connect=_가짜커넥션)

    assert 재무.dates == [다음날], "마스터가 재무 다일 의미를 대신 정했다"


# ---------------------------------------------------------------------------
# 4. 못 읽어도 승인은 선다
# ---------------------------------------------------------------------------


def test_정본을_못_읽어도_승인을_멈추지_않는다(
    monkeypatch: pytest.MonkeyPatch, _배선: tuple[_재무전이, _전이]
) -> None:
    """⚠️ `record_day_opening` 이 절대 raise 하지 않는 것과 같은 규율.

    ★ 다만 그때는 낡은 행이 남고, 그 사실은 `carried_forward` 가 **비어 있는 것**으로
      드러난다 — 조용히 성공한 것처럼 보이지 않는다.
    """

    def _못읽음(*, after: date, sim_run_id: str, connect: Any = None) -> None:
        return None

    _, 물류 = _배선
    monkeypatch.setattr(transition, "opened_days_after", _못읽음)

    out = transition.apply_approval(_commitment(), connect=_가짜커넥션)
    assert out.status == "APPLIED", out.reason

    assert out.status == "APPLIED"
    assert 물류.dates == [다음날]
    assert out.carried_forward == []
    assert out.carried_forward_status == "UNREADABLE", (
        "못 읽은 것이 '앞질러 열린 날이 없었다' 와 같은 문장으로 나갔다"
    )
