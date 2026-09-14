"""⑧ review_rationale — **근거를 스스로 한 번 더 읽는다** (E3-10).

🔴 **⑦ 뒤에 선다.** 계산 검사가 끝나고 살아남은 안만 본다. ⑦ 안에 섞지 않는 이유는
경계 때문이다 — ⑦ 은 컷 권한이 있고 여기는 없다. 컷하는 함수 안에 컷 못 하는 판단을
두면 나중에 누가 「이것도 컷하면 되지 않나」로 읽는다.

🔴 **수량·분할·등급·금액·날짜·컷 결과를 안 바꾼다.** 더하는 것은 안의 ``risks`` 한두 줄뿐이다.
   ⚠️ 그래서 ``risks`` 와 산출물 해시는 **의도적으로 달라진다** — 「아무것도 안 바뀐다」가
   아니다. ``risks`` 는 ``Scenario`` 의 필드이므로 그 둘이 동시에 참일 수 없다.

🔴 **실패·비활성이면 제안이 그대로 지나간다.** 지적 0건과 **검토를 못 한 것**은 다른
   사실이고, 그 구분은 실행 흔적(``llm_calls``)이 든다.

★ 판단자에게 넣는 문장은 **숫자·날짜를 가린 것**이다. 가리기 전 원문이 새면 판단자가 그
  값을 지적에 베껴 쓰고, 그 순간 규칙 6 이 깨진다 — 노드가 넣기 직전에 다시 확인한다.
"""

from typing import Any

from app.master.envelope import LLMCallMetadata
from app.purchase_agent.features import SELF_REVIEW, enabled
from app.purchase_agent.llm.mix import context_labels
from app.purchase_agent.llm.review_schemas import ClaimIn, ReviewContext
from app.purchase_agent.llm.self_review import ROLE, Reviewer
from app.purchase_agent.llm.text_guard import contains_number, sanitize_numerals
from app.purchase_agent.review_gate import GateResult, ScenarioSignals, choose
from app.purchase_agent.review_templates import FINDINGS, UnknownFinding, render
from app.purchase_agent.schemas import PurchaseProposal, revalidate_for_output
from app.purchase_agent.state import PurchaseAgentState

#: 주장이 얼마나 센가 — 🔴 **규칙이 어휘로 판정한다.** 판단자에게 매기게 하면 그 판정을
#: 근거로 다시 판정하는 셈이 되어, 무엇을 재는지가 사라진다.
#: ⑧ 의 역할 이름. 🔴 어댑터가 같은 이름을 쓰지만 **그쪽을 import 하면 순환**이다
#: (어댑터가 그래프를 부른다). 문자열을 두 곳에 적는 대신, 계약 검사가 둘이 같은지 잠근다.
RATIONALE_SELF_REVIEW = "rationale_self_review"

_ASSERTIVE = ("이다", "확실", "반드시", "전량", "최대")
_HEDGED = ("가능", "예상", "보인다", "추정", "일 수")


def claim_strength(text: str) -> str:
    """문장의 어조. 어휘로만 가른다 — 뜻을 재는 것이 아니다."""
    if any(말 in text for 말 in _HEDGED):
        return "HEDGED"
    if any(말 in text for 말 in _ASSERTIVE):
        return "ASSERTIVE"
    return "NEUTRAL"


def scenario_signals(scenario: dict, drafts: list[dict], mix_applied: bool) -> ScenarioSignals:
    """사전검사 신호를 **구조에서** 뽑는다.

    🔴 문장 찾기로 재는 것은 **지급 집중일 하나뿐**이고, 그 문면은 ⑥ 이 상수로 들고 있다
    (``PAYMENT_CONFLICT_NOTE``) — 같은 말을 두 곳에 적으면 한쪽만 바뀐다.
    """
    from app.purchase_agent.nodes.package_scenarios import PAYMENT_CONFLICT_NOTE

    라벨 = scenario["label"]
    깎였나 = any(
        draft["label"] == 라벨 and draft.get("clipped_by") for draft in drafts
    )
    risks = scenario.get("risks") or []
    return ScenarioSignals(
        label=라벨,
        mix_applied=mix_applied,
        quantity_clipped=깎였나,
        payment_conflict=any(PAYMENT_CONFLICT_NOTE in 줄 for 줄 in risks),
        # timing 라벨인데 회차가 하나 — 라벨과 실체가 다르다
        label_body_mismatch=(
            scenario.get("strategy_type") == "timing"
            and len(scenario.get("split_plan") or []) <= 1
        ),
        only_deferred_risks=bool(risks)
        and all(_보류류(줄) for 줄 in risks),
    )


def _보류류(문장: str) -> bool:
    """*"못 판정했다"* 만 적힌 문장인가 — 검토할 **판단**이 없다는 뜻이다."""
    return any(말 in 문장 for 말 in ("보류", "읽지 못", "미확정", "못 받"))


