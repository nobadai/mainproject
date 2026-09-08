import json
import urllib.error

import pytest

from app.logistics.llm import runtime as llm_runtime
from app.logistics.llm.runtime import (
    InterpretationService,
    InterpretationValidationError,
    LLMSettings,
    ValidationIssue,
    build_template_interpretation,
    needs_llm,
    validate_interpretation,
)
from app.logistics.llm.schemas import ContextFact, SanitizedLLMContext


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, context, *, retry_guidance=None):
        del context, retry_guidance
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _service(provider):
    return InterpretationService(
        LLMSettings(
            enabled=True,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        provider,
    )


def _freshness_facts() -> list[ContextFact]:
    return [
        ContextFact(
            fact_id="freshness_risk_lot_count",
            label="신선도 임박 가용 Lot 수",
            display_value="3개",
        ),
        ContextFact(
            fact_id="freshness_min_remaining_ratio",
            label="최소 신선도 잔여 비율",
            display_value="25.0% (임계 30%)",
        ),
    ]


def _context():
    return SanitizedLLMContext(
        signals=["FRESHNESS_QUALITY_RISK"],
        facts=_freshness_facts(),
        allowed_adjustments=["우선 출고 대상으로 검토합니다."],
        # Rule 이 정한 우선 조정 — 없으면 검증기가 추천을 null 로 강제한다.
        preferred_adjustment="우선 출고 대상으로 검토합니다.",
    )


def _output(*, summary="품질 위험 검토가 필요합니다."):
    return json.dumps(
        {
            "summary": summary,
            "risks": ["FRESHNESS_QUALITY_RISK"],
            "suggested_adjustment": "우선 출고 대상으로 검토합니다.",
        },
        ensure_ascii=False,
    )


def test_logistics_runtime_uses_its_own_provider_and_validator():
    provider = FakeProvider([_output()])

    result = _service(provider).interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 1
    assert provider.calls == 1


def test_logistics_numeric_output_retries_then_falls_back():
    provider = FakeProvider([_output(summary="위험 수치가 3입니다.")] * 2)

    result = _service(provider).interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "FALLBACK"
    assert result.llm_attempts == 2
    assert result.llm_fallback_used is True


def test_logistics_provider_failure_does_not_fail_interpretation():
    provider = FakeProvider([RuntimeError("unavailable")] * 2)

    result = _service(provider).interpret(
        _context(),
        runtime_ready=True,
        has_blocking_constraints=False,
    )

    assert result.llm_status == "FALLBACK"
    assert result.interpretation.risks == ["FRESHNESS_QUALITY_RISK"]


# ---------------------------------------------------------------------------
# 호출 게이트 — LLM 정책 결정서 §2 (17-A · Qualitative · Composite 휴면)
# ---------------------------------------------------------------------------


def _gate_context(signals: list[str]) -> SanitizedLLMContext:
    # 게이트 판정은 facts 를 읽지 않는다 — signal 만으로 호출 여부를 정한다.
    return SanitizedLLMContext(
        signals=signals,
        facts=[],
        allowed_adjustments=[],
    )


@pytest.mark.parametrize(
    "signal",
    ["FRESHNESS_QUALITY_RISK", "INVENTORY_FRESHNESS_PRESSURE", "SCENARIO_ADJUSTMENT_REQUIRED"],
)
def test_qualitative_signal_triggers_alone(signal):
    assert needs_llm(_gate_context([signal]), runtime_ready=True, has_blocking_constraints=False)


def test_capacity_tight_alone_does_not_trigger():
    assert not needs_llm(
        _gate_context(["CAPACITY_TIGHT"]), runtime_ready=True, has_blocking_constraints=False
    )


def test_unresolved_only_context_does_not_trigger():
    """UNRESOLVED 는 게이트를 안 막지만(17-A), 업무 위험이 없으면 부를 이유도 없다."""
    assert not needs_llm(_gate_context([]), runtime_ready=True, has_blocking_constraints=False)


def test_fail_blocks_the_call_even_with_signals():
    assert not needs_llm(
        _gate_context(["INVENTORY_FRESHNESS_PRESSURE"]),
        runtime_ready=True,
        has_blocking_constraints=True,
    )


# ---------------------------------------------------------------------------
# 검증기 — 숫자 검사 범위 · preferred 강제 (결정서 §5)
# ---------------------------------------------------------------------------


def _validator_context(**overrides) -> SanitizedLLMContext:
    fields = {
        "signals": ["INVENTORY_FRESHNESS_PRESSURE"],
        "facts": _freshness_facts(),
        "allowed_adjustments": ["quantity", "timing"],
        "preferred_adjustment": None,
        "missing_data": [],
    }
    fields.update(overrides)
    return SanitizedLLMContext(**fields)


def _raw(summary: str, risks: list[str], suggested: str | None) -> str:
    return json.dumps(
        {"summary": summary, "risks": risks, "suggested_adjustment": suggested},
        ensure_ascii=False,
    )


def test_digit_bearing_risk_code_is_not_a_numeric_violation():
    """risks 는 signal 코드 보존 필드다 — 코드 속 숫자로 FALLBACK 이 나면 안 된다."""
    context = _validator_context(signals=["LOG-H02"], facts=[])

    interpretation = validate_interpretation(
        _raw("구역별 수용량이 확정되지 않았습니다.", ["LOG-H02"], None), context
    )

    assert interpretation.risks == ["LOG-H02"]


def test_digit_in_summary_is_still_rejected():
    context = _validator_context()

    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(
            _raw("위험 수치가 3입니다.", ["INVENTORY_FRESHNESS_PRESSURE"], None), context
        )

    assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in error.value.issues


def test_suggested_must_match_preferred():
    context = _validator_context(preferred_adjustment="quantity")

    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(
            _raw("신선도 압박이 있습니다.", ["INVENTORY_FRESHNESS_PRESSURE"], "timing"), context
        )

    assert ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION in error.value.issues


def test_no_preferred_means_no_suggestion():
    context = _validator_context(preferred_adjustment=None)

    with pytest.raises(InterpretationValidationError) as error:
        validate_interpretation(
            _raw("신선도 압박이 있습니다.", ["INVENTORY_FRESHNESS_PRESSURE"], "quantity"), context
        )

    assert ValidationIssue.PREFERRED_ADJUSTMENT_VIOLATION in error.value.issues


def test_template_follows_preferred_rule():
    """Rule 이 방향을 안 정했으면 템플릿도 추천하지 않는다 — allowed[0] 자동 추천 폐기."""
    without_preferred = build_template_interpretation(_validator_context())
    with_preferred = build_template_interpretation(
        _validator_context(preferred_adjustment="quantity")
    )

    assert without_preferred.suggested_adjustment is None
    assert with_preferred.suggested_adjustment == "quantity"


# ---------------------------------------------------------------------------
# 전송 오류 분류 — 재시도 가능만 1회, 확정 오류는 즉시 FALLBACK (결정서 §6)
# ---------------------------------------------------------------------------


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://fake", code, "err", None, None)


@pytest.mark.parametrize(
    ("code", "kind"),
    [(401, "AUTH_ERROR"), (403, "AUTH_ERROR"), (429, "QUOTA_EXCEEDED"), (400, "BAD_REQUEST")],
)
def test_terminal_http_errors_skip_retry(code, kind):
    provider = FakeProvider([_http_error(code), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 1  # 재시도 없음 — Quota 를 더 태우지 않는다
    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == kind


def test_timeout_retries_once_then_records_kind():
    provider = FakeProvider([TimeoutError(), TimeoutError()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 2
    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "TIMEOUT"


def test_retry_then_success_leaves_no_error_kind():
    """최종 상태만 기록한다 — 재시도 후 성공이면 error_kind 는 null 이다."""
    provider = FakeProvider([TimeoutError(), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert result.llm_error_kind is None


def test_validation_fallback_records_validation_failed():
    provider = FakeProvider([_output(summary="수치 3 포함"), _output(summary="수치 5 포함")])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "VALIDATION_FAILED"


def test_transport_and_validation_retries_have_independent_budgets():
    """timeout → 잘못된 출력 → 교정 출력 — 전송 재시도가 correction 기회를 먹으면 안 된다."""
    provider = FakeProvider([TimeoutError(), _output(summary="수치 3 포함"), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 3
    assert result.llm_error_kind is None


# ---------------------------------------------------------------------------
# 설정 폴백 — 모델은 provider 종속 값이다 (경계를 넘는 상속 금지)
# ---------------------------------------------------------------------------


def _pin_env(monkeypatch, **values):
    """관련 키 전부를 고정한다 — 빈 문자열은 '미설정'으로 동작한다 (or 폴백 사슬).

    load_dotenv 는 이미 있는 환경변수를 덮지 않으므로, .env 파일 값이 테스트에
    새어 들어오지 않게 모든 관련 키를 명시적으로 setenv 한다.
    """
    defaults = {
        "LLM_PROVIDER": "",
        "LLM_MODEL": "",
        "LOGISTICS_LLM_PROVIDER": "",
        "LOGISTICS_LLM_MODEL": "",
    }
    defaults.update(values)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)


def test_same_provider_still_inherits_global_model(monkeypatch):
    """물류 provider 를 명시해도 전역과 같으면 전역 모델 상속(폴백 사슬)은 유지된다."""
    from app.logistics.llm.runtime import get_llm_settings

    _pin_env(
        monkeypatch,
        LLM_PROVIDER="ollama",
        LLM_MODEL="custom-team-model",
        LOGISTICS_LLM_PROVIDER="ollama",
    )

    assert get_llm_settings().model == "custom-team-model"


def test_cross_provider_global_model_is_not_inherited(monkeypatch):
    """provider 가 다르면 전역 모델(Ollama 용)을 건너뛰고 provider 기본으로 간다."""
    from app.logistics.llm.runtime import get_llm_settings

    _pin_env(
        monkeypatch,
        LLM_PROVIDER="ollama",
        LLM_MODEL="gemma3:4b",
        LOGISTICS_LLM_PROVIDER="gemini",
    )

    settings = get_llm_settings()

    assert settings.provider == "gemini"
    assert settings.model == "gemini-3.5-flash-lite"


def test_explicit_logistics_model_always_wins(monkeypatch):
    from app.logistics.llm.runtime import get_llm_settings

    _pin_env(
        monkeypatch,
        LLM_PROVIDER="ollama",
        LLM_MODEL="gemma3:4b",
        LOGISTICS_LLM_PROVIDER="gemini",
        LOGISTICS_LLM_MODEL="gemini-custom-pin",
    )

    assert get_llm_settings().model == "gemini-custom-pin"


def _service_without_transport_retry(provider):
    return InterpretationService(
        LLMSettings(
            enabled=True,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=0,
        ),
        provider,
    )


def test_validation_correction_survives_zero_transport_retries():
    """MAX_RETRIES=0 은 전송 재시도만 끈다 — correction 1회는 정책 고정이다 (결정서 §6)."""
    provider = FakeProvider([_output(summary="수치 3 포함"), _output()])

    result = _service_without_transport_retry(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert result.llm_error_kind is None


def test_zero_transport_retries_fall_back_on_first_timeout():
    provider = FakeProvider([TimeoutError(), _output()])

    result = _service_without_transport_retry(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert provider.calls == 1
    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "TIMEOUT"


# ---------------------------------------------------------------------------
# Provider 호출 latency — 재시도 포함 합산 · 미호출은 None (#402)
#
# ★ 실 sleep 을 쓰지 않는다. 시간을 재는 테스트가 실제로 시간을 쓰면 느리고 기계 부하에
#   따라 흔들린다. production 에 Clock 추상 클래스를 새로 만들지도 않는다 — 저장소
#   어디에도 그런 것이 없고(전부 `time.perf_counter()` 직접 호출), 테스트 하나 때문에
#   생산 코드에 층을 얹는 것은 과설계다. `runtime` 이 `perf_counter` 를 **모듈 수준
#   이름**으로 들여오므로 그 이름 하나만 갈아 끼우면 된다 — 어댑터 테스트가
#   `adapter._load_read` 를 갈아 끼우는 것과 같은 seam 이다.
# ---------------------------------------------------------------------------


class _Clock:
    """호출 쌍(시작·끝)마다 정해진 간격만큼 흐르는 결정적 시계.

    🔴 **간격의 단위는 초다 — 밀리초가 아니다.** 밀리초로 만들면 `now_ms / 1000` 이
      이진 부동소수로 정확히 떨어지지 않아 production 의 `int((끝-시작)*1000)` 이
      값에 따라 1 씩 어긋난다. 실측: 간격 `[10, 20, 30]` ms 는 `[10, 19, 30]` 으로
      읽힌다. 정수 초는 이진수로 정확하므로 차이도 곱도 정확하다 — **테스트가 재는
      것은 합산 규칙이지 부동소수 반올림이 아니다.**

    ★ `perf_counter` 는 한 호출당 정확히 두 번 읽힌다(시작 · `finally` 의 끝).
      그 전제가 깨지면 간격이 조용히 어긋나므로 `reads` 를 함께 고정한다.
    """

    def __init__(self, *deltas_seconds: int):
        self.deltas = deltas_seconds
        self.now = 0.0
        self.reads = 0

    def __call__(self) -> float:
        index = self.reads
        self.reads += 1
        if index % 2 == 1:
            # 끝 읽기 — 이번 호출의 간격만큼 밀어 둔다.
            self.now += float(self.deltas[index // 2])
        return self.now


def _pin_clock(monkeypatch, *deltas_seconds: int) -> _Clock:
    clock = _Clock(*deltas_seconds)
    monkeypatch.setattr(llm_runtime, "perf_counter", clock)
    return clock


def _disabled_service(provider):
    return InterpretationService(
        LLMSettings(
            enabled=False,
            provider="fake",
            model="fake-model",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=1,
            max_retries=1,
        ),
        provider,
    )


def _no_signal_context():
    """게이트가 닫히는 Context — signal 이 없으면 부를 이유가 없다 (SKIPPED_TEMPLATE)."""
    return SanitizedLLMContext(signals=[], facts=[], allowed_adjustments=[])


def test_success_records_the_single_provider_call_latency(monkeypatch):
    clock = _pin_clock(monkeypatch, 5)
    provider = FakeProvider([_output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 1
    assert result.llm_provider_elapsed_ms == 5000
    assert clock.reads == 2, "한 호출당 시작·끝 두 번이다 — 전제가 깨지면 간격이 어긋난다"


def test_transport_retry_latency_is_summed_not_replaced(monkeypatch):
    """🔴 마지막 호출만 남기는 것이 이 이슈의 대표 반례다 (#402 M1).

    간격을 **서로 다르게** 준다 — 같은 값이면 "마지막만 기록" 변이가 살아남는다.
    """
    _pin_clock(monkeypatch, 5, 7)
    provider = FakeProvider([TimeoutError(), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert result.llm_provider_elapsed_ms == 12000
    assert result.llm_provider_elapsed_ms != 7000, "마지막 호출만 기록하면 안 된다"
    assert result.llm_provider_elapsed_ms != 5000, "첫 호출만 기록해도 안 된다"


def test_validation_correction_latency_is_summed(monkeypatch):
    _pin_clock(monkeypatch, 3, 4)
    provider = FakeProvider([_output(summary="수치 3 포함"), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2
    assert result.llm_provider_elapsed_ms == 7000


def test_transport_and_validation_retries_sum_all_three_calls(monkeypatch):
    """최악 경로 3회 — 전송 재시도와 correction 은 별도 예산이고 시간은 하나로 합친다."""
    _pin_clock(monkeypatch, 5, 7, 11)
    provider = FakeProvider([TimeoutError(), _output(summary="수치 3 포함"), _output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_attempts == 3
    assert result.llm_provider_elapsed_ms == 23000


def test_failed_provider_calls_are_counted_in_the_total(monkeypatch):
    """🔴 **예외로 끝난 호출의 시간이 합에 들어간다.**

    Provider 안에서 재면(반환 객체 설계) 이 값이 사라진다 — 그런데 timeout 이야말로
    가장 오래 걸린 호출이고, FALLBACK 진단에서 제일 알고 싶은 숫자다. 계측을
    `InterpretationService` 의 `try/finally` 에 둔 이유가 이것이다.
    """
    _pin_clock(monkeypatch, 5, 7)
    provider = FakeProvider([TimeoutError(), TimeoutError()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "FALLBACK"
    assert result.llm_error_kind == "TIMEOUT"
    assert result.llm_attempts == 2
    assert result.llm_provider_elapsed_ms == 12000


def test_disabled_records_no_provider_latency(monkeypatch):
    _pin_clock(monkeypatch, 5)
    provider = FakeProvider([_output()])

    result = _disabled_service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "DISABLED"
    assert result.llm_attempts == 0
    assert provider.calls == 0
    assert result.llm_provider_elapsed_ms is None


def test_skipped_template_records_no_provider_latency(monkeypatch):
    _pin_clock(monkeypatch, 5)
    provider = FakeProvider([_output()])

    result = _service(provider).interpret(
        _no_signal_context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert result.llm_attempts == 0
    assert provider.calls == 0
    assert result.llm_provider_elapsed_ms is None


def test_a_call_measured_at_zero_is_zero_and_not_none(monkeypatch):
    """🔴 **`0` 과 `None` 은 다른 사실이다.**

    `0` 은 *"불렀고 쟀더니 0ms"* 이고 `None` 은 *"안 불렀다"* 다. 미호출을 `0ms` 로
    적으면 실행이력에서 둘을 구별할 방법이 사라진다.
    """
    _pin_clock(monkeypatch, 0)
    provider = FakeProvider([_output()])

    result = _service(provider).interpret(
        _context(), runtime_ready=True, has_blocking_constraints=False
    )

    assert result.llm_attempts == 1
    assert result.llm_provider_elapsed_ms == 0
    assert result.llm_provider_elapsed_ms is not None


def test_latency_does_not_leak_across_calls_on_a_reused_service(monkeypatch):
    """🔴 Provider 에 `last_latency` 같은 상태를 남기면 여기서 드러난다 (#402 M5).

    같은 Service · 같은 Provider 인스턴스를 두 번 부른다. 한 호출의 정보가 인스턴스에
    남으면 두 번째 결과가 첫 번째를 상속한다 — 동시 호출·Provider 재사용에서 값이
    섞이는 바로 그 구조다. 한 호출의 시간은 그 호출의 지역 변수로만 살아야 한다.
    """
    _pin_clock(monkeypatch, 5, 7)
    provider = FakeProvider([_output(), _output()])
    service = _service(provider)

    first = service.interpret(_context(), runtime_ready=True, has_blocking_constraints=False)
    second = service.interpret(_context(), runtime_ready=True, has_blocking_constraints=False)

    assert first.llm_provider_elapsed_ms == 5000
    assert second.llm_provider_elapsed_ms == 7000, "두 번째가 첫 번째를 누적하면 안 된다"


def test_provider_keeps_no_call_state(monkeypatch):
    """계약을 실행이 아니라 **구조**로도 잠근다 — mutable Provider 상태 금지 (#402).

    ★ 계측 후 Provider 객체에 호출 흔적이 생기지 않는다. `_Provider` 가 아니라 실제
      Provider 구현체 셋을 본다 — 금지 대상은 테스트 stub 이 아니라 production 이다.
    """
    from app.logistics.llm.runtime import GeminiProvider, OllamaProvider, UnavailableProvider

    settings = _service(None).settings
    금지 = {"last_latency", "last_usage", "last_error", "last_call", "last_elapsed_ms"}
    for provider in (OllamaProvider(settings), GeminiProvider(settings), UnavailableProvider()):
        assert 금지 & set(dir(provider)) == set(), type(provider).__name__
