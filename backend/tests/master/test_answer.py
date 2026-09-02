"""⑥ 사용자 응답 생성 — 규칙이 만든 숫자 · LLM 이 쓴 문장.

★ **네트워크를 타지 않는다.** `FakeProvider` 가 문자열을 그대로 돌려준다.

이 파일이 지키는 명제는 하나다 — **LLM 이 숫자를 바꿀 경로가 없다.**
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.master.answer import (
    facts_from_procurement,
    facts_from_status,
    render_answer,
)
from app.master.llm.answer_runtime import (
    NarrativeRejected,
    NarrativeService,
    validate_narrative,
)
from app.master.llm.runtime import LLMSettings
from app.master.plan import ExecutionPlan
from app.master.status_flow import StatusOutcome

SETTINGS = LLMSettings(
    enabled=True,
    provider="fake",
    model="fake-model",
    base_url="",
    timeout_seconds=1.0,
    max_retries=1,
    max_output_tokens=512,
    effort=None,
)


class FakeProvider:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses) or ["{}"]
        self.calls: list[str] = []

    def generate(self, system: str, user: str, schema: dict) -> str:
        self.calls.append(user)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class BoomProvider:
    def generate(self, system: str, user: str, schema: dict) -> str:
        raise RuntimeError("서버가 없다")


def summary(text: str) -> str:
    return json.dumps({"summary": text}, ensure_ascii=False)


def narrator(*responses: str, settings: LLMSettings = SETTINGS) -> NarrativeService:
    return NarrativeService(settings, FakeProvider(*responses))


def status(**kw) -> StatusOutcome:
    base = {
        "status_code": "S1_ANSWERED",
        "reason": "...",
        "plan": ExecutionPlan(request_id="REQ-TEST", as_of=date(2025, 12, 31)),
        "answers": {
            "finance": {
                "as_of": "2025-12-31",
                "available_cash": 31993913.0,
                "minimum_cash_balance_krw": 12941280.0,
                "payment_pressure": "LOW",
                "policy_version_used": "v1.0",
            },
            "inventory": {
                "warehouse_free_kg": 7636.72,
                "lot_count": 4,
                "min_remaining_freshness_days": 10,
            },
        },
    }
    base.update(kw)
    return StatusOutcome(**base)


# ── 규칙이 만드는 숫자 ────────────────────────────────────────────────────


def test_금액은_원_단위로_끊고_kg_은_소수_한_자리다():
    facts = facts_from_status(status())
    values = {f.label: f.value for f in facts.facts}

    assert values["가용 현금"] == "31,993,913원"
    assert values["창고 여유"] == "7,636.7kg"
    assert values["보관 로트"] == "4건"
    assert values["최단 잔여 신선도"] == "10일"


def test_기준일과_정책버전은_사실이_아니라_꼬리말이다():
    """**답이 아니라 답의 기준이다.** 사실 줄에 섞으면 읽는 사람이 값과 헷갈린다."""
    facts = facts_from_status(status())

    labels = {f.label for f in facts.facts}
    assert "as_of" not in labels
    assert "policy_version_used" not in labels
    assert any("v1.0" in b for b in facts.basis)


def test_라벨을_모르는_키도_이름_그대로_싣는다():
    """**감추지 않는다.** 부서가 필드를 늘렸을 때 조용히 사라지면 안 된다 (§3.7.6)."""
    outcome = status(answers={"finance": {"새로_생긴_값": 1234}})
    facts = facts_from_status(outcome)

    assert [(f.label, f.value) for f in facts.facts] == [("새로_생긴_값", "1,234")]


def test_못_답한_부서를_지우지_않고_두_가지로_나눈다():
    """터진 것과 값이 없는 것은 **다시 물어볼 값어치가 다르다.**"""
    outcome = status(
        status_code="S2_PARTIAL",
        answers={"finance": {"available_cash": 1000.0}},
        unavailable=("inventory",),
        errors={"inventory": "커넥션이 끊겼다"},
    )
    facts = facts_from_status(outcome)

    assert any("다시 시도해 볼 수 있습니다" in g for g in facts.gaps)


def procurement(**kw) -> SimpleNamespace:
    """매입 응답 대역. `ProcurementRunResponse` 를 짓는 대신 **읽는 필드만** 흉내낸다."""
    base = {
        "end_code": "E1_APPROVED",
        "reason": "...",
        "scenarios": [],
        "findings": [],
        "concerns": [],
        "skipped_checks": [],
        "blocked_by": [],
        "blocked_failures": [],
        "adjustments": [],
        "missing_adapters": [],
        "verification_skipped": False,
        "single_option": False,
        "purchase_attempts": 1,
        "request_id": "REQ-1",
        "as_of": date(2026, 8, 28),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_매입_결과는_검증_분수를_반드시_싣는다():
    """`findings` 가 비었다고 "문제 없음"으로 쓰면 **못 돈 검사가 통과로 읽힌다.**"""
    facts = facts_from_procurement(
        procurement(
            scenarios=[{"label": "기본"}, {"label": "보수"}],
            skipped_checks=["L3-1 근거 없음", "L5-2 문장 없음"],
        )
    )
    values = {f.label: f.value for f in facts.facts}

    assert facts.headline.startswith("매입안을 제시합니다")
    assert values["검증"] == "지적 0건 · 판정하지 못한 검사 2건"


def test_거절도_사람이_읽는_결론으로_나온다():
    """멘토 지적 — **"사라 / 사지 마라" 가 텍스트로 나와야 한다.**"""
    facts = facts_from_procurement(
        procurement(end_code="E3_REJECTED", findings=["창고 초과"], purchase_attempts=2)
    )
    assert "매입하지 않는 것을 권합니다" in facts.headline
    assert any("창고 초과" in g for g in facts.gaps)


# ── LLM 이 쓰는 문장 ─────────────────────────────────────────────────────


def test_숫자를_쓰면_거부한다():
    """**이 검사가 이 모듈의 핵심이다.** 숫자는 규칙만 만든다."""
    with pytest.raises(NarrativeRejected) as error:
        validate_narrative(summary("가용 현금이 3199만원입니다."))
    assert "HAS_NUMBER" in error.value.issues


def test_숫자_없는_문장은_통과한다():
    assert validate_narrative(summary("재무와 물류 상태를 확인했습니다.")) == (
        "재무와 물류 상태를 확인했습니다."
    )


def test_빈_문장과_긴_문장을_막는다():
    with pytest.raises(NarrativeRejected):
        validate_narrative(summary("   "))
    with pytest.raises(NarrativeRejected) as error:
        validate_narrative(summary("가" * 201))
    assert "TOO_LONG" in error.value.issues


def test_숫자가_섞이면_교정을_실어_재시도한다():
    svc = narrator(
        summary("가용 현금이 3199만원입니다."),
        summary("재무와 물류 상태를 확인했습니다."),
    )
    result = svc.write(facts_from_status(status()))

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert "correction" in json.loads(svc.provider.calls[1])


def test_모델에_값도_항목_라벨도_보여주지_않는다():
    """**애초에 못 보면 틀리게 쓸 수도 없다.**

    🔴 라벨까지 뺀 것은 실측 때문이다 — 라벨을 주면 모델이 그것을 **부서와 헷갈려**
    물류가 답한 상황에서 *"창고 여유 정보는 확인되지 않았습니다"* 라고 썼다.
    """
    svc = narrator(summary("확인했습니다."))
    svc.write(facts_from_status(status()))

    sent = json.loads(svc.provider.calls[0])["facts"]
    assert "31,993,913" not in sent
    assert "가용 현금" not in sent
    assert "답한 부서: 재무, 물류" in sent  # 부서 이름만 준다
    assert "답하지 못한 부서: 없음" in sent


def test_값을_못_본_모델이_상태를_평가하면_거부한다():
    """🔴 **실측에서 나온 결함이다.**

    현금 압박이 `LOW` 인 날에 모델이 *"현금 상황이 다소 어려운 편입니다"* 라고 썼다.
    값을 안 보여준 것이 원인인데, 보여주면 이번엔 숫자를 옮겨 적는다 —
    **평가할 일 자체를 빼는 것**이 둘 다 피하는 유일한 길이다.
    """
    with pytest.raises(NarrativeRejected) as error:
        validate_narrative(summary("현재 현금 상황이 다소 어려운 편입니다."))
    assert "EVALUATED" in error.value.issues


def test_다_답했는데_못_봤다고_쓰면_거부한다():
    """🔴 이것도 실측이다 — 물류가 답했는데 *"확인되지 않았습니다"* 라고 썼다."""
    facts = facts_from_status(status())
    assert not facts.gaps

    with pytest.raises(NarrativeRejected) as error:
        validate_narrative(summary("일부 정보는 확인되지 않았습니다."), facts)
    assert "INVENTED_GAP" in error.value.issues


def test_실제로_못_본_것이_있으면_그렇게_써도_된다():
    """같은 문장이 상황에 따라 참이다. **`gaps` 가 있을 때만 허용한다.**"""
    facts = facts_from_status(
        status(
            status_code="S2_PARTIAL",
            answers={"inventory": {"warehouse_free_kg": 1.0}},
            unavailable=("finance",),
            missing_data={"finance": ("finance_state",)},
        )
    )
    assert validate_narrative(summary("재무는 확인하지 못했습니다."), facts)


# ── 실패해도 답이 나간다 ──────────────────────────────────────────────────


def test_서버가_죽어도_답은_완결된다():
    """★ ①과 다른 점 — **⑥은 실패해도 답할 수 있다.**"""
    result = NarrativeService(SETTINGS, BoomProvider()).write(facts_from_status(status()))
    text = render_answer(facts_from_status(status()), result.narrative)

    assert result.llm_status == "FALLBACK"
    assert result.narrative is None
    assert "31,993,913원" in text
    assert "7,636.7kg" in text


def test_꺼_두면_부르지_않고_사실만_낸다():
    settings = LLMSettings(**{**SETTINGS.__dict__, "enabled": False})
    svc = narrator(summary("..."), settings=settings)
    result = svc.write(facts_from_status(status()))

    assert result.llm_status == "DISABLED"
    assert svc.provider.calls == []


def test_끝까지_숫자를_쓰면_문장을_버리고_사실만_낸다():
    result = narrator(summary("현금 3199만원")).write(facts_from_status(status()))

    assert result.llm_status == "FALLBACK"
    assert result.narrative is None


def test_문장은_맨_앞에_얹히고_숫자는_아래_줄에만_있다():
    facts = facts_from_status(status())
    text = render_answer(facts, "재무와 물류 상태를 확인했습니다.")

    assert text.startswith("재무와 물류 상태를 확인했습니다.")
    first_paragraph = text.split("\n\n")[0]
    assert not any(ch.isdigit() for ch in first_paragraph)


def test_묻지도_않은_부서를_문장에_넣으면_거부한다():
    """🔴 실측 — 물류 하나만 물었는데 *"재무 및 물류는 확인되지 않았습니다"* 라고 썼다.

    부서 이름은 닫힌 목록이라 **프롬프트에 없던 이름이 나오면 지어낸 것이 확실하다.**
    대조 대상이 `to_prompt()` 인 것이 요점이다 — 모델이 볼 수 있었던 것과 맞춘다.
    """
    facts = facts_from_status(
        status(
            status_code="S3_UNAVAILABLE",
            answers={},
            unavailable=("inventory",),
            missing_data={"inventory": ("ADAPTER_NOT_REGISTERED",)},
        )
    )
    with pytest.raises(NarrativeRejected) as error:
        validate_narrative(summary("재무 및 물류 부서는 확인되지 않았습니다."), facts)
    assert "INVENTED_AGENT" in error.value.issues

    assert validate_narrative(summary("물류 부서는 확인되지 않았습니다."), facts)


def test_매입은_결론에_있으면_문장에_써도_된다():
    """`매입` 은 부서 이름이자 결론의 단어다. **본 것 안이면 허용한다.**"""
    facts = facts_from_procurement(procurement(scenarios=[{"label": "기본"}]))
    assert validate_narrative(summary("매입안을 준비했으니 확인해 주세요."), facts)
