"""판매 전략 Planner — **자세를 정하는 자리. 숫자를 정하는 자리가 아니다.**

```text
사실 수집 (derive_signals)   물류·재무·ML 이 이미 보낸 것만 읽는다
        ↓
전략 구성 (plan_strategies)   LLM 이 A/B/C 의 자세를 고른다 (실패하면 규칙 템플릿)
        ↓
결정론 구체화 (proposal.py)   그 자세를 실제 수량·단가·금액으로 만든다
```

🔴 **LLM 이 고르는 것은 자세뿐이다.** 단가·수량·금액·마진·판정은 한 글자도
  건드리지 못한다 — 자세는 닫힌 어휘이고, 어휘 밖 값이 오면 그 계획은 통째로
  버려지고 템플릿이 대신 선다.

🔴 **자세가 사실을 이기지 못한다.** 모델이 `DEPLETION` 을 고르더라도 소진 신호가
  **물류 회신에 없으면** 그 자세는 내려간다 (`_clamp`). 모델이 시장 하단 가격을
  여는 길을 스스로 만들 수 없다는 뜻이고, 그것이 이 파일의 핵심 방어다.

★ **소진 신호를 되먹임에 의존하지 않는다** (2026-09-16). 종전에는
  `sell_priority`·`inventory_risk_severity` 를 `domain_replies` 에서만 읽었는데, 그
  칸은 물류 `PRE_SALES` payload 에 **없다** — 1차 생성에서는 영원히 `None` 이었고
  (실측: 판매 안 17,364 건 전부 NULL) 그래서 공격안이 한 번도 소진 전략으로 서지
  못했다. 이제 1차 입력인 `logistics_context` 를 정본으로 읽고, 되먹임 회신은
  **보조**로만 얹는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StrategyName = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]

PricePosture = Literal["MARGIN_DEFENSE", "MARKET_ALIGNED", "DEPLETION"]
"""가격을 **어느 기준에 붙일 것인가.** 값이 아니라 기준이다.

```text
MARGIN_DEFENSE   시장 상단과 마진 경고선 중 높은 쪽
MARKET_ALIGNED   시장 예측치와 마진 최저선 중 높은 쪽
DEPLETION        시장 하단까지 열되 마진 최저선 아래로는 안 간다
```
"""

QuantityPosture = Literal["LIMITED", "NORMAL", "EXPANDED"]
InventoryPosture = Literal["NORMAL", "FIFO", "FRESHNESS_RISK_FIRST"]
CreditPosture = Literal["STRICT", "NORMAL", "WITHIN_LIMIT"]
CashPosture = Literal["DEFENSIVE", "NORMAL", "CASH_CONVERSION"]

#: 물류가 이미 쓰고 있는 신선도 위험 코드. **새로 만들지 않는다.**
#:
#: 🔴 `STORAGE_TARGET_EXCEEDED` 를 여기 넣지 않는다. 보관 목표를 넘겼다는 것은
#:   *"많이 쌓였다"* 이지 *"상한다"* 가 아니다 — 물류가 그 둘을 다른 코드로 낸다.
FRESHNESS_RISK_CODE = "FRESHNESS_QUALITY_RISK"

#: 되먹임 회신에서 읽는 강한 소진 신호. **기존 판정 그대로다** (`_depletion_pressure`).
_HIGH_SELL_PRIORITY = "HIGH"
_SEVERE_INVENTORY_RISK = frozenset({"SEVERE", "CRITICAL"})

#: 자금 압박 라벨. **재무 어휘 그대로다** (`derive_cash_priority`).
_TIGHT_CASH = frozenset({"HIGH", "MEDIUM"})


class StrategyProfile(BaseModel):
    """A/B/C 하나의 **자세**. 숫자가 한 칸도 없다 — 그것이 계약이다."""

    model_config = ConfigDict(extra="forbid")

    strategy: StrategyName
    price_posture: PricePosture
    quantity_posture: QuantityPosture
    inventory_posture: InventoryPosture
    credit_posture: CreditPosture
    cash_posture: CashPosture
    #: 왜 이 자세인가. **닫힌 어휘가 아니다** — 사람이 읽는 사유이고 판정에 안 쓴다.
    reason_codes: list[str] = Field(default_factory=list)


class StrategyPlan(BaseModel):
    """세 자세와 **그것을 누가 만들었는가.**

    🔴 **출처를 숨기지 않는다** (§10). 모델이 실패했는데 성공처럼 보이면, 모델이
      죽은 날과 산 날이 화면에서 같아진다.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["LLM", "TEMPLATE_FALLBACK"]
    llm_status: Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]
    profiles: list[StrategyProfile]
    llm_provider: str | None = None
    llm_model: str | None = None
    #: 모델이 고른 자세를 사실이 내린 자리. **내렸다는 사실 자체를 남긴다.**
    clamped_reason_codes: list[str] = Field(default_factory=list)

    def of(self, strategy: str) -> StrategyProfile | None:
        return next((p for p in self.profiles if p.strategy == strategy), None)


