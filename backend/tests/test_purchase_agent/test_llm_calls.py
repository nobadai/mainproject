"""역할별 LLM 호출 기록과 **요약의 뜻** (M-1 · M-2).

🔴 요약 칸 넷(``llm_status``·``llm_model``·``llm_attempts``·``llm_fallback_used``)은
**호출 하나를 전제**한다. 역할이 둘·셋으로 늘면 그 칸으로는 *"어느 역할이 fallback
이었나"* 를 영영 못 읽는다. 그래서 정본을 ``llm_calls`` 로 옮기고 넷은 **결정적으로
접은 요약**으로 남겼다.

★ 이 파일이 지키는 것은 «접는 규칙의 뜻» 이다 — 특히 ``DISABLED``(설정이 꺼짐)와
``SKIPPED_TEMPLATE``(켜졌는데 호출 조건이 아님)을 **안 뭉치는 것**.

⚠️ 계약 자체는 ``app/master/envelope.py`` 에 있다(마스터 소유). 여기서는 **매입이 그
계약을 어떻게 쓰는가**와 접는 규칙을 잠근다.
"""

from datetime import date

import pytest

from app.master.envelope import (
    ContractViolation,
    LLMCallMetadata,
    summarize_llm_calls,
)
from app.purchase_agent.adapter import SOURCING_SELECTION

ITEM = "배추"
AS_OF = date(2026, 8, 21)


def _호출(status: str, **칸) -> LLMCallMetadata:
    if status.startswith("SKIPPED_"):
        칸.setdefault("skip_reason", "사유")
    return LLMCallMetadata(role="r", status=status, **칸)


def test_빈_목록은_예전_그대로다() -> None:
    """이 칸을 안 채우는 파트는 **아무것도 안 바뀐다** — 그것이 하위 호환이다."""
    assert summarize_llm_calls(()) == ("DISABLED", "", 0, False)


@pytest.mark.parametrize(
    "status", ["SUCCESS", "FALLBACK", "DISABLED", "SKIPPED_TEMPLATE"]
)
def test_역할이_하나면_요약이_그_상태_그대로다(status: str) -> None:
    """🔴 **역할이 하나뿐이던 때와 값이 같아야 한다** — 안 그러면 이 변경이 회귀다."""
    assert summarize_llm_calls((_호출(status),))[0] == status


def test_하나라도_fallback_이면_fallback_이다() -> None:
    """성공한 역할이 있어도 **떨어진 역할이 있으면 떨어진 실행**이다."""
    상태, _, 시도, 떨어짐 = summarize_llm_calls(
        (
            _호출("SUCCESS", attempts=1, model="haiku"),
            _호출("FALLBACK", attempts=2, fallback_used=True, model="haiku"),
        )
    )
    assert (상태, 시도, 떨어짐) == ("FALLBACK", 3, True)


def test_게이트로_안_부른_것을_꺼짐으로_안_접는다() -> None:
    """🔴 **이것이 이 요약의 요점이다.**

    ``DISABLED`` 는 *"설정이 꺼짐"* 이고 ``SKIPPED_BY_GATE`` 는 *"켜져 있는데 사전검사가
    안 골랐다"* 다. 뭉치면 **「왜 안 돌았나」가 거짓이 된다** — 사람이 설정을 들여다보다
    없는 문제를 찾는다.
    """
    assert summarize_llm_calls((_호출("SKIPPED_BY_GATE"),))[0] == "SKIPPED_TEMPLATE"
    assert summarize_llm_calls((_호출("SKIPPED_BUDGET"),))[0] == "SKIPPED_TEMPLATE"


def test_전부_꺼졌을_때만_꺼짐이다() -> None:
    assert summarize_llm_calls((_호출("DISABLED"), _호출("DISABLED")))[0] == "DISABLED"
    assert (
        summarize_llm_calls((_호출("DISABLED"), _호출("SKIPPED_BY_GATE")))[0]
        == "SKIPPED_TEMPLATE"
    )


def test_건너뛴_호출은_사유를_반드시_적는다() -> None:
    """🔴 이유 없는 「그 밖」 상태를 두면 **거기로 다 흘러간다.**

    ``REVIEW_NOT_RUN`` 같은 포괄 칸을 안 만든 것도 같은 이유다.
    """
    with pytest.raises(ContractViolation):
        LLMCallMetadata(role="r", status="SKIPPED_BY_GATE")
    with pytest.raises(ContractViolation):
        LLMCallMetadata(role="r", status="SKIPPED_BUDGET", skip_reason="   ")


def test_모델이_여럿이면_멈춘다() -> None:
    """🔴 빈 문자열로 적으면 **「모델 없음」과 「여러 모델」이 같아진다.**

    지금은 매입의 역할이 같은 모델을 쓰도록 제한했고, 그 제한이 깨지는 날 이 등식을
    고치는 것이 계약 변경(M-3)이다 — 조용히 빈칸으로 넘어가지 않게 막는다.
    """
    with pytest.raises(ContractViolation):
        summarize_llm_calls(
            (_호출("SUCCESS", model="haiku"), _호출("SUCCESS", model="opus"))
        )


def _메타():
    from app.purchase_agent.adapter import build_state  # noqa: F401  (경로 확인용)
    from app.purchase_agent.graph import build_graph
    from app.purchase_agent.state import build_initial_state

    state = build_initial_state(ITEM, AS_OF)
    build_graph().invoke(state)
    return state


def test_매입은_역할마다_한_줄을_남긴다() -> None:
    """⑤를 **부를 자리까지 못 간 실행**도 한 줄을 남긴다.

    안 남기면 목록이 비어 「설정이 꺼졌다」와 구분되지 않는다 — ``_uncalled_status`` 가
    가르던 그 자리다.
    """
    from app.purchase_agent.adapter import _llm_calls

    비었을_때 = _llm_calls(None)
    assert [c.role for c in 비었을_때] == [SOURCING_SELECTION]
    assert 비었을_때[0].status in {"DISABLED", "SKIPPED_TEMPLATE"}


def test_안_부른_호출에는_모델을_빈_문자열로_안_적는다() -> None:
    """🔴 「안 불렀다」와 「빈 이름으로 불렀다」는 다르다 (규칙 3 의 문자열 판)."""
    from app.purchase_agent.adapter import _llm_calls

    호출 = _llm_calls(None)[0]
    assert 호출.model is None
    assert 호출.provider is None
