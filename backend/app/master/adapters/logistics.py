"""
adapters/logistics.py — 재고·물류 에이전트 접점 (마스터 ↔ 물류)

    AgentPort = (AgentRequest) -> (AgentReply, ExecutionMetadata)

★ **어댑터는 계산하지 않는다.**
  숫자는 전부 `app.logistics.tools` · `app.logistics.rules` 의 결정론 함수가 만든다.
  여기가 하는 일은 **번역**뿐이다 (재무 어댑터와 같은 규율).

★ **없는 값을 만들지 않는다.**
  `rental_cap_kg` 을 `burst − guaranteed` 로 채우지 않았다. burst 는 3PL 의 순간 초과
  허용이고 rental 은 창고 임대다 — **다른 개념**이라 섞으면 숫자는 나오고 에러도 안
  나며 검증도 통과한다. 비워 두고 물었더니 **물류가 `0` 으로 확정**했다
  (2026-08-27 회신 §1). 추측이 아니라 소유 파트의 답이므로 이제 싣는다.

  ★ **`0` 은 미확정이 아니다** — *"1차 MVP 에서 임차 가능량이 0 으로 확정"* 이다.
    누락으로 되돌리지 않는다 (물류 회신 §7).

★ **LLM 을 타지 않는다.**
  `run_logistics_procurement_with_snapshot()` 은 마지막에 `enrich_logistics_response()`
  로 해석 서비스를 부른다. 마스터 경로에서는 그 앞 단계(`scenario_engine` · `rules`)만
  직접 부른다 — 판정에 필요한 것은 전부 거기서 나오고, **`llm_status="DISABLED"` 라는
  말이 사실이 된다.**

★ **`as_of` 는 마스터가 준 것을 쓴다** (§1.2-6).

물류 확정분 근거 — `agent_policy_config` domain=logistics · `MVP-DECISION-20260825`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.logistics.repository import (
    get_active_logistics_policy,
    get_current_inventory_logistics_snapshot,
)
from app.logistics.rules import derive_logistics_verdict, evaluate_procurement_rules
from app.logistics.scenario_engine import run_logistics_procurement_scenario
from app.logistics.schemas import InventoryLogisticsSnapshot, LogisticsPolicy
from app.logistics.tools import build_lot_constraints, calculate_cap_by_date
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata, Verdict
from app.orchestrator.contracts_core import Evidence
from app.purchase_agent.schemas import PurchaseProposal

_AGENT = "inventory"

# 물류 Tool — 실제로 부른 것만 남긴다.
_T_RULES = "evaluate_procurement_rules"
_T_CAP = "calculate_cap_by_date"
_T_ARRIVAL = "calculate_expected_arrival_dates"
_T_LOTS = "build_lot_constraints"

_JUDGMENT_FIELDS = ("cap_by_date_policy",)

_CAP_WINDOW_DAYS = 18
"""PRE_PURCHASE 에서 `cap_by_date` 를 뽑는 **조회 창**의 길이.

★ **제약값이 아니다.** 물류의 `calculate_cap_by_date()` 는 도착일 목록을 받는데,
  제안 전에는 도착일이 없다. 그래서 `as_of + lead` 부터 이 길이만큼 훑는다.

  짧으면 매입이 **덜 볼 뿐** 값이 달라지지 않는다. 18 인 것은 매입 커버일수 상한
  D+18(ML 지평)에서 왔다 — 그보다 뒤의 날짜는 매입이 쓰지 않는다.

  창의 길이 자체는 `cap_by_date_window_days` 로 payload 에 밝힌다. 받는 쪽이
  *"이 날짜까지밖에 안 왔다"* 를 알아야 없는 날을 0 으로 읽지 않는다.