@dataclass(frozen=True)
class StrategySignals:
    """전략을 고르는 데 쓰는 **사실**. 전부 남이 보낸 값이다.

    ★ 여기서 정책을 만들지 않는다. 임계값도 점수도 없다 — 물류가 *"위험하다"* 고
      적은 코드, 재무가 *"압박이 높다"* 고 적은 라벨을 그대로 읽는다.
    """

    #: 소진 압력. **물류가 낸 신호가 하나라도 있으면 참이다.**
    depletion_pressure: bool = False
    #: 그 판단의 근거가 된 코드·로트. 화면과 이력이 *"왜"* 를 말할 수 있게 남긴다.
    freshness_risk_codes: tuple[str, ...] = ()
    freshness_risk_lot_ids: tuple[str, ...] = ()
    #: 되먹임 회신에서 온 보조 신호 (1차 생성에는 없다).
    sell_priority: str | None = None
    inventory_risk_severity: str | None = None
    remaining_freshness_days: int | None = None
    #: 재무 선행 사실. **`None` 은 못 받았다는 뜻이다** — 0 과 다르다.
    payment_pressure: str | None = None
    credit_available_krw: Decimal | None = None
    credit_limit_known: bool = False
    has_finance_context: bool = False
    #: ML 밴드를 실제로 쓸 수 있는가 (`use_recommended` · `target_kind` 게이트).
    ml_gate_open: bool = False

    @property
    def cash_is_tight(self) -> bool:
        return self.payment_pressure in _TIGHT_CASH

    @property
    def credit_is_exhausted(self) -> bool:
        """여신 여력이 남지 않았는가. **모르면 참이 아니다** (fail-open 이 아니다).

        ★ 모르는 것을 *"여력 없음"* 으로 읽으면 한도가 안 선 거래처가 전부 막힌다.
          한도가 없다는 사실은 `credit_limit_known` 이 따로 나른다.
        """
        return self.credit_available_krw is not None and self.credit_available_krw <= 0


# ---------------------------------------------------------------------------
# ① 사실 — 이미 받은 것만 읽는다
# ---------------------------------------------------------------------------


def _warning_codes(context: Any) -> tuple[str, ...]:
    """`soft_warnings[].code` 를 꺼낸다. **물류가 적은 코드 그대로다.**

    ★ 모양이 다르면 읽지 않는다. 물류 `PRE_SALES` 는 `{"code": ...}` 로 보내고
      (`logistics/adapter.py`), 그 약속을 벗어난 항목에서 사실을 만들지 않는다.
    """
    if context is None:
        return ()
    codes: list[str] = []
    for warning in context.soft_warnings:
        raw = warning.model_dump() if hasattr(warning, "model_dump") else warning
        code = raw.get("code") if isinstance(raw, Mapping) else None
        if isinstance(code, str):
            codes.append(code)
    return tuple(dict.fromkeys(codes))


def _freshness_risk_lots(context: Any, item: str) -> tuple[str, ...]:
    """유효 신선도 한계에 닿은 로트. **물류가 낸 두 숫자를 비교만 한다.**

    🔴 **새 임계값을 만들지 않는다.** 물류가 `remaining_freshness_days` 와 그것을
      만든 분모(`effective_freshness_limit_days`)를 같이 보내는 이유가 이것이다 —
      *"며칠 남았으면 위험"* 을 판매가 정하면 같은 판정의 주인이 둘이 된다.

    ★ **합산하지 않는다.** 로트를 더해 가용량을 만들면 판매가 물류의 가용 판정을
      다시 하는 것이 된다 (`_confirmed_sellable_qty` 가 못박은 자리). 여기서는
      **이름만** 센다.
    """
    if context is None or context.sellable_supply is None:
        return ()
    lots: list[str] = []
    for lot in context.sellable_supply.lot_constraints:
        if lot.item != item:
            continue
        remaining = lot.remaining_freshness_days
        limit = lot.effective_freshness_limit_days
        if remaining is None or limit is None:
            continue
        if remaining <= limit:
            lots.append(lot.lot_id)
    return tuple(dict.fromkeys(lots))


