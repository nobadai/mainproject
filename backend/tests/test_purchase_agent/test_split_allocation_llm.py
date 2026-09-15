"""④ 회차 배분 판단자 (E3-9) — **고르는 것은 id 하나뿐이다.**

🔴 ⑤ 등급 조합과 **같은 규율, 다른 계약**이다. 규율(후보 제한·숫자 금지·검증·fallback·
상태 기록·조립 시 주입)은 같고, 요청·응답 모델은 섞지 않는다.

⚠️ 기능 플래그가 **기본 꺼짐**이라 운영에서는 이 경로가 안 돈다. 여기서 재는 것은
배선이고, 켜는 것은 비율이 승인된 뒤의 별도 판단이다.
"""

from datetime import date

import pytest

from app.purchase_agent.llm import split_allocation as sa
from app.purchase_agent.llm.split_schemas import (
    SplitAllocationChoice,
    SplitAllocationResult,
    SplitCandidate,
)

ITEM = "배추"


def _context(*ids: str) -> sa.SplitAllocationContext:
    return sa.build_context(
        ITEM,
        rounds=3,
        rising=True,
        cap_tight=False,
        signals=["SPLIT_ENTERED_BY_VOLUME"],
        facts=["규칙이 만든 배분 후보 중 하나를 고른다."],
        candidates=[SplitCandidate(candidate_id=i, summary=i) for i in ids],
    )


def _reply(candidate_id: str, reason: str = "상승 궤적이라 앞 회차를 두껍게 간다") -> str:
    return SplitAllocationChoice(
        chosen_candidate_id=candidate_id, reason=reason
    ).model_dump_json()


def test_컨텍스트에_숫자가_없다() -> None:
    """🔴 값을 넘기면 판단자가 **사유에 베껴 쓴다** (규칙 6 · ⑤ 와 같은 규율)."""
    from app.purchase_agent.llm.text_guard import contains_number

    본문 = _context("BASE_EQUAL", "FRONT_LOADED").model_dump_json()
    assert contains_number(본문) is False


def test_응답_스키마에_숫자_필드가_없다() -> None:
    """비율·수량·날짜를 돌려받으면 그 순간 **LLM 이 만든 숫자**가 출력에 실린다."""
    칸 = sa.SplitAllocationChoice.model_json_schema()["properties"]
    assert set(칸) == {"chosen_candidate_id", "reason"}
    assert all(정의.get("type") == "string" for 정의 in 칸.values())


def test_모르는_후보는_거부한다() -> None:
    """후보 밖 id 는 **비율을 지어낸 것과 같다** — 노드가 그 id 로 비율을 못 찾는다."""
    with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
        sa.validate_choice(_reply("SIDEWAYS"), _context("BASE_EQUAL", "FRONT_LOADED"))
    assert "UNKNOWN_CANDIDATE" in 잡힘.value.issues


def test_사유에_든_숫자는_거부한다() -> None:
    with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
        sa.validate_choice(
            _reply("FRONT_LOADED", "앞 회차에 60% 싣는다"),
            _context("BASE_EQUAL", "FRONT_LOADED"),
        )
    assert "NUMERIC_OUTPUT_FORBIDDEN" in 잡힘.value.issues


def test_숫자가_든_후보_id_는_거부하지_않는다() -> None:
    """🔴 후보 id 는 규칙이 만든 **식별자**다 — 숫자가 들어가도 정상이다.

    여기 숫자 검사를 걸면 정상 선택이 매번 fallback 으로 떨어진다 (⑤ 와 같은 경계).
    """
    해석 = sa.validate_choice(
        _reply("FRONT_LOADED_3"), _context("BASE_EQUAL", "FRONT_LOADED_3")
    )
    assert 해석.chosen_candidate_id == "FRONT_LOADED_3"


def test_JSON_이_아니면_거부한다() -> None:
    for 쓰레기 in ('{"chosen": "X"}', "[]", "not json"):
        with pytest.raises(sa.SplitAllocationInvalid):
            sa.validate_choice(쓰레기, _context("BASE_EQUAL", "FRONT_LOADED"))


def test_후보가_하나면_안_부른다() -> None:
    """고를 것이 없는데 부르면 **비용만 들고 상태만 흐려진다.**"""
    assert sa.needs_call(_context("BASE_EQUAL")) is False
    assert sa.needs_call(_context("BASE_EQUAL", "FRONT_LOADED")) is True


