"""입고 실행이 **실제로 등록되는가** — 그리고 검수 원천이 **없다는 것이 보이는가**.

🔴 **`register_inbound` 호출이 0건이었다.** 경계는 `#316` 으로, 구현은 물류가 `#329`
   로 세웠는데 등록하는 줄이 어디에도 없어서 `receive_arrivals` 가 매일 *"입고 실행
   미등록"* 으로 돌아섰다 — `apply_approval` 이 승인마다 *"상태전이 미등록"* 으로
   돌아서던 것과 **같은 모양**이고, `test_transition_registration.py` 가 잠근 자리와
   같은 종류다.

★ **가짜 구현으로는 이 자리를 못 잰다.** `test_inbound.py` 는 대역을 직접 등록해
  경계를 재므로 배선이 통째로 빠져 있어도 초록불이다. 여기서는 `app.main` 이 import
  시점에 등록한 **실제 구현**으로 잰다.

---

🔴 **두 번째로 잠그는 것이 더 중요하다 — `NoInspectionSource` 가 조용해지면 안 된다.**

검수 사실의 주인이 아직 없어서 마스터가 *"항상 `None` 을 내는 provider"* 로 배선했다
(물류가 `InspectionProvider` docstring 에서 정당하다고 명시한 길). 그 결과 도착분이
매일 `BLOCKED` 로 보인다. **그것이 목적이다.**

⚠️ 누가 편의로 여기에 그럴듯한 검수 사실을 채워 넣으면 *"아무도 정한 적 없는 비율"*
  이 곧 업무 사실이 되어 원가·폐기·판매 판단으로 흘러간다 — 물류가 기본 구현을
  일부러 안 만든 이유가 그것이다. 그 날 이 파일이 빨간불이어야 한다.
"""

from __future__ import annotations

from datetime import date

import pytest

import app.main  # noqa: F401  — import 시점에 입고 실행을 등록한다. 이 검사의 전제다
from app.logistics.inbound_execution import LogisticsInboundExecution
from app.master import inbound
from app.master.inbound_inspection import NoInspectionSource
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

AS_OF = date(2026, 1, 7)
"""도착 예정이 실제로 걸려 있는 날 — `02-06` 까지 `in_transit` 에 묻혀 있던 그 건이다."""


# ---------------------------------------------------------------------------
# 1. 배선
# ---------------------------------------------------------------------------


def test_입고_실행이_등록된다() -> None:
    """★ **미등록과 못 받음은 다른 사실이다.** 이 줄이 없으면 앞으로 나간다."""
    assert inbound.missing() == (), (
        f"입고 실행이 미등록인 파트가 있다: {inbound.missing()}. "
        "app/main.py 의 register_inbound 를 확인한다"
    )


def test_등록된_것이_물류의_실제_구현이다() -> None:
    """🔴 **대역이 등록돼 있으면 배선이 있는 것처럼 보이지만 아무것도 안 받는다.**"""
    impl = inbound.registered()["logistics"]
    assert isinstance(impl, LogisticsInboundExecution), (
        f"등록된 것이 물류 구현이 아니다: {type(impl).__name__}"
    )


def test_마스터가_정한_장부에_앉힌다() -> None:
    """★ `sim_run_id` 는 마스터 값이다 — 물류 모듈 상수로 새면 실행이 둘이 되는 날 깨진다."""
    impl = inbound.registered()["logistics"]
    assert impl._sim_run_id == BURN_IN_SIM_RUN_ID


# ---------------------------------------------------------------------------
# 2. 검수 원천이 **없다**는 것
# ---------------------------------------------------------------------------


def test_검수_provider_가_Null_이다() -> None:
    """⚠️ 진짜 provider 가 들어오는 날 이 검사를 지우는 것이 그 PR 의 일부다."""
    impl = inbound.registered()["logistics"]
    assert isinstance(impl._provider, NoInspectionSource), (
        f"검수 provider 가 바뀌었다: {type(impl._provider).__name__}. "
        "진짜 원천이 생긴 것이면 이 검사와 app/master/inbound_inspection.py 를 같이 지운다"
    )


@pytest.mark.parametrize(
    "inbound_row, detail",
    [
        (None, None),
        ({"inbound_id": "IN-1"}, {"item": "배추", "qty_kg": 100}),
        # ★ **그럴듯한 값**을 넣어 본다. 여기서 답이 갈리면 그것은 이미 검수 정책이다.
        ({"inbound_id": "IN-2", "grade": "특"}, {"item": "배추", "qty_kg": 5000}),
    ],
)
def test_무엇이_들어와도_모른다고_답한다(inbound_row: object, detail: object) -> None:
    """🔴 **값에 따라 답이 갈리면 아무도 정한 적 없는 비율이 업무 사실이 된 것이다.**"""
    fact = NoInspectionSource().provide(
        as_of=AS_OF, inbound=inbound_row, purchase_detail=detail
    )
    assert fact is None, f"검수 사실을 지어냈다: {fact!r}"


def test_부재를_예외로_바꾸지_않는다() -> None:
    """★ **`None` 은 실패가 아니라 부재다** (물류 `InspectionProvider`).

    예외를 올리면 마스터가 롤백하고 그날 입고를 통째로 `FAILED` 로 만든다 — 그것은
    *"검수 원천이 없다"* 가 아니라 *"실행이 깨졌다"* 이고, 사람이 없는 장애를 찾는다.
    """
    NoInspectionSource().provide(as_of=AS_OF, inbound=None, purchase_detail=None)


def test_provider_없이는_물류_구현이_서지_않는다() -> None:
    """⚠️ 기본값이 생기는 날 빨간불. **물류가 기본값을 안 둔 것이 계약이다.**"""
    with pytest.raises(TypeError):
        LogisticsInboundExecution(sim_run_id=BURN_IN_SIM_RUN_ID)  # type: ignore[call-arg]