def _finance_number(context: Any, path: Sequence[str]) -> Decimal | None:
    """재무 선행 사실 하나. **없으면 `None` 이고 0 으로 바꾸지 않는다.**"""
    current: Any = context.model_dump() if hasattr(context, "model_dump") else context
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    if isinstance(current, bool) or not isinstance(current, (int, float, Decimal)):
        return None
    return Decimal(str(current))


def _finance_label(context: Any, name: str) -> str | None:
    raw = context.model_dump() if hasattr(context, "model_dump") else context
    value = raw.get(name) if isinstance(raw, Mapping) else None
    return value if isinstance(value, str) else None


def derive_signals(request: Any, replies: Sequence[Any] = ()) -> StrategySignals:
    """전략이 볼 사실을 모은다. **1차 입력이 정본이고 되먹임은 보조다.**

    ```text
    logistics_context   1차 호출부터 있다      ← 소진 신호의 정본
    domain_replies      되먹임 회차에만 있다    ← 있으면 더 강한 신호로 얹는다
    finance_context     1차 호출부터 있다      ← 2026-09-16 에 연결됐다
    ```
    """
    logistics = request.logistics_context
    item = request.user_request.item
    codes = _warning_codes(logistics)
    risk_codes = tuple(code for code in codes if code == FRESHNESS_RISK_CODE)
    risk_lots = _freshness_risk_lots(logistics, item)

    sell_priority, severity, freshness = _reply_ranking_facts(replies)
    depletion = bool(risk_codes or risk_lots)
    depletion = depletion or sell_priority == _HIGH_SELL_PRIORITY
    depletion = depletion or severity in _SEVERE_INVENTORY_RISK

    finance = request.finance_context
    forecast = request.ml_context
    return StrategySignals(
        depletion_pressure=depletion,
        freshness_risk_codes=risk_codes,
        freshness_risk_lot_ids=risk_lots,
        sell_priority=sell_priority,
        inventory_risk_severity=severity,
        remaining_freshness_days=freshness,
        payment_pressure=None if finance is None else _finance_label(finance, "payment_pressure"),
        credit_available_krw=(
            None
            if finance is None
            else _finance_number(finance, ("partner_credit", "partner_credit_available_krw"))
        ),
        credit_limit_known=(
            finance is not None
            and _finance_number(finance, ("partner_credit", "partner_credit_limit_krw")) is not None
        ),
        has_finance_context=finance is not None,
        ml_gate_open=(
            forecast is not None
            and forecast.use_recommended is True
            and forecast.target_kind == "WHSL"
        ),
    )


def _reply_ranking_facts(replies: Sequence[Any]) -> tuple[str | None, str | None, int | None]:
    """되먹임 물류 회신의 보조 사실. **없으면 전부 `None` 이다.**"""
    for reply in replies:
        if getattr(reply, "source_agent", None) != "logistics":
            continue
        payload = getattr(reply, "payload", {}) or {}
        priority = payload.get("sell_priority")
        severity = payload.get("inventory_risk_severity")
        freshness = payload.get("remaining_freshness_days")
        return (
            priority if isinstance(priority, str) else None,
            severity if isinstance(severity, str) else None,
            freshness if isinstance(freshness, int) and not isinstance(freshness, bool) else None,
        )
    return None, None, None


# ---------------------------------------------------------------------------
# ② 템플릿 — 모델이 없어도 세 전략은 선다
# ---------------------------------------------------------------------------


