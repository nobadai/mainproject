"""④ 회차 배분 판단자 (E3-9) — **고르는 것은 id 하나뿐이다.**

🔴 ⑤ 등급 조합과 **같은 규율, 다른 계약**이다. 규율(후보 제한·숫자 금지·검증·fallback·
상태 기록·조립 시 주입)은 같고, 요청·응답 모델은 섞지 않는다.

⚠️ 기능 플래그가 **기본 꺼짐**이라 운영에서는 이 경로가 안 돈다. 여기서 재는 것은
배선이고, 켜는 것은 비율이 승인된 뒤의 별도 판단이다.
"""

from datetime import date

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.llm import split_allocation as sa
from app.purchase_agent.llm.split_schemas import (
    SplitAllocationChoice,
    SplitAllocationResult,
    SplitCandidate,
)

ITEM = "배추"

#: 🔴 **사유 상한은 선언이 소유한다** (2026-09-18 · 규칙 7). 전에는 ④ 가 코드 상수를
#: 들고 있어 ⑤ 와 같은 뜻의 값이 두 곳에 살았다. 여기에 ``300`` 을 베끼면 선언을 옮겨도
#: 검사가 옛 값을 계속 시험한다 — 값 비교로는 「검사가 그 상한을 쓰는가」를 못 증명한다.
상한 = load_constraints()["grade"]["mix_reason_max_chars"]


def _검증(
    응답: str, context: sa.SplitAllocationContext, *, 쓸_상한: int | None = None
) -> SplitAllocationChoice:
    """검증기를 부르는 한 자리. 상한을 안 주면 **선언값**으로 부른다."""
    return sa.validate_choice(
        응답, context, reason_max_chars=상한 if 쓸_상한 is None else 쓸_상한
    )


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
        _검증(_reply("SIDEWAYS"), _context("BASE_EQUAL", "FRONT_LOADED"))
    assert "UNKNOWN_CANDIDATE" in 잡힘.value.issues


def test_사유에_든_숫자는_거부한다() -> None:
    with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
        _검증(
            _reply("FRONT_LOADED", "앞 회차에 60% 싣는다"),
            _context("BASE_EQUAL", "FRONT_LOADED"),
        )
    assert "NUMERIC_OUTPUT_FORBIDDEN" in 잡힘.value.issues


def test_숫자가_든_후보_id_는_거부하지_않는다() -> None:
    """🔴 후보 id 는 규칙이 만든 **식별자**다 — 숫자가 들어가도 정상이다.

    여기 숫자 검사를 걸면 정상 선택이 매번 fallback 으로 떨어진다 (⑤ 와 같은 경계).
    """
    해석 = _검증(
        _reply("FRONT_LOADED_3"), _context("BASE_EQUAL", "FRONT_LOADED_3")
    )
    assert 해석.chosen_candidate_id == "FRONT_LOADED_3"


def test_JSON_이_아니면_거부한다() -> None:
    for 쓰레기 in ('{"chosen": "X"}', "[]", "not json"):
        with pytest.raises(sa.SplitAllocationInvalid):
            _검증(쓰레기, _context("BASE_EQUAL", "FRONT_LOADED"))


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


