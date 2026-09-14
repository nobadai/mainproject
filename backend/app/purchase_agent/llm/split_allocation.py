"""④ 회차 배분 판단자 (E3-9) — **후보 하나를 고르는 것이 전부다.**

🔴 **비율·수량·날짜를 만들지 않는다.** 규칙이 후보를 만들고 안전 검사까지 끝냈고, 여기서
하는 일은 그중 하나를 고르는 것뿐이다. 고른 뒤 실제 수량과 날짜로 펴는 것은 ⑥ 이다.

★ **재시도·오류 분류·fallback 은 ``runtime.run_with_fallback`` 이 소유한다.** 이 파일에는
이 역할의 것만 있다 — 지시문·응답 스키마·검증·기본안.
"""

from collections.abc import Callable

from app.purchase_agent.llm.runtime import (
    LLMProvider,
    LLMSettings,
    RoleSpec,
    build_provider,
    get_llm_settings,
    run_with_fallback,
)
from app.purchase_agent.llm.split_schemas import (
    SplitAllocationChoice,
    SplitAllocationContext,
    SplitAllocationResult,
    SplitCandidate,
)
from app.purchase_agent.llm.text_guard import contains_control_chars, contains_number

SYSTEM_PROMPT = """당신은 매입 에이전트의 분할 회차 배분 판단 레이어다.
계산은 이미 끝났다. 규칙이 만든 후보 중 **하나를 고르고 이유를 쓰는 것**이 전부다.

규칙:
- candidates 에 있는 candidate_id 만 고른다. 없는 id 를 지어내지 않는다.
- reason 에 숫자를 쓰지 않는다. 비율도 수량도 날짜도 쓰지 않는다.
- reason 은 한국어 한 문장이다.

판단 기준:
- trend 가 TREND_RISING 이면 앞 회차에 더 싣는 쪽이 단가에 유리하다.
  다만 앞에 실을수록 그 물량이 창고에서 더 오래 늙는다.
- cap 이 CAP_TIGHT 면 앞에 몰기 어렵다. CAP_UNKNOWN 은 넉넉하다는 뜻이 아니라
  **못 봤다**는 뜻이다 — 모르는 쪽으로 물량을 밀지 않는다.
"""

#: 사유 상한. ⑤ 와 같은 뜻의 값이고, 늘어나면 사유가 서술이 된다.
REASON_MAX_CHARS = 300

ROLE = RoleSpec(
    system_prompt=SYSTEM_PROMPT,
    response_schema=SplitAllocationChoice.model_json_schema(),
)

#: ``(context, 기본 후보 id) -> SplitAllocationResult``
SplitAllocationSelector = Callable[
    [SplitAllocationContext, str], SplitAllocationResult
]

_GUIDANCE = {
    "INVALID_SCHEMA": "지정된 두 필드만 포함한 유효한 JSON 을 작성하세요.",
    "EMPTY_FIELD": "두 필드를 모두 채우세요.",
    "UNKNOWN_CANDIDATE": "제시된 candidates 의 candidate_id 중 하나만 고르세요.",
    "NUMERIC_OUTPUT_FORBIDDEN": "reason 에 숫자를 쓰지 마세요.",
    "CONTROL_CHARACTERS": "보이지 않는 문자를 쓰지 마세요.",
    "REASON_TOO_LONG": "reason 을 한 문장으로 줄이세요.",
}


