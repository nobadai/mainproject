"""
outbound.py — 사이클 B 결합·클리핑 (S3). 담당: 이현서

═══════════════════════════════════════════════════════════════════════
 사이클 A 의 band.py 와 **모양이 다르다.**

     A(T3)   floor(kg) ≤ 수량 ≤ cap(kg)  ·  금액 ≤ cap(원)
             → 세 축이 서로 다른 단위. 금액↔수량 환산이 난점이었다.

     B(S3)   Σ배분 ≤ 하루 공용 출고 능력   ← **단일 공유 자원**
             배분[i] ≤ on_hand[i]         ← 품목별 물리 상한
             → 전부 kg. 환산이 없다. 대신 '공유 자원을 후보들이 나눠 쓰는' 문제다.

 그래서 Band 를 재사용하지 않고 OutboundBand 를 따로 둔다.
 Band 에는 cap_amount_krw · cap_by_date_kg 처럼 B 에서 의미 없는 축이 있고,
 억지로 끼우면 "이 필드는 B 에서 안 쓴다"는 주석이 계약 전체에 번진다.

 ★ 클리핑 결과는 ClipResult 를 그대로 쓴다 (scenario_id = allocation_id).
   러너(cycle.py)와 승인 경로가 ClipResult 를 전제로 짜여 있고,
   삼중 일치 관련 필드는 B 에서 비어 있어도 무해하다.
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from app.orchestrator.contracts_core import (
    ITEMS,
    ClipResult,
    Dept,
    ItemCode,
    OutboundBand,
    SaleAllocation,
    T2Reply,
)

INF = float("inf")
EPS = 1e-6


# ===========================================================================
# S3-1  결합
# ===========================================================================


def combine_outbound_band(
    replies: Mapping[Dept, T2Reply],
    items: Iterable[ItemCode] = ITEMS,
) -> OutboundBand:
    """
        cap_kg[i]    = min(재고가 낸 품목별 상한)
        cap_total_kg = min(재고가 낸 공용 출고 능력)

    ★ 재무는 밴드를 움직이지 않는다 (§3.1 — B 에서 재무는 선호 신호만 낸다).
      판매는 현금을 늘리는 행위이므로 금액 하드 상한을 걸면 방향이 반대가 된다.
      재무 회신은 soft_notes 로 모아 H2 화면에 전달한다.

    ★ 소프트 경고는 밴드를 움직이지 않는다 — 사이클 A 와 같은 규칙이다.
    """
    items = tuple(items)
    cap: dict[ItemCode, float] = {i: INF for i in items}
    cap_total = INF
    contributors: dict[str, str] = {}
    notes: list[str] = []

    for dept, reply in replies.items():
        for chk in reply.checks:
            if chk.kind != "hard":
                if chk.verdict != "ok" and chk.reason:
                    notes.append(f"[{dept}/{chk.severity}] {chk.reason}")
                continue

            if chk.cap_kg:
                for i, v in chk.cap_kg.items():
                    if v < cap.get(i, INF):
                        cap[i] = float(v)
                        contributors[f"cap_kg.{i}"] = chk.check_id

            if chk.cap_total_kg is not None and chk.cap_total_kg < cap_total:
                cap_total = float(chk.cap_total_kg)
                contributors["cap_total_kg"] = chk.check_id

    return OutboundBand(cap, cap_total, contributors, tuple(notes))


# ===========================================================================
# S3-2  ※ 사이클 B 에는 교착 판정이 없다
# ===========================================================================
#
# ★ 정의서에 사이클 B 교착이라는 개념이 없다. 의도적으로 만들지 않는다.
#
#   A 의 교착은 `floor > cap` — 사야 하는데 살 수 없는 상태이고, 후보를 만들어
#   봐야 전부 밴드 밖이므로 생성을 건너뛰는 것이 이득이다.
#
#   B 에는 floor 가 없다(OutboundBand 주석 참조). 확정 납품 의무가 floor 처럼
#   보이지만, 못 채우는 것은 **교착이 아니라 정상 상태**다 — 재고가 부족하면
#   오늘은 부족한 만큼만 내보내고 며칠 뒤 도착할 매입으로 채운다.
#   그래서 §3.7.4 는 B 의 0 후보에 대해 "되돌림 루프 → 보류, 단 계약 납품
#   의무를 못 채우면 별도 처리(E5)"라고만 정했다.
#
#   의무 충족 여부는 S3 가 `compute_has_unmet_obligation()` 으로 따로 산출하고
#   (§5.0 — S3 전속), 판정은 `resolve_end_code()` 가 한다.
#   여기서 교착으로 끊으면 **부분 충족이 가능한 날까지 판매 0 으로 끝난다.**


# ===========================================================================
# S3-3  클리핑
# ===========================================================================


def clip_allocation(alloc: SaleAllocation, band: OutboundBand) -> ClipResult:
    """
    1단계  품목별 cap 클램프          배분[i] ≤ cap_kg[i]
    2단계  공용 출고 능력 비례 축소    Σ배분 ≤ cap_total_kg

    ★ 2단계가 비례 축소인 이유.
      공용 출고 능력은 **어느 품목에도 귀속되지 않는 공유 자원**이다.
      선착순으로 깎으면 legs 의 나열 순서가 손익을 결정해 임의성이 들어간다.
      §3.7.3 이 사이클 A 의 품목 안분에서 "선착순과 순차 실행은 채택하지 않는다"고
      정한 것과 같은 논리다.

    ★ ClipResult 의 삼중 일치 필드(clipped_split_plan · clipped_sourcing_plan)는
      B 에서 비운다. 분할 입고·등급 조달은 매입 개념이며 판매 배분에는 없다.
    """
    qty = _outbound_qty_of(alloc)
    binding: list[str] = []

    # 1단계 — 품목별
    clipped = {}
    for i, v in qty.items():
        c = band.cap_kg.get(i, INF)
        clipped[i] = min(v, c)
        if clipped[i] < v - EPS:
            binding.append(f"cap_kg.{i}")

    # 2단계 — 공용 출고 능력
    #
    # ★ v1.2.3 — 대조 대상은 **그날의 총 출고량**이다 (영업 IO 명세 §5).
    #   outbound_by_date 가 있으면 그 최대 일자를 쓰고, 없으면 배분 합으로 폴백한다.
    #   두 값의 범위가 다르다 — outbound_by_date 는 확정 납품분을 포함하고
    #   legs 는 전략 가능 재고만 담는다. legs 합으로만 검사하면 확정 납품분이 빠져
    #   **출고 능력을 과소 계상**한다.
    peak = _peak_outbound_kg(alloc)
    total = sum(clipped.values())
    basis = peak if peak is not None else total
    if basis > band.cap_total_kg + EPS and total > EPS:
        # 초과분만큼 전략 배분에서 깎는다. 확정 납품분은 의무이므로 줄일 수 없다.
        allowed = max(0.0, band.cap_total_kg - (basis - total))
        factor = min(1.0, allowed / total)
        clipped = {i: v * factor for i, v in clipped.items()}
        binding.append("cap_total_kg")

    amount = _amount_after_clip(alloc, qty, clipped)
    return ClipResult(
        scenario_id=alloc.allocation_id,
        qty_kg=qty,
        clipped_qty_kg=clipped,
        clipped_amount_krw=amount,
        binding_constraints=tuple(binding),
        infeasible=sum(clipped.values()) <= EPS,
    )


def clip_allocations(
    allocs: Iterable[SaleAllocation],
    band: OutboundBand,
) -> list[ClipResult]:
    return [clip_allocation(a, band) for a in allocs]


def _outbound_qty_of(alloc) -> dict[ItemCode, float]:
    """품목별 **출고** 수량. HOLD 제외 (v1.2.3)."""
    getter = getattr(alloc, "outbound_qty_by_item", None)
    if getter is not None:
        return dict(getter)
    out: dict[ItemCode, float] = {}
    for leg in alloc.legs:
        if not getattr(leg, "is_outbound", True):
            continue
        out[leg.item] = out.get(leg.item, 0.0) + leg.qty_kg
    return out


def _peak_outbound_kg(alloc) -> float | None:
    """
    `outbound_by_date` 의 최대 일자 출고량.

    ★ 공용 출고 능력은 **하루** 한도이므로 기간 합이 아니라 피크로 본다.
      분할 납품으로 여러 날에 나눠 내보내면 각 날짜가 따로 한도를 받는다.
    """
    legs = getattr(alloc, "outbound_by_date", ()) or ()
    if not legs:
        return None
    by_date: dict = {}
    for leg in legs:
        by_date[leg.date] = by_date.get(leg.date, 0.0) + leg.qty_kg
    return max(by_date.values()) if by_date else None


def _amount_after_clip(
    alloc,
    original: Mapping[ItemCode, float],
    clipped: Mapping[ItemCode, float],
) -> float:
    """
    축소 비율을 그대로 금액에 적용한다.

    ★ 단가를 다시 계산하지 않는다. 판매 단가는 S1 이 채널별로 정한 값이고,
      오케스트레이터가 단가를 만지면 §5.1(숫자 생성 금지) 위반이다.
      비율 곱셈은 상수배이므로 생성이 아니다 — 사이클 A 의 클리핑과 같은 논리다.

    ★ v1.2.3 — HOLD 는 금액에 넣지 않는다. 팔지 않은 물량에는 매출이 없다.
    """
    total = 0.0
    for leg in alloc.legs:
        if not getattr(leg, "is_outbound", True):
            continue
        o = original.get(leg.item, 0.0)
        factor = 1.0 if o <= EPS else clipped.get(leg.item, 0.0) / o
        total += leg.qty_kg * factor * leg.unit_price_krw_per_kg
    return total


# ===========================================================================
# S3-4  후보 수렴 판정
# ===========================================================================


def detect_allocation_collapse(results: Sequence[ClipResult]) -> bool:
    """
    후보가 사실상 하나로 수렴했는가. 사이클 A 의 붕괴 판정과 같은 취지다.

    ★ 다만 분할 구조가 없으므로 지문은 **품목별 수량 벡터**뿐이다.
      A 는 총량이 같아도 분할이 다르면 살아 있는 선택지였지만(§3.7.2),
      B 에는 그런 축이 없다.
    """
    live = [r for r in results if not r.infeasible]
    if len(live) < 2:
        return True
    sigs = {tuple(sorted((i, round(v, 1)) for i, v in r.clipped_qty_kg.items())) for r in live}
    return len(sigs) < 2
