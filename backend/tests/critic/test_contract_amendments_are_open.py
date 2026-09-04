"""`CONTRACT_AMENDMENTS` 가 **아직 안 된 일만** 담고 있는지 계약에서 직접 확인한다.

🔴 2026-09-04 — 목록 여섯 중 하나(`ApprovedPurchaseCommitment.arrival_schedule`)가
   이미 계약에 들어갔는데 목록에 남아 있었다. 같은 파일 머리에는
   `contracts_core.py 는 FROZEN` 이라고 적혀 있었지만 계약은 그 뒤로 두 번
   개정됐다 (v1.2.1 · #265).

   **닫힌 요청이 목록에 남아 있으면 다음 사람이 사이드카를 또 만든다.**
   `critic_v0_4.py` 의 `DeptMeta` 가 정확히 그렇게 생긴 우회다.

주석은 다시 낡는다. 그래서 그날 낡은 것을 발견한 방법 자체를 여기에 박는다 —
항목마다 계약에서 확인할 수 있는 **탐침**을 달고, 누가 그 개정을 구현하는 날
탐침이 뒤집혀 이 검사가 red 가 된다. 그 red 가 목록에서 걷으라는 신호다.

탐침은 계약(`app.contracts.core`)만 읽는다. 이 파일이 계약의 현재 상태를
따로 적어 두면 그 사본이 또 낡는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from typing import get_args

import pytest

from app.contracts.core import (
    FAIL_ROUTING,
    ApprovedPurchaseCommitment,
    CheckResult,
    CriticLayer,
    CriticVerdict,
    EndStage,
    T2Reply,
)
from app.critic.critic_v0_4 import CONTRACT_AMENDMENTS, CONTRACT_AMENDMENTS_CLOSED


def _field_names(cls: type) -> set[str]:
    return {f.name for f in fields(cls)}


def _annotation_of(cls: type, name: str) -> str:
    """필드 주석을 문자열로. 계약은 `from __future__ import annotations` 라 이미 문자열이다."""
    for f in fields(cls):
        if f.name == name:
            return str(f.type)
    return ""


# ---------------------------------------------------------------------------
# 탐침 — (계약에서 무엇을 읽는가, 아직 열려 있는가)
# ---------------------------------------------------------------------------
#
# 반환 True = **아직 안 됐다**(목록에 있어야 한다). False = 계약에 반영됐다(걷어야 한다).

_Probe = tuple[str, Callable[[], bool]]

_PROBES: dict[str, _Probe] = {
    "CheckResult.inputs_used": (
        "dataclasses.fields(CheckResult) 에 inputs_used 가 있는가",
        lambda: "inputs_used" not in _field_names(CheckResult),
    ),
    "T2Reply.produced_fields": (
        "dataclasses.fields(T2Reply) 에 produced_fields 가 있는가",
        lambda: "produced_fields" not in _field_names(T2Reply),
    ),
    "CriticLayer 에 L0_format · L4_combine · L5_logic 추가": (
        "typing.get_args(CriticLayer) 와 FAIL_ROUTING 키에 세 값이 다 있는가",
        # 요청은 "CriticLayer + FAIL_ROUTING 함께 확장" 이다. 한쪽만 되면 아직 열린 것이다 —
        # 라우팅이 없는 레이어로 FAIL 이 나면 FAIL_ROUTING[layer] 가 KeyError 로 죽는다.
        lambda: (
            not (
                {"L0_format", "L4_combine", "L5_logic"} <= set(get_args(CriticLayer))
                and {"L0_format", "L4_combine", "L5_logic"} <= set(FAIL_ROUTING)
            )
        ),
    ),
    "EndStage 에 CRITIC_A · CRITIC_B 추가": (
        "typing.get_args(EndStage) 에 CRITIC_A · CRITIC_B 가 있는가",
        lambda: not {"CRITIC_A", "CRITIC_B"} <= set(get_args(EndStage)),
    ),
    "CriticVerdict.status: PASS|CONCERN|FAIL": (
        "CriticVerdict 에 status 필드가 있고 그 주석이 PASS·CONCERN·FAIL 을 담는가",
        # 이름만 보지 않는다. `status: bool` 이 들어와도 요청(3값)은 안 된 것이다.
        lambda: (
            not all(
                v in _annotation_of(CriticVerdict, "status") for v in ("PASS", "CONCERN", "FAIL")
            )
        ),
    ),
    "ApprovedPurchaseCommitment.arrival_schedule": (
        "dataclasses.fields(ApprovedPurchaseCommitment) 에 arrival_schedule 이 있는가",
        lambda: "arrival_schedule" not in _field_names(ApprovedPurchaseCommitment),
    ),
}


def _probe(key: str) -> _Probe:
    assert key in _PROBES, (
        f"계약 개정 '{key}' 에 탐침이 없다.\n"
        f"  할 일: 이 파일 `_PROBES` 에 계약에서 확인할 수 있는 탐침을 달아라.\n"
        f"         (반환 True = 아직 안 됐다 / False = 계약에 반영됐다)\n"
        f"  탐침이 없으면 이 항목만 낡는 것을 아무도 못 잡는다 — 오늘 arrival_schedule 이 그랬다."
    )
    return _PROBES[key]


@pytest.mark.parametrize("key", [k for k, _ in CONTRACT_AMENDMENTS])
def test_열린_개정은_계약에_아직_없다(key: str) -> None:
    """목록에 있는 것이 정말로 아직 안 됐는가. 되어 있으면 걷으라고 말한다."""
    reads, still_open = _probe(key)
    assert still_open(), (
        f"계약 개정 '{key}' 이 이미 반영됐다.\n"
        f"  확인한 것: {reads}\n"
        f"  할 일: `app/critic/critic_v0_4.py` 의 CONTRACT_AMENDMENTS 에서 이 항목을 걷고,\n"
        f"         CONTRACT_AMENDMENTS_CLOSED 로 옮겨라\n"
        f"         (요청, 무엇이 닫았나, 원래 왜 필요했나).\n"
        f"         사이드카(DeptMeta 등)로 우회하던 자리가 있으면 계약을 직접 읽도록 바꿔라.\n"
        f"  닫힌 요청이 남아 있으면 다음 사람이 사이드카를 또 만든다."
    )


@pytest.mark.parametrize("key", [k for k, _, _ in CONTRACT_AMENDMENTS_CLOSED])
def test_닫힌_개정은_계약에_정말로_있다(key: str) -> None:
    """닫혔다고 적어 둔 것이 계약에서 사라지면(되돌림·리네임) 여기서 잡힌다."""
    reads, still_open = _probe(key)
    assert not still_open(), (
        f"계약 개정 '{key}' 은 닫혔다고 적혀 있는데 계약에 없다.\n"
        f"  확인한 것: {reads}\n"
        f"  할 일: 계약이 되돌아갔으면 CONTRACT_AMENDMENTS 로 되돌리고,\n"
        f"         이름만 바뀐 것이면 이 파일의 탐침을 새 이름으로 맞춰라."
    )


def test_탐침_표가_두_목록을_정확히_덮는다() -> None:
    """탐침만 남고 항목이 사라지는 반대 방향도 막는다. 표가 낡으면 검사가 헛돈다."""
    listed = {k for k, _ in CONTRACT_AMENDMENTS} | {k for k, _, _ in CONTRACT_AMENDMENTS_CLOSED}
    assert set(_PROBES) == listed, (
        f"탐침 표와 개정 목록이 어긋난다.\n"
        f"  탐침만 있는 것: {sorted(set(_PROBES) - listed)}\n"
        f"  목록만 있는 것: {sorted(listed - set(_PROBES))}"
    )


def test_같은_요청이_두_목록에_동시에_있지_않다() -> None:
    """열린 것과 닫힌 것의 주인은 하나다."""
    both = {k for k, _ in CONTRACT_AMENDMENTS} & {k for k, _, _ in CONTRACT_AMENDMENTS_CLOSED}
    assert not both, f"열림·닫힘 양쪽에 있는 요청: {sorted(both)}"
