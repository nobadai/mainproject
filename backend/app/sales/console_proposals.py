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

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from psycopg import sql
from pydantic import BaseModel, ConfigDict

from app.sales.db import fetch_all, get_db_schema

#: 한 안이 **사용자 앞에서 어떤 자리에 서 있는가.**
#:
#: ```text
#: PRESENTABLE       판정이 났고 통과했다        → 승인으로 갈 수 있다
#: REVIEW_REQUIRED   판정이 났고 «확인 필요» 다  → 사람이 봐야 한다
#: REJECTED          판정이 났고 «안 된다» 다    → 조건을 바꿔야 한다
#: UNRESOLVED        판정 자체가 안 났다          → 없는 자료를 채워야 한다
#: ```
#:
#: 🔴 **`UNRESOLVED` 와 `REJECTED` 를 같은 줄로 보여주면 화면이 거짓말을 한다.**
#:    탈락은 «다 봤는데 안 된다» 이고 미판정은 «아직 안 봤다» 다. 둘을 섞으면 사용자는
#:    자료를 채워야 할 날에 조건을 바꾸고, 같은 자리에서 또 막힌다. 마스터가 종료 코드에
#:    `SL3_ALL_REJECTED` 와 `SL6_VALIDATION_UNRESOLVED` 를 따로 둔 것과 같은 이유다.
SalesPresentationState = Literal[
    "PRESENTABLE", "REVIEW_REQUIRED", "REJECTED", "UNRESOLVED"
]

#: 그날 판매 화면 전체가 어떤 상태인가. **후보가 없는 것과 판정이 없는 것은 다르다.**
#:
#: ```text
#: EMPTY         안 자체를 못 만들었다
#: UNRESOLVED    안은 있는데 권위 검증이 안 끝났다
#: REJECTED      판정이 다 났고 통과가 하나도 없다
#: PRESENTABLE   통과한 안이 하나라도 있다
#: ```
SalesProposalsState = Literal["EMPTY", "UNRESOLVED", "REJECTED", "PRESENTABLE"]


class ConsoleSalesStrategy(BaseModel):
    """그 요청의 **전략이 어떻게 섰는가.** 저장된 라벨만 담는다.

    🔴 **HTTP 원문도 provider 응답 본문도 담지 않는다.** 실패 사유는 저장된 어휘
       (`HTTP_400` · `HTTP_429` · `PROVIDER_UNREACHABLE` · `CONTRACT_VIOLATION`)뿐이다 —
       원문을 화면까지 내보내면 키나 내부 주소가 사용자 브라우저에 실린다.
    """

    model_config = ConfigDict(extra="forbid")

    #: 무엇이 자세를 골랐나. `LLM` 또는 `TEMPLATE_FALLBACK`.
    source: str | None
    #: 모델에 무슨 일이 있었나. `SUCCESS` · `SKIPPED_TEMPLATE` · `FALLBACK` · `DISABLED`.
    llm_status: str | None
    #: 🔴 **왜 실패했나.** 성공했거나 안 켠 날은 `None` 이다.
    llm_failure_reason: str | None
    #: 모델이 고른 자세를 사실이 내린 자리.
    clamped_reason_codes: list[str]
    #: 자세는 갈렸는데 숫자가 수렴했는가.
    collapsed: bool
    collapse_reason_codes: list[str]


