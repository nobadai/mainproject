"""④ split_plan — 조건부 진입 + 분할 유형 선택 (상세설계 §4-④ · 백로그 E3-3).

**계산만 한다** (규칙 6). 진입 여부와 회차 수는 규칙이 정하고, LLM은 회차별 수량·날짜
배분 판단만 맡는다 (다음 단계 — §4-④ "LLM은 회차별 수량·날짜 배분 판단만").

§4-④ E3-3 확정(8/25)이 이 파일의 구조를 정했다:

* **적용 범위 = timing 축을 받은 안에만** (확정 1). 그 판정은 축을 배정하는 ⑥이 한다 —
  여기서는 "그날 분할이 가능한가"까지만 정한다.
* **궤적 판정은 ①의 ``is_sustained_rise()`` 재사용** (확정 2). 두 노드가 각자 정의하면
  "축은 열렸는데 분할은 안 되는" 모순이 난다.
* **rule_only 단계는 균등 비율** (확정 3). 앞당길지 미룰지는 §4-④ 트레이드오프
  ("상승장 분할 = 평균단가 손해 vs 로트 나이 분산 = 폐기리스크 감소")의 판단이라 LLM 몫이고,
  규칙이 한쪽으로 기울이면 그 판단을 미리 대신해버린다.
* **④는 비율만, 날짜는 ⑥이 안별 D로 만든다** (확정 4). ⑤가 비율만 내고 ⑥이 총량을 곱하는
  것과 같은 구조다 — 여기서 절대 날짜를 박으면 D가 다른 안에 같은 날짜가 박힌다.
"""

from math import ceil
from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.classify_situation import is_sustained_rise, split_entry_cap
from app.purchase_agent.schemas import TIMING_AXIS
from app.purchase_agent.state import PurchaseAgentState


def largest_total_kg(base_plan: dict) -> int:
    """가장 큰 안의 총량. 수량 트리거의 비교 대상이다.

    ①도 같은 트리거를 보지만 그때는 수량이 없어 **추정 총량**(일평균 × 최대 D)을 썼다.
    ④는 ③ 뒤라 실제 안별 총량을 본다 — 추정으로 열린 축이 실제 수량에서 닫히는 것도
    정상이다 (§4-④ E3-3 확정 2).
    """
    return max((draft["total_qty_kg"] for draft in base_plan["drafts"]), default=0)


def choose_rounds(total_kg: int, cap_kg: float | None, constraints: dict) -> int:
    """회차 수를 **고정 목록에서 고른다** (§4-④ "생성 말고 선택").

    ``clamp(ceil(총량 / 도착일 여유), 목록 경계)``. 여유 하나당 한 회차이고, 진입했으면
    최소 2회차다 — 그 "2"는 상수가 아니라 **목록에서 1 다음으로 작은 유형**이다.

    🔴 **분모가 고정 임계에서 도착일 여유로 바뀌었다** (`#308`). *"20,000kg 짜리 덩어리
      몇 개인가"* 가 아니라 *"그날 들어갈 만큼씩 나누면 몇 번인가"* 다. 근거는 ①
      ``split_entry_cap`` docstring.

    ★ **회차 수는 매입이 정한다** — 물류가 붙인 조건 그대로다. 여유는 물류 정본을 읽고,
      그것으로 **몇 번에 나눌지**를 정하는 것은 이쪽 판단이다.

    ⚠️ **여유 ``0`` 은 나눗셈이 아니라 «아무리 나눠도 그날엔 안 들어간다» 다.** ``ceil``
      로는 ∞ 라 목록 최대로 클램프한다. 0으로 나누기를 피하려는 방어가 아니라 뜻을
      옮긴 것이다 — 뒤에서 ⑦ ``check_arrival_capacity`` 가 그 안을 어차피 컷한다.

    ``cap_kg`` 가 ``None`` 이면 여기까지 오지 않는다 — ``evaluate_split_entry`` 가
    수량 트리거를 세우지 않으므로, 진입했다면 궤적으로 진입한 것이라 하한만 걸린다.

    🔴 **지금 데이터에서는 나눠도 도착일 컷을 못 피한다 — 그 사실을 여기 적어 둔다.**

      ⑦ ``check_arrival_capacity`` 는 도착일까지의 **누적**을 그날 여유와 견준다
      (앞 회차가 아직 창고에 있으므로 옳다). 그런데 실측에서 ``cap_by_date`` 는
      **창 전체가 한 값**이다 — 원장 2,743 봉투 **전부** 그렇고, 끝−처음이 0 이다
      (2026-09-12). 누적은 늘고 여유는 안 늘면, 나누는 것으로는 그 컷을 못 넘는다.

      ⚠️ 그래서 이 조항의 **되살리는 효과는 0**이다. 값은 다른 데 있다 — 기준의 뜻,
        근거 문장, 그리고 로트 나이 분산이다. 창이 날짜별로 실제로 갈리는 날
        (재고가 실제로 나가기 시작하면) 이 문단을 다시 재야 한다.
    """
    types = sorted(constraints["split"]["types"])
    splittable = [size for size in types if size > 1]
    if not splittable:
        return 1
    if cap_kg is None:
        chunks = 1  # 궤적 진입 — 여유를 못 봤으므로 수량으로는 회차를 못 정한다
    elif cap_kg <= 0:
        chunks = max(splittable)
    else:
        chunks = ceil(total_kg / cap_kg)
    return min(max(chunks, min(splittable)), max(splittable))


