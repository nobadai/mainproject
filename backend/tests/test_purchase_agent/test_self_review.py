"""⑧ 근거 자기 검토 (E3-10) — **경고만 더한다.**

🔴 이 파일이 지키는 것 셋.

① **컷 권한이 없다** — 수량·분할·등급·금액·날짜·컷 결과가 한 칸도 안 바뀐다.
② **실패를 「문제 없음」으로 안 적는다** — 안 본 안도 흔적에 남는다.
③ **같은 코드 집합이면 문장과 순서가 같다** — 판단자가 뒤집어 돌려줘도 그렇다.

⚠️ ③이 「SUCCESS 결과가 결정적이다」는 뜻은 **아니다.** 고정되는 것은 문장과 순서이고,
*"어떤 코드를 고르는가"* 는 판단자의 응답이다.
"""

from datetime import date

import pytest

from app.purchase_agent.llm import self_review as sr
from app.purchase_agent.llm.review_schemas import (
    ClaimIn,
    FindingOut,
    ReviewContext,
    ReviewOutput,
    ReviewResult,
)
from app.purchase_agent.nodes import review_rationale as rr
from app.purchase_agent.review_templates import FINDINGS

ITEM = "배추"
AS_OF = date(2026, 8, 21)

_불변 = (
    "total_qty_kg",
    "total_amount_krw",
    "coverage_days",
    "split_plan",
    "sourcing_plan",
    "payment_schedule",
    "cut_unit_price",
    "max_price",
    "strategy_type",
)


def _context(*, claims: list[ClaimIn] | None = None) -> ReviewContext:
    return ReviewContext(
        scenario_label="기본",
        strategy_type="timing",
        round_count="SINGLE",
        claims=claims
        if claims is not None
        else [
            ClaimIn(
                ref_id="Q-1",
                evidence_category="시세관측",
                evidence_strength="ASSUMED",
                claim_strength="ASSERTIVE",
                claim_text="등급 조합을 <NUM> 비중으로 간다",
            )
        ],
        risk_categories=["WAREHOUSE"],
        signals=["LABEL_BODY_MISMATCH"],
        offered_findings=list(FINDINGS),
    )


def _응답(*findings: tuple[str, str | None]) -> str:
    return ReviewOutput(
        findings=[FindingOut(code=c, target_ref_id=r) for c, r in findings]
    ).model_dump_json()


# ── 계약 ───────────────────────────────────────────────────────
def test_요청에_원본_숫자가_없다() -> None:
    """🔴 값을 주면 판단자가 **지적에 베껴 쓴다** (규칙 6)."""
    from app.purchase_agent.llm.text_guard import contains_number

    context = _context()
    # 🔴 **자연어 칸만 본다.** ``ref_id`` 는 규칙이 만든 식별자라 숫자가 들어가도 정상이다
    #   — 거기까지 재면 멀쩡한 근거를 「샜다」로 읽는다.
    for claim in context.claims:
        assert contains_number(claim.claim_text.replace("<NUM>", "")) is False
    assert any("<NUM>" in claim.claim_text for claim in context.claims)


def test_응답에_자유_문장이_없다() -> None:
    """문장을 받으면 같은 판정에서도 표현이 매번 달라진다."""
    칸 = ReviewOutput.model_json_schema()["$defs"]["FindingOut"]["properties"]
    assert set(칸) == {"code", "target_ref_id"}


def test_모르는_코드를_거부한다() -> None:
    with pytest.raises(sr.ReviewInvalid) as 잡힘:
        sr.validate_output(_응답(("MADE_UP", None)), _context())
    assert "UNKNOWN_CODE" in 잡힘.value.issues


def test_없는_근거를_가리키면_거부한다() -> None:
    with pytest.raises(sr.ReviewInvalid) as 잡힘:
        sr.validate_output(_응답(("CLAIM_SOURCE_MISMATCH", "DOC-999")), _context())
    assert "UNKNOWN_REF" in 잡힘.value.issues


def test_가리켜야_하는_지적은_근거를_요구한다() -> None:
    with pytest.raises(sr.ReviewInvalid) as 잡힘:
        sr.validate_output(_응답(("CLAIM_SOURCE_MISMATCH", None)), _context())
    assert "MISSING_REF" in 잡힘.value.issues