"""

_RULE_PREFIX = "logistics_rule/"
"""물류 규칙이 낸 `ConstraintCode` 에 붙이는 접두어 — 출처를 이름에 남긴다."""

_RENTAL_CAP_KEY = "rental_cap_kg"
_RENTAL_CAP_KG = 0.0
_RENTAL_CAP_REF = "LOGISTICS-REPLY-20260827:rental_cap_kg"
"""외부 창고 임차 상한. **1차 MVP 는 임차 기능이 없다** (2026-08-27 물류 회신 §1 · §6).

★ `0` 은 *"모른다"* 가 아니라 *"임차 가능량이 0 으로 확정됐다"* 다. 매입은 이 값을
  창고 상한에 더하므로 둘의 구분이 결과를 바꾼다 — 모르는 값을 0 으로 쓰면 **살 수
  있는 양을 실제보다 적게** 잡고, 확정 0 을 미확정으로 두면 **매입이 아예 못 돈다.**

★ 상수로 둔 것은 DB 에 키가 없어서다. `missing_data` 에 출처 부재를 남긴다.
"""

_VERDICT_MAP: Mapping[str, Verdict] = {
    "PASS": "ok",
    "REVIEW_REQUIRED": "conditional",
    "FAIL": "reject",
}
"""물류 `FinalVerdict` → 공통 `Verdict`.

`REVIEW_REQUIRED` 를 `conditional` 로 옮기는 것은 재무와 같은 매핑이다 (정의서 §7.1).
"""


def logistics_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 접점."""
    if request.mode == "PRE_PURCHASE":
        return _pre_purchase(request)
    if request.mode == "SCENARIO_VALIDATION":
        return _scenario_validation(request)
    return _not_implemented(request)


# ---------------------------------------------------------------------------
# PRE_PURCHASE — 경계 제공
# ---------------------------------------------------------------------------


