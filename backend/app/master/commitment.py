"""
commitment.py — 승인된 매입안 → **확정 입고 약정** (H1)

사람이 안을 고르면, 그 안은 물류의 **미래 창고 점유 계산에 겹쳐지는 사실**이 된다.
그 변환이 여기다.

```text
사용자 APPROVE → 승인된 시나리오 → ApprovedCommitment → 물류 H1 미래 점유
```

★ **오케스트레이터를 거치지 않는다** (지시 2026-09-01).
  같은 변환이 옛 `master/cycle.py` 에도 있었지만 그 경로는 M-1 관통에서 안 돌았고,
  **2026-09-08 에 그 파일을 지웠다.** 남은 것은 Critic 테스트가 시나리오를 짓는
  받침대(`tests/master/critic/cycle_harness.py`)뿐이고, 승인 약정의 주인은 여기다 —
  `tests/master/test_orchestrator_is_gone.py` 와
  `tests/master/test_master_legacy_cycle_is_gone.py` 가 그 방향을 잠근다.

★ **품목을 잃지 않는다.** 오케 쪽 변환은 회차 수량을 `sum(leg.qty_kg.values())` 로
  합쳐 품목을 없앴고, 그래서 물류 H1 이 총 kg 으로만 계산했다. *"배추 출고가 양파
  재고를 대신 소진한다"* 가 그 결과다 (물류 질의 2026-09-01 §1).

★ **마스터는 숫자를 만들지 않는다.** 여기서 하는 계산은 둘뿐이고 둘 다 옮기기다.

  ```text
  도착일 = 매입 실행일 + inbound_lead_days     N4 는 물류가 준다
  회차 수량 = 안이 적은 회차 수량 그대로        재계산하지 않는다
  회차 금액 = 안이 적은 회차 금액 그대로        총액을 회차 수로 나누지 않는다
  ```

  두 값 다 부서가 낸 것이고, 마스터는 **자리를 옮기기만** 한다 (§3.2.2).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.contracts.core import ITEMS

__all__ = [
    "ITEM_CODES",
    "ApprovedCommitment",
    "ArrivalLeg",
    "CommitmentNotBuildable",
    "SourcingLine",
    "build_commitment",
]

#: 🔴 `contracts/core.py` 의 `ItemCode` 는 `str` 별칭이고 품목 목록은 **주석**이다.
#:   그래서 `InventoryLot(item="(승인분)")` 같은 값이 품목 자리에 들어가도 아무도
#:   안 막았다 (2026-09-01 실측). 여기서는 값으로 막는다.
#:
#: ★ 다만 **목록을 여기서 다시 세지 않는다.** 2026-09-03 에 피마늘을 뺄 때, 계약은
#:   셋인데 여기만 넷으로 남는 것이 정확히 이 파일이 만들 수 있는 사고였다.
#:   막는 자리는 여기지만 무엇을 막을지는 계약이 정한다.
ITEM_CODES: frozenset[str] = frozenset(ITEMS)


class CommitmentNotBuildable(ValueError):
    """약정을 만들 수 없다. **비어 있는 약정을 대신 만들지 않는다.**

    ★ 여기서 조용히 0 이나 빈 값을 채우면, 물류가 *"입고 예정이 없다"* 로 읽는다.
      없는 것과 못 만든 것은 다르다 (§1.2-10).
    """


@dataclass(frozen=True)
class SourcingLine:
    """등급 조달 1줄. **매입이 보낸 모양 그대로다** (`sourcing_plan[]`).

    ★ 이름도 값도 안 바꾼다. `grade_unit_price` 를 `unit_price_krw_per_kg` 로 고쳐
      부르지 않고, 수량을 다시 세지도 않는다 — `amount_krw` 를 마스터가 안 만드는
      것과 같은 규율이다 (§3.2.2). 여기는 **옮기는 자리**다.

    ⚠️ `contracts/core.py` 의 `SourcingLot` 을 쓰지 않는다. 그쪽은 단가 이름이
      `unit_price_krw_per_kg` 라 매입이 보낸 `grade_unit_price` 를 옮기려면 이름을
      바꿔야 하고, 그 순간 **한 사실이 두 이름**이 된다.

    🔴 **회차(`ArrivalLeg`)와 다른 축이다.** 한 회차가 여러 등급을 담을 수 있고 한
       등급이 여러 회차에 걸칠 수 있다. 오늘은 둘 다 하나뿐이지만(#308 이 분할을
       못 세우고 있다) 그것이 계약이 아니다 — 그래서 등급을 회차에 붙이지 않고
       **목록으로 따로 나른다.** 분할이 서는 날 축이 갈려도 재료가 이미 여기 있다.
    """

    grade: str
    market: str | None = None
    qty_kg: float | None = None
    grade_unit_price: float | None = None


@dataclass(frozen=True)
class ArrivalLeg:
    """입고 1회분. **품목이 붙어 있다.**

    🔴 **`grade` 가 여기 없다.** 넣으면 *"회차 하나 = 등급 하나"* 가 계약으로 굳고,
       `#308` 이 풀려 2·3회차가 서는 날 **말없이 틀린다.** 등급은
       `ApprovedCommitment.sourcing_plan` 이 목록으로 나른다.
    """

    item: str
    qty_kg: float
    arrival_date: date
    purchase_date: date
    seq: int

    amount_krw: float | None = None
    """회차 금액. **매입이 보낸다** — 마스터가 총액을 회차 수로 나눠 만들지 않는다.

    ★ 여기는 스칼라다. 약정 하나가 품목 하나라 회차마다 품목이 하나뿐이다
      (`__post_init__` 이 `leg.item != self.item` 을 이미 막는다).
      `SplitLeg.amount_krw` 가 품목별 매핑인 것과 모순이 아니라, 축이 이미 좁혀진 자리다.

    ★ `None` 이 기본이고 매입이 값을 보내기 전까지는 늘 None 이다.
      0.0 으로 채우지 않는다 — 없는 것과 0 원은 다르다 (§1.2-10).
    """

    payment_due_date: date | None = None
    """매입대금 지급예정일. **매입일 + N5(재무 `purchase_payment_days`)** 다.

    ★ **`arrival_date` 와 완전히 같은 모양이다.** 물류가 봉투로 N4 를 주고 마스터가
      도착일을 만들듯, 재무가 봉투로 N5 를 주고 마스터가 지급일을 만든다 —
      **값은 부서가 공급하고 계산은 쓰는 쪽이 한다.** 마스터가 재무 DB 를 다시 읽으면
      같은 사실의 주인이 둘이 된다.

    🔴 **N5 가 없으면 `None` 이다 — 0 으로 대체하지 않는다.** N4 를 0 으로 못 쓰게 한
       것과 같은 이유다. 0 이면 *"오늘 승인분이 오늘 지급"* 이 되어 지급일이라는
       사실 자체가 사라진다. 없으면 없는 채로 두고, `purchases.payment_due_date` 가
       NOT NULL 이므로 **원장 쓰기가 그때 멈춘다** (`master/transition.py`).
    """


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
    sourcing_plan: tuple[SourcingLine, ...] = ()
    """매입이 짠 **등급별 조달**. 안이 적은 목록 그대로다.

    ★ **회차와 다른 축이라 목록이다.** 한 줄뿐인 오늘도 목록이고, 여럿이 되는 날
      모양이 안 바뀐다 — 스칼라로 접었다가 나중에 펴면 그 사이 쓰인 코드가 전부
      *"등급은 하나"* 를 가정하고 있다.

    ★ **비어 있을 수 있다.** 매입이 안 실어 보내면 빈 목록이고, 그러면 원장
      `purchase_items.grade` 도 `NULL` 로 남는다 — 지어내지 않는다 (§1.2-10).
    """
    inbound_lead_days: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.item not in ITEM_CODES:
            raise CommitmentNotBuildable(
                f"계약 품목이 아니다: {self.item!r}. 가능: {', '.join(sorted(ITEM_CODES))}"
            )
        if not self.arrival_schedule:
            return
        drift = self.total_qty_kg - sum(leg.qty_kg for leg in self.arrival_schedule)
        if abs(drift) > 1e-6:
            raise CommitmentNotBuildable(
                f"회차 합이 총량과 어긋난다 (차 {drift:g}kg) — 마스터가 맞춰 주지 않는다."
            )
        if any(leg.item != self.item for leg in self.arrival_schedule):
            raise CommitmentNotBuildable("회차 품목이 약정 품목과 다르다.")
        # ★ 금액도 수량과 같다 — 전 회차에 실려 있을 때만 본다. 하나도 없으면 오늘
        #   상태이므로 검사하지 않는다. 허용 오차를 수량과 같은 1e-6 으로 둔 것은
        #   같은 자리에서 다른 상수를 쓰면 왜 다른지를 아무도 모르기 때문이다.
        loaded = [leg.amount_krw for leg in self.arrival_schedule]
        if all(a is not None for a in loaded):
            drift = self.total_amount_krw - sum(a for a in loaded if a is not None)
            if abs(drift) > 1e-6:
                raise CommitmentNotBuildable(
                    f"회차 금액 합이 총액과 어긋난다 (차 {drift:g}원) — 마스터가 맞춰 주지 않는다."
                )

    @property
    def first_arrival(self) -> date | None:
        return min((leg.arrival_date for leg in self.arrival_schedule), default=None)

    @property
    def grades(self) -> tuple[str, ...]:
        """실려 온 등급. **중복 없이 · 온 순서대로 · 값은 그대로.**

        ★ **같은 등급이 두 줄이면 등급은 하나다.** 시장이 달라 줄이 갈린 것뿐이고
          등급 칸에 담길 값은 여전히 하나다 — 그때까지 막으면 담을 수 있는 것을
          못 담는다.

        🔴 **겹침 판정만 NFC 로 하고, 돌려주는 값은 받은 그대로다.** 조합형 `특` 과
           분해형 `특` 을 다른 등급으로 세면 등급이 하나인 안이 *"둘"* 로 읽혀 원장이
           엉뚱하게 멈춘다. 값 자체를 정규화해 내보내면 그건 매입이 보낸 문자열을
           마스터가 고쳐 쓴 것이다 — **비교만 접고 값은 안 만진다.**
        """
        seen: set[str] = set()
        표: list[str] = []
        for line in self.sourcing_plan:
            열쇠 = unicodedata.normalize("NFC", line.grade)
            if 열쇠 in seen:
                continue
            seen.add(열쇠)
            표.append(line.grade)
        return tuple(표)


def build_commitment(
    *,
    request_id: str,
    as_of: date,
    item: str | None,
    scenario: Mapping[str, Any],
    inbound_lead_days: Any,
    decision_seq: int,
    purchase_payment_days: Any = None,
) -> ApprovedCommitment:
    """승인된 시나리오 하나를 약정으로 옮긴다.

    :param purchase_payment_days: N5. **재무가 봉투로 준다** — 마스터는 옮기기만 한다.
        `inbound_lead_days`(N4)와 완전히 같은 자리다. 없으면 `None` 이고, 그러면
        회차의 `payment_due_date` 도 `None` 으로 남는다 — 0 으로 대체하지 않는다.
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
    legs, notes = _legs(
        scenario.get("split_plan"),
        item,
        as_of,
        lead,
        _number(purchase_payment_days),
    )

    return ApprovedCommitment(
        approval_id=f"H1-{request_id}-{decision_seq}",
        request_id=request_id,
        as_of=as_of,
        item=item,
        scenario_label=str(scenario.get("label") or ""),
        total_qty_kg=total_qty,
        total_amount_krw=total_amount,
        arrival_schedule=legs,
        sourcing_plan=_sourcing(scenario.get("sourcing_plan")),
        inbound_lead_days=lead,
        notes=notes,
    )


