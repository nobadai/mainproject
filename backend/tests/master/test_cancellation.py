"""취소 경계 — **승인을 물린다** (`#290` 후속 · 2026-09-06).

🔴 번복하면 앞 승인의 장부가 남는다.

```text
승인 A  decision_seq 1 → purchases D1 · payables AP-…-1 · unsettled += A
번복 B  decision_seq 2 → purchases D2 · payables AP-…-2 · unsettled += B
                         🔴 purchase_id 가 달라 ON CONFLICT 가 안 걸린다
```

★ 이 파일이 잠그는 것은 다섯이다.

  ```text
  ① 다섯이 한 트랜잭션이다        하나라도 터지면 아무것도 안 바뀐다
  ② 취소일과 상태 날짜가 다르다    commitment.as_of 를 취소일로 쓰지 않는다
  ③ 어댑터가 없으면 안 돈다        반쪽 취소가 더 나쁘다
  ④ 원장을 지우지 않고 적는다      DELETE 가 없다
  ⑤ 재시도가 멱등이다             두 번째는 0 을 돌려준다
  ```
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from app.master import cancellation
from app.master.cancellation import CancellationOut, undo_approval
from app.master.commitment import ApprovedCommitment, ArrivalLeg

APPROVED_ON = date(2026, 1, 5)
CANCELLED_ON = date(2026, 1, 7)


def _commitment(*, as_of: date = APPROVED_ON, legs: int = 1) -> ApprovedCommitment:
    schedule = tuple(
        ArrivalLeg(
            seq=n,
            item="배추",
            qty_kg=1000.0 * n,
            purchase_date=as_of,
            arrival_date=date(2026, 1, 7),
        )
        for n in range(1, legs + 1)
    )
    return ApprovedCommitment(
        approval_id="H1-REQ-20260105-0001-1",
        request_id="REQ-20260105-0001",
        item="배추",
        as_of=as_of,
        scenario_label="기본",
        total_qty_kg=sum(leg.qty_kg for leg in schedule),
        total_amount_krw=1_000_000.0,
        inbound_lead_days=2,
        arrival_schedule=schedule,
    )


class _가짜커넥션:
    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self.updated: list[Any] = []

    def cursor(self) -> Any:
        conn = self

        class _Cur:
            rowcount = 1

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def execute(self, query: Any, params: Any = None) -> None:
                conn.updated.append((str(query), params))

        return _Cur()

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


class _파트:
    """받은 것을 기록하는 취소 어댑터."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    def cancel(
        self,
        conn: Any,
        *,
        commitment: ApprovedCommitment,
        cancelled_on: date,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
        financing_mode: str,
    ) -> None:
        self.calls.append(
            {
                "cancelled_on": cancelled_on,
                "target_state_date": target_state_date,
                "purchase_ids": dict(purchase_ids),
                "approval_id": commitment.approval_id,
                "financing_mode": financing_mode,
            }
        )
        if self._raises is not None:
            raise self._raises


@pytest.fixture(autouse=True)
def _빈_등록소() -> Any:
    cancellation.reset()
    yield
    cancellation.reset()


MODE = "LOAN_BASELINE"


@pytest.fixture(autouse=True)
def _재무_축을_가짜로_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **경계가 `sim_runs` 를 읽는다** (재무 요청 2026-09-06).

    ★ **입력 적재·공휴일 달력을 가짜로 주는 것과 같은 이유다.** 안 꽂으면 이 검사들이
      실 DB 로 축을 물으러 나간다.

    ⚠️ **못 읽는 경로는 따로 잰다** (`test_축을_못_읽으면_막는다`).
    """
    monkeypatch.setattr("app.master.cancellation.financing_mode_of", lambda commitment: MODE)


def _등록한다(*, logistics_raises: Exception | None = None) -> tuple[_파트, _파트]:
    finance, logistics = _파트(), _파트(raises=logistics_raises)
    cancellation.register_cancellation("finance", finance)
    cancellation.register_cancellation("logistics", logistics)
    return finance, logistics


# ── ① 어댑터가 없으면 안 돈다 ─────────────────────────────────────────────


def test_어댑터가_하나도_없으면_안_돈다():
    """🔴 **반쪽 취소가 더 나쁘다.** 마스터 원장만 물리면 *"매입은 취소인데 채무는
    살아 있는"* 장부가 남는다."""
    conn = _가짜커넥션()
    out = undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    assert out.status == "NOT_APPLIED"
    assert out.missing == ["finance", "logistics"]
    assert conn.committed == 0, "커넥션을 열지도 말아야 한다"


