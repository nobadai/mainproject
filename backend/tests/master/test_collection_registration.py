"""수금 실행이 **실제로 등록되는가** — 그리고 마스터가 **재무 축을 고르지 않는가**.

🔴 **`register_collection` 호출이 0건이었다.** 경계는 `#372` 로, 재무 구현체는 `#404`
   로 섰는데 등록하는 줄이 없어 `collect_receipts` 가 매일 *"수금 실행 미등록"* 으로
   돌아섰다 — `register_inbound` 가 `#337` 까지 0건이던 것과 **같은 모양**이고,
   `test_inbound_registration.py` 가 잠근 자리와 같은 종류다.

★ **가짜 구현으로는 이 자리를 못 잰다.** `test_collection.py` 는 대역을 직접 등록해
  경계를 재므로 배선이 통째로 빠져 있어도 초록불이다. 여기서는 `app.main` 이 import
  시점에 등록한 **실제 배선**으로 잰다.

---

🔴 **두 번째로 잠그는 것 — `financing_mode` 는 마스터가 고르는 것이 아니다.**

```text
sim_run_id       🟢 **마스터가 정한다** — 값의 주인은 BURN_IN_SIM_RUN_ID 하나
financing_mode   🔴 **마스터 축이 아니다** — 재무 축 (sim_run_id, as_of, financing_mode) 의 것
```

⚠️ 실측으로 `finance_states` 에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이 **실제로
  공존한다.** 마스터가 하나를 상수로 박으면 *"무차입 상태가 대출 baseline 자리에
  조용히 들어온다"* — 에러 없이 숫자만 바뀐다.

★ **그래서 두 값으로 다 잰다.** 한 값으로만 재면 그 값을 상수로 박아도 초록불이다.

---

🔴 **세 번째로 잠그는 것 — 사건이 0건이면 `NOTHING_DUE` 이고 `BLOCKED` 가 아니다.**

`master_collection_events` 표가 **비어 있다** (2026-09-08 실측 0행). 그래서 배선해도
수금은 한 건도 안 일어난다. 그것이 지금의 정상 상태다.

```text
전   등록 안 됨      → missing() 에 뜨고 경로가 안 돈다
후   NOTHING_DUE     → **확인했고 낼 것이 없다**
```

⚠️ **이 배선은 자리를 세울 뿐 수금을 만들지 않는다.** 무엇을 사실로 둘지는 팀 결정이고,
  재무가 *"due_date 경과를 수금으로 읽지 않는다"* 로 그은 선이 그 이유다.

---

🔴 **네 번째로 잠그는 것 — 사건을 배선 시점에 들고 있지 않다.**

배선 자리에서 사건 목록을 만들어 넘기면 **그 목록이 앱이 뜨는 순간에 고정**된다.
표에 한 줄 넣어도 앱을 다시 띄우기 전까지 아무 일도 안 일어나고, 그것은 에러 없이
*"오늘은 들어올 게 없었다"* 로 보인다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

import app.main  # noqa: F401  — import 시점에 수금 실행을 등록한다. 이 검사의 전제다
from app.finance.collection import CollectionEvent
from app.finance.db import FinanceDataNotReady, FinanceRuntimeAxis
from app.master import collection
from app.master.collection_events import read_collection_events
from app.master.finance_collection import FinanceCollectionAdapter
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_binding import bind_sim_run

AS_OF = date(2026, 1, 10)
"""토요일이다. **입금은 토요일에도 찍힌다** — 수금은 달력일이다."""

남의_실행 = "SIM-SOMEONE-ELSE"


class _사건조회기록:
    """어댑터가 **어느 축으로** 사건을 읽으러 갔는지 잡는다.

    ★ 축이 실제로 실려 갔는지는 이 호출로만 보인다 — 어댑터가 만든
      `FinanceCollectionSource` 는 밖에서 안 보인다.
    """

    def __init__(self, events: tuple[CollectionEvent, ...] = ()) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events = events

    def __call__(self, *, sim_run_id: str, financing_mode: str) -> tuple[CollectionEvent, ...]:
        self.calls.append({"sim_run_id": sim_run_id, "financing_mode": financing_mode})
        return self.events


def _축(*, sim_run_id: str = BURN_IN_SIM_RUN_ID, financing_mode: str) -> Any:
    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        return FinanceRuntimeAxis(sim_run_id=sim_run_id, financing_mode=financing_mode)

    return read_axis


# ---------------------------------------------------------------------------
# 1. 배선
# ---------------------------------------------------------------------------

#: 이 검사가 등록소에 주는 실행 축. 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)과 다르다** —
#:   같으면 등록소가 축을 상수에서 다시 읽는 뮤턴트가 전부 살아남는다.
등록축 = "SIM-REG-COLLECTION"


def _등록된() -> Any:
    """등록소가 **호출 때** 축을 받아 세운 어댑터 (`#531` 후속 · 2026-09-10).

    ```text
    전   registered()["finance"] 가 곧 어댑터였다 — 축이 프로세스 시작 때 굳었다
    후   그 자리에 축을 기다리는 SimRunBound 가 앉고, 어댑터는 축이 올 때 선다
    ```

    ★ **재는 것은 그대로다** — *"등록된 것이 마스터 어댑터이고 마스터가 정한 장부에
      앉히는가"*. 바뀐 것은 **언제 서는가** 하나다.
    """
    return bind_sim_run(collection.registered()["finance"], 등록축)



def test_수금_실행이_등록된다() -> None:
    """★ **미등록과 「들어올 것 없음」은 다른 사실이다.** 이 줄이 없으면 앞으로 나간다."""
    assert collection.missing() == (), (
        f"수금 실행이 미등록인 파트가 있다: {collection.missing()}. "
        "app/main.py 의 register_collection 을 확인한다"
    )
    assert "finance" in collection.registered()


def test_등록된_것이_마스터_어댑터다() -> None:
    """⚠️ **어댑터는 마스터가 얹은 얇은 배선이다** (`#280` 전례).

    재무가 축 둘을 직접 들고 오는 구현을 올리면 이 검사가 그 교체를 알려 준다.
    """
    impl = _등록된()
    assert isinstance(impl, FinanceCollectionAdapter), (
        f"등록된 것이 마스터 수금 어댑터가 아니다: {type(impl).__name__}"
    )


def test_마스터가_정한_장부에_앉힌다() -> None:
    """★ `sim_run_id` 는 마스터 값이다 — 재무 모듈 상수로 새면 실행이 둘이 되는 날 깨진다.

    🔴 **부른 쪽이 준 축이 그대로 앉는다** (`#531` 후속). 전에는 배선이 든 상수를
       쟀는데, 그러면 걷기가 번인 아닌 실행을 타는 날 **이 어댑터만 번인에 남는다.**
    """
    assert _등록된().sim_run_id == 등록축
    assert _등록된().sim_run_id != BURN_IN_SIM_RUN_ID


def test_배선이_financing_mode_를_들고_있지_않다() -> None:
    """🔴 **마스터가 고르지 않는다.** 배선 자리에 그 값이 있으면 그것이 고른 것이다."""
    impl = _등록된()
    assert not hasattr(impl, "financing_mode"), (
        "마스터 어댑터가 financing_mode 를 들고 있다 — 재무 축의 값이지 마스터 것이 아니다"
    )


def test_배선이_사건_목록을_들고_있지_않다() -> None:
    """🔴 **사건은 배선이 아니라 표에서 온다.**

    ⚠️ 배선 자리에서 사건 목록을 만들어 넘기면 **그 목록이 앱이 뜨는 순간에 고정된다.**
      표에 한 줄 넣어도 다시 띄우기 전까지 아무 일도 안 일어나고, 그것은 에러 없이
      *"오늘은 들어올 게 없었다"* 로 보인다.
    """
    impl = _등록된()
    assert not hasattr(impl, "source"), (
        "배선 자리의 어댑터가 사건 원천을 들고 있다 — 사건은 호출 시점에 표에서 읽는다"
    )


def test_사건을_정본_표에서_읽는다() -> None:
    """★ 읽는 방법의 주인은 `master_collection_events` 조회 하나다.

    ⚠️ 여기에 다른 함수가 오면 **사건을 어디서 얻는지가 두 곳이 된다.**
    """
    impl = _등록된()
    assert impl.load_events is read_collection_events, (
        f"배선이 정본 표 조회가 아닌 것을 쓴다: {impl.load_events!r}"
    )


# ---------------------------------------------------------------------------
# 2. 재무 축을 **고르지 않고 물어본다**
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["LOAN_BASELINE", "BASE_NO_LOAN"])
def test_재무_축의_financing_mode_를_그대로_싣는다(mode: str) -> None:
    """🔴 **두 값으로 다 잰다.** 한 값만 재면 그 값을 상수로 박아도 초록불이다.

    ⚠️ 실 DB 에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이 공존한다.
    """
    원천 = _사건조회기록()
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        load_events=원천,
        read_axis=_축(financing_mode=mode),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "NOTHING_DUE"
    assert len(원천.calls) == 1
    assert 원천.calls[0]["financing_mode"] == mode, (
        f"마스터가 financing_mode 를 골랐다: 재무 축은 {mode!r} 인데 "
        f"{원천.calls[0]['financing_mode']!r} 가 실렸다"
    )
    assert 원천.calls[0]["sim_run_id"] == BURN_IN_SIM_RUN_ID, "sim_run_id 는 마스터 값이다"


def test_축_조회를_임포트_시점에_하지_않는다() -> None:
    """🔴 **배선이 DB 를 요구하기 시작하면 앱이 뜨는 조건이 조용히 늘어난다.**

    ★ `app.main` 은 이미 import 됐다. 그때 축을 읽었다면 이 검사에 오기 전에 터졌다.
      여기서는 **부를 때마다 다시 읽는지**를 잰다.
    """
    호출 = []

    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        호출.append(1)
        return FinanceRuntimeAxis(sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode="LOAN_BASELINE")

    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=read_axis,
        load_events=_사건조회기록(),
    )
    assert 호출 == [], "생성만 했는데 축을 읽었다"

    adapter.collect(conn=None, as_of=AS_OF)
    adapter.collect(conn=None, as_of=AS_OF)

    assert len(호출) == 2, "호출마다 축을 다시 읽어야 한다 — 축은 마스터가 캐시할 값이 아니다"


# ---------------------------------------------------------------------------
# 3. 축이 어긋나면 **막는다** (fail-closed)
# ---------------------------------------------------------------------------


def test_실행_축이_다르면_막는다() -> None:
    """🔴 **덮어 쓰지 않는다.** 조용히 남의 실행 장부에 수금을 적으면 안 된다.

    ⚠️ `FinanceCollectionSource` 는 자기가 받은 축으로만 사건을 고르므로 덮어 써도
      **아무 예외 없이 돈다** — 그래서 어댑터가 여기서 세운다.
    """
    원천 = _사건조회기록()
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        load_events=원천,
        read_axis=_축(sim_run_id=남의_실행, financing_mode="LOAN_BASELINE"),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED", f"축이 다른데 {out.status} 로 지나갔다"
    assert BURN_IN_SIM_RUN_ID in out.reason, "사유에 마스터가 준 값이 없다"
    assert 남의_실행 in out.reason, "사유에 재무가 읽은 값이 없다"
    assert 원천.calls == [], "막았는데 사건을 물어봤다"


def test_축이_모호하면_사유에_그대로_남는다() -> None:
    """⚠️ **삼키지 않는다.** 접기만 하고 사유를 버리면 *"막혔다"* 만 남고 고칠 곳이 사라진다."""
    원천 = _사건조회기록()

    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        raise FinanceDataNotReady("finance_runtime_axis_ambiguous")

    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        load_events=원천,
        read_axis=read_axis,
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED", f"축이 모호한데 {out.status} 로 지나갔다"
    assert "finance_runtime_axis_ambiguous" in out.reason, (
        f"무엇이 모호했는지가 사유에서 사라졌다: {out.reason!r}"
    )
    assert 원천.calls == [], "축을 모르는데 사건을 물어봤다"


def test_축이_없으면_사유에_그대로_남는다() -> None:
    """★ `get_finance_runtime_axis` 는 행이 0건이면 `LookupError` 를 던진다."""
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_없는_축,
        load_events=_사건조회기록(),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert "Current Finance State was not found" in out.reason


def _없는_축(**_kwargs: object) -> FinanceRuntimeAxis:
    raise LookupError("Current Finance State was not found")


# ---------------------------------------------------------------------------
# 4. 사건이 0건이면 **`NOTHING_DUE` 이고 `BLOCKED` 가 아니다**
# ---------------------------------------------------------------------------


def test_사건이_0건이면_NOTHING_DUE_다() -> None:
    """🔴 **`BLOCKED` 로 접으면 뒤의 orchestration 이 사람을 부른다.**

    ★ 지금 정본 표가 비어 있으므로 **이것이 매일 나오는 답**이다.
      *"확인했고 낼 것이 없다"* 이지 *"막혔다"* 가 아니다.
    """
    adapter = FinanceCollectionAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_events=_사건조회기록(),
    )

    out = adapter.collect(conn=None, as_of=AS_OF)

    assert out.status == "NOTHING_DUE", f"사건이 0건인데 {out.status} 다"
    assert out.status != "BLOCKED"
    assert out.part == "finance"
    assert out.collected == []


def test_사건이_0건이어도_미등록이_아니다() -> None:
    """★ **둘 다 아무 일도 안 일어나지만 다른 사실이다.**

    ```text
    미등록        collect_receipts 가 missing=["finance"] 로 돌아선다
    NOTHING_DUE   물어봤고 낼 것이 없었다
    ```
    """
    assert "finance" not in collection.missing()
