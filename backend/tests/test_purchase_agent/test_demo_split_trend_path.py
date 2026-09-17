"""시연 경로 — 가격 지속 상승으로 분할에 들어가 판단자 선택이 최종안에 적용되기까지 (E3-9).

🔴 **입력은 인위 조건이다 — 실측이 아니다.** ``fixtures/demo_split_trend_artificial.json``

  실제 예측 배치 전수(``v_ml_price_forecast`` · AUC · 528개 · 2025-12-31~2026-09-17)를 읽어
  stable · 상승률 10% 이상 · 지속 상승(2026-09-17 정의)을 **함께** 만족한 사례를 찾았고
  **0건**이었다. 그래서 실제 배치의 **모양**(주말 복사 행 · 앞 장날 lead_time 게이트 = 앵커 ·
  보합 섞인 상승)만 따르고 값은 mock_rising 배추에 맞춘 입력을 따로 만들었다.
  ``model_version`` 이 ``demo-artificial-v0`` 라 근거 ``ref_id`` 에도 그 표시가 남는다.

★ **네 단계를 따로 잰다** — 하나가 서도 다음이 선다는 뜻이 아니다.

```text
1 분할 생성          ① 가격 경로로 축이 열리고 ④ 가 궤적으로 진입해 회차가 선다
2 안전한 복수 후보    남긴 후보가 둘 이상이고, 남긴 비균등 후보는 ⑥ 이 그대로 적용한다
3 선택의 최종 적용    판단자가 고른 배분이 공격안 회차 물량으로 나간다
4 검증 통과          ⑦ 사중 일치 · 도착일 · 창고 · 지급 검사와 출력 스키마를 그대로 지난다
```

🔴 **외부 호출이 없다.** 판단자는 호출 수를 세는 가짜 프로바이더이고, 세션 전체가 LLM 키를
  빈 값으로 둔다 (``conftest._disable_llm_by_default``). 기능 플래그는 이 검사 안에서만 켠다.
🔴 **이 검사는 경로가 이어지는지를 잰다.** 앞 회차에 싣는 배분이 유리하다거나 분할이
  경제적이라는 근거가 아니다.
"""

import json
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from app.purchase_agent import adapter
from app.purchase_agent.allocation import allocation_candidates, split_quantities
from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import build_graph
from app.purchase_agent.nodes.classify_situation import (
    TREND_RISING,
    classify_situation,
    compute_rise_rate_2w,
    judge_sustained_rise,
)
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.schemas import TIMING_AXIS
from app.purchase_agent.state import build_initial_state
from tests.test_purchase_agent.test_split_allocation_applied import (
    _강제_선택,
    _결정,
    _공격,
    _그대로_적용됐나,
    _선택자,
    _일곱번이_통과시킨다,
    _펴고_검사,
    세는_프로바이더,
)

FIXTURE = Path(__file__).parent / "fixtures" / "demo_split_trend_artificial.json"
FLAG = "PURCHASE_LLM_SPLIT_ALLOCATION_ENABLED"
LEAD_DAYS = 2
N5 = 7


def _시연_문서() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _초기_state() -> dict:
    """① 전 State — mock 배추 2026-08-21 에 **시연 예측 · 넉넉한 날짜별 여유 · N4 · N5** 를 꽂는다.

    여유를 넉넉히 두는 이유 — 수량 경로가 서면 무엇으로 진입했는지 흐려진다.
    가격 경로 하나로 좁힌다.
    """
    문서 = _시연_문서()
    as_of = date.fromisoformat(문서["as_of"])
    state = build_initial_state(문서["item"], as_of)
    state["forecast"] = {k: v for k, v in 문서.items() if not k.startswith("_") and k != "as_of"}
    state["inbound_lead_days"] = LEAD_DAYS
    state["purchase_payment_days"] = N5
    state["inventory"] = {
        **state["inventory"],
        "cap_by_date": {(as_of + timedelta(days=o)).isoformat(): 1_000_000.0 for o in range(40)},
        "warehouse_free_kg": 1_000_000.0,
        "rental_cap_kg": 0.0,
    }
    return state


def _분류와_초안까지() -> dict:
    state = _초기_state()
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    return state


