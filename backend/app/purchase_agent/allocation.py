"""분할 회차의 **비율·오프셋·수량 투영** — 노드가 공유하는 순수 함수층.

🔴 **왜 모듈을 따로 뒀나 — 역 import 가 순환이기 때문이다.**

노드 사이의 의존 방향이 이미 한쪽이다 (실측 · ``dev@2b0c270``)::

    split_plan          → classify_situation                     (그것뿐)
    package_scenarios   → split_plan · allocate_sourcing · draft_plan · classify_situation
    self_check          → split_plan · draft_plan · collect_context

그래서 ④ ``split_plan`` 이 ⑥ ``package_scenarios`` 나 ⑦ ``self_check`` 의 함수를 쓰려고
그쪽을 import 하면 **순환**이 된다. ④가 «비율 후보를 만들어 놓고 그 후보가 실제로 설 수
있는지» 를 미리 보려면 ⑥의 투영 산술이 필요한데, 그 자리를 노드 밖으로 뺀 것이 여기다.

★ **그래서 이 모듈은 노드를 하나도 import 하지 않는다.** 이 규율이 깨지면 순환이 돌아오고,
  그것을 검사가 잠근다 (``test_allocation.py``).

🟢 **이 판은 옮기기만 한다 — 동작 변경 0.** 아래 셋은 몸통도 주석도 원래 자리의 것을
그대로 가져왔고, 원래 모듈은 여기서 import 해 **같은 이름으로 계속 내보낸다**::

    equal_ratios      ← nodes/split_plan.py
    split_offsets     ← nodes/package_scenarios.py
    split_quantities  ← nodes/package_scenarios.py

⚠️ **아래 셋의 docstring 은 한 글자도 안 고쳤다.** 그래서 ``split_offsets`` 안의
*"날짜를 ④가 아니라 **여기서** 만드는 이유"* 의 「여기」는 **원래 살던 ⑥ ``package_scenarios``**
를 가리킨다 — 지금도 그 함수를 부르는 것은 ⑥이다. 옮기면서 문면을 손보면 *"몸통이 같다"* 를
AST 로 증명할 수 없게 되고, 「옮긴 것」과 「고친 것」이 한 커밋에 섞인다.

🔄 **``round_offsets`` · ``arrival_dates`` 도 뒤따라 옮겼다** (2026-09-14 · 같은 이유).
처음에는 *"달력 봉투를 읽으니 순수 산술이 아니다"* 로 남겨 뒀는데, 재보니 **둘 다 넘겨받은
``Mapping`` 만 본다** — 봉투 모양을 아는 것이 아니라 **인자를 읽는 것**이다. 그리고 ④가
«이 후보가 실제로 설 수 있나» 를 미리 보려면 ⑥·⑦과 **같은 날짜 계산**을 써야 한다.
다른 함수로 재면 «④는 된다는데 ⑦이 컷하는» 안이 생긴다.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from itertools import pairwise
from typing import Any


def equal_ratios(rounds: int) -> list[float]:
    """균등 비율. 마지막을 ``1 − Σ앞``으로 **구성**한다.

    각자 계산한 ``1/n``을 n번 더하면 부동소수점 합이 1에서 밀려 ⑥의 합계 검사(1e-9)에
    걸릴 수 있다 — E3-1에서 등급 비율에 쓴 것과 같은 장치다.
    """
    head = [1 / rounds] * (rounds - 1)
    return [*head, 1.0 - sum(head)]


def split_offsets(coverage_days: int, rounds: int) -> list[int]:
    """회차별 **매입 실행일** 오프셋 = ``round(i × D / rounds)``.

    첫 회차는 항상 0(= as_of)이다 — IO명세 §2 "seq 1의 date = as_of".
    날짜를 ④가 아니라 여기서 만드는 이유: 안마다 D가 다르다 (§4-④ E3-3 확정 4).
    보수(D=2)와 공격(D=12)에 같은 날짜를 박으면 보수안의 2회차가 커버 구간 밖으로 나간다.

    ⚠️ 이 date는 **도착일이 아니다.** 도착일 = ``date + N4``이고 N4가 NULL이라 계산하지
    않는다 (§5.5 · 규칙 3).
    """
    return [round(index * coverage_days / rounds) for index in range(rounds)]


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


def weighted_ratios(weights: list[float], rounds: int) -> list[float]:
    """앞 회차 가중치 목록 → 비율. **마지막은 ``1 − Σ앞`` 으로 구성한다.**

    ``equal_ratios`` 와 **같은 방식**이다 — 마지막을 따로 적지 않고 잔차를 흡수시킨다.
    각자 적은 수를 더하면 부동소수점 합이 1에서 밀려 ⑥의 합계 검사(1e-9)에 걸린다.

    🔴 **값을 여기 안 박는다.** 가중치는 선언(``constraints.yaml`` ``split.allocation_weights``)
    이 소유한다 (규칙 7). 그리고 그 값은 지금 **``PROVISIONAL``** 이다 — 정책 승인 전이라
    운영 기능은 꺼 둔다.
    """
    if len(weights) != rounds - 1:
        raise ValueError(
            f"가중치 {len(weights)}개는 {rounds}회차에 안 맞는다 — 앞 회차만 적고 "
            "마지막은 잔차로 둔다"
        )
    return [*weights, 1.0 - sum(weights)]


def occupancy_fits(
    quantities: list[int],
    arrivals: list[str] | None,
    cap_by_date: Mapping[str, float] | None,
) -> bool:
    """이 배분이 **도착일 수용량 안에 드는가.** 넘으면 ``False`` — 깎지 않는다.

    🔴 **⑦ ``check_arrival_capacity`` 와 같은 셈이다** — 도착일까지 **누적**해 그날 여유와
    견준다 (앞 회차가 아직 창고에 있으므로). 다른 셈으로 재면 «④는 된다는데 ⑦이 컷하는»
    후보가 생기고, 그 안은 **왜 죽었는지 설명할 수 없다.**

    ⚠️ **모르는 날이 하나라도 있으면 ``False``** 다 (규칙 3). 못 본 것을 「든다」로 읽으면
    모르는 것이 판정을 만든다. 그 경우 후보에서 빠지고 균등안만 남는다 — 판정을 안 한
    쪽이 아니라 **안 고르는 쪽**으로 기운다.

    ★ 여기서 ``True`` 가 곧 «⑦을 통과한다» 는 아니다. ⑦은 클립·재배분 뒤 최종값을 보고
      여기는 후보 단계의 값을 본다 — 이 함수는 **선택 전에 명백히 못 서는 것을 걷는** 자리다.
    """
    if arrivals is None or cap_by_date is None:
        return False
    occupied = 0
    for quantity, day in zip(quantities, arrivals, strict=True):
        cap = cap_by_date.get(day)
        if cap is None:
            return False
        occupied += quantity
        if occupied > int(cap):
            return False
    return True


#: 선언이 낼 수 있는 후보 이름. 🔴 **여기 없는 이름이 선언에 들어오면 멈춘다.**
#:
#: ⚠️ 후보 집합이 선언 하나에서만 늘어나면, LLM 이 그 이름을 골랐을 때 **무슨 뜻인지
#:   아무도 모른다** — 배분의 성격(앞으로 몰까 뒤로 몰까)은 코드가 알아야 하는 사실이고,
#:   ``risks`` 문장도 그 이름으로 쓴다. 선언은 «얼마나» 를 정하고 «무엇이 있나» 는 여기다.
WEIGHTED_CANDIDATES = ("FRONT_LOADED", "BACK_LOADED")

#: 🔴 승인 전 상태. 이 값이면 **후보를 안 세운다** — 근거 없는 비율로 안을 만들면
#: 나중에 그 배분이 「검증된 것」으로 보인다.
PROVISIONAL = "PROVISIONAL"


def allocation_candidates(
    declaration: Mapping[str, Any],
    rounds: int,
    *,
    approved_only: bool = True,
) -> dict[str, list[float]]:
    """규칙이 만드는 **배분 후보 집합**. LLM 은 이 중 하나를 고르기만 한다.

    돌려주는 것은 ``{candidate_id: 비율 목록}`` 이고 ``BASE_EQUAL`` 이 늘 들어 있다 —
    **fallback 대상이 후보 안에 있어야** 실패했을 때 고를 것이 남는다.

    🔴 **승인 전 값으로는 후보를 안 세운다.** 선언이 ``PROVISIONAL`` 이면 균등 하나만
    돌려준다. 근거 없는 비율로 안을 만들면 나중에 그 배분이 **「검증된 것」으로 보인다** —
    `#390` 에서 무른 것과 같은 모양이다.

    ⚠️ 후보가 하나면 부르는 쪽이 LLM 을 **안 부른다** (⑤ ``needs_llm`` 과 같은 게이트).
    고를 것이 없는데 부르면 비용만 들고 상태만 흐려진다.
    """
    후보 = {"BASE_EQUAL": equal_ratios(rounds)}
    if approved_only and declaration.get("status") == PROVISIONAL:
        return 후보
    판 = {2: "two_rounds", 3: "three_rounds"}.get(rounds)
    if 판 is None:
        return 후보
    선언된 = declaration.get(판) or {}
    모르는 = sorted(set(선언된) - set(WEIGHTED_CANDIDATES))
    if 모르는:
        raise ValueError(
            f"모르는 배분 후보가 선언에 있다 {모르는} — 아는 것은 "
            f"{list(WEIGHTED_CANDIDATES)} 다. 선언만 늘리면 고른 뒤 그 뜻을 아무도 모른다"
        )
    for 이름 in WEIGHTED_CANDIDATES:
        가중치 = 선언된.get(이름)
        if 가중치:
            후보[이름] = weighted_ratios(list(가중치), rounds)
    return 후보
