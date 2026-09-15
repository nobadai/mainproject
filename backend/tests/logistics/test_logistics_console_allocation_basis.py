"""할당 근거 어휘의 **타입 경계**를 잠근다.

```text
조회 (ConsoleAllocation)         FEFO_TOOL_CONFIRMED · HUMAN_OVERRIDE · FEFO_AUTO_SELECTED
코어 (outbound.allocate_stock)   셋 다 받는다 — 사람 경로와 자동 경로가 함께 쓴다
사람 어휘 (HumanAllocationBasis) 앞 둘
```

🔴 **왜 갈라 두나.** `FEFO_AUTO_SELECTED` 는 시뮬레이션 자동 경로
   (`fefo_allocation.allocate_reserved_stock_fefo`) 가 **스스로 적는 값**이다. 사람이 그
   값을 적으면 *"규칙이 자동으로 골랐다"* 가 거짓으로 서고, 나중에 왜 그 Lot 이었는지
   물을 때 근거가 없다.

⚠️ **조회 쪽은 좁히지 않는다.** 자동으로 선 할당도 화면에서 읽혀야 한다 — 거기서
   좁히면 이미 적힌 사실을 못 읽는다.

★ **사람 입력 검사 넷은 2026-09-15 에 지웠다 — 검사를 피한 것이 아니라 그 입구가
  없어졌다.** 콘솔 Command(`POST /logistics/outbound/{id}/allocations`)와 그 DTO
  (`ConsoleAllocateRequest`)를 부르는 자리가 `app/logistics/router.py` 하나였고, 그
  라우터를 화면도 마스터도 안 써서 걷어냈다 (물류 문서 28). 지금 사람이 근거를 적는
  HTTP 입구는 **없다.** 되살릴 때는 그 검사도 같이 되살린다.

★ **DB 를 안 읽는다.** Pydantic 검증과 타입만 보므로 `-m db` 없이 기본 스위트에 든다.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import get_args

import pytest

from app.logistics.outbound import AllocationBasis, HumanAllocationBasis
from app.logistics.schemas import ConsoleAllocation

_HUMAN = ("FEFO_TOOL_CONFIRMED", "HUMAN_OVERRIDE")
_AUTO = "FEFO_AUTO_SELECTED"

_DECIDED_AT = datetime(2026, 1, 20, 9, 34, tzinfo=UTC)


# ── 조회 응답 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("basis", [*_HUMAN, _AUTO])
def test_조회_응답은_세_어휘를_다_싣는다(basis: str) -> None:
    """⚠️ 여기서 좁히면 자동으로 선 할당을 화면이 못 읽는다."""
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
    """🔴 사람 어휘와 전체 어휘가 갈려 있어야 한다 — 입구가 다시 생길 때 이 경계를 쓴다."""
    assert set(get_args(HumanAllocationBasis)) == set(_HUMAN)
    assert set(get_args(AllocationBasis)) == set(_HUMAN) | {_AUTO}


def test_코어는_좁히지_않는다() -> None:
    """🔴 `allocate_stock` 은 사람 경로와 자동 경로가 함께 쓴다 — 셋을 다 받아야 한다."""
    from app.logistics import outbound

    힌트 = inspect.signature(outbound.allocate_stock).parameters

    assert 힌트["allocation_basis"].annotation == "AllocationBasis"
