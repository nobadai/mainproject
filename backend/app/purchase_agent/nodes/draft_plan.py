"""③ draft_plan — 커버일수 D 기반 수량 초안 (상세설계 §4-③).

수량은 전부 계산이 소유한다 (규칙 6). LLM 몫은 "하드 제약 안에서 어떤 조합이 나은가"라는
트레이드오프 판단이며 Epic 3에서 붙는다 — 그때도 아래 클립 결과를 **입력**으로 받는다.
"""

from typing import Any

from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes._guards import (
    require_capacity_kg,
    require_non_empty,
    require_positive,
)
from app.purchase_agent.nodes.classify_situation import (
    coverage_by_label,
    estimate_daily_demand,
)
from app.purchase_agent.quotes import quote_block_reason
from app.purchase_agent.schemas import FIXED_MARKET
from app.purchase_agent.state import PurchaseAgentState


def fixed_market_quotes(market_quotes: list[dict]) -> list[dict]:
    """가락 시세만 남긴다.

    ``market``을 버리고 등급·가격만 보면 **다른 시장의 가격을 가락 가격으로 둔갑**시킬 수
    있다. 지금 mock은 전부 가락이라 결과가 같지만, 필터가 없으면 그 사실에 기대는 코드가 된다.
    """
    return require_non_empty(
        [quote for quote in market_quotes if quote["market"] == FIXED_MARKET],
        f"market_quotes[{FIXED_MARKET}]",
    )


def reference_unit_price(market_quotes: list[dict], reference_grade: str) -> int:
    """기준 등급의 당일 가락 시세. 없으면 가장 비싼 등급으로 보수적으로 잡는다.

    ⑤도 이 등급을 배분의 기준으로 삼는다 — 등급을 constraints에서 한 번만 읽어 양쪽에
    넘긴다 (규칙 7). ⑤가 더 싼 중품을 섞으면 실제 매입단가는 이 값보다 낮아지므로, 여기서
    낸 현금 상한은 **보수적인 쪽으로만** 어긋난다. 반대로 이 등급을 ⑤보다 싸게 잡으면
    ③이 살 수 있다고 계산한 양을 ⑦의 금액 검사가 컷하게 된다.

    🔴 **여기의 ``max`` 는 그대로 둔다. 이 자리에서는 참이기 때문이다** (`#574` ·
      2026-09-11). ⑤의 같은 식은 **고쳤다** — 두 파일이 같은 모양을 들고 뜻이 갈렸으니
      다음에 여는 사람이 *"저쪽은 고쳤는데 여기는?"* 을 다시 재지 않도록 이유를 적는다.

    ```text
    여기 (③)   고른 값이 쓰이는 곳은 ``cash_cap_kg(budget, unit_price)`` **하나**다
                단가 ↑ → 현금 상한 ↓ → 수량 ↓          🟢 높게 잡는 것이 보수적이다
                🔴 반대로 낮게 잡으면 못 살 양을 살 수 있다고 계산한다 — 그게 위험한 방향

    ⑤         고른 값은 **무엇을 살 것인가**다 (``base_grade``)
                비싸게 고르면 돈을 더 쓰고, 사다리를 안 보면 **낮은 등급을 비싸게 산다**
                🔴 실측: 2026-01-22 양파에서 「하」 1,100원이 「특」 972원을 이겨 선택됐다
    ```

    ★★ **갈림은 「기준등급이 시세에 있느냐」가 아니다** — 있든 없든, *"추정 단가"* 자리면
      높게 잡는 것이 보수적이고 *"배정 등급"* 자리면 높은 값이 보수와 무관하다.

    ⚠️ **그래서 두 자리를 같은 값으로 묶는 검사를 두지 않는다.** 묶으면 한쪽을 고칠 때
      다른 쪽이 따라가야 하는 것처럼 보이는데, 지금은 **뜻이 달라서 값이 달라도 맞다.**
      대신 ``test_grade_fallback`` 이 *"두 자리가 갈렸다"* 를 명시적으로 잠근다.
    """
    prices = {quote["grade"]: quote["price"] for quote in fixed_market_quotes(market_quotes)}
    chosen = prices.get(reference_grade, max(prices.values()))
    return require_positive(chosen, "reference_unit_price")