def _sourcing(raw: Any) -> tuple[SourcingLine, ...]:
    """안의 `sourcing_plan` 을 **줄 수 그대로** 옮긴다.

    🔴 **접지 않는다.** 등급별 수량을 합치거나 대표 등급 하나로 줄이면, 그 순간
       *"등급이 여럿이었다"* 는 사실이 사라져 원장이 막아야 할 자리를 통과한다.
       줄이 셋이면 셋을 그대로 들고 온다.

    ★ **검사하지 않는다.** 수량 합이 총량과 맞는지, 단가가 양수인지는 매입
      `SourcingPlanItem` 이 이미 본다. 여기서 다시 세면 허용 오차가 갈리는 날
      같은 안을 한 곳은 통과시키고 한 곳은 막는다.

    ★ **없으면 빈 목록이다.** `sourcing_plan` 이 아예 없는 안(조회·옛 응답)이 있고,
      그때 등급이 없다는 것은 정상 상태다 — 못 만든 것이 아니라 안 온 것이다.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    lines: list[SourcingLine] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        grade = entry.get("grade")
        if not isinstance(grade, str) or not grade.strip():
            # ★ 매입 `SourcingPlanItem.grade` 는 `NonEmptyStr` 라 여기 오는 빈 등급은
            #   계약 밖 값이다. 지어내지 않고 **등급을 안 싣는다** — 이 줄이 빠지면
            #   등급이 하나도 안 남아 원장이 `NULL` 로 가고, 그것이 정직한 결과다.
            continue
        market = entry.get("market")
        lines.append(
            SourcingLine(
                grade=grade,
                market=market if isinstance(market, str) else None,
                qty_kg=_number(entry.get("qty_kg")),
                grade_unit_price=_number(entry.get("grade_unit_price")),
            )
        )
    return tuple(lines)


def _legs(
    split_plan: Any,
    item: str,
    as_of: date,
    lead: float | None,
    payment_days: float | None = None,
) -> tuple[tuple[ArrivalLeg, ...], tuple[str, ...]]:
    """회차별 입고. **N4 가 없으면 일정을 만들지 않는다.**

    🔴 `lead` 를 0 으로 대체하면 *"오늘 승인분이 오늘 도착"* 이 되어 재고 전환 금지가
      무의미해진다 (§1.2-10 · §3.2.3). 일정 없이 약정만 남기고, **왜 없는지를 적는다.**

    ★ **N5(`payment_days`)는 다르다 — 없어도 일정은 선다.** 지급일이 없다고 입고가
      없는 것은 아니다. `payment_due_date` 만 `None` 으로 두고, 그 상태로 원장을 쓸 수
      없다는 판단은 전이 경계가 한다. 다만 **일수로 읽히지 않는 값**은 도착일과 같은
      태도로 막는다 — 지어낸 지급일이 원장에 남는 것보다 멈추는 편이 낫다.
    """
    if not isinstance(split_plan, Sequence) or isinstance(split_plan, (str, bytes)):
        return (), ("안에 분할 계획이 없어 회차별 입고 일정을 만들지 못했다.",)
    supplied = [
        _date(raw.get("expected_arrival_date")) for raw in split_plan if isinstance(raw, Mapping)
    ]
    filled = sum(1 for eta in supplied if eta is not None)
    if 0 < filled < len(supplied):
        # 🔴 **부분 공급은 섞어 만들지 않는다 (매입 제보 2026-09-01).** 전에는 실린
        #   회차는 매입 값, 빈 회차는 마스터 계산으로 **출처가 섞인 일정**이 나갔고,
        #   N4 까지 없으면 실린 값마저 "N4 가 없어" 라는 **틀린 사유**로 통째로
        #   버려졌다. null 은 "N4 미결로 매입도 못 냈다" 인데 같은 제안에서 회차마다
        #   있고 없고는 자기모순이다 — 매입도 구조적으로 부분을 안 낸다(_rounds 가
        #   통째로 쓰거나 통째로 None). 부분은 계약 이상 신호로 보고 일정을 안 만든다.
        note = (
            f"회차 도착일이 {len(supplied)}회차 중 {filled}회차만 실려 있어"
            " 일정을 만들지 않았다 — 출처를 섞어 만들지 않는다."
        )
        return (), (note,)
    # ★ **금액도 도착일과 같은 부분 공급 규칙을 따른다.** 같은 제안에서 회차마다 있고
    #   없고는 자기모순이고, 섞어 만들면 출처가 섞인 값이 원장(purchase_items)으로 간다.
    #
    #   ⚠️ 다만 **버리는 것이 다르다.** 도착일이 없으면 회차가 성립하지 않아 일정을
    #     통째로 버리지만, 금액이 없어도 회차는 선다 — 오늘이 정확히 그 상태다.
    #     그래서 금액만 안 싣고 일정은 만든다.
    amounts = [_number(raw.get("amount_krw")) for raw in split_plan if isinstance(raw, Mapping)]
    amount_filled = sum(1 for a in amounts if a is not None)
    carry_amounts = bool(amounts) and amount_filled == len(amounts)
    amount_notes: tuple[str, ...] = ()
    if 0 < amount_filled < len(amounts):
        amount_notes = (
            (
                f"회차 금액이 {len(amounts)}회차 중 {amount_filled}회차만 실려 있어"
                " 금액을 싣지 않았다 — 출처를 섞어 만들지 않는다."
            ),
        )

    if filled and filled == len(supplied):
        lead = 0.0  # 계산 안 함 — 아래 게이트만 지나가는 무해한 값. 회차마다 매입 값을 쓴다
    elif lead is None:
        return (), ("물류 inbound_lead_days(N4) 가 없어 도착일을 계산하지 못했다.",)
    if lead < 0 or lead != int(lead):
        # 🔴 처음에는 `int(lead)` 로 바로 잘랐다 (2026-09-01 자기 리뷰에서 발견).
        #   2.9 가 조용히 2일이 되고 -1 은 **매입일보다 과거 도착**을 만들었다 —
        #   에러 없이 창고 점유가 하루 이르게 계산되는 종류다. 일수로 읽을 수 없는
        #   값이면 자르지 않고 일정을 안 만든다. N4 를 마스터가 고쳐 주지 않는다.
        return (), (f"inbound_lead_days 가 일수로 읽히지 않아({lead:g}) 도착일을 계산하지 않았다.",)

    # ★ N5 도 도착일과 같은 문을 지난다. 없으면(`None`) 통과하고 지급일만 안 싣는다 —
    #   그 경우가 오늘의 정상 상태다. 있는데 일수로 안 읽히면 지어내지 않고 멈춘다.
    pay_days: int | None = None
    if payment_days is not None:
        if payment_days < 0 or payment_days != int(payment_days):
            note = (
                f"purchase_payment_days 가 일수로 읽히지 않아({payment_days:g})"
                " 지급일을 계산하지 않았다."
            )
            return (), (note,)
        pay_days = int(payment_days)

    legs: list[ArrivalLeg] = []
    for index, raw in enumerate(split_plan, 1):
        if not isinstance(raw, Mapping):
            continue
        qty = _number(raw.get("qty_kg"))
        purchase_date = _date(raw.get("date"))
        if qty is None or purchase_date is None:
            return (), (f"{index}회차에 수량 또는 매입일이 없어 일정을 만들지 못했다.",)
        # ★ **매입이 도착일을 실어 주면 계산하지 않는다** (매입 회신 2026-09-01 합의).
        #   매입은 arrival_dates(§5.5)로 같은 값을 이미 계산한다 — 같은 사실을 두 곳에서
        #   각자 계산하면 어긋나는 날이 온다. null 이면 "N4 미결로 매입도 못 냈다"이므로
        #   마스터도 계산하지 않는다(같은 N4 원천을 다시 쓰면 두-곳-계산이 재현된다).
        eta = _date(raw.get("expected_arrival_date"))
        if eta is not None and eta < purchase_date:
            # 🔴 받은 값도 모순은 막는다 (매입 참고 2026-09-01). 마스터 계산 경로는
            #   lead<0 을 막는데 수신 값은 안 보고 있었다 — 도착이 매입보다 앞서면
            #   물류 점유가 이르게 계산되고 에러가 안 난다. 고쳐 쓰지 않고 막는다.
            raise CommitmentNotBuildable(
                f"{index}회차 도착일({eta})이 매입일({purchase_date})보다 앞선다"
                " — 받은 값을 고쳐 쓰지 않는다."
            )
        if eta is None:
            # ★ **매입 실행일 + N4 다.** 안의 `date` 는 도착일이 아니라 매입일이다
            #   (매입 schemas.py SplitPlanItem 주석 — 처음엔 "IO명세 §4"로 잘못 인용했다.
            #   §4는 1차 범위 밖 변환 계약이다 · 매입 정정 2026-09-01).
            #   도착일로 읽으면 물류 cap_by_date 창(도착일 기준) **밖의 키**를 조회해
            #   검사 자체가 안 돈다 — 과소 계산이 아니라 그보다 앞에서 무너진다.
            eta = purchase_date + timedelta(days=int(lead))
        legs.append(
            ArrivalLeg(
                item=item,
                qty_kg=qty,
                arrival_date=eta,
                purchase_date=purchase_date,
                seq=int(raw.get("seq") or index),
                amount_krw=_number(raw.get("amount_krw")) if carry_amounts else None,
                # ★ **도착일 옆에서 같이 만든다.** 기준은 매입일이다 — 재무 전이가
                #   `purchase_date + N5` 로 만기를 세우는 것과 같은 식이어야 원장의
                #   `purchases.payment_due_date` 와 `payables.due_date` 가 갈리지 않는다.
                payment_due_date=(
                    purchase_date + timedelta(days=pay_days) if pay_days is not None else None
                ),
            )
        )
    if not legs:
        return (), ("분할 계획이 비어 회차별 입고 일정을 만들지 못했다.",)
    return tuple(legs), amount_notes


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
