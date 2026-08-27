"""의도 분류 — 닫힌 열거 · 검증 체인 · fallback.

★ **네트워크를 타지 않는다.** `FakeProvider` 가 문자열을 그대로 돌려준다.
"""

from __future__ import annotations

import json

import pytest

from app.master.llm.runtime import (
    IntentIssue,
    IntentService,
    IntentValidationError,
    LLMSettings,
    validate_intent,
)
from app.master.llm.schemas import Intent

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
    """정해 둔 응답을 순서대로 돌려준다. 다 쓰면 마지막 것을 반복한다."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses) or ["{}"]
        self.calls: list[str] = []

    def generate(self, system: str, user: str, schema: dict) -> str:
        self.calls.append(user)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class BoomProvider:
    def generate(self, system: str, user: str, schema: dict) -> str:
        raise RuntimeError("키가 없다")


def payload(**kw) -> str:
    base = {"action": "UNKNOWN", "agents": [], "confidence": "LOW"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def service(*responses: str, settings: LLMSettings = SETTINGS) -> IntentService:
    return IntentService(settings, FakeProvider(*responses))


# ── 정상 ────────────────────────────────────────────────────────────────


def test_상태_조회는_확인_없이_바로_간다():
    result = service(
        payload(action="STATUS_QUERY", agents=["finance"], confidence="HIGH")
    ).classify("지금 자금 상황 알려줘")

    assert result.llm_status == "SUCCESS"
    assert result.intent.action == "STATUS_QUERY"
    assert result.intent.agents == ["finance"]
    assert result.needs_confirmation is False


def test_매입_실행은_확실해도_확인을_받는다():
    """오분류 비용이 비대칭이다 — 예산 12회와 매입 LLM 을 태운다."""
    result = service(payload(action="PROCUREMENT_RUN", item="배추", confidence="HIGH")).classify(
        "오늘 배추 얼마나 사야 해?"
    )

    assert result.intent.action == "PROCUREMENT_RUN"
    assert result.needs_confirmation is True
    assert "배추" in result.clarification


def test_확신이_낮으면_상태_조회도_확인을_받는다():
    result = service(
        payload(action="STATUS_QUERY", agents=["inventory"], confidence="MEDIUM")
    ).classify("창고 어때?")

    assert result.needs_confirmation is True


# ── 검증 ────────────────────────────────────────────────────────────────


def test_없는_에이전트는_스키마에서_막힌다():
    with pytest.raises(IntentValidationError) as error:
        validate_intent(
            payload(action="STATUS_QUERY", agents=["marketing"], confidence="HIGH"), "..."
        )
    assert error.value.issues == [IntentIssue.SCHEMA]


def test_없는_품목은_스키마에서_막힌다():
    with pytest.raises(IntentValidationError):
        validate_intent(payload(action="PROCUREMENT_RUN", item="딸기", confidence="HIGH"), "..")


def test_없는_action_은_스키마에서_막힌다():
    with pytest.raises(IntentValidationError):
        validate_intent(payload(action="DELETE_EVERYTHING", confidence="HIGH"), "...")


def test_조회가_아닌데_에이전트를_실으면_막는다():
    with pytest.raises(IntentValidationError) as error:
        validate_intent(
            payload(action="PROCUREMENT_RUN", agents=["finance"], confidence="HIGH"), "..."
        )
    assert IntentIssue.AGENTS_ON_NON_QUERY in error.value.issues


def test_조회인데_에이전트가_비면_막는다():
    with pytest.raises(IntentValidationError) as error:
        validate_intent(payload(action="STATUS_QUERY", confidence="HIGH"), "...")
    assert IntentIssue.AGENTS_MISSING in error.value.issues


def test_발화문에_없는_숫자를_조건에_넣으면_막는다():
    """**이 검사가 이 모듈의 핵심이다.** 사용자가 말한 숫자는 옮기되 지어내지 못하게 한다."""
    with pytest.raises(IntentValidationError) as error:
        validate_intent(
            payload(
                action="RERUN_WITH_CONDITION",
                condition="예산을 2000만원으로 낮춰서",
                confidence="HIGH",
            ),
            "예산 좀 줄여서 다시 해줘",
        )
    assert IntentIssue.CONDITION_INVENTED_NUMBER in error.value.issues


def test_사용자가_말한_숫자는_그대로_옮길_수_있다():
    intent = validate_intent(
        payload(
            action="RERUN_WITH_CONDITION",
            condition="예산 2000만원으로",
            confidence="HIGH",
        ),
        "예산 2000만원으로 다시 해줘",
    )
    assert intent.condition == "예산 2000만원으로"


def test_UNKNOWN_인데_다른_필드가_차_있으면_막는다():
    with pytest.raises(IntentValidationError) as error:
        validate_intent(payload(action="UNKNOWN", item="배추", confidence="LOW"), "...")
    assert IntentIssue.UNKNOWN_NOT_EMPTY in error.value.issues


def test_선택인데_안_이름이_없으면_막는다():
    with pytest.raises(IntentValidationError) as error:
        validate_intent(payload(action="SELECT_SCENARIO", confidence="HIGH"), "...")
    assert IntentIssue.LABEL_MISSING in error.value.issues


def test_제시되지_않은_라벨은_여기서_막지_않는다():
    """라벨 대조는 `decision_service` 가 **그 실행의 응답과** 한다 — 더 강한 검사다."""
    intent = validate_intent(
        payload(action="SELECT_SCENARIO", scenario_label="초공격", confidence="HIGH"),
        "초공격안으로 해줘",
    )
    assert intent.scenario_label == "초공격"


# ── 재시도 · fallback ────────────────────────────────────────────────────


def test_첫_출력이_틀리면_교정을_실어_재시도한다():
    svc = service(
        payload(action="STATUS_QUERY", confidence="HIGH"),  # agents 누락
        payload(action="STATUS_QUERY", agents=["finance"], confidence="HIGH"),
    )
    result = svc.classify("자금 알려줘")

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    second = json.loads(svc.provider.calls[1])
    assert "correction" in second


def test_끝까지_틀리면_UNKNOWN_으로_되묻는다():
    result = service(payload(action="STATUS_QUERY", confidence="HIGH")).classify("자금")

    assert result.llm_status == "FALLBACK"
    assert result.intent.action == "UNKNOWN"
    assert result.needs_confirmation is True
    assert result.clarification is not None


def test_키가_없어도_죽지_않고_되묻는다():
    """브랜치만 받은 팀원 환경에서 API 가 깨지지 않아야 한다."""
    result = IntentService(SETTINGS, BoomProvider()).classify("오늘 배추 사야 해?")

    assert result.llm_status == "FALLBACK"
    assert result.llm_fallback_used is True
    assert result.intent.action == "UNKNOWN"


def test_꺼_두면_호출하지_않는다():
    settings = LLMSettings(**{**SETTINGS.__dict__, "enabled": False})
    svc = service(payload(action="STATUS_QUERY", confidence="HIGH"), settings=settings)
    result = svc.classify("자금 알려줘")

    assert result.llm_status == "DISABLED"
    assert result.llm_attempts == 0
    assert svc.provider.calls == []


def test_빈_발화문은_부르지_않는다():
    svc = service(payload(action="STATUS_QUERY", confidence="HIGH"))
    result = svc.classify("   ")

    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert svc.provider.calls == []


# ── 스키마 ──────────────────────────────────────────────────────────────


def test_출력_스키마에_수량_금액_칸이_없다():
    """**안전장치의 전부다.** 만들 자리를 없앤다 (오케 selector · 매입 ⑤ 선례)."""
    fields = set(Intent.model_fields)
    assert fields == {"action", "agents", "item", "scenario_label", "condition", "confidence"}
    assert not {"qty_kg", "amount_krw", "budget", "payload"} & fields
