"""실행 흔적 — Evidence · DeptMeta · 실행이력.

이 파일이 소유하는 것
    Evidence 생성과 정책 출처 규율 · Critic 이 읽는 DeptMeta(입력 계보/산출 필드) ·
    Agent 실행이력 저장(append-only)과 조회

여기 **없는 것**
    금액 계산 · 판정 · 실행 통제 · 사람이 읽는 문장

★ 셋은 한 가지를 다룬다 — **무슨 일이 있었고, 무엇이 그것을 받치며, 어떻게 남기는가.**
  Evidence 없이 DeptMeta 를 못 만들고, 둘 없이 남길 이력이 없다. 갈라 두면 근거 하나를
  고칠 때마다 세 파일을 오간다.

★ **선언이 아니라 관측이다.** DeptMeta 는 실제로 성공한 Tool(`state.tool_order`)과
  실제로 실린 payload 키만 보고 만든다. 손으로 적은 목록이 실행과 어긋나면 Critic 은
  우리가 적은 거짓말을 검사하게 된다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from app.contracts.core import Evidence, SuggestedAdjustment
from app.finance.db import (
    FinanceDataNotReady,
    execute_returning_one,
    fetch_all,
    fetch_one,
    get_db_schema,
)
from app.finance.schemas import (
    FinalVerdict,
    FinanceAgentRunResponse,
    FinanceCycle,
    FinancePolicy,
    RuntimeStatus,
)
from app.master.critic_bridge import DEPT_CAP_CHECK_ID
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata

# ---------------------------------------------------------------------------
# Evidence 생성 · 정책 출처 규율
# ---------------------------------------------------------------------------

if TYPE_CHECKING:  # 순환 import 방지 — 상태는 타입으로만 필요하다.
    from app.finance.state import FinanceAgentState

def missing_source_name(key: str) -> str:
    """근거 없이 뺀 정책값을 `missing_data` 에 적을 때 쓰는 이름.

    ★ **한 이름으로만 부른다.** 어댑터 경계와 Tool 이 서로 다른 이름으로 적으면, 같은
      사실이 두 이름으로 이력에 남아 나중에 세어 볼 수 없다.
    """
    return f"{key}@policy_source_ref"


def resolve_optional_source_ref(
    policy: FinancePolicy, key: str, record: Callable[[str], None]
) -> str | None:
    """급여 외 정책값의 출처를 찾고, 없으면 **그 사실을 기록한 뒤 `None`** 을 준다.

    이 규칙의 **유일한 주인**이다. Controller 경로(`_optional_source_ref`)와 조회
    경로(`adapter._policy_ref`)가 담는 곳만 다르고 규칙은 같다 — 두 벌로 두면 한쪽만
    고쳐지고, 그때 갈리는 것은 *"근거 없는 값을 냈는가"* 다.

    `record` 는 빠진 이름을 어디에 적을지만 정한다 (실행 상태 · 조회의 missing 목록).
    """
    ref = policy.source_refs.get(key)
    if ref:
        return ref
    record(missing_source_name(key))
    return None


_PAYROLL_SOURCE_KEYS: tuple[str, ...] = ("monthly_labor_cost_krw", "payroll_date")
"""이 둘만 **출처가 없으면 계산 자체가 안 된다** (재무 #63 · M-23).

출처 없는 급여 이벤트를 만들지 않기로 재무가 정했으므로 급여 유출이 통째로 빠지고,
그 상태의 `finance_cap` 은 틀린 게 아니라 **낙관적으로 틀린다** — 그 상한으로 매입이
실행된다. 나머지 정책값은 값 자체를 쓸 수 있어 실행을 세우지 않는다
(`_optional_source_ref`)."""


def _source_ref(policy: FinancePolicy, key: str) -> str:
    """**계산 자체가 성립하지 않는** 정책 출처. 없으면 멈춘다.

    급여 두 키 전용이다 (`_PAYROLL_SOURCE_KEYS`). 출처 없는 급여 이벤트를 만들지
    않기로 재무가 정했으므로(재무 #63 · M-23) 급여 유출이 통째로 빠지고, 그 상태의
    `finance_cap` 은 틀린 게 아니라 **낙관적으로 틀린다** — 그 상한으로 매입이 실행된다.

    ★ `KeyError` 로 두지 않는 이유: Controller 의 일반 예외 경로로 빠져 `ERROR` 가
      된다. **출처가 없는 것은 프로그램 오류가 아니라 그날의 사실**이므로
      `RUNTIME_NOT_READY` + `missing_data` 다 — 둘은 재시도 가치가 다르다 (M-1 §5.1).
    """
    ref = policy.source_refs.get(key)
    if not ref:
        raise FinanceDataNotReady(missing_source_name(key))
    return ref


def _optional_source_ref(
    policy: FinancePolicy, key: str, state: FinanceAgentState
) -> str | None:
    """급여 외 정책값의 출처. **없어도 실행은 계속한다.**

    ★ 급여만 특별하다 (`_PAYROLL_SOURCE_KEYS`). 나머지는 값 자체를 쓸 수 있으므로
      계산은 그대로 돌고, 실행을 통째로 세우지 않는다 — 기존 재무 정책이다.

    ★ 다만 **지어내지 않는다.** 없는 출처를 `finance-policy:{version}:{key}` 같은
      문자열이나 스냅샷 id 로 채우면, 값은 멀쩡히 나오고 에러도 안 나지만 그 ref 는
      따라갔을 때 **아무 데도 닿지 않는다.** 근거가 있는 척하는 판정만 남는다.

    ★ 그래서 `None` 을 돌려주고, 부르는 쪽이 **그 claim 의 payload 필드와 Evidence 를
      함께 뺀다.** 숫자만 남기고 근거를 빼면 봉투 검증이 `E-EVIDENCE-MISSING` 을
      낸다 — 낼 수 없는 근거를 요구받는 것이 아니라, 낼 수 없는 값을 안 내는 것이다.
      빠진 사실은 `missing_data` 의 `<key>@policy_source_ref` 로 밝힌다.
    """
    return resolve_optional_source_ref(
        policy, key, lambda name: state.note_missing_source_name(name)
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def _evidence(
    claim: str,
    value: Any,
    unit: str,
    ref_id: str,
    *,
    source: Literal["finance", "tool_calc", "persona"] = "tool_calc",
) -> Evidence:
    numeric = float(value)
    return Evidence(
        claim=claim,
        source=source,
        ref_ids=(ref_id,),
        value=numeric,
        unit=unit,
        evidence_grade="OFFICIAL",
    )


def _tool_ref(tool_name: str, state: FinanceAgentState) -> str:
    return _branch_ref(tool_name, state)


def _branch_ref(kind: str, state: FinanceAgentState) -> str:
    return (
        f"FIN-AGENT:{state.request.context.request_id}:{state.request.call_seq}:"
        f"{state.branch_id}:{kind}"
    )


def _evidence_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "claim": evidence.claim,
        "source": evidence.source,
        "ref_ids": list(evidence.ref_ids),
        "value": evidence.value,
        "unit": evidence.unit,
        "evidence_grade": evidence.evidence_grade,
        "evidence_detail": evidence.evidence_detail,
    }


def _evidence_from_dict(value: dict[str, Any]) -> Evidence:
    return Evidence(
        claim=value["claim"],
        source=value["source"],
        ref_ids=tuple(value["ref_ids"]),
        value=value["value"],
        unit=value["unit"],
        evidence_grade=value["evidence_grade"],
        evidence_detail=value["evidence_detail"],
    )


def _indexed_verdict_evidence(results: list[dict[str, Any]]) -> list[Evidence]:
    """실제 숫자 branch claim을 Envelope v0.4 인덱스 경로에 다시 바인딩한다."""
    indexed: list[Evidence] = []
    for index, result in enumerate(results):
        raw_evidence = result.get("evidences", [])
        by_claim = {
            item.get("claim"): item
            for item in raw_evidence
            if isinstance(item, dict) and isinstance(item.get("claim"), str)
        }
        for claim, value in result.items():
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            source = by_claim.get(claim)
            if source is None:
                continue
            indexed.append(
                Evidence(
                    claim=f"verdicts[{index}].{claim}",
                    source=source["source"],
                    ref_ids=tuple(source["ref_ids"]),
                    value=float(value),
                    unit=source["unit"],
                    evidence_grade=source["evidence_grade"],
                    evidence_detail=source.get("evidence_detail"),
                )
            )
    return indexed


def _adjustment_from_dict(value: dict[str, Any]) -> SuggestedAdjustment:
    """재무 조정안 dict 를 공용 계약으로 옮긴다.

    🔴 예전에는 여섯 칸만 옮겼다. 그래서 상류가 `scenario_labels` 를 채워도 이 지점에서
       **조용히 사라졌다** — 마스터는 "어느 안에 대한 조정인지"를 영영 알 수 없고,
       빈 목록만 받는다. 값을 옮기면서 옮기는 자리를 빠뜨리는 그 모양이다.

    ★ 없으면 만들지 않는다. `scenario_labels` 가 없으면 빈 tuple 그대로 나간다 —
      빈 tuple 은 *"적용 대상을 특정하지 못했다"* 이지 *"모든 안에 적용"* 이 아니다.

    ★ `split_date` 는 회차 개념이 있는 축의 칸이다. 재무 `amount` 에는 회차가 없어서
      보통 `None` 이고, 그 `None` 은 정상이다 — 완전성을 위해 옮기기만 하고 재무가
      날짜를 지어내지 않는다.
    """
    return SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=value["target_value"],
        unit=value["unit"],
        reason=value["reason"],
        ref_ids=tuple(value["ref_ids"]),
        scenario_labels=tuple(value.get("scenario_labels") or ()),
        split_date=value.get("split_date"),
    )


# ---------------------------------------------------------------------------
# DeptMeta — Critic 이 재무를 검사할 수 있게 하는 사이드카
# ---------------------------------------------------------------------------

#: Critic에 전달되는 검사 id의 정본은 합성 주체인 Master가 소유한다.
FINANCE_CAP_CHECK_ID = DEPT_CAP_CHECK_ID["finance"]

#: `_context()` 가 **항상** 읽는 것. PRE_PURCHASE Tool 은 전부 이것을 거친다.
#:
#: 여기에 현금이벤트가 들어가는 이유: `_context` 는 어느 Tool 이 불렀든 채무·채권·급여를
#: 함께 읽어 투영 입력을 만든다. 한 Tool 만 그것을 "쓴다"고 적으면, 그 Tool 이
#: `tool_order` 에 없던 실행에서 **같은 읽기가 사라진 것처럼** 보인다.
_CONTEXT_INPUTS: tuple[str, ...] = (
    "finance_state.current_cash_krw",
    "finance_state.current_debt_krw",
    "finance_policy.cashflow_projection_days",
    "finance_policy.monthly_labor_cost_krw",
    "finance_policy.payroll_date",
    "finance_cash_events.obligations",
    "finance_cash_events.receivables",
)

#: 부채가 있을 때만 읽는다 (`_context` 의 `current_debt > 0` 분기).
_DEBT_CONTEXT_INPUT = "finance_cash_events.debt_service"

#: Tool 이 `_context` **위에서 추가로** 읽는 입력.
#:
#: ★ 이것은 **재무가 소유한 정적 의존 계약**이다 — 실행에서 관측한 것이 아니다.
#:   관측되는 것은 "어느 Tool 이 실제로 돌았는가"(`state.tool_order`)뿐이고, 그 Tool 이
#:   무엇을 읽는지는 여기에 적힌 대로 해석된다. 둘을 섞어 말하면 안 된다.
#:
#: ★ 그래서 **드리프트가 위험하다.** 코드가 새 입력을 읽기 시작했는데 여기를 안 고치면,
#:   Critic 의 등급 누출 검사는 *우리가 적은 것*을 검사하게 된다 — 실제로 읽은 것이
#:   아니라. 매입 소유 입력(`qty_kg` · `grade_unit_price` · `sourcing_plan` …)이 재무
#:   cap 계산에 들어오는 날이 오면 **숨기지 말고 여기에 나타나야 한다.**
_CAP_TOOL_INPUTS: dict[str, tuple[str, ...]] = {
    "assess_finance_position": (
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
        "finance_policy.margin_defense_floor_rate",
    ),
    "project_cashflow": (),
    "calculate_purchase_finance_cap": (
        "base_projection.projected_cash_by_date",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.purchase_payment_days",
    ),
    "analyze_payment_pressure": (
        "base_projection.projected_cash_min",
        "finance_policy.minimum_cash_balance_krw",
        "finance_policy.cash_priority_reference",
        "finance_policy.cash_priority_high_ratio",
        "finance_policy.cash_priority_medium_ratio",
    ),
}

#: Tool 이 **선행으로 요구하는** Tool — 입력 계보용.
#:
#: 🔴 `capability_graph.TOOL_DEPENDENCIES` 와 겹쳐 보이지만 **묻는 것이 다르다.**
#:    저쪽은 *"지금 이 Tool 을 부를 수 있는가"* 이고, 여기는 *"이 Tool 의 결과는
#:    무엇을 읽고 만들어졌는가"* 다. 전이 폐포를 끊으면 cap 을 만든 현금흐름 입력이
#:    `inputs_used` 에서 사라지고, Critic 의 등급 누출 검사가 대상을 못 본다.
_TOOL_PREREQUISITE_TOOLS: dict[str, tuple[str, ...]] = {
    "calculate_purchase_finance_cap": ("project_cashflow",),
    "analyze_payment_pressure": ("project_cashflow",),
}


class FinanceToolDependencyMissing(RuntimeError):
    """실행한 Tool 의 의존 계약이 없다. **조용히 0개로 보고하지 않는다.**

    비어 있는 `inputs_used` 는 Critic 이 *"금지 입력이 없다"* 로 읽고 통과시킨다 —
    모르는 것이 통과가 되는 구조라, 여기서는 크게 실패하는 편이 낫다.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"Finance tool has no declared dependency contract: {tool}")


def _resolve_tool_inputs(tool: str, *, has_debt: bool) -> list[str]:
    """Tool 하나가 읽는 입력의 **전이 폐포**.

    `_context` 공통 입력 + Tool 고유 입력 + 내부에서 부르는 Tool 의 입력.
    """
    if tool not in _CAP_TOOL_INPUTS:
        raise FinanceToolDependencyMissing(tool)
    names: list[str] = [*_CONTEXT_INPUTS]
    if has_debt:
        names.append(_DEBT_CONTEXT_INPUT)
    pending = [tool]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current not in _CAP_TOOL_INPUTS:
            raise FinanceToolDependencyMissing(current)
        names.extend(_CAP_TOOL_INPUTS[current])
        pending.extend(_TOOL_PREREQUISITE_TOOLS.get(current, ()))
    return names


def _observed_has_debt(states: list[FinanceAgentState]) -> bool:
    """이번 실행에서 부채 일정을 실제로 읽었는가.

    `_context` 가 `current_debt > 0` 일 때만 읽으므로, 고정 선언이 아니라 그날의
    상태에서 판단한다 — 읽지 않은 것을 읽었다고 적지 않기 위해서다.
    """
    for state in states:
        cache = state.context_cache
        if cache is None:
            continue
        position = cache[0]
        debt = position.get("current_debt_krw")
        if debt is not None and Decimal(str(debt)) > 0:
            return True
    return False


def _finance_dept_meta(
    mode: str, payload: dict[str, Any], states: list[FinanceAgentState]
) -> dict[str, Any] | None:
    """이번 실행의 사용 입력·산출 필드를 **재무 자신이** 기계가 읽을 형태로 낸다.

    Critic 의 `E-GRADE-LEAK`(재무 cap 에 등급·수량이 섞였나)와 `E-AUTHORITY`(부서가
    S3 전속 판정을 냈나)는 이 둘이 없으면 아예 돌지 않는다 — 통과가 아니라 **생략**이다.

    ★ **마스터가 추측하면 안 되는 것이라 재무가 낸다.** 마스터는 Tool 이름이나
      payload 키를 보고 *"재무가 무엇을 읽었는지"* 를 알 수 없다. 모르는 것을 빈
      dict 로 보내면 Critic 은 *"금지 입력이 없다"* 로 읽고 **통과시킨다** — 모르는
      것이 통과가 되는 구조라, 마스터는 아예 안 보내고 생략으로 남겨 왔다.

    ★ **관측이지 선언이 아니다.** `inputs_used` 는 실행에서 실제로 성공한 Tool
      (`state.tool_order`)만 보고 만든다. `produced_fields` 는 실제로 실린 payload
      키다. 둘 다 실행과 어긋날 수 없다.

    PRE_PURCHASE 만 낸다 — Critic 의 두 검사가 조언자 경계 회신을 대상으로 한다.
    """
    if not states:
        return None
    if mode == "SCENARIO_VALIDATION":
        # 시나리오 판정에는 재무 cap 검사(`E-GRADE-LEAK`)에 해당하는 축이 없다.
        # 없는 검사에 가짜 `inputs_used` 를 지어내지 않고, 권한 검사(`E-AUTHORITY`)가
        # 볼 수 있게 **실제 산출 필드만** 낸다.
        return {
            "observation_type": "finance_dept_meta",
            "inputs_used": {},
            "produced_fields": _produced_fields(payload),
        }
    if mode != "PRE_PURCHASE":
        return None
    executed = [tool for state in states for tool in state.tool_order]
    has_debt = _observed_has_debt(states)
    inputs: list[str] = []
    for tool in executed:
        for name in _resolve_tool_inputs(tool, has_debt=has_debt):
            if name not in inputs:
                inputs.append(name)
    return {
        "observation_type": "finance_dept_meta",
        "inputs_used": {FINANCE_CAP_CHECK_ID: inputs},
        "produced_fields": _produced_fields(payload),
    }


def _produced_fields(payload: dict[str, Any]) -> list[str]:
    """이번 회신에 **실제로 실린** 필드.

    값이 `None` 인 키는 뺀다 — 어댑터가 경계에서 실제로 빼는 것과 같은 기준이다
    (`_controller_run` 의 `margin_defense_floor_rate`). 산출하지 않은 필드를
    산출했다고 적으면 권한 검사가 엉뚱한 것을 본다.
    """
    return sorted(key for key, value in payload.items() if value is not None)


def _assert_dependency_contract_is_complete(pre_purchase_tools: frozenset[str]) -> None:
    """PRE_PURCHASE Tool 은 **전부** 의존 계약을 가져야 한다.

    기동 시점에 확인한다 — Tool 을 새로 만들고 계약을 안 적으면, 그 사실이 조용한
    `inputs_used` 누락이 아니라 **import 실패**로 즉시 드러난다.

    ★ Tool 목록을 **받아서** 검사한다. 여기서 Harness 를 import 하면 순환이 된다
      (Harness → capabilities → execution). 부르는 쪽은 `application.harness` 이고,
      실행 경로는 반드시 그 모듈을 지나므로 확인 시점은 그대로다.
    """
    undeclared = sorted(pre_purchase_tools - set(_CAP_TOOL_INPUTS))
    if undeclared:
        raise FinanceToolDependencyMissing(", ".join(undeclared))
    unknown_targets = sorted(
        {target for targets in _TOOL_PREREQUISITE_TOOLS.values() for target in targets}
        - set(_CAP_TOOL_INPUTS)
    )
    if unknown_targets:
        raise FinanceToolDependencyMissing(", ".join(unknown_targets))


# ---------------------------------------------------------------------------
# 실행이력 — 저장(append-only)과 조회
# ---------------------------------------------------------------------------

class FinanceAgentRun(TypedDict):
    run_id: UUID
    cycle: FinanceCycle
    as_of: date
    snapshot_id: str | None
    runtime_status: RuntimeStatus
    verdict: FinalVerdict | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    created_at: datetime


_SELECT_COLUMNS = sql.SQL(
    """
    SELECT
        run_id,
        cycle,
        as_of,
        snapshot_id,
        runtime_status,
        verdict,
        request_payload,
        response_payload,
        created_at
    FROM {}.finance_agent_runs
    """
)


def save_finance_agent_run(
    *,
    cycle: FinanceCycle,
    as_of: date,
    snapshot_id: str | None,
    runtime_status: RuntimeStatus,
    verdict: FinalVerdict | None,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> FinanceAgentRun:
    """완성된 Finance Agent Request와 Response를 실행이력으로 저장한다."""
    if response_payload.get("verdict") != verdict:
        raise ValueError("Finance run verdict metadata must match response_payload.verdict")
    query = sql.SQL(
        """
        INSERT INTO {}.finance_agent_runs (
            run_id,
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            verdict,
            request_payload,
            response_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING
            run_id,
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            verdict,
            request_payload,
            response_payload,
            created_at
        """
    ).format(sql.Identifier(get_db_schema()))
    row = execute_returning_one(
        query,
        (
            uuid4(),
            cycle,
            as_of,
            snapshot_id,
            runtime_status,
            verdict,
            Jsonb(request_payload),
            Jsonb(response_payload),
        ),
    )
    return cast(FinanceAgentRun, row)


def get_finance_agent_run(run_id: UUID) -> FinanceAgentRun:
    """run_id로 Finance Agent 실행이력 한 건을 조회한다."""
    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema())) + sql.SQL(" WHERE run_id = %s")
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"Finance Agent run was not found: {run_id}")
    return cast(FinanceAgentRun, row)