def _pre_purchase(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_RULES]

    snapshot = _load_snapshot(as_of)
    if snapshot is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("logistics_snapshot", "logistics_runtime_fixture"),
            reason="물류 스냅샷을 읽지 못했다",
        )

    # ★ as_of 대조 — 다른 날의 재고는 그날의 사실이 아니다 (§1.2-6)
    if snapshot.as_of != as_of:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=(f"logistics_snapshot@{as_of.isoformat()}",),
            reason=f"물류 스냅샷 기준일이 {snapshot.as_of} 다 — 요청은 {as_of} 다",
        )

    policy = _load_policy()
    rules = evaluate_procurement_rules(as_of=as_of, snapshot=snapshot)

    # 🔴 물류가 **못 돌겠다고 한 이유**를 그대로 옮긴다 (2026-08-27 물류 B-1 회신 §5).
    #
    # ★ 물류가 `missing_data` 필드를 새로 만들 필요가 없다. `ConstraintResult` 에
    #   `code` · `status` · `skip_reason` 이 이미 있고 어휘도 이미 있다
    #   (`IN_TRANSIT_SCHEDULE_UNRESOLVED` 등). **번역이 어댑터 일이다.**
    #
    # ★ 이걸 안 옮기면 `runtime_status` 만 NOT_READY 로 오고 **왜인지가 안 남는다.**
    #   마스터가 사용자에게 무엇을 달라고 할지 모른다 (M-1 §5.1).
    #
    # ★ 그리고 계약상 필요하다 — `RUNTIME_NOT_READY` 는 `missing_data` 가 비면
    #   `ContractViolation` 이다. 지금은 다른 이름이 우연히 채워 주고 있을 뿐이라,
    #   그 이름이 사라지는 날 봉투가 터진다.
    # ★ `logistics_rule/` 을 앞에 붙인다. 물류 규칙이 말한 것과 **어댑터가 payload 를
    #   만들다 못 채운 것**을 구분하기 위해서다 — `LOG-H01` 이 UNRESOLVED 면
    #   `guaranteed_capacity_kg` 도 비는데, 접두어가 없으면 같은 사실이 두 이름으로
    #   섞여 어느 쪽이 원인인지 흐려진다.
    missing: list[str] = [
        f"{_RULE_PREFIX}{c.code}" for c in rules["hard_constraints"] if c.status != "PASS"
    ]
    payload: dict[str, Any] = {}

    # ── 창고 여유 ────────────────────────────────────────────────
    #
    # ★ 뺄셈의 정의를 지어내지 않았다 — 물류 자신의 `calculate_cap_by_date()` 가
    #   `free_capacity = guaranteed_capacity_kg − projected_occupancy` 로 쓴다.
    #   확정 입·출고가 없는 as_of 시점의 점유는 `used_capacity_kg` 다.
    #
    # ★ 기준이 `guaranteed`(8,000)이지 `burst`(9,600)가 아닌 것도 물류 코드가 정한
    #   것이다. 페르소나의 6,400kg 은 수요 역산이라 하드 제약으로 못 쓴다
    #   (`INVALID_FOR_HARD_N2`) — DB 의 독립 SLA 값을 쓴다.
    free_kg = _free_capacity(snapshot)
    if free_kg is None:
        missing.append("guaranteed_capacity_kg")
    else:
        payload["warehouse_free_kg"] = _num(free_kg)

    # `rental_cap_kg` — 2026-08-27 물류 회신 §1 로 **0 확정**.
    #
    # ★ DB `agent_policy_config` 에는 아직 이 키가 없다. 값은 쓰되 **출처가 DB 가
    #   아니라는 사실**을 밝힌다 — 재무 `payroll_date` 가 Schema default 로 조용히
    #   쓰이던 것과 같은 자리다. 등록되면 이 줄이 저절로 사라진다.
    payload["rental_cap_kg"] = _num(_RENTAL_CAP_KG)
    if policy is None or _RENTAL_CAP_KEY not in policy.source_refs:
        missing.append(f"{_RENTAL_CAP_KEY}@policy_source_ref")

    payload.update(
        {
            "used_capacity_kg": _num(snapshot.used_capacity_kg),
            "cap_by_date_policy": policy.cap_by_date_policy if policy else "UNKNOWN",
            "cap_by_date_window_days": _CAP_WINDOW_DAYS,
        }
    )
    for name in (
        "guaranteed_capacity_kg",
        "burst_capacity_kg",
        "inbound_lead_days",
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
    ):
        value = getattr(snapshot, name)
        if value is None:
            missing.append(name)
        else:
            payload[name] = _num(value)

    # ── 날짜별 입고 Band ─────────────────────────────────────────
    tools.append(_T_CAP)
    cap = _cap_window(snapshot, as_of)
    if cap is None:
        # 계산 불능은 빈 dict 로 덮지 않는다 — 0 과 "모름"은 다르다 (§1.2-10)
        missing.append("cap_by_date")
    else:
        payload["cap_by_date"] = {d.isoformat(): _num(v) for d, v in sorted(cap.items())}

    tools.append(_T_LOTS)
    payload["lots"] = [
        {
            "lot_id": lot.lot_id,
            "item": lot.item,
            "available_qty_kg": _num(lot.available_qty_kg),
            # 신선도는 **없을 수 있다** — 0 으로 채우지 않는다 (§1.2-10)
            "remaining_freshness_days": lot.remaining_freshness_days,
            # 등급도 **없을 수 있다** — 물류가 정규화 근거를 못 찾으면 None 이다.
            #   키를 빼지 않는다: *"등급 축이 없다"* 와 *"등급을 모른다"* 는 다르다.
            #   여기서 값을 만들지 않는다 — 물류가 준 값을 그대로 옮긴다.
            "grade": lot.grade,
            "status": lot.status,
        }
        for lot in build_lot_constraints(snapshot)
    ]

    # 물류가 *"돌긴 돌지만 이런 점을 봐 달라"* 고 남긴 것. 판정을 바꾸지 않지만
    # 검증 경로에 흘러야 Critic 과 사람이 본다.
    if rules["soft_warnings"]:
        payload["soft_warnings"] = list(rules["soft_warnings"])

    if policy is not None:
        payload["policy_version_used"] = policy.policy_version
        # 정책값이 DB 에서 온 것인지 — 값이 아니라 **출처**의 문제라 READY 는 유지한다
        missing.extend(
            f"{key}@policy_source_ref"
            for key in (
                "guaranteed_capacity_kg",
                "inbound_lead_days",
                "daily_inbound_capacity_kg",
                "inbound_transport_capacity_kg",
                "cap_by_date_policy",
            )
            if key not in policy.source_refs
        )
    else:
        missing.append("logistics_policy")

    ref = _ref(snapshot)
    evidences = [
        _ev("used_capacity_kg", snapshot.used_capacity_kg, "kg", ref, "현재 점유량"),
        _ev(
            "cap_by_date_policy",
            _CAP_WINDOW_DAYS,
            "days",
            _policy_ref(policy, "cap_by_date_policy", ref),
            "확정 입·출고만 반영한다(CONFIRMED_ONLY · N15) — 예정분은 Band 에 안 든다. "
            f"조회 창 D+{_CAP_WINDOW_DAYS}",
            grade="SIM_FIXED",
        ),
        _ev(
            _RENTAL_CAP_KEY,
            _RENTAL_CAP_KG,
            "kg",
            _policy_ref(policy, _RENTAL_CAP_KEY, _RENTAL_CAP_REF),
            "1차 MVP 는 외부 창고 임차 기능이 없다 — 임차 가능량 0 확정. "
            "미확정이 아니다 (2026-08-27 물류 회신 §1)",
            grade="SIM_FIXED",
        ),
        _ev(
            "cap_by_date_window_days",
            _CAP_WINDOW_DAYS,
            "days",
            ref,
            "조회 창의 길이. 제약값이 아니라 훑은 범위다 — "
            "이 창 밖의 날짜는 '0' 이 아니라 '안 봤다' 다",
            source="tool_calc",
            grade="SIM_FIXED",
        ),
    ]
    if free_kg is not None:
        evidences.append(
            _ev(
                "warehouse_free_kg",
                free_kg,
                "kg",
                ref,
                "보장 capacity − 현재 점유 (물류 calculate_cap_by_date 의 free_capacity 정의). "
                "기준은 독립 SLA 보장치이며 burst 가 아니다",
                source="tool_calc",
            )
        )
    for name, unit in (
        ("guaranteed_capacity_kg", "kg"),
        ("burst_capacity_kg", "kg"),
        ("inbound_lead_days", "days"),
        ("daily_inbound_capacity_kg", "kg"),
        ("inbound_transport_capacity_kg", "kg"),
        ("shared_daily_outbound_capacity_kg", "kg"),
    ):
        value = getattr(snapshot, name)
        if value is not None:
            evidences.append(
                _ev(
                    name,
                    value,
                    unit,
                    _policy_ref(policy, name, ref),
                    f"Logistics Policy {policy.policy_version if policy else '?'}",
                    grade="SIM_FIXED",
                )
            )
    if cap is not None:
        evidences.append(
            _ev(
                "cap_by_date",
                len(cap),
                "date_count",
                ref,
                f"D+{snapshot.inbound_lead_days} 부터 {_CAP_WINDOW_DAYS} 일 · "
                "min(창고여유, 일일입고, 운송) — 물류 Tool 산출",
                source="tool_calc",
            )
        )
    if payload.get("lots"):
        lots_ref = _lots_ref(snapshot)
        evidences.append(
            _ev(
                "lots",
                len(payload["lots"]),
                "lot_count",
                lots_ref,
                "현재 on_hand Lot — 등급·신선도 배분 대조용",
            )
        )
        # ★ **Lot 안의 숫자마다 근거를 단다** (봉투 v0.3 — 배열 항목 숫자에도 Evidence).
        #
        #   `claim` 에 이름 선택자를 쓴다 — `lots[LOT-...].available_qty_kg`.
        #   번호(`lots[0]`)로 쓰면 **Lot 순서가 바뀌는 날 근거가 다른 Lot 을 가리킨다.**
        #   `canonical_claim()` 이 항목의 문자열 필드로 찾아 주므로 lot_id 가 안전하다.
        #
        #   중복처럼 보이지만 아니다 — Lot 마다 **다른 DB 행**이다 (M-25 의 B 안이
        #   문제였던 "같은 근거를 두 벌"과 다르다).
        for lot in payload["lots"]:
            evidences.append(
                _ev(
                    f"lots[{lot['lot_id']}].available_qty_kg",
                    lot["available_qty_kg"],
                    "kg",
                    lots_ref,
                    f"{lot['item']} · 상태 {lot['status']}",
                )
            )
            if lot["remaining_freshness_days"] is not None:
                evidences.append(
                    _ev(
                        f"lots[{lot['lot_id']}].remaining_freshness_days",
                        lot["remaining_freshness_days"],
                        "days",
                        lots_ref,
                        "잔여 신선도 — 등급 배분·소진 순서 판단용",
                    )
                )

    # 🔴 물류가 NOT_READY 를 냈는데 **이름이 하나도 없으면 계약 위반**이다
    #    (M-1 §5.1 — 봉투가 ContractViolation 을 던진다).
    #
    #    `rules["runtime_status"]` 는 물류가 정하고 `missing` 은 어댑터가 따로 모은다.
    #    둘이 어긋날 수 있다 — 물류 Rule 이 막았는데 어댑터가 읽은 값은 다 멀쩡한 경우다.
    #    지금은 `rental_cap_kg@policy_source_ref` 가 늘 들어 있어 우연히 안 비어 있지만,
    #    **DB 에 그 키가 등록되면 비게 된다.** 그날 물류 어댑터가 예외로 죽는다.
    #
    #    통과 못 한 하드 체크의 **코드를 그대로 적는다** — 지어내지 않고 물류가 낸 이름이다.
    if rules["runtime_status"] != "READY" and not missing:
        missing.extend(
            f"logistics_rule/{check.code}"
            for check in rules["hard_constraints"]
            if check.status != "PASS"
        )
    if rules["runtime_status"] != "READY" and not missing:
        missing.append("logistics_runtime")  # 코드조차 없으면 최소한 사실은 남긴다

    if payload.get("soft_warnings"):
        evidences.append(
            _ev(
                "soft_warnings",
                len(payload["soft_warnings"]),
                "warning_count",
                ref,
                "물류 규칙이 남긴 관찰 — 판정을 바꾸지 않지만 사람과 Critic 이 본다",
                source="tool_calc",
            )
        )

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status=rules["runtime_status"],
        business_status="ok" if rules["runtime_status"] == "READY" else "skipped",
        payload=payload,
        evidences=tuple(evidences),
        judgment_fields=_JUDGMENT_FIELDS,
        missing_data=tuple(dict.fromkeys(missing)),
        reasoning="물류 경계를 산출했다.",
    )
    return reply, _meta(request, run_id, tools)