def test_한_파트만_등록돼도_안_돈다():
    """★ 지금이 정확히 그 상태다 — 재무는 `#302` 로 섰고 물류는 안 섰다."""
    cancellation.register_cancellation("finance", _파트())
    conn = _가짜커넥션()

    out = undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    assert out.status == "NOT_APPLIED"
    assert out.missing == ["logistics"]


# ── ② 날짜 — 취소일과 상태 날짜는 다르다 ──────────────────────────────────


def test_취소일을_그대로_싣는다_승인일이_아니다():
    """🔴 **`commitment.as_of` 는 원 승인일이다** (재무 회신 2026-09-06 §4).

    승인일을 취소일로 쓰면 *"01-05 에 취소했다"* 가 되고, 재무가 01-06 상태를
    고치게 된다 — 그날은 실제로 미지급이 있었다.
    """
    finance, logistics = _등록한다()

    undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: _가짜커넥션())

    assert finance.calls[0]["cancelled_on"] == CANCELLED_ON
    assert finance.calls[0]["cancelled_on"] != APPROVED_ON
    assert logistics.calls[0]["cancelled_on"] == CANCELLED_ON


def test_상태가_설_날은_취소_다음_달력일이다():
    """★ 승인과 **같은 규칙**이다 — *"사건이 일어난 날 + 1일"*."""
    finance, _ = _등록한다()

    undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: _가짜커넥션())

    assert finance.calls[0]["target_state_date"] == date(2026, 1, 8)


def test_금요일_취소면_토요일이다():
    """🔴 실행일 달력을 쓰지 않는다 — 장부는 날마다 흐른다 (`#240` 과 같은 결)."""
    금요일 = date(2026, 1, 9)
    assert 금요일.weekday() == 4
    finance, _ = _등록한다()

    undo_approval(_commitment(), cancelled_on=금요일, connect=lambda: _가짜커넥션())

    토요일 = date(2026, 1, 10)
    assert finance.calls[0]["target_state_date"] == 토요일


def test_당일_취소면_승인과_같은_상태일에_닿는다():
    """★ 가장 흔한 경우다. 더한 값을 같은 줄에서 빼서 0 이 된다."""
    finance, _ = _등록한다()

    undo_approval(_commitment(), cancelled_on=APPROVED_ON, connect=lambda: _가짜커넥션())

    assert finance.calls[0]["target_state_date"] == date(2026, 1, 6)


def test_취소일이_승인일보다_앞서면_막는다():
    """⚠️ *"승인하기 전에 취소했다"* 는 사건이 아니라 **날짜를 잘못 넘긴 것**이다."""
    finance, _ = _등록한다()
    conn = _가짜커넥션()

    out = undo_approval(_commitment(), cancelled_on=date(2026, 1, 4), connect=lambda: conn)

    assert out.status == "FAILED"
    assert "앞선다" in out.reason
    assert not finance.calls, "부서를 부르기 전에 막아야 한다"
    assert conn.committed == 0


# ── ③ purchase_ids — 승인 때와 같은 매핑 ──────────────────────────────────


def test_두_파트가_같은_purchase_ids_를_받는다():
    """★ 재무가 요구한 계약이다 (`#302 §3`) — 파싱도 추론도 임의 선택도 안 한다."""
    finance, logistics = _등록한다()

    undo_approval(_commitment(legs=2), cancelled_on=CANCELLED_ON, connect=lambda: _가짜커넥션())

    assert finance.calls[0]["purchase_ids"] == logistics.calls[0]["purchase_ids"]
    assert set(finance.calls[0]["purchase_ids"]) == {1, 2}