def list_finance_agent_runs(
    *,
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: int = 100,
) -> list[FinanceAgentRun]:
    """선택한 필터로 최신 Finance Agent 실행이력을 조회한다."""
    conditions: list[sql.Composable] = []
    params: list[object] = []
    if cycle is not None:
        conditions.append(sql.SQL("cycle = %s"))
        params.append(cycle)
    if as_of is not None:
        conditions.append(sql.SQL("as_of = %s"))
        params.append(as_of)
    if runtime_status is not None:
        conditions.append(sql.SQL("runtime_status = %s"))
        params.append(runtime_status)
    if verdict is not None:
        conditions.append(sql.SQL("verdict = %s"))
        params.append(verdict)

    query = _SELECT_COLUMNS.format(sql.Identifier(get_db_schema()))
    if conditions:
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
    query += sql.SQL(" ORDER BY created_at DESC, run_id DESC LIMIT %s")
    params.append(limit)
    return cast(list[FinanceAgentRun], fetch_all(query, params))


def save_finance_execution(
    *, request: AgentRequest, reply: AgentReply, metadata: ExecutionMetadata
) -> None:
    """마이그레이션이 설치된 경우 v2.2 하위 trace를 저장한다.

    저장 실패는 의도적으로 삼키지 않는다. 정상적인 Business 완료에는 해석 가능한
    run_id가 반드시 있어야 한다.
    """
    query = sql.SQL(
        """
        INSERT INTO {}.finance_agent_runs_v22 (
            run_id, request_id, agent, mode, as_of, policy_version, trigger, call_seq,
            runtime_status,
            business_status, request_payload, response_payload,
            used_tools, tool_order, observations, rules_applied, replans,
            llm_status, llm_model, llm_attempts, llm_fallback_used, elapsed_ms
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """
    ).format(sql.Identifier(get_db_schema()))
    execute_returning_one(
        query + sql.SQL(" RETURNING run_id"),
        (
            UUID(reply.run_id),
            request.context.request_id,
            "finance",
            request.mode,
            request.context.as_of,
            request.context.policy_version,
            request.context.trigger,
            request.call_seq,
            reply.runtime_status,
            reply.business_status,
            Jsonb(dict(request.payload)),
            Jsonb(dict(reply.payload)),
            Jsonb(list(metadata.used_tools)),
            Jsonb(list(metadata.tool_order)),
            Jsonb(list(metadata.observations)),
            Jsonb(list(metadata.rules_applied)),
            metadata.replans,
            metadata.llm_status,
            metadata.llm_model,
            metadata.llm_attempts,
            metadata.llm_fallback_used,
            metadata.elapsed_ms,
        ),
    )


