"""매입 에이전트 출력 제안 JSON 스키마 (IO명세 v1.1 §2).

오케스트레이터·Critic·재무가 함께 의존하는 **출력 계약**이다. 필드명과 불변조건의 단일
소스는 ``docs/매입에이전트_IO명세_v1.1.md`` §2 "필드 규약" 표.

read-only (CLAUDE.md 규칙 2): 반환값의 형태만 정의한다 — 이 모듈은 DB에 쓰지 않는다.

이 스키마가 **강제하지 않는** 조항 (전부 ⑦ self_check 몫 — 런타임 컨텍스트가 있어야 판정 가능):

* ``strategy_type`` 이 그날 ``allowed_axes`` 안의 값인가 → ① 노드가 계산한 목록 필요
* 전 안이 동일 축이면 반려 → 시나리오 1개일 땐 무의미. variant_collapsed 판정은 T3
* ``coverage_days`` 가 D 범위 안인가 → 임계는 ``constraints.yaml`` 단일 소스(규칙 7).
  여기에 ``[2, 18]`` 을 박으면 임계가 두 곳에 존재하게 된다
* ``grade_unit_price`` 가 당일 시세에 실재하는 값인가 → 당일 market_quotes 대조 필요
* 재고·현금·재무 cap 대조 → 부서 밴드·T0 스냅샷 필요
* 근거 환각 대조 → 원문 문서 필요

⚠️ **finance 측 kg 전환 반영 대기.** 수량 단위는 **kg 통일로 확정**됐다(팀 결정).
아래 표는 전환이 끝날 때까지의 임시 대조표다 — **전환 완료 시 이 표는 삭제한다.**

현재 이 출력을 받는 소비자가 셋이고, 그중 부서 DTO 둘이 아직 ton이다::

    소비자 (진입점)                                    수량 필드                축 필드
    -----------------------------------------------  ----------------------  -------------
    orchestrator/contracts_core.py:PurchaseScenario   qty_kg                  strategy_type
      Protocol v1.2 — band 클리핑·critic이 사용         unit_price_krw_per_kg   ← kg, 정합
    finance·logistics:PurchaseAgentScenario (v0.4)    total_quantity_ton      strategy_type
      POST /finance|logistics/procurement             quantity_ton            ← ton
    finance/schemas.py:PurchaseScenario (v0.3)        total_quantity_ton      timing
      POST /finance/core (review_finance_core)        unit_price              ← ton, 구판

**v0.4에서 정합이 끝난 것**: ``coverage_days`` · ``strategy_type`` · ``grade_unit_price`` ·
``total_amount_krw``. 이름도 의미도 일치한다 (v0.3에서는 각각 없음 / ``timing`` /
``unit_price`` / ``expected_cost`` 였다).

**남은 차이는 수량 필드 3개뿐이다** — finance·logistics v0.4 기준::

    finance·logistics (v0.4)              IO명세 v1.1 (이 파일)
    ------------------------------------  ---------------------------
    total_quantity_ton: Decimal           total_qty_kg: int
    SplitPlanItem.quantity_ton            SplitPlanItem.qty_kg
    PurchaseSourcingPlanItem.quantity_ton SourcingPlanItem.qty_kg

전 모델이 ``extra="forbid"`` 라 지금 그대로 보내면 422로 거부된다. 덧붙여 v0.4 쪽에는
``max_price`` · ``margin_warning`` · ``expected_margin_rate`` · ``rationale`` · ``risks``가
아예 없다 — 부서가 안 보는 필드인지 누락인지 확인이 필요하다.

⚠️ **거부되는 것보다 변환 코드가 더 위험하다.** 나중에 옮겨 담는 코드가 생기면
**1000배 오차가 조용히 통과**한다. ``finance/tools.py``는
``quantity_ton × KG_PER_TON × grade_unit_price``로, 우리는 ``qty_kg × grade_unit_price``로
계산한다 — 결과는 같지만 입력 단위가 다르다.

전환 방향은 **부서 DTO를 kg로** 맞추는 쪽이다. 팀 코어 계약인
``contracts_core.py:PurchaseScenario``가 이미 ``qty_kg`` · ``unit_price_krw_per_kg``라,
kg로 모으면 세 소비자가 한 단위로 정렬된다. 이 파일은 그대로 IO명세 v1.1(kg)을 따른다.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

# 수량 단위가 kg이므로 금액은 ``qty_kg × grade_unit_price(원/kg)``로 곧바로 원이 된다.
# ton 시절의 ``× 1000`` 변환 계수(KG_PER_TON)는 더 이상 필요하지 않다.
#
# 수량·금액을 Decimal이 아니라 **int**로 두는 이유: IO명세 §2가 ``total_qty_kg``를
# ``integer``로, "정수 kg — 소수 불허"로 규정한다(도매 매입 단위). 정수 kg × 정수 원/kg은
# 언제나 정수 원이므로, 사중 일치가 정수 연산으로 정확히 떨어지고 float 직렬화 오차가
# 들어올 자리 자체가 없어진다. Decimal을 쓰던 시절 필요했던 커스텀 직렬화기도 사라진다.

#: 공백만 든 문자열을 거부한다. ``min_length=1``은 "   "을 통과시켜 ref_id 필수 조항이
#: 우회된다 — strip 후 길이를 재고, 값 자체도 trim된 상태로 보관한다.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# --- 고정 어휘 (IO명세 §2 필드 규약) ---
ItemName = Literal["배추", "무", "피마늘", "양파"]
ScenarioLabel = Literal["보수", "기본", "공격"]
StrategyType = Literal["quantity", "timing", "mix"]
RationaleSource = Literal["예측", "시세관측", "재고", "주문", "현금", "문서ID"]
Confidence = Literal["high", "medium", "low"]
Situation = Literal["stable", "uncertain"]
#: market 고정값 (IO명세 §1-②, 8/20 결정 — 지방시장 데이터 파편화로 제외).
#: 아래 ``Market`` Literal과 **같은 값이어야 한다** — 검사 코드가 문자열을 필요로 해서
#: 상수로도 노출한다. 둘이 갈라지지 않는지는 계약 테스트가 확인한다.
FIXED_MARKET = "가락"
Market = Literal["가락"]

#: 근거 등급 4단계 (정의서 §7.3). 서열: OFFICIAL > VENDOR > SIM_FIXED > ASSUMED
#:
#: * ``OFFICIAL``   공공기관·법령·공개 통계        → 하드 제약 사용 허용
#: * ``VENDOR``     업체 공개 견적·계약서          → 허용
#: * ``SIM_FIXED``  팀이 백테스트 전 확정 선언한 값 → 허용
#: * ``ASSUMED``    근거 없는 임시값·파생값        → 소프트 경고만 (하드 제약 사용 불가)
#:
#: 우리는 등급을 **정확히 채우는 데까지** 책임진다. "낮은 등급으로 하드 제약을 계산했는가"의
#: 검사는 오케스트레이터의 ``check_evidence_grade()`` 몫이다.
#:
#: ⚠️ SIM_FIXED 요건 4번 = 제약 독립성(정의서 §7.1). 매입 값 중 **수요에서 파생된 것은
#: SIM_FIXED 자격을 잃고 ASSUMED가 된다.** 등급을 매길 때 "이 값이 규율 대상에서
#: 파생됐는가"를 자문할 것.
EvidenceGrade = Literal["OFFICIAL", "VENDOR", "SIM_FIXED", "ASSUMED"]


def _reject_boolean(value: object) -> object:
    """bool은 int의 서브클래스라 ge/gt 검사를 그냥 통과한다 — 숫자 자리에서 막는다."""
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid numeric inputs")  # noqa: TRY004
    return value


class ProposalMeta(BaseModel):
    """IO명세 §2 ``meta``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: date
    item: ItemName
    agent_version: NonEmptyStr
    is_refeed: bool = False
    feedback_attempt: int = Field(default=0, ge=0)

    @field_validator("feedback_attempt", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SplitPlanItem(BaseModel):
    """분할 매입 1회차.

    ``date`` 는 **매입 실행일**이다 — 도착일이 아니다. 실제 도착일은
    ``date + inbound_lead_days(N4)`` 이고 N4는 현재 미결이라 계산하지 않는다 (IO명세 §4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    date: date
    qty_kg: int = Field(gt=0)

    @field_validator("seq", "qty_kg", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class SourcingPlanItem(BaseModel):
    """등급 배분 1건. 등급·단가는 당일 시세에 실재하는 값만 쓴다 (대조는 self_check)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: Market = "가락"
    #: 가락 경락 원문 기준(특/상/중/하). DB 담당과 표준화 진행 중이라 Literal로 굳히지 않는다.
    grade: NonEmptyStr
    qty_kg: int = Field(gt=0)
    grade_unit_price: int = Field(gt=0)  # 원/kg

    @field_validator("qty_kg", "grade_unit_price", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)


class RationaleItem(BaseModel):
    """근거 1건. **ref_id 없는 근거는 근거가 아니다** (규칙 4 · 정의서 §1.2-5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: RationaleSource
    claim: NonEmptyStr
    ref_id: NonEmptyStr
    evidence_grade: EvidenceGrade
    #: 등급으로 정규화하기 전의 원본 값을 보존한다 — 나중에 재분류할 때 되돌릴 수 있도록.
    evidence_detail: NonEmptyStr


class RejectedReason(BaseModel):
    """self_check가 컷한 이력. 데모에서 "검증이 실제로 작동한다"를 보이는 증거다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: NonEmptyStr
    reason: NonEmptyStr


class Scenario(BaseModel):
    """시나리오 1안 (IO명세 §2 ``scenarios[]``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: ScenarioLabel
    strategy_type: StrategyType
    #: 커버일수 D. 수량 = 확정수요 × D. 범위 검사는 constraints.yaml을 읽는 self_check 몫.
    coverage_days: int = Field(gt=0)
    total_qty_kg: int = Field(gt=0)
    total_amount_krw: int = Field(ge=0)
    #: q90 기반 하드 상한(경락가). **마진 방어선과 무관** — 마진 쪽 표시는 margin_warning.
    max_price: int = Field(ge=0)
    #: 매입단가가 contract_price 방어선을 넘었다는 **표시**. 컷이 아니다(영업이 T2에서 판정).
    #:
    #: 규칙 3(0/NULL 구분)을 bool에 적용한 것 — ``None``은 "아직 계산되지 않음"이다.
    #: 계약단가·방어선은 T0 스냅샷이 주는 입력값이라 없을 수 있고, 그때 ``False``로 채우면
    #: "확인했더니 문제 없음"과 구분되지 않아 경고가 조용히 사라진다.
    margin_warning: bool | None = None
    split_plan: list[SplitPlanItem] = Field(min_length=1)
    sourcing_plan: list[SourcingPlanItem] = Field(min_length=1)
    #: v1.1 개정 — ``margin_warning``과 같은 정보 가족이다(둘 다 contract_price 파생).
    #: 미계산이면 ``null``. **0.0으로 채우지 않는다** — "마진 0%"는 거짓이고, 이건
    #: 규칙 3(0과 NULL 구분)의 float 판이다. 기본값이 None인 것도 같은 이유다.
    expected_margin_rate: float | None = Field(default=None, ge=0, le=1)
    rationale: list[RationaleItem] = Field(min_length=1)
    risks: list[NonEmptyStr] = Field(default_factory=list)

    @field_validator("coverage_days", "max_price", "expected_margin_rate", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        return _reject_boolean(value)

    @model_validator(mode="after")
    def validate_margin_fields_are_synchronised(self) -> "Scenario":
        """``margin_warning``과 ``expected_margin_rate``의 null 여부가 일치해야 한다.

        둘 다 ``contract_price``에서 나온다 — 계약단가를 받았으면 둘 다 계산되고, 못 받았으면
        둘 다 미계산이다. **한쪽만 null이면 모순**이라 소비자가 어느 쪽을 믿어야 할지 알 수 없다
        (IO명세 §2 "동기화 규칙").

        이 판정은 스키마 몫이다 — 출력 문서 안의 두 필드만 보면 되고 런타임 값이 필요 없다.
        (같은 이유로 축 중복 검사는 ⑦ self_check에 있다. 그쪽은 그날 ``allowed_axes``를
        알아야 판정할 수 있어 문서만으로는 불가능하다.)
        """
        warning_missing = self.margin_warning is None
        rate_missing = self.expected_margin_rate is None
        if warning_missing != rate_missing:
            computed = "margin_warning" if rate_missing else "expected_margin_rate"
            missing = "expected_margin_rate" if rate_missing else "margin_warning"
            raise ValueError(
                f"margin_warning and expected_margin_rate must both be null or both set; "
                f"{computed} is set but {missing} is null"
            )
        return self

    @model_validator(mode="after")
    def validate_quadruple_match(self) -> "Scenario":
        """사중 일치 — 수량 3축 + 금액 1축 (규칙 4 · IO명세 §2).

        금액 축이 없으면 T3가 재무 cap(금액)과 매입 제안(수량)을 결합할 수 없다.
        등급 배분이 수량↔금액 변환 계수이기 때문이다.
        """
        split_total = sum(item.qty_kg for item in self.split_plan)
        sourcing_total = sum(item.qty_kg for item in self.sourcing_plan)
        if self.total_qty_kg != split_total:
            raise ValueError("total_qty_kg must equal split_plan quantity total")
        if self.total_qty_kg != sourcing_total:
            raise ValueError("total_qty_kg must equal sourcing_plan quantity total")

        # kg × 원/kg = 원. 단위가 맞아떨어져 변환 계수가 없다 (상세설계 §4-⑦).
        amount_total = sum(item.qty_kg * item.grade_unit_price for item in self.sourcing_plan)
        if self.total_amount_krw != amount_total:
            raise ValueError("total_amount_krw must equal sourcing_plan amount total")
        return self

    @model_validator(mode="after")
    def validate_split_sequence(self) -> "Scenario":
        """분할 회차는 1부터 1씩 증가한다. 일괄 매입이면 seq 1개짜리 목록."""
        if [item.seq for item in self.split_plan] != list(range(1, len(self.split_plan) + 1)):
            raise ValueError("split_plan seq must start at 1 and increase by 1")
        return self


class PurchaseProposal(BaseModel):
    """에이전트의 **유일한 산출물** (IO명세 §2).

    소비 경로: 오케스트레이터(조정) → Critic(대조) → 승인 → proposals 적재.
    적재는 실행 스크립트 몫이다 — 이 에이전트는 반환만 한다(규칙 2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: ProposalMeta
    scenarios: list[Scenario] = Field(default_factory=list, max_length=3)
    confidence: Confidence | None = None
    situation: Situation | None = None
    context_docs_used: list[NonEmptyStr] = Field(default_factory=list)
    rejected_reasons: list[RejectedReason] = Field(default_factory=list)
    #: 제안 불가 사유. **"유효 시나리오 없음"이라는 사실만 반환한다** — 납품 의무 미충족
    #: 판정(has_unmet_obligation)은 오케스트레이터 몫이다. 매입 0 ≠ 납품 실패(IO명세 §2).
    no_proposal_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_proposal_rules(self) -> "PurchaseProposal":
        # scenarios와 no_proposal_reason은 상호 배타다 — "안이 있는데 제안 불가"는 모순이다.
        if not self.scenarios:
            if not self.no_proposal_reason:
                raise ValueError("empty scenarios require no_proposal_reason")
            return self
        if self.no_proposal_reason:
            raise ValueError("no_proposal_reason must be absent when scenarios exist")

        if self.situation is None or self.confidence is None:
            raise ValueError("situation and confidence are required when scenarios exist")

        labels = [scenario.label for scenario in self.scenarios]
        if len(labels) != len(set(labels)):
            raise ValueError("scenario labels must be unique")

        # 규칙 4: uncertain이면 공격안 금지 — 보수/기본 2안만 낸다.
        if self.situation == "uncertain":
            if len(self.scenarios) > 2:
                raise ValueError("uncertain situation allows at most 2 scenarios")
            if "공격" in labels:
                raise ValueError("uncertain situation forbids the 공격 scenario")

        # IO명세 §2: split_plan seq 1의 date는 as_of다 (첫 회차는 오늘 실행).
        for scenario in self.scenarios:
            if scenario.split_plan[0].date != self.meta.as_of:
                raise ValueError("split_plan seq 1 date must equal meta.as_of")
        return self

    # 전 안 동일 축 검사는 여기 없다 — ⑦ self_check.check_axis_diversity() 몫이다.
    #
    # 정의서 §3.5.1이 "3) 코드가 최종 중복 검사 — 전 안이 동일 축이면 반려 (self_check)"로
    # 소유자를 명시한다. 판정에 그날 ``allowed_axes``가 필요한 게 이유다: 하락일처럼 timing
    # 트리거가 미달하고 mix가 편중으로 게이팅되면 남는 축이 quantity 하나뿐이라, 3안이 전부
    # quantity인 게 정상이다. 출력 JSON만 보고는 그날 축이 하나였는지 알 수 없으므로 스키마는
    # 이 판정을 내릴 자격이 없다.
    #
    # (이력: Epic 1에서 여기에 두었다가 Epic 2에서 mock_falling이 스키마에 막혀 제안 자체를
    # 만들지 못하는 것으로 반증됐다.)

    @model_serializer(mode="wrap")
    def _omit_null_no_proposal_reason(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        """정상 제안에는 ``no_proposal_reason`` 키 자체를 싣지 않는다 (IO명세 §2 정상 예시).

        validator가 "시나리오가 있으면 이 필드는 없어야 한다"고 규정하는데 출력에는
        ``null``이 실려 나가면 말과 결과가 어긋난다.

        ``margin_warning``의 ``null``은 **"아직 계산되지 않음"이라는 정보**를 담으므로
        그대로 둔다 — 여기서 빼는 것은 이 필드 하나뿐이고, ``exclude_none``으로
        일괄 처리하지 않는 이유가 그것이다.
        """
        data = handler(self)
        if data.get("no_proposal_reason") is None:
            data.pop("no_proposal_reason", None)
        return data


def revalidate_for_output(proposal: PurchaseProposal) -> PurchaseProposal:
    """출력 직전, 원시 데이터에서 모델을 다시 세워 계약을 재확인한다.

    ``frozen=True``가 필드 재대입을 막지만 **리스트 자체는 여전히 가변**이다.
    ``proposal.scenarios[0].rationale.append({"source": ...})`` 처럼 리스트에 값을 끼워
    넣는 경로는 어떤 validator도 거치지 않는다. 원시 dict로 내렸다가 다시 올리면 그렇게
    끼어든 값도 전부 검증을 다시 통과해야 한다.

    ⑦ self_check의 첫 단계로 재사용할 함수다 — 노드가 만든 제안을 내보내기 전에 통과시키고,
    ``ValidationError``가 나면 **직렬화하지 않는다.** "검증을 통과한 객체"가 아니라
    "지금 이 순간의 값"이 계약을 만족하는지가 판단 기준이어야 한다.
    """
    return PurchaseProposal.model_validate(proposal.model_dump())