def warehouse_cap_kg(inventory: dict) -> int:
    """창고 여유 + 외부임차 한도. 상세설계 §4-⑦의 수량 하드 상한이다.

    ⚠️ §4-⑦은 이 검사를 ``check_warehouse_capacity()`` **공용 모듈**로 두고 매입·T3·Critic이
    import하라고 규정한다("자체 구현 금지 — 매입 통과, T3 FAIL 반복 방지"). 그 모듈이 아직
    없어서 지금은 여기 있다. 생기면 이 함수를 지우고 import로 바꾼다.

    ★ **내림한다.** 물류가 보내는 창고 여유는 소수다(실측 7,636.72kg). 이 값은 **상한**이라
      올리면 못 넣는 양을 계획하게 된다 — 7,637kg을 사면 0.28kg이 갈 곳이 없다. 수량 상한을
      ``min()``으로 클립하는 것과 같은 보수 방향이다.

      ⚠️ mock이 우연히 정수라(12,000 + 3,600) 이 자리가 오래 드러나지 않았다. 선언은
      ``-> int``인데 실제로는 float을 그대로 돌려주고 있었고, 실연동에서 ``total_qty_kg``가
      소수가 되어 출력 스키마 검증이 막았다 (2026-08-28 통합 실행).

    🔴 **두 값의 타입을 강제한다.** 물류가 준 수량이라는 점에서 로트 ``shelf_life_days``와
      같은 종류인데 그쪽만 막혀 있었다 — *"한쪽만 지킨"* 상태다 (2026-08-31 확인). 실측:

          True          → 창고 상한 1kg. 전 안이 창고에 눌려 죽는데 원인이 안 보인다
          '1000' · [1]  → 더하는 자리에서 TypeError. 어느 키가 문제인지 안 남는다
          -500          → 상한이 음수. 수량이 음수로 클립된다
          NaN           → int() 변환에서 ValueError
          Decimal + float → TypeError (물류는 float이지만 출처가 하나가 아니다)

      수신 payload는 ``adapter.validate_payload``가 같은 검사를 먼저 해 ``missing_data``로
      **사유를 내고 멈춘다** — 여기까지 오지 않는다.
    """
    return int(
        require_capacity_kg(inventory["warehouse_free_kg"], "warehouse_free_kg")
        + require_capacity_kg(inventory["rental_cap_kg"], "rental_cap_kg")
    )


def purchase_budget_krw(state: PurchaseAgentState, constraints: dict) -> float:
    """이 사이클에 쓸 수 있는 **매입 가능액(원)**. 경로가 둘이다 (IO명세 §2-B · B6).

    1. **재무 cap을 받은 경우** (어댑터 경로) — 그 값이 곧 상한이다. 여기에
       ``max_purchase_ratio``를 곱하지 않는다. 재무가 이미 *"이만큼까지"*를 계산해
       보낸 값이라, 같은 목적으로 한 번 더 조이면 상한이 두 겹이 되고
       **"왜 이만큼밖에 못 사나"의 근거가 흐려진다** (재무 회신 v2.2.1 — "같은 목적
       60% 재적용 금지").
    2. **못 받은 경우** (mock 경로) — 종전대로 ``base_projected_cash_min × 비율``.

    ⚠️ 2번이 죽은 경로가 아니다. 어댑터를 거치지 않는 ``run_purchase_agent`` 직접 호출이
    그 길로 가고, 회귀 테스트 전량이 매일 그 길을 밟는다. 다만 **실운영에서는 재무 경계
    미수신이 곧 ``RUNTIME_NOT_READY``라**(M-1 제출 §4) 1번만 돈다.
    """
    cap = state.get("finance_cap_amount_krw")
    if cap is not None:
        return float(cap)
    return state["projected_cash_min"] * constraints["cash"]["max_purchase_ratio"]


