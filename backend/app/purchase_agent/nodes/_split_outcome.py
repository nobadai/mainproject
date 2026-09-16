"""④ 와 ⑥ 이 **같이 쓰는** 분할 성립·적용 판정 (E3-9 · 2026-09-17).

🔴 **왜 여기로 뺐나.** ⑥ 이 «이 분할을 실제로 적용하나 · 회차를 바꾸나 · 일괄로 접나» 를
  정하는데, ④ 는 그 전에 판단자(LLM)를 부를지 정한다. 두 곳이 **다른 기준**을 쓰면
  ④ 가 부르고 고른 배분을 ⑥ 이 버린다 — 실제로 창이 한 값인 날 판단자가 SUCCESS 를 내고
  ⑥ 이 «실익 없음» 으로 일괄로 되돌렸다 (1차 실호출 실험 · 2026-09-17).
  ⑥ 은 ④ 를 import 하므로(``split_decision``) ④ 가 ⑥ 을 부르면 순환이 된다 — 그래서
  둘 다 이 모듈을 부른다.

★ **네 갈래를 가른다** — 진입(④)과 선택(④ 판단자) 뒤에 오는 «적용» 의 결과다::

    AS_CHOSEN        ④ 가 낸 비율 그대로 나눈다 (균등이든 판단자가 고른 배분이든)
    ROUNDS_CHANGED   그 회차 수가 안 서서 **다른 회차 수를 균등으로** 나눈다
    ROLLED_BACK      분할을 접고 **한 번에** 산다
    NOT_SPLIT        이 안은 분할 대상이 아니다 (timing 을 못 받았다)

⚠️ **계산만 한다** (규칙 6). 문장은 사유 하나만 만들고, 설명 문면은 ⑥ 이 이 갈래를 읽어 쓴다.
"""

from collections.abc import Mapping
from functools import partial
from typing import Any, NamedTuple

from app.purchase_agent.allocation import (
    arrival_dates,
    cumulative_overflow,
    equal_ratios,
    split_infeasible_reason,
    split_quantities,
)
from app.purchase_agent.nodes.draft_plan import WAREHOUSE_CAP_NAME
from app.purchase_agent.schemas import TIMING_AXIS

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


def split_stands(
    total_qty_kg: int,
    chosen: list[dict] | None,
    *,
    coverage_days: int,
    as_of: str,
    lead_days: int | None,
    cap_by_date: Mapping[str, float] | None,
    calendar: Mapping[str, Any] | None,
    widened: bool,
) -> bool:
    """이 분할이 **실제로 서는가** — 회차·비율·날짜·누적까지 밟아 본다 (E3-9 앞단).

    🔴 **「④ 가 진입했다」는 안전을 보장하지 않는다.** 진입은 *"나눠 볼 만하다"* 이고,
      성립은 회차 수와 배분 비율로 만든 **실제 도착일**이 날짜별 누적 여유를 지킬 때다.
      그 사이에 ⑥ 의 재배분(``cap_constrained_quantities``)이 한 번 끼어든다.

    막는 것 넷 (하나라도 걸리면 분할이 안 선다)::

        ㉠ 회차가 없다/하나다                    chosen 이 비었거나 길이 1
        ㉡ 회차·날짜가 안 선다                   split_infeasible_reason
        ㉢ 실제 날짜를 못 놓는다                 arrival_dates 가 None · 여유 칸이 없다
        ㉣ 재배분 뒤에도 누적이 넘는다            cumulative_overflow

    🔴 ㉣ 는 ⑦ ``arrival_capacity`` 와 **같은 함수**를 부른다 — 두 곳이 각자 더하면
      ⑥ 이 «선다» 고 본 분할을 ⑦ 이 컷하고, 그날 안은 왜 죽었는지 설명할 수 없다.

    ⚠️ **여유를 못 보면 «선다» 로 읽지 않는다** (규칙 3). 넓힌 근거가 ``cap_by_date`` 인데
      그것을 못 보면 넓힌 채로 둘 근거도 사라진다 — 일괄로 되돌리는 쪽이 보수적이다.
    """
    if not chosen or len(chosen) < 2:
        return False
    if split_infeasible_reason(total_qty_kg, chosen, coverage_days):
        return False
    arrivals = arrival_dates(as_of, coverage_days, len(chosen), lead_days, calendar)
    if arrivals is None:
        return False
    if cap_by_date is None or any(cap_by_date.get(day) is None for day in arrivals):
        # 🔴 **「못 봤다」는 「선다」가 아니다** (규칙 3 · 2026-09-16 검토).
        #   다만 **넓힌 안과 안 넓힌 안의 처지가 다르다** —
        #
        #       안 넓힌 안   원래 서 있던 계획이다. 못 본 것을 이유로 우리가 먼저 깎으면
        #                    ⑦ 이 컷하지도 않을 안을 죽인다 → 그대로 둔다 (⑦ 태도)
        #       넓힌 안      넓힌 **근거가 바로 그 여유**다. 그 여유를 못 보면 넓힐 근거가
        #                    사라진 것이라 **일괄로 되돌린다** — 「모르는 곳에 더 쌓는」
        #                    계획을 내지 않는다 (``cap_constrained_quantities`` 와 같은 태도)
        return not widened
    quantities, _ = cap_constrained_quantities(
        split_quantities(total_qty_kg, chosen), arrivals, cap_by_date
    )
    return cumulative_overflow(quantities, arrivals, cap_by_date) is None


