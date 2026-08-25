"""
band.py v1.2 — T3-1 결합 · T3-2 클리핑 · T3-3 교착 · T3-4 붕괴 감지

v0.2 → v0.3 (정의서 v0.13)
  · N12 확정 반영 — **T3 는 단가 환산을 하지 않는다.**
    시나리오가 낸 total_amount_krw 로만 금액 밴드를 대조한다.
  · cap_by_date 창고 점유 검사 신설 (N15, §3.5.4 결합 검사 2번)
  · 허용 축이 quantity 하나뿐이면 회송을 생략 (timing 게이팅 대응)

v0.1 → v0.2 (`매입파트_답변서_v0.9_검토의견` 반영)
  ① clip_scenario 가 split_plan · sourcing_plan 을 함께 축소 (B1)
  ② detect_variant_collapse 를 상대 비율 + 분할 구조 조건으로 재정의 (§3-4)
  ③ is_structurally_narrow — 회송해도 소용없는 밴드를 사전 판별 (B5)
  ④ build_feedback 이 allowed_variant_axes 에서 target_axis 를 고른다

이 모듈에는 LLM 호출도 DB 세션도 없다. 전부 산술이다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from app.orchestrator.contracts_core import (
    ITEMS,
    STRUCTURAL_SLACK_MIN,
    VARIANT_SPREAD_MIN,
    Band,
    ClipResult,
    ContractViolation,
    Deadlock,
    Dept,
    FeedbackHint,
    ItemCode,
    PurchaseScenario,
    SourcingLot,
    SplitLeg,
    T0Snapshot,
    T2Reply,
    check_triple_identity,
)

EPS = 1e-6
INF = float("inf")


# ===========================================================================
# T3-1  밴드 결합
# ===========================================================================


def _iter_replies(
    replies: Mapping[Dept, T2Reply | Sequence[T2Reply]],
) -> Iterable[tuple[Dept, T2Reply]]:
    """
    부서당 회신이 하나일 수도, 품목별로 여럿일 수도 있다 (v1.2.4).

    ★ 영업 IO 명세 §0 — `run_floor_reply(item, as_of, snapshot)` 는 **품목마다**
      호출된다. 4품목이면 영업 회신이 4개다.
      반면 재무 A 는 `scope = ALL_ITEMS_TOTAL` 로 전사 총액 하나를 낸다(통합 문서 §1).
      두 축이 한 밴드에서 만나므로 결합기가 둘 다 받아야 한다.
    """
    for dept, value in replies.items():
        if isinstance(value, T2Reply):
            yield dept, value
        else:
            for r in value:
                yield dept, r


def combine_band(
    replies: Mapping[Dept, T2Reply | Sequence[T2Reply]],
    items: Iterable[ItemCode] = ITEMS,
) -> Band:
    """
        floor_kg[i]    = max(영업 floor)        ← **품목별**. 총량 floor 는 없다.
        cap_kg[i]      = min(재고 cap, 영업 loose cap)
        cap_total_kg   = min(재고 집계 cap)
        cap_amount_krw = min(재무 금액 cap)     ← §4.3 재무는 금액 축만

    ★ 소프트 경고는 밴드를 움직이지 않는다. H1 표시용으로만 흐른다.
    ★ 재무 cap 이 kg 이 아니라는 것이 결합의 난점이다. 금액→수량 환산은
      시나리오 단가에 의존하므로 clip() 에서 시나리오별로 수행한다 (§3.1).

    ★★ v1.2.4 — 부서당 회신이 여럿일 수 있다.

      `{"sales": [배추회신, 무회신, ...], "finance": 단일회신}` 형태를 받는다.
      품목별 회신의 `floor_kg` 가 자기 품목 밖을 채우면 계약 위반으로 막는다 —
      배추 회신이 무의 하한을 정하면 어느 회신이 구속했는지 추적이 끊긴다.

    ★★★ runtime_status 가 READY 가 아닌 회신은 **밴드에 넣지 않고** not_ready 에 남긴다.
      조용히 건너뛰면 그 부서의 cap 이 무한대가 되어, 재고가 죽은 날
      무제한 매입이 통과한다.
    """
    items = tuple(items)
    floor: dict[ItemCode, float] = {i: 0.0 for i in items}
    cap: dict[ItemCode, float] = {i: INF for i in items}
    cap_total = INF
    cap_amount = INF
    cap_by_date: dict = {}
    contributors: dict[str, str] = {}
    not_ready: list[Dept] = []

    for dept, reply in _iter_replies(replies):
        if reply.runtime_status != "READY":
            if dept not in not_ready:
                not_ready.append(dept)
            contributors[f"not_ready.{dept}"] = reply.runtime_status
            continue

        for chk in reply.checks:
            # 품목별 회신은 자기 품목만 채운다
            if reply.item is not None:
                stray = {i for i in (chk.floor_kg or {}) if i != reply.item}
                stray |= {i for i in (chk.cap_kg or {}) if i != reply.item}
                if stray:
                    raise ContractViolation(
                        f"[{chk.check_id}] {reply.item} 회신이 다른 품목 {sorted(stray)} 의 "
                        f"밴드를 채웠다. 품목별 회신은 자기 품목만 채운다 (v1.2.4)."
                    )
            if chk.kind != "hard":
                continue

            if chk.floor_kg:
                for i, v in chk.floor_kg.items():
                    if v > floor.get(i, 0.0):
                        floor[i] = float(v)
                        contributors[f"floor_kg.{i}"] = chk.check_id

            if chk.cap_kg:
                for i, v in chk.cap_kg.items():
                    if v < cap.get(i, INF):
                        cap[i] = float(v)
                        contributors[f"cap_kg.{i}"] = chk.check_id

            if chk.cap_by_date_kg:
                for d, v in chk.cap_by_date_kg.items():
                    if v < cap_by_date.get(d, INF):
                        cap_by_date[d] = float(v)
                        contributors[f"cap_by_date.{d}"] = chk.check_id

            if chk.cap_total_kg is not None and chk.cap_total_kg < cap_total:
                cap_total = float(chk.cap_total_kg)
                contributors["cap_total_kg"] = chk.check_id

            if chk.cap_amount_krw is not None and chk.cap_amount_krw < cap_amount:
                cap_amount = float(chk.cap_amount_krw)
                contributors["cap_amount_krw"] = chk.check_id

    return Band(
        floor, cap, cap_total, cap_amount, contributors, cap_by_date, not_ready=tuple(not_ready)
    )


# ===========================================================================
# T3-3  교착 판정 — 클리핑보다 먼저
# ===========================================================================


def detect_deadlock(band: Band, unit_price: Mapping[ItemCode, float]) -> Deadlock | None:
    """밴드가 비어 있는 세 경우. T1 회송해도 풀리지 않는다 (회사 상태의 문제)."""
    for i, f in band.floor_kg.items():
        c = band.cap_kg.get(i, INF)
        if f - c > EPS:
            return Deadlock(
                "DEADLOCK_ITEM",
                f"{i}: floor {f:,.0f}kg > cap {c:,.0f}kg — 팔아야 할 양이 보관 가능량을 넘는다",
                i,
                f - c,
                "kg",
                (
                    band.contributors.get(f"floor_kg.{i}", "?"),
                    band.contributors.get(f"cap_kg.{i}", "?"),
                ),
            )

    ft = band.floor_total_kg
    if ft - band.cap_total_kg > EPS:
        return Deadlock(
            "DEADLOCK_SPACE",
            f"Σfloor {ft:,.0f}kg > 창고 상한 {band.cap_total_kg:,.0f}kg",
            None,
            ft - band.cap_total_kg,
            "kg",
            (band.contributors.get("cap_total_kg", "?"),),
        )

    fa = sum(band.floor_kg[i] * unit_price.get(i, 0.0) for i in band.floor_kg)
    if fa - band.cap_amount_krw > EPS:
        return Deadlock(
            "DEADLOCK_CASH",
            f"확정수요 이행에 {fa:,.0f}원 필요, 가용 {band.cap_amount_krw:,.0f}원 "
            f"— 납기는 있는데 살 돈이 없다",
            None,
            fa - band.cap_amount_krw,
            "krw",
            (band.contributors.get("cap_amount_krw", "?"),),
        )
    return None


# ===========================================================================
# T3-2  클리핑 — 2단계 + 하위 계획 동반 축소
# ===========================================================================


def _shrink_to_limit(
    qty: dict[ItemCode, float],
    floor: Mapping[ItemCode, float],
    weight: Mapping[ItemCode, float],
    limit: float,
) -> tuple[dict[ItemCode, float], bool]:
    """
    floor 위 여유분에서만 비례 차감한다. **floor 는 절대 깎지 않는다.**

    ★ 검토의견 §3-3 의 '품목 안분 규칙'이 바로 이 함수다.
      총량 클리핑 후 별도 안분을 하면 품목별 floor(확정주문)가 깨진다.
      floor 를 먼저 배정하고 잔여만 비중 배분하는 구조라야 납기가 지켜진다.
      잔여 배분 비중은 **원안 비율 유지** — 매입이 정한 품목 구성을
      T3 가 뒤집지 않는 것이 §5.1(조정만 한다)에 맞다.
    """
    current = sum(qty[i] * weight[i] for i in qty)
    if current - limit <= EPS:
        return qty, True

    floor_load = sum(floor.get(i, 0.0) * weight[i] for i in qty)
    if floor_load - limit > EPS:
        return qty, False

    slack_load = current - floor_load
    if slack_load <= EPS:
        return qty, False

    ratio = (current - limit) / slack_load
    return {
        i: floor.get(i, 0.0) + max(0.0, q - floor.get(i, 0.0)) * (1.0 - ratio)
        for i, q in qty.items()
    }, True


def _scale_sourcing(
    lots: Sequence[SourcingLot],
    factor: Mapping[ItemCode, float],
) -> tuple[tuple[SourcingLot, ...], float]:
    """
    등급 배분을 품목별 비율로 축소한다. min_lot_kg 가 있으면 로트 배수 내림.
    반환: (축소된 로트, 내림으로 버린 잔차 kg)
    """
    out: list[SourcingLot] = []
    residual = 0.0
    for lot in lots:
        f = factor.get(lot.item, 1.0)
        target = lot.qty_kg * f
        if lot.min_lot_kg:
            n = math.floor(target / lot.min_lot_kg)
            q = n * lot.min_lot_kg
            residual += target - q
        else:
            q = target
        out.append(
            SourcingLot(
                lot.item,
                lot.grade,
                round(q, 3),
                lot.unit_price_krw_per_kg,
                lot.ref_ids,
                lot.min_lot_kg,
            )
        )
    return tuple(out), round(residual, 3)


def _scale_split(
    legs: Sequence[SplitLeg], factor: Mapping[ItemCode, float]
) -> tuple[SplitLeg, ...]:
    """
    ★ v1.2.1 — expected_arrival_date 를 반드시 함께 넘긴다.

      축소 대상은 **수량뿐이다.** 도착일은 클리핑으로 변하지 않는다.
      v1.2 까지는 이 값을 넘기지 않아 SplitLeg 가 기본값 None 으로 재생성됐고,
      **클리핑이 일어나지 않은 안에서도 회차별 도착일이 사라졌다.**
      그 결과 N4 미확정 구간에서 check_occupancy_by_date() 가 전 회차를 스킵해
      창고 점유 검사가 통째로 무검사가 됐다 (§3.4.5-③ 위반).
    """
    return tuple(
        SplitLeg(
            leg.offset_days,
            {i: round(v * factor.get(i, 1.0), 3) for i, v in leg.qty_kg.items()},
            leg.expected_arrival_date,
        )
        for leg in legs
    )


def clip_scenario(scenario: PurchaseScenario, band: Band) -> ClipResult:
    """
    1단계  품목별 [floor_i, cap_i] 클램프
    2단계  집계 제약 — 창고 총량(kg) → 재무 금액(krw)
    3단계  ★ v0.2 — split_plan · sourcing_plan 동반 축소로 삼중 일치 유지

    qty_kg(원안)와 clipped_qty_kg(조정안)를 반드시 둘 다 보존한다.
    """
    original = dict(scenario.qty_kg)
    price = scenario.unit_price_krw_per_kg
    binding: list[str] = []

    # ── 1단계 ───────────────────────────────────────────────────
    q: dict[ItemCode, float] = {}
    for i, v in original.items():
        f = band.floor_kg.get(i, 0.0)
        c = band.cap_kg.get(i, INF)
        nv = min(max(float(v), f), c)
        if nv > v + EPS:
            binding.append(f"floor_kg.{i}")
        elif nv < v - EPS:
            binding.append(f"cap_kg.{i}")
        q[i] = nv

    ones = {i: 1.0 for i in q}

    # ── 2단계-a 창고 총량 ───────────────────────────────────────
    if math.isfinite(band.cap_total_kg):
        q2, ok = _shrink_to_limit(q, band.floor_kg, ones, band.cap_total_kg)
        if not ok:
            return ClipResult(
                scenario.scenario_id,
                original,
                q,
                binding_constraints=tuple(binding + ["cap_total_kg"]),
                infeasible=True,
            )
        if any(abs(q2[i] - q[i]) > EPS for i in q):
            binding.append("cap_total_kg")
        q = q2

    # ── 2단계-b 재무 금액 — ★ N12 확정 반영 (v0.3) ──────────────
    #
    #   정의서 v0.13 §3.4.5-④ 가 환산 주체를 T1 로 고정했다.
    #     T3(오케)   ❌ 단가를 알아야 함 → §5.1 "원본 DB 를 읽지 않는다" 와 충돌
    #     재무       ❌ 수량을 산출하게 됨 → 부서별 축 제한 위반
    #     T1 시나리오 ✅ 이미 수량과 단가를 모두 알고 있다
    #
    #   → T3 는 **시나리오가 낸 total_amount_krw 와 total_qty_kg 의 비율**만 쓴다.
    #     DB 를 조회하지 않으므로 §5.1 을 지키고, 재무는 금액 축만 유지한다.
    #     품목별 단가를 쓰지 않는 것이 v0.2 와의 차이다.
    if math.isfinite(band.cap_amount_krw):
        total_amt = _scenario_amount(scenario)
        total_qty = sum(original.values())
        implied = (total_amt / total_qty) if total_qty > EPS else 0.0  # 시나리오 자체 제공값
        cur_amt = sum(q.values()) * implied
        if cur_amt - band.cap_amount_krw > EPS:
            uniform = {i: implied for i in q}
            q3, ok = _shrink_to_limit(q, band.floor_kg, uniform, band.cap_amount_krw)
            if not ok:
                return ClipResult(
                    scenario.scenario_id,
                    original,
                    q,
                    binding_constraints=tuple(binding + ["cap_amount_krw"]),
                    infeasible=True,
                )
            binding.append("cap_amount_krw")
            q = q3

    q = {i: round(v, 3) for i, v in q.items()}

    # ── 3단계 하위 계획 동반 축소 (B1) ──────────────────────────
    factor = {i: (q[i] / original[i] if original[i] > EPS else 1.0) for i in original}
    split = _scale_split(getattr(scenario, "split_plan", ()) or (), factor)
    sourcing, residual = _scale_sourcing(getattr(scenario, "sourcing_plan", ()) or (), factor)

    # min_lot 내림이 있었다면 품목 수량을 로트 합에 맞춰 되맞춘다 (항등식 우선)
    floor_broken: list[ItemCode] = []
    if sourcing and residual > EPS:
        for i in q:
            lot_sum = sum(l.qty_kg for l in sourcing if l.item == i)
            if lot_sum > EPS:
                q[i] = round(lot_sum, 3)
                if q[i] < band.floor_kg.get(i, 0.0) - EPS:
                    floor_broken.append(i)
        split = _scale_split(
            getattr(scenario, "split_plan", ()) or (),
            {i: (q[i] / original[i] if original[i] > EPS else 1.0) for i in original},
        )

    amount = (
        sum(l.amount_krw for l in sourcing)
        if sourcing
        else sum(q[i] * price.get(i, 0.0) for i in q)
    )

    problems = check_triple_identity(q, split, sourcing, amount)

    return ClipResult(
        scenario_id=scenario.scenario_id,
        qty_kg=original,
        clipped_qty_kg=q,
        clipped_split_plan=split,
        clipped_sourcing_plan=sourcing,
        clipped_amount_krw=round(amount, 2),
        binding_constraints=tuple(dict.fromkeys(binding)),
        identity_problems=tuple(problems),
        lot_residual_kg=residual,
        floor_broken=tuple(floor_broken),
        infeasible=bool(floor_broken),
    )


def _scenario_amount(scenario) -> float:
    """
    시나리오의 총액. **T1 이 필수로 산출한다** (§3.4.5-④ N12).
    sourcing_plan 이 있으면 그 합이 정본이고, 없으면 total_amount_krw 필드를 쓴다.
    둘 다 없으면 계약 위반이므로 0 을 반환해 금액 밴드가 바인딩되지 않게 한다.
    """
    lots = getattr(scenario, "sourcing_plan", ()) or ()
    if lots:
        return sum(l.amount_krw for l in lots)
    return float(
        getattr(scenario, "total_amount_krw", 0.0)
        or sum(
            scenario.qty_kg[i] * scenario.unit_price_krw_per_kg.get(i, 0.0) for i in scenario.qty_kg
        )
    )


# ===========================================================================
# 창고 점유 검사 — cap_by_date (N15, §3.5.4 결합 검사 2번)
# ===========================================================================


# ★ v1.2 §3.4.5-④ — cap_by_date self_check 접합
#   T1(매입 self_check) · T3(오케스트레이터) · Critic 셋이 **같은 함수**를 쓴다.
#   입력 데이터만 다르고 판정 로직은 하나다 (계약서 §6.4).
#   매입이 자체 구현하면 T3 와 미세하게 달라져 "매입은 통과인데 T3 는 FAIL"이 반복된다.
#
#     T1     매입 self_check   자기 시나리오가 cap_by_date 를 넘는가   선제
#     T3     오케스트레이터    클리핑 후 전 시나리오 재검사            결합
#     Critic 검증              T3 결과 재검증                          감사
@dataclass(frozen=True)
class OccupancyResult:
    """
    ★ v1.2.1 신설 — **몇 건을 검사했는지**를 함께 돌려준다.

      v1.2 는 problems 리스트만 반환했다. 그래서 회차별 도착일이 없어 전 회차를
      스킵한 경우와 전부 검사해서 위반이 없는 경우가 **둘 다 빈 리스트**였다.
      회귀 테스트가 `isinstance(_, list)` 만 보고 통과시킨 것이 이 때문이다.
      "검사했는데 깨끗하다"와 "검사를 못 했다"는 완전히 다른 상태다.
    """

    problems: tuple[str, ...] = ()
    dates_checked: int = 0
    legs_total: int = 0
    legs_dated: int = 0
    skipped: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        """실제로 판정이 일어났는가. False 면 통과가 아니라 미검사다."""
        return self.dates_checked > 0

    def __bool__(self) -> bool:  # 하위 호환 — if occ: 는 '위반 있음'을 뜻했다
        return bool(self.problems)


def check_occupancy_by_date(
    clip: ClipResult,
    band: Band,
    snapshot: T0Snapshot,
    inbound_lead_days: int | None = None,
) -> list[str]:
    """T1·T3·Critic 공용 진입점. 하위 호환을 위해 위반 목록만 돌려준다.

    검사가 실제로 돌았는지까지 알아야 하면 `check_occupancy_detailed()` 를 쓴다."""
    return list(check_occupancy_detailed(clip, band, snapshot, inbound_lead_days).problems)


def check_occupancy_detailed(
    clip: ClipResult,
    band: Band,
    snapshot: T0Snapshot,
    inbound_lead_days: int | None = None,
) -> OccupancyResult:
    """
        날짜 d 점유 = 확정 점유[d] + Σ(매입안 중 d까지 도착분) ≤ cap_by_date[d]

    ★ 선매입은 며칠 뒤 도착한다. 오늘 창고가 여유로워도 D+5 에 이미 다른 입고가
      잡혀 있으면 그 매입은 실행 불가능하다. 반대로 오늘 꽉 찼어도 내일 대량
      납품이 확정돼 있으면 선매입이 가능하다. 스칼라 cap 으로는 둘 다 못 잡는다.

    ★ cap_by_date 는 **확정분만** 반영한다 (v0.13 명문화).
      전략적 판매를 창고 여유로 계산하면 판매가 안 됐을 때 창고가 넘친다.
      사이클 A 는 전략 판매 = 0 을 가정하고 **안전한 방향으로만 틀린다.**
    """
    if not band.cap_by_date_kg:
        return OccupancyResult(skipped=("cap_by_date 미제공 (N15) — 검사 대상 아님",))

    lead = inbound_lead_days if inbound_lead_days is not None else snapshot.inbound_lead_days
    problems: list[str] = []
    skipped: list[str] = []

    # 분할 계획이 있으면 각 leg 의 도착일을, 없으면 리드타임 하나로 본다
    legs = clip.clipped_split_plan or ()
    arrivals: dict = {}
    dated = 0
    if legs:
        for idx, leg in enumerate(legs, 1):
            # ★ v1.2 — 매입이 회차별로 계산한 도착일을 우선 쓴다 (N4 3자 공유).
            #   없으면 리드타임으로 파생하되, N4 가 NULL 이면 계산하지 않는다.
            eta = leg.expected_arrival_date
            if eta is None:
                if lead is None:
                    # N4 미결 — 0 으로 대체하지 않는다 (§1.2-10)
                    skipped.append(f"{idx}회차: 도착일 부재 + N4 미결 — 산출 불가")
                    continue
                eta = snapshot.as_of + timedelta(days=leg.offset_days + lead)
            dated += 1
            arrivals[eta] = arrivals.get(eta, 0.0) + sum(leg.qty_kg.values())
    else:
        if lead is None:
            return OccupancyResult(
                legs_total=0,
                skipped=("분할 없음 + N4 미결 — 도착일 산출 불가",),
            )
        arrivals[snapshot.as_of + timedelta(days=lead)] = clip.total_kg

    if not arrivals:
        # ★ 여기서 빈 problems 를 그대로 돌려주면 '통과'로 읽힌다. 미검사임을 명시한다.
        return OccupancyResult(
            legs_total=len(legs),
            legs_dated=0,
            skipped=tuple(skipped),
        )

    for d in sorted(band.cap_by_date_kg):
        arrived = sum(v for a, v in arrivals.items() if a <= d)  # d 까지 도착한 누적분
        confirmed = snapshot.confirmed_occupancy_by_date.get(d, 0.0)
        occupied = confirmed + arrived
        cap_d = band.cap_by_date_kg[d]
        if occupied - cap_d > EPS:
            problems.append(
                f"{d} 점유 {occupied:,.0f}kg > cap_by_date {cap_d:,.0f}kg "
                f"(확정 {confirmed:,.0f} + 매입안 도착 {arrived:,.0f})"
            )
    return OccupancyResult(
        problems=tuple(problems),
        dates_checked=len(band.cap_by_date_kg),
        legs_total=len(legs),
        legs_dated=dated,
        skipped=tuple(skipped),
    )


def clip_all(scenarios: Iterable[PurchaseScenario], band: Band) -> list[ClipResult]:
    return [clip_scenario(s, band) for s in scenarios]


# ===========================================================================
# T3-4  시나리오 붕괴 — v0.2 재정의 (검토의견 §3-4 + B2)
# ===========================================================================


def detect_collapse_type(results: list[ClipResult], axes: list[str]) -> str | None:
    """
    ★ v1.2 — 유저플로우 §⑧ 이 collapse_type 을 AXIS | QUANTITY 로 나눴다.
      두 붕괴는 **대응이 다르다.**

        AXIS     전 안이 같은 축으로 만들어졌다  → 다른 축으로 재생성 요청
        QUANTITY 축은 달랐는데 클리핑 후 수량이 수렴했다 → 밴드 자체가 좁다

      한 값으로 뭉치면 회송 지시가 틀린다 — QUANTITY 붕괴에 "다른 축을 쓰라"고
      보내봐야 밴드가 좁은 것이 원인이므로 결과가 같다.
    """
    if not detect_variant_collapse(results):
        return None
    if axes and len(set(axes)) < 2:
        return "AXIS"
    return "QUANTITY"


def detect_variant_collapse(
    results: list[ClipResult],
    spread_min: float = VARIANT_SPREAD_MIN,
) -> bool:
    """
    검토의견 §3-4 제안:
        (max − min) / max < 0.15

    ★ 두 가지를 보강한다.

      ① 분할 구조가 다르면 붕괴가 아니다.
         총량 5,449kg 을 오늘 전량 vs 오늘 2,725 / D+3 2,724 로 나눈 두 안은
         현금흐름과 로트 나이가 다른 서로 다른 선택지다. 총량만 보면
         (max−min)/max = 0 이라 붕괴로 오판되고, **timing 축이 무력화된다.**

      ② 총량이 아니라 품목 벡터로 본다.
         "배추 1,400 + 무 100" 과 "배추 100 + 무 1,400" 은 총량이 같다.
         지금은 배추 81.2% 라 문제가 안 되지만, N10 이후 급식 채널이
         양파·무 수요를 만들면 오판이 시작된다.
    """
    feasible = [r for r in results if not r.infeasible]
    if len(feasible) < 2:
        return True

    # ① 지문(수량 벡터 + 분할 구조)이 다르면 살아 있는 선택지다
    if len({r.signature() for r in feasible}) >= 2:
        return False

    # ② 지문이 같아도 총량 스프레드가 임계 이상이면 붕괴 아님 (방어적)
    totals = [r.total_kg for r in feasible]
    mx = max(totals)
    if mx <= 0:
        return True
    return (mx - min(totals)) / mx < spread_min


def is_structurally_narrow(band: Band, slack_min: float = STRUCTURAL_SLACK_MIN) -> bool:
    """
    B5 — 밴드 폭이 구조적으로 좁으면 재생성해도 다양성이 나올 수 없다.

    회송은 "매입이 밴드를 몰라서 넘겼을 때"만 의미가 있고,
    "밴드가 애초에 좁을 때"는 T1·T2 를 3배 호출하는 낭비다.
    붕괴가 상시화되는 v0.9 조건에서는 이 구분이 없으면 호출 예산이 3배가 된다.
    """
    if not math.isfinite(band.cap_total_kg) or band.cap_total_kg <= EPS:
        return False
    # 분모는 cap_total — "밴드가 허용하는 폭 대비 움직일 수 있는 여유"가 판단 기준이다.
    # 이 비율이 붕괴 임계(VARIANT_SPREAD_MIN)보다 작으면 어떤 2안을 만들어도
    # 스프레드가 임계를 넘을 수 없으므로 회송이 수학적으로 무의미하다.
    return band.aggregate_slack_kg / band.cap_total_kg < slack_min


# ===========================================================================
# 사전 feedback 힌트 — 코드가 만든다. LLM 이 만들지 않는다.
# ===========================================================================


def build_feedback(
    results: list[ClipResult],
    band: Band,
    collapsed: bool,
    allowed_axes: Sequence[str] = ("quantity", "timing"),
) -> FeedbackHint | None:
    """계약서 §5.3 — 무엇을 완화할지 명시한다."""
    if collapsed:
        binding = tuple(dict.fromkeys(c for r in results for c in r.binding_constraints))
        slack = band.aggregate_slack_kg
        desc = (
            f"집계 여유 {slack:,.0f}kg (cap_total {band.cap_total_kg:,.0f} − "
            f"Σfloor {band.floor_total_kg:,.0f})"
            if math.isfinite(slack)
            else "밴드 폭 불명"
        )

        # ★ 허용 축 목록에서 quantity 를 뺀 것 중 첫 번째를 지시한다.
        #   T0 게이팅 결과를 그대로 쓰므로 매입과 T3 가 같은 목록을 본다.
        alt = [a for a in allowed_axes if a != "quantity"]
        if not alt:
            # ★ v0.3 — timing 게이팅(§3.5.1)으로 허용 축이 quantity 뿐인 날.
            #   회송해도 매입이 만들 수 있는 축이 없으므로 지시할 것이 없다.
            #   §5.0 단일안 예외로 직행한다.
            return None
        return FeedbackHint(
            "VARIANT_COLLAPSED",
            alt[0],
            band.floor_kg,
            band.cap_kg,
            binding or ("variant_diversity",),
            max(0.0, slack) if math.isfinite(slack) else 0.0,
            "kg",
            f"quantity 축이 {desc} 로 좁아 시나리오가 한 점으로 수렴했다. "
            f"바인딩: {', '.join(binding) or '-'}. "
            f"허용 축 {list(allowed_axes)} 중 '{alt[0]}' 로 2안 이상 재생성할 것.",
        )

    if results and all(r.clipped for r in results):
        violated = tuple(dict.fromkeys(c for r in results for c in r.binding_constraints))
        worst = max(r.original_total_kg - r.total_kg for r in results)
        return FeedbackHint(
            "ALL_CLIPPED",
            "quantity",
            band.floor_kg,
            band.cap_kg,
            violated,
            worst,
            "kg",
            f"전 시나리오가 밴드 밖이다. 최대 초과 {worst:,.0f}kg. "
            f"바인딩 제약: {', '.join(violated)}. 해당 축을 완화해 재생성할 것.",
        )
    return None
