"""근거 자기 검토 판단자 (E3-10) — **지적 코드만 고른다.**

🔴 **컷 권한이 없다.** 여기서 나온 것은 경고이고, 수량·분할·등급·금액·날짜·컷 결과를
바꾸지 않는다. 계산 검사는 ⑦ 이 이미 결정적으로 끝냈다.

🔴 **문장을 안 쓴다.** 고르는 것은 ``review_templates.FINDINGS`` 의 코드와 입력에 있던
근거 id 뿐이고, 사람이 읽는 문장은 코드가 만든다.

⚠️ **같은 판단자가 자기 출력을 본다.** ⑤ 의 사유를 검토할 때는 자기가 쓴 문장을 자기가
보는 셈이고, 같은 모델 계열이라 같은 맹점을 공유한다. 그래서 경고까지이고 컷이 아니다.
"""

from collections.abc import Callable

from app.purchase_agent.llm.review_schemas import (
    ReviewContext,
    ReviewOutput,
    ReviewResult,
)
from app.purchase_agent.llm.runtime import (
    LLMProvider,
    LLMSettings,
    RoleSpec,
    build_provider,
    get_llm_settings,
    run_with_fallback,
)
from app.purchase_agent.llm.text_guard import contains_control_chars, contains_number
from app.purchase_agent.review_templates import FINDINGS

SYSTEM_PROMPT = """당신은 매입 에이전트의 근거 검토 레이어다.
수량·금액·제약 검사는 이미 끝났다. 당신이 보는 것은 **근거와 주장이 서로 맞는가**다.

규칙:
- offered_findings 에 있는 code 만 고른다. 없는 code 를 지어내지 않는다.
- target_ref_id 는 claims 에 있는 ref_id 중 하나여야 한다. 지어내지 않는다.
- 문장을 쓰지 않는다. 숫자를 쓰지 않는다. code 와 ref_id 만 고른다.
- 지적할 것이 없으면 findings 를 빈 목록으로 둔다. **없는 지적을 만들지 않는다.**

보는 것:
- claim_text 가 그 근거(evidence_category)로 뒷받침되는가.
- claim_strength 가 ASSERTIVE 인데 evidence_strength 가 ASSUMED 면 결론이 근거보다 세다.
- risk_categories 에 이 안의 상태에서 나와야 할 위험이 빠져 있는가.
- mix_reason 이 signals 와 맞는가.
- strategy_type 이 timing 인데 round_count 가 SINGLE 이면 라벨과 실체가 다르다.

claim_text 의 <NUM>·<DATE>·<PCT>·<AMT> 는 가려진 값이다. 그 값을 추측하지 않는다.
"""

ROLE = RoleSpec(
    system_prompt=SYSTEM_PROMPT,
    response_schema=ReviewOutput.model_json_schema(),
    prompt_version="self-review-1",
    schema_version="self-review-1",
)

#: ``(context) -> ReviewResult``
Reviewer = Callable[[ReviewContext], ReviewResult]

_GUIDANCE = {
    "INVALID_SCHEMA": "findings 배열만 포함한 유효한 JSON 을 작성하세요.",
    "UNKNOWN_CODE": "offered_findings 에 있는 code 만 고르세요.",
    "UNKNOWN_REF": "claims 에 있는 ref_id 만 가리키세요.",
    "MISSING_REF": "그 지적은 어느 근거인지 가리켜야 합니다.",
    "NUMERIC_OUTPUT_FORBIDDEN": "숫자를 쓰지 마세요.",
    "CONTROL_CHARACTERS": "보이지 않는 문자를 쓰지 마세요.",
}


