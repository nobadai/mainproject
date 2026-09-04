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
from app.purchase_agent.nodes._guards import require_positive
from app.purchase_agent.nodes.allocate_sourcing import candidate_summary
from app.purchase_agent.nodes.classify_situation import (
    compute_ci_width,
    compute_rise_rate_2w,
    is_gate_excluded,
)
from app.purchase_agent.nodes.draft_plan import pending_value, purchase_budget_krw
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
    as_of: str, coverage_days: int, rounds: int, lead_days: int | None
) -> list[str] | None:
    """회차별 **도착일** = 회차일 + N4 (상세설계 §5.5).

    ``split_offsets``를 재사용한다 — 매입일을 두 곳에서 각자 계산하면 회차 날짜와
    도착일이 어긋나고, 어긋난 쪽을 아무도 못 찾는다.

    N4가 없으면 ``None``이다. **0으로 채우지 않는다** — 0은 "당일 도착"이라는 확정된
    값이라, 미결을 0으로 적으면 "오늘 승인분이 오늘 도착"이 사실이 된다 (규칙 3).
    """
    if lead_days is None:
        return None
    start = date.fromisoformat(as_of)
    return [
        (start + timedelta(days=offset + lead_days)).isoformat()
        for offset in split_offsets(coverage_days, rounds)
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
) -> list[dict]:
    """회차별 매입 계획. ④가 유형·비율만 정하므로 안별 총량과 날짜를 여기서 만든다.

    ``None``(일괄)이거나 이 안이 분할을 감당하지 못하면 단일 회차로 되돌린다.
    되돌린 사실은 ``_split_risks``가 안의 risks에 싣는다 — timing 라벨인데 회차가 하나인
    상태를 조용히 넘기면 소비자가 라벨과 행동의 불일치를 추적할 수 없다.
    """
    if not chosen:
        return _rounds(as_of, coverage_days, [total_qty_kg], lead_days)
    _validate_ratios(chosen, "split_plan")
    if split_infeasible_reason(total_qty_kg, chosen, coverage_days):
        return _rounds(as_of, coverage_days, [total_qty_kg], lead_days)

    quantities, _ = cap_constrained_quantities(
        split_quantities(total_qty_kg, chosen),
        arrival_dates(as_of, coverage_days, len(chosen), lead_days),
        cap_by_date,
    )
    return _rounds(as_of, coverage_days, quantities, lead_days)


