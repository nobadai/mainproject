"""입고 실행이 **실제로 등록되는가** — 그리고 검수 원천을 **누가 골랐는가**.

🔴 **`register_inbound` 호출이 0건이었다.** 경계는 `#316` 으로, 구현은 물류가 `#329`
   로 세웠는데 등록하는 줄이 어디에도 없어서 `receive_arrivals` 가 매일 *"입고 실행
   미등록"* 으로 돌아섰다 — `apply_approval` 이 승인마다 *"상태전이 미등록"* 으로
   돌아서던 것과 **같은 모양**이고, `test_transition_registration.py` 가 잠근 자리와
   같은 종류다.

★ **가짜 구현으로는 이 자리를 못 잰다.** `test_inbound.py` 는 대역을 직접 등록해
  경계를 재므로 배선이 통째로 빠져 있어도 초록불이다. 여기서는 `app.main` 이 import
  시점에 등록한 **실제 구현**으로 잰다.

---

🔴 **두 번째로 잠그는 것 — 검수 provider 는 마스터가 고르는 것이 아니다.**

이 파일은 처음에 `NoInspectionSource`(마스터가 임시로 든 Null)를 잠갔다. 물류가
`#336` 으로 `ScenarioSimulatedInspectionProvider` 를 올리면서 **주인이 정해졌고**,
그 임시 파일은 설계대로 지워졌다.

★ **그래서 여기서 재는 것이 바뀌었다.** 전에는 *"Null 이 조용해지면 안 된다"* 였고
  지금은 *"마스터가 자기 판정을 지어내지 않는다"* 다. 잠그는 값이 아니라 **잠그는
  이유가 같다** — 검수 규칙의 주인은 물류다.

⚠️ **전량 PASS 는 품질 모델이 아니다.** 물류가 자기 파일에 적었듯 *"이번 MVP 가
  품질손실 축을 아직 쓰지 않는다"* 는 명시적 가정이다. 마스터가 여기서 합격률을
  손보면 **아무도 정한 적 없는 비율이 업무 사실이 된다.**
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

import app.main  # noqa: F401  — import 시점에 입고 실행을 등록한다. 이 검사의 전제다
from app.logistics.inbound_execution import LogisticsInboundExecution
from app.logistics.simulated_inspection import ScenarioSimulatedInspectionProvider
from app.master import inbound
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.sim_run_binding import bind_sim_run

AS_OF = date(2026, 1, 7)
"""도착 예정이 실제로 걸려 있는 날 — 250일째 `in_transit` 에 묻혀 있는 그 건이다."""

#: 이 검사가 등록소에 주는 실행 축. 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)과 다르다** —
#:   같으면 등록소가 축을 상수에서 다시 읽는 뮤턴트가 전부 살아남는다.
등록축 = "SIM-REG-INBOUND"


def _등록된() -> Any:
    """등록소가 **호출 때** 축을 받아 세운 물류 구현 (`#531` 후속 · 2026-09-10).

    ★ **재는 것은 그대로다** — 바뀐 것은 구현이 **언제 서는가** 하나다.
      전에는 `registered()["logistics"]` 가 곧 구현이라 축이 프로세스 시작 때 굳었다.
    """
    return bind_sim_run(inbound.registered()["logistics"], 등록축)



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
    impl = _등록된()
    assert isinstance(impl, LogisticsInboundExecution), (
        f"등록된 것이 물류 구현이 아니다: {type(impl).__name__}"
    )


def test_마스터가_정한_장부에_앉힌다() -> None:
    """★ `sim_run_id` 는 마스터 값이다 — 물류 모듈 상수로 새면 실행이 둘이 되는 날 깨진다.

    🔴 **부른 쪽이 준 축이 그대로 앉는다** (`#531` 후속). 배선이 든 상수를 재면
       걷기가 번인 아닌 실행을 타는 날 **입고만 번인에 남는 것**을 못 잡는다.
    """
    assert _등록된()._sim_run_id == 등록축
    assert _등록된()._sim_run_id != BURN_IN_SIM_RUN_ID


# ---------------------------------------------------------------------------
# 2. 검수 원천을 **누가 골랐는가**
# ---------------------------------------------------------------------------


def test_검수_provider_는_물류의_것이다() -> None:
    """🔴 **마스터가 자기 판정을 지어내지 않는다.**

    `app/master/` 안의 클래스가 여기 오면 그것이 곧 마스터가 검수 규칙을 정한
    것이다 — `NoInspectionSource` 를 지운 이유가 그것이다.
    """
    impl = _등록된()
    provider = impl._provider
    assert isinstance(provider, ScenarioSimulatedInspectionProvider), (
        f"검수 provider 가 물류 것이 아니다: {type(provider).__name__}"
    )
    assert type(provider).__module__.startswith("app.logistics"), (
        f"검수 판정이 마스터 모듈에서 나온다: {type(provider).__module__}. "
        "검수 규칙의 주인은 물류다 (#336)"
    )


def test_배선이_provider_를_명시적으로_고른다() -> None:
    """⚠️ 기본값이 생기는 날 빨간불. **물류가 기본값을 안 둔 것이 계약이다.**

    저장소에 구현이 하나뿐이라고 그것이 기본값이 되면, 둘째 구현이 생기는 날
    **아무도 안 고른 것이 계속 돈다.**
    """
    with pytest.raises(TypeError):
        LogisticsInboundExecution(sim_run_id=BURN_IN_SIM_RUN_ID)  # type: ignore[call-arg]


def test_마스터가_합격률을_손대지_않는다() -> None:
    """🔴 **전량 PASS 는 물류의 MVP 가정이지 마스터가 정한 비율이 아니다.**

    마스터가 provider 를 감싸 수량을 깎거나 등급을 붙이면 그 순간 *"아무도 정한 적
    없는 비율"* 이 업무 사실이 된다 — 물류가 `#336` 에서 `random` · `seed` ·
    품목별 손실률을 다 거절한 이유가 그것이다.
    """
    impl = _등록된()
    assert type(impl._provider) is ScenarioSimulatedInspectionProvider, (
        "provider 가 감싸여 있다 — 마스터가 판정에 손댄 자리가 있는지 본다"
    )