def _reclipped_to_bulk(draft: dict, single_round_cap_kg: int) -> dict:
    """수량을 **일괄 상한으로 되돌리고** ``clipped_by`` 를 다시 계산한 새 draft.

    🔴 **``clipped_by`` 를 같이 고치는 이유.** ⑥ 의 축소 문장 · ``_no_quantity_reason`` ·
      ⑧ ``review_rationale`` 이 그 칸을 읽는다. 수량만 되돌리면 **문장이 틀린 수를 적는다.**

    🔴 ``raw_qty_kg`` · ``demand_qty_kg`` · ``deducted_holdings_kg`` 는 **안 건드린다** —
      그것은 사실이고, 되돌린 것은 «얼마나 살 수 있나» 뿐이다.
    """
    raw = draft["raw_qty_kg"]
    남은 = [clip for clip in draft["clipped_by"] if clip["constraint"] != WAREHOUSE_CAP_NAME]
    창고 = (
        [{"constraint": WAREHOUSE_CAP_NAME, "cap_kg": single_round_cap_kg, "raw_qty_kg": raw}]
        if single_round_cap_kg < raw
        else []
    )
    return {
        **draft,
        "total_qty_kg": min(draft["total_qty_kg"], single_round_cap_kg),
        # 창고를 맨 앞에 둔다 — ③ ``caps`` 의 선언 순서가 그렇고, 문장이 그 순서를 읽는다.
        "clipped_by": [*창고, *남은],
    }


def _되돌림_사유(total_qty_kg: int, single_round_cap_kg: int) -> str:
    """일괄로 접은 사유. **넓혔던 안과 안 넓힌 안의 문장이 다르다** (2026-09-17).

    ⚠️ 전에는 한 문장이었다 — *"분할을 전제로 잡았던 X kg 을 한 번에 들어가는 Y kg 으로
      되돌렸다"*. 안 넓힌 안(X ≤ Y)에 붙으면 **줄이지도 않은 것을 줄였다고** 읽힌다.
    """
    if total_qty_kg > single_round_cap_kg:
        return (
            f"분할을 전제로 잡았던 {total_qty_kg:,}kg 을 한 번에 들어가는 "
            f"{single_round_cap_kg:,}kg 으로 되돌렸다 — "
            "회차를 나눠도 날짜별 창고 여유를 지키지 못한다"
        )
    return (
        f"분할을 접고 한 번에 산다 — 회차를 나누면 날짜별 창고 여유를 지키지 못하고, "
        f"{total_qty_kg:,}kg 은 한 번에 들어간다"
    )


def _실익_없음_사유(total_qty_kg: int, single_round_cap_kg: int) -> str:
    return (
        f"분할 축이 열렸지만 총량 {total_qty_kg:,}kg 이 한 번에 들어가는 "
        f"{single_round_cap_kg:,}kg 안에 들어 나누지 않는다 — 회차를 늘려도 더 살 수 없다"
    )


#: ⑥ 이 이 안의 분할을 어떻게 끝냈나 (모듈 머리말).
AS_CHOSEN = "AS_CHOSEN"
ROUNDS_CHANGED = "ROUNDS_CHANGED"
ROLLED_BACK = "ROLLED_BACK"
NOT_SPLIT = "NOT_SPLIT"


class SplitOutcome(NamedTuple):
    """⑥ 이 한 안에 대해 내린 적용 판정."""

    #: 되돌렸으면 일괄 상한으로 다시 깎은 draft · 아니면 원본
    draft: dict
    #: ⑥ 이 **실제로 쓸** 비율 목록. ``None`` 이면 한 번에 산다
    ratios: list[dict] | None
    kind: str
    #: risks 로 나갈 사유 문장 (없으면 ``None``)
    note: str | None