def _설정(*, enabled: bool = True, reason_max_chars: int | None = None):
    return type(
        "S",
        (),
        {
            "enabled": enabled,
            "max_retries": 1,
            "provider": "anthropic",
            "model": "haiku",
            # 🔴 ``300`` 을 안 베낀다 — 기본은 **선언값**이다 (위 ``상한``).
            "reason_max_chars": 상한 if reason_max_chars is None else reason_max_chars,
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


# ── 가드레일 구멍 메우기 (2026-09-18) ────────────────────────────────────────
#
# 🔴 **⑤ 에만 있던 검사 넷을 여기 복제한다.** 검증기 ``validate_choice`` 에는 넷이 다
#   구현돼 있었는데 **재는 검사가 없었다** — 구현과 검사가 갈린 상태라, 누가 한 줄을
#   지워도 스위트가 초록이었다. 아래 변이 시험으로 그 갈림을 닫는다.


def test_빈_칸은_거부한다() -> None:
    """``reason`` 이 비면 **근거 없는 판단**이 출력에 실리고, id 가 비면 후보 대조가 무의미해진다.

    🔴 스키마로 못 막는다 — 두 API 의 JSON Schema 가 문자열 길이 제약을 지원하지 않아
      ``min_length`` 를 뺐다 (``SplitAllocationChoice`` 참조 · ⑤ 와 같은 사정).
    """
    for 응답 in (
        _reply("", "앞 회차를 두껍게 간다"),
        _reply("BASE_EQUAL", ""),
        _reply("BASE_EQUAL", "   \n\t "),
    ):
        with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
            _검증(응답, _context("BASE_EQUAL", "FRONT_LOADED"))
        assert "EMPTY_FIELD" in 잡힘.value.issues


def test_유니코드_수치도_거부한다() -> None:
    """``\\d`` 만으로는 ``½``·``²``·``Ⅻ`` 를 못 막는다 — ``str.isnumeric()`` 이 그쪽을 덮는다.

    ⚠️ 한글 수사("사할")는 여전히 통과한다. 정규식으로 판별 불가능한 영역이고 그건
      프롬프트가 맡는다 — 여기서 잡는 건 기계적으로 판별되는 것뿐이다.
    """
    for 나쁨 in ("앞 회차가 ½ 더 무겁다", "뒤가 ² 배다", "Ⅻ 단위로 나눈다"):
        with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
            _검증(
                _reply("BASE_EQUAL", 나쁨), _context("BASE_EQUAL", "FRONT_LOADED")
            )
        assert "NUMERIC_OUTPUT_FORBIDDEN" in 잡힘.value.issues


def test_제어문자는_사유에서도_후보_id_에서도_거부한다() -> None:
    """zero-width·bidi 는 ``rationale`` 에 **그대로 실리므로** 표시 안전성 문제다.

    🔴 후보 id 도 같이 본다 — id 는 숫자 검사에서 일부러 빼 뒀지만(식별자라서),
      **제어문자까지 면제하면** 눈에 안 보이는 글자가 섞인 id 가 후보 대조를 통과할 길이
      생긴다. ⑤ 도 두 칸을 같이 본다 (``_validation_issues``).
    """
    # 리터럴 제어문자는 ruff PLE2502(난독화 가능)에 걸린다 — chr() 로 만든다.
    zero_width, nul, bidi = chr(0x200B), chr(0x00), chr(0x202E)
    for 나쁨 in (f"정상{zero_width}텍스트", f"제어{nul}문자", f"방향{bidi}전환"):
        with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
            _검증(
                _reply("BASE_EQUAL", 나쁨), _context("BASE_EQUAL", "FRONT_LOADED")
            )
        assert "CONTROL_CHARACTERS" in 잡힘.value.issues

    숨은_id = f"BASE{zero_width}_EQUAL"
    with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
        _검증(_reply(숨은_id), _context("BASE_EQUAL", 숨은_id))
    assert "CONTROL_CHARACTERS" in 잡힘.value.issues


def test_사유가_상한을_넘으면_거부한다() -> None:
    """상한을 넘으면 **사유가 한 문장이 아니라 서술이 된다** — 화면이 그 문장을 그대로 싣는다.

    🔴 **상수를 베끼지 않는다.** ``300`` 을 여기 적으면 상한을 옮길 때 검사가 옛 값을 계속
      시험한다 — 값 비교로는 「검사가 그 상한을 실제로 쓰는가」를 증명 못 한다 (규칙 8).
    """
    with pytest.raises(sa.SplitAllocationInvalid) as 잡힘:
        _검증(
            _reply("BASE_EQUAL", "가" * (상한 + 1)),
            _context("BASE_EQUAL", "FRONT_LOADED"),
        )
    assert "REASON_TOO_LONG" in 잡힘.value.issues
    # 상한 이하는 통과 — 검사가 상한을 **실제로 쓰는지** 확인한다
    해석 = _검증(
        _reply("BASE_EQUAL", "가" * 상한), _context("BASE_EQUAL", "FRONT_LOADED")
    )
    assert 해석.chosen_candidate_id == "BASE_EQUAL"


def test_사유_상한은_코드가_아니라_설정에서_온다() -> None:
    """🔴 **규칙 8 — 값 비교가 아니라 「바꾸면 따라 바뀌나」로 잰다.**

    전에는 ④ 가 ``REASON_MAX_CHARS = 300`` 을 모듈 상수로 들고 있어, ⑤ 가 읽는 선언
    (``grade.mix_reason_max_chars``)을 옮겨도 **④ 만 옛 값에 남았다.** 두 값이 같아서
    안 아팠을 뿐이다. 이 검사는 상한을 **설정에서 낮춰** 판정이 따라 내려오는지 본다 —
    코드 상수가 다시 생기면 낮춘 상한이 무시되고 여기서 빨개진다.
    """
    사유 = "가" * 6
    떨어짐 = sa.SplitAllocationService(
        _설정(reason_max_chars=5), _됨(_reply("BASE_EQUAL", 사유))
    ).select(_context("BASE_EQUAL", "FRONT_LOADED"), "BASE_EQUAL")
    assert (떨어짐.llm_status, 떨어짐.llm_fallback_used) == ("FALLBACK", True)
    assert 떨어짐.interpretation.reason == "규칙 기본안"

    통과 = sa.SplitAllocationService(
        _설정(reason_max_chars=6), _됨(_reply("BASE_EQUAL", 사유))
    ).select(_context("BASE_EQUAL", "FRONT_LOADED"), "BASE_EQUAL")
    assert (통과.llm_status, 통과.interpretation.reason) == ("SUCCESS", 사유)


def test_판단_기준_네_줄이_지시문에_없다() -> None:
    """🔴 **작업 4 ① 회귀** (2026-09-18 · 검증설계 v0.2 §4).

    네 줄이 전부 못 섰다 — 거짓 하나(「앞 회차가 단가에 유리」) · 확인 못 함 하나 ·
    무용 하나 · 도달 불가 하나. 실측에서 그 거짓이 판단자 사유로 되돌아왔으므로
    (E3-9 2차 · PS-01 스무 사유 전부) 문장을 지웠다. **되살아나면 여기서 빨개진다.**

    ⚠️ 낱말이 아니라 **지시문의 문장**을 본다 — 판단자가 「단가」라고 쓰는 것을 막는
      검사가 아니다. 그건 다음 판이 채점으로 잰다.
    """
    지시문 = sa.SYSTEM_PROMPT
    for 걷어낸_것 in ("판단 기준", "단가에 유리", "오래 늙는다", "앞에 몰기", "CAP_UNKNOWN"):
        assert 걷어낸_것 not in 지시문, 걷어낸_것
    # 남은 것 — 「안전한 후보 중 고르라」
    assert "안전 검사까지 끝낸 것" in 지시문
    # 🔴 지시문이 바뀌었으면 판 이름도 바뀌어야 한다 — 안 그러면 전후를 못 가른다
    assert sa.ROLE.prompt_version == "split-alloc-2"


@pytest.mark.parametrize(
    "실패",
    [
        RuntimeError("ANTHROPIC_API_KEY is not set"),
        TimeoutError("timed out"),
        ConnectionError("server down"),
        ValueError("unexpected sdk error"),
    ],
)
def test_모든_실패_유형이_같은_기본안으로_수렴한다(실패: Exception) -> None:
    """**무엇이 터지든 규칙 기본안이다** — 키 없음·타임아웃·연결 끊김·SDK 예외 무관.

    한 유형만 잡으면 나머지가 그래프를 죽인다. 팀원 환경마다 실패 모양이 다르므로
    「무엇이 터지든 균등」이 요건이다 (⑤ 와 같은 파라미터라이즈).
    """

    class _던짐:
        def generate(self, context, *, retry_guidance=None):
            raise 실패

    결과 = sa.SplitAllocationService(_설정(), _던짐()).select(
        _context("BASE_EQUAL", "FRONT_LOADED"), "BASE_EQUAL"
    )
    assert 결과.llm_status == "FALLBACK"
    assert 결과.llm_fallback_used is True
    assert 결과.interpretation.chosen_candidate_id == "BASE_EQUAL"
