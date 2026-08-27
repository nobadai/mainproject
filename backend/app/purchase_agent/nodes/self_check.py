"""⑦ self_check — 사중 일치 검사 + 컷 사유 기록 + 출력 조립 (상세설계 §4-⑦).

여기서 컷된 안은 ``rejected_reasons``에 ``{label, reason}``으로 남는다. 마지막에
``revalidate_for_output()``으로 계약을 한 번 더 확인한 뒤에야 출력이 만들어진다.

**환각 대조(LLM)는 Epic 3다.** 지금 있는 건 전부 계산 검사다 — 규칙 6대로 숫자·제약은
순수 함수가 소유하고, LLM은 rationale의 claim이 원본과 맞는지 보는 데만 쓴다.
"""

from itertools import pairwise
from typing import Any

from app.purchase_agent import AGENT_VERSION
from app.purchase_agent.config import load_constraints
from app.purchase_agent.nodes.collect_context import TRUNCATION_MARK
from app.purchase_agent.nodes.draft_plan import purchase_budget_krw, warehouse_cap_kg
from app.purchase_agent.schemas import (
    DOCUMENT_SOURCE,
    FIXED_MARKET,
    PurchaseProposal,
    document_ref,
    is_document_ref,
    revalidate_for_output,
)
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
    """매입액 ≤ 매입 가능액.

    상한을 ③과 **같은 함수**(``purchase_budget_krw``)로 구한다. 두 노드가 각자 계산하면
    재무 cap 수신 여부에 따라 한쪽만 바뀌고, ③이 통과시킨 안을 ⑦이 컷하는 상태가 된다.
    """
    budget = purchase_budget_krw(state, constraints)
    if scenario["total_amount_krw"] > budget:
        return f"현금 초과: {scenario['total_amount_krw']:,}원 > 매입 가능액 {budget:,.0f}원"
    return None


def check_split_dates(scenario: dict, as_of: str) -> str | None:
    """seq 1의 date는 as_of, seq는 1부터 연속, **날짜는 앞으로만 간다** (IO명세 §2).

    날짜 검사는 ④가 실제로 분할하면서 붙었다. 회차가 하나뿐이던 동안에는 순서를 어길
    방법이 없었지만, 2회차가 생기면 "같은 날 두 번"이 통과할 수 있다 — 그건 분할이 아니라
    같은 매입을 두 줄로 적은 것이고, 사중 일치는 멀쩡히 통과한다.

    ISO 날짜 문자열은 사전순 비교가 곧 시간순 비교라 그대로 비교한다.
    스키마도 같은 검사를 하지만 거기서는 제안 전체가 죽고, 여기서는 **그 안만 컷한다**.
    """
    rounds = scenario["split_plan"]
    if rounds[0]["date"] != as_of:
        return f"1회차 날짜가 as_of와 다름: {rounds[0]['date']} != {as_of}"
    if [item["seq"] for item in rounds] != list(range(1, len(rounds) + 1)):
        return f"회차 번호가 연속이 아님: {[item['seq'] for item in rounds]}"
    dates = [item["date"] for item in rounds]
    if any(earlier >= later for earlier, later in pairwise(dates)):
        return f"회차 날짜가 앞으로 가지 않음: {dates}"
    return None