def mix_reason_for_review(mix: Any) -> str | None:
    """⑤ 가 등급 조합을 고른 사유를 **정제해서** 넘긴다. 안 돌았으면 ``None`` 이다.

    🔴 **⑤ 가 실제로 적용된 날만 넘긴다** (``applied``). 규칙 기본안으로 떨어진 날의
      사유는 판단자가 쓴 문장이 아니라 **코드가 박은 상수**다 — 그것을 넘기면 판단자가
      «자기가 고른 사유» 로 읽고, 안 한 판단을 검토하게 된다.

    🔴 **가린 뒤에도 숫자가 남으면 안 넘긴다.** 못 가린 것을 넣느니 안 본다 — 근거 문장을
      다루는 규율(``build_context``)과 같다.

    ⚠️ **빈 문자열로 안 적는다** (규칙 3 의 문자열 판). 「안 돌았다」와 「사유가 비었다」는
      다른 사실이고, 계약이 그 둘을 ``None`` 과 ``str`` 로 가른다.

    ★ **그날 판단은 하나다.** ⑤ 는 그날 한 번 돌고 ⑥ 이 같은 등급 비율을 **모든 안에**
      곱한다. 그래서 이 사유는 안마다 다르지 않고, 안 루프 **밖에서 한 번** 만든다.
    """
    if mix is None or not getattr(mix, "applied", False):
        return None
    가린 = sanitize_numerals(mix.reason or "")
    if not 가린.strip() or contains_number(가린):
        return None
    return 가린


def build_context(
    scenario: dict,
    signals: ScenarioSignals,
    *,
    mix_reason: str | None = None,
    mix_labels: tuple[str, ...] = (),
) -> ReviewContext:
    """검토 재료. **숫자·날짜를 가려서** 넣는다.

    ⚠️ 가리기 전 원문이 새면 판단자가 그 값을 지적에 베껴 쓴다. 노드가 넣기 직전에 다시
    확인하고, 남아 있으면 그 근거를 **아예 안 넣는다** — 못 가린 것을 넣느니 안 본다.

    ★ ``mix_reason`` · ``mix_labels`` 는 **그날 하나**라 부르는 쪽이 만들어 준다
      (``mix_reason_for_review`` · ``llm.mix.context_labels``).
    """
    claims = []
    for 항목 in scenario.get("rationale") or []:
        가린 = sanitize_numerals(항목.get("claim") or "")
        if not 가린.strip() or contains_number(가린):
            continue
        claims.append(
            ClaimIn(
                ref_id=항목["ref_id"],
                evidence_category=항목["source"],
                evidence_strength=항목.get("evidence_grade") or "ASSUMED",
                claim_strength=claim_strength(가린),
                claim_text=가린,
            )
        )
    return ReviewContext(
        scenario_label=scenario["label"],
        strategy_type=scenario["strategy_type"],
        round_count="MULTI" if len(scenario.get("split_plan") or []) > 1 else "SINGLE",
        claims=claims,
        risk_categories=sorted(
            {_위험범주(줄) for 줄 in (scenario.get("risks") or [])}
        ),
        signals=[이름 for 이름, 켜짐 in _SIGNAL_LABELS(signals) if 켜짐],
        mix_reason=mix_reason,
        mix_labels=list(mix_labels),
        offered_findings=list(FINDINGS),
    )


def _SIGNAL_LABELS(signals: ScenarioSignals) -> list[tuple[str, bool]]:
    return [
        ("MIX_APPLIED", signals.mix_applied),
        ("QUANTITY_CLIPPED", signals.quantity_clipped),
        ("PAYMENT_CONFLICT", signals.payment_conflict),
        ("LABEL_BODY_MISMATCH", signals.label_body_mismatch),
    ]


def _위험범주(문장: str) -> str:
    """위험 문장을 **범주 하나**로 접는다 — 빠진 것을 물으려면 있는 것을 알아야 한다."""
    for 말, 범주 in (
        ("창고", "WAREHOUSE"),
        ("신선도", "FRESHNESS"),
        ("지급", "CASH"),
        ("등급", "GRADE"),
        ("분할", "SPLIT"),
        ("로트", "LOT_AGE"),
    ):
        if 말 in 문장:
            return 범주
    return "OTHER"


