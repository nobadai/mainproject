"""⑦ self_check — 사중 일치 검사 + 컷 사유 기록 + 출력 조립 (상세설계 §4-⑦).

여기서 컷된 안은 ``rejected_reasons``에 ``{label, reason}``으로 남는다. 마지막에
``revalidate_for_output()``으로 계약을 한 번 더 확인한 뒤에야 출력이 만들어진다.

**환각 대조(LLM)는 Epic 3다.** 지금 있는 건 전부 계산 검사다 — 규칙 6대로 숫자·제약은
순수 함수가 소유하고, LLM은 rationale의 claim이 원본과 맞는지 보는 데만 쓴다.
"""

from datetime import date, timedelta
from itertools import pairwise
from typing import Any, NamedTuple

from app.purchase_agent import AGENT_VERSION
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.collect_context import TRUNCATION_MARK
from app.purchase_agent.nodes.draft_plan import (
    pending_value,
    purchase_budget_krw,
    warehouse_cap_kg,
)
from app.purchase_agent.schemas import (
    DOCUMENT_SOURCE,
    FIXED_MARKET,
    PurchaseProposal,
    document_ref,
    is_document_ref,
    revalidate_for_output,
)
from app.purchase_agent.state import PurchaseAgentState


def check_quadruple_match(scenario: dict) -> str | None:
    """사중 일치 — 수량 3축 + 금액 1축 (현서님 검토 §3-2).

    금액 축이 재무 cap 대조값이고, 등급 배분이 수량↔금액 변환 계수라 빠뜨릴 수 없다.
    스키마도 같은 검사를 하지만 여기서 먼저 본다 — 스키마는 예외를 던져 제안 전체를
    죽이고, 여기서는 **그 안만 컷하고 나머지를 살린다**.
    """
    total = scenario["total_qty_kg"]
    split_total = sum(item["qty_kg"] for item in scenario["split_plan"])
    sourcing_total = sum(item["qty_kg"] for item in scenario["sourcing_plan"])
    if total != split_total:
        return f"수량 불일치: total {total:,} != split 합 {split_total:,}"
    if total != sourcing_total:
        return f"수량 불일치: total {total:,} != sourcing 합 {sourcing_total:,}"
    amount = sum(line["qty_kg"] * line["grade_unit_price"] for line in scenario["sourcing_plan"])
    if scenario["total_amount_krw"] != amount:
        return f"금액 불일치: total {scenario['total_amount_krw']:,} != sourcing 합 {amount:,}"
    return None


def check_axis_allowed(scenario: dict, allowed_axes: list[str]) -> str | None:
    """그날 허용 축 안의 값인가 (규칙 4 · 정의서 §3.5.1-2)."""
    if scenario["strategy_type"] not in allowed_axes:
        return f"허용되지 않은 축: {scenario['strategy_type']} (그날 허용 {allowed_axes})"
    return None


def check_prices_exist(scenario: dict, market_quotes: list[dict]) -> str | None:
    """등급·단가가 **당일 시세에 실재**하는가 (규칙 4). 지어낸 단가를 막는 유일한 검사다.

    **시장까지 함께 본다.** ``(등급, 가격)``만 대조하면 다른 시장의 가격에 ``market="가락"``을
    붙여도 통과한다 — 형식은 맞고 내용은 거짓인 출력이 만들어진다.
    """
    real = {(quote["market"], quote["grade"], quote["price"]) for quote in market_quotes}
    for line in scenario["sourcing_plan"]:
        if line["market"] != FIXED_MARKET:
            return f"허용되지 않은 시장: {line['market']} ({FIXED_MARKET} 고정)"
        key = (line["market"], line["grade"], line["grade_unit_price"])
        if key not in real:
            return (
                f"당일 시세에 없는 단가: {line['market']} {line['grade']} "
                f"{line['grade_unit_price']:,}원 (실재 {sorted(real)})"
            )
    return None


def check_max_price(scenario: dict) -> str | None:
    """매입단가가 max_price를 넘는가.

    max_price는 예측 q90 기반 **하드 상한**이다 (§4-⑦). 계약단가(contract_price) 초과와
    혼동하지 않는다 — 그쪽은 컷이 아니라 margin_warning 표시일 뿐이다 (규칙 5).
    """
    ceiling = scenario["max_price"]
    over = [line for line in scenario["sourcing_plan"] if line["grade_unit_price"] > ceiling]
    if over:
        worst = max(line["grade_unit_price"] for line in over)
        return f"매입단가 {worst:,}원이 max_price {scenario['max_price']:,}원 초과"
    return None


def check_warehouse_capacity(scenario: dict, inventory: dict) -> str | None:
    """창고 **총량 축** — 총수량 ≤ 창고 여유 + 외부임차 한도.

    ⚠️ **날짜 축은 여기가 아니라** ``check_arrival_capacity`` 가 본다. 같은 자원(창고)의
    두 축이라 둘 다 있어야 하고, 둘 다 통과해야 안이 산다::

        총량 축   Σ회차 ≤ 여유 + 임차            ← 이 함수
        날짜 축   도착일까지 누적 ≤ 그날 여유     ← check_arrival_capacity

    🔴 **전에는 여기 주석이 "날짜별 검사는 하지 않는다 — N4가 NULL이라"였다.** 그 사유가
      낡았다: N4는 물류가 보내고 State 최상위에 실린다(#58). 사유가 사라진 뒤에도 주석이
      남아 있어, 검사가 없는 것이 **여전히 옳은 것처럼** 읽혔다 (#93).

    ⚠️ **공용화 지점.** §4-⑦은 이 검사를 공용 모듈의 ``check_warehouse_capacity()``로 두고
    매입·T3·Critic이 import하라고 규정한다 — "자체 구현 금지, 매입 통과·T3 FAIL 반복 방지".
    그 모듈이 아직 없어 여기 있다. 생기면 이 함수를 지우고 import로 바꾼다 (현서님 협의 항목).
    상한 식 자체는 ③과 공유한다(``warehouse_cap_kg``) — 두 곳에 복제하면 한쪽만 바뀐다.
    """
    cap = warehouse_cap_kg(inventory)
    if scenario["total_qty_kg"] > cap:
        return f"창고 초과: {scenario['total_qty_kg']:,}kg > 여유+임차 {cap:,}kg"
    return None


