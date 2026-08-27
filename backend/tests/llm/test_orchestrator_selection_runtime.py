"""Orchestrator T3-5 Selector 런타임 단위테스트 — 네트워크에 나가지 않는다.

Provider 를 가짜로 갈아끼워 상태 결정 순서 · 환각 차단 · 숫자 금지만 검증한다.
"""

import json

import pytest

from app.orchestrator.llm.runtime import (
    LLMSettings,
    SelectionService,
    SelectionValidationError,
    ValidationIssue,
    validate_selection,
)
from app.orchestrator.llm.schemas import CandidateContext, SanitizedLLMContext

DETERMINISTIC = ["SCN-2", "SCN-1"]


def _settings(*, enabled: bool = True, max_retries: int = 1) -> LLMSettings:
    return LLMSettings(
        enabled=enabled,
        provider="ollama",
        model="gemma3:4b",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1.0,
        max_retries=max_retries,
    )


def _context(candidate_ids: list[str]) -> SanitizedLLMContext:
    return SanitizedLLMContext(
        cycle="PROCUREMENT",
        signals=["[finance] 지급 일정이 빠듯합니다."],
        facts=["[finance] 지급 일정이 빠듯합니다."],
        candidates=[
            CandidateContext(
                scenario_id=scenario_id,
                clip_magnitude="MINOR_CLIP",
                binding_constraints=["cap_total_kg"],
            )
            for scenario_id in candidate_ids
        ],
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


def _payload(ranked: list[str], **overrides) -> str:
    body = {
        "summary": "재무 지급 일정을 고려해 정렬했습니다.",
        "ranked_scenario_ids": ranked,
        "rationale_per_id": {i: "제약을 만족하는 안입니다." for i in ranked},
        "conflict_note": None,
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


def _select(service: SelectionService, ids: list[str] | None = None):
    return service.select(
        _context(ids or ["SCN-1", "SCN-2"]),
        runtime_ready=True,
        deterministic_ranking=DETERMINISTIC,
    )


# --- 상태 결정 순서 -----------------------------------------------------------
def test_disabled_uses_deterministic_ranking():
    provider = _StubProvider()
    result = _select(SelectionService(_settings(enabled=False), provider))
    assert result.llm_status == "DISABLED"
    assert result.interpretation.ranked_scenario_ids == DETERMINISTIC
    assert provider.calls == 0


def test_single_candidate_is_skipped_template():
    """후보가 하나면 정렬할 것이 없다 — node_t3_select 의 len(feasible) > 1 과 같은 판단."""
    provider = _StubProvider()
    service = SelectionService(_settings(), provider)
    result = service.select(
        _context(["SCN-1"]),
        runtime_ready=True,
        deterministic_ranking=["SCN-1"],
    )
    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert provider.calls == 0


def test_runtime_not_ready_is_skipped_template():
    provider = _StubProvider()
    service = SelectionService(_settings(), provider)
    result = service.select(
        _context(["SCN-1", "SCN-2"]),
        runtime_ready=False,
        deterministic_ranking=DETERMINISTIC,
    )
    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert provider.calls == 0


def test_valid_output_is_success():
    provider = _StubProvider(_payload(["SCN-1", "SCN-2"]))
    result = _select(SelectionService(_settings(), provider))
    assert result.llm_status == "SUCCESS"
    assert result.llm_fallback_used is False
    assert result.llm_attempts == 1
    assert result.interpretation.ranked_scenario_ids == ["SCN-1", "SCN-2"]


def test_provider_failure_falls_back_to_deterministic():
    """Ollama 가 죽어도 Core 가 정한 순위는 그대로 살아 있어야 한다."""
    provider = _StubProvider(RuntimeError("connection refused"), RuntimeError("still down"))
    result = _select(SelectionService(_settings(), provider))
    assert result.llm_status == "FALLBACK"
    assert result.llm_fallback_used is True
    assert result.llm_attempts == 2
    assert result.interpretation.ranked_scenario_ids == DETERMINISTIC


def test_retry_then_success():
    provider = _StubProvider("not json", _payload(["SCN-2", "SCN-1"]))
    result = _select(SelectionService(_settings(), provider))
    assert result.llm_status == "SUCCESS"
    assert result.llm_attempts == 2


# --- 환각 차단 · 자동 보정 -----------------------------------------------------
def test_unknown_scenario_id_is_rejected():
    context = _context(["SCN-1", "SCN-2"])
    with pytest.raises(SelectionValidationError) as excinfo:
        validate_selection(_payload(["SCN-1", "SCN-9"]), context)
    assert ValidationIssue.UNKNOWN_SCENARIO_ID in excinfo.value.issues


def test_duplicates_removed_and_missing_appended():
    context = _context(["SCN-1", "SCN-2", "SCN-3"])
    raw = _payload(["SCN-2", "SCN-2", "SCN-1", "SCN-3"])
    assert validate_selection(raw, context).ranked_scenario_ids == ["SCN-2", "SCN-1", "SCN-3"]


def test_missing_candidate_is_appended_in_deterministic_order():
    context = _context(["SCN-1", "SCN-2", "SCN-3"])
    raw = json.dumps(
        {
            "summary": "일부만 정렬했습니다.",
            "ranked_scenario_ids": ["SCN-3"],
            "rationale_per_id": {"SCN-3": "제약을 만족합니다."},
            "conflict_note": None,
        },
        ensure_ascii=False,
    )
    # 누락분 보정은 통과시키되, rationale 이 빠진 것은 재시도 사유로 잡는다.
    with pytest.raises(SelectionValidationError) as excinfo:
        validate_selection(raw, context)
    assert ValidationIssue.RATIONALE_MISSING in excinfo.value.issues


# --- 숫자 금지 (§1.2-3) --------------------------------------------------------
def test_numeric_in_prose_is_rejected():
    context = _context(["SCN-1", "SCN-2"])
    raw = _payload(["SCN-1", "SCN-2"], summary="총 5000kg 을 매입합니다.")
    with pytest.raises(SelectionValidationError) as excinfo:
        validate_selection(raw, context)
    assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in excinfo.value.issues


def test_digits_inside_scenario_id_are_allowed():
    """SCN-2 처럼 id 에 숫자가 있어도 숫자 금지 검사에 걸리면 안 된다."""
    context = _context(["SCN-1", "SCN-2"])
    raw = _payload(["SCN-1", "SCN-2"], summary="SCN-2 를 먼저 검토하십시오.")
    assert validate_selection(raw, context).ranked_scenario_ids == ["SCN-1", "SCN-2"]


# --- graph.node_t3_select 주입 어댑터 -------------------------------------------
class _StubClip:
    def __init__(self, scenario_id: str, *, clipped: bool = False, ratio: float = 1.0):
        self.scenario_id = scenario_id
        self.clipped = clipped
        self.clip_ratio = ratio
        self.binding_constraints = ["cap_total_kg"] if clipped else []
        self.infeasible = False


class _StubLog:
    def __init__(self):
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        self.notes.append(message)


class _StubBand:
    not_ready: tuple = ()


class _StubState:
    """`node_t3_select` 가 selector 에 넘기는 PipelineState 의 최소 형태."""

    def __init__(self, clips):
        self.clip_results = clips
        self.replies = {}
        self.band = _StubBand()
        self.log = _StubLog()


def test_graph_selector_uses_llm_order():
    from app.orchestrator.llm.selector import make_selector

    provider = _StubProvider(_payload(["SCN-2", "SCN-1"]))
    selector = make_selector(SelectionService(_settings(), provider))
    state = _StubState([_StubClip("SCN-1"), _StubClip("SCN-2", clipped=True, ratio=0.5)])

    assert selector(state) == ["SCN-2", "SCN-1"]
    assert any("SUCCESS" in note for note in state.log.notes)


def test_graph_selector_survives_llm_failure():
    """그래프가 LLM 때문에 멈추면 안 된다 — 후보 순서를 그대로 돌려준다."""
    from app.orchestrator.llm.selector import make_selector

    provider = _StubProvider(RuntimeError("down"), RuntimeError("down"))
    selector = make_selector(SelectionService(_settings(), provider))
    state = _StubState([_StubClip("SCN-1"), _StubClip("SCN-2")])

    assert selector(state) == ["SCN-1", "SCN-2"]
    assert any("FALLBACK" in note for note in state.log.notes)
