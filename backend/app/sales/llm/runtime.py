"""Sales 후보 해석을 Gemini 구조화 출력으로 연결한다."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from app.sales.schemas import SalesCandidate, SalesRecommendation

_NUMBER = re.compile(r"\d")
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_SYSTEM_PROMPT = """Sales 후보 중 하나만 추천하세요. 후보 ID 외 숫자·금액·수량·날짜를 쓰지 말고,
새 후보나 조건을 만들지 마세요. 모든 문장은 자연스러운 한국어로 작성하세요."""
_ENV_FILES = (
    Path(__file__).resolve().parents[3] / ".env",
    Path(__file__).resolve().parents[4] / ".env",
)


_PLANNER_SYSTEM_PROMPT = """당신은 판매 전략 자세만 정합니다.
CONSERVATIVE · BALANCED · AGGRESSIVE 세 전략의 자세를 각각 한 번씩 고르세요.

주어진 재무·물류·ML 사실은 **판단 재료**입니다. 그 값을 답에 옮겨 적지 마세요.
가격·수량·금액·마진·판정은 결정론 코드가 계산하므로 절대 만들지 마세요.
reason_codes 에 숫자를 한 글자도 쓰지 말고, 주어진 어휘 밖의 값을 만들지 마세요.
DEPLETION 자세는 소진 신호가 실제로 있을 때만 고르세요."""


class StrategyPlanningInput(BaseModel):
    """Planner 가 보는 **사실**. 재무·물류·ML 이 실제로 보낸 값이다.

    ★ **숫자를 보여 준다. 숫자를 받지는 않는다.**

      ```text
      입력   현금·채무·채권·여신·재고·시장 밴드 — 판단에 필요하니 준다
      출력   닫힌 어휘의 자세뿐 — 숫자가 섞이면 계획을 통째로 버린다
      ```

      모델이 본 숫자가 가격이 될 길이 없다. 단가·수량·금액은 `proposal.py` 의
      결정론 계산이 **자세만 읽고** 만들고, 모델 출력에는 숫자를 담을 칸이 없다.

    🔴 **판정 라벨을 주지 않는다.** `finance_verdict` 같은 값은 여기 없다 — 후보가
      아직 없으므로 판정도 없고, 있다 해도 모델이 판정을 따라 적을 자리를 만들지
      않는다.
    """

    model_config = ConfigDict(extra="forbid")

    #: 사용자
    business_mode: str | None = None
    #: 사용자가 말로 남긴 의도. **해석은 모델이 하고 숫자는 여기서 안 나온다** (§16).
    user_intent_text: str | None = None
    #: 물류
    depletion_pressure: bool
    freshness_risk_codes: list[str] = Field(default_factory=list)
    has_freshness_risk_lots: bool = False
    sell_priority: str | None = None
    inventory_risk_severity: str | None = None
    inventory_available_kg: float | None = None
    inventory_cost_basis_known: bool = False
    delivery_status: str | None = None
    #: 재무
    payment_pressure: str | None = None
    credit_state: Literal["AVAILABLE", "EXHAUSTED", "UNKNOWN"] = "UNKNOWN"
    finance_context_available: bool = False
    available_cash_krw: float | None = None
    base_projected_cash_min_krw: float | None = None
    minimum_cash_balance_krw: float | None = None
    payables_total_krw: float | None = None
    payables_due_7d_krw: float | None = None
    payables_due_30d_krw: float | None = None
    receivables_total_krw: float | None = None
    partner_receivable_krw: float | None = None
    partner_credit_available_krw: float | None = None
    #: ML
    ml_band_available: bool = False
    ml_target_kind: str | None = None
    ml_use_recommended: bool | None = None
    ml_lower: float | None = None
    ml_predicted: float | None = None
    ml_upper: float | None = None
    #: 되먹임 회차에서만 채워진다.
    feedback_reason_codes: list[str] = Field(default_factory=list)


class LlmStrategyProfileOutput(BaseModel):
    """모델이 돌려주는 자세 하나. **닫힌 어휘라 숫자가 들어올 칸이 없다.**"""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
    price_posture: Literal["MARGIN_DEFENSE", "MARKET_ALIGNED", "DEPLETION"]
    quantity_posture: Literal["LIMITED", "NORMAL", "EXPANDED"]
    inventory_posture: Literal["NORMAL", "FIFO", "FRESHNESS_RISK_FIRST"]
    credit_posture: Literal["STRICT", "NORMAL", "WITHIN_LIMIT"]
    cash_posture: Literal["DEFENSIVE", "NORMAL", "CASH_CONVERSION"]
    reason_codes: list[str] = Field(default_factory=list)


class LlmStrategyPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategies: list[LlmStrategyProfileOutput]


@dataclass(frozen=True)
class StrategyPlanOutcome:
    """자세 셋과 **그것을 누가 만들었는가.** 실패를 숨기지 않는다 (§10)."""

    source: str
    llm_status: str
    profiles: list[Any]
    llm_provider: str | None = None
    llm_model: str | None = None


def plan_strategy_profiles(*, signals: Any, template: list[Any]) -> StrategyPlanOutcome:
    """후보를 만들기 **전에** 세 전략의 자세를 정한다.

    ```text
    설정 꺼짐          DISABLED   → 템플릿
    호출 실패·계약 위반 FALLBACK   → 템플릿
    성공              SUCCESS    → 모델 자세 (호출부가 사실로 한 번 더 깎는다)
    ```

    🔴 **템플릿으로 떨어져도 세 전략은 선다.** 외부 모델 하나 때문에 판매안이
      안 나오면, 그 모델이 없는 날 사업이 멈춘다.
    """
    settings = load_settings()
    if not settings.enabled:
        return StrategyPlanOutcome("TEMPLATE_FALLBACK", "DISABLED", template, None, settings.model)
    try:
        output = _call_gemini_planner(_planner_context(signals), settings)
        profiles = _validated_profiles(output, template)
    except Exception:  # noqa: BLE001 - 외부 호출 실패는 판매안 실패가 아니다.
        return StrategyPlanOutcome(
            "TEMPLATE_FALLBACK", "FALLBACK", template, settings.provider, settings.model
        )
    return StrategyPlanOutcome("LLM", "SUCCESS", profiles, settings.provider, settings.model)


def _planner_context(signals: Any) -> StrategyPlanningInput:
    """모델에 나가는 것 전부. **여기 없는 것은 모델이 못 본다.**"""
    if signals.credit_available_krw is None:
        credit_state = "UNKNOWN"
    else:
        credit_state = "EXHAUSTED" if signals.credit_available_krw <= 0 else "AVAILABLE"

    def num(value: Any) -> float | None:
        return None if value is None else float(value)

    return StrategyPlanningInput(
        business_mode=signals.business_mode,
        user_intent_text=signals.user_intent_text,
        depletion_pressure=signals.depletion_pressure,
        freshness_risk_codes=list(signals.freshness_risk_codes),
        has_freshness_risk_lots=bool(signals.item_lot_ids) and bool(signals.freshness_risk_codes),
        sell_priority=signals.sell_priority,
        inventory_risk_severity=signals.inventory_risk_severity,
        inventory_available_kg=num(signals.inventory_available_kg),
        inventory_cost_basis_known=signals.inventory_cost_basis_known,
        delivery_status=signals.delivery_status,
        payment_pressure=signals.payment_pressure,
        credit_state=credit_state,
        finance_context_available=signals.has_finance_context,
        available_cash_krw=num(signals.available_cash_krw),
        base_projected_cash_min_krw=num(signals.base_projected_cash_min_krw),
        minimum_cash_balance_krw=num(signals.minimum_cash_balance_krw),
        payables_total_krw=num(signals.payables_total_krw),
        payables_due_7d_krw=num(signals.payables_due_7d_krw),
        payables_due_30d_krw=num(signals.payables_due_30d_krw),
        receivables_total_krw=num(signals.receivables_total_krw),
        partner_receivable_krw=num(signals.partner_receivable_krw),
        partner_credit_available_krw=num(signals.credit_available_krw),
        ml_band_available=signals.ml_gate_open,
        ml_target_kind=signals.ml_target_kind,
        ml_use_recommended=signals.ml_use_recommended,
        ml_lower=num(signals.ml_lower),
        ml_predicted=num(signals.ml_predicted),
        ml_upper=num(signals.ml_upper),
        feedback_reason_codes=list(signals.feedback_reason_codes),
    )


def _call_gemini_planner(context: StrategyPlanningInput, settings: LLMSettings):
    """해석 호출과 **같은 전선, 다른 계약**이다 — 스키마와 지시문만 다르다."""
    return _gemini_structured(
        system_prompt=_PLANNER_SYSTEM_PROMPT,
        user_json=json.dumps(context.model_dump(), ensure_ascii=False),
        schema_model=LlmStrategyPlanOutput,
        settings=settings,
    )


def _validated_profiles(output: LlmStrategyPlanOutput, template: list[Any]) -> list[Any]:
    """모델 자세를 받아들일지 정한다. **어긋나면 통째로 버린다.**

    ★ 일부만 고쳐 쓰지 않는다. 세 전략 중 하나가 빠졌거나 두 번 왔으면 모델이 계약을
      이해하지 못한 것이고, 그런 계획에서 한 줄만 건져 쓰면 **어디까지가 모델의
      판단인지** 아무도 말할 수 없다.

    🔴 **사유에 숫자가 있으면 버린다.** 자세는 라벨이고, 라벨에 숫자가 섞이는 순간
      모델이 값을 말하기 시작한 것이다.
    """
    from app.sales.strategy import StrategyProfile

    names = [item.strategy for item in output.strategies]
    if sorted(names) != ["AGGRESSIVE", "BALANCED", "CONSERVATIVE"]:
        raise ValueError("strategy set is not A/B/C exactly once")
    for item in output.strategies:
        if any(_NUMBER.search(code) for code in item.reason_codes):
            raise ValueError("unsafe planner output")
    del template
    return [StrategyProfile.model_validate(item.model_dump()) for item in output.strategies]


class CandidateInterpretationInput(BaseModel):
    """LLM은 식별자와 의미 라벨만 받아 숫자를 바꿀 수 없다."""

    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    strategy_label: str | None = None
    adjustment_axis: str
    conditional: bool
    risk_labels: list[str] = Field(default_factory=list)
    uncertainty_labels: list[str] = Field(default_factory=list)


class LlmInterpretationOutput(BaseModel):
    """숫자 필드가 없는 LLM 해석 결과."""

    model_config = ConfigDict(extra="forbid")
    recommended_candidate_id: str
    summary: str
    recommendation_reason: str
    risk_explanation: str
    user_message: str


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    model: str
    timeout_seconds: float


def load_settings() -> LLMSettings:
    """Sales 전용 설정을 우선하고 전역 Ollama 설정이 모델로 섞이지 않게 한다."""
    _load_environment()
    enabled = _read_bool("SALES_LLM_ENABLED")
    if enabled is None:
        enabled = _read_bool("LLM_ENABLED")
    provider = (os.getenv("SALES_LLM_PROVIDER") or "gemini").strip().lower()
    explicit_model = os.getenv("SALES_LLM_MODEL")
    common_provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    common_model = os.getenv("LLM_MODEL")
    # 공통 Provider가 다르면 다른 Agent의 모델명을 Sales에 물려주지 않는다.
    inherited_model = (
        common_model if provider == common_provider and common_model else _DEFAULT_MODEL
    )
    model = explicit_model or inherited_model
    return LLMSettings(
        enabled=False if enabled is None else enabled,
        provider=provider,
        model=model,
        timeout_seconds=max(0.1, float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))),
    )


def interpret_candidates(
    candidates: list[SalesCandidate], *, recommended_candidate_id: str | None = None
) -> SalesRecommendation:
    """Gemini 실패는 Scenario를 바꾸지 않고 결정론 fallback으로만 전환한다."""
    settings = load_settings()
    if not candidates:
        return _fallback(candidates, "SKIPPED_TEMPLATE", settings, 0, recommended_candidate_id)
    if not settings.enabled:
        return _fallback(candidates, "DISABLED", settings, 0, recommended_candidate_id)
    try:
        output = _call_gemini(_safe_context(candidates), settings)
        return _validated(candidates, output, settings, recommended_candidate_id)
    except Exception:  # noqa: BLE001 - 외부 호출 실패는 Sales 제안 실패가 아니다.
        return _fallback(candidates, "FALLBACK", settings, 1, recommended_candidate_id)


def _safe_context(candidates: list[SalesCandidate]) -> list[CandidateInterpretationInput]:
    """LLM에는 수량·가격·날짜를 전달하지 않고 의미 라벨만 전달한다."""
    return [
        CandidateInterpretationInput(
            candidate_id=c.candidate_id,
            strategy_label=c.strategy_label,
            adjustment_axis=c.adjustment_axis,
            conditional=c.conditional,
            risk_labels=c.risks,
            uncertainty_labels=c.uncertainties,
        )
        for c in candidates
    ]


def _call_gemini(context: list[CandidateInterpretationInput], settings: LLMSettings):
    """Gemini의 JSON Schema 응답을 받아 Sales 전용 계약으로 검증한다."""
    return _gemini_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_json=json.dumps([c.model_dump() for c in context], ensure_ascii=False),
        schema_model=LlmInterpretationOutput,
        settings=settings,
    )


def _gemini_structured(
    *, system_prompt: str, user_json: str, schema_model: type[BaseModel], settings: LLMSettings
):
    """구조화 출력 한 번. **두 호출(해석·전략)이 같은 전선을 쓴다.**

    ★ 전선을 두 벌로 두면 타임아웃·키·스키마 낮추기가 두 곳에서 갈린다 — 한쪽만
      고치는 날이 오고, 그날 한쪽 호출만 조용히 다른 규칙으로 돈다.
    """
    if settings.provider != "gemini":
        raise RuntimeError("unsupported Sales LLM provider")
    api_key = os.getenv("SALES_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Sales Gemini API key is not set")
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_json}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_safe_schema(schema_model.model_json_schema()),
        },
    }
    request = urllib.request.Request(
        f"{_GEMINI_BASE_URL}/models/{settings.model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
        document = json.loads(response.read().decode("utf-8"))
    return schema_model.model_validate_json(_gemini_response_text(document))


def _gemini_response_text(document: dict[str, Any]) -> str:
    candidates = document.get("candidates") or []
    parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    raise ValueError("empty Gemini response")


def _gemini_safe_schema(node: Any, defs: Mapping[str, Any] | None = None) -> Any:
    """Pydantic Schema를 Gemini가 받는 표현으로만 낮추고 계약 의미는 유지한다.

    🔴 **중첩 모델의 `$defs`·`$ref` 를 펴 넣는다** (2026-09-16 실측으로 고친 자리).

      Pydantic 은 모델 안에 모델이 있으면 그 정의를 `$defs` 로 빼고 자리에는
      `$ref` 만 남긴다. Gemini `responseSchema` 는 그 둘을 모르고 **요청 자체를
      거부한다.**

      ```text
      HTTP 400  Unknown name "$defs" at 'generation_config.response_schema'
                Unknown name "$ref"  at '...properties[0].value.items'
      ```

      ⚠️ **전략 Planner 가 그래서 한 번도 안 돌았다.** 해석 호출
        (`LlmInterpretationOutput`)은 중첩이 없어 평면 스키마라 통과했고, 중첩이 있는
        `LlmStrategyPlanOutput` 만 매번 400 을 받아 `FALLBACK` 으로 떨어졌다 —
        상태 칸은 정직하게 `FALLBACK` 을 말하고 있었지만 **원인이 호출 밖이 아니라
        우리 스키마였다.**

    ★ **`$defs` 는 결과에서 지운다.** 펴 넣은 뒤에도 남겨 두면 Gemini 가 그 키를
      모른다고 다시 거부한다.

    ⚠️ **자기 자신을 참조하는 모델은 못 편다.** 지금 두 계약에는 없고, 생기면 여기서
      무한히 돈다 — 그런 모델을 만들지 않는 것이 계약이다.
    """
    if defs is None and isinstance(node, dict):
        defs = node.get("$defs") or {}
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        # ★ `#/$defs/Name` 의 마지막 조각이 정의 이름이다. 못 찾으면 펴지 않고
        #   그대로 두는 대신 **비운다** — 없는 정의를 지어내면 계약이 달라진다.
        target = (defs or {}).get(ref.rsplit("/", 1)[-1])
        if not isinstance(target, Mapping):
            raise TypeError(f"cannot resolve schema reference: {ref}")
        # `$ref` 옆에 붙은 칸(description 등)은 정의를 덮어쓴다 — JSON Schema 관례다.
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _gemini_safe_schema(merged, defs)
    excluded = {"const", "anyOf", "additionalProperties", "$defs"}
    safe = {key: value for key, value in node.items() if key not in excluded}
    if "const" in node:
        safe.update({"type": "string", "enum": [node["const"]]})
    if "anyOf" in node:
        branches = [b for b in node["anyOf"] if isinstance(b, dict) and b.get("type") != "null"]
        if len(branches) != len(node["anyOf"]):
            safe["nullable"] = True
        if len(branches) == 1:
            safe.update(_gemini_safe_schema(branches[0], defs))
        elif branches:
            safe["anyOf"] = [_gemini_safe_schema(branch, defs) for branch in branches]
    if "properties" in node:
        safe["properties"] = {
            name: _gemini_safe_schema(child, defs) for name, child in node["properties"].items()
        }
    if "items" in node:
        safe["items"] = _gemini_safe_schema(node["items"], defs)
    return safe


def _validated(candidates, output, settings, fixed_recommendation=None) -> SalesRecommendation:
    """후보 ID·빈 문장·숫자 포함 여부를 검사해 LLM의 권한을 제한한다."""
    selectable = {c.candidate_id for c in candidates if "FINANCE_FAIL" not in c.risks}
    if fixed_recommendation is None and output.recommended_candidate_id not in selectable:
        raise ValueError("unknown candidate")
    if fixed_recommendation is not None and fixed_recommendation not in selectable:
        raise ValueError("deterministic recommendation is not selectable")
    texts = (
        output.summary,
        output.recommendation_reason,
        output.risk_explanation,
        output.user_message,
    )
    if any(not text.strip() or _NUMBER.search(text) for text in texts):
        raise ValueError("unsafe LLM output")
    return SalesRecommendation(
        status="SUCCESS",
        recommended_candidate_id=(fixed_recommendation or output.recommended_candidate_id),
        summary=output.summary,
        recommendation_reason=output.recommendation_reason,
        risk_explanation=output.risk_explanation,
        user_message=output.user_message,
        llm_provider="gemini",
        llm_model=settings.model,
        llm_attempts=1,
        llm_fallback_used=False,
    )


def _fallback(
    candidates, status, settings, attempts, fixed_recommendation=None
) -> SalesRecommendation:
    selectable = [c for c in candidates if "FINANCE_FAIL" not in c.risks]
    candidate = next((c for c in selectable if not c.conditional), None)
    if candidate is None and selectable:
        candidate = selectable[0]
    return SalesRecommendation(
        status=status,
        recommended_candidate_id=(
            fixed_recommendation
            if fixed_recommendation in {item.candidate_id for item in selectable}
            else candidate.candidate_id
            if candidate
            else None
        ),
        summary="규칙 기반 판매안을 준비했습니다.",
        recommendation_reason="외부 해석 없이 근거가 있는 판매 조건을 우선 표시합니다.",
        risk_explanation="외부 검증 결과와 조건부 조달 여부를 함께 확인해 주세요.",
        user_message="현재 확인된 조건을 기준으로 판매안을 검토해 주세요.",
        llm_provider=settings.provider if settings.enabled else None,
        llm_model=settings.model,
        llm_attempts=attempts,
        llm_fallback_used=status == "FALLBACK",
    )


def _load_environment() -> None:
    for env_file in _ENV_FILES:
        load_dotenv(env_file, override=False)


def _read_bool(name: str) -> bool | None:
    value = os.getenv(name)
    return None if value is None else value.strip().lower() in {"1", "true", "yes", "on"}
