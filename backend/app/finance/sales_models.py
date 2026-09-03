"""판매 재무 검증의 **Finance 내부 전용** 도메인 모델.

이 파일이 소유하는 것
    판매 원가 기준 구성요소 · 원가 산출방식 어휘 · 내부 계산 입력/결과 모양

여기 **없는 것**
    계산 · 판정 · 외부 계약
    → `tools` · `rules` · `schemas` 소유다.

★ `schemas.py` 와 갈라놓은 이유는 **읽는 사람이 다르기 때문이다.** `schemas.py` 는
  프론트 · Master · Critic 이 읽는 요청/회신 계약이고, 거기 놓인 모양은 그것만으로
  외부 정본처럼 굳는다. 이 파일의 모양은 Finance 안에서만 산다 — Router 응답에도,
  Master AgentRequest/AgentReply 에도 실리지 않는다. 밖으로 낼 것이 생기면 그때
  `schemas.py` 에 외부 계약을 따로 세운다.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.contracts.core import EvidenceGrade
from app.finance.rules import SalesRuleResult
from app.finance.schemas import (
    CashflowProjection,
    FinalVerdict,
    RuntimeStatus,
    _reject_boolean,
)

# ---------------------------------------------------------------------------
# 원가 산출방식 — EvidenceGrade 와 **다른 축이다**
#
# EvidenceGrade(OFFICIAL·VENDOR·SIM_FIXED·ASSUMED·INVALID_FOR_HARD) 는 "그 숫자를
# 얼마나 믿을 수 있는 출처에서 얻었나"이고, 아래 어휘는 "그 원가를 어떤 방식으로
# 산출했나"이다. 두 축은 동시에 성립한다 — 예를 들어
#
#     cost_method = ACTUAL, evidence_grade = OFFICIAL
#
# 은 모순이 아니라 가장 흔한 조합이다. 둘을 한 필드로 합치면 "실제원가인데 근거가
# 약함"과 "표준원가인데 근거가 공식"을 구분할 수 없게 된다.
#
# ★ 여기서 우선순위(ACTUAL > STANDARD > …)로 후보를 **고르지 않는다.** 어느 원가가
#   정본인지 정하는 계약이 아직 도메인 간에 없다. 이 어휘는 들어온 값이 무엇인지
#   기록만 하고, 선택은 권위 있는 입력을 만드는 바깥이 한다.
# ---------------------------------------------------------------------------

#: 원가를 어떤 방식으로 산출했는지. UNKNOWN 은 "0원"이 아니라 "모른다"이다.
SalesCostMethod = Literal["ACTUAL", "STANDARD", "SIM_FIXED", "UNKNOWN"]


class InventoryCostBasis(BaseModel):
    """이미 권위 있는 것으로 선택되어 Finance 에 들어온 재고원가.

    Finance 가 후보를 고르지 않는다 — 어느 재고/매입 원가가 정본인지는 아직
    도메인 간 계약이 없다. 이 모델은 **선택이 끝난 값**만 받는다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_krw: Decimal = Field(ge=0)
    cost_method: SalesCostMethod
    #: 이 금액이 이미 품고 있는 원가 구성요소 이름. 직접비 중복 계상 차단에 쓴다.
    included_components: tuple[str, ...] = ()
    source_ref: str = Field(min_length=1)
    evidence_grade: EvidenceGrade

    @field_validator("amount_krw", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        return _reject_boolean(value)

    @field_validator("included_components")
    @classmethod
    def reject_blank_or_duplicate_components(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() for name in value):
            raise ValueError("included_components must not contain blank names")
        if len(set(value)) != len(value):
            raise ValueError("included_components must not repeat a component")
        return value


class VerifiedDirectCost(BaseModel):
    """재고원가 밖에서 검증된 직접비 1건. 추정치·LLM 산출값은 들어올 수 없다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1)
    amount_krw: Decimal = Field(ge=0)
    cost_method: SalesCostMethod
    source_ref: str = Field(min_length=1)
    evidence_grade: EvidenceGrade

    @field_validator("amount_krw", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        return _reject_boolean(value)

    @field_validator("component")
    @classmethod
    def reject_blank_component(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("component must not be blank")
        return value


class SalesCostBasis(BaseModel):
    """합성된 판매 원가 기준. 어떤 숫자가 어디서 왔는지까지 같이 남긴다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_krw: Decimal = Field(ge=0)
    inventory_amount_krw: Decimal = Field(ge=0)
    inventory_cost_method: SalesCostMethod
    inventory_source_ref: str = Field(min_length=1)
    inventory_evidence_grade: EvidenceGrade
    #: 실제로 더해진 직접비만 남는다.
    added_direct_costs: tuple[VerifiedDirectCost, ...] = ()
    #: 이미 재고원가에 포함돼 있어 더하지 않은 구성요소 — 중복 계상을 막았다는 기록.
    already_included_components: tuple[str, ...] = ()
    included_components: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)