#: 날짜 축 검사를 **하지 않은** 사유 중 **문장이 고정된 것**들. 나머지 둘
#: (``out_of_window`` · ``missing``)은 날짜를 이름으로 짚어야 해서 아래에서 만든다.
#:
#: ⚠️ ⑥ ``CAP_BLOCK_REASONS`` 와 **머리는 같고 꼬리가 다르다.** 저쪽은 *"그래서 회차를
#:   균등하게 나눴다"*(재배분을 안 했다)이고 여기는 *"그래서 컷하지 않았다"*(판정을 안 했다)다.
#:   같은 원인이 두 단계에서 서로 다른 결과를 낳으므로 문장도 갈린다 — 복제가 아니다.
#:
#: 🔴 **고지는 여기서만 나간다** (#93 결정). 전에는 ⑥이 말했는데, ⑥의 고지 경로가
#:   timing 축 전용이라 **quantity 축 안은 검사도 고지도 없었다.** 검사하는 쪽과 말하는
#:   쪽이 갈라져 있던 것이 그 구멍의 원인이다.
ARRIVAL_SKIP_REASONS = {
    "no_cap": (
        "회차별 창고 여유 검사를 하지 않았다 — 물류에서 날짜별 입고 여유를 받지 못했다. "
        "여유를 0으로 가정하지 않았으므로 이 안은 날짜 축으로 판정받지 않았다"
    ),
    "no_lead": (
        "회차별 창고 여유 검사를 하지 않았다 — 입고 소요일이 정해지지 않아 도착일을 "
        "계산할 수 없다. 이 안은 날짜 축으로 판정받지 않았다"
    ),
}

#: 물류가 날짜별 여유를 **계산한 구간의 길이**를 밝히는 칸 (물류 payload).
#:
#: ⚠️ **이름을 상수로 둔 것은 오타 방지이지 값 소유가 아니다.** 값(18)은 물류
#:   ``tools.CAP_BY_DATE_WINDOW_DAYS`` 소유이고 우리는 payload 로 받은 것만 쓴다 —
#:   여기 숫자를 적으면 물류가 창을 바꾼 날 우리만 옛 창으로 판정한다 (규칙 7).
CAP_WINDOW_KEY = "cap_by_date_window_days"


class ArrivalCapacity(NamedTuple):
    """날짜 축 판정 결과. **컷과 미검사를 한 값으로 뭉치지 않는다.**

    둘 다 "통과가 아님"이지만 하나는 안을 죽이고 하나는 안 죽인다. 한 문자열로 돌려주면
    호출부가 *"이걸 컷 사유로 쓸 수 있나"* 를 문면으로 판단하게 되고, 문구를 다듬는 날
    컷이 조용히 멈춘다.
    """

    violation: str | None = None
    """컷 사유. 채워지면 그 안은 죽는다."""

    skipped: str | None = None
    """미검사 고지. 채워지면 안은 살고 risks 에 한 줄이 붙는다."""


