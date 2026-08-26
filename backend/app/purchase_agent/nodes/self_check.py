"""⑦ self_check — 사중 일치 검사 + 컷 사유 기록 + 출력 조립 (상세설계 §4-⑦).

여기서 컷된 안은 ``rejected_reasons``에 ``{label, reason}``으로 남는다. 마지막에
``revalidate_for_output()``으로 계약을 한 번 더 확인한 뒤에야 출력이 만들어진다.

**환각 대조(LLM)는 Epic 3다.** 지금 있는 건 전부 계산 검사다 — 규칙 6대로 숫자·제약은
순수 함수가 소유하고, LLM은 rationale의 claim이 원본과 맞는지 보는 데만 쓴다.
"""

from typing import Any

from app.purchase_agent import AGENT_VERSION
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.draft_plan import warehouse_cap_kg
from app.purchase_agent.schemas import FIXED_MARKET, PurchaseProposal, revalidate_for_output
from app.purchase_agent.state import PurchaseAgentState


def check_quadruple_match(scenario: dict) -> str | None:
    """사중 일치 — 수량 3축 + 금액 1축 (현서님 검토 §3-2).

    금액 축이 재무 cap 대조값이고, 등급 배분이 수량↔금액 변환 계수라 빠뜨릴 수 없다.
    스키마도 같은 검사를 하지만 여기서 먼저 본다 — 스키마는 예외를 던져 제안 전체를
    죽이고, 여기서는 **그 안만 컷하고 나머지를 살린다**.
    """
    total = scenario["total_qty_kg"]
    split_total = sum(item["qty_kg"] for item in scenario["split_plan"])
    sourcing_total = sum(item["qty_kg"] for item in scenario["sourcing_plan"])
    if total != split_total:
        return f"수량 불일치: total {total:,} != split 합 {split_total:,}"
    if total != sourcing_total:
        return f"수량 불일치: total {total:,} != sourcing 합 {sourcing_total:,}"
    amount = sum(line["qty_kg"] * line["grade_unit_price"] for line in scenario["sourcing_plan"])
    if scenario["total_amount_krw"] != amount:
        return f"금액 불일치: total {scenario['total_amount_krw']:,} != sourcing 합 {amount:,}"
    return None


def check_axis_allowed(scenario: dict, allowed_axes: list[str]) -> str | None:
    """그날 허용 축 안의 값인가 (규칙 4 · 정의서 §3.5.1-2)."""
    if scenario["strategy_type"] not in allowed_axes:
        return f"허용되지 않은 축: {scenario['strategy_type']} (그날 허용 {allowed_axes})"
    return None


def check_prices_exist(scenario: dict, market_quotes: list[dict]) -> str | None:
    """등급·단가가 **당일 시세에 실재**하는가 (규칙 4). 지어낸 단가를 막는 유일한 검사다.

    **시장까지 함께 본다.** ``(등급, 가격)``만 대조하면 다른 시장의 가격에 ``market="가락"``을
    붙여도 통과한다 — 형식은 맞고 내용은 거짓인 출력이 만들어진다.
    """
    real = {(quote["market"], quote["grade"], quote["price"]) for quote in market_quotes}
    for line in scenario["sourcing_plan"]:
        if line["market"] != FIXED_MARKET:
            return f"허용되지 않은 시장: {line['market']} ({FIXED_MARKET} 고정)"
        key = (line["market"], line["grade"], line["grade_unit_price"])
        if key not in real:
            return (
                f"당일 시세에 없는 단가: {line['market']} {line['grade']} "
                f"{line['grade_unit_price']:,}원 (실재 {sorted(real)})"
            )
    return None


def check_max_price(scenario: dict) -> str | None:
    """매입단가가 max_price를 넘는가.

    max_price는 예측 q90 기반 **하드 상한**이다 (§4-⑦). 계약단가(contract_price) 초과와
    혼동하지 않는다 — 그쪽은 컷이 아니라 margin_warning 표시일 뿐이다 (규칙 5).
    """
    ceiling = scenario["max_price"]
    over = [line for line in scenario["sourcing_plan"] if line["grade_unit_price"] > ceiling]
    if over:
        worst = max(line["grade_unit_price"] for line in over)
        return f"매입단가 {worst:,}원이 max_price {scenario['max_price']:,}원 초과"
    return None


def check_warehouse_capacity(scenario: dict, inventory: dict) -> str | None:
    """총수량 ≤ 창고 여유 + 외부임차 한도.

    ⚠️ **공용화 지점.** §4-⑦은 이 검사를 공용 모듈의 ``check_warehouse_capacity()``로 두고
    매입·T3·Critic이 import하라고 규정한다 — "자체 구현 금지, 매입 통과·T3 FAIL 반복 방지".
    그 모듈이 아직 없어 여기 있다. 생기면 이 함수를 지우고 import로 바꾼다 (현서님 협의 항목).
    상한 식 자체는 ③과 공유한다(``warehouse_cap_kg``) — 두 곳에 복제하면 한쪽만 바뀐다.

    ⚠️ 날짜별 검사(``cap_by_date``)는 하지 않는다. 입고일 = 회차 date + N4인데 N4가 NULL이라
    계산 자체를 막는다 (규칙 3 · §1.2-10). 그 사실은 안의 risks에 실린다.
    """
    cap = warehouse_cap_kg(inventory)
    if scenario["total_qty_kg"] > cap:
        return f"창고 초과: {scenario['total_qty_kg']:,}kg > 여유+임차 {cap:,}kg"
    return None


