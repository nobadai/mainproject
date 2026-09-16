"""④ 배분 판단의 **진입 · 선택 · 적용 · 되돌림**을 가른다 (E3-9 · 2026-09-17).

1차 실호출 실험(플래그를 한 프로세스에서만 켜고 승인은 메모리 사본에만)에서 나온 셋을 잠근다.

```text
㉠ 버려지는 호출     창이 한 값인 날 판단자가 SUCCESS 를 냈고 ⑥ 이 그 선택을 버렸다
                   → ④ 가 ⑥ 과 같은 함수(settle_split)로 «그대로 적용될 후보» 만 남긴다
㉡ 거짓 문면        앞으로 싣는 배분이 적용됐는데 「비율은 균등 · 창고 여유로 옮겨졌다」
                   → 적용한 비율과 실제로 옮긴 것만 적는다 (반올림 차는 옮김이 아니다)
㉢ 선택이 안 남음    어느 후보를 · 왜 · 적용됐는지를 말하는 근거가 없었다
                   → 근거 1건이 선택 후보 · 선택 사유 · 적용 여부를 가른다
```

⚠️ 입력은 **합성**이다 — 저장 기록에는 날짜별로 갈리는 여유가 한 건도 없다. 승인 상태는
**검사 안에서만** ``APPROVED`` 이고, 선언 파일은 ``PROVISIONAL`` 그대로다. 판단자는 가짜
프로바이더라 네트워크를 안 탄다.
"""

import copy
import dataclasses
import json
from datetime import date, timedelta

import pytest

from app.purchase_agent import adapter
from app.purchase_agent.allocation import APPROVED, allocation_candidates, split_quantities
from app.purchase_agent.config import load_constraints
from app.purchase_agent.llm.runtime import get_llm_settings
from app.purchase_agent.llm.split_allocation import (
    SplitAllocationService,
    make_split_selector,
)
from app.purchase_agent.llm.split_schemas import SplitAllocationChoice, SplitAllocationResult
from app.purchase_agent.nodes._split_outcome import (
    AS_CHOSEN,
    ROLLED_BACK,
    ROUNDS_CHANGED,
    settle_split,
)
from app.purchase_agent.nodes.allocate_sourcing import allocate_sourcing
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import package_scenarios
from app.purchase_agent.nodes.self_check import (
    check_arrival_capacity,
    check_payment_schedule,
    check_warehouse_capacity,
    self_check,
)
from app.purchase_agent.nodes.split_plan import (
    EXCLUDED_OVER_CAPACITY,
    split_plan,
)
from app.purchase_agent.schemas import TIMING_AXIS
from app.purchase_agent.state import build_initial_state

ITEM = "배추"
AS_OF = date(2026, 8, 21)
LEAD_DAYS = 2
N5 = 7
FLAG = "PURCHASE_LLM_SPLIT_ALLOCATION_ENABLED"


def _날(offset: int) -> str:
    return (AS_OF + timedelta(days=offset)).isoformat()


def _창(여유) -> dict[str, float]:
    return {_날(offset): 여유(offset) for offset in range(40)}


#: 이름 → (날짜별 여유, 오늘 여유, 예측을 평탄하게 누르나)
WINDOWS = {
    # 넉넉하고 지속 상승 — 궤적으로 2회차 진입 · 세 후보가 다 그대로 선다
    "넉넉_상승": (_창(lambda o: 1_000_000.0), 1_000_000.0, False),
    # 첫 도착일만 좁고 뒤가 넓다 · 평탄 — 수량으로 3회차 · 앞으로 싣는 배분은 첫 날을 넘는다
    "뒤로_넓어짐": (_창(lambda o: 2_000.0 if o <= LEAD_DAYS else 20_000.0), 2_000.0, True),
    # 같은 창인데 상승 — 균등을 고르면 물량이 옮겨진다
    "뒤로_넓어짐_상승": (
        _창(lambda o: 2_000.0 if o <= LEAD_DAYS else 20_000.0),
        2_000.0,
        False,
    ),
    # 저장 기록 모양 — 창 내내 한 값 · 나눠도 더 살 수 없다
    "한_값": (_창(lambda o: 3_569.0), 3_569.0, True),
    # ① 은 추정 총량으로 축을 열고 ④ 는 깎기 전 수요(12,429)가 여유 안이라 진입 안 한다
    "진입_안함": (_창(lambda o: 15_000.0), 15_000.0, True),
    # 3회차의 마지막 도착일이 좁다 — 2회차로 바뀐다
    "회차_바뀜": (
        _창(lambda o: 2_000.0 if o <= LEAD_DAYS else (20_000.0 if o <= 8 else 1_000.0)),
        2_000.0,
        True,
    ),
    # 어느 회차 수로도 안 선다 — 한 번에 산다
    "일괄_복귀": (
        _창(
            lambda o: 2_000.0
            if o <= LEAD_DAYS
            else (500.0 if o <= 7 else (1_000.0 if o == 8 else 20_000.0))
        ),
        2_000.0,
        True,
    ),
}


