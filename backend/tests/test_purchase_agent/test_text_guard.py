"""``text_guard`` — 숫자·제어문자 판정과 **거는 자리의 경계**.

🔴 이 파일이 재는 것은 둘이다.

① 판정 자체가 옮기기 전과 같은가 (``llm/runtime.py`` 에서 공개 이름으로 올렸다)
② 🔴 **자연어 필드에만 거는가** — ``candidate_id`` 처럼 숫자가 들어갈 수 있는 칸을
   숫자 응답으로 오인해 거부하면, 멀쩡한 선택이 매번 fallback 으로 떨어진다.

★ ②는 ``runtime`` 이 이미 지키던 경계다 (*"chosen_candidate_id 는 검사 대상이 아니다 —
후보 id 에 숫자가 들어갈 수 있다"*). 그 경계를 **검사로 못박아**, 앞으로 붙는 역할이
같은 실수를 반복하지 않게 한다.
"""

import ast
from pathlib import Path

import pytest

from app.purchase_agent.llm import text_guard
from app.purchase_agent.llm.mix import build_mix_context
from app.purchase_agent.llm.runtime import MixValidationError, validate_interpretation
from app.purchase_agent.llm.schemas import MixCandidate, SanitizedLLMContext

ITEM = "배추"


def _context(candidate_ids: tuple[str, ...]) -> SanitizedLLMContext:
    return build_mix_context(
        ITEM,
        spread_widened=True,
        shelf_days=6.0,
        shelf_tight=True,
        signals=["GRADE_SPREAD_WIDENED"],
        facts=["등급 스프레드가 평시보다 확대됐다."],
        candidates=[MixCandidate(candidate_id=cid, summary=cid) for cid in candidate_ids],
    )


def _reply(candidate_id: str, reason: str) -> str:
    return f'{{"chosen_candidate_id": "{candidate_id}", "reason": "{reason}"}}'


@pytest.mark.parametrize(
    ("문장", "숫자가_있나"),
    [
        ("숫자 없는 문장이다", False),
        ("중품이 130원 싸다", True),
        ("전각 １２３ 도 잡는다", True),
        ("½ 조각은 정규식이 못 잡는다", True),
        ("Ⅻ 같은 로마 숫자도", True),
        ("", False),
        ("백삼십원", False),
    ],
)
def test_숫자_판정이_옮기기_전과_같다(문장: str, 숫자가_있나: bool) -> None:
    """⚠️ 마지막 줄이 **이 검사의 한계**다 — 한글 수사는 정규식으로 못 막는다.

    그건 판단 영역이라 프롬프트가 맡는다. 여기서 잡는 것은 기계적으로 판별 가능한
    것뿐이고, 그 한계를 알고 쓴다 (모듈 주석과 같은 사실).
    """
    assert text_guard.contains_number(문장) is 숫자가_있나


def test_줄바꿈과_탭은_제어문자로_안_센다() -> None:
    """문장 안에서 정상으로 쓰이는 둘이라 걸면 멀쩡한 응답이 죽는다."""
    assert text_guard.contains_control_chars("줄\n바꿈\t있음") is False


def test_보이지_않는_문자는_잡는다() -> None:
    """zero-width 는 rationale 에 그대로 실려 화면에서 안 보인다 — 표시 안전성 문제다."""
    assert text_guard.contains_control_chars("상품\u200b등급") is True


def test_숫자가_든_후보_id_는_거부하지_않는다() -> None:
    """🔴 **착수 조건 ③ — 자연어 필드에만 건다.**

    후보 id 는 규칙이 만든 식별자라 숫자가 들어갈 수 있다. 여기에 숫자 검사를 걸면
    «LLM 이 숫자를 지어냈다» 로 오인해 **정상 선택이 매번 fallback 으로 떨어진다.**
    """
    문제없는_응답 = _reply("MID_CAPPED_60", "중품을 상한까지 담고 나머지는 기준등급으로 간다")
    해석 = validate_interpretation(
        문제없는_응답, _context(("BASE_ONLY", "MID_CAPPED_60")), reason_max_chars=300
    )
    assert 해석.chosen_candidate_id == "MID_CAPPED_60"


def test_사유에_든_숫자는_그대로_거부한다() -> None:
    """경계의 반대쪽 — id 는 봐주고 **사유는 안 봐준다**.

    둘을 한 검사로 뭉치면 어느 쪽을 풀어 준 것인지 아무도 모르게 된다.
    """
    with pytest.raises(MixValidationError):
        validate_interpretation(
            _reply("MID_CAPPED_60", "중품이 130원 싸다"),
            _context(("BASE_ONLY", "MID_CAPPED_60")),
            reason_max_chars=300,
        )


def test_runtime_의_밑줄_이름을_빌려_쓰는_모듈이_없다() -> None:
    """🔴 **착수 조건 ② — private 을 빌려 쓰지 않는다.**

    빌려 쓰면 ``runtime`` 을 고칠 때 **누가 그 안을 들여다보고 있는지** 알 수 없다.
    공개 이름으로 올린 것이 이 판이고, 이 줄이 되돌아가는 것을 막는다.
    """
    빌린_곳 = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.purchase_agent.llm.runtime":
                continue
            빌린_곳 += [
                f"{path}:{alias.name}" for alias in node.names if alias.name.startswith("_")
            ]
    assert 빌린_곳 == []