def _whole_days(value: Any) -> int | None:
    """일 단위 정수로 읽는다. 아니면 ``None`` — **0으로 대체하지 않는다** (규칙 3).

    🔴 **``isinstance(value, int)`` 로 보면 안 된다.** 물류는 숫자를 ``_num()`` 으로
      싸서 보내므로 실제 payload 의 ``inbound_lead_days`` 는 **``2.0``(float)** 이다
      (실측 2026-09-03 · 12-31 피마늘). ``int`` 만 받으면 **실운영에서 창을 한 번도
      못 만들고** 늘 *"가르지 못했다"* 로 떨어진다 — 검사가 있는데 안 도는 상태다.

    ★ 판정 규칙은 ``adapter._arrival_input_problems`` 와 **같다**: 수치이고 소수부가
      없으면 정수로 읽는다. 두 곳이 다르면 어댑터가 통과시킨 값을 여기서 버린다.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != int(value):
        return None
    return int(value)


def cap_window(state: PurchaseAgentState) -> tuple[str, str] | None:
    """물류가 날짜별 여유를 **계산한 구간** ``[시작, 끝]`` (ISO 문자열, 양끝 포함).

    물류 ``tools.build_cap_window`` 와 **같은 식**이다::

        start = as_of + N4
        dates = [start + d for d in range(window_days)]      # 끝 = start + (window_days - 1)

      N4=2 · 창 18일이면 **D+2 ~ D+19** 다 (실측 2026-09-03).

    🔴 **두 값을 같은 dict 에서 읽는다.** ``inbound_lead_days`` 와 ``cap_by_date_window_days``
      는 물류가 **같은 payload 에 함께** 실어 보내고, 물류는 그 둘로 창을 만들었다.
      한쪽을 설정 기본값(``pending``)에서 읽으면 **물류가 쓴 적 없는 창**을 재구성하게 된다.

    못 만들면 ``None`` — 창을 지어내지 않는다 (규칙 3). 창을 모르면 "창 밖"과
    "창 안 누락"을 가를 수 없고, **가를 수 없다는 사실 자체가 고지 대상**이다.
    """
    inventory = state["inventory"]
    lead = _whole_days(inventory.get("inbound_lead_days"))
    window = _whole_days(inventory.get(CAP_WINDOW_KEY))
    if lead is None or lead < 0 or window is None or window < 1:
        return None
    start = date.fromisoformat(state["date"]) + timedelta(days=lead)
    return start.isoformat(), (start + timedelta(days=window - 1)).isoformat()


def _unknown_reason(unknown: list[str], state: PurchaseAgentState) -> str:
    """값이 안 온 도착일들을 **두 갈래로 갈라** 문장을 만든다 (물류 규약 2026-09-03).

    🔴 **뭉치면 안 되는 이유.** 물류 규약은 셋을 다른 상태로 규정한다::

        키 존재 + 값 0      계산 결과 입고 가능량이 0        → 판정 대상
        창 안인데 키 누락    계산 누락 또는 미결              → **고쳐야 할 것**
        창 밖               계산 대상이 아니다               → **정상**

      전에는 뒤 둘을 *"조회 기간 밖이거나 값이 비어 있다"* 한 문장으로 냈다. 읽는 사람이
      **고쳐야 할 것과 정상을 구분할 수 없었다** — 매일 같은 문장이 나가면 둘 다 배경이 된다.

    ★ **행동은 셋 다 같다 — 컷하지 않는다.** 갈리는 것은 문장뿐이다. 창 밖이라고 통과로
      치지 않고, 누락이라고 죽이지도 않는다. 둘 다 *"판정하지 않았다"* 이고 **왜** 가 다르다.

    창을 못 만들면(``cap_window`` 가 ``None``) 갈리지 않는다. 그때는 **가를 수 없다는
    사실을 밝힌다** — 물류가 창 길이를 payload 에 싣기로 했으므로(2026-09-03), 없으면
    그것이 곧 확인할 거리다.
    """
    window = cap_window(state)
    if window is None:
        return (
            f"회차별 창고 여유 검사를 하지 않았다 — 도착일 {', '.join(unknown)}의 여유를 "
            "물류에서 받지 못했다. 계산 구간의 길이를 함께 받지 못해 **구간 밖이라 대상이 "
            "아닌 것인지, 구간 안인데 값이 빠진 것인지 가르지 못했다.** 받지 못한 날을 "
            "여유 0으로 읽지 않았으므로 이 안은 날짜 축으로 판정받지 않았다"
        )

    start, end = window
    outside = [day for day in unknown if day < start or day > end]
    inside = [day for day in unknown if start <= day <= end]
    span = f"물류가 계산한 구간은 {start}~{end}이다"

    if inside:
        head = (
            f"회차별 창고 여유 검사를 하지 않았다 — 도착일 {', '.join(inside)}가 "
            f"**계산 구간 안인데 여유 값이 오지 않았다**({span}). 계산이 빠졌거나 아직 "
            "정해지지 않은 것이라 확인이 필요하다"
        )
        if outside:
            head += f". 도착일 {', '.join(outside)}는 구간 밖이라 애초에 계산 대상이 아니다"
        return head + (
            ". 받지 못한 날을 여유 0으로 읽지 않았으므로 이 안은 날짜 축으로 판정받지 않았다"
        )

    return (
        f"회차별 창고 여유 검사를 하지 않았다 — 도착일 {', '.join(outside)}가 "
        f"**계산 구간 밖이라 애초에 대상이 아니다**({span}). 빠진 값이 아니라 물류가 "
        "그 날짜까지는 계산하지 않는다는 뜻이다. 구간 밖을 여유 0으로 읽지 않았으므로 "
        "이 안은 날짜 축으로 판정받지 않았다"
    )


def arrival_capacity(scenario: dict, state: PurchaseAgentState) -> ArrivalCapacity:
    """창고 **날짜 축** — 도착일까지의 누적이 그날 여유를 넘는가 (§4-⑦ · #93).

    ★ **``split_plan[].expected_arrival_date`` 를 읽는다. 다시 계산하지 않는다.**
      PR #141로 안에 이미 실려 있다(``schemas.py``). 도착일을 여기서 또 만들면
      ⑥이 옮긴 결과와 ⑦이 검사하는 대상이 어긋날 수 있는데, 실린 값을 읽으면
      **그 어긋남이 구조적으로 불가능해진다.**

    🔴 **``chosen``·``axis``·``entered`` 를 보지 않는다.** 회차가 하나든 둘이든 같은
      코드가 돈다. ⑥의 재배분은 timing 축 분할 진입 경로에서만 도는데(``materialize_split``
      가 ``if not chosen: return`` 으로 조기 반환한다), 컷까지 그 경로에만 두면
      **"회차를 안 나눌수록 검사를 안 받는" 구조**가 된다 — 실측으로 보수 2,571kg ·
      기본 6,429kg 이 여유 100kg 을 넘는데 risks 줄조차 없었다 (#93 재현).
      ``_rounds()`` 가 일괄·분할불가·정상 세 경로를 전부 지나므로 **1회차 안도
      도착일을 갖고 있다** — 그래서 같은 코드로 검사할 수 있다.

    ★ **누적으로 본다.** ``cap_by_date[d]`` 는 그날의 *여유 공간*이고, 물류는 **기존
      일정만** 재생해 그 값을 낸다 (``logistics/tools.py`` ``calculate_cap_by_date``:
      guaranteed − projected_occupancy). **우리가 새로 넣을 회차는 거기 없다.**
      날짜마다 독립으로 비교하면 1회차가 아직 창고에 있는데도 2회차가 그날 상한을
      통째로 쓰는 계획이 통과한다. ⑥ ``cap_constrained_quantities`` 와 같은 셈이다.

    ⚠️ **한계 — 중간 출고를 해제하지 않는다.**
      회차를 도착일 순으로 **더하기만 하고 빼지 않는다.** 중간에 확정 출고가 앞 회차를
      실제로 소진해도 해제하지 않는다. 물류 ``_available_capacity`` 도 같은 방식이고
      (``arrival <= day``), **방향은 안전하다 — 덜 사게 틀린다.**

      *"왜 이렇게 빡빡한가"* 가 나오면 원인이 여기일 수 있다
      (물류 지적 2026-09-03 · 물류 미결 §0-3).

      ★ **risks 에는 안 싣는다.** 매일 모든 안에 붙으면 신호가 죽는다 — 이건 *"이 안이
        위험하다"* 가 아니라 우리 검사 방식의 성질이다. 되짚을 실마리는 컷 사유에 이미
        있다: *"누적 N kg … M회차 …까지 더한 값"*.

    🔴 **왜 ``orchestrator/band.py`` 의 ``check_occupancy_by_date`` 를 안 쓰나** (2026-09-03 판단).
      §4-⑦이 "자체 구현 금지"라고 했고 그 함수가 실재하는데도 안 쓴 이유가 넷이다.
      나중에 *"왜 안 썼지"* 로 되돌리지 않도록 적어 둔다::

        ㄱ. cap_by_date 의 뜻이 다르다
              물류  guaranteed − projected_occupancy       → 잔여 여유 (net)
              band  confirmed_occupancy[d] + arrived ≤ cap → 총 용량 (gross)
            band 는 점유를 따로 더하는데 우리가 받는 값은 이미 뺀 값이다. 그대로
            넘기면 이중 계상이거나, confirmed 가 비어 **우연히** 맞는 상태가 된다.
        ㄴ. 타입이 다르다 — ``Band.cap_by_date_kg`` 는 ``date`` 키, 우리는 ISO 문자열.
            조회가 전부 미스 나면 값이 와 있는데도 "받지 못했다"로 고지된다.
        ㄷ. dataclass 셋을 지어내야 부를 수 있다 — ``ClipResult``·``Band``·``T0Snapshot``.
            ``T0Snapshot`` 만 해도 forecasts·spot_price·finance·budget 을 요구하는데
            이 검사와 무관하다. **가짜 값을 채워야 부를 수 있는 함수는 공용 모듈이 아니다.**
        ㄹ. 소유가 저쪽이다 — band.py 를 import 하는 것은 critic/* 과 orchestrator/graph
            뿐이고, ``tests/master/test_no_orchestrator_runtime.py`` 는 마스터에 대해
            ``app.orchestrator.band`` 를 금지 목록에 올려 뒀다.

      ㄱ이 풀리고 공용 모듈이 생기면 **이 함수를 지우고 import 로 바꾼다** —
      ``check_warehouse_capacity`` 와 같은 약속이다.
    """
    cap_by_date = state["inventory"].get("cap_by_date")
    if cap_by_date is None:
        return ArrivalCapacity(skipped=ARRIVAL_SKIP_REASONS["no_cap"])

    rounds = scenario["split_plan"]
    arrivals = [item.get("expected_arrival_date") for item in rounds]
    if any(day is None for day in arrivals):
        # N4 미결이면 ⑥이 전 회차를 None 으로 채운다. 0으로 읽지 않는다 (규칙 3).
        return ArrivalCapacity(skipped=ARRIVAL_SKIP_REASONS["no_lead"])

    unknown = [day for day in arrivals if cap_by_date.get(day) is None]
    if unknown:
        # **하나라도 모르면 아무 회차도 판정하지 않는다.** 아는 날짜만 컷하면 판정이
        # "어느 도착일이 우연히 계산 구간 안이었나"에 좌우된다 — ⑥이 재배분을 통째로
        # 포기하는 것과 같은 이유다.
        return ArrivalCapacity(skipped=_unknown_reason(unknown, state))

    occupied = 0
    for item, day in zip(rounds, arrivals, strict=True):
        occupied += item["qty_kg"]
        # 수용량은 **상한**이라 내림한다 — ⑥·``warehouse_cap_kg`` 와 같은 방향이다.
        cap = int(cap_by_date[day])
        if occupied > cap:
            # 🔴 **정도를 보지 않는다.** cap_by_date 는 물류 guaranteed 기반 하드 제약이라
            #   넘으면 물리적으로 안 들어간다. 완화 임계를 두면 물류 값을 우리가 무르는
            #   것이 되고, 총량 축(``check_warehouse_capacity``)이 1kg 초과도 컷하는 것과
            #   기준이 갈린다 (#93 결정).
            return ArrivalCapacity(
                violation=(
                    f"날짜별 창고 초과: {day} 도착 누적 {occupied:,}kg > 그날 여유 {cap:,}kg "
                    f"({item['seq']}회차 {item['qty_kg']:,}kg까지 더한 값)"
                )
            )
    return ArrivalCapacity()


def check_arrival_capacity(scenario: dict, state: PurchaseAgentState) -> str | None:
    """검사 체인용 얇은 껍데기 — 컷 사유만 돌려준다.

    미검사 고지는 ``self_check`` 가 ``arrival_capacity`` 에서 직접 꺼내 risks 에 얹는다.
    """
    return arrival_capacity(scenario, state).violation


def check_cash_ceiling(scenario: dict, state: PurchaseAgentState, constraints: dict) -> str | None:
    """매입액 ≤ 매입 가능액.

    상한을 ③과 **같은 함수**(``purchase_budget_krw``)로 구한다. 두 노드가 각자 계산하면
    재무 cap 수신 여부에 따라 한쪽만 바뀌고, ③이 통과시킨 안을 ⑦이 컷하는 상태가 된다.
    """
    budget = purchase_budget_krw(state, constraints)
    if scenario["total_amount_krw"] > budget:
        return f"현금 초과: {scenario['total_amount_krw']:,}원 > 매입 가능액 {budget:,.0f}원"
    return None


def check_split_dates(scenario: dict, as_of: str) -> str | None:
    """seq 1의 date는 as_of, seq는 1부터 연속, **날짜는 앞으로만 간다** (IO명세 §2).

    날짜 검사는 ④가 실제로 분할하면서 붙었다. 회차가 하나뿐이던 동안에는 순서를 어길
    방법이 없었지만, 2회차가 생기면 "같은 날 두 번"이 통과할 수 있다 — 그건 분할이 아니라
    같은 매입을 두 줄로 적은 것이고, 사중 일치는 멀쩡히 통과한다.

    ISO 날짜 문자열은 사전순 비교가 곧 시간순 비교라 그대로 비교한다.
    스키마도 같은 검사를 하지만 거기서는 제안 전체가 죽고, 여기서는 **그 안만 컷한다**.
    """
    rounds = scenario["split_plan"]
    if rounds[0]["date"] != as_of:
        return f"1회차 날짜가 as_of와 다름: {rounds[0]['date']} != {as_of}"
    if [item["seq"] for item in rounds] != list(range(1, len(rounds) + 1)):
        return f"회차 번호가 연속이 아님: {[item['seq'] for item in rounds]}"
    dates = [item["date"] for item in rounds]
    if any(earlier >= later for earlier, later in pairwise(dates)):
        return f"회차 날짜가 앞으로 가지 않음: {dates}"
    return None


def check_document_refs(scenario: dict, context_docs: list[dict]) -> str | None:
    """인용한 문서가 **실제로 읽은 것인가** (§4-⑦ 근거 환각 대조 중 계산으로 되는 부분).

    ``check_prices_exist``가 단가에 대해 하는 일을 문서에 대해 한다 — 지어낸 ``DOC-``을
    막는 유일한 검사다. 등급·단가는 당일 시세에 실재해야 하고, 문서 근거는 그날 ②가
    실제로 로드한 것이어야 한다.

    **역방향은 검사하지 않는다.** "읽었는데 근거에 안 썼다"는 위반이 아니다 — ② 스텁
    시절부터 그 상태를 **의도적으로 구분 가능하게** 두었다("문서를 읽었는데 근거에 안 썼다"와
    "아직 안 읽는다"가 출력에서 구분된다). 컷해버리면 그 구분이 사라지고, ``context_docs_used``와
    rationale이 항상 같아져 두 필드 중 하나가 무의미해진다.

    **``source``와 ``ref_id`` 접두어 중 하나만 봐서는 안 된다.** 처음엔 ``source == "문서ID"``만
    봤는데, Codex 교차검증이 P1을 짚었다 — 출처를 "예측"으로 적고 ``ref_id``에 ``"DOC-999"``를
    넣으면 검사를 통째로 빠져나가고 스키마도 둘의 정합을 요구하지 않는다. 재현해 확인했다.
    그래서 **둘 중 하나라도 문서를 가리키면** 문서 근거로 보고, 이어서 **둘이 어긋난 것 자체**를
    막는다. 어긋난 항목은 어느 검사에도 안 걸리는 사각지대였다.
    """
    loaded = {document_ref(doc["doc_id"]) for doc in context_docs}
    cited: set[str] = set()
    for item in scenario["rationale"]:
        by_source = item["source"] == DOCUMENT_SOURCE
        by_ref = is_document_ref(item["ref_id"])
        if by_source != by_ref:
            return (
                f"문서 근거 표기 불일치: source={item['source']!r} / ref_id={item['ref_id']!r} — "
                f"문서 참조는 source가 {DOCUMENT_SOURCE!r}이고 ref_id가 DOC- 로 시작해야 한다"
            )
        if by_source:
            cited.add(item["ref_id"])
    unknown = sorted(cited - loaded)
    if unknown:
        return f"읽지 않은 문서를 인용: {unknown} (그날 로드분 {sorted(loaded)})"
    return None


def check_excerpt_fidelity(scenario: dict, context_docs: list[dict]) -> str | None:
    """인용 발췌가 **원문에 실재하는 문자열인가** (§4-⑦ 근거 환각 대조 · 현서님 2차 합의 8/26).

    현서님 회신(8/26)으로 역할 경계가 정해졌다: **문서 원문 대조는 매입 소유다.** Critic은
    월보·기상 문서를 스냅샷으로 받지 않기로 해(8/25 B4-8) 원문 접근권이 없고, 접근권 없는
    쪽은 이 검사를 구조적으로 수행할 수 없다. 대체 불가능하므로 여기가 유일한 자리다.

    **오늘은 이 검사가 통과할 수밖에 없다.** ②의 발췌가 서두 잘라내기라 원문 문자가 변조될
    길이 없기 때문이다. 그래도 지금 넣는 이유는, ②·⑥에 LLM이 붙어 "구절 선별"과 "claim
    요약"이 생기는 순간(E3-5 이후) **이 검사가 없으면 환각이 그대로 출력에 실리기 때문**이다.
    검사를 나중에 만들면 그 사이 산출물은 검증된 적이 없는 채로 남는다.

    두 지점을 본다 — ②가 원문에서 떴는가, ⑥이 그걸 그대로 실었는가:

    1. ``excerpt`` ⊆ ``content``  — ②의 발췌가 원문에서 온 문자열인가
    2. ``excerpt`` ⊆ ``evidence_detail`` — ⑥이 옮기며 고치지 않았는가

    **``substring``이지 ``prefix``가 아니다.** 지금 발췌는 항상 서두라 접두어 검사로도
    통과하지만, LLM이 구절을 고르기 시작하면 발췌는 본문 중간에서 온다. 오늘의 구현이
    아니라 **계약이 약속하는 것**에 맞춰 검사한다.

    절단 표시는 떼고 맞춘다 — ``…``는 ②가 붙인 표식이지 원문 문자가 아니다.
    접미사를 보고 판별하지 않고 ``excerpt_truncated`` 플래그를 쓰는 이유는
    ``leading_excerpt`` docstring에 있다.
    """
    by_ref = {document_ref(doc["doc_id"]): doc for doc in context_docs}
    for item in scenario["rationale"]:
        if item["source"] != DOCUMENT_SOURCE:
            continue
        doc = by_ref.get(item["ref_id"])
        if doc is None:
            # 검사 순서상 check_document_refs가 먼저 잡는 상태다. 순서에 기대지 않고
            # 여기서도 막는다 — 체인 순서가 바뀌면 조용히 통과하는 자리가 된다.
            return f"인용한 문서를 찾을 수 없어 발췌 대조 불가: {item['ref_id']}"
        excerpt = doc["excerpt"]
        if doc.get("excerpt_truncated"):
            excerpt = excerpt.removesuffix(TRUNCATION_MARK)
        if excerpt not in doc["content"]:
            return f"발췌가 원문에 없다: {item['ref_id']} — 인용 {excerpt[:40]!r}"
        if excerpt not in item["evidence_detail"]:
            return (
                f"근거 문구가 로드한 발췌와 다르다: {item['ref_id']} — "
                f"발췌 {excerpt[:40]!r}가 evidence_detail에 없다"
            )
    return None


def check_document_publication(
    scenario: dict, context_docs: list[dict], as_of: str
) -> str | None:
    """인용 문서가 **as_of 시점에 실재하는 발행물인가** (규칙 1 look-ahead 방어).

    포트가 이미 ``published_at <= as_of``로 거른다(``mocks._load.filter_by_published_at``).
    그런데도 출력 경계에서 한 번 더 보는 이유는, **필터를 통과하는 것과 출력이 지키는 것이
    다른 약속**이기 때문이다 — 스키마의 ``DOC-`` backstop을 ⑦ 검사와 별개로 둔 것과 같은
    자리다. 9/4에 9/5 발행 문서(DOC-6)를 인용하면 백테스트 성적이 무효가 되는데, 그건
    사업적 판단이 아니라 버그이므로 컷 사유로 남긴다.

    ``published_at``이 **없는** 것도 위반이다. 없으면 비교 자체가 불가능한데 조용히
    넘기면 "검사했다"가 거짓이 된다 — 로더가 ``published_at`` 없는 문서를 적재 거부하는
    것과 같은 이유다 (IO명세 §1-⑥).

    ISO 날짜 문자열은 사전순 비교가 곧 시간순이라 ``date`` 변환 없이 비교한다.
    """
    by_ref = {document_ref(doc["doc_id"]): doc for doc in context_docs}
    for item in scenario["rationale"]:
        if item["source"] != DOCUMENT_SOURCE:
            continue
        doc = by_ref.get(item["ref_id"])
        if doc is None:
            return f"인용한 문서를 찾을 수 없어 발행일 대조 불가: {item['ref_id']}"
        published_at = doc.get("published_at")
        if not published_at:
            return f"발행일 없는 문서를 인용: {item['ref_id']} — as_of 대조가 불가능하다"
        if published_at > as_of:
            return (
                f"as_of 이후 발행 문서를 인용: {item['ref_id']} "
                f"(발행 {published_at} > as_of {as_of}) — look-ahead"
            )
    return None


def check_payment_schedule(scenario: dict, state: PurchaseAgentState) -> str | None:
    """지급 계획이 **분할·총량·총액과 어긋나지 않는가** (재무 확정분 · 제안 §3.2 항등식 5).

    재무가 이 값을 자기 Cashflow에 얹어 검증하므로(회신 §6), 여기서 어긋난 채 나가면
    **재무 판정이 틀린 입력 위에서 이뤄진다.** 어느 쪽도 에러를 내지 않는다.

    다섯 가지를 본다:

    1. ``Σ qty_kg == total_qty_kg``            — 사중 일치의 지급 축 판
    2. ``Σ amount_krw == total_amount_krw``    — BASE Cashflow의 전제
    3. ``purchase_date == split_plan[i].date`` — seq 대응. 어긋나면 다른 회차의 돈이 된다
    4. ``payment_date == purchase_date + N5``  — calendar day, 영업일 보정 없음
    5. **일괄 안에는 키가 없다**               — 있으면 실을 것이 없는데 실은 것이다

    N5가 미결이면 애초에 만들어지지 않으므로(``build_payment_schedule``) 검사 대상도
    아니다 — 그 사실은 ``deferred_checks``가 싣는다.
    """
    schedule = scenario.get("payment_schedule")
    rounds = scenario["split_plan"]
    payment_days = pending_value(state, load_constraints(), "purchase_payment_days")
    #: N5를 받았고 분할이면 **있어야 한다.** 없으면 만들어야 할 것이 사라진 것이다.
    expected = payment_days is not None and payment_days >= 0 and len(rounds) > 1

    if schedule is None:
        # ⚠️ 처음엔 여기서 무조건 통과시켰는데, **키를 지우면 검사가 통째로 빠졌다**
        # (Codex 교차검증 P1). 있어야 하는 날 없는 것도 위반이다.
        if expected:
            return f"분할 {len(rounds)}회인데 payment_schedule이 없다 (N5={payment_days} 수신)"
        return None
    if not expected:
        # N5가 미결이거나 일괄인데 실렸다 — 계산할 수 없거나 실을 것이 없는 값이다.
        reason = "일괄 안" if len(rounds) <= 1 else f"N5 미결(={payment_days})"
        return f"{reason}에 payment_schedule이 실렸다 — 만들 수 없는 값이다"
    if len(schedule) != len(rounds):
        return f"지급 계획 회차 수 불일치: {len(schedule)}건 vs 분할 {len(rounds)}회"

    qty = sum(row["qty_kg"] for row in schedule)
    if qty != scenario["total_qty_kg"]:
        return f"지급 계획 수량 합 {qty:,}kg ≠ 총량 {scenario['total_qty_kg']:,}kg"

    amount = sum(row["amount_krw"] for row in schedule)
    if amount != scenario["total_amount_krw"]:
        return f"지급 계획 금액 합 {amount:,}원 ≠ 총액 {scenario['total_amount_krw']:,}원"

    for row, item in zip(schedule, rounds, strict=True):
        if row["seq"] != item["seq"] or row["purchase_date"] != item["date"]:
            return (
                f"지급 계획이 분할과 어긋난다: seq {row['seq']}/{item['seq']} · "
                f"{row['purchase_date']}/{item['date']}"
            )
        # ⚠️ **회차별 수량도 본다.** 합만 보면 1회차에서 빼 2회차에 더하는 이동이
        # 통과하고, 그러면 **BASE 현금유출이 잘못된 날짜에 배치된다** (Codex 교차검증 P1).
        if row["qty_kg"] != item["qty_kg"]:
            return (
                f"회차 {item['seq']} 수량이 분할과 다르다: "
                f"{row['qty_kg']:,}kg ≠ {item['qty_kg']:,}kg"
            )
        due = date.fromisoformat(row["purchase_date"]) + timedelta(days=payment_days)
        if row["payment_date"] != due.isoformat():
            return (
                f"지급일이 매입일 + N5({payment_days})와 다르다: "
                f"{row['payment_date']} ≠ {due.isoformat()}"
            )
        # ⚠️ **STRESS 금액을 검증한다.** 재무가 이 값을 그대로 STRESS Cashflow에 쓰므로
        # 틀리면 REVIEW/FAIL이 PASS로 왜곡된다 (Codex 교차검증 P1).
        stress = row["qty_kg"] * scenario["max_price"]
        if row["amount_max_krw"] != stress:
            return (
                f"회차 {item['seq']} STRESS 금액이 수량×상한가와 다르다: "
                f"{row['amount_max_krw']:,} ≠ {stress:,}"
            )
    return None


def check_axis_diversity(scenarios: list[dict], allowed_axes: list[str]) -> str | None:
    """전 안이 동일 축이면 반려 (정의서 §3.5.1-3 — "코드가 최종 중복 검사 ... (self_check)").

    **축이 하나뿐인 날은 면제한다.** 하락일처럼 timing 트리거가 미달하고 mix가 편중으로
    게이팅되면 남는 축이 quantity뿐이라, 전 안이 같은 축인 게 정상이다. 그날까지 반려하면
    제안 자체가 불가능해진다.

    "같은 안의 크기 변주"를 잡는 장치는 따로 있다 — §7의 ``variant_collapsed``(안 간 수량 차가
    15% 미만)이고, 그 판정은 T3 몫이다.

    이 검사가 스키마가 아니라 여기 있는 이유: ``allowed_axes``는 ①이 그날 계산한 런타임
    값이라 출력 JSON만 보고는 알 수 없다.
    """
    if len(allowed_axes) < 2 or len(scenarios) < 2:
        return None
    axes = {scenario["strategy_type"] for scenario in scenarios}
    if len(axes) == 1:
        return (
            f"전 안이 동일 축({axes.pop()})인데 그날 허용 축은 {allowed_axes}로 여럿이다 — "
            "선택지가 아니라 같은 안의 크기 변주다"
        )
    return None


def self_check(state: PurchaseAgentState) -> dict[str, Any]:
    """안별 검사로 컷하고, 살아남은 것으로 제안을 조립해 계약을 재확인한다."""
    constraints = load_constraints()
    survivors: list[dict] = []
    rejected: list[dict] = list(state["rejected_reasons"])

    for scenario in state["scenarios_final"]:
        # **한 번만 계산한다.** 컷 사유와 미검사 고지가 배타적인 두 결과라 같은 판정에서
        # 나와야 한다 — 따로 부르면 체인이 컷한 안에 "검사 안 했다"가 붙을 수 있다.
        arrival = arrival_capacity(scenario, state)
        reason = (
            check_quadruple_match(scenario)
            or check_axis_allowed(scenario, state["allowed_axes"])
            or check_prices_exist(scenario, state["market_quotes"])
            or check_max_price(scenario)
            # 창고 두 축은 붙여 둔다 — **총량이 먼저다.** 총량이 이미 넘으면 날짜별
            # 사유는 부차적이고, 컷 사유는 한 안에 하나만 나간다.
            or check_warehouse_capacity(scenario, state["inventory"])
            or arrival.violation
            or check_cash_ceiling(scenario, state, constraints)
            or check_split_dates(scenario, state["date"])
            or check_document_refs(scenario, state["context_docs"])
            # 문서 검사 3종은 순서가 있다: 인용이 로드분인가(refs) → 발행일이 as_of 이전인가
            # (publication) → 발췌가 원문 문자인가(fidelity). 뒤 두 검사는 앞이 통과했다고
            # 가정하지 않고 각자 문서를 다시 찾는다.
            or check_document_publication(scenario, state["context_docs"], state["date"])
            or check_excerpt_fidelity(scenario, state["context_docs"])
            or check_payment_schedule(scenario, state)
        )
        if reason:
            rejected.append({"label": scenario["label"], "reason": reason})
        elif arrival.skipped:
            # **새 dict 를 만든다.** ``state["scenarios_final"]`` 을 제자리에서 고치면
            # ⑥이 만든 값과 ⑦이 내보내는 값이 같은 객체가 되어, 나중에 둘을 대조할 수 없다.
            survivors.append({**scenario, "risks": [*scenario["risks"], arrival.skipped]})
        else:
            survivors.append(scenario)

    diversity = check_axis_diversity(survivors, state["allowed_axes"])
    if diversity:
        rejected.extend({"label": s["label"], "reason": diversity} for s in survivors)
        survivors = []

    # 이 노드가 **직접 컷한 것**만 세어 넘긴다 — 아래 no_proposal_reason 이 원인을
    # self_check 으로 돌릴 자격이 여기서 갈린다.
    cut_here = len(rejected) - len(state["rejected_reasons"])
    proposal = _assemble(state, survivors, rejected, cut_here=cut_here)
    return {"scenarios_final": survivors, "rejected_reasons": rejected, "proposal": proposal}


def _no_proposal_reason(rejected: list[dict], cut_here: int) -> str:
    """안이 하나도 없는 날, **어느 단계에서 없어졌는지**를 정확히 말한다.

    🔴 전에는 무조건 "모든 안이 self_check에서 컷됨"으로 시작했다. 시세를 못 받아 ③이
      초안을 아예 만들지 않은 날에도 그렇게 나갔고 — 2025-12-31 피마늘이 실제로 그랬다 —
      어댑터의 Evidence 도 그 문장을 그대로 "자기 검증에서 컷된 안"으로 옮겼다
      (Codex 교차검증 2026-08-31). 사유는 맞고 **원인 분류가 틀린** 종류의 거짓이라,
      읽는 사람이 검증 로직을 들여다보게 된다.

    ``cut_here`` 는 ⑦이 직접 컷한 안의 수다. 0이면 여기 오기 전에 이미 없었다는 뜻이다.
    """
    detail = "; ".join(f"{item['label']}({item['reason']})" for item in rejected)
    if cut_here == 0:
        return f"안이 만들어지지 않았다: {detail}"
    if cut_here == len(rejected):
        return f"모든 안이 self_check에서 컷됨: {detail}"
    # 섞인 날 — 앞 단계에서 빠진 것과 여기서 컷된 것이 함께 있다.
    return f"앞 단계에서 빠지거나 self_check에서 컷됨: {detail}"


def _assemble(
    state: PurchaseAgentState,
    survivors: list[dict],
    rejected: list[dict],
    *,
    cut_here: int = 0,
) -> dict:
    """제안 JSON을 만들고 **출력 경계에서 계약을 재확인**한다.

    ``revalidate_for_output()``은 Epic 1에서 만들어둔 함수다 — 원시 데이터에서 모델을 다시
    세워, 조립 과정에서 어긋난 값이 그대로 나가는 걸 막는다. 여기서 ``ValidationError``가
    나면 **직렬화하지 않고 터진다**. 계약 위반은 사업적 결과가 아니라 버그이므로 조용히
    빈 제안으로 바꾸지 않는다.
    """
    feedback = state["feedback"] or {}
    raw = {
        "meta": {
            "as_of": state["date"],
            "item": state["item"],
            "agent_version": AGENT_VERSION,
            "is_refeed": bool(state["feedback"]),
            "feedback_attempt": feedback.get("attempt", 0),
            # **받은 사실을 산출물에 남긴다.** 반영은 안 하지만(⑥이 risks 에 고지),
            # 몇 건이 도착했는지는 보내는 쪽이 대조할 수 있어야 한다 — 0 으로만
            # 보이면 "안 보냈다" 와 "보냈는데 못 받았다" 가 같아진다.
            "received_adjustments": len(state.get("adjustments") or []),
        },
        "scenarios": survivors,
        "confidence": state["confidence"],
        "situation": state["situation"],
        "context_docs_used": [document_ref(doc["doc_id"]) for doc in state["context_docs"]],
        "rejected_reasons": rejected,
    }
    if not survivors:
        raw["no_proposal_reason"] = _no_proposal_reason(rejected, cut_here)
    return revalidate_for_output(PurchaseProposal.model_validate(raw)).model_dump(mode="json")
