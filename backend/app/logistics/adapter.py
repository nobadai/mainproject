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

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.logistics.repository import LogisticsRead, get_current_logistics_read
from app.logistics.rules import (
    derive_procurement_verdict,
    evaluate_procurement_business_signals,
    evaluate_procurement_rules,
    merge_business_warnings,
)
from app.logistics.scenario_engine import (
    derive_preferred_adjustment,
    run_logistics_procurement_scenario,
)
from app.logistics.schemas import InventoryLogisticsSnapshot, LogisticsPolicy
from app.logistics.tools import (
    CAP_BY_DATE_WINDOW_DAYS,
    build_cap_window,
    build_inventory_by_item,
    build_lot_constraints,
    calculate_cap_by_date,
)
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata, Verdict
from app.orchestrator.contracts_core import Evidence, SuggestedAdjustment
from app.purchase_agent.schemas import PurchaseProposal

logger = logging.getLogger(__name__)

_AGENT = "inventory"

# 물류 Tool — 실제로 부른 것만 남긴다.
_T_RULES = "evaluate_procurement_rules"
_T_CAP = "calculate_cap_by_date"
_T_ARRIVAL = "calculate_expected_arrival_dates"
_T_LOTS = "build_lot_constraints"
_T_INVENTORY = "build_inventory_by_item"
_T_SIGNALS = "evaluate_procurement_business_signals"

_JUDGMENT_FIELDS = ("cap_by_date_policy",)

