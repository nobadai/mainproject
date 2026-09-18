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

# 🔴 **「판단 기준」 네 줄을 지웠다** (2026-09-18 · 검증설계 v0.2 §4 · 결정 ①).
#   네 줄이 **전부 못 섰다** — 감사 판정이 이렇다:
#     「TREND_RISING 이면 앞 회차가 단가에 유리」  거짓. 회차 금액은 ``grade_unit_price``
#         스칼라 하나로 계산되고 마지막 회차가 잔량을 흡수해 **총액이 배분과 무관**하다.
#         ⑦ 의 사중 일치가 그 항등식을 잠그고, 그래프 순서상 여기서는 단가가 아직 안 붙었다
#     「앞에 실을수록 창고에서 더 오래 늙는다」      확인 못 함. 로트 나이를 재는 계산이 없다
#     「CAP_TIGHT 면 앞에 몰기 어렵다」              무용. 못 모는 날은 ``FRONT_LOADED`` 가
#         사전검사에서 이미 빠져 후보에 없다
#     「CAP_UNKNOWN 은 못 봤다는 뜻」                 도달 불가. 그 라벨이 붙는 날은 후보가
#         하나라 ``needs_call`` 이 막는다
#   ★ 실측이 그것을 그대로 보여 줬다 — 거짓 지시가 심은 「단가에 유리」가 판단자 사유로
#     되돌아왔다 (E3-9 2차 기준선 · PS-01 스무 사유 전부가 입력에 없는 이득을 주장했다).
#   ⇒ **라벨만 주고 「안전한 후보 중 고르라」로 둔다.** 고칠 문장이 없으니 지운다.
#
# ⚠️ **「이유를 쓰라」는 틀은 이번에 안 건드린다** (충환 2026-09-18). 이득어가 지시와
#   함께 사라지는지, 아니면 말만 바꿔 남는지를 다음 판이 잰다 — 같이 고치면 못 가른다.
SYSTEM_PROMPT = """당신은 매입 에이전트의 분할 회차 배분 판단 레이어다.
계산은 이미 끝났다. 규칙이 만든 후보 중 **하나를 고르고 이유를 쓰는 것**이 전부다.

규칙:
- candidates 에 있는 candidate_id 만 고른다. 없는 id 를 지어내지 않는다.
- reason 에 숫자를 쓰지 않는다. 비율도 수량도 날짜도 쓰지 않는다.
- reason 은 한국어 한 문장이다.

후보는 규칙이 **안전 검사까지 끝낸 것**이다 — 어느 것을 골라도 제약을 안 넘는다.
그중 하나를 고른다.
"""

#: 🔴 **지시문이 바뀌면 판 이름도 바뀐다.** ``llm_calls`` 에 남는 ``prompt_version`` 의
#: 뜻이 *"이 판으로 물어봤다"* 라, 문면을 고치고 이름을 두면 **기준선과 고친 뒤가 같은
#: 이름을 달고** 전후를 못 가른다 (v0.2 §5-4 가 재는 단위가 그 전후다).
#: ⚠️ ``schema_version`` 은 그대로다 — 응답 계약은 안 건드렸다.
ROLE = RoleSpec(
    system_prompt=SYSTEM_PROMPT,
    response_schema=SplitAllocationChoice.model_json_schema(),
    prompt_version="split-alloc-2",
    schema_version="split-alloc-1",
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
    raw_output: str, context: SplitAllocationContext, *, reason_max_chars: int
) -> SplitAllocationChoice:
    """프로바이더 밖의 공통 관문. **어느 API 를 쓰든 같은 문을 지난다.**

    🔴 ``chosen_candidate_id`` 는 **숫자 검사 대상이 아니다.** 후보 id 는 규칙이 만든
    식별자라 숫자가 들어갈 수 있다 — 거기 걸면 정상 선택이 매번 fallback 으로 떨어진다.
    검사는 **자연어인 ``reason``** 에만 건다 (⑤ 와 같은 경계).

    🔴 **``reason_max_chars`` 는 인자다 — 이 파일에 상수로 안 둔다** (2026-09-18 ·
    규칙 7). 전에는 여기 ``REASON_MAX_CHARS = 300`` 이 있었고 ⑤ 는 선언
    (``constraints.yaml`` 의 ``grade.mix_reason_max_chars``)을 읽어, **같은 뜻의 값이 두
    곳에 살았다.** 값이 같아서 안 아팠을 뿐이고, 선언을 옮기는 날 ⑤ 만 따라가고 ④ 는
    옛 값에 남는다 — 「단일 소스」가 깨지는 전형이다.

    ★ **새 키를 안 팠다.** 두 사유는 같은 칸(``rationale[].claim``)에 같은 모양으로
    실려 같은 화면이 읽는다 — 상한이 갈릴 근거가 지금 0이다. 여기서 키를 하나 더 파면
    「단일 소스로 만들겠다」면서 소스를 둘로 늘리는 셈이고, 그 둘이 같은 값이라 갈려도
    안 아프다. 갈라야 할 날이 오면 **그날 판다** (`#379` 의 「죽은 선언」과 같은 결).
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
    if len(choice.reason) > reason_max_chars:
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
            # 🔴 상한은 **설정이 들고 온다** — 설정은 ``constraints.yaml`` 을 읽는다
            #   (``get_llm_settings``). ⑤ 와 같은 선언을 같은 경로로 지난다.
            validate=lambda raw: validate_choice(
                raw, context, reason_max_chars=self.settings.reason_max_chars
            ),
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
