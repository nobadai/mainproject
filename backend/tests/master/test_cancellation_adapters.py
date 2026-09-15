"""취소 어댑터 둘 — **재무·물류가 실제로 물린다** (2026-09-06 · 2차).

★ 1차(`test_cancellation.py`)가 **경계**를 쟀다면 여기는 **양쪽 끝**을 잰다.

```text
마스터 Protocol   cancel(conn, *, commitment, cancelled_on, target_state_date, purchase_ids)
재무              cancel_finance_payables(conn, *, purchase_ids, as_of, target_state_date,
                                           financing_mode)
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
from decimal import Decimal
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


# 🔴 **취소가 고치는 곳이 바뀌었다** (물류 `#484` · W3-3 · 2026-09-10).
#
# ```text
# ① ~2026-09-09  그날 fixture 행의 두 JSON 목록에서 항목을 빼고
#                in_transit_status · confirmed_inbound_status 를 다시 세웠다
# ② 2026-09-10~  inbound_schedules 한 행의 cancelled_as_of 에 날짜를 적는다
#                (Reader 가 더 이상 그 JSON 칸을 안 읽는다)
# ```
#
# ★ **재는 사실들은 대부분 그대로다** — 내 것만 걷는다 · 재시도는 0 이다 · 없는 것을
#   만들지 않는다 · 잠그고 읽는다 · 상태일을 적는다. 바뀐 것은 **재는 문**이다.
#   무엇을 잃었는지는 `test_취소_흔적에_source_ref_가_남는다` 에 적었다.


class _가짜커서:
    """`inbound_schedules` · `inbound_receipts` 두 조회에 답한다."""

    def __init__(self, 일정: dict[str, dict[str, Any]], 도착함: set[str]) -> None:
        self._일정 = 일정
        self._도착함 = 도착함
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 1
        self._rows: list[Any] = []

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self.executed.append((text, params))
        self._rows = []
        if "inbound_receipts" in text:
            # params = (sim_run_id, inbound_id)
            self._rows = [{"있음": 1}] if params[1] in self._도착함 else []
        elif "inbound_schedules" in text and "SELECT" in text:
            찾은것 = self._일정.get(params[1])
            self._rows = [dict(찾은것)] if 찾은것 is not None else []

    def fetchall(self) -> list[Any]:
        return self._rows


class _가짜커넥션:
    def __init__(
        self, *일정들: dict[str, Any], 도착함: set[str] | None = None
    ) -> None:
        self.cur = _가짜커서(
            {행["inbound_id"]: 행 for 행 in 일정들}, 도착함 or set()
        )

    def cursor(self) -> Any:
        return self.cur


def _일정(inbound_id: str, *, cancelled_as_of: date | None = None) -> dict[str, Any]:
    """`inbound_schedules` 한 행. **취소 판정이 보는 칸만 채운다.**"""
    return {
        "inbound_id": inbound_id,
        "sim_run_id": "SIM-1",
        "purchase_item_id": "PI-1",
        "quantity_kg": Decimal("1000.0"),
        "expected_arrival_date": date(2026, 1, 7),
        "created_as_of": APPROVED_ON,
        "cancelled_as_of": cancelled_as_of,
        "source_ref": "MASTER-APPROVAL",
        "note": None,
    }


def _취소된것(conn: _가짜커넥션) -> list[tuple[Any, str]]:
    """실제로 나간 취소 UPDATE 들 — `(cancelled_as_of, inbound_id)`.

    ⚠️ `"UPDATE" in text` 로 거르지 않는다 — 잠그는 SELECT 의 `FOR UPDATE` 가 걸린다.

    ⚠️ 열쇠는 **끝에서** 센다 — `SET` 절이 늘면 앞자리가 밀린다.
    """
    return [
        (params[0], params[-1])
        for text, params in conn.cur.executed
        if "SET cancelled_as_of" in text
    ]


def _물은열쇠(conn: _가짜커넥션) -> set[Any]:
    """조회가 실제로 짚은 `inbound_id` 들. **두 조회 다 둘째 자리가 열쇠다.**"""
    return {
        params[1]
        for text, params in conn.cur.executed
        if "SELECT" in text and "SET cancelled_as_of" not in text
    }


MINE = "INB-H1-REQ-20260105-0001-1-1"
OTHER = "INB-H1-OTHER-9"


def test_내_승인분만_걷는다():
    """🔴 **남의 승인분이 사라지면 안 된다.**

    ★ ① 재는 사실이 그대로다. 문만 바뀌었다 — 전에는 *"JSON 목록을 통째로 새로
      쓰지 않는가"* 였고, 지금은 *"내 `inbound_id` 행만 UPDATE 하는가"* 다.

    ⚠️ **셈이 2 에서 1 로 바뀌었다** (②). 전에는 두 JSON 목록에서 하나씩 빠져 2 였다.
      지금은 일정 한 건의 정본이 **한 행**이라 1 이다 — 같은 사실을 한 번만 센다.
    """
    conn = _가짜커넥션(_일정(MINE), _일정(OTHER))

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE], source_ref="X"
    )

    assert _취소된것(conn) == [(TARGET, MINE)], "남의 일정 행을 건드렸다"
    assert removed == 1, "일정 한 건은 한 번 걷힌다"


def test_다_걷히면_그날부터_없음으로_선다():
    """🔴 **`UNRESOLVED` 가 아니다.** 취소는 *"확인했고 그날부터 없다"* 이지
    *"모른다"* 가 아니다.

    ★ ① 재는 사실이 그대로다. 전에는 `CONFIRMED_ZERO` 라는 **상태 이름**이 그 뜻을
      들었고, 지금은 `cancelled_as_of` 에 **날짜가 적힌다**는 것이 그 뜻이다 —
      `NULL` 이 곧 *"아직 안 취소"* 이므로 날짜가 들어가야 확인된 사실이 된다.
    """
    conn = _가짜커넥션(_일정(MINE))

    withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE], source_ref="X"
    )

    적힌날, _ = _취소된것(conn)[0]
    assert 적힌날 == TARGET
    assert 적힌날 is not None, "취소인데 '모른다'(NULL) 로 남겼다"


def test_이미_걷힌_뒤_재시도는_0이다():
    """★ 재무 `#302` 의 *"retry no-op"* 과 같은 모양이다.

    ★ ① 재는 사실이 그대로다. 전에는 *"목록에 내 것이 없다"* 로 재시도를 만들었고,
      지금은 **같은 날짜로 이미 취소된 행**이 그 자리다.
    """
    conn = _가짜커넥션(_일정(MINE, cancelled_as_of=TARGET))

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE], source_ref="X"
    )

    assert removed == 0
    assert _취소된것(conn) == [], "이미 취소된 행을 또 고쳤다"


def test_열쇠가_빈_항목은_묻지도_않는다():
    """★ **없는 열쇠로 DB 에 묻지 않는다.** 빈 열쇠는 아무 행에도 안 맞고, 그 0행은
    *"그 일정이 없다"* 로 읽힌다 — 두 사실이 뭉개진다.

    ⚠️ **뜻이 좁아졌다** (②). 전에는 *"물류가 다른 경로로 넣은, `inbound_id` 없는
      항목"* 을 안 건드린다는 검사였다. 그 JSON 목록이 없어져 그런 항목이 존재할 수
      없다 — 남의 행을 안 건드린다는 쪽은 `test_내_승인분만_걷는다` 가 잰다.
      여기 남은 것은 **빈 열쇠를 걸러내는가** 다.
    """
    conn = _가짜커넥션(_일정(MINE))

    removed = withdraw_inventory(
        conn,
        sim_run_id="SIM-1",
        as_of=TARGET,
        inbound_ids=[MINE, "", None],  # type: ignore[list-item]
        source_ref="X",
    )

    assert _물은열쇠(conn) == {MINE}, f"빈 열쇠로 DB 에 물었다: {_물은열쇠(conn)}"
    assert removed == 1


def test_걷을_것이_없으면_DB_를_안_친다():
    """★ 회차 일정이 없던 약정도 승인은 살아 있다."""
    conn = _가짜커넥션(_일정(MINE))

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[], source_ref="X"
    )

    assert removed == 0
    assert not conn.cur.executed


def test_그날_행이_없으면_만들지도_터지지도_않는다():
    """🔴 **② 뒤집힌 검사다** (물류 `#484` · 2026-09-10).

    ```text
    ① ~2026-09-09  그날 fixture 행이 없으면 LogisticsFixtureMissing 으로 터졌다
                   — 그 행은 물류 판단이라 마스터 취소가 만들면 안 됐다
    ② 2026-09-10~  일정 행이 없으면 아무것도 안 하고 0 이다
                   — Backfill 이전에 사라진 일정도 있을 수 있고,
                     없는 것을 걷는 것은 오류가 아니다
    ```

    ★ **안 만든다는 쪽은 그대로다.** INSERT 가 한 줄도 안 나가야 한다 — 그것이
      원래 이 검사의 이름이 지키던 것이다.
    """
    conn = _가짜커넥션()

    removed = withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=["INB-X"], source_ref="X"
    )

    assert removed == 0
    assert not any("INSERT" in text for text, _ in conn.cur.executed), "없는 일정을 만들었다"


def test_행을_잠그고_읽는다():
    """🔴 `FOR UPDATE` 가 없으면 두 취소가 같은 옛 행을 읽고 마지막이 이긴다."""
    conn = _가짜커넥션(_일정(MINE))

    withdraw_inventory(
        conn, sim_run_id="SIM-1", as_of=TARGET, inbound_ids=[MINE], source_ref="X"
    )

    잠근것 = [
        text
        for text, _ in conn.cur.executed
        if "inbound_schedules" in text and "SELECT" in text
    ]
    assert 잠근것, "일정 행을 읽지도 않고 고쳤다"
    assert all("FOR UPDATE" in text for text in 잠근것)


# ── ③ 물류 어댑터 — 취소일이 아니라 상태일을 적는다 ────────────────────────


def test_물류가_target_state_date_를_적는다():
    """🔴 **승인이 쓴 날이 아니다.** 승인 01-05 → 01-06 부터 서 있고, 취소 01-07 →
    01-08 부터 없다. 그 사이 날들은 **그대로 둔다** — 그때는 실제로 오는 중이었다.

    ★ ① 재는 사실이 그대로다. 전에는 그 날짜가 *"어느 fixture 행을 잠그나"* 로
      드러났고, 지금은 *"`cancelled_as_of` 에 무엇을 적나"* 로 드러난다.
    """
    conn = _가짜커넥션(_일정(MINE))

    LogisticsCancellationAdapter(sim_run_id="SIM-1").cancel(
        conn,
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={1: "PUR-X-S1"},
        financing_mode="LOAN_BASELINE",
    )

    적힌날, _ = _취소된것(conn)[0]
    assert 적힌날 == TARGET, f"상태일이 아니라 {적힌날} 을 적었다"
    assert 적힌날 != CANCELLED_ON, "취소일 자체를 적으면 이미 지나간 하루의 사실이 바뀐다"
    assert 적힌날 != APPROVED_ON


def test_취소_흔적에_source_ref_가_남는다():
    """🔴 **취소가 왜 났는지 DB 에 남는다** — 물류가 `cancel_source_ref` 에 적는다.

    ★ **`xfail(strict)` 를 걷은 자리다.** 그 마크의 reason 이 *"XPASS 로 빨개지면
      물류가 적기 시작했다는 뜻이다 — 그때 이 마크를 걷어라"* 라고 적어 둔 그대로다.

    ⚠️ **여전히 이 파일뿐이다.** `withdraw_inventory` · `cancel_schedule` ·
      `assert_cancellable` 을 잰 검사는 저장소 전체에서 여기밖에 없다.
    """
    conn = _가짜커넥션(_일정(MINE))

    LogisticsCancellationAdapter(sim_run_id="SIM-1").cancel(
        conn,
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={},
        financing_mode="LOAN_BASELINE",
    )

    나간값 = [str(값) for _, params in conn.cur.executed for 값 in (params or ())]
    assert any("MASTER-CANCEL" in 값 for 값 in 나간값)
    assert any(CANCELLED_ON.isoformat() in 값 for 값 in 나간값)


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
        financing_mode="LOAN_BASELINE",
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
        financing_mode="LOAN_BASELINE",
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
        financing_mode="LOAN_BASELINE",
    )

    assert not 불렸나


# ── ⑤ 배선 — 둘 다 등록된다 ───────────────────────────────────────────────


def test_main_이_두_파트를_다_등록한다():
    """🔴 **하나만 등록되면 `undo_approval` 이 `NOT_APPLIED` 로 접힌다.**

    ⚠️ 전역 등록소를 읽지 않는다 — 다른 검사가 `reset()` 을 하면 순서에 따라 답이
      달라진다. **배선 원문**을 읽어 두 줄이 다 있는지 본다.

    ★ **읽는 파일이 바뀌었다** (`#442` · 2026-09-09). 등록 줄이 `app/main.py` 에서
      `app/master/bootstrap.py` 로 옮겨졌다 — 진입점이 둘(FastAPI · CLI)이라 조립
      뿌리를 함수로 뺐다. 재는 것은 그대로다.
    """
    import pathlib

    from app.master import bootstrap

    원문 = pathlib.Path(bootstrap.__file__).read_text(encoding="utf-8")

    assert 'register_cancellation("finance"' in 원문
    assert '"logistics",\n        SimRunBound(lambda axis: LogisticsCancellationAdapter' in 원문, (
        "물류 취소 등록 줄이 없거나 축을 호출 때 안 받는다"
    )
    # 🔴 **축이 배선에 상수로 박혀 있으면 안 된다** (`#531` 후속 · 2026-09-10).
    #
    #    전에는 이 자리가 `"BURN_IN_SIM_RUN_ID" in ...` 이었다 — *"마스터가 축을 눈에
    #    보이게 준다"* 를 재려던 것인데, 그 모양이 곧 **프로세스 시작 때 축이 굳는
    #    것**이었다. 이제 배선은 `SimRunBound` 로 감싸고 축은 `undo_approval` 이 나른다.
    #
    # ★ **주석을 걷어내고 잰다.** 이 파일은 근거를 길게 적고 위 `전 / 후` 표에도 그
    #   상수가 나온다 — 안 걷으면 코드가 아니라 문장을 재게 된다
    #   (`test_sim_run_axis.py` 의 `_벗긴_원문` 과 같은 이유).
    코드 = "\n".join(
        줄 for 줄 in 원문.splitlines() if not 줄.lstrip().startswith("#")
    )
    assert "BURN_IN_SIM_RUN_ID" not in 코드, (
        "배선이 축을 상수로 든다 — 걷기가 번인 아닌 실행을 타는 날 등록소만 번인에 남는다"
    )


# ── ⑥ financing_mode — 권위 축을 그대로 넘긴다 ────────────────────────────


def test_재무_어댑터가_financing_mode_를_받는다(monkeypatch: pytest.MonkeyPatch):
    """Master가 읽은 권위 축을 Finance core에 한 글자도 바꾸지 않고 넘긴다."""
    받은것: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.master.finance_cancellation.cancel_finance_payables",
        lambda conn, **kw: 받은것.update(kw),
    )

    FinanceCancellationAdapter().cancel(
        object(),
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={1: "PUR-A-S1"},
        financing_mode="LOAN_BASELINE",
    )

    assert 받은것["financing_mode"] == "LOAN_BASELINE"


def test_물류_어댑터도_financing_mode_를_받는다():
    """⚠️ **안 쓰더라도 받는다.** 두 파트가 같은 모양이어야 호출부가 하나로 선다 —
    `purchase_ids` 를 재무에만 줬다가 물류 Arrival 이 막힌 자리가 그 교훈이다."""
    conn = _가짜커넥션(_일정(MINE))

    LogisticsCancellationAdapter(sim_run_id="SIM-1").cancel(
        conn,
        commitment=_commitment(),
        cancelled_on=CANCELLED_ON,
        target_state_date=TARGET,
        purchase_ids={},
        financing_mode="LOAN_BASELINE",
    )

    assert conn.cur.executed, "받고도 아무것도 안 했다"


def test_두_Protocol_구현이_같은_인자를_요구한다():
    """🔴 **규약 원문을 잠근다.** 한쪽만 넓히면 호출부가 갈린다 — `#313` 에서 전이
    Protocol 을 같은 방식으로 잠갔다."""
    import inspect

    재무 = inspect.signature(FinanceCancellationAdapter.cancel).parameters
    물류 = inspect.signature(LogisticsCancellationAdapter.cancel).parameters

    assert set(재무) == set(물류), (
        f"두 어댑터의 인자가 갈렸다 — 재무 {set(재무)} · 물류 {set(물류)}"
    )
    for 이름 in (
        "commitment",
        "cancelled_on",
        "target_state_date",
        "purchase_ids",
        "financing_mode",
    ):
        assert 이름 in 물류, f"물류 어댑터에 {이름} 이 없다"
        assert 물류[이름].kind is inspect.Parameter.KEYWORD_ONLY
