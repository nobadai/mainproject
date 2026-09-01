"""
commitment.py — 승인된 매입안 → **확정 입고 약정** (H1)

사람이 안을 고르면, 그 안은 물류의 **미래 창고 점유 계산에 겹쳐지는 사실**이 된다.
그 변환이 여기다.

```text
사용자 APPROVE → 승인된 시나리오 → ApprovedCommitment → 물류 H1 미래 점유
```

★ **오케스트레이터를 거치지 않는다** (지시 2026-09-01).
  같은 변환이 `orchestrator/cycle.py` 에도 있지만 그 경로는 M-1 관통에서 안 돈다.
  거기 것을 부르지 않고 여기서 만든다 — `tests/master/test_no_orchestrator_runtime.py`
  가 그 방향을 잠근다.

★ **품목을 잃지 않는다.** 오케 쪽 변환은 회차 수량을 `sum(leg.qty_kg.values())` 로
  합쳐 품목을 없앴고, 그래서 물류 H1 이 총 kg 으로만 계산했다. *"배추 출고가 양파
  재고를 대신 소진한다"* 가 그 결과다 (물류 질의 2026-09-01 §1).

★ **마스터는 숫자를 만들지 않는다.** 여기서 하는 계산은 둘뿐이고 둘 다 옮기기다.

  ```text
  도착일 = 매입 실행일 + inbound_lead_days     N4 는 물류가 준다
  회차 수량 = 안이 적은 회차 수량 그대로        재계산하지 않는다
  ```

  두 값 다 부서가 낸 것이고, 마스터는 **자리를 옮기기만** 한다 (§3.2.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

__all__ = [
    "ITEM_CODES",
    "ApprovedCommitment",
    "ArrivalLeg",
    "CommitmentNotBuildable",
    "build_commitment",
]

#: 4품목 체제. **여기가 마스터의 어휘 정본이다.**
#:
#: 🔴 `orchestrator/contracts_core.py:88` 의 `ItemCode` 는 `str` 별칭이고 4품목은
#:   **주석**이다. 그래서 `InventoryLot(item="(승인분)")` 같은 값이 품목 자리에
#:   들어가도 아무도 안 막았다 (2026-09-01 실측). 여기서는 값으로 막는다.
ITEM_CODES: frozenset[str] = frozenset({"배추", "무", "양파", "피마늘"})


class CommitmentNotBuildable(ValueError):
    """약정을 만들 수 없다. **비어 있는 약정을 대신 만들지 않는다.**

    ★ 여기서 조용히 0 이나 빈 값을 채우면, 물류가 *"입고 예정이 없다"* 로 읽는다.
      없는 것과 못 만든 것은 다르다 (§1.2-10).
    """


@dataclass(frozen=True)
class ArrivalLeg:
    """입고 1회분. **품목이 붙어 있다.**"""

    item: str
    qty_kg: float
    arrival_date: date
    purchase_date: date
    seq: int


@dataclass(frozen=True)
class ApprovedCommitment:
    """승인 1건이 만드는 확정 입고 약정.

    ★ `total_qty_kg` 는 **안이 적은 값**이고, `sum(leg.qty_kg)` 와 어긋나면
      `__post_init__` 이 막는다. 마스터가 둘 중 하나를 고쳐 맞추지 않는다 —
      고친 값이 근거가 되면 그건 검증이 아니라 창작이다.
    """

    approval_id: str
    request_id: str
    as_of: date
    item: str
    scenario_label: str
    total_qty_kg: float
    total_amount_krw: float
    arrival_schedule: tuple[ArrivalLeg, ...] = ()
    inbound_lead_days: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.item not in ITEM_CODES:
            raise CommitmentNotBuildable(f"품목이 4품목 어휘가 아니다: {self.item!r}")
        if not self.arrival_schedule:
            return
        drift = self.total_qty_kg - sum(leg.qty_kg for leg in self.arrival_schedule)
        if abs(drift) > 1e-6:
            raise CommitmentNotBuildable(
                f"회차 합이 총량과 어긋난다 (차 {drift:g}kg) — 마스터가 맞춰 주지 않는다."
            )
        if any(leg.item != self.item for leg in self.arrival_schedule):
            raise CommitmentNotBuildable("회차 품목이 약정 품목과 다르다.")

    @property
    def first_arrival(self) -> date | None:
        return min((leg.arrival_date for leg in self.arrival_schedule), default=None)


def build_commitment(
    *,
    request_id: str,
    as_of: date,
    item: str | None,
    scenario: Mapping[str, Any],
    inbound_lead_days: Any,
    decision_seq: int,
) -> ApprovedCommitment:
    """승인된 시나리오 하나를 약정으로 옮긴다.

    :raises CommitmentNotBuildable: 옮길 수 없을 때. **빈 약정을 만들지 않는다.**
    """
    if not item:
        raise CommitmentNotBuildable("실행에 품목이 없다 — 약정에 실을 품목을 지어내지 않는다.")

    total_qty = _number(scenario.get("total_qty_kg"))
    if total_qty is None:
        raise CommitmentNotBuildable("안에 총량이 없다.")
    total_amount = _number(scenario.get("total_amount_krw"))
    if total_amount is None:
        raise CommitmentNotBuildable("안에 총액이 없다.")

    lead = _number(inbound_lead_days)
    legs, notes = _legs(scenario.get("split_plan"), item, as_of, lead)

    return ApprovedCommitment(
        approval_id=f"H1-{request_id}-{decision_seq}",
        request_id=request_id,
        as_of=as_of,
        item=item,
        scenario_label=str(scenario.get("label") or ""),
        total_qty_kg=total_qty,
        total_amount_krw=total_amount,
        arrival_schedule=legs,
        inbound_lead_days=lead,
        notes=notes,
    )


def _legs(
    split_plan: Any,
    item: str,
    as_of: date,
    lead: float | None,
) -> tuple[tuple[ArrivalLeg, ...], tuple[str, ...]]:
    """회차별 입고. **N4 가 없으면 일정을 만들지 않는다.**

    🔴 `lead` 를 0 으로 대체하면 *"오늘 승인분이 오늘 도착"* 이 되어 재고 전환 금지가
      무의미해진다 (§1.2-10 · §3.2.3). 일정 없이 약정만 남기고, **왜 없는지를 적는다.**
    """
    if not isinstance(split_plan, Sequence) or isinstance(split_plan, (str, bytes)):
        return (), ("안에 분할 계획이 없어 회차별 입고 일정을 만들지 못했다.",)
    if lead is None:
        return (), ("물류 inbound_lead_days(N4) 가 없어 도착일을 계산하지 못했다.",)
    if lead < 0 or lead != int(lead):
        # 🔴 처음에는 `int(lead)` 로 바로 잘랐다 (2026-09-01 자기 리뷰에서 발견).
        #   2.9 가 조용히 2일이 되고 -1 은 **매입일보다 과거 도착**을 만들었다 —
        #   에러 없이 창고 점유가 하루 이르게 계산되는 종류다. 일수로 읽을 수 없는
        #   값이면 자르지 않고 일정을 안 만든다. N4 를 마스터가 고쳐 주지 않는다.
        return (), (f"inbound_lead_days 가 일수로 읽히지 않아({lead:g}) 도착일을 계산하지 않았다.",)

    legs: list[ArrivalLeg] = []
    for index, raw in enumerate(split_plan, 1):
        if not isinstance(raw, Mapping):
            continue
        qty = _number(raw.get("qty_kg"))
        purchase_date = _date(raw.get("date"))
        if qty is None or purchase_date is None:
            return (), (f"{index}회차에 수량 또는 매입일이 없어 일정을 만들지 못했다.",)
        legs.append(
            ArrivalLeg(
                item=item,
                qty_kg=qty,
                # ★ **매입 실행일 + N4 다.** 안의 `date` 는 도착일이 아니라 매입일이다
                #   (매입 IO명세 §4). 그것을 도착일로 읽으면 리드타임만큼 앞당겨진다.
                arrival_date=purchase_date + timedelta(days=int(lead)),
                purchase_date=purchase_date,
                seq=int(raw.get("seq") or index),
            )
        )
    if not legs:
        return (), ("분할 계획이 비어 회차별 입고 일정을 만들지 못했다.",)
    return tuple(legs), ()


def _number(value: Any) -> float | None:
    """숫자만 받는다. **`bool` 은 숫자가 아니다.**"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