def review_rationale(
    state: PurchaseAgentState, *, reviewer: Reviewer | None = None
) -> dict[str, Any]:
    """살아남은 안의 근거를 검토해 **경고만** 더한다.

    🔴 제안을 **다시 계약에 태운다** (``revalidate_for_output``). 리스트에 값을 끼워 넣는
    경로는 어떤 validator 도 안 거치기 때문이고, 그 함수가 바로 그것을 막으려고 이미 있다.
    """
    proposal = state.get("proposal")
    if not proposal or not proposal.get("scenarios"):
        return {}
    if reviewer is None or not enabled(SELF_REVIEW):
        # 🔴 설정이 꺼진 것과 게이트가 안 고른 것은 다른 사실이다 — 안마다 한 줄씩 남긴다.
        return {
            "review_calls": tuple(
                _기록(안["label"], "DISABLED") for 안 in proposal["scenarios"]
            )
        }

    drafts = ((state.get("base_plan") or {}).get("drafts")) or []
    판단 = ((state.get("sourcing_plan") or [{}])[0].get("decision")) or {}
    mix = 판단.get("mix")
    신호 = [
        scenario_signals(안, drafts, bool(mix is not None and mix.applied))
        for 안 in proposal["scenarios"]
    ]
    고른: GateResult = choose(신호)
    기록 = [
        *(_기록(라벨, "SKIPPED_BY_GATE", "사전검사가 대상으로 안 골랐다")
          for 라벨 in 고른.skipped_by_gate),
        *(_기록(라벨, "SKIPPED_BUDGET", "실행당 검토 상한에 걸렸다")
          for 라벨 in 고른.skipped_by_budget),
    ]
    if not 고른.selected:
        return {"review_calls": tuple(기록)}

    # ★ 그날 판단이 하나라 **루프 밖에서** 한 번 만든다 — 안마다 다시 만들면 같은 사유가
    #   안마다 달리 정제될 수 있고, 그러면 같은 판단을 세 번 다르게 검토하게 된다.
    검토용_사유 = mix_reason_for_review(mix)
    # 🔴 **라벨을 같이 넘긴다.** 사유만 주면 「사유가 라벨과 맞나」를 물을 수 없다 —
    #   그 지적이 비교할 대상이 없어진다. 안 돌았으면 빈 목록이다.
    검토용_라벨 = context_labels(판단) if 검토용_사유 is not None else ()
    보임 = {s.label: s for s in 신호}
    바뀐 = [dict(안) for 안 in proposal["scenarios"]]
    for 안 in 바뀐:
        if 안["label"] not in 고른.selected:
            continue
        context = build_context(
            안,
            보임[안["label"]],
            mix_reason=검토용_사유,
            mix_labels=검토용_라벨,
        )
        결과 = reviewer(context)
        상태, 사유, 문장 = 결과.llm_status, None, []
        if 상태 == "SUCCESS":
            try:
                문장 = render(
                    [(f.code, f.target_ref_id) for f in 결과.output.findings],
                    {c.ref_id for c in context.claims},
                )
            except UnknownFinding as error:
                # 🔴 **「검토 성공, 문제 없음」으로 안 적는다.** 지적을 버리고 SUCCESS 를
                #   남기면 화면에는 아무 말이 없고 흔적에는 «봤는데 깨끗했다» 가 남는다 —
                #   실제로는 **검토 결과를 적용하지 못한 것**이다.
                #
                # ⚠️ 예외를 그대로 올리지 않는 이유: ⑦ 이 이미 통과시킨 제안이 ⑧ 때문에
                #   통째로 사라진다. 경고만 하는 자리가 산출물을 죽이면 경계가 뒤집힌다.
                #   ★ 검증(``validate_output``)이 이미 막는 자리라 **여기 오면 내부 계약
                #     위반**이고, 그래서 조용히 넘기지 않고 사유를 남긴다.
                상태, 사유, 문장 = "FALLBACK", f"검토 결과를 문장으로 못 옮겼다 — {error}", []
        기록.append(
            _기록(
                안["label"],
                상태,
                사유,
                attempts=결과.llm_attempts,
                fallback_used=결과.llm_fallback_used or 상태 == "FALLBACK",
                provider=결과.llm_provider,
                model=결과.llm_model,
            )
        )
        if 문장:
            안["risks"] = [*(안.get("risks") or []), *문장]

    새제안 = {**proposal, "scenarios": 바뀐}
    return {
        "proposal": revalidate_for_output(
            PurchaseProposal.model_validate(새제안)
        ).model_dump(mode="json"),
        "review_calls": tuple(기록),
    }


def _기록(
    label: str,
    status: str,
    skip_reason: str | None = None,
    *,
    attempts: int = 0,
    fallback_used: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> LLMCallMetadata:
    """안 하나의 검토 흔적.

    🔴 **안 본 안도 남긴다.** 목록에서 빼면 *"봤는데 깨끗했다"* 와 구분되지 않고,
    검토율이 거짓이 된다.
    """
    # 🔴 **부른 호출에만 판을 적는다.** 안 불렀는데 적으면 *"이 판으로 물어봤다"* 로
    #   읽힌다 — ``LLMCallMetadata`` 가 그 등식을 계약으로 잠근다.
    판 = (
        {"prompt_version": ROLE.prompt_version, "schema_version": ROLE.schema_version}
        if attempts > 0
        else {}
    )
    return LLMCallMetadata(
        role=RATIONALE_SELF_REVIEW,
        status=status,  # type: ignore[arg-type]
        attempts=attempts,
        fallback_used=fallback_used,
        target=label,
        provider=provider or None,
        model=model or None,
        **판,
        skip_reason=skip_reason,
    )