def test_가리킬_근거가_없는_지적은_통과한다() -> None:
    """🔴 없는 것을 필수로 두면 판단자가 **아무 id 나 붙인다.**"""
    출력 = sr.validate_output(_응답(("RISK_CATEGORY_MISSING", None)), _context())
    assert len(출력.findings) == 1


def test_근거가_없으면_안_부른다() -> None:
    assert sr.needs_call(_context(claims=[])) is False


def test_역할_지시문이_다른_둘과_겹치지_않는다() -> None:
    from app.purchase_agent.llm.runtime import MIX_ROLE
    from app.purchase_agent.llm.split_allocation import ROLE as SPLIT_ROLE

    assert len({sr.ROLE.system_prompt, MIX_ROLE.system_prompt, SPLIT_ROLE.system_prompt}) == 3


# ── 판단자 ─────────────────────────────────────────────────────
class _터짐:
    def generate(self, context, *, retry_guidance=None):
        raise RuntimeError("키가 없다")


class _됨:
    def __init__(self, 응답: str):
        self.응답 = 응답

    def generate(self, context, *, retry_guidance=None):
        return self.응답


def _설정(*, enabled: bool = True):
    return type(
        "S",
        (),
        {
            "enabled": enabled,
            "max_retries": 1,
            "provider": "anthropic",
            "model": "haiku",
            "reason_max_chars": 300,
        },
    )()


def test_실패하면_지적_0건이고_상태가_남는다() -> None:
    """🔴 **실패를 「문제 없음」으로 안 적는다** — 지적 0건과 검토 못 함은 다른 사실이다."""
    결과 = sr.SelfReviewService(_설정(), _터짐()).review(_context())
    assert 결과.output.findings == []
    assert (결과.llm_status, 결과.llm_fallback_used) == ("FALLBACK", True)


def test_성공하면_지적이_실린다() -> None:
    결과 = sr.SelfReviewService(_설정(), _됨(_응답(("LABEL_BODY_MISMATCH", None)))).review(
        _context()
    )
    assert [f.code for f in 결과.output.findings] == ["LABEL_BODY_MISMATCH"]
    assert 결과.llm_status == "SUCCESS"


# ── 노드 ───────────────────────────────────────────────────────
def _제안(monkeypatch: pytest.MonkeyPatch, *, 켬: bool, reviewer=None):
    from app.purchase_agent.graph import build_graph
    from app.purchase_agent.state import build_initial_state

    monkeypatch.setattr(rr, "enabled", lambda key, default=False: 켬)
    state = build_initial_state(ITEM, AS_OF)
    final = build_graph(reviewer=reviewer).invoke(state)
    return final["proposal"], final.get("review_calls") or ()


def _모두지적(context: ReviewContext) -> ReviewResult:
    return ReviewResult(
        output=ReviewOutput(findings=[FindingOut(code="LABEL_BODY_MISMATCH")]),
        llm_status="SUCCESS",
        llm_provider="anthropic",
        llm_model="haiku",
        llm_attempts=1,
        llm_fallback_used=False,
    )


def test_꺼지면_제안이_그대로다(monkeypatch: pytest.MonkeyPatch) -> None:
    끈, _ = _제안(monkeypatch, 켬=False, reviewer=_모두지적)
    켠, _ = _제안(monkeypatch, 켬=True, reviewer=lambda ctx: ReviewResult(
        output=ReviewOutput(findings=[]), llm_status="FALLBACK", llm_provider=None,
        llm_model=None, llm_attempts=2, llm_fallback_used=True,
    ))
    assert 끈 == 켠


def test_지적이_있어도_숫자_결과가_안_바뀐다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **컷 권한이 없다.** 바뀌는 것은 ``risks`` 뿐이다.

    ⚠️ ``risks`` 와 산출물 해시는 **의도적으로 달라진다** — ``risks`` 가 ``Scenario`` 의
    필드라 「아무것도 안 바뀐다」와 동시에 참일 수 없다.
    """
    끈, _ = _제안(monkeypatch, 켬=False, reviewer=_모두지적)
    켠, _ = _제안(monkeypatch, 켬=True, reviewer=_모두지적)
    for 전, 후 in zip(끈["scenarios"], 켠["scenarios"], strict=True):
        for 칸 in _불변:
            assert 전.get(칸) == 후.get(칸), 칸
        assert len(후["risks"]) >= len(전["risks"])
    assert 끈["rejected_reasons"] == 켠["rejected_reasons"]


def test_안_본_안도_흔적에_남는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 목록에서 빼면 *"봤는데 깨끗했다"* 와 구분되지 않는다 — 검토율이 거짓이 된다."""
    제안, 기록 = _제안(monkeypatch, 켬=True, reviewer=_모두지적)
    적힌_라벨 = {c.target for c in 기록}
    assert 적힌_라벨 == {안["label"] for 안 in 제안["scenarios"]}
    assert all(c.role == "rationale_self_review" for c in 기록)