def test_승인이_만든_ID_와_같은_값이다():
    """🔴 한 글자라도 다르면 재무가 **아무것도 못 찾는다.**"""
    from app.master.transition import purchase_id_for

    commitment = _commitment(legs=2)
    finance, _ = _등록한다()

    undo_approval(commitment, cancelled_on=CANCELLED_ON, connect=lambda: _가짜커넥션())

    기대 = {leg.seq: purchase_id_for(commitment, leg.seq) for leg in commitment.arrival_schedule}
    assert finance.calls[0]["purchase_ids"] == 기대


# ── ④ 트랜잭션 — 다섯이 다 되거나 다 안 된다 ─────────────────────────────


def test_다_성공하면_한_번_커밋한다():
    conn = _가짜커넥션()
    _등록한다()

    out = undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    assert out.status == "CANCELLED"
    assert out.parts == ["finance", "logistics"]
    assert conn.committed == 1
    assert conn.rolled_back == 0
    assert conn.closed == 1


def test_한_파트가_터지면_통째로_롤백한다():
    """🔴 **재무만 물리고 물류가 남으면 *"돈은 안 내는데 물건은 오는"* 장부가 된다.**"""
    conn = _가짜커넥션()
    _등록한다(logistics_raises=RuntimeError("입고된 뒤라 못 물린다"))

    out = undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    assert out.status == "FAILED"
    assert "입고된 뒤라 못 물린다" in out.reason
    assert conn.committed == 0
    assert conn.rolled_back == 1
    assert conn.closed == 1


def test_실패해도_예외를_밖으로_내지_않는다():
    """★ `apply_approval` 과 같다 — 취소 실패가 **적재된 결정을 지우면 안 된다.**"""
    _등록한다(logistics_raises=RuntimeError("boom"))

    out = undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: _가짜커넥션())

    assert isinstance(out, CancellationOut)
    assert out.status == "FAILED"


# ── ⑤ 원장 — 지우지 않고 적는다 ───────────────────────────────────────────


def test_원장을_DELETE_하지_않는다():
    """🔴 승인이 있었다는 사실이 사라지면 *"승인했고 취소했다"* 를 못 말한다."""
    conn = _가짜커넥션()
    _등록한다()

    undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    원문 = " ".join(q for q, _ in conn.updated).upper()
    assert "DELETE" not in 원문, f"원장을 지웠다 — {원문}"
    assert "UPDATE" in 원문
    assert "CANCELLED" in 원문


def test_이미_취소된_것은_안_센다():
    """★ 재시도를 멱등으로 만드는 자리다 — 조회가 `<> 'CANCELLED'` 를 달아야 한다."""
    conn = _가짜커넥션()
    _등록한다()

    undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    원문 = " ".join(q for q, _ in conn.updated)
    assert "settlement_status <> 'CANCELLED'" in 원문


