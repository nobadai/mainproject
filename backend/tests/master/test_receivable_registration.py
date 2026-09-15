"""채권 발행이 **실제로 등록되는가** — 그리고 마스터가 **값을 발명하지 않는가**.

🔴 **`confirm_receivable` 호출이 0건이었다** (실측 2026-09-09).

```text
haetdeul.sales   2026-01-05 · 01-07 · 01-09  CONFIRMED→DELIVERED  총 297,000원
haetdeul.receivables 에 그 셋의 채권   **0행**   ← 배선이 없어서
```

★ **가짜 구현으로는 이 자리를 못 잰다.** `test_receivable.py` 는 대역을 직접 등록해
  경계를 재므로 배선이 통째로 빠져 있어도 초록불이다. 여기서는 `app.main` 이 import
  시점에 등록한 **실제 배선**으로 잰다.

---

🔴 **이 파일이 잠그는 것은 다섯이다.**

```text
① 배선                 register_receivable 이 실제로 불린다
② financing_mode       마스터가 고르지 않는다 — 재무 축을 물어본다
③ 축 불일치            fail-closed → BLOCKED (덮어 쓰지 않는다)
④ due_date             sales.collection_due_date 를 읽는다 · NULL 이면 BLOCKED
⑤ DELIVERED            대상에 들어간다 — 빠지면 재실행이 죽는다
```

⚠️ **⑤ 가 왜 치명적인가.** 하루 실행의 출고 단계가 같은 날 안에서
  `CONFIRMED → DELIVERED` 로 바꾼다. 대상에서 `DELIVERED` 를 빼면 두 번째 걸음에서
  그 판매가 목록에서 사라지고, 첫 걸음에 채권이 안 섰으면 **그 뒤로 기회가 없다.**
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import app.main  # noqa: F401  — import 시점에 채권 발행을 등록한다. 이 검사의 전제다
from app.finance.db import FinanceDataNotReady, FinanceRuntimeAxis
from app.finance.receivables import ReceivablePersistenceConflict, receivable_id_for
from app.master import receivable
from app.master.finance_receivable import (
    ISSUABLE_ORDER_STATUSES,
    ConfirmedSale,
    FinanceReceivableAdapter,
    read_confirmed_sales,
)
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_binding import bind_sim_run

AS_OF = date(2026, 1, 5)
남의_실행 = "SIM-SOMEONE-ELSE"


def _축(*, sim_run_id: str = BURN_IN_SIM_RUN_ID, financing_mode: str) -> Any:
    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        return FinanceRuntimeAxis(sim_run_id=sim_run_id, financing_mode=financing_mode)

    return read_axis


def _판매(
    sale_id: str = "SALE-1",
    *,
    due: date | None = date(2026, 2, 4),
    amount: str = "132000.000000",
) -> ConfirmedSale:
    return ConfirmedSale(
        sale_id=sale_id,
        sim_run_id=BURN_IN_SIM_RUN_ID,
        sale_date=AS_OF,
        customer_partner_id="KIMCHI_FACTORY_001",
        collection_due_date=due,
        total_amount_krw=Decimal(amount),
    )


class _판매조회기록:
    """어댑터가 **언제 · 어느 날짜로** 판매를 읽으러 갔는지 잡는다."""

    def __init__(self, *sales: ConfirmedSale) -> None:
        self.calls: list[date] = []
        #: 어댑터가 **어느 축으로** 물었는가. 축을 안 넘기면 남의 실행 판매가 섞인다.
        self.axes: list[str] = []
        self.sales = sales

    def __call__(
        self, conn: Any, *, as_of: date, sim_run_id: str
    ) -> tuple[ConfirmedSale, ...]:
        self.calls.append(as_of)
        self.axes.append(sim_run_id)
        return self.sales


class _가짜원장:
    """`confirm_receivable` 대역. **`ON CONFLICT (sale_id) DO NOTHING` 을 흉내낸다.**

    ★ 이 대역이 있어야 *"두 번 불러도 행이 안 는다"* 를 DB 없이 잰다. 실 DB 실측은
      걷기(`python -m app.master.backtest_runner`)가 따로 낸다.
    """

    def __init__(self, *, conflict_on: str | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.requests: list[Any] = []
        self.conflict_on = conflict_on

    def __call__(self, conn: Any, request: Any) -> Any:
        self.requests.append(request)
        if self.conflict_on is not None and request.sale_id == self.conflict_on:
            raise ReceivablePersistenceConflict(
                f"finance state was not found: {request.sale_id}"
            )
        written = 0
        if request.sale_id not in self.rows:
            self.rows[request.sale_id] = {
                "due_date": request.due_date,
                "financing_mode": request.financing_mode,
                "original_amount_krw": request.original_amount_krw,
            }
            written = 1

        class _R:
            receivable_id = receivable_id_for(request.sale_id)
            receivables_written = written

        return _R()


# ---------------------------------------------------------------------------
# ① 배선
# ---------------------------------------------------------------------------

#: 이 검사가 등록소에 주는 실행 축. 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)과 다르다** —
#:   같으면 등록소가 축을 상수에서 다시 읽는 뮤턴트가 전부 살아남는다.
등록축 = "SIM-REG-RECEIVABLE"


def _등록된() -> Any:
    """등록소가 **호출 때** 축을 받아 세운 어댑터 (`#531` 후속 · 2026-09-10).

    ★ **재는 것은 그대로다** — 바뀐 것은 어댑터가 **언제 서는가** 하나다.
      전에는 `registered()["finance"]` 가 곧 어댑터라 축이 프로세스 시작 때 굳었다.
    """
    return bind_sim_run(receivable.registered()["finance"], 등록축)



def test_채권_발행이_등록된다() -> None:
    """★ **미등록과 「확정 판매 없음」은 다른 사실이다.** 이 줄이 없으면 앞으로 나간다."""
    assert receivable.missing() == (), (
        f"채권 발행이 미등록인 파트가 있다: {receivable.missing()}. "
        "app/master/bootstrap.py 의 register_receivable 을 확인한다"
    )
    assert "finance" in receivable.registered()


def test_등록된_것이_마스터_어댑터다() -> None:
    """⚠️ **어댑터는 마스터가 얹은 얇은 배선이다** (`#280` 전례)."""
    impl = _등록된()
    assert isinstance(impl, FinanceReceivableAdapter), (
        f"등록된 것이 마스터 채권 어댑터가 아니다: {type(impl).__name__}"
    )


def test_마스터가_정한_장부에_앉힌다() -> None:
    """★ `sim_run_id` 는 마스터 값이다 — 앞의 다섯 등록소가 쓰는 그 축 하나다.

    🔴 **부른 쪽이 준 축이 그대로 앉는다** (`#531` 후속). 배선이 든 상수를 재면
       걷기가 번인 아닌 실행을 타는 날 **채권만 번인에 남는 것**을 못 잡는다.
    """
    assert _등록된().sim_run_id == 등록축
    assert _등록된().sim_run_id != BURN_IN_SIM_RUN_ID


def test_배선이_financing_mode_를_들고_있지_않다() -> None:
    """🔴 **마스터가 고르지 않는다.** 배선 자리에 그 값이 있으면 그것이 고른 것이다."""
    impl = _등록된()
    assert not hasattr(impl, "financing_mode"), (
        "마스터 어댑터가 financing_mode 를 들고 있다 — 재무 축의 값이지 마스터 것이 아니다"
    )


def test_배선이_판매_목록을_들고_있지_않다() -> None:
    """🔴 **판매는 배선이 아니라 표에서 온다.**

    ⚠️ 배선 시점에 고정하면 판매가 한 줄 들어와도 앱을 다시 띄우기 전까지 아무 일도
      안 일어나고, 그것은 에러 없이 *"오늘은 판 게 없었다"* 로 보인다.
    """
    impl = _등록된()
    assert impl.load_sales is read_confirmed_sales, (
        f"배선이 정본 조회가 아닌 것을 쓴다: {impl.load_sales!r}"
    )


def test_원장에_직접_쓰지_않고_재무_경계를_부른다() -> None:
    """🔴 **마스터가 `receivables` 에 직접 INSERT 하지 않는다.**"""
    from app.finance.receivables import confirm_receivable

    impl = _등록된()
    assert impl.confirm is confirm_receivable, (
        f"채권 원장을 재무 경계 밖에서 건드린다: {impl.confirm!r}"
    )


# ---------------------------------------------------------------------------
# ② 재무 축을 **고르지 않고 물어본다**
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["LOAN_BASELINE", "BASE_NO_LOAN"])
def test_재무_축의_financing_mode_를_그대로_싣는다(mode: str) -> None:
    """🔴 **두 값으로 다 잰다.** 한 값만 재면 그 값을 상수로 박아도 초록불이다.

    ⚠️ 실 DB 에 `LOAN_BASELINE` 과 `BASE_NO_LOAN` 이 공존한다.
    """
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode=mode),
        load_sales=_판매조회기록(_판매()),
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "ISSUED"
    assert len(원장.requests) == 1, "판매가 하나인데 재무를 안 불렀다"
    assert 원장.requests[0].financing_mode == mode, (
        f"마스터가 financing_mode 를 골랐다: 재무 축은 {mode!r} 인데 "
        f"{원장.requests[0].financing_mode!r} 가 실렸다"
    )
    assert 원장.requests[0].sim_run_id == BURN_IN_SIM_RUN_ID, "sim_run_id 는 마스터 값이다"


def test_축_조회를_임포트_시점에_하지_않는다() -> None:
    """🔴 **배선이 DB 를 요구하기 시작하면 앱이 뜨는 조건이 조용히 늘어난다.**"""
    호출: list[int] = []

    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        호출.append(1)
        return FinanceRuntimeAxis(sim_run_id=BURN_IN_SIM_RUN_ID, financing_mode="LOAN_BASELINE")

    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=read_axis,
        load_sales=_판매조회기록(),
        confirm=_가짜원장(),
    )
    assert 호출 == [], "생성만 했는데 축을 읽었다"

    adapter.issue(conn=None, as_of=AS_OF)
    adapter.issue(conn=None, as_of=AS_OF)

    assert len(호출) == 2, "호출마다 축을 다시 읽어야 한다 — 축은 마스터가 캐시할 값이 아니다"


def test_판매를_마스터_축으로_읽는다() -> None:
    """🔴 **남의 실행 판매를 읽으면 안 된다** (마스터 판단 2026-09-09).

    ⚠️ 안 거르면 남의 축 판매가 같은 `sale_date` 에 들어왔을 때 `confirm_receivable`
      이 conflict 를 내고 **그날 전체가 `BLOCKED`** 가 된다. 남의 축 판매는 *"못 만든
      것"* 이 아니라 **애초에 내 대상이 아니다** — 그 둘을 접으면 막힌 날을 나중에
      설명할 수 없다.
    """
    조회 = _판매조회기록()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=조회,
        confirm=_가짜원장(),
    )

    adapter.issue(conn=None, as_of=AS_OF)

    assert 조회.axes == [BURN_IN_SIM_RUN_ID], (
        f"판매를 마스터 축으로 안 읽었다: {조회.axes}"
    )


def test_판매조회_대역이_축을_실제로_받는다() -> None:
    """🔴 **자기 생존 검사.** 정본 조회가 `sim_run_id` 를 안 받으면 위 검사가 공짜다."""
    import inspect

    from app.master.finance_receivable import read_confirmed_sales

    파라미터 = inspect.signature(read_confirmed_sales).parameters
    assert "sim_run_id" in 파라미터, (
        "정본 조회가 sim_run_id 를 안 받는다 — 대역만 받으면 배선을 안 재는 것이다"
    )
    assert 파라미터["sim_run_id"].kind is inspect.Parameter.KEYWORD_ONLY, (
        "sim_run_id 가 위치 인자면 as_of 와 자리를 바꿔 부를 수 있다"
    )


def test_판매도_호출마다_다시_읽는다() -> None:
    """★ 배선 시점에 고정하면 표에 한 줄 넣어도 앱을 다시 띄우기 전까지 아무 일도 없다."""
    조회 = _판매조회기록()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=조회,
        confirm=_가짜원장(),
    )

    adapter.issue(conn=None, as_of=AS_OF)
    adapter.issue(conn=None, as_of=date(2026, 1, 6))

    assert 조회.calls == [AS_OF, date(2026, 1, 6)], f"판매 조회가 고정됐다: {조회.calls}"


# ---------------------------------------------------------------------------
# ③ 축이 어긋나면 **막는다** (fail-closed)
# ---------------------------------------------------------------------------


def test_실행_축이_다르면_막는다() -> None:
    """🔴 **덮어 쓰지 않는다.** 조용히 남의 실행 장부에 채권을 세우면 안 된다."""
    조회 = _판매조회기록(_판매())
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(sim_run_id=남의_실행, financing_mode="LOAN_BASELINE"),
        load_sales=조회,
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED", f"축이 다른데 {out.status} 로 지나갔다"
    assert BURN_IN_SIM_RUN_ID in out.reason, "사유에 마스터가 준 값이 없다"
    assert 남의_실행 in out.reason, "사유에 재무가 읽은 값이 없다"
    assert 조회.calls == [], "막았는데 판매를 물어봤다"
    assert 원장.rows == {}, "막았는데 채권을 세웠다"


def test_축이_모호하면_사유에_그대로_남는다() -> None:
    """⚠️ **삼키지 않는다.** 접기만 하고 사유를 버리면 고칠 곳이 사라진다."""
    조회 = _판매조회기록(_판매())

    def read_axis(**_kwargs: object) -> FinanceRuntimeAxis:
        raise FinanceDataNotReady("finance_runtime_axis_ambiguous")

    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=read_axis,
        load_sales=조회,
        confirm=_가짜원장(),
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert "finance_runtime_axis_ambiguous" in out.reason
    assert 조회.calls == [], "축을 모르는데 판매를 물어봤다"


def test_판매를_못_읽으면_NOTHING_DUE_가_아니라_BLOCKED_다() -> None:
    """🔴 **없는 것과 못 읽은 것은 다르다.** `()` 로 접으면 DB 가 끊긴 날이 정상으로 보인다."""

    def 터진다(conn: Any, *, as_of: date, sim_run_id: str):
        raise RuntimeError("relation \"sales\" does not exist")

    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=터진다,
        confirm=_가짜원장(),
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED", f"못 읽었는데 {out.status} 다"
    assert "does not exist" in out.reason, "무엇이 터졌는지가 사라졌다"


def test_확정_판매가_0건이면_NOTHING_DUE_다() -> None:
    """🔴 **`BLOCKED` 로 접으면 뒤의 orchestration 이 사람을 부른다.**"""
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=_판매조회기록(),
        confirm=_가짜원장(),
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "NOTHING_DUE", f"판매가 0건인데 {out.status} 다"
    assert out.status != "BLOCKED"
    assert out.issued == []
    assert out.created == 0


# ---------------------------------------------------------------------------
# ④ due_date 는 **판매가 정한다**
# ---------------------------------------------------------------------------


def test_판매가_정한_기일을_그대로_싣는다() -> None:
    """🔴 **마스터가 `sale_date + payment_days` 를 다시 쓰지 않는다.**

    ⚠️ 다시 쓰면 결제조건이 바뀌는 날 두 곳이 다른 답을 내고, 그 사고는 에러 없이
      기일만 바꾼다.
    """
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        # ⚠️ **일부러 30일이 아니다.** 마스터가 식을 다시 쓰면 이 값이 안 나온다.
        load_sales=_판매조회기록(_판매(due=date(2026, 3, 17))),
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "ISSUED"
    assert 원장.requests[0].due_date == date(2026, 3, 17), (
        f"마스터가 기일을 계산했다: {원장.requests[0].due_date}"
    )
    assert 원장.requests[0].sale_date == AS_OF, "issued_date 는 sale_date 다"


def test_기일이_비어_있으면_지어내지_않고_막는다() -> None:
    """🔴 **`collection_due_date` 가 NULL 이면 `BLOCKED` 다.**

    ⚠️ 기일을 마스터가 발명하면 그 값으로 수금 판정이 돌고, 틀려도 에러가 안 난다.
    """
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=_판매조회기록(_판매("SALE-NO-DUE", due=None)),
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED", f"기일이 없는데 {out.status} 로 지나갔다"
    assert out.status != "NOTHING_DUE", "판매는 있었다 — 없다고 말하면 안 된다"
    assert "SALE-NO-DUE" in out.reason, "어느 판매가 막았는지가 사유에 없다"
    assert "collection_due_date" in out.reason
    assert 원장.requests == [], "기일도 모르면서 재무를 불렀다"


def test_한_건이_막혀도_나머지는_서고_전체는_BLOCKED_다() -> None:
    """★ *"세울 게 있었는데 못 세웠다"* 가 *"세웠다"* 보다 먼저 알려야 하는 사실이다."""
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=_판매조회기록(_판매("SALE-OK"), _판매("SALE-NO-DUE", due=None)),
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert out.issued == [receivable_id_for("SALE-OK")], "선 것까지 지웠다"
    assert out.created == 1


def test_재무가_못_적는다고_하면_BLOCKED_이고_나머지는_계속_본다() -> None:
    """★ `ReceivablePersistenceConflict` 는 재무가 **정상으로 마치고** 낸 판정이다.

    ⚠️ 그래서 트랜잭션이 살아 있고, 다음 판매를 계속 볼 수 있다.
    """
    원장 = _가짜원장(conflict_on="SALE-BAD")
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=_판매조회기록(_판매("SALE-BAD"), _판매("SALE-OK")),
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "BLOCKED"
    assert "SALE-BAD" in out.reason
    assert out.issued == [receivable_id_for("SALE-OK")], "뒤 판매를 안 봤다"


def test_모르는_예외는_삼키지_않고_올린다() -> None:
    """🔴 **DB 오류면 트랜잭션이 이미 죽어 있다.** 건별 사유로 적으면 사고 하나가
    사고 여럿으로 보인다 — 올려서 마스터가 롤백하고 `FAILED` 로 적게 한다."""

    def 터진다(conn: Any, request: Any):
        raise RuntimeError("current transaction is aborted")

    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=_판매조회기록(_판매()),
        confirm=터진다,
    )

    with pytest.raises(RuntimeError, match="aborted"):
        adapter.issue(conn=None, as_of=AS_OF)


# ---------------------------------------------------------------------------
# ⑤ DELIVERED 도 대상이다 — **빠지면 재실행이 죽는다**
# ---------------------------------------------------------------------------


def test_대상_상태에_DELIVERED_가_들어_있다() -> None:
    """🔴 **출고가 같은 날 안에서 `CONFIRMED → DELIVERED` 로 바꾼다.**

    ⚠️ 빼면 두 번째 걸음에서 그 판매가 목록에서 사라지고, 첫 걸음에 채권이 안 섰으면
      **그 뒤로 기회가 없다.**
    """
    assert "DELIVERED" in ISSUABLE_ORDER_STATUSES, (
        f"DELIVERED 가 대상에서 빠졌다 — 재실행이 죽는다: {ISSUABLE_ORDER_STATUSES}"
    )
    assert "CONFIRMED" in ISSUABLE_ORDER_STATUSES
    assert "READY" in ISSUABLE_ORDER_STATUSES
    assert "CANCELLED" not in ISSUABLE_ORDER_STATUSES, (
        "취소된 판매에 채권을 세우면 없는 돈이 매입 cap 을 늘린다"
    )


def test_조회가_그_상태들을_실제로_묻는다() -> None:
    """🔴 **상수만 맞고 SQL 이 다른 목록을 쓰면 아무것도 안 잠긴다.**

    ★ 실제로 실려 가는 파라미터를 잡는다.
    """
    잡은: dict[str, Any] = {}

    class _커서:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query: Any, params: Any) -> None:
            잡은["query"] = query
            잡은["params"] = params

        def fetchall(self) -> list[Any]:
            return []

    class _커넥션:
        def cursor(self):
            return _커서()

    rows = read_confirmed_sales(_커넥션(), as_of=AS_OF, sim_run_id=BURN_IN_SIM_RUN_ID)

    assert rows == ()
    assert 잡은["params"][0] == AS_OF, "sale_date 로 안 걸렀다"
    #: 🔴 **축을 SQL 이 실제로 싣는가** (마스터 판단 2026-09-09). 어댑터가 넘겨도
    #:   조회가 안 쓰면 남의 실행 판매가 그대로 딸려 온다.
    assert 잡은["params"][1] == BURN_IN_SIM_RUN_ID, (
        f"조회가 sim_run_id 를 안 싣는다: {잡은['params']}"
    )
    assert "DELIVERED" in 잡은["params"][2], (
        f"조회가 DELIVERED 를 안 묻는다: {잡은['params'][2]}"
    )
    assert list(잡은["params"][2]) == list(ISSUABLE_ORDER_STATUSES), (
        "상수와 실제 조회 목록이 갈렸다 — 상수만 고치면 조용히 안 먹는다"
    )


def test_출고가_보는_목록과_같은_함수를_쓰지_않는다() -> None:
    """🔴 **판정 대상이 다르다.** `due_sale_items` 는 *"오늘 나가야 하는가"* 라
    `CONFIRMED · READY` 만 본다 — 한 함수로 묶으면 한쪽 규칙을 고치는 날 다른 쪽이
    조용히 바뀐다."""
    import inspect as _inspect

    from app.master import outbound_flow

    출고_원문 = _inspect.getsource(outbound_flow.due_sale_items)
    assert "IN ('CONFIRMED', 'READY')" in 출고_원문, (
        "출고 조회의 상태 목록이 바뀌었다 — 이 검사가 무엇을 비교하는지부터 다시 본다"
    )
    assert "'DELIVERED'" not in 출고_원문.split("SELECT")[1], (
        "출고 목록이 이미 나간 판매를 다시 내보낸다"
    )
    assert outbound_flow.due_sale_items is not read_confirmed_sales


def test_DELIVERED_판매도_채권이_선다() -> None:
    """★ 상수·SQL 을 넘어 **실제로 발행까지 간다**는 것을 잰다."""
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        # ★ 실측한 그 판매다 — 2026-01-05 · DELIVERED · 132,000원.
        load_sales=_판매조회기록(_판매("SALE-SALES-FIXTURE-2026-01-05")),
        confirm=원장,
    )

    out = adapter.issue(conn=None, as_of=AS_OF)

    assert out.status == "ISSUED"
    assert out.created == 1
    assert 원장.rows["SALE-SALES-FIXTURE-2026-01-05"]["original_amount_krw"] == Decimal(
        "132000.000000"
    ), "금액을 마스터가 바꿨다"


# ---------------------------------------------------------------------------
# ⑥ 멱등 — **두 번 불러도 행이 안 는다**
# ---------------------------------------------------------------------------


def test_두_번_불러도_채권이_하나다() -> None:
    """🔴 **재무가 멱등이라고 적어 뒀지만 믿지 않고 검사로 박는다.**

    ★ 두 번째는 `ISSUED` 인데 `created == 0` 이다 — 그것이 *"이미 서 있었다"* 이고
      *"대상이 없었다"* 와 다른 사실이다.
    """
    원장 = _가짜원장()
    adapter = FinanceReceivableAdapter(
        sim_run_id=BURN_IN_SIM_RUN_ID,
        read_axis=_축(financing_mode="LOAN_BASELINE"),
        load_sales=_판매조회기록(_판매("SALE-1"), _판매("SALE-2")),
        confirm=원장,
    )

    첫걸음 = adapter.issue(conn=None, as_of=AS_OF)
    두걸음 = adapter.issue(conn=None, as_of=AS_OF)

    assert len(원장.rows) == 2, f"두 번 걸었더니 행이 늘었다: {len(원장.rows)}"
    assert 첫걸음.status == "ISSUED"
    assert 첫걸음.created == 2
    assert 두걸음.status == "ISSUED", "이미 서 있는 것을 NOTHING_DUE 로 접었다"
    assert 두걸음.created == 0, f"두 번째 걸음이 또 만들었다: {두걸음.created}"
    assert 두걸음.issued == 첫걸음.issued, "서 있는 채권 목록이 걸음마다 달라진다"