def test_건너뛴_흔적에는_사유가_있다(monkeypatch: pytest.MonkeyPatch) -> None:
    _, 기록 = _제안(monkeypatch, 켬=True, reviewer=_모두지적)
    for 줄 in 기록:
        if 줄.status.startswith("SKIPPED_"):
            assert (줄.skip_reason or "").strip()


def test_역할_이름이_어댑터와_같다() -> None:
    """🔴 순환을 피해 문자열을 두 곳에 뒀다 — **갈리는 것을 검사가 막는다.**"""
    from app.purchase_agent.adapter import RATIONALE_SELF_REVIEW

    assert rr.RATIONALE_SELF_REVIEW == RATIONALE_SELF_REVIEW


# ── ⑤ 사유가 실제로 들어가는가 ──────────────────────────────────
#: ⑤ 가 **실제로 불리는** 앵커. 다른 앵커는 규칙이 중품을 안 골라 판단자를 안 부른다.
MIX_CALLED = date(2026, 9, 11)


class _가짜판단:
    """``MixDecision`` 이 드는 칸만 흉내 낸다 — ``applied`` 가 갈림길이다."""

    def __init__(self, reason: str, *, applied: bool = True):
        self.reason = reason
        self.llm_status = "SUCCESS" if applied else "FALLBACK"

    @property
    def applied(self) -> bool:
        return self.llm_status == "SUCCESS"


def test_안_돌았으면_사유를_안_넣는다() -> None:
    """🔴 ``None`` 이다 — **빈 문자열이 아니다** (규칙 3 의 문자열 판)."""
    assert rr.mix_reason_for_review(None) is None


def test_규칙_기본안이면_사유를_안_넣는다() -> None:
    """떨어진 날의 사유는 판단자가 쓴 문장이 아니라 **코드가 박은 상수**다."""
    assert rr.mix_reason_for_review(_가짜판단("규칙 기본안", applied=False)) is None


def test_사유의_숫자를_가려서_넣는다() -> None:
    가린 = rr.mix_reason_for_review(_가짜판단("2026-09-11 기준 중품을 30% 더 싣는다"))
    assert 가린 is not None
    assert "2026-09-11" not in 가린 and "30%" not in 가린
    assert "<DATE>" in 가린 and "<PCT>" in 가린


def test_못_가린_숫자가_남으면_아예_안_넣는다() -> None:
    """🔴 못 가린 것을 넣느니 **안 본다** — 근거 문장을 다루는 규율과 같다.

    ``½`` 는 ``\\d`` 로는 안 잡히는데 ``isnumeric()`` 에는 걸린다. 정제가 못 덮는 자리다.
    """
    assert rr.mix_reason_for_review(_가짜판단("중품을 ½ 만큼 싣는다")) is None


def test_사유가_있으면_그_지적을_고를_수_있다() -> None:
    """왕복 — 사유가 실린 컨텍스트로 물어보고, 검증을 지나 그 코드가 돌아온다."""
    context = _context()
    실린 = context.model_copy(update={"mix_reason": "스프레드가 좁은데 중품을 늘린다"})
    결과 = sr.SelfReviewService(
        _설정(), _됨(_응답(("MIX_REASON_LABEL_MISMATCH", None)))
    ).review(실린)
    assert 결과.llm_status == "SUCCESS"
    assert [f.code for f in 결과.output.findings] == ["MIX_REASON_LABEL_MISMATCH"]
    assert sr.validate_output(_응답(("MIX_REASON_LABEL_MISMATCH", None)), 실린)


def _사유를_고정한_mix(reason: str):
    from app.purchase_agent.llm.mix import MixDecision

    def selector(context, default_candidate_id: str) -> MixDecision:
        return MixDecision(
            candidate_id=default_candidate_id,
            reason=reason,
            llm_status="SUCCESS",
            llm_model="haiku",
            llm_fallback_used=False,
            llm_attempts=1,
        )

    return selector