def template_profiles(signals: StrategySignals) -> list[StrategyProfile]:
    """규칙만으로 만든 세 자세. **모델이 실패해도 판매는 돈다** (§10).

    ★ 공격안의 가격 자세는 **신호가 있을 때만** `DEPLETION` 이다. 신호 없이 시장
      하단을 여는 것은 근거 없이 싸게 파는 것이다.
    """
    aggressive_price: PricePosture = (
        "DEPLETION" if signals.depletion_pressure else "MARKET_ALIGNED"
    )
    aggressive_reasons = list(signals.freshness_risk_codes)
    if signals.freshness_risk_lot_ids:
        aggressive_reasons.append("LOT_FRESHNESS_LIMIT_REACHED")
    if not signals.depletion_pressure:
        aggressive_reasons.append("DEPLETION_SIGNAL_ABSENT")

    conservative_reasons: list[str] = []
    if signals.cash_is_tight:
        conservative_reasons.append("CASH_PRESSURE")
    if signals.credit_is_exhausted:
        conservative_reasons.append("CREDIT_EXHAUSTED")
    if not signals.has_finance_context:
        conservative_reasons.append("FINANCE_CONTEXT_ABSENT")

    return [
        StrategyProfile(
            strategy="CONSERVATIVE",
            price_posture="MARGIN_DEFENSE",
            quantity_posture="LIMITED",
            inventory_posture="NORMAL",
            credit_posture="STRICT",
            cash_posture="DEFENSIVE" if signals.cash_is_tight else "NORMAL",
            reason_codes=conservative_reasons,
        ),
        StrategyProfile(
            strategy="BALANCED",
            price_posture="MARKET_ALIGNED",
            quantity_posture="NORMAL",
            inventory_posture="FIFO",
            credit_posture="NORMAL",
            cash_posture="NORMAL",
            reason_codes=["ML_BAND_AVAILABLE"] if signals.ml_gate_open else ["ML_BAND_GATED"],
        ),
        StrategyProfile(
            strategy="AGGRESSIVE",
            price_posture=aggressive_price,
            quantity_posture="EXPANDED",
            inventory_posture=(
                "FRESHNESS_RISK_FIRST" if signals.depletion_pressure else "FIFO"
            ),
            credit_posture="WITHIN_LIMIT",
            cash_posture="CASH_CONVERSION" if signals.cash_is_tight else "NORMAL",
            reason_codes=aggressive_reasons,
        ),
    ]


# ---------------------------------------------------------------------------
# ③ 사실이 자세를 이긴다
# ---------------------------------------------------------------------------


def clamp_profiles(
    profiles: Sequence[StrategyProfile], signals: StrategySignals
) -> tuple[list[StrategyProfile], list[str]]:
    """모델이 고른 자세를 **사실로 깎는다. 깎았다는 사실을 남긴다.**

    🔴 **소진 자세는 신호가 있어야 선다.** 없으면 시장 정합으로 내린다 — 모델이
      *"싸게 팔자"* 를 스스로 여는 길을 막는 자리다.

    ★ **깎기는 한 방향이다.** 모델이 더 보수적인 자세를 골랐으면 그대로 둔다 —
      사실이 허락한다고 공격을 강요하지 않는다.
    """
    clamped: list[StrategyProfile] = []
    notes: list[str] = []
    for profile in profiles:
        if profile.price_posture == "DEPLETION" and not signals.depletion_pressure:
            notes.append(f"{profile.strategy}:DEPLETION_SIGNAL_ABSENT")
            profile = profile.model_copy(
                update={
                    "price_posture": "MARKET_ALIGNED",
                    "reason_codes": [*profile.reason_codes, "DEPLETION_SIGNAL_ABSENT"],
                }
            )
        if profile.inventory_posture == "FRESHNESS_RISK_FIRST" and not signals.depletion_pressure:
            notes.append(f"{profile.strategy}:FRESHNESS_RISK_ABSENT")
            profile = profile.model_copy(update={"inventory_posture": "FIFO"})
        clamped.append(profile)
    return clamped, notes


def plan_strategies(
    request: Any, replies: Sequence[Any] = ()
) -> tuple[StrategyPlan, StrategySignals]:
    """세 전략의 자세를 정한다. **모델이 실패해도 세 자세는 선다.**

    ★ 모델 호출은 `app.sales.llm.runtime` 이 소유한다 — 이 파일은 **무엇을 물을지**와
      **무엇을 받아들일지**만 정한다.
    """
    from app.sales.llm.runtime import plan_strategy_profiles

    signals = derive_signals(request, replies)
    template = template_profiles(signals)
    outcome = plan_strategy_profiles(signals=signals, template=template)
    profiles, notes = clamp_profiles(outcome.profiles, signals)
    return (
        StrategyPlan(
            source=outcome.source,
            llm_status=outcome.llm_status,
            profiles=profiles,
            llm_provider=outcome.llm_provider,
            llm_model=outcome.llm_model,
            clamped_reason_codes=notes,
        ),
        signals,
    )