class ConsoleSalesProposal(BaseModel):
    """판매안 하나. **저장된 값만 담는다.**"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    #: 사용자가 보고 선택한 마스터 판매 실행. 승인 요청은 이 값을 반드시 함께 보낸다.
    #: 없으면 최신 실행을 고르는 경합이 생기므로 화면은 확정을 열지 않는다.
    history_run_id: str | None
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
    #: 🔴 여신 칸은 **재무가 센 값**을 옮긴다. 판매·화면이 한도에서 미수를 빼지 않는다.
    current_partner_ar_krw: Decimal | None
    available_credit_krw: Decimal | None
    projected_partner_ar_krw: Decimal | None
    credit_limit_krw: Decimal | None
    #: 판매 전에 먼저 받아야 하는 미수금. **0 은 «더 받을 필요 없음» 이고 `None` 은 «모름» 이다.**
    required_collection_before_sale_krw: Decimal | None
    credit_utilization_rate: Decimal | None
    #: 계약상 결제 예정일 기준의 예상. 입금 보장일이 아니다.
    expected_credit_recovery_date: date | None
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
    #: 추천한 안에 판매가 **저장해 둔** 추천 이유. 없으면 `None` 이다 — 화면이 지어내지 않는다.
    recommendation_reason: str | None = None
    #: 이 안이 실제 판매로 확정됐는지. 같은 실행의 `sales` 행이 있을 때만 그 주문 상태다
    #: (`CONFIRMED` · `DELIVERED`). **없으면 `None` 이고 «선택» 이나 «추천» 과 다르다.**
    sale_status: str | None = None
    #: 🔴 **이 안이 사용자 앞에서 서는 자리.** 판매 상태와 재무 판정을 함께 읽어 정한다.
    presentation_state: SalesPresentationState = "UNRESOLVED"
    #: 승인으로 보낼 수 없는가. **`PRESENTABLE` 이 아닌 모든 안이 참이다.**
    #:
    #: ★ 화면이 후보를 **보여주는 것**과 **확정으로 보내는 것**은 다른 사실이다. 미판정
    #:   후보도 보여줄 수 있지만 확정 경계는 그대로 닫혀 있어야 한다.
    approval_blocked: bool = True
    #: 판정이 왜 안 났는가. `presentation_state` 가 `UNRESOLVED` 일 때만 채운다.
    unresolved_reason_codes: list[str] = []
    #: 그 요청의 전략이 어떻게 섰는가. 같은 요청의 모든 안이 같은 값을 본다.
    strategy: ConsoleSalesStrategy | None = None


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
    #: 🔴 **«후보가 없다» 와 «판정이 없다» 를 한 문구로 합치지 않는다.**
    state: SalesProposalsState = "EMPTY"
    presentable_count: int = 0
    unresolved_count: int = 0
    rejected_count: int = 0
    review_required_count: int = 0
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

    🔴 **재무 판정에도 실행 축을 건다.** `request_id` 만으로 잇는 것은 안전해 보이지만
       아니다 — 축을 담지 않는 키가 실제로 있고(`REQ-DAILY-SALES-20260107-배추` 는 네
       실행에 걸쳐 있다), 그때는 남의 실행 판정이 이 안에 붙는다.

    ★ **축은 마스터가 안다.** `finance_agent_runs_v22` 에는 `sim_run_id` 칸이 없어,
      그 연결을 기록한 `master_agent_runs` 에 묻는다 — 재무 자신의 실행 이력 read model
      (`finance.console_runs`)이 같은 자리에서 같은 방법을 쓴다.

    ⚠️ **키 문자열을 파싱하지 않는다.** `REQ-DAILY-SALES-{실행}-…` 모양에 기대면 그
      모양이 바뀌는 날 화면이 오류 없이 남의 실행을 가리킨다. `request_id` 는 업무
      키이지 스키마가 아니다.
    """
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT latest.request_id,
               latest.payload,
               history.run_id AS history_run_id,
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
        LEFT JOIN LATERAL (
            SELECT axis.run_id
            FROM {schema}.master_agent_runs axis
            WHERE axis.cycle = 'SALES'
              AND axis.request_id = latest.request_id
              AND axis.sim_run_id = %s
            ORDER BY axis.run_seq DESC, axis.created_at DESC
            LIMIT 1
        ) AS history ON TRUE
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
              AND EXISTS (
                  SELECT 1
                  FROM {schema}.master_agent_runs axis
                  WHERE axis.request_id = check_run.request_id
                    AND axis.sim_run_id = %s
              )
            ORDER BY check_run.created_at DESC
            LIMIT 1
        ) AS finance ON TRUE
        ORDER BY latest.request_id ASC, scenario.value->>'scenario_id' ASC
        """
    ).format(schema=sql.Identifier(schema))
    #  ⚠️ `%s` 는 네 개다 — 판매 실행 축, 기준일, 화면이 본 마스터 실행, 재무 판정 축.
    return fetch_all(statement, [sim_run_id, as_of, sim_run_id, sim_run_id])


def load_sale_statuses(*, sim_run_id: str, request_ids: list[str]) -> dict[tuple[str, str], str]:
    """그날 요청에서 **실제로 확정된 판매**. `(요청 키, 안 번호) → 주문 상태`.

    ★ 판매 확정은 `sales` 행으로 남는다 (`persistence.build_sale_confirmation_plan`).
      `source_order_id` 가 요청 키이고 `sale_id` 끝이 안 번호다 (`sale_id_for`).

    🔴 **실행 축을 건다.** 같은 요청 키가 다른 실행에도 있을 수 있다.
    """
    if not request_ids:
        return {}
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT sale.source_order_id, sale.sale_id, sale.order_status
        FROM {schema}.sales sale
        WHERE sale.sim_run_id = %s
          AND sale.source_order_id = ANY(%s)
        """
    ).format(schema=sql.Identifier(schema))
    found: dict[tuple[str, str], str] = {}
    for raw in fetch_all(statement, [sim_run_id, request_ids]):
        request_id = str(raw["source_order_id"])
        sale_id = str(raw["sale_id"])
        found[(request_id, sale_id)] = str(raw["order_status"])
    return found