# ---------------------------------------------------------------------------
# SCENARIO_VALIDATION — 시나리오별 판정
# ---------------------------------------------------------------------------


def _scenario_validation(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """★ 재무와 달리 **스키마 추측이 필요 없다.**

    물류는 `PurchaseAgentOutput = PurchaseProposal` 로 **매입의 실물 스키마를 그대로
    임포트**한다(`app/logistics/schemas.py`). 마스터는 받은 제안을 그 모델로 되살려
    넘기기만 하면 된다 — 이름을 손으로 맞추는 자리가 없으므로 조용히 틀릴 자리도 없다.
    """
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_ARRIVAL, _T_CAP, _T_RULES]

    proposal = _as_proposal(request.payload)
    if proposal is None:
        return _not_ready(
            request,
            run_id,
            [],
            missing=("purchase_proposal",),
            reason="매입 제안을 물류 입력 모델로 되살리지 못했다",
        )

    snapshot = _load_snapshot(as_of)
    if snapshot is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("logistics_snapshot",),
            reason="물류 스냅샷을 읽지 못했다",
        )

    policy = _load_policy()
    scenario = run_logistics_procurement_scenario(proposal, snapshot)
    rules = evaluate_procurement_rules(as_of=as_of, snapshot=snapshot)
    verdict = derive_logistics_verdict(rules)

    cap = scenario["cap_by_date"] if rules["calculation_ready"] else {}
    payload: dict[str, Any] = {
        "verdict": _VERDICT_MAP.get(verdict or "", "skipped"),
        "expected_arrival_dates": [d.isoformat() for d in scenario["expected_arrival_dates"]],
        "cap_by_date": {d.isoformat(): _num(v) for d, v in sorted(cap.items())},
        "hard_constraints": [
            {"code": c.code, "status": c.status, "skip_reason": c.skip_reason}
            for c in rules["hard_constraints"]
        ],
        "soft_warnings": list(rules["soft_warnings"]),
    }

    ref = _ref(snapshot)
    evidences = (
        _ev(
            "verdict",
            len([c for c in rules["hard_constraints"] if c.status != "PASS"]),
            "failed_check_count",
            ref,
            f"물류 하드 체크 {len(rules['hard_constraints'])} 건 중 통과 못 한 수 → {verdict}",
            source="tool_calc",
        ),
        _ev(
            "cap_by_date",
            len(cap),
            "date_count",
            ref,
            "도착일별 신규 입고 상한 — min(창고여유, 일일입고, 운송)",
            source="tool_calc",
        ),
        _ev(
            "expected_arrival_dates",
            snapshot.inbound_lead_days if snapshot.inbound_lead_days is not None else 0,
            "days",
            _policy_ref(policy, "inbound_lead_days", ref),
            "매입 분할 회차일 + 입고 리드타임 — 물류 calculate_expected_arrival_dates 산출",
            source="tool_calc",
        ),
    )

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status=rules["runtime_status"],
        business_status=payload["verdict"],
        payload=payload,
        evidences=evidences,
        judgment_fields=("verdict",),
        missing_data=() if rules["calculation_ready"] else ("cap_by_date",),
        reasoning="매입 시나리오를 물류 관점에서 판정했다.",
    )
    return reply, _meta(request, run_id, tools)


