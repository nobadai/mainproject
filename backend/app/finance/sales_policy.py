"""Finance/Sales MVP Policy v0.1 — 판매 재무 판정에 쓰는 실행 정책.

이 파일이 소유하는 것
    판매 마진 임계값 · 최대 결제일수 · 지원 결제방식 · 회수위험 판정 방식과
    그 값들의 **성격**(버전 · 상태 · 사용범위 · 근거등급 · 결정 ref)

여기 **없는 것**
    판정 로직 (`rules`) · 계산 (`tools`) · 조회 (`db`)

★ **정책값을 함수 안에 흩뿌리지 않는다.** 예전에는 capability 가 전부 `None` 을
  넘겨 판정이 늘 RUNTIME_NOT_READY 로 닫혔다. 그렇다고 `if rate < Decimal("0.2642")`
  처럼 규칙 안에 숫자를 박으면, 값이 바뀔 때 어디를 고쳐야 하는지 아무도 모른다.
  값과 그 값의 성격을 한곳에 모아 두고 규칙은 받아서 쓴다.

★ **`PROVISIONAL` 과 `SIM_FIXED` 는 다른 축이다.**
      PROVISIONAL — 향후 재산정될 수 있다 (정책 수명)
      SIM_FIXED   — 현재 MVP Simulation 에서 팀이 실행값으로 확정했다 (근거 등급)
  둘을 하나로 뭉치면 "임시라서 못 믿을 값" 과 "지금 실행에 쓰기로 정한 값" 이
  구분되지 않는다.

★ 매입의 `margin_defense_floor_rate` 를 가져다 쓰지 않는다. 판매 마진은 별도
  정책이고, 값이 우연히 비슷해도 같은 결정이 아니다 — 한쪽을 바꿀 때 다른 쪽이
  조용히 따라가면 안 된다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 회수위험 판정 방식. **점수를 만들지 않는다.**
#:
#: ★ 닫힌 어휘 하나뿐이다. 가중치·등급·연체기간별 점수는 권위 있는 임계값 계약이
#:   없으면 숫자처럼 보이는 추측이다 — 만들지 않는다.
#:   ANY_OVERDUE_REVIEW: 연체가 1원이라도 있으면 사람이 한 번 본다.
SalesCollectionRiskMode = Literal["ANY_OVERDUE_REVIEW"]

SalesPaymentTermsType = Literal["SINGLE", "INSTALLMENT"]

#: 이 정책 **결정 자체**를 가리키는 Finance 내부 ref.
#:
#: 🔴 외부 자료를 사칭하는 ref 가 아니다. DB 행도, 계약서도, 시세 자료도 가리키지
#:    않는다. "이 값들은 Finance/Sales MVP Policy v0.1 결정문에서 왔다" 는 사실
#:    하나만 가리킨다. 밖에서 따라가면 닿을 곳이 있는 척하지 않는다.
FINANCE_SALES_MVP_POLICY_REF = "FIN-SALES-MVP-POLICY-V0.1"


class FinanceSalesMvpPolicy(BaseModel):
    """판매 재무 판정에 실제로 들어가는 값과 그 값의 성격."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 이 아래면 FAIL. 상환기 손익분기 CM 기반 MVP 기준.
    finance_minimum_margin_rate: Decimal = Field(ge=0, le=1)
    #: 이 아래면 REVIEW_REQUIRED. target CM 기반 MVP 기준.
    finance_warning_margin_rate: Decimal = Field(ge=0, le=1)
    max_finance_allowed_payment_terms_days: int = Field(ge=0)
    #: 지금 판정할 수 있는 결제방식. INSTALLMENT 는 정책이 없어 여기 없다.
    supported_payment_terms_type: SalesPaymentTermsType
    collection_risk_mode: SalesCollectionRiskMode

    policy_version: str = Field(min_length=1)
    status: Literal["PROVISIONAL"]
    usage_scope: Literal["AGENT_MVP_DEMO"]
    evidence_grade: Literal["SIM_FIXED"]
    decision_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def warning_is_not_below_minimum(self) -> FinanceSalesMvpPolicy:
        if self.finance_warning_margin_rate < self.finance_minimum_margin_rate:
            raise ValueError(
                "finance_warning_margin_rate must not be below finance_minimum_margin_rate"
            )
        return self


#: 🔴 **여신한도는 여기 없다.** 그것은 Finance 가 정하는 정책값이 아니라 거래처·계약이
#:    소유한 권위 있는 사실이다. 여기 기본값을 두면 없는 한도를 재무가 발명하게 된다.
#:    권위 있는 값이 없으면 판정은 RUNTIME_NOT_READY 로 닫힌다.
_FINANCE_SALES_MVP_POLICY_V0_1 = FinanceSalesMvpPolicy(
    finance_minimum_margin_rate=Decimal("0.2642"),
    finance_warning_margin_rate=Decimal("0.30"),
    max_finance_allowed_payment_terms_days=30,
    supported_payment_terms_type="SINGLE",
    collection_risk_mode="ANY_OVERDUE_REVIEW",
    policy_version="Finance/Sales MVP Policy v0.1",
    status="PROVISIONAL",
    usage_scope="AGENT_MVP_DEMO",
    evidence_grade="SIM_FIXED",
    decision_ref=FINANCE_SALES_MVP_POLICY_REF,
)


def load_finance_sales_mvp_policy() -> FinanceSalesMvpPolicy:
    """이번 MVP 실행 정책을 돌려준다. **언제 불러도 같은 값이다.**

    ★ 조회도 계산도 하지 않는다. 실행마다 값이 달라지면 같은 제안이 날마다 다른
      판정을 받는다 — 정책은 그런 종류의 값이 아니다.
    """
    return _FINANCE_SALES_MVP_POLICY_V0_1