def _sale_status(
    found: dict[tuple[str, str], str], request_id: str, scenario_id: str
) -> str | None:
    """`sale_id_for` 가 만든 번호는 `…-{안 번호}` 로 끝난다.

    ★ **다른 안과 섞이지 않게 끝까지 맞춘다.**
    """
    suffix = f"-{scenario_id}"
    for (request, sale_id), status in found.items():
        if request == request_id and sale_id.endswith(suffix):
            return status
    return None


#: 판매가 스스로 «아직 판정 못 받았다» 고 적은 상태.
_SALES_UNRESOLVED_STATUSES = frozenset({"UNRESOLVED", "REVIEW_REQUIRED"})

#: 판매가 스스로 «막혔다» 고 적은 상태.
_SALES_BLOCKED_STATUSES = frozenset({"INFEASIBLE"})


def _presentation(
    *,
    sales_status: str | None,
    finance_verdict: str | None,
    missing_capabilities: list[str],
) -> tuple[str, list[str]]:
    """이 안이 **사용자 앞에서 어느 자리에 서는가**와 그 이유.

    두 축을 함께 읽는다 — 판매가 스스로 매긴 상태와 재무가 내린 판정이다.

    ```text
    재무가 FAIL 이라고 했다            → REJECTED        판정이 났다
    판매가 INFEASIBLE 이라고 했다      → REJECTED        판정이 났다
    재무가 아무 말도 안 했다           → UNRESOLVED      판정이 안 났다
    판매가 UNRESOLVED 라고 했다        → UNRESOLVED      판정이 안 났다
    재무가 REVIEW_REQUIRED 라고 했다   → REVIEW_REQUIRED 사람이 봐야 한다
    재무가 PASS 라고 했다              → PRESENTABLE     승인으로 갈 수 있다
    ```

    🔴 **`UNRESOLVED` 를 `PASS` 로 바꾸지 않는다.** 판정을 안 받은 안을 통과로 적으면
       재무가 막았을 거래가 사용자 화면에서 승인 가능으로 보인다.

    🔴 **`UNRESOLVED` 를 `REJECTED` 로도 적지 않는다.** 아무도 탈락시키지 않았는데
       탈락이라고 적으면, 사용자는 자료를 채워야 할 날에 조건을 바꾼다.

    ★ **막힌 안이 판정 순서보다 앞선다.** 재무가 `FAIL` 을 냈으면 판매가 스스로 뭐라
      적었든 그 안은 탈락이다 — 권위 있는 판정이 이겼다.
    """
    if finance_verdict == "FAIL":
        return "REJECTED", []
    if sales_status in _SALES_BLOCKED_STATUSES:
        return "REJECTED", []
    if finance_verdict is None:
        #  ★ 왜 판정이 안 났는지를 **판매가 적어 둔 사실에서만** 읽는다. 없는 이유를
        #    지어내면 사용자가 채울 수 없는 것을 채우려 한다.
        reasons = list(missing_capabilities) or ["FINANCIAL_VALIDATION_PENDING"]
        return "UNRESOLVED", reasons
    if sales_status in _SALES_UNRESOLVED_STATUSES and finance_verdict != "PASS":
        return "UNRESOLVED", list(missing_capabilities) or [f"SALES_STATUS_{sales_status}"]
    if finance_verdict == "REVIEW_REQUIRED":
        return "REVIEW_REQUIRED", []
    if finance_verdict == "PASS":
        return "PRESENTABLE", []
    #  ⚠️ 모르는 판정 어휘를 통과로 접지 않는다.
    return "UNRESOLVED", [f"UNKNOWN_FINANCE_VERDICT_{finance_verdict}"]


