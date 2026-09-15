"""근거 자기 검토의 **대상 고르기와 문장 만들기** (E3-10 · LLM 배선 전).

🔴 여기까지는 판단자가 없다. 재는 것은 **규칙의 결정성**이다 —
같은 입력이면 같은 안이 같은 순서로 뽑히고, 같은 코드 집합이면 같은 문장이 나온다.

⚠️ 그것이 «SUCCESS 결과가 결정적이다» 라는 뜻은 **아니다.** 여기서 고정되는 것은
문장과 순서이고, *"어떤 코드를 고르는가"* 는 판단자의 응답이다.
"""

import pytest

from app.purchase_agent import review_gate as gate
from app.purchase_agent import review_templates as tpl
from app.purchase_agent.llm.text_guard import (
    PLACEHOLDERS,
    contains_number,
    sanitize_numerals,
)


def _신호(label: str, **켠다) -> gate.ScenarioSignals:
    return gate.ScenarioSignals(label=label, **켠다)


# ── 정제 ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "문장",
    [
        "창고 제약으로 원안 5,021kg에서 3,569kg으로 축소",
        "회차 지급일 2026-03-10이 재무의 지급 집중일과 겹친다",
        "등급 스프레드가 21.2% 확대됐다",
        "확정주문 10,042.2kg → 일평균 717kg",
    ],
)
def test_정제하면_숫자가_안_남는다(문장: str) -> None:
    """🔴 원본 숫자를 넣으면 판단자가 **사유에 베껴 쓴다** (규칙 6).

    ⑤ 가 라벨만 넘기기로 한 이유와 같다.
    """
    assert contains_number(sanitize_numerals(문장)) is False


def test_표시_자체에_숫자가_없다() -> None:
    """``<N1>`` 처럼 번호를 붙이면 **자기가 만든 표시에 검사가 걸린다.**"""
    assert not any(contains_number(p) for p in PLACEHOLDERS)


def test_날짜를_먼저_가린다() -> None:
    """숫자를 먼저 가리면 ``2026-01-05`` 가 뭉개져 **무엇이었는지 사라진다.**"""
    assert sanitize_numerals("2026-01-05 도착") == "<DATE> 도착"


def test_문장의_뜻은_남는다() -> None:
    """값만 가리고 **서술은 그대로 둔다** — 안 그러면 검토할 것이 없어진다."""
    가린 = sanitize_numerals("창고 제약으로 원안 5,021kg에서 3,569kg으로 축소")
    assert "창고 제약으로 원안" in 가린 and "축소" in 가린


# ── 게이트 ─────────────────────────────────────────────────────
def test_신호가_없으면_안_본다() -> None:
    결과 = gate.choose([_신호("기본"), _신호("공격")])
    assert 결과.selected == ()
    assert 결과.skipped_by_gate == ("기본", "공격")


def test_보류만_있는_안은_뺀다() -> None:
    """🔴 검토할 **판단 문장이 없다** — 그 사유는 부족 정보 요청 소관이다."""
    결과 = gate.choose([_신호("기본", quantity_clipped=True, only_deferred_risks=True)])
    assert 결과.selected == ()
    assert 결과.skipped_by_gate == ("기본",)


def test_신호가_많은_안을_먼저_본다() -> None:
    결과 = gate.choose(
        [
            _신호("보수", label_body_mismatch=True),
            _신호("기본", mix_applied=True, quantity_clipped=True),
        ]
    )
    assert 결과.selected == ("기본", "보수")


def test_같은_개수면_선언_순서로_가른다() -> None:
    """🔴 판단자가 실제로 고른 안(E)이 라벨 어긋남(D)보다 볼 값이 크다."""
    결과 = gate.choose(
        [_신호("보수", label_body_mismatch=True), _신호("기본", mix_applied=True)]
    )
    assert 결과.selected == ("기본", "보수")


def test_같은_신호면_라벨_순서로_가른다() -> None:
    """보수 · 기본 · 공격 — ``ScenarioLabel`` 선언 순서다."""
    결과 = gate.choose(
        [
            _신호("공격", quantity_clipped=True),
            _신호("보수", quantity_clipped=True),
            _신호("기본", quantity_clipped=True),
        ]
    )
    assert 결과.selected == ("보수", "기본", "공격")