class ReviewInvalid(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__(", ".join(issues))
        self.issues = issues


def validate_output(raw_output: str, context: ReviewContext) -> ReviewOutput:
    """프로바이더 밖의 공통 관문.

    🔴 **숫자 검사는 식별자에 안 건다.** ``code`` 도 ``ref_id`` 도 규칙이 만든 이름이라
    숫자가 들어갈 수 있다 — 거기 걸면 정상 지적이 매번 fallback 으로 떨어진다.
    이 응답에는 자유 문장이 아예 없으므로, 숫자 검사는 **제어문자와 같은 결의 위생 검사**로만
    남긴다 (식별자에 숨은 제어문자가 화면에 그대로 실리는 것을 막는다).
    """
    try:
        output = ReviewOutput.model_validate_json(raw_output)
    except Exception as error:
        raise ReviewInvalid(["INVALID_SCHEMA"]) from error

    허용코드 = set(context.offered_findings)
    허용근거 = {claim.ref_id for claim in context.claims}
    issues: list[str] = []
    for finding in output.findings:
        if finding.code not in 허용코드:
            issues.append("UNKNOWN_CODE")
            continue
        필요 = FINDINGS[finding.code].needs_ref if finding.code in FINDINGS else False
        if 필요 and not finding.target_ref_id:
            issues.append("MISSING_REF")
        if finding.target_ref_id and finding.target_ref_id not in 허용근거:
            issues.append("UNKNOWN_REF")
        if contains_control_chars(finding.code) or contains_control_chars(
            finding.target_ref_id or ""
        ):
            issues.append("CONTROL_CHARACTERS")
    if issues:
        raise ReviewInvalid(sorted(set(issues)))
    return output


def guidance_for(error: Exception) -> list[str]:
    if isinstance(error, ReviewInvalid):
        return [_GUIDANCE[issue] for issue in error.issues if issue in _GUIDANCE]
    return ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]


def needs_call(context: ReviewContext) -> bool:
    """근거가 있어야 검토할 것이 있다."""
    return bool(context.claims) and bool(context.offered_findings)


class SelfReviewService:
    """이 역할의 설정. 골격은 ``run_with_fallback`` 이 소유한다."""

    def __init__(self, settings: LLMSettings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def review(self, context: ReviewContext) -> ReviewResult:
        """지적을 받아 온다. **실패하면 지적 0건이다.**

        🔴 실패를 「문제 없음」으로 적지 않는다 — 상태가 ``FALLBACK`` 으로 남고, 그 사실은
        실행 흔적이 든다. 지적이 0건인 것과 **검토를 못 한 것**은 다른 사실이다.
        """
        template = ReviewOutput(findings=[])
        출력, 상태, 시도, 떨어짐 = run_with_fallback(
            settings=self.settings,
            provider=self.provider,
            context=context,
            template=template,
            validate=lambda raw: validate_output(raw, context),
            needs_call=needs_call(context),
            guidance_for=guidance_for,
        )
        return ReviewResult(
            output=출력,
            llm_status=상태,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model,
            llm_attempts=시도,
            llm_fallback_used=떨어짐,
        )


def make_reviewer(service: SelfReviewService | None = None) -> Reviewer:
    """꺼져 있거나 실패해도 **지적 0건**을 돌려준다 — 그래프가 멈추지 않는다."""
    selection = service or _service()

    def reviewer(context: ReviewContext) -> ReviewResult:
        return selection.review(context)

    return reviewer


def _service() -> SelfReviewService:
    settings = get_llm_settings()
    # 🔴 **조립은 ``build_provider`` 하나가 한다.** 역할마다 베끼면 모르는 provider 일
    #   때만 갈라지는 길이 생긴다 — 그 클래스 docstring 에 실제로 밟은 자리가 있다.
    return SelfReviewService(settings, build_provider(settings, ROLE))


#: 🔴 ``contains_number`` 는 이 역할에서 **응답이 아니라 요청**을 잰다 —
#: 정제가 실제로 됐는지 노드가 확인할 때 쓴다 (``nodes/review_rationale``).
__all__ = [
    "ROLE",
    "ReviewInvalid",
    "Reviewer",
    "SelfReviewService",
    "contains_number",
    "guidance_for",
    "make_reviewer",
    "needs_call",
    "validate_output",
]
