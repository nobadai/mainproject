"""재고·물류 Agent의 결정론적 계산 도구."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from app.logistics.schemas import (
    InventoryByItem,
    InventoryCostBasisSnapshot,
    InventoryLogisticsSnapshot,
    InventoryLotSnapshot,
    LogisticsApprovedPurchaseCommitment,
    LotConstraint,
    PurchaseAgentOutput,
    ScheduledQuantity,
)

# 🔴 **FEFO 정렬 규칙을 여기서 다시 적지 않는다.** 키의 주인은 `turnover.fefo_sort_key`
#    하나이고 실제 자동 출고(`outbound.recommend_fefo_candidates`)도 같은 것을 쓴다 —
#    두 벌로 적으면 «나갈 Lot» 과 «원가를 배부한 Lot» 이 갈린다.
from app.logistics.turnover import fefo_sort_key

#: cap_by_date 조회 창 길이 (`as_of + inbound_lead_days`부터, Policy 확정값 18).
#: Window 밖은 0이 아니라 미조회 영역이다.
#:
#: ★ **이 값과 창 구성은 여기 한 벌이다** (#121 ⑤). 종전에는 어댑터가 같은 18과
#:   같은 날짜 나열을 따로 들고 있어, 한쪽만 바뀌는 날 판정 창(CAPACITY_TIGHT)과
#:   M-1 조회 창이 조용히 갈라질 자리였다.
CAP_BY_DATE_WINDOW_DAYS = 18


def build_cap_window(snapshot: InventoryLogisticsSnapshot, as_of: date) -> list[date] | None:
    """`as_of + inbound_lead_days` 부터 조회 창 길이만큼의 날짜 목록.

    제안 전(PRE)에는 도착일이 없어 이 고정 창을 훑고, 판정 창(창고 사용률)도 제안
    날짜가 아니라 같은 창을 써야 판정이 제안에 흔들리지 않는다. 리드타임(N4)이
    미확정이면 창을 지어내지 않고 None 이다 — 0 으로 채우면 "오늘 승인분이 오늘
    도착"이 되어 창고 검사가 무의미해진다 (§1.2-10).
    """
    lead = snapshot.inbound_lead_days
    if lead is None:
        return None
    start = as_of + timedelta(days=lead)
    return [start + timedelta(days=offset) for offset in range(CAP_BY_DATE_WINDOW_DAYS)]


#: 가용재고로 인정하는 Lot 상태. DB에 없는 상태를 새로 만들지 않는다 —
#: ACTIVE가 아닌 상태(검수/격리/사용불가 등)는 물리 점유만 하고 가용에서 빠진다.
_AVAILABLE_LOT_STATUS = "ACTIVE"


def calculate_expected_arrival_dates(
    purchase: PurchaseAgentOutput,
    inbound_lead_days: int,
) -> list[date]:
    """각 매입일에 입고 Lead Time을 더한 고유 도착일을 반환한다."""
    return sorted(
        {
            item.date + timedelta(days=inbound_lead_days)
            for scenario in purchase.scenarios
            for item in scenario.split_plan
        }
    )


def calculate_window_capacity_usage(
    snapshot: InventoryLogisticsSnapshot,
    as_of: date,
) -> Decimal | None:
    """고정 18일 창에서 가장 빡빡한 날의 창고 사용률을 계산한다.

    CAPACITY_TIGHT 판정의 분자다 (LLM 정책 결정서 §3). 판정 창을 매입 제안의
    도착일이 아니라 `as_of + lead` 부터 고정 창으로 잡는 이유: 창고 상태 판정이
    제안이 어떤 날짜를 담았느냐에 따라 흔들리면 안 되기 때문이다.

    사용률 = 1 − (창 내 최소 cap / guaranteed). 점유가 guaranteed 를 넘으면
    cap 이 0 으로 클램프되어 사용률은 1 에서 멈춘다. 계산에 필요한 입력이
    없으면 None — 그 사실은 기존 Hard Constraint(LOG-H01·H05 등)가 이미
    드러내므로 여기서 이름을 중복으로 만들지 않는다.
    """
    guaranteed = snapshot.guaranteed_capacity_kg
    # ★ guaranteed 를 **먼저** 본다 — 창 구성보다 앞이다. 순서를 바꾸면 극단 입력
    #   (`as_of` 가 date.max 근처 + lead > 0)에서 종전의 `None` 대신 OverflowError 가
    #   난다. 관측 가능성은 낮지만 순서 하나로 종전 동작이 그대로 남는다.
    if guaranteed is None:
        return None
    window = build_cap_window(snapshot, as_of)
    if window is None:
        return None
    try:
        caps = calculate_cap_by_date(snapshot, window)
    except ValueError:
        # IN_TRANSIT_SCHEDULE_UNRESOLVED 등 — 필수 Fact 미확인. 그 사실은 기존
        # Constraint 가 이미 이름을 밝히므로 여기서 중복으로 만들지 않는다.
        return None
    return Decimal(1) - (min(caps.values()) / guaranteed)


@dataclass(frozen=True)
class FreshnessLotCensus:
    """가용(ACTIVE) Lot 을 신선도 계산 가능성으로 가른 결과. **분류의 유일한 주인이다.**

    ```text
    ratios                비율을 셈할 수 있었던 Lot (잔여 > 0 · 유효 한계 > 0)
    unresolved_lot_count  잔여 또는 유효 한계를 확인하지 못해 뺀 Lot
    expired_lot_count     잔여가 0 이하로 **확인된** Lot
    ```

    ★ 세 갈래는 서로 배타이고 합이 ACTIVE Lot 수다 — 어느 Lot 도 두 번 세이지 않고,
      어느 Lot 도 조용히 사라지지 않는다.

    🔴 **`expired_lot_count` 는 폐기 판정이 아니다.** 잔여가 0 이하라는 사실은
       `build_inventory_by_item` 이 판매 가용에서 빼는 기준이고
       `turnover.is_disposal_candidate` 가 폐기**대기**로 읽는 입력이지만, 폐기 여부의
       판정과 실행은 `turnover` · `disposal` 소유이며 사람 확정을 거친다. 여기서
       세는 것은 상태 건수뿐이다.
    """

    ratios: list[Decimal]
    unresolved_lot_count: int
    expired_lot_count: int


def collect_freshness_lot_census(
    snapshot: InventoryLogisticsSnapshot,
) -> FreshnessLotCensus:
    """가용 Lot 을 신선도 계산 가능성으로 가른다. **비교도 판정도 하지 않는다.**

    비율 = remaining_freshness_days ÷ effective_freshness_limit_days.
    분모는 remaining 계산에 실제 사용된 유효 한계다 — operational_limit 원값을
    쓰면 `중` 등급이 갓 입고돼도 임박 판정된다 (LLM 정책 결정서 §3).

    대상은 가용 재고 Lot 이다: 비-ACTIVE(격리·검수 등)는 `build_inventory_by_item` 과
    같은 기준으로 제외한다. remaining 이나 유효 한계가 None 인 Lot 은 0 취급도 위험
    강제도 하지 않고 비율에서 빼되 **센다** (조용한 누락 금지). grade=None 은 제외
    사유가 아니다 — remaining 은 등급과 무관하게 계산된다.

    ★ **갈래의 순서가 계약이다.** 잔여가 `None` 인지 먼저 보고(미확인), 그 다음 0 이하
      인지 본다(만료 확인). 순서를 바꾸면 `None` 이 만료로 읽힌다.
    """
    ratios: list[Decimal] = []
    unresolved = 0
    expired = 0
    for lot in snapshot.on_hand_by_lot:
        if lot.status != _AVAILABLE_LOT_STATUS:
            continue
        if lot.remaining_freshness_days is None or lot.effective_freshness_limit_days is None:
            unresolved += 1
            continue
        if lot.remaining_freshness_days <= 0:
            expired += 1
            continue
        if lot.effective_freshness_limit_days <= 0:
            unresolved += 1
            continue
        ratios.append(
            Decimal(lot.remaining_freshness_days) / Decimal(lot.effective_freshness_limit_days)
        )
    return FreshnessLotCensus(
        ratios=ratios,
        unresolved_lot_count=unresolved,
        expired_lot_count=expired,
    )


def collect_freshness_pressure_inputs(
    snapshot: InventoryLogisticsSnapshot,
) -> tuple[list[Decimal], int]:
    """가용 Lot 의 신선도 잔여 비율 목록과, 비율을 셈할 수 없어 제외된 Lot 수.

    ★ **`collect_freshness_lot_census` 의 얇은 wrapper 다** (#396). 분류를 한 곳에 두어
      *"만료로 빠진 Lot"* 을 새로 세면서도 signal 판정의 입력은 글자 그대로 같게 남긴다 —
      이 함수의 반환은 추출 전과 동일하고, `rules` 의 두 signal 도 그대로다.
    """
    census = collect_freshness_lot_census(snapshot)
    return census.ratios, census.unresolved_lot_count


#: 품목을 식별할 수 없는 물리 점유·입고·출고를 담는 버킷 키.
_UNATTRIBUTED: str | None = None


def _replay_aggregate_occupancy(
    *,
    start_occupancy: Decimal,
    inbound: list[ScheduledQuantity],
    outbound: list[ScheduledQuantity],
    target_date: date,
) -> Decimal:
    """품목 축 없이 총 kg만 날짜순으로 재생한다 (H1 미래 점유 전용).

    출고가 열어주는 공간은 **그 시점에 실제 창고에 있는 물량까지**다. 확정 출고가
    보유량보다 많은 것은 계산이 무너져야 하는 오류가 아니라 추가 매입이 필요할 수
    있는 정상 업무 상태이고, 모자란 물량은 음수 점유량이 아니라 별개의 업무 사실이다
    (상세설계 §4 물리 점유량 정의).

    H1 승인 매입 Schedule에는 품목 축이 없어(`ArrivalScheduleItem`) 품목별 재생을
    할 수 없으므로 이 경로만 총량 계산을 유지한다. PRE의 `cap_by_date`는
    `_replay_occupancy_by_item()`을 쓴다.

    날짜 규칙은 기존 정책 그대로다 — 입고는 `<= target_date`로 당일부터 점유하고,
    출고는 `< target_date`로 당일 공간을 열지 않고 D+1부터 해제한다 (상세설계 §9).
    """
    inbound_by_date: dict[date, Decimal] = {}
    for row in inbound:
        if row.date <= target_date:
            inbound_by_date[row.date] = inbound_by_date.get(row.date, Decimal(0)) + row.quantity_kg
    outbound_by_date: dict[date, Decimal] = {}
    for row in outbound:
        if row.date < target_date:
            released = outbound_by_date.get(row.date, Decimal(0))
            outbound_by_date[row.date] = released + row.quantity_kg

    occupancy = start_occupancy
    for day in sorted({*inbound_by_date, *outbound_by_date}):
        occupancy += inbound_by_date.get(day, Decimal(0))
        # 같은 날 입고분까지 포함한 실재 물량이 그날 출고가 해제할 수 있는 상한이다.
        # 못 내보낸 물량을 이후 입고에 떠넘기지 않는다 — 애초에 없던 재고다.
        occupancy -= min(outbound_by_date.get(day, Decimal(0)), occupancy)
    return occupancy


def _initial_occupancy_by_item(
    snapshot: InventoryLogisticsSnapshot,
) -> dict[str | None, Decimal]:
    """현재 물리 점유를 품목별로 나눈다. 총량 정본은 `used_capacity_kg`다.

    Lot으로 식별되는 만큼만 품목에 귀속시키고, `used_capacity_kg`에 못 미치는
    차이는 품목 미귀속 점유로 남긴다 — 특정 품목의 출고가 그 몫을 대신 소진했다고
    보지 않는다. `sum(on_hand_by_lot)`으로 총량 정본을 대체하지 않는다.
    """
    buckets: dict[str | None, Decimal] = {}
    for lot in snapshot.on_hand_by_lot:
        buckets[lot.item] = buckets.get(lot.item, Decimal(0)) + lot.available_qty_kg
    identified = sum(buckets.values(), start=Decimal(0))
    buckets[_UNATTRIBUTED] = max(Decimal(0), snapshot.used_capacity_kg - identified)
    return buckets


def _replay_occupancy_by_item(
    snapshot: InventoryLogisticsSnapshot,
    target_date: date,
) -> Decimal:
    """품목별 물리 점유를 날짜순으로 재생해 target_date 시점의 창고 점유량을 만든다.

    품목이 명확한 확정 출고는 **그 품목 재고에서만** 공간을 연다 — 다른 품목이나
    미귀속 점유를 대신 소진했다고 계산하지 않는다. 창고 Capacity는 총 kg만 맞으면
    되는 숫자가 아니기 때문이다.

    품목을 알 수 없는 출고(`item=None`)만 기존처럼 남은 전체 물량 범위에서 총량으로
    차감한다. 임의 품목 배분은 하지 않는다 — Partial Output 정책상 이 행이 있어도
    총량 Capacity는 계속 제공해야 한다.

    품목 불명 출고가 한 번 나오면 **그 날짜부터 품목별 잔량을 확정할 수 없다.**
    어느 품목에서 나갔느냐에 따라 이후 품목 지정 출고가 실제로 열 수 있는 공간이
    달라지기 때문이다. 배정을 추정하지 않고, 그 시점부터는 품목 지정 출고를 추가
    해제 근거로 쓰지 않는다. 보장할 수 없는 공간을 있는 것처럼 열어 주는 쪽보다
    보수적으로 잡는 쪽이 안전하다.

    Lot 단위 배정은 하지 않는다 (FIFO/FEFO 없음). 날짜 규칙은 기존 정책 그대로다.
    """
    assert snapshot.confirmed_inbound_schedule is not None
    assert snapshot.confirmed_outbound_schedule is not None

    inbound_on: dict[date, list[ScheduledQuantity]] = {}
    for row in snapshot.confirmed_inbound_schedule:
        if row.date <= target_date:
            inbound_on.setdefault(row.date, []).append(row)
    outbound_on: dict[date, list[ScheduledQuantity]] = {}
    for row in snapshot.confirmed_outbound_schedule:
        if row.date < target_date:
            outbound_on.setdefault(row.date, []).append(row)

    buckets = _initial_occupancy_by_item(snapshot)
    #: 품목별 잔량을 더 이상 확정할 수 없어지는 날짜. 같은 날에 섞여 있으면 그날부터다.
    unresolved_from = min(
        (day for day, rows in outbound_on.items() if any(row.item is None for row in rows)),
        default=None,
    )
    #: 품목 불명 출고가 총량에서 걷어낸 누계. 어느 품목에도 귀속시키지 않는다.
    unattributed_release = Decimal(0)
    for day in sorted({*inbound_on, *outbound_on}):
        for row in inbound_on.get(day, []):
            buckets[row.item] = buckets.get(row.item, Decimal(0)) + row.quantity_kg
        # 품목 지정 출고는 배정이 아직 확정적인 구간에서만 자기 품목 재고를 연다.
        if unresolved_from is None or day < unresolved_from:
            for row in outbound_on.get(day, []):
                if row.item is None:
                    continue
                held = buckets.get(row.item, Decimal(0))
                buckets[row.item] = held - min(row.quantity_kg, held)
        for row in outbound_on.get(day, []):
            if row.item is not None:
                continue
            gross = sum(buckets.values(), start=Decimal(0))
            remaining = max(Decimal(0), gross - unattributed_release)
            unattributed_release += min(row.quantity_kg, remaining)

    gross = sum(buckets.values(), start=Decimal(0))
    return gross - min(unattributed_release, gross)


def calculate_cap_by_date(
    snapshot: InventoryLogisticsSnapshot,
    arrival_dates: list[date],
) -> dict[date, Decimal]:
    """guaranteed capacity 하나를 1차 Hard Constraint로 날짜별 입고 Band를 계산한다.

    burst/daily inbound/transport/shared outbound capacity는 1차 Hard 판정에
    개입하지 않는다 (Policy 결정값 §3).
    """
    if not is_inbound_schedule_complete(snapshot):
        raise ValueError("IN_TRANSIT_SCHEDULE_UNRESOLVED")
    if snapshot.guaranteed_capacity_kg is None or snapshot.confirmed_outbound_schedule is None:
        raise ValueError("LOGISTICS_CAPACITY_INPUT_MISSING")

    result: dict[date, Decimal] = {}
    for arrival_date in arrival_dates:
        projected_occupancy = _replay_occupancy_by_item(snapshot, arrival_date)
        # 품목별 재생이 각 버킷을 0 아래로 내리지 않으므로 정상 입력에서는 걸리지
        # 않는다. 불변식이 깨진 Snapshot을 잡는 최후 방어로 남긴다.
        if projected_occupancy < Decimal(0):
            raise ValueError("NEGATIVE_PROJECTED_OCCUPANCY")
        result[arrival_date] = max(
            Decimal(0), snapshot.guaranteed_capacity_kg - projected_occupancy
        )
    return result


def overlay_approved_purchase(
    snapshot: InventoryLogisticsSnapshot,
    approved_purchase: LogisticsApprovedPurchaseCommitment,
) -> list[ScheduledQuantity] | None:
    """H1 승인 매입을 on_hand가 아닌 미래 입고 Schedule에 Overlay한다."""
    if not is_inbound_schedule_complete(snapshot):
        return None
    assert snapshot.confirmed_inbound_schedule is not None
    approved_schedule = [
        ScheduledQuantity(date=item.date, quantity_kg=item.quantity_kg)
        for item in approved_purchase.arrival_schedule
    ]
    return [*snapshot.confirmed_inbound_schedule, *approved_schedule]


def calculate_future_occupancy_by_date(
    snapshot: InventoryLogisticsSnapshot,
    inbound_schedule: list[ScheduledQuantity],
) -> dict[date, Decimal] | None:
    """H1 미래 입고와 확정 출고를 반영한 날짜별 창고 점유량을 계산한다."""
    if not is_inbound_schedule_complete(snapshot) or snapshot.confirmed_outbound_schedule is None:
        return None
    dates = sorted({item.date for item in inbound_schedule})
    occupancy: dict[date, Decimal] = {}
    for target_date in dates:
        # 출고는 D+1부터 해제하고, 해제량은 그 시점에 실제 존재하는 물량을 넘지
        # 않는다. 품목 축은 쓰지 않는다 — 승인 매입 Schedule에 item이 없어서다.
        value = _replay_aggregate_occupancy(
            start_occupancy=snapshot.used_capacity_kg,
            inbound=inbound_schedule,
            outbound=snapshot.confirmed_outbound_schedule,
            target_date=target_date,
        )
        if value < Decimal(0):
            raise ValueError("NEGATIVE_PROJECTED_OCCUPANCY")
        occupancy[target_date] = value
    return occupancy


def find_in_transit_schedule_gap(snapshot: InventoryLogisticsSnapshot) -> str | None:
    """B-1: confirmed_inbound_schedule 완전성을 검증하고 실패 원인 코드를 돌려준다.

    in_transit 3상태를 명시적으로 구분한다 — None(미확인)과 [](0건 확인)은 다르다.
    행이 존재하면 inbound_id로 confirmed schedule 포함 여부와 item/quantity/도착일
    일치를 검증한다. 성공해도 Capacity에는 confirmed_inbound_schedule만 반영한다.

    confirmed schedule 안의 inbound_id 중복은 in_transit 유무와 무관하게 먼저 막는다
    (아래 참조).
    """
    if snapshot.in_transit is None:
        return "IN_TRANSIT_UNRESOLVED"
    if snapshot.confirmed_inbound_schedule is None:
        return "CONFIRMED_INBOUND_SCHEDULE_UNRESOLVED"

    # 같은 inbound_id가 confirmed schedule에 두 번 있으면 정상 데이터로 보지 않는다.
    # 대조는 id 하나에 행 하나를 전제하는데, Capacity 계산은 두 행을 **모두** 점유로
    # 더한다 — 조용히 넘기면 대조는 통과하고 점유만 이중 계상된다. 어느 행이 진짜인지
    # 여기서 고르지 않는다(뒤 행이 앞 행을 덮는 것도 하나를 고르는 것이다).
    #
    # in_transit이 0건이어도 검사한다 — confirmed schedule 자체의 무결성 문제다.
    # DB UNIQUE 제약이 없어 여기가 유일한 방어선이다.
    confirmed_by_id: dict[str, ScheduledQuantity] = {}
    for row in snapshot.confirmed_inbound_schedule:
        if row.inbound_id is None:
            continue
        if row.inbound_id in confirmed_by_id:
            return "CONFIRMED_INBOUND_ID_DUPLICATED"
        confirmed_by_id[row.inbound_id] = row

    if snapshot.in_transit == []:
        return None

    for transit in snapshot.in_transit:
        if transit.inbound_id is None:
            return "IN_TRANSIT_INBOUND_ID_MISSING"
        confirmed = confirmed_by_id.get(transit.inbound_id)
        if confirmed is None:
            return "IN_TRANSIT_NOT_IN_CONFIRMED_SCHEDULE"
        if (
            confirmed.item != transit.item
            or confirmed.quantity_kg != transit.quantity_kg
            or confirmed.date != transit.expected_arrival_date
        ):
            return "IN_TRANSIT_CONFIRMED_SCHEDULE_MISMATCH"
    return None


def is_inbound_schedule_complete(snapshot: InventoryLogisticsSnapshot) -> bool:
    """중복 가산 없이 미래 입고를 계산할 수 있는 상태인지 확인한다."""
    return find_in_transit_schedule_gap(snapshot) is None


def has_unattributed_confirmed_outbound(snapshot: InventoryLogisticsSnapshot) -> bool:
    """품목 식별이 없는 확정 출고 행이 있는지 — Partial Output 판별용.

    이 경우 품목을 임의 추정하지 않고 inventory_by_item만 생략한다(PRE는 READY 유지).
    """
    return snapshot.confirmed_outbound_schedule is not None and any(
        row.item is None for row in snapshot.confirmed_outbound_schedule
    )


def build_inventory_by_item(
    snapshot: InventoryLogisticsSnapshot,
) -> list[InventoryByItem] | None:
    """가용재고 정의를 적용한 품목별 자유재고를 집계한다.

    가용 제외: 비-ACTIVE 상태(검수/격리/사용불가), 신선도 만료(<= 0),
    **출고가 이미 잡아 둔 몫(예약·할당)**. 예상 판매·계획 출고는 차감하지 않는다.

    🔴 **차감 축은 한 벌이다 — 예약·할당뿐이다 (WP-3).** 종전에는 여기서
       `confirmed_outbound_schedule` 도 함께 뺐다. 그런데 확정 판매는 그날 마스터
       출고 흐름이 **예약으로 내려보내는 바로 그 사실**이라, 둘을 다 빼면 같은 판매가
       두 번 차감된다.

    ```text
    ~WP-2   on_hand − 예약·할당 − confirmed_outbound   🔴 같은 판매를 두 번 뺀다
    WP-3~   on_hand − 예약·할당                        ✅ 한 벌
    ```

       ⚠️ 실측(2026-09-05~09-09)에서 fixture 쪽이 전부 `CONFIRMED_ZERO`·`[]` 라
          겹치지 않았을 뿐이다. 이 파일의 종전 주석이 *"실제 값이 들어오는 날 한 축으로
          합쳐야 한다"* 고 예고했고(`schemas.InventoryLogisticsSnapshot`), WP-3 이 출고
          정본을 판매로 옮기면서 그날이 왔다.

    ★ **미래 Capacity 와는 다른 셈이다.** `_replay_occupancy_by_item` 은 여전히
      `confirmed_outbound_schedule` 을 쓴다 — 저쪽은 *"미래 어느 날 창고가 얼마나
      비는가"* 이고 이쪽은 *"지금 더 팔 수 있는가"* 다. 둘을 한 축으로 합치지 않는다.

    🔴 **`outbound.item_free_stock_qty` 와 같은 답을 내야 한다.** 매입에 나가는 이 값이
       예약이 실제로 잡을 수 있는 양보다 크면, 매입은 팔 수 있다고 보고 판매는 못 잡는
       상태가 된다. 그래서 차감 규칙을 글자 그대로 맞춘다.

    ```text
    Lot 기여   = available_qty_kg − 그 Lot 의 살아있는 할당 (ALLOCATED · PICKED)
    품목 차감  = 아직 Lot 을 안 고른 예약의 미할당 잔여
    ```

      ★ 제외된 Lot(비-ACTIVE·신선도 만료)의 할당은 **자동으로 함께 빠진다** — 그 Lot 이
        애초에 합계에 안 들어가므로 따로 빼면 과다 차감이 된다.

    ⚠️ `outbound_commitments` 가 `None`(미조회)이면 `None` 을 돌려준다.
       `confirmed_outbound_schedule` 이 `None` 일 때와 같은 규율이다 — 못 읽은 축을
       0 으로 놓으면 이미 팔린 재고를 다시 팔 수 있다고 답하게 된다.

    ★ **품목을 `ITEMS` 로 거르지 않는다.** ML Forecast 가 없거나 계약 품목에서 빠진
      품목이라도 창고에 실물이 있으면 그 사실은 나간다 — 계약 `app/contracts/core.py`
      `ITEMS` 주석의 *"예측이 없다는 이유로 재고를 숨기지 않는다"* 가 이 자리를 가리킨다.
      재고 축과 제안 축은 다르다: `E-UNKNOWN-ITEM`(`critic_v0_4.py`)이 거르는 것은
      `scenario.qty_kg` 의 제안 품목이고 재고 집계가 아니다.
    """
    # 🔴 **`confirmed_outbound_schedule` 로 막지 않는다 (WP-3).** 이 셈이 그 축을 더
    #    이상 안 쓰므로, 그것을 못 읽었다는 이유로 판매가능량을 못 낸다고 답하면
    #    **상관없는 축 때문에 화면이 비는** 것이 된다.
    axes = _commitment_axes(snapshot)
    if axes is None:
        return None
    allocated_by_lot, unallocated_by_item = axes

    totals: dict[str, Decimal] = {}
    for lot, 기여 in _sellable_lot_contributions(snapshot, allocated_by_lot):
        totals[lot.item] = totals.get(lot.item, Decimal(0)) + 기여
    for item, reserved in unallocated_by_item.items():
        if item in totals:
            totals[item] = max(Decimal(0), totals[item] - reserved)
    return [
        InventoryByItem(item=item, available_qty_kg=quantity)
        for item, quantity in sorted(totals.items())
    ]


def _sellable_lot_contributions(
    snapshot: InventoryLogisticsSnapshot, allocated_by_lot: Mapping[str, Decimal]
) -> list[tuple[InventoryLotSnapshot, Decimal]]:
    """**판매가능 판정의 주인은 여기 하나다.** Lot 별로 더 팔 수 있는 양을 낸다.

    ★ 품목 합계(`build_inventory_by_item`)와 FEFO 원가 배부(`fefo_inventory_cost_basis`)가
      **같은 함수**를 부른다. 같은 규칙을 두 벌 적어 두면 한쪽만 고치는 날
      *"팔 수 있다고 센 재고"* 와 *"원가를 배부한 재고"* 가 갈리고, 그때 나오는 것은
      오류가 아니라 **맞지 않는 원가**다.

    ⚠️ 품목 단위 미할당 예약은 여기서 빼지 않는다 — Lot 에 붙지 않은 차감이라
      Lot 축에 나눌 근거가 없다. 부르는 쪽이 자기 축에서 뺀다.
    """
    out: list[tuple[InventoryLotSnapshot, Decimal]] = []
    for lot in snapshot.on_hand_by_lot:
        if lot.status != _AVAILABLE_LOT_STATUS:
            continue
        # 신선도 만료 확인(<= 0)만 제외한다. None은 만료가 확인된 상태가 아니므로
        # 가용에서 숨기지 않는다 (0 != null).
        if lot.remaining_freshness_days is not None and lot.remaining_freshness_days <= 0:
            continue
        # 이 Lot 에 이미 붙은 할당분은 남에게 팔 수 없다. 음수로 내려가지 않게 0에서 멈춘다.
        기여 = max(Decimal(0), lot.available_qty_kg - allocated_by_lot.get(lot.lot_id, Decimal(0)))
        out.append((lot, 기여))
    return out


def _commitment_axes(
    snapshot: InventoryLogisticsSnapshot,
) -> tuple[dict[str, Decimal], dict[str, Decimal]] | None:
    """출고가 이미 잡아 둔 몫을 (Lot 축, 품목 축) 두 벌로 모은다.

    ⚠️ `outbound_commitments` 가 `None`(미조회)이면 `None` 이다. 못 읽은 축을 0 으로
      놓으면 **이미 팔린 재고를 다시 팔 수 있다**고 답하게 된다.
    """
    if snapshot.outbound_commitments is None:
        return None
    allocated_by_lot: dict[str, Decimal] = {}
    unallocated_by_item: dict[str, Decimal] = {}
    for commitment in snapshot.outbound_commitments:
        if commitment.lot_id is None:
            unallocated_by_item[commitment.item] = (
                unallocated_by_item.get(commitment.item, Decimal(0)) + commitment.quantity_kg
            )
        else:
            allocated_by_lot[commitment.lot_id] = (
                allocated_by_lot.get(commitment.lot_id, Decimal(0)) + commitment.quantity_kg
            )
    return allocated_by_lot, unallocated_by_item


def fefo_inventory_cost_basis(
    snapshot: InventoryLogisticsSnapshot,
    *,
    item: str,
    quantity_kg: Decimal,
) -> InventoryCostBasisSnapshot | None:
    """확정 판매 물량에 창고 Lot 의 **실제 취득단가**를 FEFO 로 배부한다.

    ```text
    정렬      turnover.fefo_sort_key — 실제 자동 출고와 **같은 키**다
    배부      남은 물량이 0 이 될 때까지 앞 Lot 부터 헌다
    완료 조건  remaining == 0 일 때만 기준이 선다
    ```

    🔴 **예상이지 사실이 아니다.** PRE_SALES 는 이 판매의 예약도 할당도 서기 **전**이라
       (판매 승인 → 예약 → `fefo_allocation` → 출고 순서다), 여기서 고른 Lot 은
       *"지금 출고한다면 FEFO 가 집을 Lot"* 이다. 실제 출고 Lot 이 아니다 —
       납기가 미래면 그 사이 입고·만료·남의 예약으로 달라질 수 있다.

       ★ 그래서 **순서만이라도 실제와 같아야 한다.** 규칙이 다르면 예상이 빗나가는
         것이 아니라 **처음부터 다른 것을 재는** 것이 된다.

    🔴 **모자라면 기준을 세우지 않는다 (`None`).** 0원이나 평균단가로 남은 물량을
       메우면 재무는 *"원가를 안다"* 고 읽고 마진을 판정한다 — 없는 것을 채운 수치로
       승인이 난다. `None` 이면 재무는 `RUNTIME_NOT_READY` 로 멈춘다.

    🔴 **판매가능 판정을 여기서 다시 만들지 않는다.** ACTIVE · 신선도 · 예약/할당
       규칙은 `_sellable_lot_contributions` 한 곳이 주인이고, 이 함수는 그 결과를
       **소비만** 한다. 두 벌로 적으면 «팔 수 있다고 센 재고» 와 «원가를 배부한 재고»
       가 갈리고, 그때 나오는 것은 오류가 아니라 맞지 않는 원가다.

    ★ **품목 축 미할당 예약도 FEFO 앞 Lot 부터 먹는다.** Lot 에 안 붙은 예약이라 어느
      Lot 에서 나갈지는 아직 모르지만, **실제 자동 할당이 FEFO 앞쪽부터 집으므로**
      (`fefo_allocation.allocate_reserved_stock_fefo`) 그 순서로 선점된 것으로 본다.
      선점을 순서대로 소비하므로 여기 배부 가능한 총량은
      `max(0, Σ기여 − 선점)` 이고, `build_inventory_by_item` 의 품목 합계와
      **정확히 같다** — 그 합계는 순서와 무관하다.

    ⚠️ `received_at` 이나 `unit_cost_krw_per_kg` 를 못 읽은 Lot 이 배부 대상에 걸리면
      기준을 세우지 않는다 — 순서를 모르는 Lot 을 아무 데나 끼우거나 단가를 추정하는
      대신 멈춘다.

      ★ **신선도(`remaining_freshness_days`)가 `None` 인 것은 막지 않는다.** 그것은
        *"모른다"* 이고 `fefo_sort_key` 가 **맨 뒤**로 보낸다 — 실제 출고와 같은
        처리다 (`0 != null`).
    """
    if quantity_kg <= 0:
        return None
    axes = _commitment_axes(snapshot)
    if axes is None:
        return None
    allocated_by_lot, unallocated_by_item = axes

    사용가능: list[tuple[InventoryLotSnapshot, Decimal]] = [
        (lot, 기여)
        for lot, 기여 in _sellable_lot_contributions(snapshot, allocated_by_lot)
        if lot.item == item and 기여 > 0
    ]
    # 순서를 모르는 Lot 이 하나라도 배부 후보에 있으면 FEFO 키를 세울 수 없다.
    if any(lot.received_at is None for lot, _ in 사용가능):
        return None
    사용가능.sort(
        key=lambda 쌍: fefo_sort_key(
            remaining_freshness_days=쌍[0].remaining_freshness_days,
            received_at=쌍[0].received_at,
            lot_id=쌍[0].lot_id,
        )
    )

    선점 = unallocated_by_item.get(item, Decimal(0))
    remaining = quantity_kg
    amount = Decimal(0)
    refs: list[str] = []
    for lot, 기여 in 사용가능:
        가용 = 기여
        if 선점 > 0:
            먹은양 = min(선점, 가용)
            선점 -= 먹은양
            가용 -= 먹은양
        if 가용 <= 0:
            continue
        쓸양 = min(가용, remaining)
        if lot.unit_cost_krw_per_kg is None:
            # 단가를 모르는 Lot 을 헐어야 한다면 이 판매의 원가는 알 수 없다.
            return None
        amount += 쓸양 * lot.unit_cost_krw_per_kg
        refs.append(lot.lot_id)
        remaining -= 쓸양
        if remaining == 0:
            break

    if remaining != 0:
        return None
    return InventoryCostBasisSnapshot(
        item=item,
        quantity_kg=quantity_kg,
        amount_krw=amount,
        allocation_method="FEFO",
        cost_method="ACTUAL",
        included_components=("inventory_acquisition_cost",),
        source_refs=tuple(refs),
        evidence_grade="SIM_FIXED",
    )


def build_lot_constraints(snapshot: InventoryLogisticsSnapshot) -> list[LotConstraint]:
    """현재 on_hand Lot만 S3 대조용 최소 Constraint로 변환한다."""
    return [
        LotConstraint(
            lot_id=lot.lot_id,
            item=lot.item,
            available_qty_kg=lot.available_qty_kg,
            remaining_freshness_days=lot.remaining_freshness_days,
            # 등급은 Snapshot이 이미 정규화해 둔 값을 그대로 나른다 — 여기서
            # 다시 계산하거나 None을 임의 등급으로 채우지 않는다.
            grade=lot.grade,
            status=lot.status,
        )
        for lot in snapshot.on_hand_by_lot
    ]


# ── WP-4 · 날짜별 공급량과 납기 ─────────────────────────────────────────

#: 🔴 **운송 소요시간의 정본이 스키마에 없다.** `transport.TransportPlan.standard_minutes`
#:   가 늘 `None` 인 그 자리다 — 지도 API·평균속도·거리÷속도로 분을 지어내지 않기로
#:   한 결정이 저쪽 모듈 주석에 있고, 여기서 그것을 뒤집지 않는다.
#:
#: ★ **그래서 MVP 확정값 0 일을 상수로 든다.** `agent_policy_config` 행으로 만들면
#:   *"정본이 있다"* 로 읽히는데 실제로는 없다. 0 은 «당일 출고분이 당일 닿는다» 는
#:   MVP 가정이지 측정값이 아니다.
#:
#: 🔴 **`outbound_prep_lead_days` 와 합치지 않는다.** 준비(1일)와 운송(0일)은 다른
#:   사실이고, 합쳐 두면 운송 정본이 생기는 날 무엇을 고쳐야 하는지 알 수 없다.
TRANSPORT_LEAD_DAYS = 0

#: 납기를 못 낸 이유. 🔴 **`LogisticsReasonCode`(창고 축)와 섞지 않는다** —
#: 저쪽 `CAPACITY_EXCEEDED` 는 **창고 보관** 용량이고 이쪽은 **하루 출고** 여력이다.
DeliveryReasonCode = Literal[
    "DELIVERY_BEFORE_PREP_LEAD",
    "DAILY_OUTBOUND_CAPACITY_EXCEEDED",
]

#: 그날 이미 확정된 출고가 하루 여력을 넘었다. 🔴 **0 으로 접지 않는다** — 접으면
#: 정책·데이터 이상이 «여력 0» 이라는 정상 사실로 보인다.
OUTBOUND_CAPACITY_OVERCOMMITTED = "OUTBOUND_CAPACITY_OVERCOMMITTED"

#: 미래 확정 출고 축을 **확인한 적이 없다** (fixture status 가 `UNRESOLVED`).
#: 🔴 *"확인했고 0 건"* 과 다른 사실이라 0 으로 놓지 않는다 — 놓으면 하루 출고
#: 여력이 통째로 비어 있다고 답하게 된다.
CONFIRMED_OUTBOUND_UNRESOLVED = "CONFIRMED_OUTBOUND_UNRESOLVED"


@dataclass(frozen=True)
class SupplyByDate:
    """어느 하루의 공급 사실. **두 수량의 뜻이 다르다.**

    ```text
    confirmed_sellable_quantity_kg            그날 판매 근거로 쓸 수 있는 양
    freshness_unresolved_inbound_quantity_kg  그날까지 들어오지만 신선도가 안 정해진 양
    ```

    🔴 **뒤엣것을 앞엣것에 더하지 않는다.** 입고 예정은 Lot 이 아직 없어 신선도가
       확정되지 않았고, 확정 안 된 재고를 판매 근거로 쓰면 **팔고 나서 못 내보내는
       상태**가 된다.

    ★ `None` 은 «모른다» 이고 `0` 은 «0kg 확인» 이다 (§1.2-10).
    """

    date: date
    confirmed_sellable_quantity_kg: Decimal | None
    freshness_unresolved_inbound_quantity_kg: Decimal
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryFeasibility:
    """납기 판정 한 벌. **숫자는 전부 이미 있던 사실이다.**"""

    status: Literal["READY", "FAIL", "UNRESOLVED"]
    daily_outbound_capacity_kg: Decimal | None
    delivery_route: str | None
    #: 운송 소요 **달력일**. 🔴 **이름에 단위를 안 붙인다** — 판매 계약의 정본 이름이
    #: `transport_lead_time` 이고, 물류가 `_days` 를 붙이면 같은 사실이 두 이름으로
    #: 다닌다 (WP-4B). 단위는 이 주석과 Evidence 의 `unit` 이 나른다.
    #:
    #: ⚠️ **`outbound_prep_lead_days` 와 다른 값이다.** 저쪽은 창고가 내보낼 준비를
    #:    하는 날이고 이쪽은 실려서 닿는 날이다 — 합쳐서 한 정책으로 만들지 않는다.
    transport_lead_time: int | None
    earliest_delivery_date: date | None
    reason_codes: tuple[DeliveryReasonCode, ...] = ()
    uncertainties: tuple[str, ...] = ()


def earliest_delivery_date_for(
    as_of: date, *, outbound_prep_lead_days: int, transport_lead_days: int = TRANSPORT_LEAD_DAYS
) -> date:
    """가장 이른 납기일. **`as_of` 만 읽는다 — 벽시계를 안 본다.**

    ```text
    as_of + 준비 1일 + 운송 0일 = as_of + 1
    ```

    ★ 달력일이다. 영업일 달력을 여기서 만들지 않는다 — 그 정본은 마스터가 들고 있고
      (`master/market_calendar.py`) 물류가 두 번째 달력을 만들면 둘이 갈린다.
    """
    if outbound_prep_lead_days < 0 or transport_lead_days < 0:
        raise ValueError(
            f"납기 리드는 음수일 수 없다 (prep={outbound_prep_lead_days},"
            f" transport={transport_lead_days})."
        )
    return as_of + timedelta(days=outbound_prep_lead_days + transport_lead_days)


def _fresh_until(snapshot: InventoryLogisticsSnapshot, lot: InventoryLotSnapshot) -> date | None:
    """그 Lot 이 **언제까지** 팔 수 있나. 신선도를 모르면 `None`."""
    if lot.remaining_freshness_days is None:
        return None
    return snapshot.as_of + timedelta(days=int(lot.remaining_freshness_days))


def supply_capacity_by_date(
    snapshot: InventoryLogisticsSnapshot,
    *,
    dates: Sequence[date],
    inventory_by_item: Sequence[InventoryByItem],
    confirmed_outbound_by_date: Mapping[date, Decimal],
    daily_outbound_capacity_kg: Decimal,
    item: str | None = None,
) -> list[SupplyByDate]:
    """날짜마다 **판매 근거로 쓸 수 있는 양**을 낸다 (WP-4).

    ```text
    confirmed_sellable(d) = min( 그날까지 신선한 판매가능 재고 ,
                                 하루 출고 여력 − 그날 이미 확정된 출고 )
    ```

    🔴 **새 판매가능량 엔진이 아니다.** 재고 축의 출발점은 `build_inventory_by_item`
       이 낸 값 그대로다 — 그것이 예약·할당을 이미 뺀 정본이고, 여기서 다시 세면
       `outbound.item_free_stock_qty` 와 갈린다.

    🔴 **입고 예정을 confirmed 에 더하지 않는다.** 아직 Lot 이 없어 신선도가 확정되지
       않았다 — 그 몫은 `freshness_unresolved_inbound_quantity_kg` 에만 담는다.

    ★ **신선도 차감은 보수적으로 한다.** 그날 이전에 신선도가 끝나는 Lot 의 **물리
      잔량 전부**를 뺀다. 그 Lot 몫 중 일부가 이미 예약에 잡혀 있으면 두 번 빼는
      셈이지만, 방향이 «적게 판다» 라 안전하다 — 반대로 접으면 못 내보낼 재고를
      팔게 된다. 신선도를 **모르는** Lot 은 빼지 않는다
      (`build_inventory_by_item` 이 `None` 을 만료로 안 보는 것과 같은 규율).

    🔴 **이미 확정된 출고가 여력을 넘으면 `None` 이다.** 0 으로 접으면 정책·데이터
       이상이 «오늘은 더 못 나간다» 는 정상 사실로 보인다. 그 날 행에
       `OUTBOUND_CAPACITY_OVERCOMMITTED` 를 남긴다.

    :param dates: 답할 날짜들. **물류가 만들지 않는다** — 사용자가 물은 날이다.
    :param item: 품목을 좁힐지. `None` 이면 전 품목 합이다.
    """
    if daily_outbound_capacity_kg < 0:
        raise ValueError(f"하루 출고 여력이 음수다: {daily_outbound_capacity_kg}")

    기준 = sum(
        (row.available_qty_kg for row in inventory_by_item if item is None or row.item == item),
        start=Decimal(0),
    )
    만료: list[tuple[date, Decimal]] = []
    for lot in snapshot.on_hand_by_lot:
        if item is not None and lot.item != item:
            continue
        끝나는날 = _fresh_until(snapshot, lot)
        if 끝나는날 is not None:
            만료.append((끝나는날, lot.available_qty_kg))
    입고 = [
        row
        for row in (snapshot.confirmed_inbound_schedule or ())
        if item is None or row.item == item
    ]

    out: list[SupplyByDate] = []
    for day in dates:
        확정출고 = confirmed_outbound_by_date.get(day, Decimal(0))
        여력 = daily_outbound_capacity_kg - 확정출고
        넘었다 = 여력 < 0
        시든것 = sum((수량 for 끝, 수량 in 만료 if 끝 < day), start=Decimal(0))
        재고 = max(Decimal(0), 기준 - 시든것)
        out.append(
            SupplyByDate(
                date=day,
                confirmed_sellable_quantity_kg=None if 넘었다 else min(재고, 여력),
                freshness_unresolved_inbound_quantity_kg=sum(
                    (row.quantity_kg for row in 입고 if snapshot.as_of < row.date <= day),
                    start=Decimal(0),
                ),
                uncertainties=(OUTBOUND_CAPACITY_OVERCOMMITTED,) if 넘었다 else (),
            )
        )
    return out


def evaluate_delivery_feasibility(
    *,
    as_of: date,
    daily_outbound_capacity_kg: Decimal,
    outbound_prep_lead_days: int | None,
    delivery_route: str | None,
    confirmed_outbound_known: bool,
    requested_quantity_kg: Decimal | None,
    preferred_delivery_date: date | None,
    confirmed_outbound_on_preferred_kg: Decimal | None,
    transport_lead_days: int = TRANSPORT_LEAD_DAYS,
) -> DeliveryFeasibility:
    """납기가 되나. **사용자가 물은 것만 판정한다.**

    ```text
    준비일 정책이 없다 · Route 를 못 읽었다     UNRESOLVED   ← 답을 안 낸다
    미래 확정 출고 축을 확인한 적이 없다        UNRESOLVED   ← 0 으로 놓지 않는다
    희망일 < 가장 이른 납기일                    FAIL         DELIVERY_BEFORE_PREP_LEAD
    요청량 + 그날 확정 출고 > 하루 여력          FAIL         DAILY_OUTBOUND_CAPACITY_EXCEEDED
    그 밖                                        READY
    ```

    :param confirmed_outbound_known: 미래 확정 출고 축을 **읽었나**.
        🔴 `False` 를 «출고 0kg» 으로 접으면 하루 여력이 통째로 비어 있다고 답한다 —
        fixture 가 그 축을 `UNRESOLVED` 로 적었다는 것은 *"확인한 적 없다"* 이지
        *"확인했고 0 건"* 이 아니다 (`repository._schedule_source`).

    🔴 **정책이 없으면 코드 상수로 메우지 않는다.** `outbound_prep_lead_days` 가
       `None` 이면 가장 이른 납기일을 못 내고, 못 내는 것을 `READY` 로 답하면
       **DB 에 정책이 없는데도 납기가 확정된 것처럼** 나간다 (WP-4 M4).

    ★ **희망일이 없어도 `READY` 다.** 그때 이 블록이 답하는 것은 *"가장 이른 납기일이
      언제인가"* 이고 그 답은 냈다 — 판정할 희망일이 없는 것과 판정에 실패한 것은 다르다.
    """
    uncertainties: list[str] = []
    if outbound_prep_lead_days is None:
        uncertainties.append("OUTBOUND_PREP_LEAD_DAYS_UNRESOLVED")
    if delivery_route is None:
        uncertainties.append("DELIVERY_ROUTE_UNRESOLVED")
    if not confirmed_outbound_known:
        uncertainties.append(CONFIRMED_OUTBOUND_UNRESOLVED)
    if uncertainties:
        return DeliveryFeasibility(
            status="UNRESOLVED",
            daily_outbound_capacity_kg=daily_outbound_capacity_kg,
            delivery_route=delivery_route,
            transport_lead_time=transport_lead_days,
            earliest_delivery_date=None,
            uncertainties=tuple(uncertainties),
        )

    earliest = earliest_delivery_date_for(
        as_of,
        outbound_prep_lead_days=outbound_prep_lead_days,
        transport_lead_days=transport_lead_days,
    )
    reasons: list[DeliveryReasonCode] = []
    if preferred_delivery_date is not None and preferred_delivery_date < earliest:
        reasons.append("DELIVERY_BEFORE_PREP_LEAD")
    if requested_quantity_kg is not None and preferred_delivery_date is not None:
        이미 = confirmed_outbound_on_preferred_kg or Decimal(0)
        if 이미 + requested_quantity_kg > daily_outbound_capacity_kg:
            reasons.append("DAILY_OUTBOUND_CAPACITY_EXCEEDED")
    return DeliveryFeasibility(
        status="FAIL" if reasons else "READY",
        daily_outbound_capacity_kg=daily_outbound_capacity_kg,
        delivery_route=delivery_route,
        transport_lead_time=transport_lead_days,
        earliest_delivery_date=earliest,
        reason_codes=tuple(reasons),
    )