_CAP_WINDOW_DAYS = CAP_BY_DATE_WINDOW_DAYS
"""PRE_PURCHASE 에서 `cap_by_date` 를 뽑는 **조회 창**의 길이 — 물류 Tool 소유값.

★ **제약값이 아니다.** 물류의 `calculate_cap_by_date()` 는 도착일 목록을 받는데,
  제안 전에는 도착일이 없다. 그래서 `as_of + lead` 부터 이 길이만큼 훑는다.

  짧으면 매입이 **덜 볼 뿐** 값이 달라지지 않는다. 18 인 것은 매입 커버일수 상한
  D+18(ML 지평)에서 왔다 — 그보다 뒤의 날짜는 매입이 쓰지 않는다.

  창의 길이 자체는 `cap_by_date_window_days` 로 payload 에 밝힌다. 받는 쪽이
  *"이 날짜까지밖에 안 왔다"* 를 알아야 없는 날을 0 으로 읽지 않는다.

★ 값은 `tools.CAP_BY_DATE_WINDOW_DAYS` 를 그대로 쓴다 (#121 ⑤) — 어댑터가 자기
  숫자를 들고 있으면 판정 창과 조회 창이 갈린다. 이름만 여기 남긴 것은 Evidence
  문구·payload 키가 이 이름을 참조하기 때문이다.
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
    if request.mode == "STATUS_QUERY":
        return _status_query(request)
    return _not_implemented(request)


# ---------------------------------------------------------------------------
# STATUS_QUERY — "지금 창고·재고 상황" 조회
# ---------------------------------------------------------------------------


def _status_query(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """묻기만 하는 요청. **경계가 아니라 상태**를 돌려준다.

    ★ `PRE_PURCHASE` 와 읽는 것은 같고 **싣는 것이 다르다.** `cap_by_date` ·
      `inbound_lead_days` · `daily_inbound_capacity_kg` 는 *매입이 분할 계획을 짤 때
      쓰는 경계*라, "지금 창고 어떠냐" 를 묻는 사람에게는 답이 아니다.
      D+18 Band 를 조회 답에 실으면 사람이 읽을 것이 아닌 표가 화면을 덮는다.

    ★ **하드 제약 위반이 조회를 막지 않는다.** `PRE_PURCHASE` 는 `LOG-H01` 이
      UNRESOLVED 면 경계를 못 내지만, 조회는 실행으로 이어지지 않는다. 규칙이 못 본
      것은 `missing_data` 로 이름만 밝히고 **읽어낸 상태는 답한다** (§3.7.6 —
      못 한 것을 한 척하지 않되, 할 수 있는 것을 안 한 척도 하지 않는다).

    ★ `lots` 를 통째로 싣지 않는다. 조회에 필요한 것은 *"몇 건이 얼마나 있고 임박한
      것이 있는가"* 이지 Lot 목록이 아니다 — 목록은 매입이 배분할 때 쓴다.
    """
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_LOTS]

    try:
        read = _load_read(as_of)
    except _SnapshotLoadError:
        return _snapshot_error(request, run_id, tools)
    snapshot = read.snapshot if read is not None else None
    if snapshot is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("logistics_snapshot", "logistics_runtime_fixture"),
            reason="물류 스냅샷을 읽지 못했다",
        )
    if snapshot.as_of != as_of:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=(f"logistics_snapshot@{as_of.isoformat()}",),
            reason=f"물류 스냅샷 기준일이 {snapshot.as_of} 다 — 요청은 {as_of} 다",
        )

    policy = read.policy
    ref = _ref(snapshot)
    lots_ref = _lots_ref(snapshot)
    lots = build_lot_constraints(snapshot)

    missing: list[str] = []
    payload: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "used_capacity_kg": _num(snapshot.used_capacity_kg),
        "lot_count": len(lots),
    }

    free_kg = _free_capacity(snapshot)
    if free_kg is None:
        # 0 과 "모름" 은 다르다 (§1.2-10) — 여유를 못 셈했으면 이름을 밝힌다
        missing.append("guaranteed_capacity_kg")
    else:
        payload["warehouse_free_kg"] = _num(free_kg)

    # 여유만 싣고 총량을 빼면 "7,499kg 남았다" 가 큰 건지 작은 건지 알 수 없다.
    # 실으면 **근거도 같이 달아야 한다** — 봉투가 E-EVIDENCE-MISSING 으로 잡는다.
    guaranteed = snapshot.guaranteed_capacity_kg
    if guaranteed is not None:
        payload["guaranteed_capacity_kg"] = _num(guaranteed)

    # ── 임박 신선도 ──────────────────────────────────────────────
    #
    # ★ 임계를 **지어내지 않는다.** "며칠 이하가 임박인가" 는 물류 정책이지 조회의
    #   판단이 아니라서, 최솟값과 그 Lot 만 밝히고 위험 여부는 사람이 본다.
    #   여기서 3일·5일 같은 수를 고르면 §1.2-8(하드 제약값 파생 금지)이 된다.
    fresh = [
        (lot.remaining_freshness_days, lot.lot_id)
        for lot in lots
        if lot.remaining_freshness_days is not None
    ]
    if fresh:
        days, lot_id = min(fresh)
        payload["min_remaining_freshness_days"] = days
        payload["min_freshness_lot_id"] = lot_id
    elif lots:
        # Lot 은 있는데 신선도가 하나도 안 실렸다 — 빈 값으로 덮지 않는다
        missing.append("lots[].remaining_freshness_days")

    evidences = [
        _ev("used_capacity_kg", snapshot.used_capacity_kg, "kg", ref, "현재 점유량"),
        _ev("lot_count", len(lots), "count", lots_ref, "ACTIVE Lot 건수"),
    ]
    if guaranteed is not None:
        evidences.append(
            _ev(
                "guaranteed_capacity_kg",
                guaranteed,
                "kg",
                _policy_ref(policy, "guaranteed_capacity_kg", ref),
                "3PL 보장 Capacity (독립 SLA) — burst 9,600 은 순간 초과라 기준이 아니다",
                grade="SIM_FIXED",
            )
        )
    if free_kg is not None:
        evidences.append(
            _ev(
                "warehouse_free_kg",
                free_kg,
                "kg",
                ref,
                "guaranteed_capacity_kg − 현재 물리 점유량(as_of 시점)",
            )
        )
    if "min_remaining_freshness_days" in payload:
        evidences.append(
            _ev(
                "min_remaining_freshness_days",
                payload["min_remaining_freshness_days"],
                "days",
                lots_ref,
                f"Lot {payload['min_freshness_lot_id']} — 가장 짧은 잔여 신선도",
            )
        )

    # ★ policy 는 항상 있다 — Snapshot 조립이 Policy 로 만들어지므로 read 가 성공한
    #   순간 둘 다 존재한다 (#121 ⑤).
    #
    #   🔴 **이 변경이 그렇게 만든 것이지, 종전에도 그랬던 것이 아니다.** 종전에는
    #   어댑터가 policy 를 **두 번째로 독립 조회**하고 모든 예외를 삼켰기 때문에,
    #   첫 조회 성공 후 둘째만 실패하면 *"snapshot 은 있는데 policy 는 None"* 이
    #   실제로 성립했다. 그때 나가던 것은 `cap_by_date_policy="UNKNOWN"` 이고,
    #   그것은 **판정 필드(`judgment_fields`)에 실린 지어낸 값**이며 근거까지 붙어
    #   봉투 검증을 통과했다 (§1.2-10 위반). 분기를 없앤 진짜 이유가 이것이다 —
    #   "죽은 코드라서" 가 아니라 **조작값이 나가던 경로라서**다.
    payload["policy_version_used"] = policy.policy_version
    if "guaranteed_capacity_kg" not in policy.source_refs:
        missing.append("guaranteed_capacity_kg@policy_source_ref")

    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status="READY",
        business_status="ok",
        payload=payload,
        evidences=tuple(evidences),
        # 조회는 판정을 내지 않는다 — cap_by_date_policy 는 경계 해석이라 여기 없다
        judgment_fields=(),
        missing_data=tuple(missing),
        reasoning="현재 창고·재고 상태를 조회했다."
        if not missing
        else "읽어낸 상태는 답했고, 채우지 못한 값은 이름을 밝혔다.",
    )
    return reply, _meta(request, run_id, tools)


# ---------------------------------------------------------------------------
# PRE_PURCHASE — 경계 제공
# ---------------------------------------------------------------------------


def _pre_purchase(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_RULES]

    try:
        read = _load_read(as_of)
    except _SnapshotLoadError:
        return _snapshot_error(request, run_id, tools)
    snapshot = read.snapshot if read is not None else None
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

    policy = read.policy
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
    if _RENTAL_CAP_KEY not in policy.source_refs:
        missing.append(f"{_RENTAL_CAP_KEY}@policy_source_ref")

    payload.update(
        {
            "used_capacity_kg": _num(snapshot.used_capacity_kg),
            "cap_by_date_policy": policy.cap_by_date_policy,
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
            # 물류가 `LotConstraint.grade` 를 나르게 됐다 (#77). 매입 등급 배분이 이
            # 값을 본다 — 없으면 필터가 **에러 없이 전부 미스**로 지나간다.
            #
            # ★ `None` 을 임의 등급으로 채우지 않는다. 현재 `_RAW_GRADE_NORMALIZATION`
            #   이 비어 있어 raw `'상품'` 은 정규화되지 않고 그대로 `None` 이 온다.
            #   그 사실이 payload 에 드러나는 것이 맞다 — 키를 빼면 *"물류가 안 준 것"*
            #   과 *"근거가 없어 못 정한 것"* 이 구분되지 않는다 (§1.2-10).
            "grade": lot.grade,
            "status": lot.status,
        }
        for lot in build_lot_constraints(snapshot)
    ]

    # ── 품목별 가용재고 집계 ─────────────────────────────────────
    #
    # ★ Lot 목록과 **별개로** 싣는다 (#111 A1). 매입/마스터가 Lot 을 재합산하면 가용재고
    #   정의(비-ACTIVE 제외 · 신선도 만료 제외 · 확정 출고 예약분 차감)를 남의 도메인에서
    #   재구현하게 된다 — 집계는 물류 Tool 이 소유한다.
    #
    # ★ `None` 은 Partial Output 이다 — 확정 출고에 item 없는 행이 있으면 임의 배분하지
    #   않고 키를 생략한다. `[]`(품목 0 건 확인)로 위장하지 않는다 (§1.2-10).
    tools.append(_T_INVENTORY)
    inventory_by_item = build_inventory_by_item(snapshot)
    if inventory_by_item is None:
        missing.append("inventory_by_item")
    else:
        payload["inventory_by_item"] = [
            {"item": entry.item, "available_qty_kg": _num(entry.available_qty_kg)}
            for entry in inventory_by_item
        ]

    # ── 품목 보관 정책 ───────────────────────────────────────────
    #
    # ★ Lot 의 `remaining_freshness_days` 와 **다른 값이다.** 잔여 신선도는 *이미 창고에
    #   있는 그 Lot* 이 앞으로 며칠 쓸 수 있나이고, `operational_limit_days` 는 그 품목을
    #   **새로 들일 때** 적용할 품목 단위 보관 한계다. 새로 매입할 물량의 기준은 후자라
    #   기존 Lot 의 잔여일수에서 역산하면 안 된다 (`ItemStoragePolicyFact` 참조).
    #
    # ★ 그래서 Lot 목록에서 뽑지 않고 Repository 가 정책 테이블에서 직접 읽은 것을
    #   그대로 나른다 — 재고가 0kg 인 품목의 보관 한계도 매입은 알아야 한다.
    #
    # ★ `None`(미조회)과 `[]`(정책 0 건 확인)은 다르다 (§1.2-10). 미조회면 키를 만들지
    #   않고 이름만 남긴다 — 빈 배열로 덮으면 *"정책이 없다"* 로 읽힌다.
    policies = snapshot.item_storage_policies
    if policies is None:
        missing.append("item_storage_policies")
    else:
        payload["item_storage_policies"] = [
            {
                "item": policy_fact.item,
                # 값이 없으면 없는 대로 둔다 — 0 이나 기본 계수를 어댑터가 지어내지 않는다.
                "operational_limit_days": policy_fact.operational_limit_days,
                "medium_grade_factor": (
                    None
                    if policy_fact.medium_grade_factor is None
                    else _num(policy_fact.medium_grade_factor)
                ),
            }
            for policy_fact in policies
        ]

    # 물류가 *"돌긴 돌지만 이런 점을 봐 달라"* 고 남긴 것. 판정을 바꾸지 않지만
    # 검증 경로에 흘러야 Critic 과 사람이 본다.
    if rules["soft_warnings"]:
        payload["soft_warnings"] = list(rules["soft_warnings"])

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
                "guaranteed_capacity_kg − 현재 점유(as_of 시점). 기준은 독립 SLA "
                "보장치이며 burst 가 아니다. cap_by_date 는 같은 뺄셈을 도착일별 "
                "예상 점유로 다시 하므로 이 값과 일치하지 않는다",
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
                    f"Logistics Policy {policy.policy_version}",
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
                "guaranteed_capacity_kg − 도착일별 예상 점유 — 물류 Tool 산출",
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

    # ★ 보관 정책의 숫자에도 근거를 단다 — 봉투는 **배열 항목 안의 숫자마다** Evidence 를
    #   요구한다(`required_claims`). Lot 과 같은 방식으로 **이름 선택자**를 쓴다:
    #   `item_storage_policies[배추].operational_limit_days`. 번호로 쓰면 품목 순서가
    #   바뀌는 날 근거가 다른 품목을 가리킨다.
    #
    # ★ ref 를 지어내지 않는다 — Repository 가 스냅샷 `evidence_refs` 에 실어 둔
    #   `DB:item_storage_policies` 를 그대로 가리킨다.
    #
    # ★ 값이 `None` 인 필드는 근거를 만들지 않는다. 봉투도 숫자가 아닌 값에는 근거를
    #   요구하지 않는다 — 없는 값에 근거를 붙이면 *"확인했다"* 는 거짓이 된다.
    if payload.get("item_storage_policies"):
        policies_ref = _policies_ref(snapshot)
        for row in payload["item_storage_policies"]:
            if row["operational_limit_days"] is not None:
                evidences.append(
                    _ev(
                        f"item_storage_policies[{row['item']}].operational_limit_days",
                        row["operational_limit_days"],
                        "days",
                        policies_ref,
                        f"{row['item']} 품목의 신규 입고분 운영 보관한계 — "
                        "기존 Lot 의 잔여 신선도와 다른 값이다",
                    )
                )
            if row["medium_grade_factor"] is not None:
                evidences.append(
                    _ev(
                        f"item_storage_policies[{row['item']}].medium_grade_factor",
                        row["medium_grade_factor"],
                        "ratio",
                        policies_ref,
                        f"{row['item']} 중등급 보관한계 계수 — DB Fact 를 그대로 나른다",
                    )
                )

    if payload.get("inventory_by_item"):
        evidences.extend(_inventory_by_item_evidences(payload["inventory_by_item"], snapshot))

    # 🔴 물류가 NOT_READY 를 냈는데 **이름이 하나도 없으면 계약 위반**이다
    #    (M-1 §5.1 — 봉투가 ContractViolation 을 던진다).
    #
    #    `rules["runtime_status"]` 는 물류가 정하고 `missing` 은 어댑터가 따로 모은다.
    #    둘이 어긋날 수 있다 — 물류 Rule 이 막았는데 어댑터가 읽은 값은 다 멀쩡한 경우다.
    #    지금은 `rental_cap_kg@policy_source_ref` 가 늘 들어 있어 우연히 안 비어 있지만,
    #    **DB 에 그 키가 등록되면 비게 된다.** 그날 물류 어댑터가 예외로 죽는다.
    #
    #    통과 못 한 하드 체크의 **코드를 그대로 적는다** — 지어내지 않고 물류가 낸 이름이다.
    # ★ 첫 fallback(비-PASS 코드 재수집)은 없앴다 — `missing` 초기화가 이미 같은
    #   comprehension 으로 무조건 채우므로 여기서 다시 돌려도 한 건도 늘지 않는다.
    #   `logistics_runtime` 만이 유효한 최후 방어다: Rule 이 막았는데 비-PASS 코드가
    #   하나도 없는(=이름을 못 내는) 경우에 사실만이라도 남긴다.
    if rules["runtime_status"] != "READY" and not missing:
        missing.append("logistics_runtime")

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

    # 🔴 기준일이 다른 제안은 판정하지 않는다 — 재무 어댑터와 같은 fail-closed (§1.2-6).
    #    스냅샷·Rule 은 요청 `as_of` 로 읽는데 시나리오만 다른 날짜로 계산하면
    #    기준일이 섞인 판정이 READY 로 나간다 (Codex 교차검증 재현 · #111).
    if proposal.meta.as_of != as_of:
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=as_of,
            agent=_AGENT,
            mode=request.mode,
            run_id=run_id,
            runtime_status="ERROR",
            business_status="skipped",
            payload={"validation_errors": ["proposal.meta.as_of"]},
            reasoning="Purchase proposal as-of does not match the Master request.",
        )
        return reply, _meta(request, run_id, [])

    try:
        read = _load_read(as_of)
    except _SnapshotLoadError:
        return _snapshot_error(request, run_id, tools)
    snapshot = read.snapshot if read is not None else None
    if snapshot is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("logistics_snapshot",),
            reason="물류 스냅샷을 읽지 못했다",
        )

    policy = read.policy
    scenario = run_logistics_procurement_scenario(proposal, snapshot)
    rules = evaluate_procurement_rules(as_of=as_of, snapshot=snapshot)
    # 시나리오 집계 ⊕ 하드 제약의 최악값 결합 (2026-09-01 마스터 확정 · #121 3단계).
    # 전 시나리오 reject 인데 하드가 전부 PASS 라고 ok 가 나가던 집계 단절의 수정이다.
    verdict = derive_procurement_verdict(rules, scenario["scenario_results"])

    # 업무 위험 판정(비교식)은 Rule 소유 — 독립 경로(service)와 같은 함수·같은 병합을
    # 쓴다 (#111 A3). 여기서 계산하는 것이 아니라 Rule 이 낸 signal 을 나를 뿐이다.
    tools.append(_T_SIGNALS)
    business = evaluate_procurement_business_signals(
        as_of=as_of,
        snapshot=snapshot,
        scenario_results=scenario["scenario_results"],
    )

    cap = scenario["cap_by_date"] if rules["calculation_ready"] else {}
    payload: dict[str, Any] = {
        "verdict": _VERDICT_MAP.get(verdict or "", "skipped"),
        "expected_arrival_dates": [d.isoformat() for d in scenario["expected_arrival_dates"]],
        "cap_by_date": {d.isoformat(): _num(v) for d, v in sorted(cap.items())},
        "hard_constraints": [
            {"code": c.code, "status": c.status, "skip_reason": c.skip_reason}
            for c in rules["hard_constraints"]
        ],
        # Rule 경고 + 업무 위험 signal + 판정 스킵 사실 — 독립 응답과 같은 채널 구성이다.
        # CAPACITY_TIGHT 같은 signal 은 판정을 바꾸지 않지만 Critic 과 사람이 봐야 한다.
        "soft_warnings": merge_business_warnings(rules, business),
        # 시나리오별 판정 상세 (#111 A2) — 총평만으로는 "어떤 시나리오가 왜 conditional
        # 인지"를 마스터가 받지 못한다. 항목 안의 라벨은 봉투 규칙상 근거 면제이고,
        # 숫자(suggested_qty_kg)는 두 겹 안이라 근거 대상이 아니다 (`required_claims`
        # — 배열은 한 겹만 파고든다).
        "scenario_results": [
            {
                "label": result.label,
                "verdict": result.verdict,
                "reason_codes": list(result.reason_codes),
                "adjustments": [
                    {
                        "axis": adjustment.axis,
                        "split_date": adjustment.split_date.isoformat(),
                        # 없는 제안값은 싣지 않는다 — null 로 채우면 "0 제안"과
                        # "제안 없음"이 구분되지 않는다 (§1.2-10)
                        **(
                            {"suggested_qty_kg": _num(adjustment.suggested_qty_kg)}
                            if adjustment.suggested_qty_kg is not None
                            else {}
                        ),
                        **(
                            {
                                "suggested_arrival_date": (
                                    adjustment.suggested_arrival_date.isoformat()
                                )
                            }
                            if adjustment.suggested_arrival_date is not None
                            else {}
                        ),
                    }
                    for adjustment in result.adjustments
                ],
            }
            for result in scenario["scenario_results"]
        ],
    }

    # Rule 이 낸 조정 제안의 우선 축 (#111 A4) — LLM 이 아니라 Scenario/Rule 의 결정이다.
    # 축이 혼재하거나 0건이면 None 이고, 그때는 키를 싣지 않는다 — 근거 없이 하나를
    # 고르지 않는다 (`derive_preferred_adjustment` docstring).
    preferred = derive_preferred_adjustment(scenario["scenario_results"])
    if preferred is not None:
        payload["preferred_adjustment"] = preferred

    # 품목별 가용재고 — Scenario 엔진이 이미 계산해 돌려준다. 안 실으면 계산한 값을
    # 버리는 것이고(#111 검증 발견 5), 마스터는 판정 회신에서 재고 맥락을 잃는다.
    # `None`(출고 귀속 불명) 위장 금지는 PRE 와 같다 (§1.2-10).
    missing: list[str] = [] if rules["calculation_ready"] else ["cap_by_date"]
    if scenario["inventory_by_item"] is None:
        missing.append("inventory_by_item")
    else:
        payload["inventory_by_item"] = [
            {"item": entry.item, "available_qty_kg": _num(entry.available_qty_kg)}
            for entry in scenario["inventory_by_item"]
        ]
    # ★ 업무 경고(`business["warnings"]`)는 여기 넣지 않는다. 독립 응답의
    #   `missing_data` 는 무숫자 번역 채널이라 그쪽에는 들어가지만, M-1 의
    #   `missing_data` 는 **마스터가 사용자에게 무엇을 달라고 할지**의 이름이고 형식도
    #   `logistics_rule/LOG-H02` · `rental_cap_kg@policy_source_ref` 처럼 네임스페이스가
    #   붙은 필드명이다. 맨 경고 코드를 섞으면 어휘가 갈라지고, NOT_READY 로 떨어지는
    #   날 *"CAPACITY_TIGHT_POLICY_UNRESOLVED 가 없어 답하지 못했습니다"* 라는
    #   이중부정 문장이 나간다 (`master/answer.py` 의 gaps 문구).
    #
    #   사실이 사라지는 것은 아니다 — `soft_warnings` 가 같은 코드를 그대로 나른다.

    ref = _ref(snapshot)
    # verdict 근거의 구성 요소 — 결합 판정에 실제로 들어간 비통과 입력의 수다.
    _failed_hard = len([c for c in rules["hard_constraints"] if c.status != "PASS"])
    _rejected = len([s for s in scenario["scenario_results"] if s.verdict == "reject"])
    _conditional = len([s for s in scenario["scenario_results"] if s.verdict == "conditional"])
    evidences = (
        _ev(
            "verdict",
            _failed_hard + _rejected + _conditional,
            "non_ok_input_count",
            ref,
            "시나리오 집계 ⊕ 하드 제약 최악값 결합 (2026-09-01 확정) — "
            f"비통과 하드 체크 {_failed_hard}건 · reject {_rejected}안 · "
            f"conditional {_conditional}안 → {verdict}",
            source="tool_calc",
        ),
        _ev(
            "cap_by_date",
            len(cap),
            "date_count",
            ref,
            "guaranteed_capacity_kg 에서 도착일별 예상 점유를 뺀 신규 입고 상한 — "
            "1차 MVP 의 Hard Capacity 는 이 하나다",
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
    # `soft_warnings` 는 봉투 메타(ENVELOPE_META_KEYS)라 근거 의무는 없지만, PRE 와
    # 같은 이유로 개수를 남긴다 — 판정을 바꾸지 않는 관찰이 몇 건 흘렀는지가 실행
    # 이력에 보여야 Critic 이 잡는다.
    if payload["soft_warnings"]:
        evidences = (
            *evidences,
            _ev(
                "soft_warnings",
                len(payload["soft_warnings"]),
                "warning_count",
                ref,
                "물류 규칙 경고 + 업무 위험 signal + 판정 스킵 사실 — 한 채널로 합류",
                source="tool_calc",
            ),
        )
    if payload.get("inventory_by_item"):
        evidences = (
            *evidences,
            *_inventory_by_item_evidences(payload["inventory_by_item"], snapshot),
        )

    judgment_fields: tuple[str, ...] = ("verdict",)
    if preferred is not None:
        # `quantity`/`timing` 은 소문자라 봉투의 대문자 라벨 휴리스틱을 지나친다.
        # 매입 행동을 바꾸는 판정이므로 직접 선언하고 근거를 단다 — envelope 의
        # judgment_fields docstring 이 말하는 바로 그 케이스다 (#111 검증 발견 2).
        judgment_fields = ("verdict", "preferred_adjustment")
        evidences = (
            *evidences,
            _ev(
                "preferred_adjustment",
                len(
                    [
                        adjustment
                        for result in scenario["scenario_results"]
                        # 집계와 같은 모집단 — reject 안의 조정은 근거 건수에서도 뺀다
                        if result.verdict != "reject"
                        for adjustment in result.adjustments
                        if adjustment.axis == preferred
                    ]
                ),
                "adjustment_count",
                ref,
                f"비-reject 시나리오 조정 제안의 고유 축이 {preferred} 하나 — 해당 축 제안 건수",
                source="tool_calc",
            ),
        )

    # M-1 전용 채널 배선 (#111 검증 발견 1) — payload 안에만 두면 마스터 flow 가 세는
    # `reply.suggested_adjustments` 는 0건이고, 사람 화면과 Critic 의 축 침범 검사가
    # 전부 빈 튜플을 본다. 축 어휘는 `_DEPT_AXES["inventory"] = ("quantity","timing")`
    # 과 정확히 같아 추측 없이 옮긴다.
    suggested: list[SuggestedAdjustment] = []
    seen_adjustments: set[tuple[str, date, float]] = set()
    for result in scenario["scenario_results"]:
        # ★ reject 안의 adjustment 는 승격하지 않는다 (#121 2단계). multi-split 에서
        #   앞 회차의 조정이 남은 채 전체가 reject 될 수 있는데, 구제 불가 판정한 안의
        #   조정을 행동 제안으로 내보내면 "reject 는 조정으로 구제 불가"와 모순된다.
        #   진단 기록은 payload.scenario_results 에 그대로 남는다 — 사실이 사라지는
        #   것이 아니라 제안으로 격상되지 않을 뿐이다. needs_followup 도 이에 따라
        #   reject 만으로는 서지 않는다.
        if result.verdict == "reject":
            continue
        for adjustment in result.adjustments:
            if adjustment.axis == "quantity" and adjustment.suggested_qty_kg is not None:
                target, unit = _num(adjustment.suggested_qty_kg), "kg"
                what = f"수량을 {target:g}kg 로 조정 제안"
            elif adjustment.axis == "timing" and adjustment.suggested_arrival_date is not None:
                # 날짜는 float 로 실을 수 없어 as_of 기준 D+N 으로 옮긴다 — 실제 날짜는
                # reason 에 그대로 남으므로 손실 없는 표기 변환이지 새 판단이 아니다.
                target = float((adjustment.suggested_arrival_date - as_of).days)
                unit = "d"
                what = f"도착일을 {adjustment.suggested_arrival_date.isoformat()} 로 조정 제안"
            else:
                # 값 없는 제안은 전용 채널로 못 옮긴다 — payload.scenario_results 에는
                # 그대로 남아 있어 사실이 사라지지는 않는다.
                continue
            key = (adjustment.axis, adjustment.split_date, target)
            if key in seen_adjustments:
                continue
            seen_adjustments.add(key)
            suggested.append(
                SuggestedAdjustment(
                    dept="inventory",
                    axis=adjustment.axis,
                    target_value=target,
                    unit=unit,
                    reason=(
                        f"{result.label} 시나리오 {adjustment.split_date.isoformat()} 회차 — {what}"
                    ),
                    ref_ids=(ref,),
                )
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
        suggested_adjustments=tuple(suggested),
        # 조정 제안이 있다는 것은 "이 안 그대로는 안 되고 재검토가 필요하다"다 —
        # 라우팅은 마스터 몫이고 여기서는 사실만 표시한다 (AgentReply docstring).
        needs_followup=bool(suggested),
        judgment_fields=judgment_fields,
        missing_data=tuple(dict.fromkeys(missing)),
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


class _SnapshotLoadError(Exception):
    """스냅샷 조회가 **실행 오류**로 실패했다 — 데이터 부재(LookupError)와 다르다."""


def _load_read(as_of: date) -> LogisticsRead | None:
    """부재만 None 으로, 실행 오류는 구분해 올린다 (#121 4단계).

    Repository 예외 계약 전수 확인(2026-09-01) — 정상적인 "데이터 없음/미확정"은
    **LookupError 둘**이다(runtime fixture 0건 · 필수 정책 미등재). 독립 Service
    경로(`service._get_snapshot_or_none`)가 같은 기준선이다.

    그 외 — ValueError/TypeError(데이터는 있는데 모양이 깨졌거나 활성 fixture 가
    중복인 무결성 위반), RuntimeError(env 부재), psycopg 오류(DB 장애) — 는 회사
    상태가 아니라 **실행 실패**다. 종전처럼 RUNTIME_NOT_READY 로 뭉개면 마스터가
    재시도하지 않고 "데이터를 달라"로 오독한다 (M-1 §5.1 — ERROR 가 재시도 가치가
    있는 쪽).

    ★ **예외가 하나 알려져 있다** — `item_storage_policies.operational_limit_days`
      는 DB 가 nullable 인데 `_inventory_lot_from_row` 가 NULL 을 TypeError 로 낸다.
      그 NULL 은 "보관한계 미등록"(부재)이라 여기서는 ERROR 로 분류되지만 재시도해도
      풀리지 않는다. Repository 안에서 같은 컬럼을 두 경로가 다르게 다루는 것이
      원인이라 어댑터 특례가 아니라 Repository 어휘를 고쳐야 하고, 그러면 독립
      Service 도 함께 고쳐진다 — **별도 안건**이다 (2026-09-01 교차검증 I-1).
      NULL 인 Lot 의 신선도를 어떻게 볼지가 함께 정해져야 해 기계적 수정이 아니다.
    """
    try:
        return get_current_logistics_read(as_of=as_of)
    except LookupError:
        return None  # 없는 것은 예외가 아니라 상태다
    except Exception as error:
        # 원문 메시지는 로그로만 남긴다 — reasoning 에 그대로 실으면 숫자가 섞여
        # E-REASONING-NUMERIC 에 걸린다 (2026-09-01 재무 400 회신에서 실측된 함정).
        logger.exception("Logistics read failed (as_of=%s)", as_of)
        raise _SnapshotLoadError from error


def _free_capacity(snapshot: InventoryLogisticsSnapshot) -> Decimal | None:
    """보장 capacity − 현재 점유. **물류 `calculate_cap_by_date` 의 정의를 따른다.**"""
    if snapshot.guaranteed_capacity_kg is None:
        return None
    return max(Decimal(0), snapshot.guaranteed_capacity_kg - snapshot.used_capacity_kg)


def _cap_window(snapshot: InventoryLogisticsSnapshot, as_of: date) -> dict[date, Decimal] | None:
    """제안 전이라 도착일이 없다 — 물류 Tool 의 조회 창을 그대로 훑는다.

    ★ 창의 정의(시작일·길이)는 `tools.build_cap_window` 소유다 (#121 ⑤). 어댑터가
      같은 날짜 나열을 따로 만들면 판정 창과 조회 창이 갈릴 자리가 생긴다.
    """
    dates = build_cap_window(snapshot, as_of)
    if dates is None:
        return None
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


def _policies_ref(snapshot: InventoryLogisticsSnapshot) -> str:
    """품목 보관 정책 근거는 **정책 테이블을 담은 참조**를 가리킨다 (`_lots_ref` 와 같은 이유)."""
    for candidate in snapshot.evidence_refs:
        if "item_storage_policies" in candidate:
            return candidate
    return _ref(snapshot)


def _policy_ref(policy: LogisticsPolicy, key: str, fallback: str) -> str:
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
    extra_ref_ids: tuple[str, ...] = (),
) -> Evidence:
    return Evidence(
        claim=claim,
        source=source,  # type: ignore[arg-type]
        ref_ids=(ref, *extra_ref_ids),
        value=float(value),
        unit=unit,
        evidence_grade=grade,  # type: ignore[arg-type]
        evidence_detail=detail,
    )


def _inventory_by_item_evidences(
    rows: list[dict[str, Any]],
    snapshot: InventoryLogisticsSnapshot,
) -> list[Evidence]:
    """품목별 가용재고 근거 — 배열 항목 안의 숫자마다, 이름 선택자로 (#111 A1).

    ★ ref 는 **집계 원본**을 가리킨다. `_ref()`(스냅샷 첫 참조)를 쓰면 runtime fixture
      에 닿는데, 이 kg 은 Lot 행 합산 − 확정 출고 차감이다 — `_lots_ref` docstring 이
      금지한 바로 그 경우다. 확정 출고 출처(스냅샷 첫 참조)는 보조 ref 로 함께 싣는다.
    """
    lots_ref = _lots_ref(snapshot)
    outbound_ref = _ref(snapshot)
    return [
        _ev(
            f"inventory_by_item[{row['item']}].available_qty_kg",
            row["available_qty_kg"],
            "kg",
            lots_ref,
            f"{row['item']} 가용재고 합계 — 비-ACTIVE·신선도 만료 Lot 제외, 확정 출고 예약분 차감",
            source="tool_calc",
            extra_ref_ids=(outbound_ref,) if outbound_ref != lots_ref else (),
        )
        for row in rows
    ]


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


def _snapshot_error(
    request: AgentRequest,
    run_id: str,
    tools: Sequence[str],
) -> tuple[AgentReply, ExecutionMetadata]:
    """스냅샷 조회의 **실행 실패** — `RUNTIME_NOT_READY` 가 아니다 (#121 4단계).

    다시 부르면 성공할 수 있는 쪽이라 재시도 가치가 있다 (M-1 §5.1 — DB 장애·크래시).
    ★ 예외 원문을 reasoning 에 싣지 않는다 — 숫자가 섞이면 E-REASONING-NUMERIC 에
      걸린다(재무 400 실측 함정). 원문은 `_load_snapshot` 이 로그로 남긴다.
    """
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status="ERROR",
        business_status="skipped",
        payload={"failed_operation": "load_logistics_snapshot"},
        reasoning=(
            "물류 스냅샷 조회가 실행 오류로 실패했다 — "
            "데이터 부재가 아니라 재시도 가치가 있는 실패다."
        ),
    )
    return reply, _meta(request, run_id, tools)


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