def evaluate_split_entry(state: PurchaseAgentState, constraints: dict) -> dict[str, Any]:
    """진입 판정과 회차 수. 근거 전체를 dict 하나로 돌려준다.

    ``timing ∈ allowed_axes AND (최대안 총량 ≥ 도착일 여유 OR 지속 상승 궤적)``
    (§4-④ v1.1 정정 — 구 "D ≥ 임계"는 낡은 표현이고 임계는 수량이다).

    🔴 **수량 가지의 기준이 고정 임계에서 도착일 여유로 바뀌었다** (`#308` · 2026-09-12).
      옛 임계 ``20,000kg`` 에서는 이 가지가 **원장 전수에서도 거의 안 섰다** — 안이 있는
      672셀 중 **1셀**뿐이다. 품목별 최대가 배추 8,727 · 무 9,429 · 양파 10,286kg 이라
      구조적으로 미만이었다. 근거는 ① ``split_entry_cap`` docstring.

    ⚠️ **①과 여기가 다른 수를 본다 — 그게 정상이다.** ①은 클립 **전** 추정 총량
      (일평균 × 최대 D)으로 축을 열고, ④는 클립 **후** 안별 실제 총량으로 진입을 본다.
      추정으로 열린 축이 실제 수량에서 닫히는 날이 생기고 (실측 20셀 · 2026-09-12),
      그 안은 **timing 라벨만 남고 회차가 하나**가 된다. 그 상태를 ⑥·⑦이
      ``effective_allowed_axes`` 로 걷는다 — 안 걷으면 «분할 안 한 분할안» 이 선다.
    """
    day = constraints["situation"]["ci_judgment_day"]
    total_kg = largest_total_kg(state["base_plan"])
    cap = split_entry_cap(state, constraints)

    facts: dict[str, Any] = {
        "timing_allowed": TIMING_AXIS in state["allowed_axes"],
        "largest_total_kg": total_kg,
        # 🔴 ``threshold_kg`` 를 갈아 끼우지 않고 **이름을 바꿨다.** 같은 칸에 다른 뜻을
        #   넣으면 근거 문장이 "임계"라고 말하면서 창고 여유를 인용한다.
        "cap_kg": cap.cap_kg,
        "arrival_date": cap.arrival_date,
        "cap_unknown_reason": cap.unknown_reason,
        "by_volume": cap.cap_kg is not None and total_kg >= cap.cap_kg,
        "by_trend": is_sustained_rise(state["forecast"], day),
        "rounds": 1,
    }
    facts["entered"] = facts["timing_allowed"] and (facts["by_volume"] or facts["by_trend"])
    if facts["entered"]:
        facts["rounds"] = choose_rounds(total_kg, cap.cap_kg, constraints)
    return facts


def split_decision(chosen: list[dict] | None) -> dict:
    """④가 첫 줄에 실어 보낸 분할 판단 근거. ⑤의 ``_sourcing_decision``과 같은 방식이다.

    🔴 **⑥에서 여기로 옮겼다** (`#308`). ⑦도 같은 값을 봐야 하는데 ⑥의 private 함수라
      못 불렀다 — 판단을 만든 쪽이 읽는 법도 들고 있는 것이 맞다.
    """
    return chosen[0].get("decision", {}) if chosen else {}