def test_못_본_여유를_넉넉함으로_안_접는다() -> None:
    """🔴 **규칙 3.** 모르는 것을 넉넉함으로 읽으면 모르는 쪽으로 물량이 밀린다."""
    ctx = sa.build_context(
        ITEM, rounds=2, rising=False, cap_tight=None, signals=[], facts=[], candidates=[]
    )
    assert ctx.cap == "CAP_UNKNOWN"


class _터짐:
    def generate(self, context, *, retry_guidance=None):
        raise RuntimeError("키가 없다")


class _됨:
    def __init__(self, 응답: str):
        self.응답 = 응답

    def generate(self, context, *, retry_guidance=None):
        return self.응답


def _설정(*, enabled: bool = True):
    return type(
        "S",
        (),
        {
            "enabled": enabled,
            "max_retries": 1,
            "provider": "anthropic",
            "model": "haiku",
            "reason_max_chars": 300,
        },
    )()


def test_전면_실패하면_기본안으로_돌아간다() -> None:
    """🔴 **회귀가 아니라 무변화다** — 기본안은 규칙이 고르던 값(균등)이다."""
    결과 = sa.SplitAllocationService(_설정(), _터짐()).select(
        _context("BASE_EQUAL", "FRONT_LOADED"), "BASE_EQUAL"
    )
    assert 결과.interpretation.chosen_candidate_id == "BASE_EQUAL"
    assert (결과.llm_status, 결과.llm_fallback_used, 결과.llm_attempts) == (
        "FALLBACK",
        True,
        2,
    )


def test_꺼져_있으면_부르지도_않는다() -> None:
    결과 = sa.SplitAllocationService(_설정(enabled=False), _터짐()).select(
        _context("BASE_EQUAL", "FRONT_LOADED"), "BASE_EQUAL"
    )
    assert (결과.llm_status, 결과.llm_attempts) == ("DISABLED", 0)


def test_성공하면_고른_후보가_실린다() -> None:
    결과 = sa.SplitAllocationService(_설정(), _됨(_reply("FRONT_LOADED"))).select(
        _context("BASE_EQUAL", "FRONT_LOADED"), "BASE_EQUAL"
    )
    assert 결과.interpretation.chosen_candidate_id == "FRONT_LOADED"
    assert 결과.llm_status == "SUCCESS"


def test_역할_지시문이_다섯번과_다르다() -> None:
    """🔴 **두 역할이 같은 지시문을 쓰면** 후보의 뜻이 섞인다."""
    from app.purchase_agent.llm.runtime import MIX_ROLE

    assert sa.ROLE.system_prompt != MIX_ROLE.system_prompt
    assert sa.ROLE.response_schema != MIX_ROLE.response_schema


def _노드결과(*, 켬: bool, monkeypatch: pytest.MonkeyPatch, selector=None):
    from app.purchase_agent.nodes import split_plan as sp
    from app.purchase_agent.nodes.classify_situation import classify_situation
    from app.purchase_agent.nodes.draft_plan import draft_plan
    from app.purchase_agent.state import build_initial_state

    monkeypatch.setattr(sp, "enabled", lambda key, default=False: 켬)
    state = build_initial_state(ITEM, date(2026, 8, 21))
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    return sp.split_plan(state, selector=selector)


def test_플래그가_꺼지면_판단자를_안_부른다(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본이 꺼짐이다 — 비율이 아직 승인 전(``PROVISIONAL``)이기 때문이다."""
    불렸나 = []

    def selector(context, default_candidate_id):
        불렸나.append(1)
        raise AssertionError("꺼졌는데 불렸다")

    결과 = _노드결과(켬=False, monkeypatch=monkeypatch, selector=selector)
    assert 불렸나 == []
    assert 결과["split_plan"][0]["decision"]["allocation_chosen"] == "BASE_EQUAL"


def test_켜도_후보가_하나면_판단자를_안_부른다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 선언이 ``PROVISIONAL`` 이라 후보가 균등 하나뿐이다 — 고를 것이 없다."""
    불렸나 = []

    def selector(context, default_candidate_id):
        불렸나.append(1)
        return SplitAllocationResult(
            interpretation=SplitAllocationChoice(
                chosen_candidate_id=default_candidate_id, reason="기본안"
            ),
            llm_status="SKIPPED_TEMPLATE",
            llm_provider=None,
            llm_model=None,
            llm_attempts=0,
            llm_fallback_used=False,
        )

    결과 = _노드결과(켬=True, monkeypatch=monkeypatch, selector=selector)
    후보 = 결과["split_plan"][0]["decision"]["allocation_candidates"]
    assert 후보 == ["BASE_EQUAL"]
    assert 결과["split_plan"][0]["decision"]["allocation_chosen"] == "BASE_EQUAL"
