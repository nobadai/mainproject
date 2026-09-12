"""⑥ package_scenarios — 보수/기본/공격 묶기 + 근거 작성 (상세설계 §4-⑥).

Epic 2는 rule_only 경로다. LLM 몫은 **문장을 다듬는 것**이고 숫자는 계산이 소유한다
(규칙 6). Epic 3에서 붙일 때도 이 함수를 다시 쓰는 게 아니라, 아래가 만든 결과를
입력으로 받아 rationale·risks의 서술만 손본다.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes._guards import pending_value, require_positive
from app.purchase_agent.nodes.allocate_sourcing import candidate_summary
from app.purchase_agent.nodes.classify_situation import (
    compute_ci_width,
    compute_rise_rate_2w,
    is_gate_excluded,
    judgment_row,
)
from app.purchase_agent.nodes.draft_plan import (
    ADJUSTMENT_CAP_NAME,
    purchase_budget_krw,
    split_adjustments,
)
from app.purchase_agent.nodes.split_plan import effective_allowed_axes, split_decision
from app.purchase_agent.quotes import observed_date, observed_spec
from app.purchase_agent.schemas import DOCUMENT_SOURCE, TIMING_AXIS, document_ref
from app.purchase_agent.state import PurchaseAgentState


def assign_axes(labels: list[str], allowed_axes: list[str], aggressive_axis: str) -> dict[str, str]:
    """안별 ``strategy_type``을 허용 축 안에서 고른다 (정의서 §3.5.1-2).

    축이 하나뿐인 날은 전 안이 같은 축을 쓴다 — 그게 정상이고, ⑦의 중복 검사도 그날은
    면제한다. 축이 여럿이면 겹치지 않게 배분해 "3안인데 사실 한 안"을 피한다.
    """
    if not labels:
        # 안이 하나도 없는 날 — ③이 시세를 못 받아 초안을 만들지 않았다. 배정할 축이 없다.
        # 이 줄이 없으면 아래 ``labels[-1]``이 IndexError로 죽고, 그러면 "왜 안이 없는가"라는
        # 사유가 오케스트레이터에 도달하지 못한다.
        return {}
    if len(allowed_axes) == 1:
        return dict.fromkeys(labels, allowed_axes[0])
    axes = dict.fromkeys(labels, "quantity")
    if aggressive_axis in allowed_axes and "공격" in labels:
        axes["공격"] = aggressive_axis
    else:
        axes[labels[-1]] = next(axis for axis in allowed_axes if axis != "quantity")
    return axes


def split_offsets(coverage_days: int, rounds: int) -> list[int]:
    """회차별 **매입 실행일** 오프셋 = ``round(i × D / rounds)``.

    첫 회차는 항상 0(= as_of)이다 — IO명세 §2 "seq 1의 date = as_of".
    날짜를 ④가 아니라 여기서 만드는 이유: 안마다 D가 다르다 (§4-④ E3-3 확정 4).
    보수(D=2)와 공격(D=12)에 같은 날짜를 박으면 보수안의 2회차가 커버 구간 밖으로 나간다.

    ⚠️ 이 date는 **도착일이 아니다.** 도착일 = ``date + N4``이고 N4가 NULL이라 계산하지
    않는다 (§5.5 · 규칙 3).
    """
    return [round(index * coverage_days / rounds) for index in range(rounds)]


def round_offsets(
    as_of: str,
    coverage_days: int,
    rounds: int,
    calendar: Mapping[str, Any] | None = None,
) -> list[int]:
    """회차 오프셋을 **장이 서는 날로 민다** (`#300` · `SHIFT`).

    ``split_offsets`` 가 낸 자리가 휴장일이면 다음 개장일로 민다. 경계는 마스터가 봉투로
    싣는 ``execution_calendar`` 하나이고, 여기서 요일도 공휴일도 다시 판정하지 않는다 —
    **값은 아는 쪽이 공급하고 계산은 쓰는 쪽이 한다** (`master/execution_calendar.py`).

    ★★ **소관이 우리다.** 마스터 모듈이 경계를 그렇게 적었다::

        마스터   비영업일 목록 + 그 목록이 덮는 지평
        매입     목록에 있으면 다음 날로 민다          ← 여기

    ★ **미는 것이지 버리는 것이 아니다** (물류 회신 2026-09-10). 남은 회차로 재분배하면
      그쪽 도착일이 ``cap_by_date`` 를 넘길 수 있어 ``DROP`` 을 안 쓴다.

    🔴 **1회차는 안 민다.** ``seq 1`` 의 날짜는 ``as_of`` 라고 IO명세 §2 가 못박았고,
      약정을 조립하는 마스터가 그 등식을 본다. ``as_of`` 자체가 휴장일이면 그날은
      살 수 없는 날이므로 **미는 것이 아니라 안이 서면 안 되는 것**이고, 그 판정은
      ⑦ ``market_open_days`` 가 컷으로 낸다.

    🔴 **지평 밖이면 아무것도 안 민다.** 지평 밖은 «안 선다» 가 아니라 «모른다» 라서,
      한 회차라도 그 밖으로 나가면 **밀기 전 자리를 그대로 돌려준다.** 절반만 민 계획은
      민 이유도 안 민 이유도 설명할 수 없다 — ⑦ 이 같은 태도로 «못 봤다» 를 적는다.

    ★ **순서를 지킨다.** 민 자리가 앞 회차와 같거나 앞서면 계속 민다. 회차가 겹치면
      분할이 아니라 같은 매입을 두 줄로 적은 것이 된다 (``split_infeasible_reason``).
    """
    base = split_offsets(coverage_days, rounds)
    closed = set((calendar or {}).get("non_execution_days") or ())
    horizon_end = (calendar or {}).get("horizon_end")
    if not closed or not horizon_end:
        # 달력이 없거나 지평을 모르면 밀 근거가 없다. **빈 목록을 «안 서는 날이 없다»로
        # 읽지 않는다** — 그 고지는 ⑦이 ``skipped`` 로 낸다.
        return base
    start = date.fromisoformat(as_of)
    shifted: list[int] = []
    for index, offset in enumerate(base):
        if index == 0:
            shifted.append(offset)
            continue
        candidate = max(offset, shifted[-1] + 1)
        while (start + timedelta(days=candidate)).isoformat() in closed:
            candidate += 1
        if (start + timedelta(days=candidate)).isoformat() > horizon_end:
            return base
        shifted.append(candidate)
    return shifted


def shifted_rounds_note(
    as_of: str,
    coverage_days: int,
    rounds: int,
    calendar: Mapping[str, Any] | None = None,
) -> str | None:
    """민 사실을 **문장 하나로**. 안 밀었으면 ``None``.

    ⚠️ **커버가 늘면 판단이 바뀐 것이다** — 마스터 회신 §4.2 가 *"밀어서 D 커버가 늘면
    그건 판단이 바뀐 것입니다. risks 에 적어 주십시오"* 라 했다. 그래서 밀린 날수만
    적지 않고 **마지막 회차가 커버 구간을 넘었는지**를 같이 적는다.

    ★ 밀기와 고지가 **같은 순수 함수**를 두 번 부른다 — 같은 입력이면 같은 답이라
    수량과 고지가 갈라질 수 없다 (``_split_risks`` 의 재계산과 같은 규율).
    """
    base = split_offsets(coverage_days, rounds)
    moved = round_offsets(as_of, coverage_days, rounds, calendar)
    if moved == base:
        return None
    start = date.fromisoformat(as_of)
    parts = [
        f"{index + 1}회차 {(start + timedelta(days=was)).isoformat()}"
        f" → {(start + timedelta(days=now)).isoformat()}"
        for index, (was, now) in enumerate(zip(base, moved, strict=True))
        if was != now
    ]
    note = f"장이 안 서는 날이라 회차일을 미뤘다 ({' · '.join(parts)}) — 받은 개장 달력 기준"
    if moved[-1] >= coverage_days:
        note += (
            f". ⚠️ 마지막 회차가 커버 {coverage_days}일 구간을 벗어났다"
            f" (오프셋 {base[-1]} → {moved[-1]}) — 커버가 늘어난 만큼 판단이 달라진다"
        )
    return note


def split_quantities(total_qty_kg: int, chosen: list[dict]) -> list[int]:
    """회차별 수량. **마지막 회차가 잔량을 흡수한다** — 반올림이 총량을 흔들면
    사중 일치가 깨진다."""
    remaining = total_qty_kg
    quantities = []
    for index, part in enumerate(chosen, start=1):
        qty = remaining if index == len(chosen) else round(total_qty_kg * part["ratio"])
        quantities.append(qty)
        remaining -= qty
    return quantities


def split_infeasible_reason(
    total_qty_kg: int, chosen: list[dict], coverage_days: int
) -> str | None:
    """이 안이 이 분할을 감당하는가. 못 하면 **사유**를, 되면 ``None``을 돌려준다.

    ④는 그날 하나의 유형을 정하고 안별 총량·D는 모른다. 감당 여부는 여기서 안별로 본다.

    막는 것 둘:

    1. **0kg 회차** — ``SplitPlanItem.qty_kg > 0``이라 하나만 나와도 스키마가 **제안 전체**를
       죽인다. E3-1에서 등급 배분이 정확히 이 자리에서 터졌다 (Codex 교차검증 P1).
    2. **겹치는 날짜** — 회차가 커버일수보다 많으면 같은 날 두 번이 되고, 그건 분할이 아니라
       같은 매입을 두 줄로 적은 것이다.
    """
    rounds = len(chosen)
    if rounds > coverage_days:
        return f"커버일수 {coverage_days}일보다 회차 수({rounds})가 많다"
    offsets = split_offsets(coverage_days, rounds)
    if any(earlier >= later for earlier, later in pairwise(offsets)):
        return f"회차 날짜가 겹친다 (오프셋 {offsets})"
    quantities = split_quantities(total_qty_kg, chosen)
    if any(qty < 1 for qty in quantities):
        return f"회차당 최소 수량 미달 — {total_qty_kg:,}kg을 {rounds}회로 나누면 {quantities}"
    return None


def arrival_dates(
    as_of: str,
    coverage_days: int,
    rounds: int,
    lead_days: int | None,
    calendar: Mapping[str, Any] | None = None,
) -> list[str] | None:
    """회차별 **도착일** = 회차일 + N4 (상세설계 §5.5).

    ``round_offsets``를 재사용한다 — 매입일을 두 곳에서 각자 계산하면 회차 날짜와
    도착일이 어긋나고, 어긋난 쪽을 아무도 못 찾는다.

    ★ **회차일이 밀리면 도착일도 따라 밀린다** (`#300`). 마스터 모듈이 *"도착일은 따라
    밀린다 — 도착일 자체는 안 본다 (물류 축)"* 로 그 방향을 적었다. 여기서 도착일을
    따로 밀면 매입이 물류 규약을 대신 정하는 것이 된다.

    N4가 없으면 ``None``이다. **0으로 채우지 않는다** — 0은 "당일 도착"이라는 확정된
    값이라, 미결을 0으로 적으면 "오늘 승인분이 오늘 도착"이 사실이 된다 (규칙 3).
    """
    if lead_days is None:
        return None
    start = date.fromisoformat(as_of)
    return [
        (start + timedelta(days=offset + lead_days)).isoformat()
        for offset in round_offsets(as_of, coverage_days, rounds, calendar)
    ]


#: 회차 수량을 **재배분하지 못한** 사유. 넷을 갈라 적는 이유는 ``shelf_days_block_reason``과
#: 같다 — 결과는 "균등 유지" 하나인데 원인이 넷이라, 뭉치면 무엇을 고쳐야 하는지가 사라진다.
#:
#: ⚠️ **더 이상 risks 로 나가지 않는다** (#93 · 2026-09-03). 날짜 축 고지는 ⑦
#:   ``ARRIVAL_SKIP_REASONS`` 가 소유한다 — ⑥의 고지 경로가 timing 축 전용이라
#:   quantity 축 안이 통째로 빠졌기 때문이다. 여기 남는 것은 ``cap_constrained_quantities``
#:   가 *어느 갈래로 갔는지* 를 밝히는 **함수 자신의 반환값**이고, 그 갈래를 시험하는
#:   단위 테스트가 소비자다.
CAP_BLOCK_REASONS = {
    "no_cap": (
        "회차별 창고 여유 검사를 하지 않았다 — 물류에서 날짜별 입고 여유를 받지 못했다. "
        "여유를 0으로 가정하지 않고 회차를 균등하게 나눴다"
    ),
    "no_lead": (
        "회차별 창고 여유 검사를 하지 않았다 — 입고 소요일이 정해지지 않아 도착일을 "
        "계산할 수 없다. 회차를 균등하게 나눴다"
    ),
}


def cap_constrained_quantities(
    quantities: list[int],
    arrivals: list[str] | None,
    cap_by_date: Mapping[str, float] | None,
) -> tuple[list[int], str | None]:
    """도착일 수용량으로 회차 수량을 재배분한다. **총합은 불변이다.**

    상한을 넘는 만큼을 뒤 회차로 넘긴다. 넘길 곳이 없으면 재배분을 **포기하고**
    균등 분할을 그대로 돌려준다 — 총량을 줄이면 사중 일치가 깨지고, 마지막 회차에
    억지로 얹으면 상한을 지킨 척하면서 어긴 계획이 된다.

    ⚠️ **받지 못한 날짜는 0이 아니라 "안 봤다"다.** ``cap_by_date``는 물류가 정한
    조회 기간만큼만 계산해 보낸다 (기간 길이는 물류 소유값이라 여기 적지 않는다 —
    같은 수를 두 곳에 적으면 한쪽만 바뀐다).
    ``.get(d, 0)``으로 읽으면 창 밖 회차가 **수용량 0**이 되어 통째로 죽는다 —
    이 함수가 막는 것이 그것이다 (규칙 3).

    회차 하나라도 수용량을 모르면 **아무 회차도 조정하지 않는다.** 아는 날짜만 조이면
    남은 물량이 모르는 날짜로 밀려가 "모르는 곳에 더 쌓는" 계획이 되고, 계획의 모양이
    "어느 날짜가 우연히 창 안이었나"에 좌우돼 설명할 수 없게 된다.
    """
    if cap_by_date is None:
        return quantities, CAP_BLOCK_REASONS["no_cap"]
    if arrivals is None:
        return quantities, CAP_BLOCK_REASONS["no_lead"]

    unknown = [day for day in arrivals if cap_by_date.get(day) is None]
    if unknown:
        return quantities, (
            f"회차별 창고 여유 검사를 하지 않았다 — 도착일 {', '.join(unknown)}의 여유를 "
            "물류에서 받지 못했다(조회 기간 밖이거나 값이 비어 있다). 받지 못한 날을 "
            "여유 0으로 읽지 않고 회차를 균등하게 나눴다"
        )

    adjusted: list[int] = []
    carried = 0
    occupied = 0
    for quantity, day in zip(quantities, arrivals, strict=True):
        want = quantity + carried
        # 수용량은 **상한**이라 내림한다 — 올림하면 못 넣는 양을 계획하게 된다
        # (``warehouse_cap_kg``와 같은 이유).
        cap = int(cap_by_date[day])
        # ★ **앞 회차가 아직 창고에 있다.** ``cap_by_date[d]``는 그날의 여유 공간인데,
        #   물류는 **기존 일정만** 재생해 그 값을 낸다 (`logistics/tools.py`
        #   ``calculate_cap_by_date``: guaranteed − projected_occupancy). 우리가 새로
        #   넣을 회차는 거기 없다. 날짜마다 독립으로 비교하면 1회차 30kg이 남아 있는데도
        #   2회차가 그날 상한을 통째로 쓰는 계획이 나온다 — 총합은 맞고 하드 제약은 깨진다.
        room = max(0, cap - occupied)
        take = min(want, room)
        adjusted.append(take)
        carried = want - take
        occupied += take

    if any(quantity < 1 for quantity in adjusted):
        # 0kg·음수 회차는 ``SplitPlanItem.qty_kg > 0``이라 **제안 전체**를 죽인다.
        # 균등 분할은 ``split_infeasible_reason``의 최소 수량 검사를 이미 통과한 값이라
        # 되돌리면 안전하다. 재배분이 그 검사를 **사후에 깨는** 자리라 여기서 한 번 더 본다
        # (음수 수용량이 섞이면 앞 회차가 음수가 되고 총합은 맞아 사중 일치는 통과한다 —
        # 스키마에서야 터지는, 조용히 지나가는 구간이다).
        return quantities, (
            "🔴 회차별 창고 여유를 지킬 수 없다 — 여유에 맞추면 물량이 0인 회차가 생겨 "
            "실행할 수 없는 계획이 된다. 회차를 균등하게 나눴으므로 "
            "**이 계획은 날짜별 창고 여유를 넘는다**"
        )
    if carried:
        return quantities, (
            f"🔴 회차별 창고 여유를 지킬 수 없다 — 총량 중 {carried:,}kg을 넣을 자리가 없다 "
            f"(마지막 도착일 {arrivals[-1]} 기준). 총량은 매입 수량 산정 단계가 정하므로 "
            "여기서 줄이지 않았고, 회차를 균등하게 나눴다 — "
            "**이 계획은 날짜별 창고 여유를 넘는다**"
        )
    if adjusted == quantities:
        return quantities, None
    moved = " · ".join(
        f"{seq}회 {before:,}→{after:,}kg({day} 도착)"
        for seq, (before, after, day) in enumerate(
            zip(quantities, adjusted, arrivals, strict=True), 1
        )
    )
    return adjusted, f"회차 물량을 날짜별 창고 여유에 맞춰 옮겼다 — {moved}"


def materialize_split(
    as_of: str,
    total_qty_kg: int,
    chosen: list[dict] | None,
    coverage_days: int,
    *,
    lead_days: int | None = None,
    cap_by_date: Mapping[str, float] | None = None,
    calendar: Mapping[str, Any] | None = None,
) -> list[dict]:
    """회차별 매입 계획. ④가 유형·비율만 정하므로 안별 총량과 날짜를 여기서 만든다.

    ``None``(일괄)이거나 이 안이 분할을 감당하지 못하면 단일 회차로 되돌린다.
    되돌린 사실은 ``_split_risks``가 안의 risks에 싣는다 — timing 라벨인데 회차가 하나인
    상태를 조용히 넘기면 소비자가 라벨과 행동의 불일치를 추적할 수 없다.

    ★ **``calendar`` 는 회차일을 미는 데만 쓴다** (`#300`). 감당 여부(``split_infeasible_reason``)
    는 **밀기 전 자리**로 본다 — 그 물음이 *"이 분할이 커버 D 안에 들어가는가"* 라서
    달력과 무관하고, 밀린 뒤로 재면 «커버를 넘어서 못 한다» 가 되어 마스터가 *"밀어라"*
    고 한 것을 «분할 불가» 로 뒤집는다.
    """
    if not chosen:
        return _rounds(as_of, coverage_days, [total_qty_kg], lead_days, calendar)
    _validate_ratios(chosen, "split_plan")
    if split_infeasible_reason(total_qty_kg, chosen, coverage_days):
        return _rounds(as_of, coverage_days, [total_qty_kg], lead_days, calendar)

    quantities, _ = cap_constrained_quantities(
        split_quantities(total_qty_kg, chosen),
        arrival_dates(as_of, coverage_days, len(chosen), lead_days, calendar),
        cap_by_date,
    )
    return _rounds(as_of, coverage_days, quantities, lead_days, calendar)


def _rounds(
    as_of: str,
    coverage_days: int,
    quantities: list[int],
    lead_days: int | None,
    calendar: Mapping[str, Any] | None = None,
) -> list[dict]:
    """회차 dict 목록. **매입일과 도착일을 같은 ``split_offsets``에서 만든다.**

    도착일을 다른 자리에서 만들면 두 날짜가 어긋나고 어긋난 쪽을 아무도 못 찾는다
    (``arrival_dates`` docstring과 같은 이유).

    ⚠️ **일괄(1회차)도 이 함수를 지난다.** ``materialize_split``의 반환 경로가 셋인데
    (일괄·분할 불가·정상) 경로마다 따로 채우면 한 곳이 빠지고, 그러면 *"회차 N건 중
    도착일이 있는 것은 M건뿐"*(Critic ``E-ARRIVAL-COLLAPSE``)이 된다.
    ``split_offsets(D, 1) == [0]``이라 1회차도 같은 식으로 덮인다.

    N4가 없으면 도착일은 **전 회차 ``None``**이다 — 0으로 채우지 않는다 (규칙 3).
    """
    start = date.fromisoformat(as_of)
    offsets = round_offsets(as_of, coverage_days, len(quantities), calendar)
    arrivals = arrival_dates(as_of, coverage_days, len(quantities), lead_days, calendar)
    if arrivals is None:
        arrivals = [None] * len(quantities)
    return [
        {
            "seq": index + 1,
            "date": (start + timedelta(days=offset)).isoformat(),
            "qty_kg": qty,
            "expected_arrival_date": eta,
        }
        for index, (offset, qty, eta) in enumerate(
            zip(offsets, quantities, arrivals, strict=True)
        )
    ]


def _validate_ratios(ratios: list[dict], name: str) -> list[dict]:
    """비율이 양수이고 합이 1인지 확인한다.

    검증하지 않으면 작은 비율은 ``round()``로 0이 되고, 합이 1을 넘으면 마지막 항목이
    **음수 수량**이 된다. 마지막이 잔량을 흡수하므로 사중 일치는 통과하고, 스키마 검증에서야
    제안 전체가 터진다 — 조용히 지나가는 구간을 만들지 않는다.
    """
    if not ratios:
        raise ValueError(f"{name} is empty")
    for line in ratios:
        require_positive(line["ratio"], f"{name}.ratio")
    total = sum(line["ratio"] for line in ratios)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"{name} ratios must sum to 1, got {total}")
    return ratios


def materialize_sourcing(total_qty_kg: int, ratios: list[dict]) -> list[dict]:
    """등급별 절대 수량. ⑤가 비율만 정하므로 안별 총량을 여기서 곱한다.

    마지막 줄이 잔량을 흡수한다 — ``Σ sourcing.qty_kg == total_qty_kg``가 사중 일치의
    한 축이라 반올림 오차를 남기면 ⑦이 컷한다.
    """
    _validate_ratios(ratios, "sourcing_plan")
    remaining = total_qty_kg
    lines = []
    for index, line in enumerate(ratios, start=1):
        qty = remaining if index == len(ratios) else round(total_qty_kg * line["ratio"])
        lines.append(
            {
                "market": line["market"],
                "grade": line["grade"],
                "qty_kg": qty,
                "grade_unit_price": line["grade_unit_price"],
            }
        )
        remaining -= qty
    return lines


def usable_forecast_window(forecast: dict, coverage_days: int) -> list[dict]:
    """커버 구간에서 **판단에 쓸 수 있는 행만** 남긴다 (#213 · ML 회신 2026-08-27).

    ``gate_reason`` 에 ``quality`` 가 든 행을 뺀다 — ``is_gate_excluded`` 가 기준이고
    **``is_gated`` 는 안 본다.** 왜 그 둘을 가르는지는 그 함수에 적었다.

    🔴 **빌 수 있다.** 창 전체가 ``quality`` 면 이 구간으로는 상한을 못 정한다.
      그 상태는 여기서 터뜨리지 않고 **어댑터가 앞에서 막는다**
      (``adapter.validate_forecast`` — 가장 짧은 커버 구간을 본다). 큰 창은 짧은 창을
      포함하므로, 짧은 창에 쓸 행이 하나라도 있으면 큰 창도 빈 적이 없다.

    ⚠️ 그래도 여기서 한 번 더 터뜨리는 이유: 어댑터를 안 거치는 경로
      (``run_purchase_agent`` 단독 실행)가 있고, **조용히 빈 max() 를 부르면
      ``ValueError: max() arg is an empty sequence`` 라는 아무 말도 아닌 메시지가 난다.**
    """
    window = [row for row in forecast["daily"][:coverage_days] if not is_gate_excluded(row)]
    if not window:
        raise ValueError(
            f"커버 {coverage_days}일 구간의 예측이 전부 품질 게이트라 매입 상한을 정할 수 없다"
        )
    return window


def compute_max_price(forecast: dict, coverage_days: int) -> int:
    """max_price = 커버 구간 안 예측 상단의 최대값 (규칙 5).

    **마진 방어선과 무관하다** (규칙 5). 혼동하기 쉬운 두 값을 구분해둔다:

    - ``max_price`` 는 **재무 STRESS 로 나간다** — ``amount_max_krw = qty × 이것`` 을
      재무·마스터가 검사한다 (``finance/capabilities/scenario.py`` 의 등식 ·
      ``master/verifier.py`` 의 ``L-PAYSCHED-MAX``). 🔴 **컷이 아니다** (`#394`).
    - 컷은 ``cut_unit_price`` 가 한다 — ``self_check.check_max_price`` 가 그 값을 읽고,
      ``grade_unit_price`` 가 넘는 안을 죽인다. ⚠️ 지금은 두 값이 같다. 갈라만 두었다
    - ``contract_price`` 초과 → 컷이 아니라 ``margin_warning=true`` 표시만

    커버 구간으로 자르는 이유: 그 안에서만 실제로 사기 때문이다.

    ★ **``quality`` 게이트 행은 빠진다** (``usable_forecast_window``). 지금 AUC 에는
      그런 행이 0건이라 값이 안 바뀐다 — 실측으로 확인했다 (12-31 관통 무변동).
    """
    return max(row["upper"] for row in usable_forecast_window(forecast, coverage_days))


def compute_cut_unit_price(forecast: dict, coverage_days: int) -> int:
    """매입 **컷 기준**. ⚠️ 값이 둘인데 지금은 같다 (2026-09-08 · `dev@de7e132` · #394 기준).

    ::

        max_price       재무 STRESS 로 나간다 (amount_max_krw = qty × 이것)
                        🔴 재무·마스터가 그 등식을 검사한다
                           finance/capabilities/scenario.py  amount_max_krw 등식
                           master/verifier.py                검사 이름 L-PAYSCHED-MAX
        cut_unit_price  우리 컷 기준 (self_check.check_max_price)

    🔴 **왜 갈랐나** — 하나였을 때 밴드가 좁아지면 컷이 엄격해지고 재무 STRESS 는
    느슨해졌다. **방향이 반대인데 값이 하나였다.**

    ★ 이 판은 **"고정"이 아니라 "갈라놓기"** 다. 밴드가 바뀌면 STRESS 는 여전히 따라간다.

    🔴 ~~`09-17` 에 밴드가 바뀐다~~ — **낡았다** (ML 회신 2026-09-10). 밴드 교체는
    `09-03` 에 이미 끝났고, `09-17` 은 «그림자 기록 2주가 차는 날» 이다. 그래서 **둘이
    갈라지는 계기는 날짜가 아니라 이 함수의 몸통을 바꾸는 것**이다.

    🟢 **재무가 답했다** (2026-09-08) — *"별도 새 기준이 오기 전까지는 **현재값을
    고정해서 진행하셔도 됩니다**"*.

    🔴 다만 *"현재값"* 이 **값인지 산식인지**가 갈린다.

    .. code-block:: text

        값을 박는다   품목 3 × 커버 2/5/12 = 여섯~아홉 개를 손으로 적어야 하고,
                      실측상 매일 최대 11% 움직인다
        산식을 둔다   지금 상태 — 밴드가 바뀌면 따라간다

    ⚠️ **이 판은 산식을 둔 쪽이다. 되물었고 답이 왔다.**

    🟢 **여유율 값이 정해졌다** (ML 회신 2026-09-10 · q80 · 초과분 분위). 커버 2일은
    `LT2` 자리이고, 그 값은 배추 `9.6%` · 무 `15.0%` · 양파 `8.8%` 다.

    .. code-block:: text

        지금   max(upper)                     밴드 상단 — 모델이 낸 «넓이»
        바꿈   max(predicted) × (1 + 여유율)   가운데 값에서 실측 초과분만큼 올림

    🔴 **둘을 더하면 안 된다** — ``upper`` 에 여유율을 또 곱하면 이중으로 실린다.
    ``upper`` 를 읽는 자리를 ``predicted`` 로 바꾸는 것이다. ``max_price`` 는 안 건드린다.

    ⚠️ **아직 안 바꿨다.** 🔴 ~~관통으로 «안 0개» 가 얼마나 느는지 먼저 재고 넣는다~~
    — **쟀다. 그리고 안 넣기로 했다** (2026-09-10).

    .. code-block:: text

        실 DB 관통 46일   «안 0개» 0건 → 2건
        시연 구간         2026-01-26 배추 2안 → 0안
        컷 이동           커버 12일 배추 -23.4%

    🔴 **그리고 위 여유율 셋을 이 자리에 그냥 쓰면 안 된다** (2026-09-10 발견). ML 이 준
    분위는 **하루짜리**다 — ``LT2`` 는 「이틀 뒤 **그 하루**」의 초과분 분포다. 그런데 컷이
    막는 것은 **커버 창 전체**이고, 창 안 **어느 하루라도** 넘으면 그 안이 죽는다.

    .. code-block:: text

        같은 자료를 창 기준으로 다시 뽑으면 (q80)
            초과분 = max(actual[:D]) / max(predicted[:D]) - 1

                     D=2     D=5     D=12
            배추    14.0%   27.8%   50.2%    ← 하루짜리 LT 기준으로는 9.5~10.4%
            무      18.8%   24.7%   32.9%
            양파    10.9%   20.7%   28.4%

    ★ **배추 12일치는 다섯 배 차이다.** 둘 다 맞는 값이고 **쓰는 자리가 다르다.**

    🔴 **그리고 지금 컷이 이미 그 분포의 위쪽에 있다** (2026-09-10 저녁 실측). 바꾸는 것은
    *"느슨한 것을 조인다"* 가 아니라 **이미 맞는 것을 다른 방법으로 다시 맞추는 것**이다.

    .. code-block:: text

        max(upper[:D]) 의 실효 여유율 vs 같은 자료 창 기준 실측 분포 (D = 2 / 5 / 12)

            배추   47.0 / 50.4 / 56.1%   → 실측 **94 · 92 · 84분위**
            무     31.6 / 33.6 / 38.2%   →      **93 · 90 · 87분위**
            양파   22.3 / 23.2 / 23.9%   →      **92 · 82 · 70분위**

        🔴 9칸 중 **8칸**이 q80 위다. 컷이 실제로 뚫리는 비율은 2.5 ~ 10.5%

    🔴 **`D=12` 칸은 실 경로에서 안 쓰인다.** 위 *"커버 12일 배추 -23.4%"* 를 보고 멈추지
    않도록 적어 둔다 — 그 수는 맞지만 **닿는 안이 없다.**

    .. code-block:: text

        공격안(D=12) 229건    **전부 2025-12-31** — 번인 시드다. 2026 이후 **0건**
        2026 이후 판정 379건   **전부 uncertain** → 규칙 4 가 공격안을 막는다

    ★ **막는 것은 이 함수가 아니라 ``ci_width`` 임계다** (`#67`). 그것이 풀려 ``stable``
      이 나오기 시작하면 `D=12` 가 살아나고, **그날 이 칸을 다시 재야 한다** — 양파
      `D=12` 는 현행 여유율 `23.9%` 가 창 기준 q80 `28.4%` 보다 **작은 유일한 칸**이다.

    ⚠️ **그리고 지금 컷은 아무것도 안 죽인다** — 관통 46일 · 안 89개 중 `0` 개. 그래서
      바꾸는 것의 **유일한 관측 가능한 효과가 «안이 사라지는 것»** 이다 (안 5개 사망 ·
      «안 0개» 인 날 `0 → 2` · 그중 하나가 시연 구간).

    ⚠️ **mock 으로 컷 변경 영향을 재지 마라.**

    ★ 2026-09-10 에 실제로 그 일이 났다. 새 컷 산식 영향을 mock 5앵커로 재니 「+6~18%
    느슨해진다」였는데, 실 DB 관통으로 재니 「-6.8% 조여지고 시연 구간에 빈 화면이
    생긴다」였다. **원인은 밴드 폭이다.**

    .. code-block:: text

        mock   upper/predicted   1.030 ~ 1.060   ← 임계 0.08 을 만들려고 좁힌 값
        실제                     1.209 ~ 1.635   품목 × LT 중앙값 · 최대 2.464

        밴드 폭 (upper/predicted - 1) 으로 보면
            mock 5앵커 평균   0.036
            사본 평균         0.332    →  9.2배
            재측정 CSV 평균   0.420    → 11.7배

    🔴 **9~12배라 방향이 뒤집힌다.** 왜 mock 을 안 넓히는지는 ``mocks/README.md`` 의
    「실측 밴드」 절에 있다.

    ★ **지금은 위임한다 — 복제하지 않는다.** 같은 산식을 두 벌 적으면 한쪽만 고쳐지는
    날이 오고, 그때 갈리는 것이 판정이다 (규칙 8 · `#388` 이 그 병이다). 갈라야 할 때
    이 함수의 몸통만 바뀐다.
    """
    return compute_max_price(forecast, coverage_days)


def compute_margin(
    unit_price: float, contract_price: float | None
) -> tuple[bool | None, float | None]:
    """계약단가 파생 두 값. **함께 계산되거나 함께 null이다** (IO명세 §2 동기화 규칙).

    ``contract_price``가 ``None``이면 아직 못 받은 것이다 — 마진을 지어내지 않고 둘 다 null로
    내보낸다. 0.0으로 채우면 "마진 0%"라는 거짓이 되고, ``False``로 채우면 "계산했고 정상"과
    "계산 안 함"이 구분되지 않는다 (규칙 3의 float·bool 판).

    반면 ``0`` 이하는 **미수령이 아니라 잘못된 값**이다. 0원짜리 계약단가는 존재할 수 없고
    분모로도 쓸 수 없으므로 멈춘다 — 0과 NULL을 구분하는 규칙 3이 여기서도 그대로 적용된다.

    ⚠️ 매입단가가 계약단가를 넘으면(역마진) 실제 마진율은 음수지만 스키마가 ``ge=0``이라
    0.0으로 깎인다. 그 사실은 ``margin_warning=True``가 대신 전달한다.
    """
    if contract_price is None:
        return None, None
    require_positive(contract_price, "contract_price")
    return unit_price > contract_price, max(0.0, (contract_price - unit_price) / contract_price)


def _weighted_unit_price(sourcing: list[dict], total_qty_kg: int) -> float:
    amount = sum(line["qty_kg"] * line["grade_unit_price"] for line in sourcing)
    return amount / require_positive(total_qty_kg, "total_qty_kg")


def _quote_provenance(market_quotes: list[dict], as_of: str) -> dict[str, str]:
    """시세 근거의 **출처 등급과 설명**. 실 경락과 mock 을 갈라 적는다 (#70).

    🔴 전에는 어느 경로로 왔든 ``SIM_FIXED`` · ``"…실측 (mock)"`` 으로 고정돼 있었다.
      실 DB 시세를 쓰는 날에도 **출처를 거짓으로 표시**했다는 뜻이고, Critic·H1 이 읽는
      값이라 "데이터는 시뮬레이션 / 실행은 실제"라는 구분이 여기서 무너진다
      (Codex 교차검증 2026-08-31).

    등급이 ``MEASURED`` 가 아닌 이유: 그건 **마스터의 입력 등급 어휘**다
    (``app/master/inputs.py`` — MEASURED · DERIVED · MOCK · MISSING). 우리 rationale 의
    사다리는 ``OFFICIAL > VENDOR > SIM_FIXED > ASSUMED`` 네 단계뿐이고(§7.3), 가락 경락
    실적은 **공영도매시장의 공식 거래 기록**이라 그 사다리에서 ``OFFICIAL`` 이 맞는 자리다.
    등급이 실제로 중요한 이유도 있다 — ``grade_unit_price`` 는 ``check_max_price`` 와 사중
    일치 금액 축을 타는 **하드 제약 입력**이고, ``HARD_ALLOWED_GRADES`` 가 그 자격을 본다.

    **데이터가 말하게 한다.** DB 공급자만 각 줄에 규격을 얹으므로(``quotes._materialize``),
    그 표시가 곧 "실 경락에서 왔다"는 증거다. 주입 경로를 따로 묻지 않는다.
    """
    spec = observed_spec(market_quotes)
    if spec is None:
        return {
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": "가락시장 등급별 당일 실측 (mock)",
        }
    observed = observed_date(market_quotes) or as_of
    # 관측일이 as_of 와 다르면 **며칠 전 값인지**까지 적는다. "12-30 경락 실적"만 적으면
    # 읽는 사람이 오늘 값인지 아닌지를 스스로 계산해야 한다.
    gap = (date.fromisoformat(as_of) - date.fromisoformat(observed)).days
    aged = f" · as_of 기준 {gap}일 전" if gap else ""
    return {
        "evidence_grade": "OFFICIAL",
        "evidence_detail": (
            f"가락시장 등급별 경락 실적 · 관측일 {observed}{aged} · {spec} · 물량가중"
        ),
    }


def _inventory_claim(lots: list[dict] | None) -> tuple[str, str, str]:
    """재고 근거 문장 · ref_id · 등급. **NULL과 확정 0을 구분한다** (규칙 3).

    ``lots``가 ``None``이면 재고를 아직 못 받은 것이고, 빈 목록이면 "가용 재고 없음"이
    확정된 것이다. 둘을 똑같이 "가용 0kg"으로 적으면 미결이 사실처럼 나간다.
    """
    if lots is None:
        return "재고 정보 미수신 — 가용량 미결", "INV-미수신", "ASSUMED"
    if not lots:
        return "가용 재고 없음 (확정)", "INV-없음", "SIM_FIXED"
    lot = lots[0]
    # 키 이름은 **물류 어휘를 그대로** 쓴다 (#76 — 매입이 흡수하기로 합의).
    # 품목 필터는 어댑터가 이미 했다(`adapter.absorb_inventory`) — 여기 lots는 이 품목 것뿐이다.
    return (
        f"가용 {lot['available_qty_kg']:,}kg (로트 {lot['lot_id']})",
        f"INV-{lot['lot_id']}",
        "SIM_FIXED",
    )


def _rationale(
    state: PurchaseAgentState, draft: dict, constraints: dict, quote_ref: str
) -> list[dict]:
    """근거. **모든 항목에 ref_id가 필수**다 (규칙 4) — 실제 데이터에서 뽑는다.

    ``evidence_grade``는 정직하게 붙인다. mock은 팀이 시뮬 조건으로 선언한 값이라
    ``SIM_FIXED``이고, 확정주문에서 파생한 일평균 수요는 IO명세 §5대로 ``ASSUMED``다
    ("수요에서 파생된 것은 SIM_FIXED 자격을 잃는다" — 제약 독립성 요건).
    """
    as_of = state["date"]
    forecast = state["forecast"]
    day = constraints["situation"]["ci_judgment_day"]
    claim, ref_id, grade = _inventory_claim(state["inventory"].get("lots"))
    return [
        {
            "source": "예측",
            "claim": (
                f"D+{day} 예측 {compute_rise_rate_2w(forecast, day):+.1%}, "
                f"신뢰구간 폭 {compute_ci_width(forecast, day):.1%}"
            ),
            "ref_id": f"FC-{forecast['model_version']}-{as_of}",
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": (
                f"{forecast['model_version']} 경락가 예측 (지평 {forecast['horizon_days']}일)"
            ),
        },
        {
            "source": "시세관측",
            # ★ **관측일을 말한다.** 12-30 값을 "12-31 당일 경락가"라고 적으면 그것도
            #   거짓이다 — 우리는 아침에 돌아서 as_of 이전 최신 거래일을 읽는다.
            "claim": (
                f"가락 {observed_date(state['market_quotes']) or as_of} 경락가 "
                f"{state['market_quotes'][0]['price']:,}원/kg 등 "
                f"{len(state['market_quotes'])}개 등급"
            ),
            # ref_id 도 관측일 기준이다. as_of 로 두면 서로 다른 날의 제안이 같은 근거
            # 식별자를 갖게 되고, Critic 이 원문을 되짚을 좌표가 사라진다.
            "ref_id": quote_ref,
            **_quote_provenance(state["market_quotes"], as_of),
        },
        {
            "source": "주문",
            "claim": (
                f"확정주문 {state['confirmed_orders']['total_kg']:,}kg "
                f"→ 일평균 {draft['daily_demand_kg']:,.0f}kg × D={draft['coverage_days']}"
            ),
            "ref_id": f"SO-{as_of}",
            "evidence_grade": "ASSUMED",
            "evidence_detail": (
                "확정주문에서 파생한 일평균 — 관측값이 아니라 계산값이라 실측 등급이 아니다"
            ),
        },
        {
            "source": "재고",
            "claim": claim,
            "ref_id": ref_id,
            "evidence_grade": grade,
            "evidence_detail": "inventory_lots 스냅샷 (mock)",
        },
        # **상한을 실제로 정한 경로를 그대로 적는다** (Codex 교차검증 P1).
        # 재무 cap을 받는 날에도 "최저 현금의 60%"라고 쓰고 있었는데, 그 문장이
        # 주장하는 금액보다 실제 안이 클 수 있어 **근거가 산출물과 어긋났다.**
        # ⑥은 계산하지 않고 ③·⑦이 쓴 것과 같은 함수(``purchase_budget_krw``)를 부른다.
        _cash_rationale(state, constraints, as_of),
        # 창고는 ③(총량 클립)과 ⑦(도착일 컷)이 둘 다 읽는데 근거 문장에는 없었다.
        # **못 받은 날은 항목 자체를 안 만든다** (규칙 3) — 아래 함수가 ``None``을 낸다.
        *filter(
            None,
            [
                _warehouse_rationale(
                    state["inventory"],
                    as_of,
                    # 🔴 **승인 이력을 넘긴다** (`#312`). 예정분과 **대조가 맞을 때만**
                    #   *"어제 승인분이 언제 온다"* 로 넓어지고, 어긋나면 종전 문장
                    #   그대로다 — 아래 ``_commitment_cause`` 가 그 판정을 한다.
                    state.get("approved_commitments"),
                    state["item"],
                )
            ],
        ),
    ]


#: 예정분 뺄셈이 음수로 미끄러지는 것을 **오차로 볼 상한(kg)**. 이 값을 넘는 음수는
#: 오차가 아니라 세 값의 출처가 갈라진 것이다.
#:
#: ⚠️ **1kg 인 이유는 표시 단위다.** 문장이 kg 정수로 반올림되므로 1kg 미만의 어긋남은
#:   어차피 화면에 안 나타난다. 실제 오차는 실측에서 ``3.4e-13`` 규모였다.
_CAP_EPSILON_KG = 1.0


def _commitment_cause(
    commitments: list[dict] | None, item: str, reserved: float
) -> tuple[float, str] | None:
    """예정분이 **어제 승인분이라고 말해도 되는가.** 맞으면 ``(수량, 도착일)``.

    ★ **이 함수가 이 판의 핵심이다.** 관통 실측에서 예정분 3,587kg 과 그날 승인
      3,587kg 이 정확히 같았는데, **그 일치는 우리가 눈으로 맞춰본 것**이지 봉투가
      둘을 이어 준 것이 아니었다. 이제 봉투가 승인 이력을 싣는다(`#312`) —
      **코드가 대신 잰다.**

    🔴 **대조가 맞을 때만 인과를 쓴다.** 어긋나면 ``None`` 이고, 부르는 쪽은 종전
      문장(*"N kg 이 예정"*)을 그대로 낸다. 어긋나는 날은 예정분 안에 승인분 말고
      **다른 점유**가 섞였다는 뜻이라, 그때 *"어제 승인분"* 이라고 적으면 남의 물량을
      우리 것이라고 말하게 된다.

    셋을 차례로 본다::

        ① 우리 품목만        배추 안의 근거에 무 승인을 적을 수 없다
        ② 수량 합 == 예정분   ±_CAP_EPSILON_KG (표시 단위가 kg 정수라 그 아래는 안 보인다)
        ③ 도착일이 하나       여러 날이면 "언제" 를 한 날로 못 적는다

    ⚠️ **③이 여러 날일 때 인과를 통째로 접는 이유.** 수량 대조가 맞았으니 *"어제
      승인분"* 까지는 사실인데, 날짜를 빼고 적으면 문장이 *"온다"* 만 남아 **언제인지
      모른다는 사실이 사라진다.** 반쯤 아는 것을 다 아는 것처럼 적느니 종전 문장이
      정확하다.

    🔴 **실데이터에 다건·다날짜 사례가 없다** (2026-09-06 실측 — 승인이 만든 매입은
      ``PUR-THRU-20260105-BAECHU-D1-S1`` 하나뿐이다). 그래서 위 셋은 **실측이 아니라
      판단**이고, 분할 승인이 실제로 서는 날 다시 볼 자리다.
    """
    if not commitments:
        return None
    mine = [row for row in commitments if row.get("item") == item]
    if not mine:
        return None
    total = sum(float(row.get("total_qty_kg") or 0.0) for row in mine)
    if abs(total - reserved) > _CAP_EPSILON_KG:
        # 예정분에 승인분 아닌 것이 섞였다 — 인과를 안 쓴다.
        return None
    arrivals = {
        leg.get("arrival_date")
        for row in mine
        for leg in (row.get("arrival_schedule") or [])
        if leg.get("arrival_date")
    }
    if len(arrivals) != 1:
        return None
    return total, arrivals.pop()


def _warehouse_rationale(
    inventory: dict,
    as_of: str,
    commitments: list[dict] | None = None,
    item: str = "",
) -> dict | None:
    """창고 근거. **대조가 맞는 날에만 «왜 이렇게 됐나» 를 말한다.**

    ⚠️ *"예정"* 이라 쓰는 근거는 ``cap_by_date_policy = CONFIRMED_ONLY`` 하나다 —
      확정분만 반영한다는 뜻이라 그 수량이 **확정된 무엇**임은 안다.

    🟢 **이제 *"무엇이"* 를 물어볼 데가 생겼다** (`#310` → 마스터 `#312`). 봉투가
      ``approved_commitments`` 를 싣는다. 다만 **그 값이 곧 답은 아니다** — 예정분
      안에는 우리 승인분 말고 다른 점유가 섞일 수 있으므로, ``_commitment_cause`` 가
      수량과 도착일을 대조해 **맞는 날에만** 인과를 쓴다.

    ⚠️ 실측 (2026-01-06 · ``THRU-20260106-BAECHU-D2B``)::

        guaranteed 8,000 − cap_by_date 4,058.6 − used 354.4 = 3,587.0

      그날 승인 수량과 정확히 같았다. **그 일치는 우리가 눈으로 맞춰본 것이었고, 이제
      코드가 대신 잰다.**

    🔴 그래도 *"줄었다"* 는 못 쓴다. 매 실행이 독립이라 **전날 payload 를 안 들고
      있다** — 비교 대상이 없다. 우리가 말할 수 있는 것은 *"이만큼이 이 날 온다"* 이지
      *"이만큼 줄었다"* 가 아니다.

    🔴 **창 안 최솟값을 쓴다.** 실측(관통 사흘)에서는 18일이 전부 같은 값이라 첫날과
      구분되지 않았지만, 반출 예정이 생기면 날짜별로 갈린다. 그때 첫날을 쓰면 창
      뒤쪽이 더 좁은 날을 **넓다고 말하게 된다** — 상한이 아니라 하한을 말해야
      보수적이다. 도착일별 개별 판정은 ⑦ ``arrival_capacity`` 가 따로 한다.
    """
    cap_by_date = inventory.get("cap_by_date")
    guaranteed = inventory.get("guaranteed_capacity_kg")
    used = inventory.get("used_capacity_kg")
    if not cap_by_date or guaranteed is None or used is None:
        # 셋 중 하나라도 없으면 뺄셈이 성립하지 않는다. 0으로 채우면 "전량이 비어 있다"
        # 또는 "전량이 찼다"를 **사실처럼** 적게 된다 (규칙 3).
        return None
    free = min(cap_by_date.values())
    reserved = guaranteed - free - used
    if reserved < -_CAP_EPSILON_KG:
        # 세 값의 출처가 갈라진 날이다 — 지어내지 않고 안 적는다.
        return None
    # 🔴 **0 은 확정된 0 이다** (규칙 3). 승인 전인 날은 예정분이 정확히 0인데, 부동소수점
    #   뺄셈이 음수 쪽으로 미끄러진다 — 실측으로 ``8000.0 − 7645.6 − 354.4 =
    #   -3.4e-13`` 이 나왔고(2026-01-05 · ``THRU-20260105-BAECHU``) 그 날 항목이
    #   **통째로 사라졌다.** 오차만 걷고 값은 그대로 둔다.
    reserved = max(0.0, reserved)
    cause = _commitment_cause(commitments, item, reserved)
    if cause is None:
        # 종전 문장 — *"지금 이렇다"* 만 말한다. 승인 이력이 안 왔거나, 왔는데 예정분과
        # 어긋나거나, 도착일이 한 날로 안 모이는 날이 여기다.
        claim = (
            f"날짜별 입고 여유 {free:,.0f}kg "
            f"(전량 {guaranteed:,.0f}kg 중 {reserved:,.0f}kg 이 예정)"
        )
        detail = (
            "물류 cap_by_date 창의 최솟값. 예정분 = 보장용량 − 그 여유 − 현재 점유이고, "
            "cap_by_date_policy 가 CONFIRMED_ONLY 라 확정분만 잡혀 있다 — "
            "다만 무엇이 예정인지는 봉투에 오지 않는다"
        )
    else:
        # 🟢 대조가 맞은 날 — 예정분이 어제 승인분과 같고 도착일도 하나다.
        qty, arrival = cause
        claim = (
            f"날짜별 입고 여유 {free:,.0f}kg — "
            f"어제 승인분 {qty:,.0f}kg 이 {arrival} 에 옵니다"
        )
        detail = (
            "물류 cap_by_date 창의 최솟값. 예정분(보장용량 − 그 여유 − 현재 점유)이 "
            "마스터가 실은 어제 승인 약정의 수량과 같아, 그 승인이 예정분의 정체다 — "
            "도착일은 약정의 arrival_schedule 에서 왔다"
        )
    return {
        "source": "재고",
        "claim": claim,
        "ref_id": f"CAP-{as_of}",
        "evidence_grade": "SIM_FIXED",
        "evidence_detail": detail,
    }


def _cash_rationale(state: PurchaseAgentState, constraints: dict, as_of: str) -> dict:
    """현금 상한의 근거. **경로가 둘이라 문장도 둘이다** (B6).

    재무 cap을 받은 날은 그 값이 곧 상한이고 출처도 재무 회신이다. 못 받은 날만
    ``base_projected_cash_min × 비율``이고, 그때만 mock 표기가 맞다.
    """
    cap = state.get("finance_cap_amount_krw")
    budget = purchase_budget_krw(state, constraints)
    if cap is not None:
        return {
            "source": "현금",
            "claim": f"재무 매입 상한 {int(budget):,}원까지 매입 가능",
            "ref_id": f"CASH-{as_of}",
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": "finance_cap_amount_krw (재무 PRE_PURCHASE 회신)",
        }
    return {
        "source": "현금",
        "claim": (
            f"향후 최저 현금 {state['projected_cash_min']:,}원의 "
            f"{constraints['cash']['max_purchase_ratio']:.0%}까지 매입 가능"
        ),
        "ref_id": f"CASH-{as_of}",
        "evidence_grade": "SIM_FIXED",
        "evidence_detail": "base_projected_cash_min (정산 산출, mock)",
    }


def _context_rationale(context_docs: list[dict]) -> list[dict]:
    """② collect_context가 읽은 문서 근거. 안 읽었으면 아무것도 안 붙는다.

    현서님 합의 8/25 (IO명세 §0 P2): 문서를 근거로 쓰면 **``ref_id`` + 해당 구절을 출력에
    동봉**한다 — Critic은 DB 조회가 금지라 발췌 없이는 근거 대조가 성립하지 않는다.
    구절은 ②가 뜬 것을 그대로 싣는다 (``doc["excerpt"]``).

    ``ref_id``는 ``"DOC-{doc_id}"`` 고정이다 (IO명세 §1-⑥ 표기 규약). ⑦이 만드는
    ``context_docs_used``와 같은 변환을 써야 두 필드가 대조 가능하다 — ⑦의
    ``check_document_refs``가 그 대조를 한다.

    ``evidence_grade``는 **코퍼스가 선언한 것을 그대로 싣는다.** IO명세 §2 예시는
    ``OFFICIAL``이지만 그건 **실제 KREI 발간물** 기준이고, 우리 코퍼스는 형식만 빌린
    가상 문서다 — 등급은 문서의 격이 아니라 **실제 데이터 출처**를 따른다.

    🔴 **전에는 여기에 ``"SIM_FIXED"`` 가 리터럴로 박혀 있었다** (2026-09-09 · E3-5).
      *"실문서로 갈아끼우면 여기가 ``OFFICIAL`` 이 된다"* 는 설명이 이 docstring 에만
      있었고, 선언(``documents.json._전부_시뮬레이션``)은 **사람만 읽는 문장**이었다.
      선언과 코드가 같은 값이라 값 비교로는 «선언에서 읽는가» 를 증명할 수 없다
      (규칙 8). 이제 ``documents.json._evidence_grade`` 가 정하고, 없으면 로더가
      적재를 거부한다.

    ⚠️ ``doc["evidence_grade"]`` 를 ``get`` 으로 읽지 않는다. 기본값을 두면 손으로 만든
      문서 dict 가 조용히 통과해 **아무도 선언한 적 없는 등급**이 근거에 실린다.

    ``claim``이 주장 요약이 아닌 이유: 규칙은 본문을 요약할 수 없다. 문서 식별로 두고 실제
    주장은 ``evidence_detail``의 발췌가 **원문 그대로** 싣는다 — 규칙이 요약한 척하지 않는다.
    LLM이 붙으면 ``claim``이 요약으로 바뀌고 발췌는 그대로 남는다.
    """
    return [
        {
            "source": DOCUMENT_SOURCE,
            "claim": f"{doc['source']} {doc['doc_type']} — {doc['title']}",
            "ref_id": document_ref(doc["doc_id"]),
            "evidence_grade": doc["evidence_grade"],
            "evidence_detail": f"{doc['published_at']} 발행 · 발췌: \"{doc['excerpt']}\"",
        }
        for doc in context_docs
    ]


def _adjustment_risks(
    adjustments: list[dict] | None, constraints: dict, label: str, *, clipped: bool
) -> list[str]:
    """받았지만 **반영하지 않은** 조정안을 고지한다. 안 왔으면 아무 줄도 안 붙는다.

    🔴 **이 줄이 없으면 "값을 실어 주고 안 쓰는" 자리가 된다.** 마스터가 2회차에
      조정안을 실어 보내는데(``flow.py`` ``_purchase_input``) 우리가 조용히 안 쓰면,
      보내는 쪽은 **자기 제안이 반영된 줄 안다.** 반영 안 된 이유를 물을 기회조차 없다 —
      우리가 다른 파트에 지적했던 것과 같은 종류다 (#165 · #166).

    ``_context_risks`` 가 *"충분성을 아무도 묻지 않았다"* 를 고지하는 것과 같은 자리다:
    **하지 않은 일을 한 것처럼 보이게 두지 않는다.**

    🔴 **두 갈래다** (2026-09-09 · E3-6). 전에는 *"받았으나 반영하지 않았다"* 한 줄이
      전부였는데, **왜 못 쓰는지가 갈린다.**

      ``draft_plan.split_adjustments`` 가 항목·단위·대상 안으로 거른 것은 사유를
      같이 적는다 — 보내는 쪽이 **고쳐서 다시 보낼 수 있는** 종류이기 때문이다.
      *"반영 규칙이 없다"* 는 우리 사정이고, *"단위가 안 맞는다"* 는 그쪽 사정이다.
      한 문장으로 뭉치면 보내는 쪽이 무엇을 고쳐야 할지 모른다.

    ⚠️ 반영이 붙어도 **이 함수는 안 지운다** — 못 쓰는 조정안이 남으므로 고지할 대상이
      사라지지 않는다. (``#177`` 이 *"반영이 붙는 날 지운다"* 라고 적었는데, 그때는
      «못 쓰는 것» 이라는 갈래를 안 보고 있었다.)

    🔴 **«반영했다» 는 여기서 안 적는다** — ``_risks`` 가 이미
      *"조정안 제약으로 원안 N kg 에서 M kg 으로 축소"* 를 낸다. 두 곳이 같은 사실을
      적으면 한쪽만 고치는 날 화면이 두 말을 한다. 실제로 이 판을 붙이자마자 그 상태가
      났다 — 같은 안에 *"축소"* 와 *"반영하지 않았다"* 가 나란히 떴다.

      여기 남는 것은 그 문장이 **못 내는 사실 하나**다: 걸었는데 안 물린 경우.
      ``clipped`` 가 그것을 가른다.
    """
    if not adjustments:
        return []
    usable, unusable = split_adjustments(adjustments, constraints)
    notes = [
        f"조정안 {len(item)}건은 반영하지 않았다 — {reason}"
        for reason, item in _grouped_by_reason(unusable).items()
    ]
    mine = [item for item in usable if label in (item.get("scenario_labels") or ())]
    if mine and not clipped:
        # 반영했는데 **아무 일도 안 일어난** 경우다. 아무 줄도 안 붙이면 읽는 사람은
        # "이 안은 조정안과 무관하다" 로 읽는데, 사실은 **걸었고 안 물린 것**이다.
        notes.append(
            f"조정안 {len(mine)}건을 이 안에 반영했으나 원안이 이미 그 상한 아래라 "
            "수량이 그대로다"
        )
    return notes


def _grouped_by_reason(unusable: list[tuple[dict, str]]) -> dict[str, list[dict]]:
    """같은 사유끼리 묶는다. **사유가 하나면 줄도 하나다.**

    ⚠️ 조정안마다 한 줄씩 내면 같은 말이 여러 번 화면에 뜬다 — 실측상 한 회차에 같은
      항목이 여러 건 온다. 사유가 정보이지 건수가 정보가 아니다.
    """
    grouped: dict[str, list[dict]] = {}
    for item, reason in unusable:
        grouped.setdefault(reason, []).append(item)
    return grouped


def _document_age(context_docs: list[dict], as_of: str) -> str:
    """가장 최근 발행일과 **as_of 로부터 며칠 전인지**. 판정하지 않고 사실만 적는다.

    🔴 **관문이 우연히 막던 자리다** (#151-② · 2026-09-03). ``load_documents`` 가
      앵커일을 검증하던 동안에는 as_of 가 2026-09-11 을 넘을 수 없어 **문서가 오래될 수
      없었다.** 관문을 걷으면 발행일 필터만 남고, 그 필터는 *"as_of 이전"* 만 볼 뿐
      **얼마나 이전인지는 안 본다** — 9개월 지난 관측월보도 통과한다.

    ★ **임계를 두지 않는다.** 시세는 나이를 판정하지만(``quotes.provenance_problem``
      · ``max_calendar_days_behind``) 그건 거래일 분포라는 근거가 있어서다. 문서는
      *"며칠이면 낡았나"* 를 정할 근거가 우리에게 없다 — 없는 임계를 지어내면
      ``is_enough`` 가 충분성을 판정하지 않는 것과 같은 자리에서 규칙이 판단하는 척하게
      된다 (규칙 6). **사실만 적고 판단은 읽는 쪽에 남긴다.**

    ⚠️ 발행일이 없는 문서는 로더가 적재를 거부하므로(IO명세 §1-⑥) 여기 오지 않는다.
    """
    newest = max(doc["published_at"] for doc in context_docs)
    days = (date.fromisoformat(as_of) - date.fromisoformat(newest)).days
    return f"가장 최근 발간물은 {newest} 발행({days}일 전)"


def _forecast_risks(forecast: dict, coverage_days: int) -> list[str]:
    """``max_price`` 를 정한 날이 **장이 안 선 날의 복사값**이면 고지한다 (#213).

    🔴 **최댓값을 낸 행만 본다 — 창 전체를 세지 않는다.** 실측 3품목 × 7배치 × 3안
    (2026-09-04)::

          창에 복사값이 섞인 조합    48 / 63   ← 76%. 매일 붙으면 신호가 죽는다
          최댓값 행이 복사값         6 / 63    ← 9.5%. 이때만 상한이 복사값에서 나온다

      ``max_price`` 는 **최댓값 하나로 정해지는 재무 상한**이다 (규칙 5 · `#394`
      로 컷과 갈라졌다 — 컷은 ``cut_unit_price`` 다). 창 어딘가에
      복사값이 있다는 사실은 그 기준을 안 움직이므로, 세어 봐야 *"오늘도 그렇다"* 가
      매일 붙을 뿐이다. 걸린 6건은 전부 **신정(2026-01-01)과 토요일(2026-08-29)** 이고
      전부 보수안(D=2)이다 — 창이 이틀뿐이라 휴장일 하나가 창의 절반이 된다.

    ★ **판정에는 안 쓴다.** ML 이 ``is_filled`` 로 무엇을 하라는 지시를 준 적이 없고,
      복사값이라고 틀린 값인 것도 아니다 — *"그날 장이 안 섰다"* 는 사실일 뿐이다.
      컷하지 않고 사람이 감안하도록 문장만 남긴다.

    ⚠️ mock 예측에는 이 칸이 아예 없어 **4앵커에 아무 줄도 안 붙는다** (규칙 3 —
      없는 것을 ``false`` 로 채우지 않는다).
    """
    window = usable_forecast_window(forecast, coverage_days)
    top = max(window, key=lambda row: row["upper"])
    notes = _judgment_day_risks(forecast)
    if not top.get("is_filled"):
        return notes
    return notes + [
        (
            f"매입 상한({top['upper']}원)을 정한 {top['date']}은 장이 서지 않아 "
            "앞 장날 값을 그대로 쓴 날이다"
        )
    ]


def _judgment_day_risks(forecast: dict) -> list[str]:
    """상황 판정일 행이 **복사값**인 날의 한 줄. 아닌 날은 빈 목록이다.

    🔴 **전에는 안 붙였고, 그 근거가 무너졌다** (2026-09-07 · ``#384``).

    ①의 주석이 *"판정일이 주(週)의 배수라 복사값을 안 밟는다"* 를 근거로 이 값을
    판정에서도 고지에서도 뺐다. 그 근거는 **21조합 실측**이었는데, 504조합으로 넓히니
    **27건이 복사값**이었다::

        D+7   30 / 504
        D+14  27 / 504   ← 전부 target_dt 가 공휴일이다 (base_dt 는 정상 개장일)

    ★ **주기 가정이 틀린 것이 아니다.** ``base_dt + 14`` 는 여전히 같은 요일이라 주말을
      안 밟는다 — 다만 **공휴일은 요일과 무관하다.** 노동절·어린이날·제헌절처럼
      평일에 오는 날이 그대로 걸린다.

    ⚠️ **컷하지 않는다** (``#384`` ㄴ · 마스터 ``#230`` 과 같은 판단 — *"컷을 고지로"*).
      복사값이라고 틀린 값이 아니다. 다만 그날 판정은 **그날의 불확실성이 아니라
      전 장날의 불확실성**을 잰 것이라, 그 사실이 사람에게 보여야 한다.

    ⚠️ mock 예측에는 이 칸이 없어 앵커에는 아무 줄도 안 붙는다 (규칙 3 — 없는 것을
      ``false`` 로 채우지 않는다).
    """
    day = load_constraints()["situation"]["ci_judgment_day"]
    row = judgment_row(forecast, day)
    if not row.get("is_filled"):
        return []
    return [
        (
            f"상황 판정일({row['date']})은 장이 서지 않아 앞 장날 값을 그대로 쓴 날이다 "
            "— 그날이 아니라 앞 장날의 예측 구간으로 상황을 갈랐다"
        )
    ]


#: 읽으려다 못 읽은 날의 고지. **"발간물이 0건"과 같은 문장을 쓰지 않는다.**
#:
#: 🔴 **규칙 3(``0`` ≠ ``NULL``)이 문면에서 깨져 있던 자리다** (2026-09-09 · E3-5).
#:   ``collect_context`` 는 두 상태를 이미 갈라 뒀다 — *"그날 그 유형이 없다"* 는 빈
#:   ``context_docs``, *"읽으려다 못 읽었다"* 는 ``context_unavailable``. 그런데 ⑥은
#:   **둘 다 «참조 가능한 발간물 0건»으로 적고 있었다.** 운영 기록 158건이 그 문장이고,
#:   읽는 사람에게는 *"그날 그 문서가 세상에 없었다"* 로 읽힌다.
#:
#: ⚠️ **못 읽은 사유를 여기에 옮기지 않는다.** ``context_unavailable`` 문자열은 내부
#:   함수 이름이 든 개발자용 문장이다. 이 필드를 읽는 쪽은 H1 승인 화면과 Critic이라
#:   (계약서 §0), **있었다는 사실만** 쓴다.
_UNREAD_ALL = "문서 {kinds}종을 요청했으나 읽지 못했다 — 그날 발간물이 0건이었다는 뜻이 아니다"
_UNREAD_REST = "요청한 유형 중 읽지 못한 것이 남았다 — 그 유형의 발간물이 0건이었다는 뜻이 아니다"


def _context_risks(
    loop_count: int,
    context_docs: list[dict],
    as_of: str,
    unavailable: str | None = None,
) -> list[str]:
    """문서 수집에서 나온 유의사항. **②가 안 돈 날은 아무 줄도 안 붙는다.**

    판정 기준이 ``situation`` 문자열이 아니라 **``context_loop_count``**인 이유: 알고 싶은
    건 "그날이 uncertain인가"가 아니라 "문서를 실제로 찾아봤는가"다. situation으로 물으면
    ②의 실행 여부를 그래프 배선을 통해 **간접 추론**하게 되고, ``Situation``에 값이 늘거나
    분기가 바뀌면 조용히 어긋난다 (Codex 교차검증 지적). 루프 수는 그 사실을 직접 들고 있다.

    ②가 돌았는데 고지가 없으면 소비자는 "검토를 거친 근거"로 읽는다. 실제로는 우선순위
    목록을 순서대로 소진했을 뿐이고, **"이만하면 충분한가"를 아무도 묻지 않았다.**
    E3-3에서 일괄 fallback을 고지하기로 한 것과 같은 라벨/행동 불일치다.

    ⚠️ **"고를 여지가 없어서"라고 적지 않는다.** 지금은 참이지만(``loop_max`` == 유형 수)
    유형이 늘면 거짓이 된다 — 근거는 ``collect_context.select_doc_types`` docstring 과
    ``test_ordering_is_moot_while_the_list_fits_the_loop_budget`` 가 들고 있고, 이 문장은
    **한 일과 안 한 일**만 적는다. 그래야 전제가 바뀌어도 문장이 거짓이 되지 않는다.

    ★ 같은 사실을 ``adapter.build_evidences`` 도 ``context_docs_used`` 근거에 적는다.
    두 자리가 갈리면 화면과 봉투가 다른 말을 하므로, 문면을 고칠 때 **둘을 같이** 본다.

    🔴 **전에 ``adapter._evidence`` 로 적었다 — 그런 이름은 없다** (2026-09-09 정정).
      `#480` 이 새로 넣은 줄인데 **그때 이름을 안 쟀다.** 자리는 grep 으로 맞게 찾아
      놓고 **함수 이름만 기억으로** 적었고, 반대쪽 주석이 ``_context_risks`` 로 정확해서
      한쪽만 틀린 것도 안 보였다. `#454` 가 걷어낸 유형이 하루 만에 다시 났다 —
      **가리키는 이름은 적기 전에 정의를 확인한다** (줄 번호는 밀리므로 안 적는다).

    문구에 내부 단계 이름을 쓰지 않고, **하지 않은 일을 한 것처럼 적지도 않는다** — 발췌는
    문장 경계 파서가 아니라 서두 잘라내기라 "첫 문장"이라고 주장하지 않는다. 이 필드를
    읽는 쪽은 코드가 아니라 H1 승인 화면과 Critic이다 (계약서 §0).

    🔴 **``unavailable`` 이 넷째 상태를 연다** (2026-09-09 · E3-5). 전에는 셋이었다 —
      안 찾아봄 / 찾았는데 없음 / 찾아서 있음. **"찾다가 못 읽음"이 둘째와 같은 문장을
      쓰고 있었다.** 근거·경위는 ``_UNREAD_ALL`` 주석에 있다.

    ⚠️ ⑦ ``self_check._CONTEXT_NOTE`` 도 못 읽은 사실을 적는다. **층이 다르다** —
      여기는 *"문서 수집이 무엇을 했나"*, 저기는 *"그래서 이 안이 어떻게 나왔나"* 다.
      둘이 한 안의 ``risks`` 에 나란히 뜨므로 **같은 말을 두 번 쓰지 않는다.**
    """
    if loop_count <= 0:
        return []  # ② 미실행 — "찾아보지 않았다"는 고지할 유의사항이 아니라 경로의 사실이다
    if not context_docs:
        if unavailable:
            return [_UNREAD_ALL.format(kinds=loop_count)]
        return [
            (
                f"문서 {loop_count}종을 찾았으나 참조 가능한 발간물 0건 — "
                "문서 근거 없이 구성된 안이다"
            )
        ]
    notes = [
        (
            f"문서 {len(context_docs)}건 참조 — 정해진 우선순위 순서대로 읽었고 "
            "어느 문서가 더 맞는지도, 이만하면 충분한지도 판정하지 않는다. "
            "발췌는 관련 구절 선별 없이 각 문서 서두에서 기계적으로 뜬 것이다. "
            f"{_document_age(context_docs, as_of)}"
        )
    ]
    if unavailable:
        # 🔴 **운영에서는 안 밟힌다** — 지금 ``get_context_docs`` 는 첫 회차부터 막혀
        #   읽은 문서가 0건이다. 실 소스가 붙어 일부만 읽히는 날 열리는 가지이고,
        #   그때 이 줄이 없으면 "N건 참조" 가 **다 읽은 것처럼** 읽힌다.
        notes.append(_UNREAD_REST)
    return notes


def _sourcing_decision(ratios: list[dict]) -> dict:
    """⑤가 첫 줄에 실어 보낸 등급 배분 판단 근거.

    State 필드를 늘리지 않으려고 비율 목록에 얹었다 (§3 "필드는 §3 State 정의 그대로").
    ``materialize_sourcing``이 계약 4필드만 투영하므로 이 키는 **출력에 새지 않는다** —
    내부 중간산출과 출력 계약이 같은 리스트를 공유하되 경계에서 잘린다.
    """
    return ratios[0].get("decision", {}) if ratios else {}


def _sourcing_rationale(decision: dict, quote_ref: str) -> list[dict]:
    """중품을 태운 날의 근거 1건. 안 태웠으면 붙이지 않는다 — 없는 판단을 적지 않는다.

    ``evidence_grade``가 ASSUMED인 이유: 평시 기준선은 선언 상수(SIM_FIXED)지만 중품
    소진 한계일이 **재고 로트에서 추론한 값**이라, 둘을 합친 판단은 가장 약한 등급을 따른다
    (IO명세 §5 — 파생값은 원천의 등급을 물려받지 못한다).
    """
    if not decision.get("ratio"):
        # 중품을 안 태운 날. **판단이 있었다면 그 사실은 남긴다** — LLM이 "확대됐지만
        # 신선도가 빡빡해 중품을 쓰지 않는다"를 고른 것과, 평시라 애초에 후보가 없던 것은
        # 다른 사실이다. 조기 반환하면 둘이 구분되지 않는다 (Codex 교차검증 P2).
        return _mix_choice_rationale(decision, quote_ref)
    widening = decision["spread"] / decision["baseline"] - 1
    return [
        {
            "source": "시세관측",
            "claim": (
                f"{decision['top_grade']}-{decision['mid_grade']} 스프레드 "
                f"{decision['spread']:.1%} — 평시 {decision['baseline']:.1%} 대비 "
                f"{widening:+.0%} 확대, {decision['mid_grade']} {decision['ratio']:.0%} 배정"
            ),
            "ref_id": quote_ref,
            "evidence_grade": "ASSUMED",
            "evidence_detail": (
                f"소진 한계 {decision['shelf_days']:.0f}일 = 운영 보관한계 "
                f"{decision['top_shelf_days']}일 × {decision['shelf_ratio']} · "
                f"스코어 {decision['score']:+.3f}"
            ),
        },
        *_mix_choice_rationale(decision, quote_ref),
    ]


def _mix_choice_rationale(decision: dict, quote_ref: str) -> list[dict]:
    """LLM이 조합을 고른 날의 근거 1건 (E3-2). **고르지 않았으면 붙이지 않는다.**

    ``evidence_grade``가 ASSUMED인 이유는 스프레드 근거와 같다 — 소진 한계일이 재고
    로트에서 추론한 값이라 그 위에 얹힌 판단은 가장 약한 등급을 따른다. LLM이 개입했다는
    사실 자체가 등급을 낮추는 건 아니다: **비율은 규칙이 만든 후보의 값 그대로**이고
    LLM은 그중 하나를 고르기만 했다.

    ``ref_id``에 모델명을 싣는다 — 어느 판단자가 골랐는지 되짚을 수 있어야 한다.
    """
    mix = decision.get("mix")
    if mix is None or not mix.applied:
        return []
    return [
        {
            "source": "시세관측",
            # 🔴 ``candidate_id`` 는 계약 값이라 그대로 두고, **사람이 읽는 설명**을 함께
            #   적는다. 화면·Critic 이 이 문장을 읽는데 ``MID_CAPPED`` 만으로는 안 읽힌다.
            #   설명은 ``_CANDIDATE_LABELS`` 단일 소스에서 가져온다 (규칙 7).
            "claim": (
                f"등급 조합 {candidate_summary(mix.candidate_id)}"
                f"({mix.candidate_id}) 선택 — {mix.reason}"
            ),
            "ref_id": quote_ref,
            "evidence_grade": "ASSUMED",
            "evidence_detail": (
                f"규칙이 만든 후보 중 선택 (판단 {mix.llm_model}) — "
                "비율·수량은 규칙 산출값 그대로"
            ),
        }
    ]


def _grade_fallback_risks(decision: dict) -> list[str]:
    """선언한 기준등급이 당일 시세에 없어 **다른 등급으로 배정한 날**의 한 줄.

    걸리지 않은 날은 빈 목록이라 문장이 안 나간다 — ⑤가 대체했을 때만 키를 세운다
    (``allocate_sourcing.reference_grade_fallback``).

    🔴 **왜 필요한가.** ``sourcing_plan`` 에는 배정한 등급 이름만 실린다. 그게 선언한
      기준등급인지 대체값인지 화면에서 구분이 안 되고, 실데이터에서는 배추·양파가
      **늘** 대체 경로다 (`#69` · 2026-09-07 실측). 읽는 사람이 *"기준등급이 뭐냐"* 를
      물었을 때 산출물이 스스로 답해야 한다.

    ⚠️ **원인이 아니라 사실만 적는다.** *"규격 안에 상 등급이 없다"* 는 ML 소유의
      설명이고 품목마다 다르다 — 우리가 아는 것은 *"오늘 시세에 그 등급이 없었다"* 뿐이다.
    """
    fallback = decision.get("reference_grade_fallback")
    if not fallback:
        return []
    return [
        (
            f"기준등급 '{fallback['declared']}'이 당일 시세에 없어 "
            f"'{fallback['used']}'({fallback['used_price']:,}원/kg)으로 배정했다"
        )
    ]


def _sourcing_risks(sourcing: list[dict], decision: dict) -> list[str]:
    """등급 배분에서 나온 유의사항. **미결로 건너뛴 검사도 여기 싣는다** (규칙 3).

    §4-⑦ 예시 출력의 risks("중품 1,500kg은 잔여신선도 6일 내 소진 필요 — 확정주문
    일정상 충족")를 재현한다.

    🔴 **등급 대체 고지는 세 갈래 전부에 붙는다.** 어느 한 갈래에만 두면 *"배분이 막힌
      날에는 고지를 안 받는"* 구조가 된다 — ⑦ ``check_arrival_capacity`` 가 회차 수와
      무관하게 같은 코드를 태우는 것과 같은 이유다.
    """
    fallback_notes = _grade_fallback_risks(decision)
    if decision.get("blocked_by"):
        # 배정한 등급을 **이름으로** 적는다. 기준등급 시세가 없으면 ⑤가 다른 등급으로
        # 대체하므로, "기준등급으로 배정했다"고 쓰면 형식만 맞고 내용이 거짓인 근거가 된다.
        grade = decision.get("base_grade", "?")
        return fallback_notes + [
            f"등급 배분 보류 — {decision['blocked_by']}. 전량 {grade} 단일 등급으로 배정했다"
        ]
    if not decision.get("ratio"):
        # 중품 미사용. 판단 미적용 고지는 여기서도 살아야 한다 — 판단자가 있었는지
        # 없었는지는 배분 결과와 별개의 사실이다 (rationale 쪽과 같은 이유).
        return fallback_notes + _mix_choice_risks(decision)
    mid_kg = sum(line["qty_kg"] for line in sourcing if line["grade"] == decision["mid_grade"])
    notes = [
        (
            f"{decision['mid_grade']}품 {mid_kg:,}kg은 소진 한계 {decision['shelf_days']:.0f}일 내 "
            f"납품분 {decision['near_qty_kg']:,}kg 안에서만 소화 가능"
        )
    ]
    if decision.get("arrival_basis_assumed"):
        # **"충족"이라고 쓰지 않는다.** 창의 시작점이 입고일인데 N4가 NULL이라 as_of로
        # 근사했다 — 근사 위에서 낸 결론을 검증된 것처럼 적으면 규칙 3이 형식만 남는다.
        notes.append(
            "위 매칭은 매입일 기준 근사다 — 실제 창은 입고일(매입일 + 입고 소요일)부터인데 "
            "물류 입고 소요일이 미확정이라 계산하지 않았다. 미확정을 0으로 채우면 "
            "'오늘 사서 오늘 도착'이 사실이 되므로 채우지 않는다. "
            "입고 소요일이 확정되면 소화 가능량이 달라질 수 있다"
        )
    if decision.get("shelf_ratio_fallback"):
        # 물류가 medium_grade_factor 를 보내지 않아 설계 기본값으로 계산했다. 값이 같아
        # 결과가 안 바뀌더라도 **무엇을 근거로 셈했는지**는 달라진다 (규칙 3).
        notes.append(
            "중품 소진 계수는 물류 중품 보관계수를 받지 못해 설계 기본값으로 "
            "계산했다 — 물류 값이 다르면 중품 비중이 달라진다"
        )
    notes.extend(_mix_choice_risks(decision))
    return fallback_notes + notes


def _mix_choice_risks(decision: dict) -> list[str]:
    """등급 조합 판단이 **적용되지 않은** 날의 고지 (E3-2).

    성공하면 아무 줄도 안 붙는다 — 판단이 적용된 건 위험이 아니라 정상이고, 그 사실은
    rationale이 이미 싣는다. 여기 적는 건 **라벨과 행동이 어긋나는 상태**뿐이다:
    "판단자가 골랐다"고 읽힐 자리에서 실제로는 규칙 기본안이 나갔다는 것.
    E3-3의 일괄 fallback 고지, E3-4의 충분성 미판정 고지와 같은 자리다.

    문구에 내부 상태 코드(FALLBACK 등)를 쓰지 않는다 — 이 필드를 읽는 쪽은 코드가 아니라
    H1 승인 화면과 Critic이다 (계약서 §0).
    """
    mix = decision.get("mix")
    if mix is None or mix.applied:
        return []
    cause = "판단자 응답 실패" if mix.llm_fallback_used else "판단자 미사용"
    return [
        (
            f"등급 조합 판단 미적용({cause}) — 규칙 기본안으로 배분했다. "
            "비율·수량은 규칙 산출값이라 결과는 판단 없이도 유효하다"
        )
    ]


def _ratio_outcome(rounds: list[dict]) -> str:
    """비율은 균등이어도 **수량은 다를 수 있다** — ⑥이 날짜별 창고 여유로 옮기기 때문이다.

    이 한 줄이 없으면 화면에 [30, 70]이 떠 있는 옆에서 근거가 "균등"이라고 말한다.
    같은 안에서 근거와 수량이 서로를 부정하는 상태다.
    """
    quantities = [line["qty_kg"] for line in rounds]
    if len(set(quantities)) <= 1:
        return "회차 물량도 균등하다."
    moved = " · ".join(f"{line['seq']}회 {line['qty_kg']:,}kg" for line in rounds)
    return f"다만 회차 물량은 날짜별 창고 여유에 맞춰 옮겨졌다 — {moved}."


def _split_rationale(decision: dict, rounds: list[dict], forecast: dict, as_of: str) -> list[dict]:
    """분할한 안의 근거. **선 트리거마다 한 건**이고 출처·ref_id·등급이 각각 다르다.

    한 건으로 뭉쳐 전부 "예측(FC-…)·SIM_FIXED"로 적었더니, 수량 단독 진입일 때
    **예측이 근거가 아닌 주장에 예측 ref_id가 붙었다** (Codex 교차검증 P2).
    총량은 확정주문에서 파생해 하드 제약으로 클립한 값이라 출처가 주문이고 등급도 낮다
    (IO명세 §5 — "수요에서 파생된 것은 SIM_FIXED 자격을 잃는다").

    회차가 하나로 되돌아갔으면 붙이지 않는다 — 일어나지 않은 판단을 적지 않는다.
    """
    if len(rounds) < 2:
        return []
    items = []
    if decision.get("by_volume"):
        items.append(
            {
                # 🔴 **출처가 「주문」에서 「재고」로 옮겼다** (`#308`). 판정을 가른 수가
                #   바뀌었기 때문이다 — 전에는 우리 선언(고정 임계)이 기준이었고 지금은
                #   물류가 낸 그날 여유가 기준이다. 옛 ref_id(``SO-``)를 그대로 두면
                #   **결정한 수를 못 찾는 근거**가 된다 (Codex P2 와 같은 자리).
                "source": "재고",
                "claim": (
                    f"안 총량 {decision['largest_total_kg']:,}kg ≥ "
                    f"{decision['arrival_date']} 도착 여유 {decision['cap_kg']:,.0f}kg → "
                    f"{len(rounds)}회 분할"
                ),
                "ref_id": f"CAP-{as_of}",
                # ⚠️ 두 수의 등급이 다르면 **낮은 쪽**이다. 여유는 물류 정본(SIM_FIXED)이지만
                #   총량은 확정주문에서 파생해 클립한 값이라 자격이 없다 (IO명세 §5).
                "evidence_grade": "ASSUMED",
                "evidence_detail": (
                    "기준값은 물류 cap_by_date 의 도착일 칸이고, 비교 대상인 안별 총량은 "
                    "확정주문에서 파생해 하드 제약으로 클립한 값이다 — "
                    "수요 파생값이라 SIM_FIXED 자격 없음"
                ),
            }
        )
    if decision.get("by_trend"):
        items.append(
            {
                "source": "예측",
                "claim": f"판정일까지 지속 상승 궤적 → {len(rounds)}회 분할로 로트 나이 분산",
                "ref_id": f"FC-{forecast['model_version']}-{as_of}",
                "evidence_grade": "SIM_FIXED",
                "evidence_detail": (
                    "상승장 분할은 평균단가에 불리하고 로트 나이 분산에 유리하다 — "
                    "그 절충 판단은 아직 사람·모델 몫이라 회차 비율은 균등으로 두었다. "
                    f"{_ratio_outcome(rounds)}"
                ),
            }
        )
    return items


def _entry_miss_reason(decision: dict) -> str:
    """④가 진입하지 않은 이유. 두 트리거 중 못 선 것을 그대로 적는다."""
    misses = []
    if not decision.get("by_volume"):
        # 🔴 **「못 봤다」와 「안 걸렸다」를 가른다** (`#308` · 규칙 3). 여유를 못 받은 날을
        #   «미달» 로 적으면 판정하지 않은 것이 판정한 것으로 읽힌다.
        if decision.get("cap_unknown_reason"):
            misses.append("도착일 창고 여유를 못 봐 총량 조건을 판정하지 않았다")
        else:
            misses.append(
                f"최대안 {decision.get('largest_total_kg', 0):,}kg < "
                f"{decision.get('arrival_date')} 도착 여유 {decision.get('cap_kg') or 0:,.0f}kg"
            )
    if not decision.get("by_trend"):
        misses.append("지속 상승 궤적 아님")
    return " · ".join(misses)


def payment_dates(rounds: list[dict], payment_days: int | None) -> list[str]:
    """회차별 **지급일** = 회차 date + N5. N5가 없으면 빈 목록이다.

    N5 = 7 확정 (8/27 재무 · **calendar day · 영업일 보정 없음**). 분할이면 회차마다
    각각 계산한다 — 총액을 한 날에 몰아 지급하는 게 아니라 회차별로 나가기 때문이다.

    ⚠️ **영업일 보정을 하지 않는다.** 재무가 calendar day로 확정했으므로 여기서 주말을
    밀면 재무 계산과 어긋난다. 보정이 필요해지면 재무 쪽에서 정한다.
    """
    if payment_days is None:
        return []  # 규칙 3 — 미결값으로는 계산하지 않는다
    return [
        (date.fromisoformat(item["date"]) + timedelta(days=payment_days)).isoformat()
        for item in rounds
    ]


def _round_amounts(rounds: list[dict], sourcing: list[dict]) -> list[int]:
    """회차별 금액 = **그 회차에 배분된 등급 구성**의 실제 금액.

    등급별 kg를 회차 수량 비율대로 나누고 등급 단가로 곱한다. 전체 가중단가를 쓰면
    합은 맞지만 **어느 정수 kg 구성으로도 재현되지 않는 값**이 나와, 재무가
    ``sourcing_plan``으로 검산할 때 어긋난다.

    **각 등급의 마지막 회차가 그 등급의 잔량을 흡수한다** — ``split_quantities``가 수량에
    쓰는 것과 같은 장치다. 그래서 ``Σ amount_krw == Σ(등급 kg × 등급 단가) ==
    total_amount_krw``가 항등식으로 성립한다.

    ⚠️ **회차별 등급 내역을 출력에 싣지는 않는다** — 재무가 ``by_grade``를 요구하지
    않았고(회신 §2) 등급은 ``sourcing_plan``이 정본이다. 여기서는 **금액을 재현 가능하게
    만들기 위해서만** 배분한다.
    """
    total_qty_kg = require_positive(sum(item["qty_kg"] for item in rounds), "total_qty_kg")
    amounts = [0] * len(rounds)
    for line in sourcing:
        remaining = line["qty_kg"]
        for index, item in enumerate(rounds):
            share = (
                remaining
                if index == len(rounds) - 1
                else round(line["qty_kg"] * item["qty_kg"] / total_qty_kg)
            )
            share = min(share, remaining)
            remaining -= share
            amounts[index] += share * line["grade_unit_price"]
    return amounts


def with_round_amounts(rounds: list[dict], sourcing: list[dict]) -> list[dict]:
    """회차 목록에 ``amount_krw`` 를 얹는다. **여기가 회차 금액의 유일한 생산지다.**

    🔴 **한 번만 계산한다.** 전에는 ``build_payment_schedule`` 이 ``_round_amounts`` 를
      따로 불렀다 — 회차 금액이 두 곳에서 나면 한쪽 계산만 바뀌는 날이 오고, 그때
      ``split_plan`` 과 ``payment_schedule`` 이 다른 금액을 들고 나간다. 이 저장소가
      도착일(#141)·N4(#58)에서 두 번 겪은 자리다.

      이제 ``build_payment_schedule`` 은 여기서 얹은 값을 **읽는다.**

    ⚠️ **일괄 안(1회차)에도 ``amount_krw`` 를 싣는다.**

      근거: 마스터 ``commitment.py:_legs`` 가 ``amount_filled == len(amounts)`` 일 때만
      ``carry_amounts`` 를 켠다 — 부분 공급을 거부한다. 일괄에 안 실으면 그 안은 금액 변
      검증이 통째로 건너뛰어진다.

      ⚠️ 1회차면 ``amount_krw == total_amount_krw`` 로 같은 값이 두 번 나간다.
      중복이지만 *"전 회차에 있다"* 가 계약이므로 그쪽을 택했다.

      🟢 **``#296`` 이 우리 방식대로 날랐다 (2026-09-05). 답이 왔다.**
    """
    return [
        {**item, "amount_krw": amount}
        for item, amount in zip(rounds, _round_amounts(rounds, sourcing), strict=True)
    ]


def build_payment_schedule(
    rounds: list[dict],
    max_price: int,
    payment_days: int | None,
) -> list[dict] | None:
    """회차별 지급 계획 (재무 확정 7필드 · 2026-08-27 회신).

    재무가 ``SCENARIO_VALIDATION``에서 회차별 Cashflow를 검증할 때 쓴다. 두 금액이
    각각 다른 검증에 들어간다 (회신 §1):

    - ``amount_krw``     → **BASE Cashflow**   (오늘 단가 기준 예상 지급액)
    - ``amount_max_krw`` → **STRESS Cashflow** (회차 수량 × ``max_price``)

    **``None``을 돌려주는 경우가 둘이고 뜻이 다르다** — 호출부가 그때 키를 만들지 않는다:

    1. ``payment_days``(N5)가 미결 — 지급일을 **계산할 수 없다**. 0으로 채우면
       "D+0 즉시지급"이 되어 운전자금이 과대 계상된다 (규칙 3). 그 사실은
       ``deferred_checks``가 싣는다.
    2. 회차가 하나 — **일괄 안이라 실을 것이 없다.** 지급일 하나는 ``split_plan``에서
       바로 파생되므로 같은 값을 두 벌 내보내지 않는다 (제안 §3.2 항등식 5).

    ⚠️ **``by_grade``를 넣지 않는다.** 등급별 수량·단가는 ``sourcing_plan``이 정본이고,
    여기는 회차별 Cashflow 정보만 있으면 충분하다 (회신 §2).

    ⚠️ **금액은 ``rounds`` 에서 읽는다 — 여기서 계산하지 않는다.**
    ``with_round_amounts`` 가 등급별 kg를 회차에 배분해 실제 단가로 곱한 값을 이미
    얹어 두었다. 전체 가중단가를 쓰면 **어떤 정수 kg 등급 구성으로도 재현되지 않는
    금액**이 나와 재무 검산이 어긋난다 (Codex 교차검증) — 그 계산은 한 곳에만 있다.

    ⚠️ **``payment_days``가 음수면 만들지 않는다.** 지급일이 매입일보다 앞서는 것은
    N5의 뜻(매입 후 며칠 뒤 지급)과 모순이고, 그대로 두면 ⑦도 같은 음수로 재계산해
    정상 판정한다 (Codex 교차검증).
    """
    if payment_days is None or payment_days < 0 or len(rounds) <= 1:
        return None

    schedule = []
    for item in rounds:
        purchase_date = date.fromisoformat(item["date"])
        schedule.append(
            {
                "seq": item["seq"],
                "purchase_date": item["date"],
                "payment_date": (purchase_date + timedelta(days=payment_days)).isoformat(),
                "qty_kg": item["qty_kg"],
                # ★ **재계산하지 않고 읽는다** — ``with_round_amounts`` 가 이미 얹었다.
                #   같은 사실을 두 곳에서 계산하면 어긋나는 날이 온다.
                "amount_krw": item["amount_krw"],
                "amount_max_krw": item["qty_kg"] * max_price,
                # ``amount_krw``를 **어떻게 추정했는가**다. 재무의 BASE/STRESS는 두 금액을
                # 각각 어느 Cashflow에 넣는지의 소비 프레이밍이라 축이 다르다.
                "basis": "as_of_unit_price",
            }
        )
    return schedule


def _payment_schedule_field(
    rounds: list[dict], max_price: int, payment_days: int | None
) -> dict:
    """실을 것이 있을 때만 키를 만든다 — ``None``을 담으면 "빈 계획"으로 읽힌다.

    ``rounds`` 는 ``with_round_amounts`` 를 지난 것이어야 한다 (``amount_krw`` 를 읽는다).
    """
    schedule = build_payment_schedule(rounds, max_price, payment_days)
    return {"payment_schedule": schedule} if schedule else {}


def _payment_risks(
    rounds: list[dict], payment_days: int | None, critical_dates: list[str] | None
) -> list[str]:
    """지급일이 **재무의 지급 집중일과 겹치는가** — 겹치면 경고만 남긴다.

    🔴 **여기가 도메인 경계다.** 우리 소관은 *"우리 회차 지급일이 집중일과 겹친다"*는
    사실을 알리는 데까지다. **날짜별 잔액을 재계산하지 않는다** — 그건 재무의
    ``SCENARIO_VALIDATION`` 소관이고(8/27 매입 회신), 우리가 하면 두 가지가 깨진다:

    1. **도메인 침범** — 재무 payload에 전체 Cashflow 배열이 오지 않는다. 우리가 가진
       것은 ``base_projected_cash_min``(최저점 하나)과 집중일 목록뿐이라, 날짜별 잔액을
       만들려면 **없는 데이터를 추정**해야 한다.
    2. **이중 계산** — 재무가 같은 검사를 제대로 하는데 우리가 근사로 한 번 더 하면,
       두 판정이 갈렸을 때 어느 쪽이 정본인지 불분명해진다.

    그래서 반환은 경고 문자열이고 **컷하지 않는다.** 컷 여부는 재무가 정한다.
    """
    if not critical_dates:
        return []
    overlap = sorted(set(payment_dates(rounds, payment_days)) & set(critical_dates))
    if not overlap:
        return []
    return [
        (
            f"회차 지급일 {', '.join(overlap)}이 재무의 지급 집중일과 겹친다 — "
            "해당 일자 현금 여력을 재무 검증에서 확인 필요"
        )
    ]


def _split_risks(
    decision: dict,
    axis: str,
    total_qty_kg: int,
    coverage_days: int,
    chosen: list[dict] | None,
    rounds: list[dict],
    *,
    as_of: str,
    lead_days: int | None,
    cap_by_date: Mapping[str, float] | None,
    calendar: Mapping[str, Any] | None = None,
    timing_withdrawn: bool = False,
) -> list[str]:
    """분할에서 나온 유의사항. **timing 라벨과 실제 행동이 어긋나면 반드시 적는다** (규칙 3).

    회차가 하나로 끝나는 경로가 둘인데 이유가 다르다:

    1. **진입 자체를 안 함** — ①이 클립 전 추정 총량으로 축을 열었고 ④가 클립 후 실제
       총량으로 판정해 닫혔다 (§4-④ E3-3 확정 2가 "정상"이라고 한 경우다)
    2. **진입했는데 이 안이 못 버팀** — 0kg 회차나 겹치는 날짜가 나온다

    둘 다 조용히 넘기면 소비자가 라벨(timing)과 행동(일괄)의 불일치를 추적할 수 없다.
    첫 번째 경로는 Codex 교차검증에서 P1으로 잡혔다 — 처음엔 두 번째만 고지했었다.

    🔴 **첫 번째 경로의 고지가 자리를 옮겼다** (`#308` · 2026-09-12). 전에는 *"timing 라벨인데
      회차가 하나"* 인 안에 붙였는데, 이제 그런 안이 서지 않는다 — ``effective_allowed_axes``
      가 그 축을 걷기 때문이다. **불일치를 고지하는 대신 불일치를 없앤다.**

      ⚠️ 그래도 **고지는 남긴다.** 출력의 ``allowed_axes`` 에는 여전히 분할 축이 들어 있어,
        읽는 사람이 *"열린 축을 아무 안도 안 썼다"* 를 보게 된다. 그 사유가 없으면
        되물을 자리가 없다. 그래서 축을 **걷힌 안**에 같은 문장을 붙인다 —
        ``timing_withdrawn`` 이 그 안을 짚는다.
    """
    # 🔴 **민 사실은 축과 무관하게 적는다** (`#300`). 아래 조기 반환들이 timing 축 ·
    #   분할 진입 안에서만 돌아서, 여기 붙이면 일괄 안이 밀렸을 때 **고지가 통째로
    #   빠진다** — `#93` 이 ``cap_by_date`` 에서 정확히 그렇게 뚫렸던 자리다.
    shift_note = shifted_rounds_note(as_of, coverage_days, len(rounds), calendar)
    moved = [shift_note] if shift_note else []
    if axis != TIMING_AXIS:
        if timing_withdrawn:
            return [
                *moved,
                (
                    f"그날 분할 축이 열렸지만 분할에 진입하지 않아"
                    f"({_entry_miss_reason(decision)}) 이 안은 수량 축으로 나간다 — "
                    "허용 축은 하드 제약으로 깎기 전 추정 총량으로 열고, "
                    "분할 진입은 깎은 뒤 실제 총량으로 판정한다"
                ),
            ]
        return moved  # quantity·mix 축 안은 애초에 분할 대상이 아니다
    reason = chosen and split_infeasible_reason(total_qty_kg, chosen, coverage_days)
    if reason:
        return [*moved, f"분할 불가({reason})로 일괄 전환 — timing 축 안이지만 회차는 하나다"]
    # 재배분 결과를 여기서 다시 계산한다 — 바로 위 ``split_infeasible_reason``과 같은
    # 방식이다. 순수 함수라 같은 입력이면 같은 답이고, 수량과 고지가 갈라질 수 없다.
    even = split_quantities(total_qty_kg, chosen or [])
    adjusted, cap_note = cap_constrained_quantities(
        even,
        arrival_dates(as_of, coverage_days, len(rounds), lead_days, calendar),
        cap_by_date,
    )
    head = f"{len(rounds)}회 분할"
    # 🔴 **⑥은 자기가 *한 일*만 말한다 — 판정은 ⑦이 말한다** (#93 결정 2026-09-03).
    #
    #   전에는 여기서 미검사 사유(``CAP_BLOCK_REASONS``)와 위반("여유를 넘는다")까지
    #   실었다. 그런데 이 함수는 **timing 축 · 분할 진입 안에서만** 돈다(위 두 줄의 조기
    #   반환). 그래서 quantity 축 안은 **검사도 고지도 없었다** — 여유 100kg 에 2,571kg 을
    #   넣는 계획이 risks 줄조차 없이 나갔다(#93 재현).
    #
    #   "검사를 누가 하는가"와 "결과를 누가 말하는가"가 갈라져 있던 것이 그 구멍의
    #   원인이다. 이제 ⑦ ``arrival_capacity`` 가 **모든 안**을 검사하고 그 결과를 말한다.
    #
    # ★ 남는 것은 **재배분이 실제로 일어났다**는 사실 하나다. 그건 ⑥의 행동이라 ⑥이
    #   말해야 한다. 문면이 아니라 ``adjusted != even`` 으로 가른다 — 문구를 다듬는 날
    #   고지가 조용히 바뀌지 않게.
    if adjusted != even:
        return [*moved, f"{head} — {cap_note}"]
    return [*moved, head]


def _risks(draft: dict, deferred: list[str], lots: list[dict] | None, as_of: str) -> list[str]:
    """위험·유의사항. **미결값 때문에 건너뛴 검사도 여기 싣는다.**

    ``rejected_reasons``가 아니라 risks인 이유: 소비자는 rejected_reasons를 "컷된 안의
    이력"으로 읽는다. "검사를 하지 않았다"는 다른 의미라 그 필드에 섞으면 계약이 오염된다.
    """
    risks = list(deferred)
    for clip in draft["clipped_by"]:
        risks.append(
            f"{clip['constraint']} 제약으로 원안 {clip['raw_qty_kg']:,}kg에서 "
            f"{clip['cap_kg']:,}kg으로 축소"
        )
    lot = lots[0] if lots else {}
    # 잔여신선도는 **물류가 계산해 보낸다** (`remaining_freshness_days` · #76).
    # 전에는 shelf_life_days − (as_of − stocked_at)으로 파생했는데, 같은 개념을 두 곳에서
    # 계산하면 어긋난다 — 물류가 재고 도메인 값을 이미 내므로 받는 쪽으로 정리했다.
    # ``None``일 수 있다: 물류가 "모른다"를 그렇게 표현한다(§1.2-10). 0으로 읽지 않는다.
    if lot.get("remaining_freshness_days") is not None:
        risks.append(
            f"기존 로트 {lot['lot_id']} 잔여신선도 {lot['remaining_freshness_days']}일 — "
            "신규 매입분이 이 로트를 밀어내지 않는지 확인 필요"
        )
    return risks


def _no_quantity_reason(draft: dict) -> dict[str, Any]:
    """수량이 0인 안의 사유. **「막혔다」와 「필요 없다」를 가른다** (상세설계 §4-③-3).

    ```text
    raw_qty_kg <= 0    보유가 커버 D일 수요를 이미 덮었다        not_needed
    그 밖              하드 제약이 수요를 0까지 깎았다           blocked
    ```

    🔴 **``kind`` 만 붙이면 사람 눈에는 안 갈린다.** 마스터 리포트도 화면도 ``label`` 과
      ``reason`` 만 그린다. 그래서 **문장 자체를 다르게 쓴다** — 기계가 세는 축과 사람이
      읽는 축을 둘 다 가르는 것이다.

    ⚠️ ``not_needed`` 같은 **내부 이름을 문장에 흘리지 않는다.** 이 문장은 H1 화면과
      Critic 이 그대로 읽으므로 그 자체로 말이 되어야 한다.

    🔴 **종전 문장은 같은 수를 두 번 적었다** (2026-09-12 정정). ``deducted_holdings_kg`` 를
      「보유 재고」로 적고 바로 옆에 ``demand_qty_kg`` 를 적었는데, **둘은 이 문장이 나갈 때
      반드시 같다** — 조건이 ``raw_qty ≤ 0`` 이고 차감이 ``일평균 × D`` 로 클램프되므로
      ``round(차감) == demand_qty`` 가 성립한다. 실측: 걷기 8판 · 이 문장 **1,384건 중
      1,384건**에서 두 수가 동일(차이 0건)이었다::

          보유 재고 44kg이 커버 2일 수요 44kg을 이미 덮어 …      ← 같은 날 110/110 · 265/265

      ★ 우연이 아니라 **구조**다. 읽는 사람에게는 *"44가 44를 덮는다"* 라는 동어반복이고,
        창고에 44kg 이 있었다는 **실측처럼 보이는데 측정이 아니다.**

    🟢 **그래서 「무엇이 덮었나」를 적는다.** 차감을 실제로 누른 값은 물류가 집계한
      가용재고이고, 그 수는 수요와 **다르다** (``가용재고 ≥ 수요`` 는 보장되지만 같을 이유가
      없다). 실측: V6 의 이 문장 263개 중 **80개(30%)가 「가용재고 < 수요」** 였다 — 종전
      문면이 사실과 어긋나 있던 자리다.

    🔴 **낱말은 봉투가 쓰는 것을 그대로 쓴다** — 물류 근거 문장이
      *"{품목} 가용재고 합계 — 비-ACTIVE·신선도 만료 Lot 제외, **확정 출고 예약분 차감**"*
      이다. 「자유재고」는 물류 docstring 2건에만 있고 밖으로 안 나가는 말이라 쓰지 않는다.
      수식어를 붙이는 이유는 **이름이 같아서 난 사고**라서다 — 수식어 없는 「가용재고」는
      우리 ``lots[].available_qty_kg`` 와 다시 구분되지 않는다.

    ⚠️ **가용재고를 못 받은 날은 수 하나로 떨어진다.** 「못 봤다」와 「덮었다」를 같은 문면으로
      내지 않는다 — 못 받은 사실은 ③이 ``risks`` 로 따로 고지한다.
    """
    if draft.get("raw_qty_kg", 0) <= 0 and draft.get("deducted_holdings_kg", 0) > 0:
        free_stock = draft.get("free_stock_kg")
        covered = f"커버 {draft['coverage_days']}일 수요 {draft['demand_qty_kg']:,}kg을"
        if free_stock is None:
            held = "보유가"
        else:
            held = f"확정 출고 예약분을 뺀 가용재고 {free_stock:,}kg이"
        return {
            "label": draft["label"],
            "reason": f"{held} {covered} 이미 덮어 이날은 매입이 필요 없다",
            "kind": "not_needed",
        }
    binding = ", ".join(clip["constraint"] for clip in draft["clipped_by"]) or "미상"
    return {
        "label": draft["label"],
        "reason": f"하드 제약({binding})으로 수량이 0까지 축소되어 제안 불가",
        "kind": "blocked",
    }


def package_scenarios(state: PurchaseAgentState) -> dict[str, Any]:
    """안별로 split·sourcing을 묶고 근거를 붙여 시나리오를 완성한다."""
    constraints = load_constraints()
    base = state["base_plan"]
    drafts = base["drafts"]
    split_choice = state["split_plan"]  # ④ 분할 유형·비율 (진입 안 했으면 1회차 목록)
    # 🔴 **실효 축으로 배정한다** (`#308`). ①이 연 축 그대로 주면 ④가 안 나눈 날에도
    #   timing 라벨이 서서 «회차 하나짜리 분할안» 이 된다 — ``effective_allowed_axes``
    #   docstring 에 근거와 실측이 있다. ⑦ ``check_axis_diversity`` 도 **같은 목록**을 본다.
    effective_axes = effective_allowed_axes(state["allowed_axes"], split_choice)
    labels = [d["label"] for d in drafts]
    aggressive_axis = constraints["allocation"]["aggressive_axis"]
    axes = assign_axes(labels, effective_axes, aggressive_axis)
    # ①이 연 축 그대로 배정했으면 어느 안이 timing 을 받았을지 — **고지를 붙일 자리**를
    # 짚는 데만 쓴다. 판정에는 안 쓴다 (`_split_risks` 의 ``timing_withdrawn``).
    declared_axes = assign_axes(labels, state["allowed_axes"], aggressive_axis)
    lots = state["inventory"].get("lots")
    contract_price = state["contract_price"]  # 미수령이면 None — 마진 두 값이 함께 null이 된다
    decision = _sourcing_decision(state["sourcing_plan"])  # ⑤ 등급 배분 판단 근거
    # N4·수용량은 그날 하나뿐이라 안 루프 밖에서 한 번만 읽는다.
    # ``cap_by_date``는 **어댑터 경로에만** 있다 — mock 재고에는 없어서 None이고,
    # 그때 회차 조정은 일어나지 않는다 (부재가 정상 경로다).
    lead_days = pending_value(state, constraints, "inbound_lead_days")
    cap_by_date = state["inventory"].get("cap_by_date")
    # 회차일을 장이 서는 날로 미는 데 쓴다 (`#300` · ``round_offsets``). 그날 하나뿐이라
    # 안 루프 밖에서 한 번 읽는다 — ``lead_days`` · ``cap_by_date`` 와 같은 이유다.
    calendar = state.get("execution_calendar")
    split_facts = split_decision(split_choice)
    # 시세 근거 좌표는 **관측일 기준**이고 그날 하나뿐이다. 안 루프 안에서 만들면 같은
    # 시세에서 나온 근거들이 서로 다른 좌표를 갖게 된다 (실제로 그랬다 — Codex 2차 지적).
    quote_ref = f"MQ-가락-{observed_date(state['market_quotes']) or state['date']}"

    scenarios = []
    dropped = []
    for draft in drafts:
        total = draft["total_qty_kg"]
        if total <= 0:
            # 수량이 0이라 안이 될 수 없다 (스키마가 total_qty_kg > 0을 요구한다). 조용히
            # 사라지지 않게 사유를 남긴다 — 안이 왜 없는지가 소비자에게 보여야 한다.
            dropped.append(_no_quantity_reason(draft))
            continue
        sourcing = materialize_sourcing(total, state["sourcing_plan"])
        # 분할은 **timing 축을 받은 안에만** 붙는다 (§4-④ E3-3 확정 1). 전 안에 걸면
        # 세 안의 split 구조가 같아져 timing이 라벨로만 남는다 — §3.5.1-3이 막으려는 상태다.
        axis = axes[draft["label"]]
        chosen = split_choice if axis == TIMING_AXIS else None
        coverage_days = draft["coverage_days"]
        # ★ 회차 금액을 여기서 얹는다 — ``sourcing`` 이 있어야 계산되므로
        #   ``materialize_split`` 안이 아니라 밖이다. 이후 ``split_plan`` 과
        #   ``payment_schedule`` 이 **같은 목록**을 본다.
        rounds = with_round_amounts(
            materialize_split(
                state["date"],
                total,
                chosen,
                coverage_days,
                lead_days=lead_days,
                cap_by_date=cap_by_date,
                calendar=calendar,
            ),
            sourcing,
        )
        rationale_input = {**draft, "daily_demand_kg": base["daily_demand_kg"]}
        unit_price = _weighted_unit_price(sourcing, total)
        margin_warning, expected_margin_rate = compute_margin(unit_price, contract_price)
        # ⚠️ **둘을 따로 부른다 — 지금은 같은 값이다** (`compute_cut_unit_price` 참조).
        #   재무 STRESS 로 나가는 것은 ``max_price`` 뿐이고, 컷은 ``cut_unit_price`` 가 한다.
        max_price = compute_max_price(state["forecast"], draft["coverage_days"])
        cut_unit_price = compute_cut_unit_price(state["forecast"], draft["coverage_days"])
        scenarios.append(
            {
                "label": draft["label"],
                "strategy_type": axis,
                "coverage_days": draft["coverage_days"],
                "total_qty_kg": total,
                "total_amount_krw": sum(
                    line["qty_kg"] * line["grade_unit_price"] for line in sourcing
                ),
                # 🔴 **재무 STRESS 전용이다** — 컷은 ``cut_unit_price`` 가 한다.
                "max_price": max_price,
                "cut_unit_price": cut_unit_price,
                # 규칙 5 — 계약단가 초과는 컷이 아니라 표시다.
                "margin_warning": margin_warning,
                "split_plan": rounds,
                "sourcing_plan": sourcing,
                # 분할 안이고 N5를 받은 날만 실린다 — 아니면 **키 자체가 없다**.
                # ★ **``max_price`` 다 — ``cut_unit_price`` 가 아니다.** 재무·마스터가
                #   ``amount_max_krw == qty × max_price`` 를 검사한다.
                **_payment_schedule_field(
                    rounds,
                    max_price,
                    pending_value(state, constraints, "purchase_payment_days"),
                ),
                "expected_margin_rate": expected_margin_rate,
                "rationale": [
                    *_rationale(state, rationale_input, constraints, quote_ref),
                    *_context_rationale(state["context_docs"]),
                    *_sourcing_rationale(decision, quote_ref),
                    *_split_rationale(split_facts, rounds, state["forecast"], state["date"]),
                ],
                "risks": [
                    *_risks(draft, base["deferred_checks"], lots, state["date"]),
                    *_forecast_risks(state["forecast"], draft["coverage_days"]),
                    *_adjustment_risks(
                        state.get("adjustments"),
                        constraints,
                        draft["label"],
                        # 물렸으면 ``_risks`` 가 이미 축소 문장을 냈다 — 겹쳐 적지 않는다.
                        clipped=any(
                            clip["constraint"] == ADJUSTMENT_CAP_NAME
                            for clip in draft["clipped_by"]
                        ),
                    ),
                    *_context_risks(
                        state["context_loop_count"],
                        state["context_docs"],
                        state["date"],
                        # 🔴 **빈 문서가 두 뜻이라 넘긴다** — "그날 없었다"와 "못 읽었다".
                        #   State 는 갈라 들고 있는데 ⑥이 안 읽어서 문면이 하나였다.
                        state.get("context_unavailable"),
                    ),
                    *_sourcing_risks(sourcing, decision),
                    *_split_risks(
                        split_facts,
                        axis,
                        total,
                        coverage_days,
                        chosen,
                        rounds,
                        as_of=state["date"],
                        lead_days=lead_days,
                        cap_by_date=cap_by_date,
                        calendar=calendar,
                        # 축이 걷힌 안 하나에만 붙는다 (`#308`) — 세 안에 다 붙이면
                        # 같은 사실이 세 번 나가고, 안 붙이면 열린 축을 아무도 안 쓴
                        # 이유가 사라진다.
                        timing_withdrawn=declared_axes[draft["label"]] == TIMING_AXIS
                        and axis != TIMING_AXIS,
                    ),
                    *_payment_risks(
                        rounds,
                        pending_value(state, constraints, "purchase_payment_days"),
                        state.get("critical_payment_dates"),
                    ),
                ],
            }
        )

    return {
        "scenarios_final": scenarios,
        "rejected_reasons": [*state["rejected_reasons"], *dropped],
        "confidence": constraints["situation"]["confidence_by_situation"][state["situation"]],
    }