def settle_split(
    draft: dict,
    axis: str,
    split_choice: list[dict] | None,
    *,
    by_trend: bool,
    constraints: dict,
    as_of: str,
    lead_days: int | None,
    cap_by_date: Mapping[str, float] | None,
    calendar: Mapping[str, Any] | None,
) -> SplitOutcome:
    """이 안에서 분할을 **적용하나 · 회차를 바꾸나 · 접나.** ⑥ 이 쓰고 ④ 가 미리 잰다.

    ★ **되돌릴 것이 없는 날이 대부분이다.** ③ 이 넓히지 않았으면(``single_round_cap_kg``
      가 ``None`` 이거나 총량이 이미 그 이하면) 대개 원본을 그대로 돌려준다.

    🔴 ``single_round_cap_kg`` 가 ``None`` 인 것은 「날짜 축을 못 봤다」다 (규칙 3).
      0 이나 무제한으로 바꾸지 않는다 — 못 본 날은 ③ 이 예전 기준(오늘 여유)으로 이미
      깎았고, 여기서 다시 손대면 **못 본 것을 본 것처럼** 다루게 된다.

    🔴 **실익 판단이 성립 판단보다 먼저다** (2026-09-17). 한 번에 들어가는 양인데
      (넓히지 않았다) 궤적 진입도 아니면 **어느 비율 · 어느 회차 수로도** 나눌 이유가 없다.
      전에는 «고른 비율이 서면» 에만 이 판단을 해서, 고른 비율이 안 서면 다른 회차 수로
      나누는 길이 열려 있었다 — 같은 날 같은 안이 **비율에 따라** 나뉘기도 하고 안 나뉘기도
      했다. 그러면 ④ 는 «어느 후보가 적용되나» 를 미리 알 수 없다.

    🔴 **다른 회차 수를 볼 때 ④ 의 비율을 재사용하지 않는다** (2026-09-16 검토).
      그 비율은 **그 회차 수 전용**이다 — 2회차용 ``[0.6, 0.4]`` 를 3회차에 늘려 쓰면
      판단자가 보지도 않은 배분이 «고른 것» 으로 나간다. 다른 회차 수는 **균등**으로만
      본다 (``BASE_EQUAL`` — 되돌아갈 자리이지 후보가 아니다).
    """
    single = draft.get("single_round_cap_kg")
    total = draft["total_qty_kg"]
    widened = single is not None and total > single
    if axis != TIMING_AXIS:
        # 축을 못 받은 안은 1회차로 나간다 — 넓힌 수량이 남아 있으면 ⑦ 이 컷한다.
        if widened:
            return SplitOutcome(
                _reclipped_to_bulk(draft, single), None, ROLLED_BACK, _되돌림_사유(total, single)
            )
        return SplitOutcome(draft, None, NOT_SPLIT, None)
    if single is None:
        return SplitOutcome(draft, split_choice, AS_CHOSEN, None)
    if not widened and not by_trend:
        # ⚠️ **궤적으로 진입한 날은 예외다** — 가격이 오르는 날 나눠 사는 것은 수량이
        #   안 늘어도 뜻이 있다 (그게 timing 축의 본래 이유다).
        return SplitOutcome(draft, None, ROLLED_BACK, _실익_없음_사유(total, single))

    잰다 = partial(
        split_stands,
        total,
        coverage_days=draft["coverage_days"],
        as_of=as_of,
        lead_days=lead_days,
        cap_by_date=cap_by_date,
        calendar=calendar,
        widened=widened,
    )
    if 잰다(split_choice):
        return SplitOutcome(draft, split_choice, AS_CHOSEN, None)

    # 그 회차 수가 안 선다 — **허용 목록의 다른 회차 수**를 균등으로 본다.
    현재 = len(split_choice or [])
    for rounds in sorted(constraints["split"]["types"]):
        if rounds < 2 or rounds == 현재:
            continue
        대안 = [{"ratio": ratio} for ratio in equal_ratios(rounds)]
        if 잰다(대안):
            return SplitOutcome(
                draft,
                대안,
                ROUNDS_CHANGED,
                f"{현재}회 분할이 날짜별 여유를 못 지켜 {rounds}회 균등으로 바꿨다",
            )
    return SplitOutcome(
        _reclipped_to_bulk(draft, single), None, ROLLED_BACK, _되돌림_사유(total, single)
    )
