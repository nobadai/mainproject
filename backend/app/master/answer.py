"""사용자 응답 — **숫자는 규칙이 만들고, 문장만 LLM 이 쓴다.**

마스터 역할 ⑥(사용자 응답 생성)의 규칙 절반이다. LLM 절반은
`app/master/llm/answer_runtime.py` 에 있고, **이 모듈은 LLM 을 모른다.**

```text
StatusOutcome ─┐
               ├→ AnswerFacts (규칙이 만든 사실 줄) ─→ render_answer(facts, 문장) → 텍스트
매입 응답     ─┘                                          ↑
                                                    LLM 이 쓴 앞머리 (없어도 완결된다)
```

★ **이 배치가 안전장치의 전부다.** 매입 ⑤·의도 분류 ①이 쓴 방법과 같다 — 프롬프트로
  *"숫자를 지어내지 마"* 라고 부탁하는 대신 **LLM 이 숫자를 쓸 자리를 없앤다.**
  사실 줄은 전부 여기서 부서 payload 를 그대로 옮겨 포맷한 것이고, LLM 은 그 위에
  **무엇을 확인했는지 옮겨 적는 한 문장**만 얹는다.

  🔴 **LLM 에게 해석을 시키지 않는다.** 처음엔 "해석 문장"을 요구했는데, 값을 안
  보여준 모델이 근거 없이 *"현금 상황이 다소 어려운 편입니다"* 라고 썼다 — 현금
  압박이 `LOW` 인 날이었다. **판단은 규칙(`_END_HEADLINE`)과 부서가 하고, 문장은
  그것을 옮기기만 한다.**

★ **LLM 이 없어도 답이 완결된다.** 문장이 비면 사실 줄만 나가고, 그것도 답이다.
  LLM 을 답의 뼈대로 쓰면 키가 없는 팀원 환경에서 API 가 답을 못 낸다.

★ **부서가 낸 키를 감추지 않는다.** 라벨을 모르는 키는 **이름 그대로** 싣는다.
  아는 것만 싣게 하면 부서가 필드를 늘렸을 때 조용히 사라지고, 그게 §3.7.6("못 한 것을
  한 척하지 않는다")이 막으려는 것이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.master.status_flow import StatusOutcome

#: 부서 이름을 사람 말로. **없는 이름은 그대로 쓴다** (지어내지 않는다).
_AGENT_LABEL: dict[str, str] = {
    "finance": "재무",
    "inventory": "물류",
    "purchase": "매입",
}

#: 답이 아니라 **기준**인 키. 사실 줄이 아니라 꼬리말로 뺀다.
#:
#: ★ **부서마다 따로 적는다.** 재무와 물류의 기준일이 다를 수 있고, 그게 다르면
#:   두 답을 나란히 읽으면 안 된다 — 앵커 정렬(M-24)이 바로 그 문제다.
_BASIS_LABEL: dict[str, str] = {
    "as_of": "기준일",
    "state_date": "기준일",
    "policy_version_used": "정책",
}

#: 아는 키의 라벨. 여기 없는 키는 **키 이름 그대로** 나간다 — 감추지 않는다.
_LABEL: dict[str, str] = {
    "available_cash": "가용 현금",
    "minimum_cash_balance_krw": "최소 보유 현금",
    "projected_cash_min": "투영 최저 현금",
    "projection_days": "투영 일수",
    "payment_pressure": "현금 압박",
    "critical_payment_dates": "위험 지급일",
    "used_capacity_kg": "창고 점유",
    "warehouse_free_kg": "창고 여유",
    "guaranteed_capacity_kg": "보장 용량",
    "lot_count": "보관 로트",
    "min_remaining_freshness_days": "최단 잔여 신선도",
    "min_freshness_lot_id": "해당 로트",
}

#: 종료 코드를 사람이 읽는 결론으로. **"사라 / 사지 마라" 가 여기서 나온다.**
_END_HEADLINE: dict[str, str] = {
    "E1_APPROVED": "매입안을 제시합니다. 고르시면 진행합니다.",
    "E2_HELD": "보류합니다 — 사람이 봐야 할 지적이 있습니다.",
    "E3_REJECTED": "이번에는 매입하지 않는 것을 권합니다.",
    "E4_NOT_STARTED": "실행하지 못했습니다 — 준비되지 않은 부서가 있습니다.",
    "E5_NO_FEASIBLE_PLAN": "실행 가능한 안이 없습니다.",
}


@dataclass(frozen=True)
class Fact:
    """사실 한 줄. **값은 이미 문자열로 굳어 있다** — 렌더링이 숫자를 손대지 않는다."""

    label: str
    value: str
    source: str | None = None

    def line(self) -> str:
        head = f"{self.source} " if self.source else ""
        return f"- {head}{self.label} {self.value}"


@dataclass(frozen=True)
class AnswerFacts:
    """LLM 에게 넘길 재료이자, LLM 이 없을 때의 답 그 자체."""

    headline: str
    facts: tuple[Fact, ...] = ()
    #: 못 본 것 · 못 받은 답. **비우지 않는다.**
    gaps: tuple[str, ...] = ()
    #: 기준일 · 정책 버전 같은 꼬리말.
    basis: tuple[str, ...] = ()
    #: 답한 부서 · 답하지 못한 부서. **LLM 에게는 이 둘만 준다.**
    answered: tuple[str, ...] = ()
    unanswered: tuple[str, ...] = ()

    def to_prompt(self) -> str:
        """LLM 에 넘길 요약. **부서 이름과 결론만 준다.**

        값을 주지 않는 이유는 분명하다 — 모델이 그 숫자를 문장에 옮겨 적고, 숫자
        검사에 걸려 재시도가 돈다. **애초에 안 보여주는 편이 싸다.**

        🔴 **항목 라벨도 주지 않는다.** 처음엔 값을 뺀 라벨(`가용 현금` · `창고 여유`)
        까지는 줬는데, 실측에서 모델이 그 라벨을 **부서와 헷갈렸다** — 물류가 답한
        상황에서 *"창고 여유와 보관 로트 정보는 확인되지 않았습니다"* 라고 썼다.
        정확히 반대다.

        모델이 쓸 문장은 **누가 답했고 누가 못 답했나** 뿐이므로 그 둘만 준다.
        값도 라벨도 안 보이면 **틀리게 쓸 재료 자체가 없다.**
        """
        lines = [f"결론: {self.headline}"]
        if self.answered or self.unanswered:
            # ★ 부서 얘기가 없는 결과(결정 기록)에까지 "답한 부서: 없음" 을 주면
            #   모델이 그 "없음" 을 문장에 옮겨 적는다.
            lines.append("답한 부서: " + (", ".join(self.answered) or "없음"))
            lines.append("답하지 못한 부서: " + (", ".join(self.unanswered) or "없음"))
        return "\n".join(lines)


def agent_label(agent: str) -> str:
    return _AGENT_LABEL.get(agent, agent)


# ── 조회 ────────────────────────────────────────────────────────────────


def facts_from_status(outcome: StatusOutcome) -> AnswerFacts:
    """조회 결과를 사실 줄로. **못 답한 부서를 지우지 않는다.**"""
    facts: list[Fact] = []
    basis: list[str] = []

    for agent, payload in outcome.answers.items():
        label = agent_label(agent)
        for key, value in payload.items():
            text = _format(key, value)
            if not text:
                continue
            if key in _BASIS_LABEL:
                entry = f"{label} {_BASIS_LABEL[key]} {text}"
                if entry not in basis:
                    basis.append(entry)
                continue
            facts.append(Fact(label=_LABEL.get(key, key), value=text, source=label))

    return AnswerFacts(
        headline=_status_headline(outcome),
        facts=tuple(facts),
        gaps=_status_gaps(outcome),
        basis=tuple(basis),
        answered=tuple(agent_label(a) for a in outcome.answers),
        unanswered=tuple(agent_label(a) for a in outcome.unavailable),
    )


def _status_headline(outcome: StatusOutcome) -> str:
    """조회의 머리말은 **문장이 아니라 머리글**이다.

    매입(`_END_HEADLINE`)은 *"사지 마십시오"* 라는 **판단**이라 문장이어야 하지만,
    조회는 판단이 없다 — 누구에게 물었고 누가 답했나가 전부다. 문장으로 쓰면 위에
    얹히는 LLM 문장과 **같은 말을 두 번 하게 된다.**
    """
    answered = ", ".join(agent_label(a) for a in outcome.answers)
    if outcome.status_code == "S1_ANSWERED":
        return f"조회 결과 — {answered}"
    if outcome.status_code == "S2_PARTIAL":
        unanswered = ", ".join(agent_label(a) for a in outcome.unavailable)
        return f"조회 결과 — {answered} 답함 · {unanswered} 답하지 못함"
    return "조회 결과 — 답한 부서 없음"


def _status_gaps(outcome: StatusOutcome) -> tuple[str, ...]:
    """**두 가지를 나눠 적는다** — 다시 물어볼 값어치가 다르기 때문이다."""
    gaps: list[str] = []
    for agent, reason in outcome.errors.items():
        gaps.append(
            f"{agent_label(agent)} 호출이 실패했습니다 ({reason}) — 다시 시도해 볼 수 있습니다"
        )
    for agent, missing in outcome.missing_data.items():
        gaps.append(f"{agent_label(agent)}는 {', '.join(missing)} 가 없어 답하지 못했습니다")
    return tuple(gaps)


# ── 매입 ────────────────────────────────────────────────────────────────


def facts_from_procurement(response: Any) -> AnswerFacts:
    """매입 실행 결과를 사실 줄로.

    ★ **검증 분수를 반드시 싣는다.** `findings` 가 비었다고 "문제 없음"으로 쓰면
      **못 돈 검사가 통과로 읽힌다** (§3.7.6). 못 판정한 수를 같은 줄에 붙인다.
    """
    facts: list[Fact] = []
    gaps: list[str] = []

    labels = [str(s.get("label", "이름 없음")) for s in (response.scenarios or [])]
    if labels:
        facts.append(Fact(label="제시한 안", value=f"{len(labels)}개 ({', '.join(labels)})"))
    if response.single_option:
        facts.append(Fact(label="남은 안", value="1개뿐입니다"))

    facts.append(
        Fact(
            label="검증",
            value=(
                f"지적 {len(response.findings)}건 · "
                f"판정하지 못한 검사 {len(response.skipped_checks)}건"
            ),
        )
    )
    if response.purchase_attempts:
        facts.append(Fact(label="매입 호출", value=f"{response.purchase_attempts}회"))

    for finding in response.findings:
        gaps.append(f"지적: {finding}")
    for concern in response.concerns:
        gaps.append(f"확인 필요: {concern}")
    if response.blocked_by:
        gaps.append(f"막은 부서: {', '.join(agent_label(a) for a in response.blocked_by)}")
    if response.missing_adapters:
        gaps.append(
            f"어댑터 미등록: {', '.join(agent_label(a) for a in response.missing_adapters)}"
        )
    if response.verification_skipped:
        gaps.append("검증을 돌리지 못했습니다 — 통과로 읽지 마십시오")

    # 🔴 **입력이 어디서 왔는지를 결론과 같은 화면에 둔다.** mock 에서 온 값이 섞였는데
    #    결론만 읽으면 실측으로 오해한다 (`inputs.py`).
    for key, source in (getattr(response, "input_sources", None) or {}).items():
        facts.append(Fact(label=f"입력 {key}", value=source))
    if getattr(response, "mocked_inputs", None):
        gaps.append(
            f"🔴 {', '.join(response.mocked_inputs)} 는 mock 에서 왔습니다 — "
            "이 결론을 실측으로 읽지 마십시오"
        )

    return AnswerFacts(
        headline=_END_HEADLINE.get(response.end_code, response.reason),
        facts=tuple(facts),
        gaps=tuple(gaps),
        basis=(f"기준일 {response.as_of.isoformat()} · 요청 {response.request_id}",),
        unanswered=tuple(
            agent_label(a) for a in (*response.blocked_by, *response.missing_adapters)
        ),
    )


# ── 결정 ────────────────────────────────────────────────────────────────

#: 사람의 결정을 사람이 읽는 문장으로.
_DECISION_HEADLINE: dict[str, str] = {
    "APPROVE": "'{label}' 안으로 진행합니다.",
    "REJECT_ALL": "제시된 안을 모두 반려했습니다.",
    "REQUEST_CHANGE": "조건을 붙여 다시 요청하도록 기록했습니다.",
}


def facts_from_decision(decision: Any) -> AnswerFacts:
    """적재된 결정 1건을 사실 줄로.

    🔴 **승인은 기록이지 실행이 아니다.** 여기서 끝난 것은 *"사람이 이 안을 골랐다"*
      까지이고 실제 발주는 이 시스템 밖이다. 그 사실을 답에 **반드시 적는다** —
      안 적으면 사용자는 발주가 나간 줄 안다.
    """
    facts = [
        Fact(label="결정", value=decision.decision),
        Fact(label="회차", value=f"{decision.decision_seq}회차"),
        Fact(label="결정자", value=decision.decided_by),
        Fact(label="대상 실행", value=decision.request_id),
        Fact(label="그때 종료 코드", value=decision.end_code_at_decision),
    ]
    if decision.condition_text:
        facts.append(Fact(label="붙인 조건", value=decision.condition_text))

    gaps: list[str] = []
    if decision.decision == "APPROVE":
        gaps.append("이 기록은 사람이 안을 골랐다는 것까지입니다 — 실제 발주는 별도입니다")
    if decision.decision == "REQUEST_CHANGE" and not decision.follow_up_request_id:
        gaps.append("조건을 반영한 재실행은 아직 걸려 있지 않습니다")

    return AnswerFacts(
        headline=_DECISION_HEADLINE.get(decision.decision, decision.decision).format(
            label=decision.scenario_label or ""
        ),
        facts=tuple(facts),
        gaps=tuple(gaps),
        basis=(f"기록 {decision.created_at:%Y-%m-%d %H:%M}",),
    )


# ── 렌더링 ──────────────────────────────────────────────────────────────


def render_answer(facts: AnswerFacts, narrative: str | None = None) -> str:
    """최종 텍스트. **문장이 없어도 완결된다.**

    문장은 맨 앞에 한 문단으로 붙고, 그 아래는 전부 규칙이 만든 줄이다 — 숫자가
    문장 안으로 들어가지 않으므로 **LLM 이 값을 바꿀 경로가 없다.**
    """
    blocks: list[str] = []
    if narrative:
        blocks.append(narrative.strip())
    blocks.append(facts.headline)
    if facts.facts:
        blocks.append("\n".join(f.line() for f in facts.facts))
    if facts.gaps:
        blocks.append("확인해 주세요\n" + "\n".join(f"- {g}" for g in facts.gaps))
    if facts.basis:
        blocks.append("(" + " · ".join(facts.basis) + ")")
    return "\n\n".join(blocks)


# ── 값 포맷 ─────────────────────────────────────────────────────────────


def _format(key: str, value: Any) -> str:
    """값 하나를 사람이 읽는 문자열로. **여기서만 숫자를 만든다.**"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, Mapping):
        return ", ".join(f"{k} {_format(k, v)}" for k, v in value.items())
    if isinstance(value, Sequence) and not isinstance(value, str):
        items = [text for v in value if (text := _format(key, v))]
        if not items:
            return "없음"
        if len(items) > 3:
            return f"{', '.join(items[:3])} 외 {len(items) - 3}건"
        return ", ".join(items)
    if isinstance(value, (int, float)):
        return _number(key, value)
    return str(value)


def _number(key: str, value: float) -> str:
    if key.endswith("_kg"):
        return f"{_trim(value)}kg"
    if key.endswith("_days"):
        return f"{_trim(value)}일"
    if key.endswith("_count"):
        return f"{_trim(value)}건"
    if key.endswith("_krw") or "cash" in key:
        return f"{value:,.0f}원"
    return _trim(value)


def _trim(value: float) -> str:
    """소수점이 의미 없으면 뗀다 — `4.0건` 은 사람이 읽는 글이 아니다."""
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"