class SplitAllocationInvalid(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__(", ".join(issues))
        self.issues = issues


def validate_choice(
    raw_output: str, context: SplitAllocationContext
) -> SplitAllocationChoice:
    """프로바이더 밖의 공통 관문. **어느 API 를 쓰든 같은 문을 지난다.**

    🔴 ``chosen_candidate_id`` 는 **숫자 검사 대상이 아니다.** 후보 id 는 규칙이 만든
    식별자라 숫자가 들어갈 수 있다 — 거기 걸면 정상 선택이 매번 fallback 으로 떨어진다.
    검사는 **자연어인 ``reason``** 에만 건다 (⑤ 와 같은 경계).
    """
    try:
        choice = SplitAllocationChoice.model_validate_json(raw_output)
    except Exception as error:
        raise SplitAllocationInvalid(["INVALID_SCHEMA"]) from error
    issues: list[str] = []
    if not choice.chosen_candidate_id.strip() or not choice.reason.strip():
        issues.append("EMPTY_FIELD")
    if choice.chosen_candidate_id not in {c.candidate_id for c in context.candidates}:
        issues.append("UNKNOWN_CANDIDATE")
    if contains_number(choice.reason):
        issues.append("NUMERIC_OUTPUT_FORBIDDEN")
    if contains_control_chars(choice.reason) or contains_control_chars(
        choice.chosen_candidate_id
    ):
        issues.append("CONTROL_CHARACTERS")
    if len(choice.reason) > REASON_MAX_CHARS:
        issues.append("REASON_TOO_LONG")
    if issues:
        raise SplitAllocationInvalid(issues)
    return choice


def guidance_for(error: Exception) -> list[str]:
    """이 역할의 **오류 분류**. 무엇이 틀렸는지 되돌려 준다."""
    if isinstance(error, SplitAllocationInvalid):
        return [_GUIDANCE[issue] for issue in error.issues if issue in _GUIDANCE]
    return ["지정된 규칙과 JSON 형식에 맞춰 다시 작성하세요."]


def needs_call(context: SplitAllocationContext) -> bool:
    """후보가 **둘 이상일 때만** 부른다.

    하나뿐이면 고를 것이 없다 — 부르면 비용만 들고 상태만 흐려진다 (⑤ ``needs_llm`` 과 같다).
    """
    return len(context.candidates) >= 2


def build_context(
    item: str,
    *,
    rounds: int,
    rising: bool,
    cap_tight: bool | None,
    signals: list[str],
    facts: list[str],
    candidates: list[SplitCandidate],
) -> SplitAllocationContext:
    """판단 재료를 **라벨로 바꿔** 넘긴다 — 숫자를 안 준다.

    ⚠️ ``cap_tight`` 가 ``None`` 이면 ``CAP_UNKNOWN`` 이다. **「넉넉하다」로 안 접는다** —
    모르는 것을 넉넉함으로 읽으면 모르는 쪽으로 물량이 밀린다 (규칙 3).
    """
    if cap_tight is None:
        cap = "CAP_UNKNOWN"
    else:
        cap = "CAP_TIGHT" if cap_tight else "CAP_AMPLE"
    return SplitAllocationContext(
        item=item,
        rounds="ROUNDS_TWO" if rounds == 2 else "ROUNDS_THREE",
        trend="TREND_RISING" if rising else "TREND_FLAT",
        cap=cap,
        signals=signals,
        facts=facts,
        candidates=candidates,
    )


class SplitAllocationService:
    """이 역할의 설정. 골격은 ``run_with_fallback`` 이 소유한다."""

    def __init__(self, settings: LLMSettings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def select(
        self, context: SplitAllocationContext, default_candidate_id: str
    ) -> SplitAllocationResult:
        """후보 하나를 고른다. **실패하면 규칙 기본안을 그대로 돌려준다.**

        ``default_candidate_id`` 는 규칙이 고르던 값(균등)이라, 판단자가 전면 실패해도
        산출물이 **붙이기 전과 같다** — 회귀가 아니라 무변화다.
        """
        template = SplitAllocationChoice(
            chosen_candidate_id=default_candidate_id, reason="규칙 기본안"
        )
        해석, 상태, 시도, 떨어짐 = run_with_fallback(
            settings=self.settings,
            provider=self.provider,
            context=context,
            template=template,
            validate=lambda raw: validate_choice(raw, context),
            needs_call=needs_call(context),
            guidance_for=guidance_for,
        )
        return SplitAllocationResult(
            interpretation=해석,
            llm_status=상태,
            llm_provider=self.settings.provider,
            llm_model=self.settings.model,
            llm_attempts=시도,
            llm_fallback_used=떨어짐,
        )


def make_split_selector(
    service: SplitAllocationService | None = None,
) -> SplitAllocationSelector:
    """꺼져 있거나 실패해도 **결정론 기본안을 돌려준다** — 그래프가 멈추지 않는다."""
    selection = service or _service()

    def selector(
        context: SplitAllocationContext, default_candidate_id: str
    ) -> SplitAllocationResult:
        return selection.select(context, default_candidate_id)

    return selector


def _service() -> SplitAllocationService:
    settings = get_llm_settings()
    # 🔴 **조립은 ``build_provider`` 하나가 한다.** 역할마다 베끼면 모르는 provider 일
    #   때만 갈라지는 길이 생긴다 — 그 클래스 docstring 에 실제로 밟은 자리가 있다.
    return SplitAllocationService(settings, build_provider(settings, ROLE))