def _state(name: str) -> dict:
    """①③ 을 실제로 태운 State — N4 · 여유 · N5 는 **① 전에** 싣는다 (어댑터와 같은 자리)."""
    caps, today_free, flat = WINDOWS[name]
    state = build_initial_state(ITEM, AS_OF)
    state["inbound_lead_days"] = LEAD_DAYS
    state["purchase_payment_days"] = N5
    state["inventory"] = {
        **state["inventory"],
        "cap_by_date": caps,
        "warehouse_free_kg": today_free,
        "rental_cap_kg": 0.0,
    }
    if flat:
        예측 = copy.deepcopy(state["forecast"])
        고정 = 예측["daily"][0]["predicted"]
        for row in 예측["daily"]:
            row["predicted"] = 고정
        state["forecast"] = 예측
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    return state


def _승인된_선언() -> dict:
    """🔴 **검사 안에서만 승인이다.** 파일은 ``PROVISIONAL`` 그대로다."""
    사본 = load_constraints()
    사본["split"]["allocation_weights"]["status"] = APPROVED
    return 사본


@pytest.fixture
def 승인_켜짐(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr("app.purchase_agent.nodes.split_plan.load_constraints", _승인된_선언)


class 세는_프로바이더:
    """판단자 **호출 수**를 센다. 고를 id 를 주면 그 응답을, 아니면 예외를 낸다."""

    def __init__(self, 고를_id: str | None, reason: str = "창고 흐름에 맞춰 고른다") -> None:
        self.고를_id = 고를_id
        self.reason = reason
        self.calls = 0

    def generate(self, context, *, retry_guidance=None) -> str:
        self.calls += 1
        if self.고를_id is None:
            raise ConnectionError("판단자 응답 실패를 흉내 낸다")
        return json.dumps(
            {"chosen_candidate_id": self.고를_id, "reason": self.reason}, ensure_ascii=False
        )


def _선택자(provider: 세는_프로바이더):
    settings = dataclasses.replace(
        get_llm_settings(), enabled=True, provider="fake", model="fake-model", max_retries=1
    )
    return make_split_selector(SplitAllocationService(settings, provider))


def _펴고_검사(state: dict, selector=None) -> tuple[dict, dict]:
    """④⑤⑥⑦ 을 태운다. ⑦ 까지 간 결과와 ⑥ 직후 State 를 같이 준다."""
    state = dict(state)
    state.update(split_plan(state, selector=selector))
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    return state, self_check(state)


def _공격(state: dict) -> dict:
    return next(s for s in state["scenarios_final"] if s["label"] == "공격")


def _결정(state: dict) -> dict:
    return state["split_plan"][0]["decision"]


def _일곱번이_통과시킨다(state: dict, final: dict) -> None:
    """🔴 **설명을 고치려고 검사를 우회하지 않는다.** 모든 안이 ⑦ 을 그대로 지난다."""
    assert [r for r in final["rejected_reasons"] if "kind" not in r] == []
    constraints = load_constraints()
    for scenario in final["scenarios_final"]:
        total = scenario["total_qty_kg"]
        assert sum(leg["qty_kg"] for leg in scenario["split_plan"]) == total
        assert sum(x["qty_kg"] for x in scenario["sourcing_plan"]) == total
        assert sum(leg["amount_krw"] for leg in scenario["split_plan"]) == scenario[
            "total_amount_krw"
        ]
        assert check_arrival_capacity(scenario, state) is None
        assert check_warehouse_capacity(scenario, state["inventory"], state, constraints) is None
        assert check_payment_schedule(scenario, state) is None
        for rationale in scenario["rationale"]:
            assert rationale["ref_id"]


def _그대로_적용됐나(scenario: dict, ratios: list[float], rounds: int) -> bool:
    legs = [leg["qty_kg"] for leg in scenario["split_plan"]]
    return (
        scenario["strategy_type"] == TIMING_AXIS
        and len(legs) == rounds
        and legs == split_quantities(scenario["total_qty_kg"], [{"ratio": r} for r in ratios])
    )


def _강제_선택(state: dict, 후보_id: str, *, judged: bool) -> tuple[dict, dict]:
    """④ 의 거름을 **건너뛰고** 그 후보를 쓴 것처럼 ⑥⑦ 을 태운다.

    ⚠️ 운영 경로는 이 후보를 목록에 안 올린다 — 여기서는 «⑥ 이 그 선택을 어떻게 처리하고
      어떻게 설명하나» 만 잰다.
    """
    state = dict(state)
    state.update(split_plan(state))
    decision = dict(_결정(state))
    선언 = _승인된_선언()["split"]["allocation_weights"]
    ratios = allocation_candidates(선언, decision["rounds"])[후보_id]
    decision["allocation_chosen"] = 후보_id
    decision["allocation_judgment"] = (
        SplitAllocationResult(
            interpretation=SplitAllocationChoice(
                chosen_candidate_id=후보_id, reason="앞 회차에 싣는 편이 단가에 유리하다"
            ),
            llm_status="SUCCESS",
            llm_provider="fake",
            llm_model="fake-model",
            llm_attempts=1,
            llm_fallback_used=False,
        )
        if judged
        else None
    )
    lines = [{"ratio": ratio} for ratio in ratios]
    lines[0] = {**lines[0], "decision": decision}
    state["split_plan"] = lines
    state.update(allocate_sourcing(state))
    state.update(package_scenarios(state))
    return state, self_check(state)


# ── ㉠ 부를지 말지 — ④ 와 ⑥ 이 같은 기준을 쓴다 ─────────────────────────────


def test_승인_전이면_판단자를_부르지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **플래그만 켜도 안 불린다** — 선언 파일은 ``PROVISIONAL`` 이다."""
    monkeypatch.setenv(FLAG, "true")
    provider = 세는_프로바이더("FRONT_LOADED")
    state, final = _펴고_검사(_state("넉넉_상승"), _선택자(provider))
    assert provider.calls == 0
    assert _결정(state)["allocation_candidates"] == ["BASE_EQUAL"]
    assert _결정(state)["allocation_approved"] is False
    [흔적] = adapter._split_allocation_call(state)
    assert 흔적.status == "SKIPPED_TEMPLATE"
    assert "승인 전" in (흔적.skip_reason or "")
    _일곱번이_통과시킨다(state, final)


def test_진입하지_않은_날은_선택_단계가_없다(승인_켜짐) -> None:
    """진입 안 한 날은 «건너뛰었다» 도 아니다 — 흔적에 줄이 안 남는다."""
    provider = 세는_프로바이더("FRONT_LOADED")
    state, _ = _펴고_검사(_state("진입_안함"), _선택자(provider))
    assert _결정(state)["entered"] is False
    assert provider.calls == 0
    assert _결정(state)["allocation_judgment"] is None
    assert adapter._split_allocation_call(state) == ()


def test_적용되지_않을_것이_확정되면_부르지_않는다(승인_켜짐) -> None:
    """🔴 **1차 실호출 실험에서 실제로 불렸던 날이다** — 창이 한 값이라 ⑥ 이 한 번에 산다.

    승인도 됐고 플래그도 켜졌고 ④ 도 진입했지만, 어느 비균등 후보를 골라도 결과가 같다.
    """
    provider = 세는_프로바이더("BACK_LOADED")
    state, final = _펴고_검사(_state("한_값"), _선택자(provider))
    결정 = _결정(state)
    assert 결정["entered"] is True and 결정["allocation_approved"] is True
    assert provider.calls == 0, "결과에 안 쓰일 선택을 받으려고 판단자를 불렀다"
    assert 결정["allocation_candidates"] == ["BASE_EQUAL"]
    assert 결정["allocation_excluded"] == {"BACK_LOADED": ROLLED_BACK, "FRONT_LOADED": ROLLED_BACK}
    [흔적] = adapter._split_allocation_call(state)
    assert "미리 확정" in (흔적.skip_reason or "")
    공격 = _공격(state)
    assert len(공격["split_plan"]) == 1 and 공격["strategy_type"] != TIMING_AXIS
    _일곱번이_통과시킨다(state, final)


def test_다른_후보가_유효하면_생략하지_않는다(승인_켜짐) -> None:
    """🔴 **하나가 빠져도 남은 것이 적용되면 부른다.** 앞으로 싣는 배분만 첫 날을 넘는다."""
    provider = 세는_프로바이더("BACK_LOADED")
    state, final = _펴고_검사(_state("뒤로_넓어짐"), _선택자(provider))
    결정 = _결정(state)
    assert 결정["allocation_excluded"] == {"FRONT_LOADED": EXCLUDED_OVER_CAPACITY}
    assert 결정["allocation_candidates"] == ["BACK_LOADED", "BASE_EQUAL"]
    assert provider.calls == 1
    assert 결정["allocation_chosen"] == "BACK_LOADED"
    공격 = _공격(state)
    assert [leg["qty_kg"] for leg in 공격["split_plan"]] == [1_745, 2_618, 4_364]
    _일곱번이_통과시킨다(state, final)


@pytest.mark.parametrize(
    ("name", "남을_비균등"),
    [
        ("넉넉_상승", {"FRONT_LOADED", "BACK_LOADED"}),
        ("뒤로_넓어짐", {"BACK_LOADED"}),
        ("뒤로_넓어짐_상승", {"BACK_LOADED"}),
        ("한_값", set()),
        ("회차_바뀜", set()),
        ("일괄_복귀", set()),
    ],
)
def test_남기는_후보는_여섯번이_그대로_적용하는_후보와_같다(
    monkeypatch: pytest.MonkeyPatch, name: str, 남을_비균등: set[str]
) -> None:
    """🔴 **④ 와 ⑥ 의 기준이 같은지** — 비균등 후보 하나하나를 ⑥ 에 억지로 넣어 본다.

    ④ 가 남긴 후보는 ⑥ 이 **회차 · 비율 · 물량 그대로** 적용하고, ④ 가 뺀 후보는 ⑥ 이
    회차를 바꾸거나 · 접거나 · 물량을 옮긴다. 둘이 갈리면 판단자가 고른 것이 버려진다.
    """
    monkeypatch.setattr("app.purchase_agent.nodes.split_plan.load_constraints", _승인된_선언)
    base = _state(name)
    after_split = dict(base)
    after_split.update(split_plan(after_split))
    결정 = _결정(after_split)
    남은 = set(결정["allocation_candidates"]) - {"BASE_EQUAL"}
    assert 남은 == 남을_비균등

    선언 = _승인된_선언()["split"]["allocation_weights"]
    for 후보_id, ratios in allocation_candidates(선언, 결정["rounds"]).items():
        if 후보_id == "BASE_EQUAL":
            continue
        state, final = _강제_선택(base, 후보_id, judged=False)
        applied = _그대로_적용됐나(_공격(state), ratios, 결정["rounds"])
        assert applied == (후보_id in 남은), (name, 후보_id, _공격(state)["split_plan"])
        _일곱번이_통과시킨다(state, final)


# ── ㉡㉢ 설명 — 선택 후보 · 선택 사유 · 적용 여부 ─────────────────────────────


def _배분_근거(scenario: dict) -> list[dict]:
    return [r for r in scenario["rationale"] if r["claim"].startswith("회차 배분 ")]


def _궤적_근거(scenario: dict) -> str:
    [row] = [r for r in scenario["rationale"] if "지속 상승 궤적" in r["claim"]]
    return row["evidence_detail"]


def test_고른_배분이_적용되면_근거가_선택_사유_적용을_가른다(승인_켜짐) -> None:
    """🔴 **1차 실험의 거짓 문면 자리** — 앞으로 싣는 배분이 적용됐는데 «균등 · 옮겨졌다» 였다."""
    provider = 세는_프로바이더("FRONT_LOADED", reason="상승세라 앞 회차에 싣는 편이 유리하다")
    state, final = _펴고_검사(_state("넉넉_상승"), _선택자(provider))
    공격 = _공격(state)
    assert [leg["qty_kg"] for leg in 공격["split_plan"]] == [5_236, 3_491]

    [근거] = _배분_근거(공격)
    assert "FRONT_LOADED" in 근거["claim"], "선택 후보"
    assert "앞 회차에 더 싣는다" in 근거["claim"]
    assert "상승세라 앞 회차에 싣는 편이 유리하다" in 근거["claim"], "선택 사유"
    assert "그대로 적용했다" in 근거["evidence_detail"], "적용 여부"
    assert 근거["ref_id"]

    궤적 = _궤적_근거(공격)
    assert "균등" not in 궤적
    assert "판단자가 고른 배분(앞 회차에 더 싣는다)을 그대로 적용했다" in 궤적
    assert "그 비율 그대로다" in 궤적 and "옮겨졌다" not in 궤적
    assert not [r for r in 공격["risks"] if "옮겼다" in r]
    assert not [r for r in 공격["risks"] if "배분 판단 미적용" in r]
    # 판단자 문장은 risks 에 안 들어간다 — 고정 문장만 산다.
    assert not [r for r in 공격["risks"] if provider.reason in r]
    _일곱번이_통과시킨다(state, final)


def test_반올림_차이를_창고_재배분으로_적지_않는다() -> None:
    """🔴 균등 2회차 4,364 / 4,363 은 **1kg 반올림 차**다 — 옮긴 것이 아니다 (플래그 꺼짐)."""
    state, final = _펴고_검사(_state("넉넉_상승"))
    공격 = _공격(state)
    assert [leg["qty_kg"] for leg in 공격["split_plan"]] == [4_364, 4_363]
    궤적 = _궤적_근거(공격)
    assert "회차 비율은 균등이다" in 궤적
    assert "그 비율 그대로다" in 궤적
    assert "옮겨졌다" not in 궤적, "반올림 차를 창고 재배분으로 적었다"
    assert not [r for r in 공격["risks"] if "옮겼다" in r]
    assert _배분_근거(공격) == [], "판단자가 안 골랐는데 선택 근거가 붙었다"
    _일곱번이_통과시킨다(state, final)


def test_창고_여유로_실제로_옮긴_것은_옮겼다고_적는다() -> None:
    """반대 방향 — 옮긴 날의 문장이 사라지는 변이도 잡는다."""
    state, final = _펴고_검사(_state("뒤로_넓어짐_상승"))
    공격 = _공격(state)
    legs = [leg["qty_kg"] for leg in 공격["split_plan"]]
    assert legs == [2_000, 3_818, 2_909]
    궤적 = _궤적_근거(공격)
    assert "옮겨졌다" in 궤적
    for qty in legs:
        assert f"{qty:,}kg" in 궤적
    assert [r for r in 공격["risks"] if "맞춰 옮겼다" in r]
    _일곱번이_통과시킨다(state, final)


def test_판단자가_균등을_골랐는데_물량이_옮겨지면_둘을_갈라_적는다(승인_켜짐) -> None:
    provider = 세는_프로바이더("BASE_EQUAL")
    state, final = _펴고_검사(_state("뒤로_넓어짐_상승"), _선택자(provider))
    assert provider.calls == 1
    [근거] = _배분_근거(_공격(state))
    assert "BASE_EQUAL" in 근거["claim"]
    assert "비율은 결과에 적용했고, 회차 물량은 날짜별 창고 여유에 맞춰 옮겼다" in 근거[
        "evidence_detail"
    ]
    _일곱번이_통과시킨다(state, final)


def test_선택_뒤_회차가_바뀌면_적용하지_않았다고_적는다() -> None:
    """⑥ 이 고른 회차 수를 못 세워 다른 회차 수를 **균등으로** 쓴 날."""
    state, final = _강제_선택(_state("회차_바뀜"), "FRONT_LOADED", judged=True)
    공격 = _공격(state)
    assert len(공격["split_plan"]) == 2
    [근거] = _배분_근거(공격)
    assert "FRONT_LOADED" in 근거["claim"]
    assert "적용하지 않았다" in 근거["evidence_detail"]
    assert "3회 분할용" in 근거["evidence_detail"] and "2회 균등" in 근거["evidence_detail"]
    [고지] = [r for r in 공격["risks"] if "배분 판단 미적용" in r]
    assert "앞 회차에 더 싣는다" in 고지 and "2회 균등으로 바꿨다" in 고지
    assert "앞 회차에 싣는 편이 단가에 유리하다" not in 고지, "판단자 문장이 risks 로 샜다"
    assert [r for r in 공격["risks"] if "회 균등으로 바꿨다" in r]
    _일곱번이_통과시킨다(state, final)


def test_선택_뒤_일괄로_돌아가면_적용하지_않았다고_적는다() -> None:
    state, final = _강제_선택(_state("일괄_복귀"), "FRONT_LOADED", judged=True)
    공격 = _공격(state)
    assert len(공격["split_plan"]) == 1 and 공격["strategy_type"] != TIMING_AXIS
    [근거] = _배분_근거(공격)
    assert "적용하지 않았다 — 이 안은 분할을 접고 한 번에 산다" in 근거["evidence_detail"]
    [고지] = [r for r in 공격["risks"] if "배분 판단 미적용" in r]
    assert "분할을 접고 한 번에 산다" in 고지
    _일곱번이_통과시킨다(state, final)


def test_판단자가_실패하면_기본안으로_나눴다고_적는다(승인_켜짐) -> None:
    provider = 세는_프로바이더(None)
    state, final = _펴고_검사(_state("넉넉_상승"), _선택자(provider))
    assert provider.calls == 2, "재시도 상한까지 불렀다"
    결정 = _결정(state)
    assert 결정["allocation_chosen"] == "BASE_EQUAL"
    [흔적] = adapter._split_allocation_call(state)
    assert 흔적.status == "FALLBACK" and 흔적.fallback_used
    공격 = _공격(state)
    assert [leg["qty_kg"] for leg in 공격["split_plan"]] == [4_364, 4_363]
    assert _배분_근거(공격) == [], "실패는 선택이 아니다"
    [고지] = [r for r in 공격["risks"] if "배분 판단 미적용" in r]
    assert "판단자 응답 실패" in 고지 and "균등 배분(기본안)으로 나눴다" in 고지
    assert "회차 비율은 균등이다" in _궤적_근거(공격)
    _일곱번이_통과시킨다(state, final)


def test_판단이_실패한_뒤_되돌아가면_나눴다고_적지_않는다() -> None:
    """실패 고지가 «균등으로 나눴다» 로 고정이면, 한 번에 산 안에서 거짓이 된다."""
    state, _ = _강제_선택(_state("일괄_복귀"), "BASE_EQUAL", judged=False)
    decision = dict(_결정(state))
    decision["allocation_judgment"] = SplitAllocationResult(
        interpretation=SplitAllocationChoice(
            chosen_candidate_id="BASE_EQUAL", reason="규칙 기본안"
        ),
        llm_status="FALLBACK",
        llm_provider="fake",
        llm_model="fake-model",
        llm_attempts=2,
        llm_fallback_used=True,
    )
    첫줄, *나머지 = state["split_plan"]
    state["split_plan"] = [{**첫줄, "decision": decision}, *나머지]
    state.update(package_scenarios(state))
    [고지] = [r for r in _공격(state)["risks"] if "배분 판단 미적용" in r]
    assert "한 번에 산다" in 고지 and "나눴다" not in 고지


def test_진입한_날_되돌리면_진입하지_않았다고_적지_않는다() -> None:
    """🔴 같은 안에 «나누지 않는다» 와 «분할에 진입하지 않아» 가 나란히 붙었다 (1차 실험)."""
    state, final = _펴고_검사(_state("한_값"))
    assert _결정(state)["entered"] is True
    for scenario in state["scenarios_final"]:
        assert not [r for r in scenario["risks"] if "분할에 진입하지 않아" in r], scenario["label"]
    assert [r for r in _공격(state)["risks"] if "나누지 않는다" in r]
    _일곱번이_통과시킨다(state, final)


def test_진입하지_않은_날의_축_고지는_그대로다() -> None:
    """반대 방향 — 진입 안 한 날의 고지까지 지우는 변이를 잡는다."""
    state, _ = _펴고_검사(_state("진입_안함"))
    assert TIMING_AXIS in state["allowed_axes"] and _결정(state)["entered"] is False
    assert [r for r in _공격(state)["risks"] if "분할에 진입하지 않아" in r]


# ── 적용 판정 자체 — settle_split ───────────────────────────────────────────


def _draft(total: int, single: int, coverage: int = 12) -> dict:
    return {
        "label": "공격",
        "total_qty_kg": total,
        "raw_qty_kg": total,
        "single_round_cap_kg": single,
        "coverage_days": coverage,
        "clipped_by": [],
    }


def _settle(draft: dict, ratios: list[float], caps: dict, *, by_trend: bool):
    return settle_split(
        draft,
        TIMING_AXIS,
        [{"ratio": r} for r in ratios],
        by_trend=by_trend,
        constraints=load_constraints(),
        as_of=AS_OF.isoformat(),
        lead_days=LEAD_DAYS,
        cap_by_date=caps,
        calendar=None,
    )


def _3회는_안_서고_2회는_서는_창() -> dict[str, float]:
    """12일 커버 · 3회 도착 +2 · +6 · +10 · 2회 도착 +2 · +8."""
    return _창(lambda o: 1_000.0 if o <= 8 else 500.0)


def test_실익이_없으면_다른_회차_수로도_나누지_않는다() -> None:
    """🔴 **실익 판단이 성립 판단보다 먼저다** (2026-09-17).

    한 번에 들어가는 양(넓히지 않음)이고 궤적 진입도 아니면, 고른 회차 수가 안 서도 다른
    회차 수로 나누지 않는다. 전에는 이 경우 2회 균등으로 **나눴다** — 같은 날 같은 안이
    비율에 따라 나뉘기도 하고 안 나뉘기도 해서 ④ 가 미리 알 수 없었다.
    """
    caps = _3회는_안_서고_2회는_서는_창()
    assert _settle(_draft(1_000, 1_000), [1 / 2, 1 / 2], caps, by_trend=True).kind == AS_CHOSEN
    결과 = _settle(_draft(1_000, 1_000), [1 / 3, 1 / 3, 1 / 3], caps, by_trend=False)
    assert 결과.kind == ROLLED_BACK and 결과.ratios is None
    assert "나누지 않는다" in (결과.note or "")


def test_궤적_진입이면_다른_회차_수를_본다() -> None:
    caps = _3회는_안_서고_2회는_서는_창()
    결과 = _settle(_draft(1_000, 1_000), [1 / 3, 1 / 3, 1 / 3], caps, by_trend=True)
    assert 결과.kind == ROUNDS_CHANGED and len(결과.ratios or []) == 2
    assert "3회 분할이 날짜별 여유를 못 지켜 2회 균등으로 바꿨다" in (결과.note or "")


def test_안_넓힌_안을_접을_때는_줄였다고_적지_않는다() -> None:
    """⚠️ 넓히지 않은 안에 «X kg 을 Y kg 으로 되돌렸다» 가 붙으면 안 줄인 것을 줄였다고 읽힌다."""
    좁은_뒤 = _창(lambda o: 1_000.0 if o <= LEAD_DAYS else 10.0)
    결과 = _settle(_draft(1_000, 1_000), [1 / 3, 1 / 3, 1 / 3], 좁은_뒤, by_trend=True)
    assert 결과.kind == ROLLED_BACK
    assert "분할을 접고 한 번에 산다" in (결과.note or "")
    assert "되돌렸다" not in (결과.note or "")
