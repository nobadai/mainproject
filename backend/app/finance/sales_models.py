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

from app.finance.schemas import CashflowProjection, _reject_boolean
from app.orchestrator.contracts_core import EvidenceGrade

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