class SalesScenarioCashflow(BaseModel):
    """BASE 와 SCENARIO 를 나란히 보존하는 판매 시나리오 현금흐름 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_projection: CashflowProjection
    scenario_projection: CashflowProjection
    base_projected_cash_min: Decimal
    base_projected_cash_min_date: date
    scenario_projected_cash_min: Decimal
    scenario_projected_cash_min_date: date
    collection_date: date
    collection_amount_krw: Decimal = Field(ge=0)
    proposed_collection_ref_id: str = Field(min_length=1)
    #: 회수일이 현재 Finance projection horizon 안인가. 밖이면 SCENARIO 는 BASE 와 같다.
    collection_within_horizon: bool
    #: SCENARIO 최저 현금이 **제안 유입 덕분에** 올라갔는가.
    #: 정의: scenario_projected_cash_min > base_projected_cash_min.
    #: True 면 그 현금 여력은 아직 확정되지 않은 돈에 기대고 있다는 뜻이다.
    depends_on_projected_inflow: bool


# ---------------------------------------------------------------------------
# Sales Core Phase 5 — 매출채권 사실
#
# ★ `receivables` 원장은 실재하고 Finance 가 이미 읽는다. 반면 **여신한도는 저장소
#   어디에도 없다** — `partners` 에도, `agent_policy_config` 에도, 어떤 테이블에도
#   credit_limit 컬럼이 없다. 그래서 채권 사실은 계산하되 여신 판정은 닫는다.
#
# ★ 빈 목록은 "채권이 없다"는 **사실**이고 "자료를 못 받았다"가 아니다. 둘을 섞지
#   않으려고 목록을 항상 명시적으로 받는다.
# ---------------------------------------------------------------------------

#: 미회수로 남아 있는 채권 상태. COLLECTED·WRITEOFF 는 잔액에 넣지 않는다.
OPEN_RECEIVABLE_STATUSES: frozenset[str] = frozenset({"OPEN", "PARTIAL"})


class PartnerReceivable(BaseModel):
    """거래처 채권 1건. `receivables` 원장 행을 Finance 안으로 옮긴 모양이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receivable_id: str = Field(min_length=1)
    due_date: date
    outstanding_amount_krw: Decimal = Field(ge=0)
    status: Literal["OPEN", "PARTIAL", "COLLECTED", "WRITEOFF"]
    source_ref: str = Field(min_length=1)

    @field_validator("outstanding_amount_krw", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        return _reject_boolean(value)


class PartnerReceivableFacts(BaseModel):
    """거래처 채권 집계 — **사실만이다.** 위험도 점수도, 판정도 들어있지 않다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    partner_id: str = Field(min_length=1)
    as_of: date
    current_ar_krw: Decimal = Field(ge=0)
    overdue_ar_krw: Decimal = Field(ge=0)
    open_receivable_count: int = Field(ge=0)
    overdue_receivable_count: int = Field(ge=0)
    source_refs: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Sales Core Phase 6 — Finance 내부 판매 검증 입력/결과
#
# ★ Sales 의 Pydantic API 모델을 Finance 정본으로 쓰지 않는다. 편해 보이지만 그
#   순간 Sales 의 화면 계약이 Finance 판정의 계약이 된다 — 저쪽이 필드 하나를
#   바꾸면 이쪽 판정이 조용히 바뀐다. Finance 는 **자기가 쓰는 사실만** 자기 모양
#   으로 갖는다. Master 가 Sales 회신 payload 를 통째로 넘겨도 여기서 필요한
#   부분집합만 엄격히 검증한다.
# ---------------------------------------------------------------------------

#: 결제 방식. INSTALLMENT 는 권위 있는 분할결제 정책이 없어 판정되지 않는다.
SalesPaymentTermsType = Literal["SINGLE", "INSTALLMENT"]


class SalesSupply(BaseModel):
    """공급 확정/조건부 구분.

    ★ 조건부 물량을 확정 재고인 것처럼 원가에 넣지 않으려고 나눠 받는다.
      확정 재고원가는 확정 물량에 대한 사실이지 제안 전체에 대한 사실이 아니다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed_quantity_kg: Decimal = Field(ge=0)
    conditional_quantity_kg: Decimal = Field(default=Decimal(0), ge=0)
    dependency_ref: str | None = None

    @field_validator("confirmed_quantity_kg", "conditional_quantity_kg", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesValidationInput(BaseModel):
    """Finance 가 판매 제안 1건을 검증하는 데 실제로 쓰는 사실만 담는다.

    envelope 이 소유하는 것(request_id · as_of · mode · call_seq · run 계보)은
    여기 두지 않는다 — 정본을 두 벌 만들면 어느 쪽이 맞는지 알 수 없게 된다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1)
    partner_id: str = Field(min_length=1)
    item: str = Field(min_length=1)
    quantity_kg: Decimal = Field(ge=0)
    unit_price_krw: Decimal = Field(ge=0)
    reported_sales_amount_krw: Decimal = Field(ge=0)
    payment_terms_type: SalesPaymentTermsType
    #: null 은 0 도 "제한 없음"도 아니다 — 결제일수를 못 받았다는 사실이다.
    payment_days: int | None = Field(default=None, ge=0)
    #: 회수일 기준점. 그 의미(납품일·송장일·계약일)는 여전히 호출자가 소유한다.
    collection_reference_date: date | None = None
    supply: SalesSupply | None = None
    inventory_cost_basis: InventoryCostBasis | None = None
    direct_costs: tuple[VerifiedDirectCost, ...] = ()
    source_ref: str = Field(min_length=1)

    @field_validator("quantity_kg", "unit_price_krw", "reported_sales_amount_krw", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        return _reject_boolean(value)


class SalesFinancialSummary(BaseModel):
    """계산된 사실만. 못 구한 값은 0이 아니라 None 이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recalculated_sales_amount_krw: Decimal
    reported_sales_amount_krw: Decimal
    amount_difference_krw: Decimal
    amount_match: bool
    sales_cost_basis_krw: Decimal | None = None
    contribution_margin_krw: Decimal | None = None
    contribution_margin_rate: Decimal | None = None
    collection_date: date | None = None
    current_partner_ar_krw: Decimal | None = None
    projected_partner_ar_krw: Decimal | None = None
    credit_limit_krw: Decimal | None = None
    available_credit_krw: Decimal | None = None
    overdue_ar_krw: Decimal | None = None
    base_projected_cash_min: Decimal | None = None
    scenario_projected_cash_min: Decimal | None = None
    depends_on_projected_inflow: bool | None = None
    collection_within_horizon: bool | None = None


class SalesValidationResult(BaseModel):
    """Finance 내부 판매 검증 결과 — Master AgentReply 가 아니다.

    Refeed 를 견디도록 **자기 완결적**으로 만든다. 판정을 만든 근거(개별 규칙 ·
    reason code · 없는 데이터/정책 · Evidence 계보)를 임시 상태에 숨기지 않는다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str | None
    runtime_status: RuntimeStatus
    #: INPUT_INCOMPLETE 는 Finance 고장이 아니라 제안에 사실이 빠졌다는 뜻이다.
    status: Literal["EVALUATED", "INPUT_INCOMPLETE", "RUNTIME_NOT_READY", "ERROR"]
    finance_verdict: FinalVerdict | None
    financial_summary: SalesFinancialSummary | None = None
    rule_results: tuple[SalesRuleResult, ...] = ()
    reason_codes: tuple[str, ...] = ()
    max_finance_allowed_amount_krw: Decimal | None = None
    max_finance_allowed_payment_terms_days: int | None = None
    missing_fields: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