def _strategy(payload: dict[str, Any]) -> ConsoleSalesStrategy | None:
    """저장된 전략 라벨. 아무 칸도 없으면 `None` 이다 — 빈 칸을 지어내지 않는다.

    ★ 여기 담기는 것은 **저장된 어휘**뿐이다. 모델이 돌려준 원문이나 HTTP 응답 본문은
      들어오지 않는다.
    """
    keys = (
        "strategy_source",
        "strategy_llm_status",
        "strategy_llm_failure_reason",
        "strategy_clamped_reason_codes",
        "strategy_collapsed",
        "strategy_collapse_reason_codes",
    )
    if not any(key in payload for key in keys):
        return None
    return ConsoleSalesStrategy(
        source=_text(payload.get("strategy_source")),
        llm_status=_text(payload.get("strategy_llm_status")),
        llm_failure_reason=_text(payload.get("strategy_llm_failure_reason")),
        clamped_reason_codes=_texts(payload.get("strategy_clamped_reason_codes")),
        collapsed=bool(payload.get("strategy_collapsed")),
        collapse_reason_codes=_texts(payload.get("strategy_collapse_reason_codes")),
    )


def _recommendation_reason(payload: dict[str, Any], recommended: bool) -> str | None:
    """추천한 안에만, 판매가 저장한 이유를 옮긴다. 비어 있으면 `None` 이다."""
    if not recommended:
        return None
    for key in ("recommendation", "llm"):
        block = payload.get(key)
        if isinstance(block, dict):
            reason = block.get("recommendation_reason")
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
    return None