def _not_implemented(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    run_id = _run_id(request)
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        # RUNTIME_NOT_READY 에는 이름이 반드시 있어야 한다 (M-1 §5.1) — 없으면 마스터가
        # 사용자에게 무엇을 달라고 할지 모른다. 여기서 없는 것은 값이 아니라 번역이다.
        missing_data=(f"{request.mode}_translation",),
        missing_capability=(f"{request.mode} 번역",),
        reasoning=f"{request.mode} 는 물류 어댑터에 아직 없다.",
    )
    return reply, _meta(request, run_id, [])


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _load_snapshot(as_of: date) -> InventoryLogisticsSnapshot | None:
    try:
        return get_current_inventory_logistics_snapshot(as_of=as_of)
    except Exception:  # noqa: BLE001 — 없는 것은 예외가 아니라 상태다
        return None


def _load_policy() -> LogisticsPolicy | None:
    try:
        return get_active_logistics_policy()
    except Exception:  # noqa: BLE001
        return None


def _free_capacity(snapshot: InventoryLogisticsSnapshot) -> Decimal | None:
    """보장 capacity − 현재 점유. **물류 `calculate_cap_by_date` 의 정의를 따른다.**"""
    if snapshot.guaranteed_capacity_kg is None:
        return None
    return max(Decimal(0), snapshot.guaranteed_capacity_kg - snapshot.used_capacity_kg)


