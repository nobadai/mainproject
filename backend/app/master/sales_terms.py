"""자동 걷기의 판매 요청에 **상업 조건을 싣는다** (2026-09-11).

```text
규칙 파일에 sales_terms 가 있으면   → 그 값을 판매 요청에 싣는다
없으면                              → 종전 그대로. 아무것도 안 싣는다
요청이 이미 값을 들고 있으면         → 🟢 그 값이 이긴다. 규칙이 못 덮는다
```

## 무엇이 막고 있었나

걷기가 판매 판정까지 갔고 **재무 검증에서 멈췄다** (실측 · `SL6_VALIDATION_UNRESOLVED`).

```text
재무 SALES_VALIDATION  status "INPUT_INCOMPLETE" · reason "SALES_INPUT_INCOMPLETE"
missing_fields  partner_id · unit_price_krw · reported_sales_amount_krw
                payment_terms_type · source_ref
```

★★ **수량 하나만 물류에서 왔고 상업 조건은 아무도 안 정했다.** 자동 걷기에는 사람이
  없다 — 화면에서 오는 `SalesRunRequest` 는 사람이 조건을 채우지만, 하루 순서가
  스스로 부르는 요청에는 채울 사람이 없었다.

## 🔴 값은 전부 규칙 파일에서 온다

거래처 id 도 지급조건 이름도 일수도 **이 파일에 없다.** 있는 것은 *"규칙이 말하면
싣는다"* 는 **모양**뿐이다.

★ `--opening-usage-scope` 를 문에 안 박은 것과 같은 이유다. 거래처도 지급조건도
  **업무의 값**이고, 코드에 박으면 바꾸는 날 diff 가 아니라 **배포**가 된다.

## 🔴 단가 — 마스터가 시세를 계산하지 않는다

```text
ML_CURRENT_PRICE   ML 이 그날 예측과 같은 행에 동봉한 current_price 를 **그대로 옮긴다**
FIXED              규칙 파일이 적은 고정값을 쓴다
```

⚠️ **어느 쪽을 썼는지가 `source_ref` 에 드러난다.** 안 드러나면 나중에 곡선을 읽는
  사람이 *"이 단가가 시세였나 고정값이었나"* 를 못 되짚는다.

## 🔴 `reported_sales_amount_krw` 는 여기서 안 만든다

재무가 요구한 다섯 중 그것만 **파생**이다 — 수량 × 단가이고, **판매가 자기 안에서
센다** (`app/sales` 의 계약). 마스터가 같이 실으면 같은 사실의 주인이 둘이 되고,
판매가 반올림을 바꾸는 날 두 숫자가 조용히 갈린다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from app.master.backfill import (
    FIXED_UNIT_PRICE,
    ML_CURRENT_PRICE,
    BackfillRuleMissing,
    BackfillRules,
    SalesTermsRule,
    read_run_rules,
)
from app.master.inputs import SourcedInput, load_forecast
from app.master.schemas import SalesRunRequest

__all__ = ["apply_sales_terms", "read_run_sales_terms", "rules_source_ref"]

logger = logging.getLogger(__name__)

#: ML 예측 봉투에서 시세가 앉는 칸. ⚠️ **ML 의 칸 이름이다** — 마스터가 짓지 않았다.
_CURRENT_PRICE_KEY = "current_price"


def read_run_sales_terms(
    sim_run_id: str, *, rules_fn: Callable[[str], BackfillRules] = read_run_rules
) -> SalesTermsRule | None:
    """그 실행이 정한 상업 조건. **없으면 `None` 이고 그것이 정상이다.**

    🔴 **부르는 자리는 걷기와 깨어남 둘뿐이다** (`backtest_runner.walk` ·
      `scheduler.wake_up`). 하루 실행은 **읽지 않고 받는다** — 하루가 제 손으로
      설정을 읽으면 검사마다 실 DB 를 치고, 그 사실이 조용히 새는 것을
      `test_db_isolation` 이 잡아 온 바로 그 모양이 된다.

    🔴 **실행당 한 번이다.** 규칙은 실행에 속하지 날에 속하지 않는다 — 날마다
      읽으면 같은 설정을 179번 다시 읽고, 그러다 하루만 다른 조건으로 도는 날이
      오면 왜 그런지를 설정만 보고는 못 읽는다.

    ⚠️ **못 읽었다고 세우지 않는다.** 조건 없이 도는 것은 종전 동작이고, 그 사실은
      로그에 남는다 (`BackfillRuleMissing` 은 *"규칙 파일 없이 연 실행"* 이라는
      기본값이라 로그도 안 남긴다 — 매번 남기면 진짜 사유가 안 읽힌다).
    """
    try:
        return rules_fn(sim_run_id).sales_terms
    except BackfillRuleMissing:
        return None
    except Exception:
        logger.exception("판매 상업조건을 못 읽었다 - 조건 없이 간다")
        return None


def rules_source_ref(sim_run_id: str, unit_price_source: str) -> str:
    """*"이 조건은 어느 실행의 규칙에서 왔고 단가는 어디서 왔나"* 한 줄.

    🔴 **사람이 말한 것처럼 보이면 안 된다.** 자동 걷기의 조건은 사람이 화면에 친
      문장이 아니라 `sim_runs.config_json` 의 규칙이고, 그 행은 되짚을 수 있다.

    ★ **모양의 주인은 `procurement_boundary._source_ref` 다** — `표/id` 로 적는다.
      새 모양을 만들면 같은 프로젝트 안에서 ref 가 두 벌이 된다.

    ⚠️ **단가 출처를 같이 적는다.** 안 적으면 같은 규칙 파일로 돈 두 실행이
      *"시세로 팔았나 고정값으로 팔았나"* 에서 구별되지 않는다.
    """
    return f"sim_runs/{sim_run_id}#sales_terms/{unit_price_source}"


def apply_sales_terms(
    request: SalesRunRequest,
    rule: SalesTermsRule | None,
    *,
    forecast_fn: Callable[[str, date], SourcedInput] = load_forecast,
) -> SalesRunRequest:
    """규칙이 말한 상업 조건을 요청에 **얹는다.**

    🔴 **규칙이 없으면 요청을 그대로 돌려준다.** 코드에 기본값을 두어 *"늘 실리게"*
      하지 않는다 — 그러면 규칙을 안 적은 사람도 **모르는 거래처에 팔게 된다.**

    🟢 **이미 들어 있는 값은 안 덮는다.** 사람이 준 조건이 규칙보다 세다. 덮으면
      화면에서 거래처를 고른 사람이 **자기가 안 고른 거래처로 판다.**

    ⚠️ **단가는 못 실을 수 있다.** `ML_CURRENT_PRICE` 인데 그날 예측이 없으면 안
      싣고, 그러면 재무가 *"단가가 없다"* 로 판정을 못 낸다 — **옛 배치로 메우지
      않는다.** 없는 것과 메운 것을 가르는 것이 §1.2-10 이다.

    :param forecast_fn: ML 예측을 읽는 자리. 🔴 **기본이 `inputs.load_forecast` 다** —
        *"`as_of` 당일 배치만 쓴다"* 는 판정의 주인이 거기 하나이고, 여기서
        `generated_at` 을 다시 보면 판정이 두 곳에 생긴다.
    """
    if rule is None:
        return request
    filled: dict[str, object] = {}
    if request.partner_id is None:
        filled["partner_id"] = rule.partner_id
    if request.preferred_payment_terms_type is None:
        filled["preferred_payment_terms_type"] = rule.payment_terms_type
    if request.preferred_payment_days is None:
        filled["preferred_payment_days"] = rule.payment_days
    if request.preferred_unit_price_krw is None:
        price = _unit_price(rule, request, forecast_fn=forecast_fn)
        if price is not None:
            filled["preferred_unit_price_krw"] = price
    if request.source_ref is None:
        filled["source_ref"] = rules_source_ref(
            _sim_run_id_of(request), rule.unit_price_source
        )
    return request.model_copy(update=filled) if filled else request


def _sim_run_id_of(request: SalesRunRequest) -> str:
    """조건이 온 실행. ⚠️ **없으면 빈 문자열이 아니라 그 사실을 적는다.**

    ★ 하루 순서는 이 칸을 늘 채워 보낸다 (`scheduler` 가 걷기 축을 잇는다). 비어
      있다는 것은 **다른 자리에서 불렀다**는 뜻이고, `sim_runs/` 로 적으면 없는
      행을 가리키게 된다.
    """
    return request.sim_run_id or "-"


def _unit_price(
    rule: SalesTermsRule,
    request: SalesRunRequest,
    *,
    forecast_fn: Callable[[str, date], SourcedInput],
) -> Decimal | None:
    """규칙이 말한 단가. **없으면 `None` — 지어내지 않는다.**"""
    if rule.unit_price_source == FIXED_UNIT_PRICE:
        return rule.unit_price_krw
    if rule.unit_price_source != ML_CURRENT_PRICE:
        # ★ 여기 올 수 없다 — `backfill._read_sales_terms` 가 아는 둘만 통과시킨다.
        #   그래도 조용히 고정값으로 접지 않는다.
        logger.warning("모르는 단가 출처다 - 단가를 안 싣는다: %s", rule.unit_price_source)
        return None
    if not request.item:
        # ★ 예측은 품목별이라 물을 대상이 없다 (`service._sales_forecast` 와 같은 자리).
        return None
    return _forecast_price(forecast_fn, request.item, request.as_of)


def _forecast_price(
    forecast_fn: Callable[[str, date], SourcedInput], item: str, as_of: date
) -> Decimal | None:
    """그날 ML 시세 하나. **못 읽어도 하루를 안 세운다.**

    ★ `service._sales_forecast` 와 같은 태도다 — ML DB 가 죽었다고 판매가 통째로
      못 도는 것은 아니다. 대신 조용히 넘어가지도 않는다.
    """
    try:
        forecast = forecast_fn(item, as_of)
    except Exception:
        logger.exception("단가로 쓸 ML 시세 조회 실패 - 단가를 안 싣는다")
        return None
    if not forecast.usable or not isinstance(forecast.payload, Mapping):
        return None
    raw = forecast.payload.get(_CURRENT_PRICE_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int | float | str | Decimal):
        return None
    try:
        price = Decimal(str(raw))
    except InvalidOperation:
        logger.warning("ML 시세를 숫자로 못 읽는다 - 단가를 안 싣는다: %r", raw)
        return None
    # 🔴 **0 이하를 그대로 싣지 않는다.** `SalesRunRequest` 가 `ge=0` 이라 음수는
    #   문 앞에서 터지고, 그러면 ML 한 행이 그날 판매를 통째로 세운다.
    return price if price > 0 else None