def _rounds(
    as_of: str, coverage_days: int, quantities: list[int], lead_days: int | None
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
    offsets = split_offsets(coverage_days, len(quantities))
    arrivals = arrival_dates(as_of, coverage_days, len(quantities), lead_days)
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
    """max_price = 커버 구간 안 예측 상단의 최대값 (규칙 5 · §4-⑦ "예측 q90 기반").

    **마진 방어선과 무관하다** (규칙 5). 혼동하기 쉬운 두 값을 구분해둔다:

    - ``max_price`` 초과 → ⑦이 **컷한다** (예측 q90 기반 하드 상한)
    - ``contract_price`` 초과 → 컷이 아니라 ``margin_warning=true`` 표시만

    커버 구간으로 자르는 이유: 그 안에서만 실제로 사기 때문이다.

    ★ **``quality`` 게이트 행은 빠진다** (``usable_forecast_window``). 지금 AUC 에는
      그런 행이 0건이라 값이 안 바뀐다 — 실측으로 확인했다 (12-31 관통 무변동).
    """
    return max(row["upper"] for row in usable_forecast_window(forecast, coverage_days))


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
    ]


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

    ``evidence_grade``가 ``SIM_FIXED``인 이유: IO명세 §2 예시는 ``OFFICIAL``이지만 그건
    **실제 KREI 발간물** 기준이다. 우리 코퍼스는 형식만 빌린 가상 문서라
    (``documents.json._전부_시뮬레이션``), 등급은 문서의 격이 아니라 **실제 데이터
    출처**를 따라 붙인다. 실문서로 갈아끼우면 여기가 ``OFFICIAL``이 된다.

    ``claim``이 주장 요약이 아닌 이유: 규칙은 본문을 요약할 수 없다. 문서 식별로 두고 실제
    주장은 ``evidence_detail``의 발췌가 **원문 그대로** 싣는다 — 규칙이 요약한 척하지 않는다.
    LLM이 붙으면 ``claim``이 요약으로 바뀌고 발췌는 그대로 남는다.
    """
    return [
        {
            "source": DOCUMENT_SOURCE,
            "claim": f"{doc['source']} {doc['doc_type']} — {doc['title']}",
            "ref_id": document_ref(doc["doc_id"]),
            "evidence_grade": "SIM_FIXED",
            "evidence_detail": f"{doc['published_at']} 발행 · 발췌: \"{doc['excerpt']}\"",
        }
        for doc in context_docs
    ]


def _adjustment_risks(adjustments: list[dict] | None) -> list[str]:
    """받았지만 **반영하지 않은** 조정안을 고지한다. 안 왔으면 아무 줄도 안 붙는다.

    🔴 **이 줄이 없으면 "값을 실어 주고 안 쓰는" 자리가 된다.** 마스터가 2회차에
      조정안을 실어 보내는데(``flow.py`` ``_purchase_input``) 우리가 조용히 안 쓰면,
      보내는 쪽은 **자기 제안이 반영된 줄 안다.** 반영 안 된 이유를 물을 기회조차 없다 —
      우리가 다른 파트에 지적했던 것과 같은 종류다 (#165 · #166).

    ``_context_risks`` 가 *"충분성을 아무도 묻지 않았다"* 를 고지하는 것과 같은 자리다:
    **하지 않은 일을 한 것처럼 보이게 두지 않는다.**

    ⚠️ 반영이 붙는 날 이 함수는 지운다 — 그때는 ``applied_adjustments`` 가 사실을
      말하므로, 이 줄이 남아 있으면 거짓이 된다.
    """
    if not adjustments:
        return []
    return [
        (
            f"조정안 {len(adjustments)}건을 받았으나 이번 실행에서 반영하지 않았다 — "
            "반영 규칙이 아직 정해지지 않았다"
        )
    ]


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

      ``max_price`` 는 **최댓값 하나로 정해지는 컷 기준**이다 (규칙 5). 창 어딘가에
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
    if not top.get("is_filled"):
        return []
    return [
        (
            f"매입 상한({top['upper']}원)을 정한 {top['date']}은 장이 서지 않아 "
            "앞 장날 값을 그대로 쓴 날이다"
        )
    ]


def _context_risks(loop_count: int, context_docs: list[dict], as_of: str) -> list[str]:
    """문서 수집에서 나온 유의사항. **②가 안 돈 날은 아무 줄도 안 붙는다.**

    판정 기준이 ``situation`` 문자열이 아니라 **``context_loop_count``**인 이유: 알고 싶은
    건 "그날이 uncertain인가"가 아니라 "문서를 실제로 찾아봤는가"다. situation으로 물으면
    ②의 실행 여부를 그래프 배선을 통해 **간접 추론**하게 되고, ``Situation``에 값이 늘거나
    분기가 바뀌면 조용히 어긋난다 (Codex 교차검증 지적). 루프 수는 그 사실을 직접 들고 있다.

    ②가 돌았는데 고지가 없으면 소비자는 "검토를 거친 근거"로 읽는다. 실제로는 우선순위
    목록을 순서대로 소진했을 뿐이고, **"이만하면 충분한가"를 아무도 묻지 않았다.**
    E3-3에서 일괄 fallback을 고지하기로 한 것과 같은 라벨/행동 불일치다.

    문구에 내부 단계 이름을 쓰지 않고, **하지 않은 일을 한 것처럼 적지도 않는다** — 발췌는
    문장 경계 파서가 아니라 서두 잘라내기라 "첫 문장"이라고 주장하지 않는다. 이 필드를
    읽는 쪽은 코드가 아니라 H1 승인 화면과 Critic이다 (계약서 §0).
    """
    if loop_count <= 0:
        return []  # ② 미실행 — "찾아보지 않았다"는 고지할 유의사항이 아니라 경로의 사실이다
    if not context_docs:
        return [
            (
                f"문서 {loop_count}종을 찾았으나 참조 가능한 발간물 0건 — "
                "문서 근거 없이 구성된 안이다"
            )
        ]
    return [
        (
            f"문서 {len(context_docs)}건 참조 — 규칙 기반 수집이라 "
            "문서 선별·충분성 판단은 미적용(우선순위 순서대로 로드). "
            "발췌는 관련 구절 선별 없이 각 문서 서두에서 기계적으로 뜬 것이다. "
            f"{_document_age(context_docs, as_of)}"
        )
    ]


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
                f"소진 한계 {decision['shelf_days']:.0f}일 = 상품 한계일 "
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


def _sourcing_risks(sourcing: list[dict], decision: dict) -> list[str]:
    """등급 배분에서 나온 유의사항. **미결로 건너뛴 검사도 여기 싣는다** (규칙 3).

    §4-⑦ 예시 출력의 risks("중품 1,500kg은 잔여신선도 6일 내 소진 필요 — 확정주문
    일정상 충족")를 재현한다.
    """
    if decision.get("blocked_by"):
        # 배정한 등급을 **이름으로** 적는다. 기준등급 시세가 없으면 ⑤가 다른 등급으로
        # 대체하므로, "기준등급으로 배정했다"고 쓰면 형식만 맞고 내용이 거짓인 근거가 된다.
        grade = decision.get("base_grade", "?")
        return [f"등급 배분 보류 — {decision['blocked_by']}. 전량 {grade} 단일 등급으로 배정했다"]
    if not decision.get("ratio"):
        # 중품 미사용. 판단 미적용 고지는 여기서도 살아야 한다 — 판단자가 있었는지
        # 없었는지는 배분 결과와 별개의 사실이다 (rationale 쪽과 같은 이유).
        return _mix_choice_risks(decision)
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
    return notes


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


def _split_decision(chosen: list[dict] | None) -> dict:
    """④가 첫 줄에 실어 보낸 분할 판단 근거. ⑤의 ``_sourcing_decision``과 같은 방식이다."""
    return chosen[0].get("decision", {}) if chosen else {}


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
                "source": "주문",
                "claim": (
                    f"안 총량 {decision['largest_total_kg']:,}kg ≥ 분할 임계 "
                    f"{decision['threshold_kg']:,}kg → {len(rounds)}회 분할"
                ),
                "ref_id": f"SO-{as_of}",
                "evidence_grade": "ASSUMED",
                "evidence_detail": (
                    "확정주문에서 파생해 하드 제약으로 클립한 안별 총량 — "
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
        misses.append(
            f"최대안 {decision.get('largest_total_kg', 0):,}kg < "
            f"임계 {decision.get('threshold_kg', 0):,}kg"
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


def build_payment_schedule(
    rounds: list[dict],
    sourcing: list[dict],
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

    ⚠️ **금액은 등급 구성에서 만든다.** 처음엔 전체 가중단가를 회차 수량에 곱했는데,
    그러면 **어떤 정수 kg 등급 구성으로도 재현되지 않는 금액**이 나온다 (Codex 교차검증).
    재무가 ``sourcing_plan``으로 검산하면 어긋난다 — ``_round_amounts``가 등급별 kg를
    회차에 배분해 실제 단가로 곱한다.

    ⚠️ **``payment_days``가 음수면 만들지 않는다.** 지급일이 매입일보다 앞서는 것은
    N5의 뜻(매입 후 며칠 뒤 지급)과 모순이고, 그대로 두면 ⑦도 같은 음수로 재계산해
    정상 판정한다 (Codex 교차검증).
    """
    if payment_days is None or payment_days < 0 or len(rounds) <= 1:
        return None

    amounts = _round_amounts(rounds, sourcing)
    schedule = []
    for item, amount in zip(rounds, amounts, strict=True):
        purchase_date = date.fromisoformat(item["date"])
        schedule.append(
            {
                "seq": item["seq"],
                "purchase_date": item["date"],
                "payment_date": (purchase_date + timedelta(days=payment_days)).isoformat(),
                "qty_kg": item["qty_kg"],
                "amount_krw": amount,
                "amount_max_krw": item["qty_kg"] * max_price,
                # ``amount_krw``를 **어떻게 추정했는가**다. 재무의 BASE/STRESS는 두 금액을
                # 각각 어느 Cashflow에 넣는지의 소비 프레이밍이라 축이 다르다.
                "basis": "as_of_unit_price",
            }
        )
    return schedule


def _payment_schedule_field(
    rounds: list[dict], sourcing: list[dict], max_price: int, payment_days: int | None
) -> dict:
    """실을 것이 있을 때만 키를 만든다 — ``None``을 담으면 "빈 계획"으로 읽힌다."""
    schedule = build_payment_schedule(rounds, sourcing, max_price, payment_days)
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
) -> list[str]:
    """분할에서 나온 유의사항. **timing 라벨과 실제 행동이 어긋나면 반드시 적는다** (규칙 3).

    회차가 하나로 끝나는 경로가 둘인데 이유가 다르다:

    1. **진입 자체를 안 함** — ①이 클립 전 추정 총량으로 축을 열었고 ④가 클립 후 실제
       총량으로 판정해 닫혔다 (§4-④ E3-3 확정 2가 "정상"이라고 한 경우다)
    2. **진입했는데 이 안이 못 버팀** — 0kg 회차나 겹치는 날짜가 나온다

    둘 다 조용히 넘기면 소비자가 라벨(timing)과 행동(일괄)의 불일치를 추적할 수 없다.
    첫 번째 경로는 Codex 교차검증에서 P1으로 잡혔다 — 처음엔 두 번째만 고지했었다.
    """
    if axis != TIMING_AXIS:
        return []  # quantity·mix 축 안은 애초에 분할 대상이 아니다
    if not decision.get("entered"):
        return [
            (
                f"timing 축 안이지만 분할 미진입({_entry_miss_reason(decision)})으로 일괄 — "
                "허용 축은 ①이 클립 전 추정 총량으로 열고, "
                "분할은 ④가 클립 후 실제 총량으로 판정한다"
            )
        ]
    reason = chosen and split_infeasible_reason(total_qty_kg, chosen, coverage_days)
    if reason:
        return [f"분할 불가({reason})로 일괄 전환 — timing 축 안이지만 회차는 하나다"]
    # 재배분 결과를 여기서 다시 계산한다 — 바로 위 ``split_infeasible_reason``과 같은
    # 방식이다. 순수 함수라 같은 입력이면 같은 답이고, 수량과 고지가 갈라질 수 없다.
    even = split_quantities(total_qty_kg, chosen or [])
    adjusted, cap_note = cap_constrained_quantities(
        even,
        arrival_dates(as_of, coverage_days, len(rounds), lead_days),
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
        return [f"{head} — {cap_note}"]
    return [head]


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


def package_scenarios(state: PurchaseAgentState) -> dict[str, Any]:
    """안별로 split·sourcing을 묶고 근거를 붙여 시나리오를 완성한다."""
    constraints = load_constraints()
    base = state["base_plan"]
    drafts = base["drafts"]
    axes = assign_axes(
        [d["label"] for d in drafts],
        state["allowed_axes"],
        constraints["allocation"]["aggressive_axis"],
    )
    lots = state["inventory"].get("lots")
    contract_price = state["contract_price"]  # 미수령이면 None — 마진 두 값이 함께 null이 된다
    decision = _sourcing_decision(state["sourcing_plan"])  # ⑤ 등급 배분 판단 근거
    split_choice = state["split_plan"]  # ④ 분할 유형·비율 (진입 안 했으면 None)
    # N4·수용량은 그날 하나뿐이라 안 루프 밖에서 한 번만 읽는다.
    # ``cap_by_date``는 **어댑터 경로에만** 있다 — mock 재고에는 없어서 None이고,
    # 그때 회차 조정은 일어나지 않는다 (부재가 정상 경로다).
    lead_days = pending_value(state, constraints, "inbound_lead_days")
    cap_by_date = state["inventory"].get("cap_by_date")
    split_decision = _split_decision(split_choice)
    # 시세 근거 좌표는 **관측일 기준**이고 그날 하나뿐이다. 안 루프 안에서 만들면 같은
    # 시세에서 나온 근거들이 서로 다른 좌표를 갖게 된다 (실제로 그랬다 — Codex 2차 지적).
    quote_ref = f"MQ-가락-{observed_date(state['market_quotes']) or state['date']}"

    scenarios = []
    dropped = []
    for draft in drafts:
        total = draft["total_qty_kg"]
        if total <= 0:
            # 하드 제약이 전량을 깎아낸 안. 스키마가 total_qty_kg > 0을 요구하므로 제안이
            # 될 수 없다. 조용히 사라지지 않게 사유를 남긴다 — 안이 왜 줄었는지가 소비자에게
            # 보여야 한다.
            binding = ", ".join(clip["constraint"] for clip in draft["clipped_by"]) or "미상"
            dropped.append(
                {
                    "label": draft["label"],
                    "reason": f"하드 제약({binding})으로 수량이 0까지 축소되어 제안 불가",
                }
            )
            continue
        sourcing = materialize_sourcing(total, state["sourcing_plan"])
        # 분할은 **timing 축을 받은 안에만** 붙는다 (§4-④ E3-3 확정 1). 전 안에 걸면
        # 세 안의 split 구조가 같아져 timing이 라벨로만 남는다 — §3.5.1-3이 막으려는 상태다.
        axis = axes[draft["label"]]
        chosen = split_choice if axis == TIMING_AXIS else None
        coverage_days = draft["coverage_days"]
        rounds = materialize_split(
            state["date"],
            total,
            chosen,
            coverage_days,
            lead_days=lead_days,
            cap_by_date=cap_by_date,
        )
        rationale_input = {**draft, "daily_demand_kg": base["daily_demand_kg"]}
        unit_price = _weighted_unit_price(sourcing, total)
        margin_warning, expected_margin_rate = compute_margin(unit_price, contract_price)
        scenarios.append(
            {
                "label": draft["label"],
                "strategy_type": axis,
                "coverage_days": draft["coverage_days"],
                "total_qty_kg": total,
                "total_amount_krw": sum(
                    line["qty_kg"] * line["grade_unit_price"] for line in sourcing
                ),
                "max_price": compute_max_price(state["forecast"], draft["coverage_days"]),
                # 규칙 5 — 계약단가 초과는 컷이 아니라 표시다.
                "margin_warning": margin_warning,
                "split_plan": rounds,
                "sourcing_plan": sourcing,
                # 분할 안이고 N5를 받은 날만 실린다 — 아니면 **키 자체가 없다**.
                **_payment_schedule_field(
                    rounds,
                    sourcing,
                    compute_max_price(state["forecast"], draft["coverage_days"]),
                    pending_value(state, constraints, "purchase_payment_days"),
                ),
                "expected_margin_rate": expected_margin_rate,
                "rationale": [
                    *_rationale(state, rationale_input, constraints, quote_ref),
                    *_context_rationale(state["context_docs"]),
                    *_sourcing_rationale(decision, quote_ref),
                    *_split_rationale(split_decision, rounds, state["forecast"], state["date"]),
                ],
                "risks": [
                    *_risks(draft, base["deferred_checks"], lots, state["date"]),
                    *_forecast_risks(state["forecast"], draft["coverage_days"]),
                    *_adjustment_risks(state.get("adjustments")),
                    *_context_risks(
                        state["context_loop_count"], state["context_docs"], state["date"]
                    ),
                    *_sourcing_risks(sourcing, decision),
                    *_split_risks(
                        split_decision,
                        axis,
                        total,
                        coverage_days,
                        chosen,
                        rounds,
                        as_of=state["date"],
                        lead_days=lead_days,
                        cap_by_date=cap_by_date,
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
