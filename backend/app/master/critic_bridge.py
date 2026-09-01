"""
critic_bridge.py — 마스터 검증 Tool ↔ Critic 56검사 번역

    마스터가 가진 것            Critic 이 받는 것
    ─────────────────────      ──────────────────────────
    매입 제안 payload           ScenarioIn[]
    조언자 경계 payload         DeptReplyIn[] (CheckIn 의 cap 축)
    조언자 Evidence            EvidenceIn[]
                               → CriticProcurementRequest

★ **번역만 한다.** 판정은 `app.critic` 이 내리고, 여기는 이름을 옮긴다.

★ **없는 것을 만들지 않는다.**
  `inputs_used` 는 *"재무가 cap 을 낼 때 무엇을 읽었나"* 인데 **마스터는 그걸 모른다.**
  빈 dict 를 보내면 Critic 의 등급 누출 검사가 **"금지 입력이 없다"로 읽고 통과**한다 —
  모르는 것이 통과가 된다. 그래서 오랫동안 아무것도 안 보냈고, Critic 은 `skipped` 에
  *"DeptMeta 미제출 — 생략"* 을 남겼다 (설계서 §8).

  🔴 **이제는 부서가 직접 낸다.** 재무가 자기 실행을 보고 `finance_dept_meta` 관측을
  `ExecutionMetadata.observations` 에 적고(`app.finance.agent._finance_dept_meta`),
  마스터는 그것을 **해석하지 않고 나른다.** 여기서 하는 일은 관측 JSON 을 Critic 의
  `DeptMetaIn` 모양으로 옮기는 것뿐이다 — Tool 이름을 보고 입력을 추정하거나
  payload 키로 의미를 짐작하지 않는다. 부서가 안 적었으면 여전히 안 보낸다.

★ **항등식이 깨진 제안은 넘기지 않는다.**
  Critic 의 금액 축은 `qty_kg × unit_price_krw_per_kg` 로 다시 만들어진다. 그 단가는
  매입이 주장한 `total_amount_krw / total_qty_kg` 에서 온다 — **두 값이 서로 맞을 때만**
  성립하는 표현이다. 항등식이 이미 깨졌다면 그 위에서 돌린 판정은 **그럴듯하지만
  아무 의미가 없다.** 넘기지 않고 그 사실을 `skipped` 로 남긴다.

★ **LLM 을 타지 않는다.** `rationale` 을 비워 보낸다 — L5 판정 대상은 오케스트레이터
  selector 가 쓴 문장인데 1차 Flow 에는 그 단계가 없다. 빈 문자열이면 Critic 이
  judge 를 돌리지 않고 `skipped` 에 남긴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from app.critic.schemas import CriticProcurementRequest, CriticVerdictOut
from app.master.envelope import AgentName
from app.orchestrator.contracts_core import Evidence

_FINANCE = "finance"
_INVENTORY = "inventory"

_FINANCE_CAP_CHECK = "finance_cap_amount_krw"
_INVENTORY_CAP_CHECK = "warehouse_cap"

_FINANCE_CAP_CLAIMS: frozenset[str] = frozenset(
    {"finance_cap_amount_krw", "available_cash", "base_projected_cash_min"}
)
"""재무 금액 cap 을 **직접 뒷받침하는** 근거만 이 check 에 붙인다.