def _cap_window(snapshot: InventoryLogisticsSnapshot, as_of: date) -> dict[date, Decimal] | None:
    """제안 전이라 도착일이 없다 — `as_of + lead` 부터 조회 창만큼 훑는다.

    ★ 물류 Tool 을 그대로 부른다. 창을 정하는 것 말고는 아무것도 하지 않는다.
    """
    lead = snapshot.inbound_lead_days
    if lead is None:
        return None
    start = as_of + timedelta(days=lead)
    dates = [start + timedelta(days=offset) for offset in range(_CAP_WINDOW_DAYS)]
    try:
        return calculate_cap_by_date(snapshot, dates)
    except ValueError:
        # IN_TRANSIT_SCHEDULE_UNRESOLVED · LOGISTICS_CAPACITY_INPUT_MISSING ·
        # NEGATIVE_PROJECTED_OCCUPANCY — 전부 "지금은 못 낸다" 다
        return None


def _as_proposal(payload: Mapping[str, Any]) -> PurchaseProposal | None:
    """봉투 payload → `PurchaseProposal`.

    ★ `allowed_axes` 처럼 **어댑터가 얹은 키**는 걸러 낸다 — 모델이 `extra="forbid"` 라
      그대로 넣으면 통째로 실패한다. 모르는 키를 버리는 것이지 값을 고치지 않는다.
    """
    known = {key: payload[key] for key in PurchaseProposal.model_fields if key in payload}
    try:
        return PurchaseProposal.model_validate(known)
    except Exception:  # noqa: BLE001 — 못 읽는 것은 예외가 아니라 상태다
        return None