def check_cash_ceiling(scenario: dict, state: PurchaseAgentState, constraints: dict) -> str | None:
    """매입액 ≤ 향후 최저 현금 × 비율."""
    budget = state["projected_cash_min"] * constraints["cash"]["max_purchase_ratio"]
    if scenario["total_amount_krw"] > budget:
        return f"현금 초과: {scenario['total_amount_krw']:,}원 > 매입 가능액 {budget:,.0f}원"
    return None


def check_split_dates(scenario: dict, as_of: str) -> str | None:
    """seq 1의 date는 as_of, seq는 1부터 연속 (IO명세 §2)."""
    rounds = scenario["split_plan"]
    if rounds[0]["date"] != as_of:
        return f"1회차 날짜가 as_of와 다름: {rounds[0]['date']} != {as_of}"
    if [item["seq"] for item in rounds] != list(range(1, len(rounds) + 1)):
        return f"회차 번호가 연속이 아님: {[item['seq'] for item in rounds]}"
    return None


def check_axis_diversity(scenarios: list[dict], allowed_axes: list[str]) -> str | None:
    """전 안이 동일 축이면 반려 (정의서 §3.5.1-3 — "코드가 최종 중복 검사 ... (self_check)").

    **축이 하나뿐인 날은 면제한다.** 하락일처럼 timing 트리거가 미달하고 mix가 편중으로
    게이팅되면 남는 축이 quantity뿐이라, 전 안이 같은 축인 게 정상이다. 그날까지 반려하면
    제안 자체가 불가능해진다.

    "같은 안의 크기 변주"를 잡는 장치는 따로 있다 — §7의 ``variant_collapsed``(안 간 수량 차가
    15% 미만)이고, 그 판정은 T3 몫이다.

    이 검사가 스키마가 아니라 여기 있는 이유: ``allowed_axes``는 ①이 그날 계산한 런타임
    값이라 출력 JSON만 보고는 알 수 없다.
    """
    if len(allowed_axes) < 2 or len(scenarios) < 2:
        return None
    axes = {scenario["strategy_type"] for scenario in scenarios}
    if len(axes) == 1:
        return (
            f"전 안이 동일 축({axes.pop()})인데 그날 허용 축은 {allowed_axes}로 여럿이다 — "
            "선택지가 아니라 같은 안의 크기 변주다"
        )
    return None


def self_check(state: PurchaseAgentState) -> dict[str, Any]:
    """안별 검사로 컷하고, 살아남은 것으로 제안을 조립해 계약을 재확인한다."""
    constraints = load_constraints()
    survivors: list[dict] = []
    rejected: list[dict] = list(state["rejected_reasons"])

    for scenario in state["scenarios_final"]:
        reason = (
            check_quadruple_match(scenario)
            or check_axis_allowed(scenario, state["allowed_axes"])
            or check_prices_exist(scenario, state["market_quotes"])
            or check_max_price(scenario)
            or check_warehouse_capacity(scenario, state["inventory"])
            or check_cash_ceiling(scenario, state, constraints)
            or check_split_dates(scenario, state["date"])
        )
        if reason:
            rejected.append({"label": scenario["label"], "reason": reason})
        else:
            survivors.append(scenario)

    diversity = check_axis_diversity(survivors, state["allowed_axes"])
    if diversity:
        rejected.extend({"label": s["label"], "reason": diversity} for s in survivors)
        survivors = []

    proposal = _assemble(state, survivors, rejected)
    return {"scenarios_final": survivors, "rejected_reasons": rejected, "proposal": proposal}


def _assemble(state: PurchaseAgentState, survivors: list[dict], rejected: list[dict]) -> dict:
    """제안 JSON을 만들고 **출력 경계에서 계약을 재확인**한다.

    ``revalidate_for_output()``은 Epic 1에서 만들어둔 함수다 — 원시 데이터에서 모델을 다시
    세워, 조립 과정에서 어긋난 값이 그대로 나가는 걸 막는다. 여기서 ``ValidationError``가
    나면 **직렬화하지 않고 터진다**. 계약 위반은 사업적 결과가 아니라 버그이므로 조용히
    빈 제안으로 바꾸지 않는다.
    """
    feedback = state["feedback"] or {}
    raw = {
        "meta": {
            "as_of": state["date"],
            "item": state["item"],
            "agent_version": AGENT_VERSION,
            "is_refeed": bool(state["feedback"]),
            "feedback_attempt": feedback.get("attempt", 0),
        },
        "scenarios": survivors,
        "confidence": state["confidence"],
        "situation": state["situation"],
        "context_docs_used": [f"DOC-{doc['doc_id']}" for doc in state["context_docs"]],
        "rejected_reasons": rejected,
    }
    if not survivors:
        raw["no_proposal_reason"] = (
            "모든 안이 self_check에서 컷됨: "
            + "; ".join(f"{item['label']}({item['reason']})" for item in rejected)
        )
    return revalidate_for_output(PurchaseProposal.model_validate(raw)).model_dump(mode="json")