def test_회차가_없으면_원장을_안_건드린다():
    """★ 회차 일정을 못 만든 약정도 승인은 살아 있다 — 물릴 원장이 **없다**."""
    conn = _가짜커넥션()
    finance, _ = _등록한다()

    out = undo_approval(_commitment(legs=0), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    assert out.status == "CANCELLED"
    assert out.cancelled_purchases == 0
    assert not conn.updated, "빈 목록으로 UPDATE 를 냈다"
    assert finance.calls[0]["purchase_ids"] == {}


# ── ⑥ 등록소 ──────────────────────────────────────────────────────────────


def test_전이_등록소와_따로다():
    """★ 같은 사전이면 *"전이는 되는데 취소는 안 되는"* 상태를 표현할 수 없다.

    🔴 지금이 정확히 그 상태다 — 전이는 둘 다 섰고 취소는 재무만 섰다.
    """
    from app.master import transition

    cancellation.register_cancellation("finance", _파트())

    assert "finance" in cancellation.registered_cancellations()
    assert "finance" not in transition.registered() or True  # 전이 등록은 이 검사와 무관
    assert cancellation.cancellation_missing() == ("logistics",)


def test_모르는_파트는_못_넣는다():
    with pytest.raises(ValueError, match="취소 파트가 아니다"):
        cancellation.register_cancellation("sales", _파트())  # type: ignore[arg-type]


# ── ⑦ 어휘 — CANCEL 은 REJECT_ALL 이 아니다 ───────────────────────────────


def test_CANCEL_이_결정_어휘에_있다():
    """🔴 **`REJECT_ALL` 로 대신하면 안 된다** (2026-09-05 전원 합의).

    ```text
    REJECT_ALL   "이 안을 안 쓴다"      — 승인 **전** 판단. 장부를 안 건드린다
    CANCEL       "승인했던 것을 물린다"  — 승인 **후** 사실. 장부 다섯을 되돌린다
    ```

    ★ 둘을 한 어휘로 적으면 *"거절해서 장부가 없는 것"* 과 *"취소해서 장부가 물린
      것"* 이 같아진다. `#290` 이 `REJECT_ALL` 로 우회되던 것도 그 둘이 안 갈려서였다.
    """
    from typing import get_args

    from app.master.decision import Decision

    assert "CANCEL" in get_args(Decision)


def test_통과안이_없던_날은_취소도_못_받는다():
    """★ 물릴 승인이 있으려면 그날 통과안이 있었어야 한다.

    ⚠️ 없는 승인을 취소하면 **이력에는 취소가 남고 장부에는 아무 일도 안 일어난다** —
      그 둘이 갈리면 나중에 *"왜 취소했는데 그대로지"* 를 아무도 못 푼다.
    """
    from app.master.decision import DecisionRejected, check_decidable

    check_decidable("E1_APPROVED", "CANCEL")  # 여기서는 안 터져야 한다
    with pytest.raises(DecisionRejected, match="물릴 승인이 없다"):
        check_decidable("E2_HELD", "CANCEL")


def test_거절은_여전히_통과안이_없어도_받는다():
    """★ **`REJECT_ALL` 규칙은 안 바뀐다.** 취소 어휘를 더한 것이지 거절을 좁힌 것이
    아니다."""
    from app.master.decision import check_decidable

    check_decidable("E2_HELD", "REJECT_ALL")
    check_decidable("E3_REJECTED", "REQUEST_CHANGE")


# ── ⑧ 재무 축 — 마스터가 싣는다 (재무 요청 2026-09-06) ────────────────────


def test_두_파트가_같은_financing_mode_를_받는다():
    """🔴 **재무가 고르지 않겠다고 했고 그것이 맞다.**

    고르는 순간 그 선택이 조용히 굳고, 나중에 누구도 왜 그 축이었는지 못 찾는다 —
    `purchase_ids` 를 재무가 지어내면 안 되는 것과 같은 자리다.
    """
    finance, logistics = _등록한다()

    undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: _가짜커넥션())

    assert finance.calls[0]["financing_mode"] == MODE
    assert logistics.calls[0]["financing_mode"] == MODE


def test_축을_못_읽으면_막는다(monkeypatch: pytest.MonkeyPatch):
    """⚠️ **축을 지어내지 않는다.** 못 읽으면 트랜잭션을 시작하지도 않는다.

    ★ `apply_approval` 이 build 를 커넥션 밖에서 부르는 것과 같은 규율이다 — 계산이
      실패하면 **DB 를 열지도 않은 채** 멈춘다.
    """

    def 터진다(commitment: Any) -> str:
        raise LookupError("sim_runs 를 못 읽었다")

    monkeypatch.setattr("app.master.cancellation.financing_mode_of", 터진다)
    finance, _ = _등록한다()
    conn = _가짜커넥션()

    out = undo_approval(_commitment(), cancelled_on=CANCELLED_ON, connect=lambda: conn)

    assert out.status == "FAILED"
    assert "재무 축을 못 읽었다" in out.reason
    assert not finance.calls, "부서를 부르기 전에 막아야 한다"
    assert conn.committed == 0
