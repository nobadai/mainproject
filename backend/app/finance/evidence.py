"""Evidence 생성과 정책 출처 규율 — **재무 근거의 진실성을 한곳에서 지킨다.**

★ 없는 출처를 지어내지 않는다. 가짜 ref 는 따라갔을 때 아무 데도 닿지 않고, 그 사실을
  아무도 모른 채 판정만 남는다.

★ 급여 두 키만 fail-closed 다 (`_PAYROLL_SOURCE_KEYS`). 나머지 정책값은 값 자체를 쓸 수
  있으므로 실행을 세우지 않고, 근거를 못 다는 claim 만 빼서 `missing_data` 로 밝힌다.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from app.finance.repository import FinanceDataNotReady
from app.finance.schemas import FinancePolicy
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment

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
    return SuggestedAdjustment(
        dept="finance",
        axis="amount",
        target_value=value["target_value"],
        unit=value["unit"],
        reason=value["reason"],
        ref_ids=tuple(value["ref_ids"]),
    )