def check_document_refs(scenario: dict, context_docs: list[dict]) -> str | None:
    """인용한 문서가 **실제로 읽은 것인가** (§4-⑦ 근거 환각 대조 중 계산으로 되는 부분).

    ``check_prices_exist``가 단가에 대해 하는 일을 문서에 대해 한다 — 지어낸 ``DOC-``을
    막는 유일한 검사다. 등급·단가는 당일 시세에 실재해야 하고, 문서 근거는 그날 ②가
    실제로 로드한 것이어야 한다.

    **역방향은 검사하지 않는다.** "읽었는데 근거에 안 썼다"는 위반이 아니다 — ② 스텁
    시절부터 그 상태를 **의도적으로 구분 가능하게** 두었다("문서를 읽었는데 근거에 안 썼다"와
    "아직 안 읽는다"가 출력에서 구분된다). 컷해버리면 그 구분이 사라지고, ``context_docs_used``와
    rationale이 항상 같아져 두 필드 중 하나가 무의미해진다.

    **``source``와 ``ref_id`` 접두어 중 하나만 봐서는 안 된다.** 처음엔 ``source == "문서ID"``만
    봤는데, Codex 교차검증이 P1을 짚었다 — 출처를 "예측"으로 적고 ``ref_id``에 ``"DOC-999"``를
    넣으면 검사를 통째로 빠져나가고 스키마도 둘의 정합을 요구하지 않는다. 재현해 확인했다.
    그래서 **둘 중 하나라도 문서를 가리키면** 문서 근거로 보고, 이어서 **둘이 어긋난 것 자체**를
    막는다. 어긋난 항목은 어느 검사에도 안 걸리는 사각지대였다.
    """
    loaded = {document_ref(doc["doc_id"]) for doc in context_docs}
    cited: set[str] = set()
    for item in scenario["rationale"]:
        by_source = item["source"] == DOCUMENT_SOURCE
        by_ref = is_document_ref(item["ref_id"])
        if by_source != by_ref:
            return (
                f"문서 근거 표기 불일치: source={item['source']!r} / ref_id={item['ref_id']!r} — "
                f"문서 참조는 source가 {DOCUMENT_SOURCE!r}이고 ref_id가 DOC- 로 시작해야 한다"
            )
        if by_source:
            cited.add(item["ref_id"])
    unknown = sorted(cited - loaded)
    if unknown:
        return f"읽지 않은 문서를 인용: {unknown} (그날 로드분 {sorted(loaded)})"
    return None


def check_excerpt_fidelity(scenario: dict, context_docs: list[dict]) -> str | None:
    """인용 발췌가 **원문에 실재하는 문자열인가** (§4-⑦ 근거 환각 대조 · 현서님 2차 합의 8/26).

    현서님 회신(8/26)으로 역할 경계가 정해졌다: **문서 원문 대조는 매입 소유다.** Critic은
    월보·기상 문서를 스냅샷으로 받지 않기로 해(8/25 B4-8) 원문 접근권이 없고, 접근권 없는
    쪽은 이 검사를 구조적으로 수행할 수 없다. 대체 불가능하므로 여기가 유일한 자리다.

    **오늘은 이 검사가 통과할 수밖에 없다.** ②의 발췌가 서두 잘라내기라 원문 문자가 변조될
    길이 없기 때문이다. 그래도 지금 넣는 이유는, ②·⑥에 LLM이 붙어 "구절 선별"과 "claim
    요약"이 생기는 순간(E3-5 이후) **이 검사가 없으면 환각이 그대로 출력에 실리기 때문**이다.
    검사를 나중에 만들면 그 사이 산출물은 검증된 적이 없는 채로 남는다.

    두 지점을 본다 — ②가 원문에서 떴는가, ⑥이 그걸 그대로 실었는가:

    1. ``excerpt`` ⊆ ``content``  — ②의 발췌가 원문에서 온 문자열인가
    2. ``excerpt`` ⊆ ``evidence_detail`` — ⑥이 옮기며 고치지 않았는가

    **``substring``이지 ``prefix``가 아니다.** 지금 발췌는 항상 서두라 접두어 검사로도
    통과하지만, LLM이 구절을 고르기 시작하면 발췌는 본문 중간에서 온다. 오늘의 구현이
    아니라 **계약이 약속하는 것**에 맞춰 검사한다.

    절단 표시는 떼고 맞춘다 — ``…``는 ②가 붙인 표식이지 원문 문자가 아니다.
    접미사를 보고 판별하지 않고 ``excerpt_truncated`` 플래그를 쓰는 이유는
    ``leading_excerpt`` docstring에 있다.
    """
    by_ref = {document_ref(doc["doc_id"]): doc for doc in context_docs}
    for item in scenario["rationale"]:
        if item["source"] != DOCUMENT_SOURCE:
            continue
        doc = by_ref.get(item["ref_id"])
        if doc is None:
            # 검사 순서상 check_document_refs가 먼저 잡는 상태다. 순서에 기대지 않고
            # 여기서도 막는다 — 체인 순서가 바뀌면 조용히 통과하는 자리가 된다.
            return f"인용한 문서를 찾을 수 없어 발췌 대조 불가: {item['ref_id']}"
        excerpt = doc["excerpt"]
        if doc.get("excerpt_truncated"):
            excerpt = excerpt.removesuffix(TRUNCATION_MARK)
        if excerpt not in doc["content"]:
            return f"발췌가 원문에 없다: {item['ref_id']} — 인용 {excerpt[:40]!r}"
        if excerpt not in item["evidence_detail"]:
            return (
                f"근거 문구가 로드한 발췌와 다르다: {item['ref_id']} — "
                f"발췌 {excerpt[:40]!r}가 evidence_detail에 없다"
            )
    return None