def test_판단자_사유가_검토_재료로_들어가고_지적이_왕복한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 **``MIX_REASON_LABEL_MISMATCH`` 를 실제로 검사할 수 있어야 한다.**

    전에는 ``build_context`` 가 늘 ``mix_reason=None`` 이라, 그 코드가 목록에는 있는데
    **볼 재료가 없었다** — 고를 수는 있지만 무엇을 보고 고르는지가 없는 상태였다.

    ★ 사유는 **그날 하나**다. ⑤ 가 그날 한 번 돌고 ⑥ 이 같은 등급 비율을 모든 안에
      곱하므로, 검토 대상 안들이 **같은 문장**을 받는다.
    """
    from app.purchase_agent.graph import build_graph
    from app.purchase_agent.state import build_initial_state

    본_것: list[ReviewContext] = []

    def 지적한다(context: ReviewContext) -> ReviewResult:
        본_것.append(context)
        return ReviewResult(
            output=ReviewOutput(
                findings=[FindingOut(code="MIX_REASON_LABEL_MISMATCH")]
            ),
            llm_status="SUCCESS",
            llm_provider="anthropic",
            llm_model="haiku",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    monkeypatch.setattr(rr, "enabled", lambda key, default=False: True)
    state = build_initial_state(ITEM, MIX_CALLED)
    final = build_graph(
        selector=_사유를_고정한_mix("스프레드가 넓어 중품을 30% 더 싣는다"),
        reviewer=지적한다,
    ).invoke(state)

    assert 본_것, "⑤ 가 안 불렸다 — 이 앵커가 더 이상 그 자리가 아니다"
    for context in 본_것:
        assert context.mix_reason == "스프레드가 넓어 중품을 <PCT> 더 싣는다"
        assert "MIX_APPLIED" in context.signals
    # 🔴 같은 판단이므로 **모든 안이 같은 문장**을 받는다.
    assert len({c.mix_reason for c in 본_것}) == 1

    문면 = FINDINGS["MIX_REASON_LABEL_MISMATCH"].template.format(ref=None)
    실린_안 = [안 for 안 in final["proposal"]["scenarios"] if 문면 in (안["risks"] or [])]
    assert len(실린_안) == len(본_것)


# ── 렌더링이 실패하면 성공으로 안 적는다 ──────────────────────────


def test_문장으로_못_옮기면_성공으로_안_적는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 지적을 버리고 ``SUCCESS`` 를 남기면 «봤는데 깨끗했다» 로 읽힌다.

    실제로는 **검토 결과를 적용하지 못한 것**이다. 검증이 이미 막는 자리라 여기 오면
    내부 계약 위반이고, 그래서 조용히 넘기지 않고 ``FALLBACK`` 과 사유를 남긴다.

    ⚠️ 예외를 그대로 올리지는 않는다 — ⑦ 이 통과시킨 제안이 ⑧ 때문에 통째로 사라지면
    «경고만 하는 자리» 가 산출물을 죽이는 것이 된다.
    """
    def 모르는_코드를_준다(context: ReviewContext) -> ReviewResult:
        return ReviewResult(
            output=ReviewOutput(findings=[FindingOut(code="NOPE_UNKNOWN")]),
            llm_status="SUCCESS",
            llm_provider="anthropic",
            llm_model="haiku",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    제안, 기록 = _제안(monkeypatch, 켬=True, reviewer=모르는_코드를_준다)
    부른_것 = [줄 for 줄 in 기록 if not 줄.status.startswith("SKIPPED_")]
    assert 부른_것, "검토 대상이 하나도 없었다 — 이 검사가 아무것도 안 잰다"
    for 줄 in 부른_것:
        assert 줄.status == "FALLBACK"
        assert 줄.fallback_used is True
        assert (줄.skip_reason or "").strip()
    # 지적을 못 옮겼으니 **아무 문장도 안 실린다.**
    끈, _ = _제안(monkeypatch, 켬=False, reviewer=모르는_코드를_준다)
    for 전, 후 in zip(끈["scenarios"], 제안["scenarios"], strict=True):
        assert 전["risks"] == 후["risks"]


# ── ⑤ 라벨도 같이 가는가 ────────────────────────────────────────


def test_라벨_판정이_다섯번과_같은_자리에서_나온다() -> None:
    """🔴 **판정이 한 곳에 있어야 대조가 성립한다.**

    ⑤ 가 묻는 라벨과 ⑧ 이 되읽는 라벨을 각자 계산하면, ⑧ 의 대조가 «남의 판단과의 대조»가
    아니라 **자기 계산끼리의 대조**가 된다. 두 값이 늘 같은지 입력 격자로 잰다.
    """
    from app.purchase_agent.llm.mix import build_mix_context, context_labels
    from app.purchase_agent.llm.schemas import MixCandidate

    for widened in (True, False):
        for shelf_days in (None, 6.0):
            for cap_ratio in (0.4, 1.0):
                판단 = {
                    "widened": widened,
                    "shelf_days": shelf_days,
                    "cap_ratio": cap_ratio,
                }
                context = build_mix_context(
                    "배추",
                    spread_widened=widened,
                    shelf_days=shelf_days,
                    shelf_tight=cap_ratio < 1.0,
                    signals=[],
                    facts=[],
                    candidates=[MixCandidate(candidate_id="BASE_ONLY", summary="기본")],
                )
                assert context_labels(판단)[:2] == (context.spread, context.freshness)


def test_고른_후보도_라벨에_실린다() -> None:
    """어느 후보를 골랐는지까지 봐야 *"사유가 그 선택과 맞나"* 를 물을 수 있다."""
    from app.purchase_agent.llm.mix import MixDecision, context_labels

    판단 = {
        "widened": True,
        "shelf_days": 6.0,
        "cap_ratio": 0.4,
        "mix": MixDecision(
            candidate_id="BASE_ONLY",
            reason="사유",
            llm_status="SUCCESS",
            llm_model="haiku",
            llm_fallback_used=False,
        ),
    }
    assert context_labels(판단) == ("SPREAD_WIDE", "SHELF_TIGHT", "BASE_ONLY")


def test_판단자에게_사유와_라벨이_같이_간다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **사유만 주면 비교할 대상이 없다.**

    ``MIX_REASON_LABEL_MISMATCH`` 의 뜻이 *"사유가 입력 라벨·선택 후보와 안 맞는다"* 라,
    라벨이 빠지면 그 코드는 고를 수는 있는데 **무엇을 보고 고르는지가 없는** 상태가 된다.
    """
    from app.purchase_agent.graph import build_graph
    from app.purchase_agent.state import build_initial_state

    본_것: list[ReviewContext] = []

    def 본다(context: ReviewContext) -> ReviewResult:
        본_것.append(context)
        return ReviewResult(
            output=ReviewOutput(findings=[]),
            llm_status="SUCCESS",
            llm_provider="anthropic",
            llm_model="haiku",
            llm_attempts=1,
            llm_fallback_used=False,
        )

    monkeypatch.setattr(rr, "enabled", lambda key, default=False: True)
    build_graph(
        selector=_사유를_고정한_mix("스프레드가 넓어 중품을 더 싣는다"), reviewer=본다
    ).invoke(build_initial_state(ITEM, MIX_CALLED))

    assert 본_것, "⑤ 가 안 불렸다 — 이 앵커가 더 이상 그 자리가 아니다"
    for context in 본_것:
        assert context.mix_labels, "사유는 갔는데 라벨이 안 갔다"
        assert any(라벨.startswith("SPREAD_") for 라벨 in context.mix_labels)
        assert any(라벨.startswith("SHELF_") for 라벨 in context.mix_labels)
    # 그날 판단은 하나 — 안들이 **같은 라벨**을 받는다.
    assert len({tuple(c.mix_labels) for c in 본_것}) == 1


def test_다섯번이_안_돌면_라벨도_빈_목록이다(monkeypatch: pytest.MonkeyPatch) -> None:
    """사유가 없으면 라벨도 없다 — 안 한 판단에 라벨을 붙이지 않는다."""
    _, 기록 = _제안(monkeypatch, 켬=True, reviewer=_모두지적)
    assert 기록  # 흔적은 남는다
    context = rr.build_context(
        {"label": "기본", "strategy_type": "quantity", "rationale": [], "risks": []},
        rr.ScenarioSignals(label="기본"),
    )
    assert context.mix_reason is None
    assert context.mix_labels == []
