"""근거 자기 검토의 **요청·응답 계약** (E3-10).

🔴 **응답에 자유 문장이 없다.** 판단자가 고르는 것은 finding 코드와 **이미 있는 근거 id**
뿐이고, 사람이 읽는 문장은 코드가 고정 템플릿으로 만든다. 그래야 같은 코드 집합이면
``risks`` 가 바이트까지 같다.

🔴 **요청에는 정제한 원문이 들어간다.** 라벨만 주면 판단자가 «코드가 만든 범주를 다시
분류» 할 뿐이라 *"결론이 근거보다 센가"* 를 판정할 수 없다. 그래서 문장은 주되 **숫자와
날짜를 표시로 가려서** 준다 (``text_guard.sanitize_numerals``) — 값을 주면 그 값을
사유에 베껴 쓴다 (규칙 6).

⚠️ ④·⑤ 의 스키마와 섞지 않는다. 저쪽은 «후보 하나를 고른다» 이고 여기는 «지적을
목록으로 낸다» 라 모양 자체가 다르다.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.purchase_agent.schemas import EvidenceGrade, RationaleSource

#: 🔴 **어휘를 새로 짓지 않는다.** 출력 스키마가 이미 들고 있는 것을 그대로 쓴다 —
#: 두 곳에서 지으면 한쪽만 바뀌는 날이 온다. (한 번 지어 봤다가 ``MEASURED`` 라는
#: 없는 값을 만들어 검증이 울었다 — 실제 어휘는 ``OFFICIAL·VENDOR·SIM_FIXED·ASSUMED`` 다.)
EvidenceCategory = RationaleSource
EvidenceStrength = EvidenceGrade
#: 주장이 얼마나 센가. 🔴 **규칙이 어휘로 판정한다** — 판단자가 매기는 값이 아니다.
ClaimStrength = Literal["HEDGED", "NEUTRAL", "ASSERTIVE"]


class ClaimIn(BaseModel):
    """근거 한 줄. **정제된 문장과 라벨 셋**이 전부다."""

    model_config = ConfigDict(extra="forbid")

    ref_id: str = Field(min_length=1)
    evidence_category: EvidenceCategory
    evidence_strength: EvidenceStrength
    claim_strength: ClaimStrength
    #: 🔴 숫자·날짜가 ``<NUM>``·``<DATE>`` 로 가려진 원문.
    claim_text: str = Field(min_length=1)


class ReviewContext(BaseModel):
    """검토 재료. **원본 업무 숫자가 하나도 없다.**"""

    model_config = ConfigDict(extra="forbid")

    domain: Literal["PURCHASE_REVIEW"] = "PURCHASE_REVIEW"
    scenario_label: Literal["보수", "기본", "공격"]
    strategy_type: Literal["quantity", "timing", "mix"]
    #: 회차가 하나인가 여럿인가. 숫자 대신 라벨로 준다.
    round_count: Literal["SINGLE", "MULTI"]
    claims: list[ClaimIn]
    #: 이 안에 **이미 적힌** 위험의 범주. 빠진 것을 물으려면 있는 것을 알아야 한다.
    risk_categories: list[str]
    #: 사전검사가 켠 신호 라벨.
    signals: list[str]
    #: ⑤ 가 등급 조합을 고른 사유 (정제본). 안 돌았으면 ``None`` — **빈 문자열이 아니다.**
    mix_reason: str | None = None
    #: 🔴 고를 수 있는 코드 목록. **이 밖은 거부한다.**
    offered_findings: list[str]


class FindingOut(BaseModel):
    """지적 하나. **문장도 숫자도 없다.**"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    #: 어느 근거를 가리키는가. 🔴 **코드마다 필수/선택이 갈린다** —
    #: 가리킬 근거가 없는 지적까지 필수로 두면 판단자가 아무 id 나 붙인다.
    target_ref_id: str | None = None


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[FindingOut]


class ReviewResult(BaseModel):
    """노드로 돌아가는 결과. 상태 5종은 팀 공통이다."""

    model_config = ConfigDict(extra="forbid")

    output: ReviewOutput
    llm_status: Literal["SUCCESS", "SKIPPED_TEMPLATE", "FALLBACK", "DISABLED"]
    llm_provider: str | None
    llm_model: str | None
    llm_attempts: int = Field(ge=0)
    llm_fallback_used: bool
