"""E3-2 검사 — ⑤ 등급 조합 LLM 판단 (백로그 E3-2 · 상세설계 §4-⑤ E3-2 확정 블록).

백로그 E3-2 DoD: **"장기 보관 계획+중품 과다 조합 회피 확인"**.

⚠️ **그 DoD는 LLM을 믿어서 지켜지지 않는다.** 규칙이 후보를 `cap_ratio` 이하로만 만들기
때문에 LLM이 무엇을 골라도 중품 과다가 구조적으로 불가능하다 — 아래 전수 테스트가
"어느 후보를 강제로 고르게 해도 사중 일치와 상한이 유지되는가"를 확인하는 이유다.

⚠️ **실 API를 타지 않는다.** 프로바이더 자리에 가짜를 꽂아 결정적으로 돌린다 —
877건이 키·서버·환경변수 없이 그대로 돌아야 한다는 게 요건이다. 실 프로바이더 경로는
`@pytest.mark.llm`으로 분리하고 `addopts = -m 'not llm'`이 기본 실행에서 뺀다.
"""

import json
import os
from datetime import date

import pytest

from app.purchase_agent.config import load_constraints
from app.purchase_agent.graph import run_purchase_agent
from app.purchase_agent.llm.mix import MixDecision, build_mix_context, make_mix_selector
from app.purchase_agent.llm.runtime import (
    _PROVIDERS,
    LLMSettings,
    MixSelectionService,
    MixValidationError,
    UnavailableProvider,
    ValidationIssue,
    needs_llm,
    validate_interpretation,
)
from app.purchase_agent.llm.schemas import MixCandidate, SanitizedLLMContext
from app.purchase_agent.nodes.allocate_sourcing import (
    allocate_sourcing,
    build_mix_candidates,
    evaluate_mid_grade,
)
from app.purchase_agent.nodes.classify_situation import classify_situation
from app.purchase_agent.nodes.draft_plan import draft_plan
from app.purchase_agent.nodes.package_scenarios import package_scenarios
from app.purchase_agent.nodes.self_check import self_check
from app.purchase_agent.nodes.split_plan import split_plan
from app.purchase_agent.state import build_initial_state

RISING = date(2026, 8, 21)
FALLING = date(2026, 8, 28)
UNCERTAIN = date(2026, 9, 4)
SPREAD_WIDE = date(2026, 9, 11)
ANCHORS = (RISING, FALLING, UNCERTAIN, SPREAD_WIDE)
ITEMS = ("배추", "무", "피마늘", "양파")

ITEM = "배추"


def _staged(item: str = ITEM, as_of: date = SPREAD_WIDE) -> dict:
    """④까지 돌린 상태 — ⑤는 ③의 안별 총량을 보므로 앞 노드가 선행해야 한다."""
    state = build_initial_state(item, as_of)
    state.update(classify_situation(state))
    state.update(draft_plan(state))
    state.update(split_plan(state))
    return state


def _context(candidate_ids: tuple[str, ...] = ("BASE_ONLY", "MID_CAPPED")) -> SanitizedLLMContext:
    return build_mix_context(
        ITEM,
        spread_widened=True,
        shelf_days=6.0,
        shelf_tight=True,
        signals=["GRADE_SPREAD_WIDENED"],
        facts=["등급 스프레드가 평시보다 확대됐다."],
        candidates=[MixCandidate(candidate_id=cid, summary=cid) for cid in candidate_ids],
    )


