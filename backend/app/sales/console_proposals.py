"""그날 만든 판매안 read model — **매입 화면의 «금일 매입안» 과 같은 자리다.**

★ 판매 화면에는 «오늘 무엇을 팔자고 했나» 를 보여 주는 자리가 없었다. 확정된 판매와
  채권은 있지만, 그건 이미 끝난 일이다. 매입 화면은 그날의 안을 카드로 펴 놓고 근거와
  걸리는 것을 함께 보여 준다 — 판매도 같은 것을 볼 수 있어야 한다.

🔴 **판매가 자기 저장소를 읽는다.** 안 자체는 `sales_agent_runs` 의
   `response_payload.payload.scenarios` 에 있다. 다시 만들지 않고 저장된 것을 편다.

🔴 **재무 판정을 판매가 계산하지 않는다.** 통과 여부는 재무가 자기 이력에 남긴 값이고,
   여기서는 **같은 요청 키와 안 번호로 찾아 읽기만** 한다. 판매가 마진이나 여신으로
   판정을 흉내 내면 두 화면이 다른 답을 말하는 날이 온다.

⚠️ **실행 축이 컬럼에 없다.** `sales_agent_runs` 는 `sim_run_id` 칸을 갖고 있지 않아
  요청 봉투 안의 값으로 거른다 (`console_runs` 와 같은 자리).
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import sql
from pydantic import BaseModel, ConfigDict

from app.sales.db import fetch_all, get_db_schema


class ConsoleSalesProposal(BaseModel):
    """판매안 하나. **저장된 값만 담는다.**"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    scenario_id: str
    #: 안의 성격. `CONSERVATIVE` · `BALANCED` · `AGGRESSIVE` 같은 저장값 그대로다.
    scenario_type: str | None
    objective: str | None
    item: str | None
    partner_id: str | None
    quantity_kg: Decimal | None
    unit_price_krw: Decimal | None
    #: 🔴 판매가 적어 보낸 매출액이다. 화면이 수량×단가로 다시 만들지 않는다.
    reported_sales_amount_krw: Decimal | None
    payment_days: int | None
    delivery_date: date | None
    #: 판매가 스스로 매긴 상태. 재무 판정과 다른 축이다.
    status: str | None
    rationale: list[str]
    risks: list[str]
    uncertainties: list[str]
    #: 재무가 같은 요청·같은 안에 남긴 판정. 없으면 `None` 이고 0 이나 통과가 아니다.
    finance_verdict: str | None
    finance_status: str | None
    #: 🔴 **판정을 가른 규칙의 사유다.** 통과 사유는 담지 않는다 — 최상위
    #:   `reason_codes` 에는 통과 사유까지 섞여 있어 그대로 쓰면 «거절 사유» 가 아니다.
    finance_reason_codes: list[str]
    #: 판정을 뒷받침한 재무 숫자. 없으면 `None` 이고 화면이 0 으로 채우지 않는다.
    contribution_margin_krw: Decimal | None
    contribution_margin_rate: Decimal | None
    available_credit_krw: Decimal | None
    projected_partner_ar_krw: Decimal | None
    credit_limit_krw: Decimal | None
    #: 판매가 «이건 아직 못 받았다» 고 적어 둔 검증. 재무 판정이 없는 이유가 여기 있다.
    missing_capabilities: list[str]
    #: 이 안이 어디에 기대어 섰는지. 판매가 회신에 실은 참조를 그대로 나른다.
    evidence_refs: list[str]
    source_ref: str | None
    #: 원가 기준. 마진을 재무가 세는 근거이고, 없으면 재무가 판정을 닫는다.
    cost_basis_amount_krw: Decimal | None
    cost_basis_quantity_kg: Decimal | None
    cost_basis_method: str | None
    cost_basis_refs: list[str]
    #: 이 안이 기댄 물량. 확정분과 조건부분을 가른다.
    confirmed_quantity_kg: Decimal | None
    conditional_quantity_kg: Decimal | None
    additional_supply_required: bool | None
    ml_support_used: bool | None
    #: 판매가 추천으로 표시한 안인지. 저장된 `recommended_scenario_id` 와 같을 때만 참이다.
    recommended: bool


class ConsoleSalesProposalsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sim_run_id: str
    as_of: date
    #: 그날 판매가 실제로 돈 요청 수. 안이 0개여도 돈 것은 돈 것이다.
    request_count: int
    #: 🔴 **팔 물량이 0인 안은 목록에서 뺀다.** 그런 안은 재무가 검토할 것도 없어
    #:   판정이 영원히 안 붙고, 화면에서는 «재무 검토 전» 으로 남아 실제로 밀린 안처럼
    #:   보인다. 지우는 것이 아니라 **몇 건을 뺐는지 숫자로 남긴다.**
    hidden_zero_quantity: int
    rows: list[ConsoleSalesProposal]


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        #  ⚠️ 못 읽는 값을 0 으로 바꾸지 않는다. 0 원 제안과 «못 읽었다» 는 다른 사실이다.
        return None


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _texts(value: object) -> list[str]:
    """문장 목록. **문자열이 아닌 항목은 버리지 않고 글자로 적는다.**"""
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def load_proposal_rows(*, sim_run_id: str, as_of: date) -> list[dict[str, Any]]:
    """그날 판매가 만든 안과 재무가 남긴 판정.

    ★ **요청 키 하나에 행 하나를 고른다.** 되먹임(refeed)이 돌면 같은 요청이 여러 번
      저장되고, 그중 마지막이 그날의 답이다.

    ★ 재무 판정도 **가장 최근 것**을 읽는다. 재검증이 돌면 같은 키에 여러 회신이 쌓인다.
    """
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT latest.request_id,
               latest.payload,
               scenario.value AS scenario,
               finance.finance_verdict,
               finance.finance_status,
               finance.rule_results,
               finance.financial_summary
        FROM (
            SELECT DISTINCT ON (run.response_payload->>'request_id')
                   run.response_payload->>'request_id' AS request_id,
                   run.response_payload->'payload' AS payload
            FROM {schema}.sales_agent_runs run
            WHERE run.request_payload->'context'->>'sim_run_id' = %s
              AND run.as_of = %s
              AND run.response_payload->>'request_id' IS NOT NULL
            ORDER BY run.response_payload->>'request_id', run.created_at DESC
        ) AS latest
        LEFT JOIN LATERAL jsonb_array_elements(
            COALESCE(latest.payload->'scenarios', '[]'::jsonb)
        ) AS scenario ON TRUE
        LEFT JOIN LATERAL (
            SELECT check_run.response_payload->>'finance_verdict' AS finance_verdict,
                   check_run.response_payload->>'status' AS finance_status,
                   check_run.response_payload->'rule_results' AS rule_results,
                   check_run.response_payload->'financial_summary' AS financial_summary
            FROM {schema}.finance_agent_runs_v22 check_run
            WHERE check_run.mode = 'SALES_VALIDATION'
              AND check_run.request_id = latest.request_id
              AND check_run.response_payload->>'scenario_id' = scenario.value->>'scenario_id'
            ORDER BY check_run.created_at DESC
            LIMIT 1
        ) AS finance ON TRUE
        ORDER BY latest.request_id ASC, scenario.value->>'scenario_id' ASC
        """
    ).format(schema=sql.Identifier(schema))
    return fetch_all(statement, [sim_run_id, as_of])


def get_console_sales_proposals(
    *, sim_run_id: str, as_of: date
) -> ConsoleSalesProposalsResponse:
    """그날의 판매안. **안이 없으면 빈 목록이지, 0원 제안이 아니다.**"""
    rows: list[ConsoleSalesProposal] = []
    requests: set[str] = set()
    hidden = 0
    for raw in load_proposal_rows(sim_run_id=sim_run_id, as_of=as_of):
        request_id = str(raw["request_id"])
        requests.add(request_id)
        scenario = raw["scenario"]
        if not isinstance(scenario, dict):
            #  그날 돌았지만 안을 못 만든 요청이다 (입력 미비 등). 요청 수에는 남는다.
            continue
        quantity = _decimal(scenario.get("quantity_kg"))
        if quantity is not None and quantity <= 0:
            #  🔴 팔 물량이 0이면 안이 선 것이 아니다. 재무도 검토할 것이 없어
            #     판정이 안 붙고, 화면에서는 «검토 전» 으로 남아 밀린 안처럼 보인다.
            hidden += 1
            continue
        payload = raw["payload"] if isinstance(raw["payload"], dict) else {}
        recommended_id = payload.get("recommended_scenario_id")
        scenario_id = scenario.get("scenario_id")
        summary = raw["financial_summary"] if isinstance(raw["financial_summary"], dict) else {}
        cost_basis = scenario.get("inventory_cost_basis")
        cost_basis = cost_basis if isinstance(cost_basis, dict) else {}
        supply = scenario.get("supply")
        supply = supply if isinstance(supply, dict) else {}
        rows.append(
            ConsoleSalesProposal(
                request_id=request_id,
                scenario_id="" if scenario_id is None else str(scenario_id),
                scenario_type=_text(scenario.get("scenario_type")),
                objective=_text(scenario.get("objective")),
                item=_text(scenario.get("item")),
                partner_id=_text(scenario.get("partner_id")),
                quantity_kg=quantity,
                unit_price_krw=_decimal(scenario.get("unit_price_krw")),
                reported_sales_amount_krw=_decimal(
                    scenario.get("reported_sales_amount_krw")
                ),
                payment_days=_int(scenario.get("payment_days")),
                delivery_date=_date(scenario.get("delivery_date")),
                status=_text(scenario.get("status")),
                rationale=_texts(scenario.get("rationale")),
                risks=_texts(scenario.get("risks")),
                uncertainties=_texts(scenario.get("uncertainties")),
                finance_verdict=_text(raw.get("finance_verdict")),
                finance_status=_text(raw.get("finance_status")),
                finance_reason_codes=_failing_reasons(raw.get("rule_results")),
                contribution_margin_krw=_decimal(summary.get("contribution_margin_krw")),
                contribution_margin_rate=_decimal(summary.get("contribution_margin_rate")),
                available_credit_krw=_decimal(summary.get("available_credit_krw")),
                projected_partner_ar_krw=_decimal(summary.get("projected_partner_ar_krw")),
                credit_limit_krw=_decimal(summary.get("credit_limit_krw")),
                missing_capabilities=_texts(payload.get("missing_capabilities")),
                evidence_refs=_texts(scenario.get("evidence_refs")),
                source_ref=_text(scenario.get("source_ref")),
                cost_basis_amount_krw=_decimal(cost_basis.get("amount_krw")),
                cost_basis_quantity_kg=_decimal(cost_basis.get("quantity_kg")),
                cost_basis_method=_text(cost_basis.get("cost_method")),
                cost_basis_refs=_texts(cost_basis.get("source_refs")),
                confirmed_quantity_kg=_decimal(supply.get("confirmed_quantity_kg")),
                conditional_quantity_kg=_decimal(supply.get("conditional_quantity_kg")),
                additional_supply_required=_bool(supply.get("additional_supply_required")),
                ml_support_used=_bool(scenario.get("ml_support_used")),
                recommended=(
                    recommended_id is not None and str(recommended_id) == str(scenario_id)
                ),
            )
        )
    return ConsoleSalesProposalsResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        request_count=len(requests),
        hidden_zero_quantity=hidden,
        rows=rows,
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _bool(value: object) -> bool | None:
    """⚠️ `None` 은 «모른다» 다. `False` 로 바꾸면 «확인했고 아니다» 가 된다."""
    return None if value is None else bool(value)


def _failing_reasons(rule_results: object) -> list[str]:
    """판정을 **가른** 규칙의 사유만 모은다.

    🔴 **최상위 `reason_codes` 를 쓰지 않는다.** 그 배열에는 통과 사유까지 함께 들어
       있어(실측: PASS 판정에도 일곱 개), 그대로 쓰면 통과한 규칙이 거절 사유로 읽힌다.

    ⚠️ `REVIEW_REQUIRED` 도 함께 담는다 — 왜 «확인 필요» 인지도 사유가 있어야 한다.
    """
    if not isinstance(rule_results, list):
        return []
    collected: list[str] = []
    for rule in rule_results:
        if not isinstance(rule, dict):
            continue
        if rule.get("verdict") not in {"FAIL", "REVIEW_REQUIRED"}:
            continue
        for code in rule.get("reason_codes") or ():
            text = str(code)
            if text not in collected:
                collected.append(text)
    return collected