@pytest.fixture
def 배분_판단_켜짐(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **이 검사 안에서만** 켠다. 배분 비율 선언은 파일이 이미 ``APPROVED`` 다 (`#794`)."""
    monkeypatch.setenv(FLAG, "true")


def test_시연_입력은_인위_조건임을_드러내고_가격_경로만_연다() -> None:
    문서 = _시연_문서()
    assert 문서["_provenance"]["kind"].startswith("ARTIFICIAL")
    assert "demo-artificial" in 문서["model_version"]

    constraints = load_constraints()
    state = _분류와_초안까지()
    forecast = state["forecast"]
    day = constraints["situation"]["ci_judgment_day"]
    assert state["situation"] == "stable"
    assert compute_rise_rate_2w(forecast, day) >= constraints["triggers"]["pre_purchase_rise_rate"]
    trend = judge_sustained_rise(forecast, constraints)
    assert trend.verdict == TREND_RISING
    # 복사 행 · 보합 · lead_time 게이트가 **섞인 채로** 통과한다 — 옛 정의(엄격 증가)였다면 닫혔다
    창 = forecast["daily"][:day]
    assert any(r["is_filled"] for r in 창) and any(r["gate_reason"] == "lead_time" for r in 창)
    assert any(a[1] == b[1] for a, b in pairwise(trend.points)), "보합이 섞여 있어야 한다"
    assert TIMING_AXIS in state["allowed_axes"]


def test_1_분할이_생성된다() -> None:
    """판단자 없이(플래그 꺼짐) — 진입 · 회차 · 공격안 분할까지. 선택은 균등이다."""
    state, _ = _펴고_검사(_분류와_초안까지())
    결정 = _결정(state)
    assert 결정["entered"] is True
    assert (결정["by_trend"], 결정["by_volume"]) == (True, False), "가격 경로로만 진입해야 한다"
    assert 결정["trend_verdict"] == TREND_RISING
    assert 결정["rounds"] == 2
    공격 = _공격(state)
    assert 공격["strategy_type"] == TIMING_AXIS
    assert len(공격["split_plan"]) == 결정["rounds"]
    assert len({leg["date"] for leg in 공격["split_plan"]}) == 결정["rounds"]
    assert 결정["allocation_chosen"] == "BASE_EQUAL"


def test_2_안전한_복수_후보가_남는다() -> None:
    """남긴 후보가 둘 이상이고, **남긴 비균등 후보마다** ⑥ 이 회차 · 비율 · 물량 그대로 적용한다."""
    base = _분류와_초안까지()
    state, _ = _펴고_검사(base)
    결정 = _결정(state)
    assert 결정["allocation_approved"] is True
    assert len(결정["allocation_candidates"]) >= 2
    비균등 = set(결정["allocation_candidates"]) - {"BASE_EQUAL"}
    assert 비균등, "고를 것이 균등 하나뿐이면 복수 후보가 아니다"
    assert 결정["allocation_excluded"] == {}

    선언 = load_constraints()["split"]["allocation_weights"]
    for 후보_id in sorted(비균등):
        ratios = allocation_candidates(선언, 결정["rounds"])[후보_id]
        forced, final = _강제_선택(base, 후보_id, judged=False)
        assert _그대로_적용됐나(_공격(forced), ratios, 결정["rounds"]), 후보_id
        _일곱번이_통과시킨다(forced, final)


def test_3_선택_결과가_최종안에_그대로_적용된다(배분_판단_켜짐) -> None:
    provider = 세는_프로바이더("FRONT_LOADED", reason="시연 — 가짜 판단자")
    state, _ = _펴고_검사(_분류와_초안까지(), _선택자(provider))
    결정 = _결정(state)
    assert provider.calls == 1
    assert 결정["allocation_chosen"] == "FRONT_LOADED"
    ratios = allocation_candidates(
        load_constraints()["split"]["allocation_weights"], 결정["rounds"]
    )["FRONT_LOADED"]
    공격 = _공격(state)
    assert _그대로_적용됐나(공격, ratios, 결정["rounds"])
    assert [leg["qty_kg"] for leg in 공격["split_plan"]] == split_quantities(
        공격["total_qty_kg"], [{"ratio": r} for r in ratios]
    )
    [배분] = [r for r in 공격["rationale"] if r["claim"].startswith("회차 배분 ")]
    assert "FRONT_LOADED" in 배분["claim"] and "그대로 적용했다" in 배분["evidence_detail"]
    assert 배분["ref_id"].startswith("FC-demo-artificial-v0-"), "인위 입력 표시가 근거에 남는다"
    [흔적] = adapter._split_allocation_call(state)
    assert (흔적.status, 흔적.provider) == ("SUCCESS", "fake")


def test_4_검증을_통과한다(배분_판단_켜짐) -> None:
    """⑦ 을 우회하지 않는다 — 노드 단위로 한 번, **그래프 전체(⑧ · 출력 스키마 포함)** 로 한 번."""
    provider = 세는_프로바이더("FRONT_LOADED", reason="시연 — 가짜 판단자")
    state, final = _펴고_검사(_분류와_초안까지(), _선택자(provider))
    _일곱번이_통과시킨다(state, final)
    assert {r.get("kind") for r in final["rejected_reasons"]} <= {"not_needed"}
    노드_공격 = [leg["qty_kg"] for leg in _공격(state)["split_plan"]]

    그래프_제공자 = 세는_프로바이더("FRONT_LOADED", reason="시연 — 가짜 판단자")
    최종 = build_graph(split_allocation_selector=_선택자(그래프_제공자)).invoke(_초기_state())
    제안 = 최종["proposal"]
    assert 그래프_제공자.calls == 1
    [공격] = [s for s in 제안["scenarios"] if s["label"] == "공격"]
    assert 공격["strategy_type"] == TIMING_AXIS
    assert [leg["qty_kg"] for leg in 공격["split_plan"]] == 노드_공격
    assert sum(leg["qty_kg"] for leg in 공격["split_plan"]) == 공격["total_qty_kg"]