def test_두_번_돌려도_같다() -> None:
    """집합 순회나 해시 순서에 기대면 실행마다 달라진다."""
    신호 = [
        _신호("공격", mix_applied=True),
        _신호("보수", quantity_clipped=True, payment_conflict=True),
        _신호("기본", label_body_mismatch=True),
    ]
    assert gate.choose(신호) == gate.choose(신호)


def test_상한은_운영에서_안_걸린다() -> None:
    """실측에서 봉투당 대상이 **최대 3** 이라 상한 3 은 정상 대상을 안 버린다."""
    assert gate.PER_RUN_LIMIT == 3
    신호 = [_신호(라벨, quantity_clipped=True) for 라벨 in ("보수", "기본", "공격")]
    assert gate.choose(신호).skipped_by_budget == ()


def test_대상이_넷_이상이면_상한에_걸린다() -> None:
    """🔴 **합성 입력으로만 재는 경로다.** 운영에서는 안이 최대 3이라 안 걸린다.

    ⚠️ 상한을 2로 낮춰 운영에서 걸리게 만드는 것은 제품 근거가 아니다 — 정상 대상을
    일부러 버리는 일이다.
    """
    신호 = [_신호(f"합성{i}", quantity_clipped=True) for i in range(4)]
    결과 = gate.choose(신호)
    assert len(결과.selected) == 3
    assert len(결과.skipped_by_budget) == 1


def test_못_본_안을_목록에서_그냥_안_뺀다() -> None:
    """🔴 안 본 것을 지우면 **「봤는데 깨끗했다」와 구분되지 않는다.**"""
    신호 = [_신호(f"합성{i}", quantity_clipped=True) for i in range(4)] + [_신호("조용")]
    결과 = gate.choose(신호)
    보인_라벨 = set(결과.selected) | set(결과.skipped_by_gate) | set(결과.skipped_by_budget)
    assert 보인_라벨 == {s.label for s in 신호}


# ── 템플릿 ─────────────────────────────────────────────────────
def test_같은_집합이면_순서가_같다() -> None:
    """🔴 판단자가 **뒤집어 돌려줘도** 같은 문장이 같은 순서로 선다."""
    앞 = tpl.render([("CLAIM_SOURCE_MISMATCH", "Q-1"), ("LABEL_BODY_MISMATCH", None)], {"Q-1"})
    뒤 = tpl.render([("LABEL_BODY_MISMATCH", None), ("CLAIM_SOURCE_MISMATCH", "Q-1")], {"Q-1"})
    assert 앞 == 뒤


def test_같은_지적을_두_번_안_적는다() -> None:
    결과 = tpl.render([("LABEL_BODY_MISMATCH", None)] * 3, set())
    assert len(결과) == 1


def test_모르는_코드는_거부한다() -> None:
    """제시한 집합 밖의 코드는 **없는 지적**이다 — 후보 밖 id 를 막는 것과 같은 규율."""
    with pytest.raises(tpl.UnknownFinding):
        tpl.render([("MADE_UP_CODE", None)], set())


def test_없는_근거를_가리키면_거부한다() -> None:
    """입력에 없던 ``ref_id`` 는 **지어낸 근거**다."""
    with pytest.raises(tpl.UnknownFinding):
        tpl.render([("CLAIM_SOURCE_MISMATCH", "DOC-999")], {"Q-1"})


def test_가리켜야_하는_지적은_근거를_요구한다() -> None:
    """*"이 근거가"* 를 가리켜야 뜻이 서는 지적이 있다."""
    with pytest.raises(tpl.UnknownFinding):
        tpl.render([("CLAIM_SOURCE_MISMATCH", None)], {"Q-1"})


def test_가리킬_근거가_없는_지적은_안_요구한다() -> None:
    """🔴 없는 것을 필수로 두면 판단자가 **아무 근거나 붙인다.**"""
    assert tpl.render([("RISK_CATEGORY_MISSING", None)], set())


def test_문장에_내부_용어가_안_들어간다() -> None:
    """화면과 Critic 이 읽는다 — 단계 이름이 그대로 나가면 읽는 사람이 못 읽는다."""
    금지 = ("SKIPPED", "GATE", "BUDGET", "rule_only", "fallback", "LLM")
    for 틀 in tpl.FINDINGS.values():
        assert not any(말 in 틀.template for 말 in 금지), 틀.code