class FakeProvider:
    """응답을 미리 정해두는 프로바이더. 예외를 넣으면 그 자리에서 던진다 (팀 관례)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.retry_guidance = []

    def generate(self, context, *, retry_guidance=None):
        del context
        self.calls += 1
        self.retry_guidance.append(retry_guidance)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _settings(*, enabled=True, retries=1, provider="fake") -> LLMSettings:
    return LLMSettings(
        enabled=enabled,
        provider=provider,
        model="fake-model",
        base_url="http://127.0.0.1:11434",
        timeout_seconds=1,
        max_retries=retries,
        max_output_tokens=1024,
        effort=None,
        reason_max_chars=300,
    )


def _reply(candidate_id: str, reason: str = "확대된 스프레드가 신선도 리스크를 넘는다") -> str:
    return json.dumps({"chosen_candidate_id": candidate_id, "reason": reason})


def _fixed_selector(candidate_id: str):
    """지정한 후보를 항상 고르는 선택자 — 그래프 전체를 결정적으로 돌릴 때 쓴다."""

    def selector(context: SanitizedLLMContext, default_candidate_id: str) -> MixDecision:
        del default_candidate_id
        assert any(c.candidate_id == candidate_id for c in context.candidates)
        return MixDecision(
            candidate_id=candidate_id,
            reason="테스트 고정 선택",
            llm_status="SUCCESS",
            llm_model="fake-model",
            llm_fallback_used=False,
        )

    return selector


# ── 877건이 키·서버 없이 도는가 ─────────────────────────────────────────────


def test_default_path_needs_no_key_or_server() -> None:
    """키도 서버도 없이 돌아가고, **산출물이 규칙 경로와 같다**.

    이게 이 에픽의 안전망이다. ``selector=None``은 "LLM 없음"이 아니라 "기본 선택자를
    만든다"이고, 그 선택자가 꺼져 있거나(설정) 실패하면(키 없음) 규칙 기본안을 돌려준다.
    conftest가 세션 전체에서 LLM을 꺼두므로 여기서는 ``DISABLED`` 경로를 밟는다.

    **고지는 붙는다** — 중품을 태운 날에 한해서. 판단이 없었다는 사실은 숨기지 않는다
    (E3-3의 일괄 fallback, E3-4의 충분성 미판정과 같은 자리).
    """
    for as_of in ANCHORS:
        proposal = run_purchase_agent(ITEM, as_of, selector=None)
        for scenario in proposal["scenarios"]:
            total = scenario["total_qty_kg"]
            assert total == sum(line["qty_kg"] for line in scenario["sourcing_plan"])
            assert scenario["total_amount_krw"] == sum(
                line["qty_kg"] * line["grade_unit_price"]
                for line in scenario["sourcing_plan"]
            )
            notes = [risk for risk in scenario["risks"] if "등급 조합 판단" in risk]
            mid_used = any(line["grade"] == "중" for line in scenario["sourcing_plan"])
            if mid_used:
                assert len(notes) == 1, "중품을 태운 날엔 판단 유무가 보여야 한다"
                assert "판단자 미사용" in notes[0]
            else:
                assert notes == [], "중품을 안 태운 날엔 없는 판단을 언급하지 않는다"


def test_settings_never_carry_the_api_key() -> None:
    """설정 객체에 키가 실리면 로그·예외에 묻어나간다 — 프로바이더가 호출 직전에 읽는다."""
    settings = _settings()
    dumped = repr(settings)
    assert "api_key" not in dumped
    assert "key" not in dumped.lower()
    assert not hasattr(settings, "api_key")


def test_env_file_is_gitignored() -> None:
    """키는 ``.env``에서만 읽는다 — 그 파일이 추적되면 키가 커밋된다."""
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", ".env", "backend/.env"],
        cwd="..",
        capture_output=True,
        text=True,
        check=False,
    )
    assert ".env" in result.stdout


# ── 게이팅 (비용) ───────────────────────────────────────────────────────────


def test_llm_is_not_called_when_the_rule_declines_mid_grade() -> None:
    """**규칙이 중품을 안 태우는 날엔 호출 0회.**

    실측: cap_ratio는 스프레드와 무관하게 근접 납품량으로 계산되므로 평시에도 후보가
    3개 나온다. "후보 ≥ 2"만으로 게이팅하면 4앵커 × 4품목 = 16회 전부 호출된다.
    규칙의 AND 게이트(확대 ∧ 스코어>0)를 함께 봐야 백로그의 비용 완화책이 성립한다.
    """
    calls = []

    def counting_selector(context, default_candidate_id):
        calls.append(context.item)
        return MixDecision(
            candidate_id=default_candidate_id,
            reason="x",
            llm_status="SUCCESS",
            llm_model="fake",
            llm_fallback_used=False,
        )

    for as_of in (RISING, FALLING, UNCERTAIN):
        for item in ITEMS:
            state = _staged(item, as_of)
            allocate_sourcing(state, selector=counting_selector)
    assert calls == [], "평시·하락·불확실엔 중품을 안 태우므로 물어볼 게 없다"

    for item in ITEMS:
        state = _staged(item, SPREAD_WIDE)
        allocate_sourcing(state, selector=counting_selector)
    assert len(calls) == 4, "확대일 4품목에서만 호출된다"


def test_service_skips_when_a_single_candidate() -> None:
    """후보가 하나면 고를 게 없다 — 서비스 층에서도 같은 게이팅이 걸린다."""
    context = _context(("BASE_ONLY",))
    assert needs_llm(context) is False
    provider = FakeProvider([])
    service = MixSelectionService(_settings(), provider)
    result = service.select(context, default_candidate_id="BASE_ONLY")
    assert result.llm_status == "SKIPPED_TEMPLATE"
    assert provider.calls == 0


def test_disabled_setting_skips_the_call() -> None:
    provider = FakeProvider([])
    service = MixSelectionService(_settings(enabled=False), provider)
    result = service.select(_context(), default_candidate_id="MID_CAPPED")
    assert result.llm_status == "DISABLED"
    assert provider.calls == 0


# ── 검증 우회 경로 (프로바이더 밖 공통 층) ─────────────────────────────────


def test_candidate_outside_the_offered_set_is_rejected() -> None:
    """후보 밖 id는 **비율을 지어낸 것과 같다** — 노드가 그 id로 비율을 못 찾는다."""
    with pytest.raises(MixValidationError) as excinfo:
        validate_interpretation(_reply("MID_DOUBLE"), _context(), reason_max_chars=300)
    assert ValidationIssue.UNKNOWN_CANDIDATE in excinfo.value.issues


def test_numbers_in_the_reason_are_rejected() -> None:
    """정의서 §1.2-3을 프롬프트가 아니라 **검증기**로 강제한다 (팀 4벌 공통 규칙)."""
    with pytest.raises(MixValidationError) as excinfo:
        validate_interpretation(
            _reply("MID_CAPPED", "중품이 130원 싸다"), _context(), reason_max_chars=300
        )
    assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in excinfo.value.issues


def test_json_shaped_garbage_is_rejected() -> None:
    """JSON으로 위장한 응답 — 파싱은 되지만 스키마가 아니다."""
    for payload in ('{"chosen": "MID_CAPPED"}', "[]", '"MID_CAPPED"', "not json at all"):
        with pytest.raises(MixValidationError) as excinfo:
            validate_interpretation(payload, _context(), reason_max_chars=300)
        assert ValidationIssue.INVALID_SCHEMA in excinfo.value.issues


def test_extra_fields_are_rejected() -> None:
    """``extra="forbid"``라 비율 필드를 **끼워 넣을 수도** 없다 — 타입이 막는다."""
    payload = json.dumps(
        {"chosen_candidate_id": "MID_CAPPED", "reason": "ok", "mid_ratio": 0.9}
    )
    with pytest.raises(MixValidationError):
        validate_interpretation(payload, _context(), reason_max_chars=300)


def test_output_schema_has_no_numeric_field() -> None:
    """**안전장치의 전부** — 비율·수량 필드가 없으므로 숫자 생성이 타입으로 불가능하다."""
    from app.purchase_agent.llm.schemas import GradeMixInterpretation

    schema = GradeMixInterpretation.model_json_schema()
    assert set(schema["properties"]) == {"chosen_candidate_id", "reason"}
    assert schema.get("additionalProperties") is False
    for spec in schema["properties"].values():
        assert spec.get("type") == "string", "숫자 타입 필드가 생기면 생성 경로가 열린다"


def test_context_carries_no_numbers() -> None:
    """입력에도 숫자를 넣지 않는다 — 보여주면 사유에 베껴 쓴다 (§4-⑤ "기호화")."""
    import re

    dumped = json.dumps(_context().model_dump(mode="json"), ensure_ascii=False)
    assert not re.search(r"\d", dumped), f"컨텍스트에 숫자가 있다: {dumped}"


# ── 재시도 → fallback ──────────────────────────────────────────────────────


def test_validation_failure_retries_with_guidance_then_falls_back() -> None:
    """검증 실패는 교정 지시와 함께 재시도하고, 끝내 실패하면 규칙 기본안이다."""
    provider = FakeProvider([_reply("NOPE"), _reply("STILL_NOPE")])
    service = MixSelectionService(_settings(retries=1), provider)
    result = service.select(_context(), default_candidate_id="MID_CAPPED")
    assert provider.calls == 2
    assert provider.retry_guidance[1] is not None
    assert result.llm_status == "FALLBACK"
    assert result.llm_fallback_used is True
    assert result.interpretation.chosen_candidate_id == "MID_CAPPED"


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("ANTHROPIC_API_KEY is not set"),
        TimeoutError("timed out"),
        ConnectionError("server down"),
        ValueError("unexpected sdk error"),
    ],
)
def test_every_failure_kind_lands_on_the_same_deterministic_fallback(failure) -> None:
    """**모든 실패 유형이 같은 결정적 결과로 수렴한다** — 키 없음·타임아웃·SDK 예외 무관.

    한 유형만 잡으면 나머지가 그래프를 죽인다. 팀원 환경마다 실패 모양이 다르므로
    "무엇이 터지든 규칙 기본안"이 요건이다.
    """
    provider = FakeProvider([failure, failure])
    service = MixSelectionService(_settings(retries=1), provider)
    result = service.select(_context(), default_candidate_id="BASE_ONLY")
    assert result.llm_status == "FALLBACK"
    assert result.interpretation.chosen_candidate_id == "BASE_ONLY"


def test_unsupported_provider_falls_back_instead_of_crashing() -> None:
    """``LLM_PROVIDER``에 오타가 나도 그래프가 멈추지 않는다."""
    service = MixSelectionService(_settings(provider="nope"), UnavailableProvider())
    result = service.select(_context(), default_candidate_id="MID_CAPPED")
    assert result.llm_status == "FALLBACK"


def test_all_three_providers_are_registered() -> None:
    """세 프로바이더가 같은 프로토콜로 등록돼 있다 — 선택은 환경변수 한 줄이다."""
    assert set(_PROVIDERS) == {"anthropic", "openai", "ollama"}
    for factory in _PROVIDERS.values():
        assert hasattr(factory(_settings()), "generate")


# ── 사중 일치: LLM이 무엇을 골라도 ─────────────────────────────────────────


@pytest.mark.parametrize("candidate_id", ["BASE_ONLY", "MID_HALF", "MID_CAPPED"])
@pytest.mark.parametrize("item", ITEMS)
def test_any_choice_keeps_the_quadruple_match(candidate_id: str, item: str) -> None:
    """**어느 후보를 강제로 고르게 해도** 사중 일치가 유지된다 (4품목 전횡단).

    비율은 규칙이 만든 후보의 값이고 수량은 ⑥이 곱하므로, LLM의 선택이 수량 축에
    닿을 수단 자체가 없다. 그 사실을 강제 선택으로 확인한다.
    """
    proposal = run_purchase_agent(item, SPREAD_WIDE, selector=_fixed_selector(candidate_id))
    assert proposal["scenarios"]
    for scenario in proposal["scenarios"]:
        total = scenario["total_qty_kg"]
        assert total == sum(line["qty_kg"] for line in scenario["split_plan"])
        assert total == sum(line["qty_kg"] for line in scenario["sourcing_plan"])
        assert scenario["total_amount_krw"] == sum(
            line["qty_kg"] * line["grade_unit_price"] for line in scenario["sourcing_plan"]
        )


@pytest.mark.parametrize("item", ITEMS)
def test_no_choice_exceeds_the_mid_grade_cap(item: str) -> None:
    """**백로그 E3-2 DoD** — "중품 과다"는 LLM 판단이 아니라 후보 생성이 막는다.

    모든 후보가 ``cap_ratio`` 이하이므로 LLM이 무엇을 골라도 상한을 넘지 못한다.
    장기 보관 계획(근접 납품이 적은 날)일수록 cap이 낮아져 후보 자체가 낮게 깎인다.
    """
    state = _staged(item, SPREAD_WIDE)
    constraints = load_constraints()
    facts = evaluate_mid_grade(state, constraints)
    cap = facts["cap_ratio"]
    candidates = build_mix_candidates(state, cap, constraints)
    assert len(candidates) >= 2
    for _, ratio, _ in candidates:
        assert ratio <= cap + 1e-9, "후보가 상한을 넘으면 게이트가 사라진 것이다"


def test_long_dated_delivery_shrinks_the_candidate_set() -> None:
    """납품이 먼 합성 입력에선 cap이 내려가 **고비중 후보가 사라진다**.

    mock 4품목은 모두 근접 납품이 있어 cap이 높다 — DoD의 "장기 보관 계획" 쪽은
    합성 입력으로만 밟힌다 (E3-3의 수량 트리거와 같은 상황).
    """
    state = _staged(ITEM, SPREAD_WIDE)
    # 모든 납품을 소진 창 밖으로 밀어낸다 = 중품으로 소화할 근접 수요가 없다
    state["confirmed_orders"] = {
        **state["confirmed_orders"],
        "orders": [{"sale_id": 1, "qty_kg": 18000, "due_date": "2026-12-31"}],
    }
    constraints = load_constraints()
    facts = evaluate_mid_grade(state, constraints)
    assert facts["cap_ratio"] == 0.0
    assert facts["ratio"] == 0.0, "소화할 수 없으면 중품을 태우지 않는다"
    assert build_mix_candidates(state, 0.0, constraints) == [("BASE_ONLY", 0.0, "전량 기준등급")]


# ── 고지 (라벨/행동 일치) ──────────────────────────────────────────────────


def test_applied_choice_appears_in_rationale_not_risks() -> None:
    """판단이 적용되면 rationale에 사유가 실리고 risks엔 아무것도 안 붙는다."""
    proposal = run_purchase_agent(ITEM, SPREAD_WIDE, selector=_fixed_selector("MID_HALF"))
    scenario = proposal["scenarios"][0]
    items = [r for r in scenario["rationale"] if "등급 조합" in r["claim"]]
    assert len(items) == 1
    assert "MID_HALF" in items[0]["claim"]
    assert "규칙 산출값" in items[0]["evidence_detail"]
    assert not [risk for risk in scenario["risks"] if "등급 조합 판단 미적용" in risk]


def test_fallback_is_disclosed_in_risks() -> None:
    """실패하면 **규칙 기본안이 나갔다는 사실**이 보여야 한다 (E3-3·E3-4와 같은 자리)."""

    def failing_selector(context, default_candidate_id):
        del context
        return MixDecision(
            candidate_id=default_candidate_id,
            reason="규칙 기본안",
            llm_status="FALLBACK",
            llm_model="fake-model",
            llm_fallback_used=True,
        )

    proposal = run_purchase_agent(ITEM, SPREAD_WIDE, selector=failing_selector)
    scenario = proposal["scenarios"][0]
    notes = [risk for risk in scenario["risks"] if "등급 조합 판단 미적용" in risk]
    assert len(notes) == 1
    assert "판단자 응답 실패" in notes[0]
    assert "FALLBACK" not in notes[0], "내부 상태 코드를 출력 문구에 쓰지 않는다"
    # 산출물 자체는 규칙 경로와 같아야 한다 — fallback은 회귀가 아니라 무변화다
    rule_only = run_purchase_agent(ITEM, SPREAD_WIDE, selector=None)
    assert [s["total_qty_kg"] for s in proposal["scenarios"]] == [
        s["total_qty_kg"] for s in rule_only["scenarios"]
    ]
    assert [s["sourcing_plan"] for s in proposal["scenarios"]] == [
        s["sourcing_plan"] for s in rule_only["scenarios"]
    ]


def test_choice_survives_self_check() -> None:
    """LLM이 고른 안이 ⑦의 검사를 통과하는가 — 근거가 늘어도 컷되면 안 된다."""
    state = _staged(ITEM, SPREAD_WIDE)
    state.update(allocate_sourcing(state, selector=_fixed_selector("MID_HALF")))
    state.update(package_scenarios(state))
    result = self_check(state)
    assert result["rejected_reasons"] == []
    assert result["scenarios_final"]


# ── 실 프로바이더 (기본 실행에서 제외) ─────────────────────────────────────


@pytest.mark.llm
def test_real_provider_round_trip() -> None:
    """실 프로바이더 왕복. ``uv run pytest -m llm``으로만 돈다.

    기본 실행에서 빠져 있는 이유: API 키(또는 로컬 서버)가 필요하고, 응답이
    결정적이지 않으며, CI가 외부 서비스에 묶이면 안 된다.
    """
    selector = make_mix_selector()
    context = _context(("BASE_ONLY", "MID_HALF", "MID_CAPPED"))
    decision = selector(context, "MID_CAPPED")
    assert decision.llm_status == "SUCCESS", f"실 프로바이더 실패: {decision}"
    assert decision.candidate_id in {"BASE_ONLY", "MID_HALF", "MID_CAPPED"}
    assert not any(ch.isdigit() for ch in decision.reason)


# ── 프로바이더 3종 — 실 API 없이 요청·응답 형태를 검증한다 ─────────────────
#
# ⚠️ 여기가 FakeProvider 테스트와 실 경로가 갈라지는 자리다. 위 테스트들은 전부
# FakeProvider를 꽂으므로 `AnthropicProvider.generate()` 같은 실제 호출 코드는 한 줄도
# 밟지 않는다 — 스키마를 엉뚱한 파라미터에 실어도, 응답 파싱이 틀려도 초록불이 뜬다.
# SDK 클라이언트를 가짜로 바꿔 **요청 페이로드와 응답 파싱**을 실 API 없이 검사한다.


class _Block:
    def __init__(self, type_: str, text: str = ""):
        self.type = type_
        self.text = text


def _anthropic_stub(monkeypatch, blocks, captured: dict):
    """``anthropic.Anthropic``을 가짜로 바꾼다 — 네트워크를 타지 않는다."""
    import anthropic

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Msg", (), {"content": blocks})()

    class _Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)


def test_anthropic_provider_sends_the_schema_and_reads_the_text_block(monkeypatch) -> None:
    """스키마가 ``output_config.format``에 실리고, **thinking 블록을 건너뛰고** text를 읽는다.

    ``content[0]``을 그냥 읽으면 사고 블록이 앞에 오는 모델에서 빈 문자열을 파싱하게 된다.
    """
    from app.purchase_agent.llm.runtime import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    captured: dict = {}
    _anthropic_stub(
        monkeypatch,
        [_Block("thinking", ""), _Block("text", _reply("MID_CAPPED"))],
        captured,
    )
    settings = _settings(provider="anthropic")
    raw = AnthropicProvider(settings).generate(_context())

    assert json.loads(raw)["chosen_candidate_id"] == "MID_CAPPED"
    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert set(fmt["schema"]["properties"]) == {"chosen_candidate_id", "reason"}
    assert captured["max_tokens"] == settings.max_output_tokens
    # SDK 자체 재시도를 끈다 — 두 층이 각자 세면 상한이 곱해지고 타임아웃이 배가 된다
    assert captured["client_kwargs"]["max_retries"] == 0
    assert captured["client_kwargs"]["timeout"] == settings.timeout_seconds


def test_anthropic_provider_raises_when_no_text_block(monkeypatch) -> None:
    """text 블록이 없으면 빈 문자열을 반환하지 않고 터진다 → 서비스가 fallback으로 보낸다."""
    from app.purchase_agent.llm.runtime import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _anthropic_stub(monkeypatch, [_Block("thinking", "")], {})
    with pytest.raises(TypeError):
        AnthropicProvider(_settings(provider="anthropic")).generate(_context())


@pytest.mark.parametrize(
    ("provider_name", "env_key"),
    [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")],
)
def test_provider_without_a_key_raises_before_any_network_call(
    monkeypatch, provider_name: str, env_key: str
) -> None:
    """키가 없으면 **호출 전에** 터진다 — 팀원 환경에서 이 경로가 fallback으로 이어진다."""
    monkeypatch.delenv(env_key, raising=False)
    factory = _PROVIDERS[provider_name]
    with pytest.raises(RuntimeError, match=env_key):
        factory(_settings(provider=provider_name)).generate(_context())


def test_provider_without_a_model_raises(monkeypatch) -> None:
    """모델명이 비면 빈 문자열을 API에 보내지 않는다 — 실패 사유가 흐려진다.

    ``openai``에 기본 모델을 두지 않았으므로(확인하지 않은 id를 코드에 박지 않는다)
    ``LLM_MODEL`` 미설정이 이 경로로 온다.
    """
    from dataclasses import replace

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings = replace(_settings(provider="openai"), model="")
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        _PROVIDERS["openai"](settings).generate(_context())


def test_openai_provider_uses_strict_json_schema(monkeypatch) -> None:
    """OpenAI strict 모드 요건: ``additionalProperties: false`` + 전 필드 ``required``."""
    import openai

    from app.purchase_agent.llm.runtime import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = type("M", (), {"content": _reply("BASE_ONLY")})()
            choice = type("C", (), {"message": message})()
            return type("R", (), {"choices": [choice]})()

    class _Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(openai, "OpenAI", _Client)
    raw = OpenAIProvider(_settings(provider="openai")).generate(_context())

    assert json.loads(raw)["chosen_candidate_id"] == "BASE_ONLY"
    schema_block = captured["response_format"]["json_schema"]
    assert schema_block["strict"] is True
    schema = schema_block["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]), (
        "strict 모드는 모든 property가 required여야 한다 — 아니면 API가 400을 낸다"
    )


def test_response_schema_satisfies_both_providers() -> None:
    """한 스키마가 Anthropic 구조화 출력과 OpenAI strict를 **둘 다** 만족하는가.

    갈라지면 프로바이더를 바꿀 때 조용히 한쪽만 동작한다.
    """
    from app.purchase_agent.llm.runtime import _response_schema

    schema = _response_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    # Anthropic 구조화 출력이 지원하지 않는 제약이 섞이지 않았는가
    for spec in schema["properties"].values():
        assert not {"minLength", "maxLength", "minimum", "maximum"} & set(spec)


def test_empty_fields_are_rejected_by_the_common_layer() -> None:
    """빈/공백 응답은 **검증기**가 막는다 — 스키마는 두 API 제약상 길이를 못 건다.

    ``reason``이 비면 근거 없는 판단이 출력에 실리고, id가 비면 후보 대조가 무의미해진다.
    """
    for payload in (
        json.dumps({"chosen_candidate_id": "", "reason": "ok"}),
        json.dumps({"chosen_candidate_id": "MID_CAPPED", "reason": ""}),
        json.dumps({"chosen_candidate_id": "MID_CAPPED", "reason": "   \n\t "}),
    ):
        with pytest.raises(MixValidationError) as excinfo:
            validate_interpretation(payload, _context(), reason_max_chars=300)
        assert ValidationIssue.EMPTY_FIELD in excinfo.value.issues


# ── Codex 교차검증 회귀 테스트 ─────────────────────────────────────────────


def test_candidate_outside_the_set_is_caught_at_the_adapter_boundary() -> None:
    """**Codex 교차검증 P2 회귀.** 주입된 selector는 서비스 검증기를 우회한다.

    우회하면 비율만 규칙값으로 되돌아가고 결정 객체는 살아남아, 출력에 "없는 후보를
    선택함"이라고 기록됐다 — 라벨과 행동이 어긋나는 상태다. 노드가 자기 후보 집합으로
    한 번 더 확인하고, **지우지 않고 실패로 표시해** 고지가 나가게 한다.
    """

    def rogue(context, default_candidate_id):
        del context, default_candidate_id
        return MixDecision("MID_DOUBLE", "없는 후보", "SUCCESS", "fake", False)

    scenario = run_purchase_agent("배추", SPREAD_WIDE, selector=rogue)["scenarios"][0]
    assert not [r for r in scenario["rationale"] if "MID_DOUBLE" in r["claim"]]
    notes = [risk for risk in scenario["risks"] if "등급 조합 판단 미적용" in risk]
    assert len(notes) == 1, "조용히 되돌리지 않는다 — 되돌린 사실이 보여야 한다"
    # 배분은 규칙 기본안 그대로
    rule_only = run_purchase_agent("배추", SPREAD_WIDE, selector=None)["scenarios"][0]
    assert scenario["sourcing_plan"] == rule_only["sourcing_plan"]


def test_choosing_base_only_is_still_a_recorded_judgment() -> None:
    """**Codex 교차검증 P2 회귀.** "확대됐지만 중품을 안 쓴다"도 판단이다.

    비율이 0이 되면서 rationale·risks가 조기 반환해 판단 사실이 통째로 사라졌다.
    소비자는 "평시라 애초에 후보가 없었다"와 구분할 수 없게 된다.
    """

    def base_only(context, default_candidate_id):
        del context, default_candidate_id
        return MixDecision(
            "BASE_ONLY", "신선도가 빡빡해 중품을 쓰지 않는다", "SUCCESS", "fake", False
        )

    scenario = run_purchase_agent("배추", SPREAD_WIDE, selector=base_only)["scenarios"][0]
    assert all(line["grade"] == "상" for line in scenario["sourcing_plan"])
    items = [r for r in scenario["rationale"] if "등급 조합" in r["claim"]]
    assert len(items) == 1, "중품을 안 쓰기로 한 판단도 근거에 남는다"
    assert "BASE_ONLY" in items[0]["claim"]


def test_broken_env_settings_do_not_break_the_graph(monkeypatch) -> None:
    """**Codex 교차검증 P2 회귀.** ``.env`` 오타 하나로 877건이 죽으면 안 된다.

    ``get_llm_settings()``는 ``build_graph()`` 안에서 불리고 그건 **게이팅 이전**이다 —
    파싱이 터지면 ``LLM_ENABLED=false``여도 그래프가 멈춘다.
    """
    for key, value in [
        ("PURCHASE_LLM_TIMEOUT_SECONDS", "삼십"),
        ("PURCHASE_LLM_MAX_RETRIES", "many"),
        ("PURCHASE_LLM_MAX_OUTPUT_TOKENS", "8k"),
    ]:
        monkeypatch.setenv(key, value)
    from app.purchase_agent.llm.runtime import get_llm_settings

    settings = get_llm_settings()
    assert settings.timeout_seconds == 30.0
    assert settings.max_retries == 1
    assert settings.max_output_tokens == 8192
    # 그래프도 그대로 돈다
    assert run_purchase_agent("배추", SPREAD_WIDE)["scenarios"]


def test_unicode_numerals_and_control_chars_are_rejected() -> None:
    """**Codex 교차검증 P2 회귀.** ``\\d``만으로는 ``½``·``²``·제어문자를 못 막는다.

    ⚠️ 한글 수사("백삼십원")는 여전히 통과한다 — 정규식으로 판별 불가능한 영역이고,
    그건 프롬프트가 맡는다. 여기서 잡는 건 기계적으로 판별되는 것뿐이라는 걸 명시해둔다.
    """
    for bad in ("중품이 ½ 더 싸다", "이득이 ² 배다", "십이 Ⅻ 단위"):
        with pytest.raises(MixValidationError) as excinfo:
            validate_interpretation(
                _reply("MID_CAPPED", bad), _context(), reason_max_chars=300
            )
        assert ValidationIssue.NUMERIC_OUTPUT_FORBIDDEN in excinfo.value.issues

    # 리터럴 제어문자는 ruff PLE2502(난독화 가능)에 걸린다 — chr()로 만든다.
    zero_width, nul, bidi = chr(0x200B), chr(0x00), chr(0x202E)
    for bad in (f"정상{zero_width}텍스트", f"제어{nul}문자", f"방향{bidi}전환"):
        with pytest.raises(MixValidationError) as excinfo:
            validate_interpretation(
                _reply("MID_CAPPED", bad), _context(), reason_max_chars=300
            )
        assert ValidationIssue.CONTROL_CHARACTERS in excinfo.value.issues


def test_unknown_fraction_still_produces_a_candidate() -> None:
    """**Codex 교차검증 P1 회귀.** constraints에 배수를 추가하면 후보가 **생겨야** 한다.

    이름 표를 단일 소스처럼 쓰면 ``0.25``를 넣었을 때 그 후보가 아무 말 없이 사라진다 —
    규칙 7("임계는 YAML 단일 소스")이 막으려는 형태다.
    """
    from app.purchase_agent.nodes.allocate_sourcing import candidate_label

    state = _staged(ITEM, SPREAD_WIDE)
    constraints = load_constraints()
    constraints["grade"]["mix_candidate_fractions"] = [0.0, 0.25, 1.0]
    candidates = build_mix_candidates(state, 0.6, constraints)
    assert len(candidates) == 3, "모르는 배수도 후보가 된다"
    assert candidate_label(0.25)[0] not in {"BASE_ONLY", "MID_HALF", "MID_CAPPED"}


def test_reason_length_threshold_comes_from_constraints() -> None:
    """**Codex 교차검증 P1 회귀.** 임계는 코드가 아니라 YAML이 소유한다 (규칙 7)."""
    threshold = load_constraints()["grade"]["mix_reason_max_chars"]
    assert isinstance(threshold, int) and threshold > 0
    long_reason = "가" * (threshold + 1)
    with pytest.raises(MixValidationError) as excinfo:
        validate_interpretation(
            _reply("MID_CAPPED", long_reason), _context(), reason_max_chars=threshold
        )
    assert ValidationIssue.REASON_TOO_LONG in excinfo.value.issues
    # 임계 이하는 통과 — 검사가 임계를 실제로 쓰는지 확인
    ok = validate_interpretation(
        _reply("MID_CAPPED", "가" * threshold), _context(), reason_max_chars=threshold
    )
    assert ok.chosen_candidate_id == "MID_CAPPED"


def test_ollama_provider_sends_the_schema_and_token_cap(monkeypatch) -> None:
    """**Codex 교차검증 P2 회귀.** Ollama 경로도 실행돼야 버그가 드러난다.

    세 프로바이더가 같은 ``max_output_tokens``를 각자의 이름으로 받는지 확인한다 —
    Ollama는 ``num_predict``다.
    """
    import urllib.request

    from app.purchase_agent.llm.runtime import OllamaProvider

    captured: dict = {}

    class _Response:
        def read(self):
            return json.dumps(
                {"message": {"content": _reply("MID_HALF")}}
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    settings = _settings(provider="ollama")
    raw = OllamaProvider(settings).generate(_context())

    assert json.loads(raw)["chosen_candidate_id"] == "MID_HALF"
    assert captured["url"].endswith("/api/chat")
    assert captured["timeout"] == settings.timeout_seconds
    payload = captured["payload"]
    assert payload["options"]["temperature"] == 0, "팀 규약 — 결정성"
    assert payload["options"]["num_predict"] == settings.max_output_tokens
    assert set(payload["format"]["properties"]) == {"chosen_candidate_id", "reason"}


def test_every_provider_applies_the_output_token_cap() -> None:
    """세 프로바이더가 **같은 설정값**을 쓰는지 — 이름만 다르다.

    한 곳이라도 빠지면 설정을 낮춰도 그 프로바이더만 장문을 생성한다 (비용·지연).
    """
    import inspect

    from app.purchase_agent.llm.runtime import (
        AnthropicProvider,
        OllamaProvider,
        OpenAIProvider,
    )

    for provider, needle in [
        (AnthropicProvider, "max_tokens"),
        (OpenAIProvider, "max_completion_tokens"),
        (OllamaProvider, "num_predict"),
    ]:
        source = inspect.getsource(provider.generate)
        assert needle in source, f"{provider.__name__}에 토큰 상한이 없다"
        assert "max_output_tokens" in source


def test_load_dotenv_cannot_resurrect_a_cleared_key(monkeypatch, tmp_path) -> None:
    """**Codex 교차검증 P2 회귀.** ``load_dotenv``가 지운 키를 ``.env``에서 되살린다.

    ``get_llm_settings()``는 ``load_dotenv()``를 부르고, 그건 기본이 ``override=False``라
    **미설정** 변수만 채운다. conftest가 ``delenv``로 키를 지우면 "미설정"이 되어 ``.env``의
    진짜 키가 다시 실린다 — 키를 가진 개발자 머신에서 테스트가 실 API를 타는 경로다.

    ⚠️ 이 저장소의 ``.env``에는 LLM 키가 없어서 그냥 변이를 넣어도 드러나지 않는다.
    가짜 ``.env``를 만들어 **그 구멍을 직접 재현**한다 — 없으면 이 방어는 검증되지 않는다.
    """
    from app.purchase_agent.llm import runtime

    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_ENV_FILES", (env_file,))

    # conftest가 하는 것과 같은 처리: 빈 문자열로 둔다
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    runtime.get_llm_settings()
    assert os.environ["ANTHROPIC_API_KEY"] == "", (
        "빈 문자열은 '설정됨'이라 load_dotenv가 덮어쓰지 않는다"
    )

    # 반대로 지워두면 되살아난다 — 이게 막으려는 경로다
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runtime.get_llm_settings()
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-from-dotenv", (
        "이 단언이 깨지면 load_dotenv 동작이 바뀐 것이다 — conftest 전략을 재검토할 것"
    )


def test_conftest_blanks_the_keys_rather_than_deleting_them() -> None:
    """conftest의 **선택 자체**를 잠근다 — 위 테스트는 동작만 문서화한다.

    ``delenv``로 지우면 ``load_dotenv``가 ``.env``의 진짜 키를 되살린다. 빈 문자열은
    "설정됨"이라 덮어쓰이지 않고, 프로바이더의 ``if not api_key``에 걸린다.

    이 저장소의 ``.env``에는 LLM 키가 없어 그 차이가 산출물로 드러나지 않는다 — 그래서
    전략을 **직접** 단언한다. 키를 가진 팀원 머신에서만 터지는 종류의 회귀다.
    """
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert os.environ.get(key) == "", (
            f"{key}가 빈 문자열이 아니다 — conftest가 delenv를 쓰면 load_dotenv가 되살린다"
        )
    assert os.environ.get("PURCHASE_LLM_ENABLED") == "false"