def effective_allowed_axes(allowed_axes: list[str], chosen: list[dict] | None) -> list[str]:
    """**실효 축** — ④가 실제로 안 나눴으면 ``timing`` 을 뺀다 (`#308`).

    ★ **왜 필요한가.** ①은 클립 전 추정으로 축을 열고 ④는 클립 후 실제 수량으로 진입을
      본다. 축은 열렸는데 진입은 안 한 날이 생기고, 그날 ⑥이 그대로 ``timing`` 을
      배정하면 **회차가 하나인 «분할안»** 이 선다 — §3.5.1-3 이 막으려는 "3안인데 사실
      한 안"이 라벨로만 위장한 꼴이다.

    🔴 **⑥만 고치면 ⑦이 그 안들을 통째로 죽인다.** ⑥이 timing 을 안 주면 전 안이
      ``quantity`` 가 되는데, ⑦ ``check_axis_diversity`` 는 ``allowed_axes`` 가 둘 이상인
      날 전 안 동일 축을 반려한다. 실측으로 **20셀**이 그렇게 사라진다 (2026-09-12 ·
      안이 있는 672셀 기준 · 원장 재생). 그래서 **⑥과 ⑦이 같은 목록을 본다** — 이 함수가
      그 목록이다.

    ★ ``quantity`` 는 안 뺀다. 수량 축은 ①이 늘 여는 축이라 뺄 조건이 없다.
    """
    if split_decision(chosen).get("entered"):
        return allowed_axes
    return [axis for axis in allowed_axes if axis != TIMING_AXIS]


def equal_ratios(rounds: int) -> list[float]:
    """균등 비율. 마지막을 ``1 − Σ앞``으로 **구성**한다.

    각자 계산한 ``1/n``을 n번 더하면 부동소수점 합이 1에서 밀려 ⑥의 합계 검사(1e-9)에
    걸릴 수 있다 — E3-1에서 등급 비율에 쓴 것과 같은 장치다.
    """
    head = [1 / rounds] * (rounds - 1)
    return [*head, 1.0 - sum(head)]


def split_plan(state: PurchaseAgentState) -> dict[str, Any]:
    """분할 유형을 고르고 회차 비율을 낸다. 진입하지 않으면 ``None``(일괄)이다.

    E3-2에서 LLM이 붙는 자리는 여기다: ``evaluate_split_entry``가 낸 사실들(트리거 종류·
    총량·회차 수)과 예측 궤적을 프롬프트로 주고 **회차별 수량·날짜 배분**을 판단하게 한다 —
    "상승장이라 앞당기면 단가는 유리하지만 로트가 한꺼번에 늙는다. 어느 쪽인가?"
    유형은 그때도 고정 목록에서 고르고(§4-④), 숫자는 계산이 소유한다 (규칙 6).

    수량은 여기서 정하지 않는다 — 안별 총량이 달라 회차 수량은 ⑥이 materialize한다.
    이 노드가 소유하는 건 **유형**이고, 그 층위는 IO명세 feedback의
    ``keep: ["sourcing_ratio", "split_type"]``과 같다.

    **일괄(진입 안 함)도 ``None``이 아니라 1회차 비율 목록으로 낸다.** 진입하지 않은 이유가
    ``decision``에 실려 ⑥까지 가야 하기 때문이다 — ①은 클립 **전** 추정 총량으로 timing 축을
    열고 ④는 클립 **후** 실제 총량으로 판정하므로, "timing 라벨인데 회차가 하나"인 안이
    정상적으로 생긴다. ``None``으로 내보내면 그 안이 왜 그런지 설명할 근거가 사라진다.
    비율 1.0짜리 한 줄은 ⑥에서 단일 회차로 materialize돼 결과가 일괄과 같다.
    """
    constraints = load_constraints()
    decision = evaluate_split_entry(state, constraints)
    lines = [{"ratio": ratio} for ratio in equal_ratios(decision["rounds"])]
    # 판단 근거를 첫 줄에 싣는다 — State 필드를 늘리지 않기 위해서다 (§3 계약).
    # ⑥의 materialize가 계약 필드만 투영하므로 출력에는 새지 않는다 (⑤와 같은 방식).
    lines[0] = {**lines[0], "decision": decision}
    return {"split_plan": lines}
