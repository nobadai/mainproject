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
from app.purchase_agent.nodes.classify_situation import is_sustained_rise
from app.purchase_agent.schemas import TIMING_AXIS
from app.purchase_agent.state import PurchaseAgentState


def largest_total_kg(base_plan: dict) -> int:
    """가장 큰 안의 총량. 수량 트리거의 비교 대상이다.

    ①도 같은 트리거를 보지만 그때는 수량이 없어 **추정 총량**(일평균 × 최대 D)을 썼다.
    ④는 ③ 뒤라 실제 안별 총량을 본다 — 추정으로 열린 축이 실제 수량에서 닫히는 것도
    정상이다 (§4-④ E3-3 확정 2).
    """
    return max((draft["total_qty_kg"] for draft in base_plan["drafts"]), default=0)


def choose_rounds(total_kg: int, constraints: dict) -> int:
    """회차 수를 **고정 목록에서 고른다** (§4-④ "생성 말고 선택").

    ``clamp(ceil(총량 / 임계), 목록 경계)``. 임계 하나당 한 회차이고, 진입했으면 최소
    2회차다 — 그 "2"는 상수가 아니라 **목록에서 1 다음으로 작은 유형**이다.

    8,727kg이면 ``ceil(0.44) = 1``이라 수량만으로는 일괄이지만, 진입 자체가 궤적으로
    이뤄졌으므로 하한이 걸려 2분할이 된다.
    """
    types = sorted(constraints["split"]["types"])
    splittable = [size for size in types if size > 1]
    if not splittable:
        return 1
    chunks = ceil(total_kg / constraints["triggers"]["split_entry_qty_kg"])
    return min(max(chunks, min(splittable)), max(splittable))


def evaluate_split_entry(state: PurchaseAgentState, constraints: dict) -> dict[str, Any]:
    """진입 판정과 회차 수. 근거 전체를 dict 하나로 돌려준다.

    ``timing ∈ allowed_axes AND (최대안 총량 ≥ split_entry_qty_kg OR 지속 상승 궤적)``
    (§4-④ v1.1 정정 — 구 "D ≥ 임계"는 낡은 표현이고 임계는 수량이다).

    ⚠️ 수량 가지는 현재 mock에서 **한 번도 서지 않는다** (최대안 8,727kg < 20,000).
    두 앵커(8/21·9/11) 모두 궤적으로만 진입한다 — mock만 돌려서는 이 가지가 살아 있는지
    알 수 없으므로 합성 입력 테스트로 따로 시험한다.
    """
    threshold = constraints["triggers"]["split_entry_qty_kg"]
    day = constraints["situation"]["ci_judgment_day"]
    total_kg = largest_total_kg(state["base_plan"])

    facts: dict[str, Any] = {
        "timing_allowed": TIMING_AXIS in state["allowed_axes"],
        "largest_total_kg": total_kg,
        "threshold_kg": threshold,
        "by_volume": total_kg >= threshold,
        "by_trend": is_sustained_rise(state["forecast"], day),
        "rounds": 1,
    }
    facts["entered"] = facts["timing_allowed"] and (facts["by_volume"] or facts["by_trend"])
    if facts["entered"]:
        facts["rounds"] = choose_rounds(total_kg, constraints)
    return facts


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