def _ref(snapshot: InventoryLogisticsSnapshot) -> str:
    refs = snapshot.evidence_refs
    return refs[0] if refs else "logistics:snapshot"


def _lots_ref(snapshot: InventoryLogisticsSnapshot) -> str:
    """Lot 근거는 **Lot 을 실제로 담은 참조**를 가리킨다.

    스냅샷 첫 참조를 쓰면 Lot 근거가 runtime fixture 를 가리키게 되는데, 나중에
    *"이 수량이 어디서 왔나"* 를 따라가면 엉뚱한 곳에 닿는다.
    """
    for candidate in snapshot.evidence_refs:
        if "inventory_lots" in candidate:
            return candidate
    return _ref(snapshot)


def _policy_ref(policy: LogisticsPolicy | None, key: str, fallback: str) -> str:
    if policy is None:
        return fallback
    return policy.source_refs.get(key, fallback)


def _num(value: Decimal | float) -> float:
    return float(value)


def _ev(
    claim: str,
    value: Any,
    unit: str,
    ref: str,
    detail: str = "",
    grade: str = "OFFICIAL",
    source: str = "inventory",
) -> Evidence:
    return Evidence(
        claim=claim,
        source=source,  # type: ignore[arg-type]
        ref_ids=(ref,),
        value=float(value),
        unit=unit,
        evidence_grade=grade,  # type: ignore[arg-type]
        evidence_detail=detail,
    )


def _run_id(request: AgentRequest) -> str:
    return f"LOG-{request.context.request_id}-{request.call_seq}"


def _meta(request: AgentRequest, run_id: str, tools: Sequence[str]) -> ExecutionMetadata:
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent=_AGENT,
        used_tools=tuple(tools),
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status="DISABLED",
    )


def _not_ready(
    request: AgentRequest,
    run_id: str,
    tools: Sequence[str],
    *,
    missing: tuple[str, ...],
    reason: str,
) -> tuple[AgentReply, ExecutionMetadata]:
    """입력이 없어서 못 낸 답. **`ERROR` 가 아니다** — 다시 불러도 같다 (M-1 §5.1)."""
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status="RUNTIME_NOT_READY",
        business_status="skipped",
        missing_data=missing,
        reasoning=reason,
    )
    return reply, _meta(request, run_id, tools)