`payment_pressure` 처럼 다른 판정을 뒷받침하는 근거까지 붙이면 *"이 cap 의 근거"* 가
흐려진다. 근거는 많이 붙이는 것이 아니라 **가리키는 것이 맞아야** 한다.
"""

_INVENTORY_CAP_CLAIMS: frozenset[str] = frozenset(
    {
        "warehouse_free_kg",
        "cap_by_date",
        "guaranteed_capacity_kg",
        "used_capacity_kg",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
    }
)


#: 🔴 **L2 가 실제로는 값을 대조하지 못한다.** 통과로 세지 않도록 매번 적는다.
#:
#: Critic 의 대조표(`service._evidence_resolver`)는 **회신이 낸 evidences 자신으로**
#: 만들어진다. 독립 원본이 아니므로 *"주장값이 실제와 다르다"* 는 구조적으로 잡을 수
#: 없다. 지금 L2 가 잡는 것은 근거 누락 · 없는 ref_id · 같은 주장을 두 값으로 내는
#: 모순 셋뿐이다.
#:
#: 이걸 적어 두지 않으면 `findings: []` 가 *"근거 숫자를 다 대조했고 문제없다"* 로
#: 읽힌다 — §3.7.6 이 막으려는 바로 그 오독이다. 진짜 대조를 하려면 마스터가 부서
#: payload 를 원본으로 하는 resolver 를 주입해야 하고, 그건 별건이다.
_L2_VALUE_NOT_CHECKED = (
    "L2 근거 값 대조: 수행하지 못함 — 대조표가 회신 자신에서 만들어져 독립 원본이 없다 "
    "(누락·없는 ref_id·자기모순은 대조함)"
)


class CriticSkipped(Exception):
    """넘길 수 없는 이유. **예외로 새지 않고 `skipped` 문장이 된다.**"""


def build_request(
    *,
    as_of: date,
    item: str | None,
    proposal: Mapping[str, Any],
    constraints: Mapping[AgentName, Mapping[str, Any]],
    evidences: Mapping[AgentName, Sequence[Evidence]],
    observations: Mapping[AgentName, Sequence[str]] | None = None,
    run_seq: int = 1,
) -> CriticProcurementRequest:
    """마스터 상태 → `CriticProcurementRequest`.

    넘길 수 없으면 `CriticSkipped` 를 올린다 — **부르는 쪽이 `skipped` 로 적는다.**
    """
    if not item:
        raise CriticSkipped("품목이 정해지지 않아 Critic 에 넘기지 못했다 (M-26 · 품목 축)")

    scenarios = _scenarios_in(proposal, item, as_of, constraints)
    if not scenarios:
        raise CriticSkipped("시나리오를 Critic 입력으로 옮기지 못했다 — 항등식 또는 필수 축 결측")

    replies = _replies_in(constraints, evidences)
    if not replies:
        raise CriticSkipped("조언자 경계를 Critic 입력으로 옮기지 못했다 — 밴드 축 결측")

    return CriticProcurementRequest(
        as_of=as_of,
        run_seq=run_seq,
        items=[item],
        inbound_lead_days=_int_of((constraints.get(_INVENTORY) or {}).get("inbound_lead_days")),
        scenarios=scenarios,
        replies=replies,
        # ★ dept_meta 는 **부서가 적어 보낸 것만** 옮긴다 (모듈 주석 참고).
        #   rationale 은 비운다 — L5 는 오케 selector 문장을 검사하는데 1차 Flow 에
        #   그 단계가 없다.
        dept_meta=_dept_meta_in(observations) or None,
        rationale="",
    )


def _dept_meta_in(
    observations: Mapping[AgentName, Sequence[str]] | None,
) -> dict[str, dict[str, Any]]:
    """부서 관측 → Critic `DeptMetaIn`. **번역만 한다.**

    ★ 부서가 `<dept>_dept_meta` 관측을 적었을 때만 만든다. 안 적었으면 그 부서는
      목록에 없고, Critic 은 예전처럼 *"DeptMeta 미제출 — 생략"* 을 남긴다.
      **빈 dict 를 채워 넣지 않는다** — 그러면 모르는 것이 통과가 된다.

    ★ 모양이 어긋난 관측도 조용히 버린다. 부서가 잘못 적은 것을 마스터가 고쳐
      주면, 고친 값이 근거가 된다.
    """
    out: dict[str, dict[str, Any]] = {}
    for dept, items in (observations or {}).items():
        for raw in items:
            try:
                observation = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(observation, dict):
                continue
            if observation.get("observation_type") != f"{dept}_dept_meta":
                continue
            inputs_used = observation.get("inputs_used")
            produced = observation.get("produced_fields")
            if not isinstance(inputs_used, dict) or not isinstance(produced, list):
                continue
            # 🔴 **덮어쓰지 않고 합친다.** 부서는 mode 마다 관측을 하나씩 낸다 —
            #    재무는 경계에서 `inputs_used` 를, 시나리오 판정에서 그쪽 산출 필드를
            #    낸다. 마지막 것만 남기면 시나리오 관측(`inputs_used` 가 빈)이 경계의
            #    cap 입력을 **지워** 등급 누출 검사가 조용히 통과한다.
            #
            #    합치는 것은 해석이 아니다 — 부서가 적은 기록을 모으기만 한다.
            merged = out.setdefault(dept, {"inputs_used": {}, "produced_fields": []})
            for check, names in inputs_used.items():
                if not isinstance(names, list):
                    continue
                target = merged["inputs_used"].setdefault(str(check), [])
                for name in names:
                    if str(name) not in target:
                        target.append(str(name))
            for name in produced:
                if str(name) not in merged["produced_fields"]:
                    merged["produced_fields"].append(str(name))
    return out


def fold(verdict: CriticVerdictOut) -> tuple[list[str], list[str], list[str]]:
    """Critic 판정 → 마스터 검증의 3단 (findings / concerns / skipped).

    ★ `coverage_ratio` 를 **`skipped` 에 항상 적는다.** `findings: []` 만 보면
      *"56검사를 통과했다"* 로 읽힌다. 실제로 몇 개가 돌았는지가 같이 보여야 한다.
    """
    findings = [
        f"CRITIC/{f.layer}/{f.check_id}: {f.detail}" + (f" (부서 {f.dept})" if f.dept else "")
        for f in verdict.findings
    ]
    concerns = [f"CRITIC/{c.code}: {getattr(c, 'detail', '') or c.code}" for c in verdict.concerns]

    ran, total = verdict.coverage_ratio
    layers = " · ".join(f"{name} {a}/{b}" for name, (a, b) in sorted(verdict.coverage.items()))
    skipped = [f"Critic 커버리지 {ran}/{total} — {layers}", _L2_VALUE_NOT_CHECKED]
    skipped += [f"CRITIC: {s}" for s in verdict.skipped]
    return findings, concerns, skipped


# ---------------------------------------------------------------------------
# 시나리오
# ---------------------------------------------------------------------------


def _scenarios_in(
    proposal: Mapping[str, Any],
    item: str,
    as_of: date,
    constraints: Mapping[AgentName, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    lead = _int_of((constraints.get(_INVENTORY) or {}).get("inbound_lead_days"))
    out: list[dict[str, Any]] = []
    for scenario in proposal.get("scenarios") or ():
        if not isinstance(scenario, Mapping):
            continue
        converted = _scenario_in(scenario, item, as_of, lead)
        if converted is not None:
            out.append(converted)
    return out


def _scenario_in(
    scenario: Mapping[str, Any], item: str, as_of: date, lead: int | None
) -> dict[str, Any] | None:
    label = scenario.get("label")
    qty = _float_of(scenario.get("total_qty_kg"))
    amount = _float_of(scenario.get("total_amount_krw"))
    if not isinstance(label, str) or qty is None or amount is None or qty <= 0:
        return None

    # ★ 품목 단일가 — 매입에는 **등급별 단가만** 있다.
    #
    #   Critic 의 금액 축은 qty × unit_price 로 다시 만들어진다. 여기에 등급 단가 중
    #   하나를 고르거나 평균을 내면 **매입이 주장한 금액과 다른 금액을 검사**하게 된다.
    #   total_amount / total_qty 만이 그 곱을 원래 금액으로 되돌린다.
    #
    #   ⚠️ 이 표현은 **항등식이 성립할 때만** 뜻이 있다. 마스터가 L-IDENTITY-QTY ·
    #      L-IDENTITY-AMOUNT 로 그 둘을 이미 독립 재검산하고, 깨져 있으면 부르는 쪽이
    #      아예 Critic 을 돌리지 않는다.
    unit_price = amount / qty

    return {
        "scenario_id": label,
        # 매입 `label` 과 Critic `stance` 는 **같은 어휘**다 (보수·기본·공격).
        # 우연이 아니라 둘 다 정의서 §4.2 를 따랐다.
        "stance": label,
        "strategy_type": scenario.get("strategy_type") or "quantity",
        "qty_kg": {item: qty},
        "unit_price_krw_per_kg": {item: unit_price},
        "split_plan": _split_legs(scenario, item, as_of, lead),
        "sourcing_plan": _sourcing_lots(scenario, item),
        "total_amount_krw": amount,
        "margin_warning": scenario.get("margin_warning"),
    }


def _split_legs(
    scenario: Mapping[str, Any], item: str, as_of: date, lead: int | None
) -> list[dict[str, Any]]:
    """절대 날짜 → `offset_days`. **되돌릴 수 있는 변환만** 한다.

    ★ `expected_arrival_date` 는 물류의 `calculate_expected_arrival_dates` 와 같은
      규칙(매입일 + 리드타임)이다. 리드타임을 못 받았으면 **비운다** — 0 일로 치면
      도착일 분해 검사가 통과해 버린다.
    """
    out: list[dict[str, Any]] = []
    for leg in scenario.get("split_plan") or ():
        if not isinstance(leg, Mapping):
            continue
        day = _date_of(leg.get("date"))
        qty = _float_of(leg.get("qty_kg"))
        if day is None or qty is None:
            continue
        offset = (day - as_of).days
        if offset < 0:
            continue  # 과거 날짜의 회차는 옮기지 않는다 — 별도 검사 대상이다
        out.append(
            {
                "offset_days": offset,
                "qty_kg": {item: qty},
                "expected_arrival_date": (
                    (day + timedelta(days=lead)).isoformat() if lead is not None else None
                ),
            }
        )
    return out


def _sourcing_lots(scenario: Mapping[str, Any], item: str) -> list[dict[str, Any]]:
    """★ `ref_ids` 를 지어내지 않는다.

    매입 `sourcing_plan[]` 에는 참조가 없다 — 근거는 `rationale[]` 에 따로 있고 회차와
    묶이지 않는다(M-25 가 B 로 정리된 경계). 빈 목록으로 두면 Critic 이 그 사실을
    자기 검사로 드러낸다.
    """
    out: list[dict[str, Any]] = []
    for lot in scenario.get("sourcing_plan") or ():
        if not isinstance(lot, Mapping):
            continue
        qty = _float_of(lot.get("qty_kg"))
        price = _float_of(lot.get("grade_unit_price"))
        grade = lot.get("grade")
        if qty is None or price is None or qty <= 0 or price <= 0 or not isinstance(grade, str):
            continue
        out.append(
            {
                "item": item,
                "grade": grade,
                "qty_kg": qty,
                "unit_price_krw_per_kg": price,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 조언자 회신
# ---------------------------------------------------------------------------


def _replies_in(
    constraints: Mapping[AgentName, Mapping[str, Any]],
    evidences: Mapping[AgentName, Sequence[Evidence]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    finance = constraints.get(_FINANCE)
    if finance is not None:
        cap = _float_of(finance.get("finance_cap_amount_krw"))
        if cap is not None:
            out.append(
                {
                    "dept": _FINANCE,
                    "runtime_status": "READY",
                    "checks": [
                        {
                            "check_id": _FINANCE_CAP_CHECK,
                            "kind": "hard",
                            "verdict": "ok",
                            "cap_amount_krw": cap,
                            "evidences": _evidences_in(
                                evidences.get(_FINANCE), _FINANCE_CAP_CLAIMS
                            ),
                        }
                    ],
                }
            )

    inventory = constraints.get(_INVENTORY)
    if inventory is not None:
        cap_total = _float_of(inventory.get("warehouse_free_kg"))
        cap_by_date = _cap_by_date(inventory.get("cap_by_date"))
        if cap_total is not None or cap_by_date:
            check: dict[str, Any] = {
                "check_id": _INVENTORY_CAP_CHECK,
                "kind": "hard",
                "verdict": "ok",
                "evidences": _evidences_in(evidences.get(_INVENTORY), _INVENTORY_CAP_CLAIMS),
            }
            if cap_total is not None:
                check["cap_total_kg"] = cap_total
            if cap_by_date:
                check["cap_by_date_kg"] = cap_by_date
            out.append({"dept": _INVENTORY, "runtime_status": "READY", "checks": [check]})

    return out


def _evidences_in(
    evidences: Sequence[Evidence] | None, claims: frozenset[str]
) -> list[dict[str, Any]]:
    """이 check 를 뒷받침하는 근거만 고른다. **없으면 빈 목록이다** — 채우지 않는다."""
    return [
        {
            "claim": e.claim,
            "source": e.source,
            "ref_ids": list(e.ref_ids),
            "value": e.value,
            "unit": e.unit,
            "evidence_grade": e.evidence_grade,
            "evidence_detail": e.evidence_detail,
        }
        for e in (evidences or ())
        if e.claim in claims and e.ref_ids
    ]


def _cap_by_date(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        day = _date_of(key)
        amount = _float_of(value)
        if day is not None and amount is not None:
            out[day.isoformat()] = amount
    return out


# ---------------------------------------------------------------------------
# 읽기 도우미 — **읽지 못한 것을 0 으로 만들지 않는다** (§1.2-10)
# ---------------------------------------------------------------------------


def _float_of(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_of(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _date_of(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