def cash_cap_kg(budget_krw: float, unit_price: int) -> int:
    """매입 가능액을 수량으로 환산. ``budget ÷ 단가``."""
    budget = budget_krw
    return int(budget // require_positive(unit_price, "unit_price"))


#: 못 쓰는 조정안의 사유. **화면과 Critic 이 읽는다** — 내부 이름을 쓰지 않는다.
_UNUSABLE_UNKNOWN_AXIS = "매입이 반영할 수 있는 조정 항목이 아니다"
_UNUSABLE_WRONG_UNIT = "{axis} 조정은 {expected} 단위로 와야 하는데 {unit} 로 왔다"
_UNUSABLE_NO_TARGET_SCENARIO = "어느 안에 적용할지가 적혀 있지 않다"


def split_adjustments(
    adjustments: list[dict] | None, constraints: dict
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """조정안을 **쓸 수 있는 것 / 못 쓰는 것(사유)** 으로 가른다.

    🔴 **왜 거르는 층이 따로 있나.** 계약(``contracts.core.SuggestedAdjustment``)의
      ``unit`` 은 자유 문자열이라 **축과 안 맞아도 봉투가 안 막는다.** 마스터 IO
      Contract 가 *"받는 쪽이 risks 로 걸러야 합니다"* 로 넘긴 자리다.

      실측(2026-09-09 · ``master_agent_runs``)에 ``axis=amount`` 인데 ``unit=kg`` ·
      ``target_value=900`` 인 조정안이 6건 있다. 그것을 원으로 알고 환산하면
      ``900 ÷ 단가 = 0kg`` 이고, ③이 ``min()`` 으로 클립하므로 **매입량이 0 으로
      눌린다. 아무도 안 운다.**

    ★ **버리지 않는다.** 못 쓰는 것도 사유와 함께 돌려주고 ⑥이 고지한다 — 값을 받고
      조용히 버리면 보내는 쪽은 자기 제안이 반영된 줄 안다 (``#165`` · ``#166`` 에서
      우리가 남에게 지적한 것과 같은 자리다).

    ★ **항목·단위 짝은 선언이 소유한다** (``constraints.feedback``). 여기 박으면
      선언을 바꿔도 판정이 안 따라오고, 그러면 "설정에서 읽는다" 를 증명할 수 없다
      (규칙 7·8).

    ⚠️ ``scenario_labels`` 가 빈 것도 못 쓰는 쪽이다. 계약이 *"안 채운 것과 해당 없는
      것을 여기서 가르지 않는다"* 라 **어느 안인지 모른다** — 모르는 채로 전 안을
      조이면 근거 없이 조이는 것이다 (규칙 3).
    """
    units = {
        row["axis"]: row["unit"] for row in constraints["feedback"]["applicable_axis_units"]
    }
    usable: list[dict] = []
    unusable: list[tuple[dict, str]] = []
    for item in adjustments or []:
        axis = item.get("axis")
        expected = units.get(axis)
        if expected is None:
            unusable.append((item, _UNUSABLE_UNKNOWN_AXIS))
            continue
        unit = item.get("unit")
        if unit != expected:
            reason = _UNUSABLE_WRONG_UNIT.format(axis=axis, expected=expected, unit=unit)
            unusable.append((item, reason))
            continue
        if not item.get("scenario_labels"):
            unusable.append((item, _UNUSABLE_NO_TARGET_SCENARIO))
            continue
        usable.append(item)
    return usable, unusable


def _freshness_cap_kg(
    state: PurchaseAgentState, daily_demand: float, constraints: dict
) -> int | None:
    """보관한계 안에 소진 가능한 양. 품목 보관한계가 미확정이면 **None**을 돌려준다.

    None은 "제약 없음"이 아니라 **계산을 하지 않았다**는 뜻이다 (규칙 3). 0으로 채우면
    매입량이 0으로 눌리고, 큰 수로 채우면 검사가 있었던 것처럼 보인다 — 둘 다 거짓이다.
    호출자는 None을 받으면 클립하지 않고 그 사실을 risks에 남긴다.
    """
    shelf_life_days = constraints["shelf_life_days"].get(state["item"])
    if shelf_life_days is None:
        return None
    return int(daily_demand * shelf_life_days)


#: 조정안 상한이 클립했을 때 ``clipped_by`` 에 남는 이름. **화면이 그대로 읽는다.**
ADJUSTMENT_CAP_NAME = "조정안"


def usable_holdings_kg(lots: list[dict] | None, daily_demand: float, days: int) -> float:
    """커버 창 ``days`` 안에서 **실제로 쓸 수 있는 보유**. 상세설계 §4-③.

    ```text
    usable = Σ_lot  min( available_qty_kg, 일평균 × min(remaining_freshness_days, days) )
    차감보유 = min(usable, 일평균 × days)
    ```

    🔴 **이 값은 ``caps`` 가 아니다.** 창고·현금·신선도·조정안은 밖에서 씌운 천장이고,
      보유는 **필요가 줄어든 것**이다. ``caps`` 에 넣으면 ``clipped_by`` 에 「보유」가 실리고
      ⑥이 *"하드 제약(보유)으로 수량이 0까지 축소"* 를 낸다 — 거짓 문장이다. 그래서
      호출자는 이 값을 **원수요에서 뺀다**.

    ★ **로트마다 잔여신선도를 본다.** 남은 신선도가 ``days`` 보다 짧은 로트는 그 창을
      **끝까지 못 덮는데** 단순 합계는 덮는다고 센다. 실측(완주 걷기 162안)에서 단순
      합계와 이 식의 차감액이 142안에서 다르다 — 판정은 아직 한 건도 안 갈렸지만,
      데이터가 안 가르면 **규칙의 뜻으로** 정한다 (`#574` 와 같은 자리).

    🟢 **이미 팔린 몫을 두 번 세지 않는다** — 물류가 만드는 ``available_qty_kg`` 는 기존
      할당을 **뺀** 값이다 (``logistics/fefo_allocation``). 판매도 같은 칸에 서 있어
      (`#567`), 물류가 그 뜻을 바꾸면 두 파트가 같이 틀린다 — 우리 detail 이 아니다.

    ⚠️ **두 칸 중 하나라도 ``None`` 인 로트는 건너뛴다** (규칙 3). 0으로 채우면
      *"쓸 수 있는 게 없다"* 가 되어 안 깎이고, 큰 수로 채우면 없는 재고를 뺀다. 모르는
      것은 세지 않는다 — 차감을 **적게 잡는 쪽**으로만 어긋난다.

    🔴 **만료 로트(잔여신선도 음수)는 0일치로 센다** (2026-09-11 · `#584` 회귀 수정).

      ``min(freshness, days)`` 만 쓰면 음수 신선도가 ``daily × 음수`` 로 들어가
      **차감이 음수**가 되고, 호출자가 ``원수요 − 차감`` 을 하므로 **원수요보다 더 사게
      된다.** 조항이 막으려던 것과 정확히 반대다::

          만료 하나 섞인 로트 셋   차감 **−16,435kg**  →  사는 양 3,587 → **20,022kg**

      ★ **이것은 규칙 3 위반이 아니다.** ``None`` 은 *"며칠 버티는지 모른다"* 라 건너뛰고,
        음수는 *"이미 지났다"* 는 **확정된 답**이다 — 그 로트가 덮는 창은 **0일**이다.
        값을 지어내는 것이 아니라 아는 값을 그대로 쓰는 것이다.

      ⚠️ 바깥 ``max(0.0, ...)`` 도 같이 둔다. 안쪽만 막으면 ``available_qty_kg`` 가
        음수로 오는 날 같은 부호 뒤집힘이 다시 난다 — 지금 원장엔 0건이지만, **한 번
        뒤집히면 화면에 「창고 제약」으로 보이고 아무도 못 찾는다.**

      🔴 **원장에 이미 있다** — 로트 26,967건 중 잔여신선도 음수 **18,944건**,
        차감이 음수가 되는 셀 **1,390 / 1,701**.

      ⚠️ **클램프가 생기면서 위 ``None`` 건너뛰기는 검사로 못 가른다** — 0으로 채워도
        ``covered_days`` 가 0이라 결과가 같다(변이로 확인: 안 물린다). 가르는 변이는
        *"큰 수로 채운다"* 쪽 하나다. **건너뛰기는 그대로 둔다** — 클램프를 걷는 날
        다시 유일한 방어가 되고, 뜻이 다른 둘을 같은 줄로 합치지 않는다.
    """
    if not lots:
        return 0.0
    usable = 0.0
    for lot in lots:
        available = lot.get("available_qty_kg")
        freshness = lot.get("remaining_freshness_days")
        if available is None or freshness is None:
            continue
        covered_days = max(0, min(int(freshness), days))
        usable += min(float(available), daily_demand * covered_days)
    return max(0.0, min(usable, daily_demand * days))


def adjustment_cap_kg(usable: list[dict], label: str, unit_price: int) -> int | None:
    """이 안에 걸리는 조정안 상한을 **kg 으로**. 걸리는 것이 없으면 ``None``.

    ``target_value`` 는 **넘지 말아야 할 값**이다 — 목표가 아니다 (마스터 IO Contract
    §4.4 확정 · *"quantity·amount 는 그 값 이하"*). 그래서 지시값이 아니라 상한이고,
    ③은 이미 ``min([raw_qty, *caps])`` 구조라 **칸 하나가 늘 뿐**이다.

    ⚠️ **원 → kg 환산에 새 산식을 만들지 않는다.** ``cash_cap_kg`` 와 같은 나눗셈이라
      따로 쓰면 두 곳이 갈린다 — 재무 상한과 조정안 상한이 다른 단가로 환산되면
      *"왜 이만큼밖에 못 사나"* 가 두 답을 갖는다.

    ⚠️ **여럿이면 가장 낮은 것을 쓴다.** 상한이 여러 개면 전부 지켜야 하고, 그건
      ``min`` 이다. 실측상 한 회차에 같은 안을 겨냥한 조정안이 여러 건 온다.

    ★ ``label`` 에 안 걸린 조정안은 여기서 조용히 빠진다 — 어느 안에 거는지는
      ``scenario_labels`` 가 말하고, 비어 있는 것은 ``split_adjustments`` 가 이미
      «못 씀» 으로 걸러 여기 오지 않는다.
    """
    caps = [
        cash_cap_kg(float(item["target_value"]), unit_price)
        for item in usable
        if label in (item.get("scenario_labels") or ())
    ]
    return min(caps) if caps else None


def draft_plan(state: PurchaseAgentState) -> dict[str, Any]:
    """안별 수량 초안을 만든다.

    ``수량 = 일평균 확정수요 × 커버일수 D`` 를 계산하고 하드 제약으로 클립한다.
    uncertain이면 공격(D=12)을 아예 만들지 않는다 (§4-③ · 규칙 4).
    """
    constraints = load_constraints()
    coverage = constraints["coverage_days"]
    daily_demand = estimate_daily_demand(state["confirmed_orders"], constraints)
    # 구간이 넓은 날엔 공격안을 만들지 않는다 — **그 규칙은 ①과 공유한다**
    # (`coverage_by_label` · `#340`). 전에는 여기서만 걸러서, ①이 축을 열 때는
    # 공격안이 있는 것처럼 D=12 로 재고 있었다.
    labels = list(coverage_by_label(state["situation"], constraints))
    blocked = quote_block_reason(state["market_quotes"], state["item"], state["date"], constraints)
    if blocked:
        return _no_quote_plan(state, constraints, daily_demand, labels, blocked)

    reference_grade = constraints["allocation"]["reference_grade"]
    unit_price = reference_unit_price(state["market_quotes"], reference_grade)

    warehouse_cap = warehouse_cap_kg(state["inventory"])
    cash_cap = cash_cap_kg(purchase_budget_krw(state, constraints), unit_price)
    freshness_cap = _freshness_cap_kg(state, daily_demand, constraints)
    # 🔴 **조정안 상한은 안마다 다르다** (2026-09-09 · E3-6). 위 셋은 그날 하나인데
    #   조정안은 ``scenario_labels`` 로 «이 안» 을 겨냥한다 — 재무가 상한 2,000만에
    #   기본·공격만 넘겼으면 보수는 안 건드려야 한다.
    usable, _ = split_adjustments(state.get("adjustments"), constraints)

    drafts = [
        _draft_one(
            label=label,
            days=coverage["by_label"][label],
            daily_demand=daily_demand,
            caps={
                "창고": warehouse_cap,
                "현금": cash_cap,
                "신선도": freshness_cap,
                ADJUSTMENT_CAP_NAME: adjustment_cap_kg(usable, label, unit_price),
            },
            coverage=coverage,
            # 🔴 **품목이 걸러진 로트다.** ``absorb_inventory`` 가 다른 품목을 이미 뺐다 —
            #   안 거르면 배추 보유로 무 수요를 깎는다.
            lots=state["inventory"].get("lots"),
        )
        for label in labels
    ]

    return {
        "coverage_days": coverage["by_label"]["기본"],  # §3 State는 대표 D 하나를 담는다
        "base_plan": {
            "daily_demand_kg": daily_demand,
            "reference_unit_price": unit_price,
            "drafts": drafts,
            "deferred_checks": _deferred_checks(
                state,
                constraints,
                freshness_cap,
                state["item"],
                # 차감이 **실제로 걸린 날**에만 리드타임 고지를 얹는다. 안 깎인 날에
                # 그 문장을 내면 "없는 일에 사과하는" 고지가 된다.
                deducted=any(draft["deducted_holdings_kg"] > 0 for draft in drafts),
            ),
        },
    }


def _no_quote_plan(
    state: PurchaseAgentState,
    constraints: dict,
    daily_demand: float,
    labels: list[str],
    reason: str,
) -> dict[str, Any]:
    """시세를 쓸 수 없는 날 — **죽지 않고 사유를 남기고 0안으로 끝낸다**.

    막히는 경우가 둘이다: 한 건도 못 받았거나(휴장·미거래·판독불가·규격 미확정),
    받았는데 **너무 오래된 값**이거나. 둘 다 "오늘 시세를 모른다"는 같은 상태이고,
    사유 문장만 다르다.

    ``reference_unit_price``는 ``require_non_empty``로 멈춘다. 그 가드의 뜻은 "빈 값으로
    조용히 계산하지 말라"이지 "죽어라"가 아니다 — 여기서 죽으면 오케스트레이터는 예외만
    받고 **왜 안이 없는지를 모른다**. 그래서 계산을 안 하는 것은 그대로 두고, 사유를 낼 수
    있는 이 자리에서 먼저 낸다.

    ⚠️ 실데이터 경로에서만 도달한다. mock 은 어느 앵커·품목에서도 빈 시세를 돌려주지
      않는다(모르는 품목이면 멈춘다) — 계약 테스트가 그 전제를 잠근다.

    ``reference_unit_price``를 **0이 아니라 None**으로 둔다 (규칙 3). 0으로 채우면
    ``cash_cap_kg``가 0으로 나누고, 그 전에 "단가 0원"이라는 없는 사실이 만들어진다.
    """
    return {
        "coverage_days": constraints["coverage_days"]["by_label"]["기본"],
        "base_plan": {
            "daily_demand_kg": daily_demand,
            "reference_unit_price": None,
            "drafts": [],
            # 시세를 모르는 날은 안이 0개라 차감 자체가 없다 — 고지할 것도 없다.
            "deferred_checks": _deferred_checks(
                state, constraints, None, state["item"], deducted=False
            ),
        },
        # 라벨마다 한 줄씩 남긴다 — 소비자가 "보수는 왜 없나"를 안별로 묻기 때문이고,
        # ⑦의 no_proposal_reason도 이 목록을 이어 붙여 만든다.
        #
        # ★ **앞의 것을 이어 붙인다.** 노드가 돌려주는 값이 State의 같은 키를 통째로
        #   대체하므로, 새 목록만 반환하면 앞 노드가 쌓은 사유가 사라진다. 지금은 ③이
        #   이 키를 처음 쓰는 노드라 결과가 같지만, ②가 사유를 남기게 되는 날 조용히
        #   지워진다 — ⑥이 ``[*state[...], *dropped]``로 쓰는 것과 같은 이유다.
        "rejected_reasons": [
            *state["rejected_reasons"],
            *({"label": label, "reason": reason} for label in labels),
        ],
    }


def _draft_one(
    *,
    label: str,
    days: int,
    daily_demand: float,
    caps: dict,
    coverage: dict,
    lots: list[dict] | None = None,
) -> dict[str, Any]:
    """안 하나. 클립이 걸리면 어느 제약이 몇 kg으로 눌렀는지 남긴다.

    수량이 나는 순서가 **뜻이다** (상세설계 §4-③).

    ```text
    demand_qty_kg          round(일평균 × D)               원수요
    deducted_holdings_kg   커버 창 안에서 쓸 수 있는 보유    ← 수요에서 뺀다
    raw_qty_kg             max(0, 원수요 − 차감)           **여기까지가 필요량**
    total_qty_kg           min([raw_qty_kg, *caps])        창고·현금·신선도·조정안 클립
    ```

    🔴 **``raw_qty_kg`` 는 차감 뒤 값이다.** ``clipped_by`` 와 ⑥의 축소 문장이
      *"그 상한이 실제로 깎은 양"* 을 말해야 하기 때문이다 — 원수요를 대면 창고가
      255kg 깎은 날에 「창고가 2,787kg 깎았다」가 된다. 원수요를 보려면
      ``demand_qty_kg`` 를 읽는다.

    ★ 보유가 원수요를 다 덮어 ``raw_qty_kg`` 가 0이 되는 것은 **막힌 것이 아니라 필요
      없는 것**이다. ⑥이 그 둘을 ``kind`` 로 가른다 — 여기서는 값만 낸다.
    """
    if not coverage["min"] <= days <= coverage["max"]:
        span = f"[{coverage['min']}, {coverage['max']}]"
        raise ValueError(f"coverage_days {days} for {label!r} is outside {span}")

    demand_qty = round(daily_demand * days)
    deducted = usable_holdings_kg(lots, daily_demand, days)
    raw_qty = max(0, demand_qty - round(deducted))
    binding = [(name, cap) for name, cap in caps.items() if cap is not None and cap < raw_qty]
    total_qty = min([raw_qty, *(cap for _, cap in binding)])
    return {
        "label": label,
        "coverage_days": days,
        "demand_qty_kg": demand_qty,
        "deducted_holdings_kg": round(deducted),
        "raw_qty_kg": raw_qty,
        "total_qty_kg": total_qty,
        "clipped_by": [
            {"constraint": name, "cap_kg": cap, "raw_qty_kg": raw_qty} for name, cap in binding
        ],
    }


def pending_value(state: PurchaseAgentState, constraints: dict, name: str) -> int | None:
    """미결 파라미터의 현재 값. **수신값이 설정값을 이긴다.**

    ``constraints.yaml``의 ``pending``은 "아직 아무도 안 줬다"는 기본값이고, 어댑터가
    재무 payload에서 받아 실으면 그 값이 정답이다. 두 곳을 각자 읽으면 한쪽만 바뀐다.

    ``or``를 쓰지 않는다 — 0은 확정된 0이라 폴백 대상이 아니다 (규칙 3).
    """
    received = state.get(name)  # type: ignore[call-overload]  # NotRequired 키
    if received is not None:
        return received
    return constraints["pending"][name]


def _deferred_checks(
    state: PurchaseAgentState,
    constraints: dict,
    freshness_cap: int | None,
    item: str,
    *,
    deducted: bool = False,
) -> list[str]:
    """미결값 때문에 **계산하지 않은** 검사들. ⑥이 안별 risks에 싣는다.

    ``rejected_reasons``가 아니라 risks로 가는 이유: 소비자는 rejected_reasons를 "컷된 안의
    이력"으로 읽는다. "검사를 건너뛰었다"는 다른 의미라 그 필드에 섞으면 계약이 오염된다.

    ``deducted``는 그날 보유 차감이 실제로 걸렸는지다 (상세설계 §4-③-4). 걸렸는데 입고
    소요일이 미결이면 **보유가 덮는 창과 매입이 덮는 창이 같은지 못 맞춘다** — 차감은
    하고 그 사실을 남긴다 (규칙 3 · 0으로 채우지 않는다).
    """
    deferred = []
    if pending_value(state, constraints, "inbound_lead_days") is None:
        deferred.append(
            "입고일 기준 창고 점유 검사 보류 — 물류 입고 소요일이 미확정이라 "
            "회차별 도착일을 계산하지 않는다"
        )
        if deducted:
            deferred.append(
                "보유 재고를 뺀 창과 매입이 덮는 창이 맞는지 확인 보류 — 물류 입고 "
                "소요일이 미확정이라 매입분 도착일을 놓지 못한다"
            )
    if pending_value(state, constraints, "purchase_payment_days") is None:
        deferred.append(
            "지급일 기준 현금 검사 보류 — 재무 대금 지급 소요일이 미확정이라 "
            "회차별 지급일을 계산하지 않는다"
        )
    if freshness_cap is None:
        deferred.append(f"신선도 상한 검사 보류 — {item} 품목 보관한계가 설정에 미확정")
    return deferred