def check_document_publication(
    scenario: dict, context_docs: list[dict], as_of: str
) -> str | None:
    """인용 문서가 **as_of 시점에 실재하는 발행물인가** (규칙 1 look-ahead 방어).

    포트가 이미 ``published_at <= as_of``로 거른다(``mocks._load.filter_by_published_at``).
    그런데도 출력 경계에서 한 번 더 보는 이유는, **필터를 통과하는 것과 출력이 지키는 것이
    다른 약속**이기 때문이다 — 스키마의 ``DOC-`` backstop을 ⑦ 검사와 별개로 둔 것과 같은
    자리다. 9/4에 9/5 발행 문서(DOC-6)를 인용하면 백테스트 성적이 무효가 되는데, 그건
    사업적 판단이 아니라 버그이므로 컷 사유로 남긴다.

    ``published_at``이 **없는** 것도 위반이다. 없으면 비교 자체가 불가능한데 조용히
    넘기면 "검사했다"가 거짓이 된다 — 로더가 ``published_at`` 없는 문서를 적재 거부하는
    것과 같은 이유다 (IO명세 §1-⑥).

    ISO 날짜 문자열은 사전순 비교가 곧 시간순이라 ``date`` 변환 없이 비교한다.
    """
    by_ref = {document_ref(doc["doc_id"]): doc for doc in context_docs}
    for item in scenario["rationale"]:
        if item["source"] != DOCUMENT_SOURCE:
            continue
        doc = by_ref.get(item["ref_id"])
        if doc is None:
            return f"인용한 문서를 찾을 수 없어 발행일 대조 불가: {item['ref_id']}"
        published_at = doc.get("published_at")
        if not published_at:
            return f"발행일 없는 문서를 인용: {item['ref_id']} — as_of 대조가 불가능하다"
        if published_at > as_of:
            return (
                f"as_of 이후 발행 문서를 인용: {item['ref_id']} "
                f"(발행 {published_at} > as_of {as_of}) — look-ahead"
            )
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
            or check_document_refs(scenario, state["context_docs"])
            # 문서 검사 3종은 순서가 있다: 인용이 로드분인가(refs) → 발행일이 as_of 이전인가
            # (publication) → 발췌가 원문 문자인가(fidelity). 뒤 두 검사는 앞이 통과했다고
            # 가정하지 않고 각자 문서를 다시 찾는다.
            or check_document_publication(scenario, state["context_docs"], state["date"])
            or check_excerpt_fidelity(scenario, state["context_docs"])
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
        "context_docs_used": [document_ref(doc["doc_id"]) for doc in state["context_docs"]],
        "rejected_reasons": rejected,
    }
    if not survivors:
        raw["no_proposal_reason"] = (
            "모든 안이 self_check에서 컷됨: "
            + "; ".join(f"{item['label']}({item['reason']})" for item in rejected)
        )
    return revalidate_for_output(PurchaseProposal.model_validate(raw)).model_dump(mode="json")