def get_console_sales_proposals(*, sim_run_id: str, as_of: date) -> ConsoleSalesProposalsResponse:
    """그날의 판매안. **안이 없으면 빈 목록이지, 0원 제안이 아니다.**"""
    rows: list[ConsoleSalesProposal] = []
    requests: set[str] = set()
    hidden = 0
    raw_rows = load_proposal_rows(sim_run_id=sim_run_id, as_of=as_of)
    sale_statuses = load_sale_statuses(
        sim_run_id=sim_run_id,
        request_ids=sorted({str(raw["request_id"]) for raw in raw_rows}),
    )
    for raw in raw_rows:
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
        is_recommended = recommended_id is not None and str(recommended_id) == str(scenario_id)
        summary = raw["financial_summary"] if isinstance(raw["financial_summary"], dict) else {}
        sales_status = _text(scenario.get("status"))
        finance_verdict = _text(raw.get("finance_verdict"))
        missing = _texts(payload.get("missing_capabilities"))
        state, unresolved_reasons = _presentation(
            sales_status=sales_status,
            finance_verdict=finance_verdict,
            missing_capabilities=missing,
        )
        cost_basis = scenario.get("inventory_cost_basis")
        cost_basis = cost_basis if isinstance(cost_basis, dict) else {}
        supply = scenario.get("supply")
        supply = supply if isinstance(supply, dict) else {}
        rows.append(
            ConsoleSalesProposal(
                request_id=request_id,
                history_run_id=_text(raw.get("history_run_id")),
                scenario_id="" if scenario_id is None else str(scenario_id),
                scenario_type=_text(scenario.get("scenario_type")),
                objective=_text(scenario.get("objective")),
                item=_text(scenario.get("item")),
                partner_id=_text(scenario.get("partner_id")),
                quantity_kg=quantity,
                unit_price_krw=_decimal(scenario.get("unit_price_krw")),
                reported_sales_amount_krw=_decimal(scenario.get("reported_sales_amount_krw")),
                payment_days=_int(scenario.get("payment_days")),
                delivery_date=_date(scenario.get("delivery_date")),
                status=sales_status,
                rationale=_texts(scenario.get("rationale")),
                risks=_texts(scenario.get("risks")),
                uncertainties=_texts(scenario.get("uncertainties")),
                finance_verdict=finance_verdict,
                finance_status=_text(raw.get("finance_status")),
                finance_reason_codes=_failing_reasons(raw.get("rule_results")),
                contribution_margin_krw=_decimal(summary.get("contribution_margin_krw")),
                contribution_margin_rate=_decimal(summary.get("contribution_margin_rate")),
                current_partner_ar_krw=_decimal(summary.get("current_partner_ar_krw")),
                available_credit_krw=_decimal(summary.get("available_credit_krw")),
                projected_partner_ar_krw=_decimal(summary.get("projected_partner_ar_krw")),
                credit_limit_krw=_decimal(summary.get("credit_limit_krw")),
                required_collection_before_sale_krw=_decimal(
                    summary.get("required_collection_before_sale_krw")
                ),
                credit_utilization_rate=_decimal(summary.get("credit_utilization_rate")),
                expected_credit_recovery_date=_date(summary.get("expected_credit_recovery_date")),
                missing_capabilities=missing,
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
                recommended=is_recommended,
                recommendation_reason=_recommendation_reason(payload, is_recommended),
                sale_status=_sale_status(sale_statuses, request_id, str(scenario_id)),
                presentation_state=state,  # type: ignore[arg-type]
                #  🔴 통과한 안만 확정 경계를 넘을 수 있다. «확인 필요» 도 막는다 —
                #     사람이 봐야 한다는 말은 아직 승인이 아니라는 뜻이다.
                approval_blocked=state != "PRESENTABLE",
                unresolved_reason_codes=unresolved_reasons,
                strategy=_strategy(payload),
            )
        )
    counted = Counter(row.presentation_state for row in rows)
    return ConsoleSalesProposalsResponse(
        sim_run_id=sim_run_id,
        as_of=as_of,
        request_count=len(requests),
        hidden_zero_quantity=hidden,
        state=_overall_state(counted, has_rows=bool(rows)),
        presentable_count=counted["PRESENTABLE"],
        unresolved_count=counted["UNRESOLVED"],
        rejected_count=counted["REJECTED"],
        review_required_count=counted["REVIEW_REQUIRED"],
        rows=rows,
    )


def _overall_state(counted: Counter[str], *, has_rows: bool) -> str:
    """그날 판매 화면 전체가 어떤 상태인가.

    ```text
    행이 하나도 없다                → EMPTY        안을 못 만들었다
    통과가 하나라도 있다            → PRESENTABLE  보여줄 것이 있다
    미판정이 하나라도 있다          → UNRESOLVED   기다릴 것이 있다
    그 밖                           → REJECTED     판정이 다 났고 다 안 된다
    ```

    🔴 **미판정이 섞여 있으면 `REJECTED` 라고 적지 않는다** — 마스터의
       `_unpassed_outcome` 이 `SL6` 과 `SL3` 을 가르는 규칙과 같은 자리다. 판정을 안
       받은 안은 탈락한 적이 없다.
    """
    if not has_rows:
        return "EMPTY"
    if counted["PRESENTABLE"]:
        return "PRESENTABLE"
    if counted["UNRESOLVED"] or counted["REVIEW_REQUIRED"]:
        return "UNRESOLVED"
    return "REJECTED"


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
