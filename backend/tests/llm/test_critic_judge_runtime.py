"""Critic L5 Judge 런타임 단위테스트 — 네트워크에 나가지 않는다.

★ 핵심 불변식: judge 가 무엇을 하든 **결정을 죽이지 못한다**.
  FAIL 은 CONCERN 까지만 올라가고, 판정하지 못했으면 PASS 로 두고 skipped 에 드러낸다.
"""

import json

import pytest

from app.critic.llm.judge import JudgeRunner, build_judge_context
from app.critic.llm.runtime import (
    JudgeService,
    JudgeValidationError,
    LLMSettings,
    ValidationIssue,
    validate_judgement,
)
from app.critic.llm.schemas import SanitizedLLMContext

RATIONALE = "재고 상한에 걸려 물량을 줄였습니다."


def _settings(*, enabled: bool = True, max_retries: int = 1) -> LLMSettings:
    return LLMSettings(
        enabled=enabled,
        provider="ollama",
        model="gemma3:4b",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1.0,
        max_retries=max_retries,
    )


def _context(rationale: str = RATIONALE) -> SanitizedLLMContext:
    return SanitizedLLMContext(
        cycle="A",
        signals=["cap_total_kg"],
        facts=["[inventory] 창고 여유가 부족합니다."],
        binding_constraints=["cap_total_kg"],
        rationale=rationale,
    )


class _StubProvider:
    def __init__(self, *outputs: str | Exception):
        self.outputs = list(outputs)
        self.calls = 0

    def generate(self, context, *, retry_guidance=None):
        del context, retry_guidance
        self.calls += 1
        value = self.outputs.pop(0) if self.outputs else ""
        if isinstance(value, Exception):
            raise value
        return value


def _payload(verdict: str = "PASS", **overrides) -> str:
    body = {
        "summary": "설명문과 제약이 일치합니다.",
        "verdict": verdict,
        "note": "재고 상한 언급이 제약과 같은 방향입니다.",
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


def _judge(service: JudgeService, context: SanitizedLLMContext | None = None):
    return service.judge(
        context or _context(),
        runtime_ready=True,
        end_stage_reached=False,
    )


# --- 상태 결정 순서 -----------------------------------------------------------
def test_disabled_skips_call():
    provider = _StubProvider()
    result = _judge(JudgeService(_settings(enabled=False), provider))
    assert result.llm_status == "DISABLED"
    assert provider.calls == 0


def test_empty_rationale_is_skipped_template():
    """검사할 설명문이 없으면 부르지 않는다."""
    provider = _StubProvider()
    result = _judge(JudgeService(_settings(), provider), _context(rationale="   "))
    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert provider.calls == 0


def test_end_stage_reached_is_skipped_template():
    """앞 레이어가 FAIL 로 끊겼으면 L5 는 돌지 않는다 (설계서 §8)."""
    provider = _StubProvider()
    service = JudgeService(_settings(), provider)
    result = service.judge(_context(), runtime_ready=True, end_stage_reached=True)
    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert provider.calls == 0


def test_valid_output_is_success():
    provider = _StubProvider(_payload("PASS"))
    result = _judge(JudgeService(_settings(), provider))
    assert result.llm_status == "SUCCESS"
    assert result.interpretation.verdict == "PASS"
    assert result.llm_attempts == 1


def test_provider_failure_falls_back_to_pass():
    """★ Ollama 가 죽어도 결정론 검증 결과를 뒤집지 않는다 — 기본값은 항상 PASS."""
    provider = _StubProvider(RuntimeError("connection refused"), RuntimeError("still down"))
    result = _judge(JudgeService(_settings(), provider))
    assert result.llm_status == "FALLBACK"
    assert result.llm_fallback_used is True
    assert result.interpretation.verdict == "PASS"


# --- 출력 검증 ----------------------------------------------------------------
def test_numeric_output_is_rejected():
    with pytest.raises(JudgeValidationError) as excinfo:
        validate_judgement(_payload("PASS", note="상한 12000kg 을 넘습니다."))
    assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in excinfo.value.issues


def test_fail_without_note_is_rejected():
    with pytest.raises(JudgeValidationError) as excinfo:
        validate_judgement(_payload("FAIL", note=""))
    assert ValidationIssue.NOTE_REQUIRED_ON_FAIL in excinfo.value.issues


def test_invalid_schema_is_rejected():
    with pytest.raises(JudgeValidationError) as excinfo:
        validate_judgement('{"verdict": "MAYBE"}')
    assert ValidationIssue.INVALID_SCHEMA in excinfo.value.issues


# --- RationaleJudge 어댑터 -----------------------------------------------------
def _runner_payload() -> dict:
    return {
        "decision_kg": {"배추": 5000.0},
        "binding_constraints": ["cap_total_kg"],
        "dept_reasons": {"inventory": "창고 여유가 부족합니다."},
        "rationale": RATIONALE,
    }


def test_context_drops_quantities():
    """payload 에 수량이 있어도 Context 에는 싣지 않는다."""
    context = build_judge_context(_runner_payload(), cycle="A")
    assert "5000" not in context.model_dump_json()
    assert context.rationale == RATIONALE
    assert context.binding_constraints == ["cap_total_kg"]


def test_runner_reports_fail_as_not_ok():
    runner = JudgeRunner(JudgeService(_settings(), _StubProvider(_payload("FAIL"))))
    ok, note = runner(_runner_payload())
    assert ok is False  # 러너가 CONCERN 으로 올린다 — FAIL 로 결정을 죽이지 않는다
    assert note
    assert runner.ran is True


def test_runner_passes_when_llm_unavailable():
    """★ 판정하지 못한 것을 FAIL 로 적으면 검증하지 않은 것을 검증했다고 말하는 셈이다."""
    runner = JudgeRunner(JudgeService(_settings(), _StubProvider(RuntimeError("down"))))
    ok, _ = runner(_runner_payload())
    assert ok is True
    assert runner.ran is False
    assert runner.result is not None
    assert runner.result.llm_status == "FALLBACK"
