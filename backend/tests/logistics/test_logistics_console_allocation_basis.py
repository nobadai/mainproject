"""운영 콘솔(#410)의 **사람 입력**에 자동 선택 어휘가 새지 않는지 잠근다.

```text
사람이 보내는 값 (ConsoleAllocateRequest)   FEFO_TOOL_CONFIRMED · HUMAN_OVERRIDE
장부에 적히는 값 · 조회 (ConsoleAllocation)  위 둘 + FEFO_AUTO_SELECTED
```

🔴 **왜 갈라야 하나.** `FEFO_AUTO_SELECTED` 는 시뮬레이션 자동 경로
   (`fefo_allocation.allocate_reserved_stock_fefo`) 가 **스스로 적는 값**이다.
   사람이 그 값을 API 로 보내면 *"규칙이 자동으로 골랐다"* 가 거짓으로 서고,
   나중에 왜 그 Lot 이었는지 물을 때 근거가 없다.

⚠️ **조회 쪽은 좁히지 않는다.** 자동으로 선 할당도 콘솔에서 읽혀야 한다 —
   거기서 좁히면 이미 적힌 사실을 못 읽는다.

★ **DB 를 안 읽는다.** Pydantic 검증과 타입만 보므로 `-m db` 없이 기본 스위트에 든다.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

from app.logistics import console_service
from app.logistics.console_schemas import (
    ConsoleAllocateRequest,
    ConsoleAllocation,
    ConsoleAllocationRequestItem,
)
from app.logistics.outbound import AllocationBasis, HumanAllocationBasis

_HUMAN = ("FEFO_TOOL_CONFIRMED", "HUMAN_OVERRIDE")
_AUTO = "FEFO_AUTO_SELECTED"

_DECIDED_AT = datetime(2026, 1, 20, 9, 34, tzinfo=UTC)


def _request(basis: str) -> ConsoleAllocateRequest:
    return ConsoleAllocateRequest(
        requests=[ConsoleAllocationRequestItem(lot_id="LOT-A", quantity_kg=Decimal(10))],
        decided_by="WH-PLANNER-01",
        decided_at=_DECIDED_AT,
        allocation_basis=basis,  # type: ignore[arg-type]
        as_of=date(2026, 1, 20),
    )


# ── 사람 입력 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("basis", _HUMAN)
def test_사람이_고를_수_있는_근거는_통과한다(basis: str) -> None:
    assert _request(basis).allocation_basis == basis


def test_자동_선택_어휘는_사람_입력에서_거부된다() -> None:
    """🔴 사람이 안 한 일이 장부에 서는 것을 **입구에서** 막는다."""
    with pytest.raises(ValidationError) as 잡힌것:
        _request(_AUTO)

    assert "allocation_basis" in str(잡힌것.value)


def test_계약_밖_어휘도_거부된다() -> None:
    with pytest.raises(ValidationError):
        _request("AUTO_PICKED")


def test_근거에_기본값이_없다() -> None:
    """★ 안 주면 검증에서 걸린다 — 묻지도 않고 장부에 적지 않는다."""
    with pytest.raises(ValidationError) as 잡힌것:
        ConsoleAllocateRequest(
            requests=[ConsoleAllocationRequestItem(lot_id="LOT-A", quantity_kg=Decimal(10))],
            decided_by="WH-PLANNER-01",
            decided_at=_DECIDED_AT,
            as_of=date(2026, 1, 20),
        )

    assert "allocation_basis" in str(잡힌것.value)


# ── 조회 응답 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("basis", [*_HUMAN, _AUTO])
def test_조회_응답은_세_어휘를_다_싣는다(basis: str) -> None:
    """⚠️ 여기서 좁히면 자동으로 선 할당을 콘솔이 못 읽는다."""
    보이는것 = ConsoleAllocation(
        allocation_id="ALC-RSV-1-LOT-A",
        lot_id="LOT-A",
        pallet_id=None,
        allocated_qty_kg=Decimal(10),
        allocation_basis=basis,  # type: ignore[arg-type]
        decided_by="LOGISTICS_FEFO_RULE",
        decided_at=_DECIDED_AT,
        status="ALLOCATED",
        note=None,
    )

    assert 보이는것.allocation_basis == basis


# ── 타입 경계 ───────────────────────────────────────────────────────────


def test_어휘_둘의_관계가_고정돼_있다() -> None:
    assert set(get_args(HumanAllocationBasis)) == set(_HUMAN)
    assert set(get_args(AllocationBasis)) == set(_HUMAN) | {_AUTO}


def test_사람_Command_서비스도_좁은_타입을_받는다() -> None:
    """★ 스키마만 좁히고 서비스가 넓으면 다른 호출자가 그리로 샌다."""
    힌트 = inspect.signature(console_service.allocate_reservation).parameters

    assert 힌트["allocation_basis"].annotation == "HumanAllocationBasis"


def test_코어는_좁히지_않는다() -> None:
    """🔴 `allocate_stock` 은 사람 경로와 자동 경로가 함께 쓴다 — 셋을 다 받아야 한다."""
    from app.logistics import outbound

    힌트 = inspect.signature(outbound.allocate_stock).parameters

    assert 힌트["allocation_basis"].annotation == "AllocationBasis"
