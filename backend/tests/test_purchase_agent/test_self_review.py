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