def get_finance_execution(run_id: UUID) -> dict[str, object]:
    query = sql.SQL("SELECT * FROM {}.finance_agent_runs_v22 WHERE run_id = %s").format(
        sql.Identifier(get_db_schema())
    )
    row = fetch_one(query, (run_id,))
    if row is None:
        raise LookupError(f"Finance v2.2 run was not found: {run_id}")
    return row


# ---------------------------------------------------------------------------
# 실행이력 조회 서비스 (UI · `/finance/runs`)
# ---------------------------------------------------------------------------

def get_finance_run(run_id: UUID) -> FinanceAgentRunResponse:
    """UI 조회용 Finance Agent 실행이력 한 건을 반환한다."""
    return FinanceAgentRunResponse.model_validate(get_finance_agent_run(run_id))


def list_finance_runs(
    *,
    cycle: FinanceCycle | None = None,
    as_of: date | None = None,
    runtime_status: RuntimeStatus | None = None,
    verdict: FinalVerdict | None = None,
    limit: int = 100,
) -> list[FinanceAgentRunResponse]:
    """UI 조회용 Finance Agent 실행이력 목록을 반환한다."""
    rows = list_finance_agent_runs(
        cycle=cycle,
        as_of=as_of,
        runtime_status=runtime_status,
        verdict=verdict,
        limit=limit,
    )
    return [FinanceAgentRunResponse.model_validate(row) for row in rows]
