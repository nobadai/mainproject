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

★ **LLM 은 판정 뒤에, `SCENARIO_VALIDATION` 에서만, 명시적 opt-in 으로 돈다** (#385).
  `run_logistics_procurement_with_snapshot()` 은 마지막에 `enrich_logistics_response()`
  로 해석 서비스를 부른다. 마스터 경로는 그 함수를 쓰지 않는다 — Service 응답 타입에
  묶여 있어 `AgentReply` 를 되살려야 하기 때문이다. 대신 같은 조립기
  (`interpretation.build_sanitized_context`)에 **이미 계산한** signals · measurements ·
  preferred · missing 원재료를 넘기고, 해석은 `payload["interpretation"]` 에, 상태는
  `ExecutionMetadata.llm_*` 에 싣는다. 판정 · 근거 · 조정안은 LLM 전후로 같다.

  ```text
  PRE_PURCHASE · PRE_SALES · STATUS_QUERY   LLM 경로 없음 — `llm_status="DISABLED"` 가 사실
  SCENARIO_VALIDATION                       opt-in 없음 DISABLED · 게이트 미통과 SKIPPED_TEMPLATE
                                            · 성공 SUCCESS · 실패 FALLBACK (결정론 결과 유지)
  ```

  🔴 **설정 부재만으로 외부 Provider 가 불리지 않는다** — `LOGISTICS_MASTER_LLM_ENABLED`
     (`interpretation.master_interpretation_service`). 테스트는 그 팩토리를 갈아 끼운다.

★ **`as_of` 는 마스터가 준 것을 쓴다** (§1.2-6).

물류 확정분 근거 — `agent_policy_config` domain=logistics · `MVP-DECISION-20260825`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.contracts.core import Evidence, SuggestedAdjustment
from app.logistics.interpretation import (
    build_sanitized_context,
    master_interpretation_service,
    uncalled_interpretation,
)
from app.logistics.llm.schemas import InterpretationResult
from app.logistics.repository import LogisticsRead, get_current_logistics_read
from app.logistics.rules import (
    derive_procurement_verdict,
    evaluate_procurement_business_signals,
    evaluate_procurement_rules,
    evaluate_sales_business_signals,
    measure_freshness_facts,
    merge_business_warnings,
)
from app.logistics.scenario_engine import (
    derive_preferred_adjustment,
    run_logistics_procurement_scenario,
)
from app.logistics.schemas import (
    InventoryByItem,
    InventoryCostBasisSnapshot,
    InventoryLogisticsSnapshot,
    LogisticsPolicy,
)
from app.logistics.tools import (
    CAP_BY_DATE_WINDOW_DAYS,
    SupplyByDate,
    build_cap_window,
    build_inventory_by_item,
    build_lot_constraints,
    calculate_cap_by_date,
    calculate_window_capacity_usage,
    evaluate_delivery_feasibility,
    fefo_inventory_cost_basis,
    supply_capacity_by_date,
)
from app.master.critic_bridge import DEPT_CAP_CHECK_ID
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata, Verdict
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
_T_SALES_SIGNALS = "evaluate_sales_business_signals"
# STATUS_QUERY 전용 (#396) — 판정이 아니라 **측정**을 부르는 둘이다.
_T_USAGE = "calculate_window_capacity_usage"
_T_FRESHNESS = "measure_freshness_facts"


# --- Critic DeptMeta (#134) -------------------------------------------------

#: Critic 의 물류 밴드 검사 id — **마스터 소유 상수**다
#: (`app/master/critic_bridge.py:51 _INVENTORY_CAP_CHECK`). 마스터가 물류 PRE_PURCHASE
#: payload 의 `warehouse_free_kg`·`cap_by_date` 로 check 를 합성할 때 붙이는 이름이고,
#: **물류는 `checks[]` 를 내지 않는다** (2026-09-01 마스터 확정 — 같은 사실의 주인이
#: 둘이 되면 마스터 합성분과 갈리는 자리가 새로 생긴다).
#:
#: 🔴 **이름이 어긋나면 검사가 조용히 통과한다.** Critic 은
#: `inputs_used.get(check.check_id, ())` 로 읽고 못 찾으면 빈 튜플인데, 빈 튜플은
#: *"금지 입력이 없다"* 로 읽혀 통과다 (`critic_v0_4.py:401`). 에러가 없으니 아무도
#: 모른다.
#:
#: ★ **그래서 문자열을 베끼지 않고 참조한다** (#137). 마스터가 PR #135 로 상수를
#:   공개하며 같은 이유를 적었다 — *"부서가 문자열을 베껴 두면 마스터가 이름을 바꾸는
#:   날 그 부서의 검사가 조용히 무력화된다."* 마스터의
#:   `tests/master/test_dept_meta_check_id.py` 는 **마스터 합성 결과와 상수**가 같은지만
#:   보므로, 물류가 베낀 문자열을 들고 있으면 그 테스트는 초록불인 채 물류 검사만 죽는다.
_CAP_CHECK_ID = DEPT_CAP_CHECK_ID[_AGENT]


class _ToolInputContractMissing(RuntimeError):
    """실행한 Tool 의 입력 계약이 없다. **조용히 0개로 보고하지 않는다.**

    빈 `inputs_used` 는 Critic 이 *"금지 입력이 없다"* 로 읽고 통과시킨다 — 모르는
    것이 통과가 되는 구조라 여기서는 크게 실패하는 편이 낫다 (재무와 같은 판단).
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"Logistics tool has no declared input contract: {tool}")


#: Tool 하나가 스냅샷·요청에서 **실제로 읽는** 입력. `inputs_used` 의 재료다.
#:
#: ★ **선언이 아니라 관측의 재료다.** 이 표만으로 관측을 만들지 않는다 — 그날 실행에
#:   실제로 들어간 Tool 만 골라 합집합을 낸다. 안 돈 Tool 의 입력은 안 실린다.
#:
#: ★ **과다 선언이 안전한 방향이다.** 적게 적으면 Critic 이 금지 입력을 못 보고 조용히
#:   통과시키고, 많이 적으면 검사가 더 걸릴 뿐이다. 그래서 Tool 이 판정에 곁들여 읽는
#:   것(경고용 `snapshot_id`·`evidence_refs`)도 빼지 않는다.
#:
#: ★ **시나리오 계열 Tool 은 금지 이름을 그대로 적는다.** `calculate_expected_arrival_dates`
#:   는 매입 제안을 읽는다. 지금은 SCENARIO_VALIDATION 에서만 돌아 `inputs_used` 에
#:   실리지 않지만, 언젠가 경계 경로로 새면 Critic 이 `E-SCENARIO-LEAK` 으로 잡아야
#:   한다 (`FORBIDDEN_SCENARIO_INPUTS`). 정직하게 적는 것이 그때의 방어다.
_TOOL_INPUTS: dict[str, tuple[str, ...]] = {
    _T_RULES: (
        "logistics_snapshot.guaranteed_capacity_kg",
        "logistics_snapshot.guaranteed_capacity_by_zone_kg",
        "logistics_snapshot.daily_inbound_capacity_kg",
        "logistics_snapshot.inbound_transport_capacity_kg",
        "logistics_snapshot.inbound_lead_days",
        "logistics_snapshot.in_transit",
        "logistics_snapshot.confirmed_inbound_schedule",
        "logistics_snapshot.confirmed_outbound_schedule",
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.snapshot_id",
        "logistics_snapshot.evidence_refs",
    ),
    _T_CAP: (
        "logistics_snapshot.guaranteed_capacity_kg",
        "logistics_snapshot.used_capacity_kg",
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.confirmed_inbound_schedule",
        "logistics_snapshot.confirmed_outbound_schedule",
    ),
    _T_LOTS: ("logistics_snapshot.on_hand_by_lot",),
    _T_INVENTORY: (
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.confirmed_outbound_schedule",
    ),
    # PRE_SALES 전용 — `inputs_used` 에 실리지 않는다 (PRE_SALES 는 DeptMeta 를 내지
    # 않는다, `_inventory_dept_meta` 참조). 그래도 적는 이유는 위 ★ 넷째 항목과 같다:
    # **관측을 내게 되는 날 이 표가 이미 맞아 있어야** 조용한 누락이 안 생긴다.
    _T_SALES_SIGNALS: (
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.freshness_pressure_ratio",
    ),
    # STATUS_QUERY 전용 (#396) — 조회는 DeptMeta 를 내지 않아 `inputs_used` 에 실리지
    # 않는다. 그래도 적는 이유는 위 ★ 넷째 항목과 같다 (관측을 내게 되는 날의 대비).
    _T_USAGE: (
        "logistics_snapshot.guaranteed_capacity_kg",
        "logistics_snapshot.used_capacity_kg",
        "logistics_snapshot.inbound_lead_days",
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.in_transit",
        "logistics_snapshot.confirmed_inbound_schedule",
        "logistics_snapshot.confirmed_outbound_schedule",
    ),
    _T_FRESHNESS: (
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.freshness_pressure_ratio",
    ),
    # 아래 둘은 SCENARIO_VALIDATION 전용이라 `inputs_used` 에 실리지 않는다. 계약을
    # 비워 두지 않는 이유는 위 ★ 넷째 항목이다.
    _T_ARRIVAL: ("scenarios", "split_plan"),
    _T_SIGNALS: (
        "scenarios",
        "logistics_snapshot.on_hand_by_lot",
        "logistics_snapshot.used_capacity_kg",
        "logistics_snapshot.guaranteed_capacity_kg",
        "logistics_snapshot.confirmed_inbound_schedule",
        "logistics_snapshot.confirmed_outbound_schedule",
        "logistics_snapshot.capacity_tight_ratio",
        "logistics_snapshot.freshness_pressure_ratio",
        "logistics_snapshot.item_storage_policies",
    ),
}

#: 어댑터가 **Tool 없이** 직접 만드는 밴드 값의 입력. `_free_capacity()` 가
#: `warehouse_free_kg` 를 만드는데, 이것이 마스터 합성 check 의 `cap_total_kg` 이 된다
#: (`critic_bridge.py:350`). 재무는 cap 을 Tool 이 만들어 이 자리가 없지만 물류는
#: 어댑터가 만든다 — 여기 안 적으면 밴드 절반의 입력이 통째로 빠진다.
_ADAPTER_BAND_INPUTS: tuple[str, ...] = (
    "logistics_snapshot.guaranteed_capacity_kg",
    "logistics_snapshot.used_capacity_kg",
)


def _assert_tool_input_contracts_complete() -> None:
    """모든 물류 Tool 은 입력 계약을 가져야 한다.

    기동 시점에 확인한다 — Tool 을 새로 만들고 계약을 안 적으면 그 사실이 조용한
    `inputs_used` 누락이 아니라 **import 실패**로 즉시 드러난다 (재무와 같은 규율).
    """
    undeclared = sorted(
        {
            _T_RULES,
            _T_CAP,
            _T_ARRIVAL,
            _T_LOTS,
            _T_INVENTORY,
            _T_SIGNALS,
            _T_SALES_SIGNALS,
            _T_USAGE,
            _T_FRESHNESS,
        }
        - set(_TOOL_INPUTS)
    )
    if undeclared:
        raise _ToolInputContractMissing(", ".join(undeclared))


_assert_tool_input_contracts_complete()


def _produced_fields(payload: Mapping[str, Any]) -> list[str]:
    """이번 회신에 **실제로 실린** 필드.

    값이 `None` 인 키는 뺀다 — 산출하지 않은 것을 산출했다고 적으면 권한 검사
    (`E-AUTHORITY`)가 엉뚱한 것을 본다. 어댑터는 원래 없는 값의 키를 아예 안 싣지만
    (§1.2-10), 기준을 명시해 둔다.
    """
    return sorted(key for key, value in payload.items() if value is not None)


_LLM_TRACE_OBSERVATION = "inventory_llm_trace"
"""LLM 실행 관측의 이름 — 결정론 관측(`inventory_dept_meta`)과 **다른 축이다.**

DeptMeta 는 LLM 상태와 무관하게 같아야 하고(그것을 #399 가 잠근다), 이쪽은 LLM
상태에 따라 달라지는 것이 정상이다. 두 책임을 한 이름에 담으면 결정론 보존 검사가
LLM 변화를 잡아 매번 깨지거나, 반대로 검사를 느슨하게 만들게 된다.

★ 상수로 두는 것은 테스트가 문자열을 베끼지 않게 하려는 것이다 (`_CAP_CHECK_ID` 와
  같은 규율). 이름이 바뀌는 날 `_trace_view` 의 제외 필터가 조용히 빗나가면
  결정론 비교가 LLM 관측까지 삼켜 매번 깨진다."""


def _inventory_llm_trace(llm: InterpretationResult | None) -> dict[str, Any] | None:
    """실제로 일어난 Provider 호출 하나의 실행 관측 (#402). **업무 결과가 아니다.**

    `_meta()` 경계에서 종전에 통째로 유실되던 것을 여기로 나른다 — 어떤 Provider 를
    썼나 · 최종 실패 원인이 무엇인가 · 얼마나 걸렸나 · **토큰을 얼마나 썼나**(#406) ·
    어떤 종류의 Fact 가 나갔나.
    공통 `ExecutionMetadata` 에 필드를 넷 더 세우지 않고 기존 확장 채널
    (`observations`)을 쓴다 — 재무가 `finance_llm_provider` 로 같은 성격의 사실을
    싣는 자리와 같다. 마스터는 읽지 않고 나르고, Critic 은 `<dept>_dept_meta` 가
    아닌 관측을 조용히 건너뛴다 (`critic_bridge._dept_meta_in`).

    🔴 **`llm_attempts > 0` 일 때만 만든다.** 그래서

    ```text
    관측이 있다  ⇔  Provider 를 실제로 불렀다
    ```

    가 계약이 된다. `llm_status` 문자열 목록을 여기 복제하지 않는 이유이기도 하다 —
    상태 어휘가 늘면 그 목록만 낡는다. DISABLED · SKIPPED_TEMPLATE 은 관측을 내지
    않는다: 부르지 않은 실행에 provider 이름을 적으면 *"썼다"* 로 읽히고, 그 사실은
    이미 `llm_status` + `llm_attempts=0` 이 정확히 말한다.

    ★ **Sanitized Context 에서 `fact_id` 만 옮긴다.** `label` · `display_value` 는
      싣지 않는다 — display_value 가 곧 판정 수치의 확정 표기라("91.7% (임계 90%)"),
      그것을 실행이력에 복제하면 Provider 전송 경계에서 막은 업무 데이터가 마스터
      실행이력·화면으로 우회해 나간다. 여기서 알아야 할 것은 *"어떤 종류의 Fact 가
      나갔나"* 이지 *"그 값이 얼마였나"* 가 아니다.
    """
    if llm is None or llm.llm_attempts <= 0:
        return None
    return {
        "observation_type": _LLM_TRACE_OBSERVATION,
        "provider": llm.llm_provider or "",
        # 최종 실패 원인 하나. SUCCESS(재시도 후 성공 포함)면 null 이다 — attempt 별
        # 이력을 만들지 않는다(중간 실패는 로그 몫)는 기존 의미를 그대로 나른다.
        "error_kind": llm.llm_error_kind,
        "provider_elapsed_ms": llm.llm_provider_elapsed_ms,
        # 관측된 호출들의 토큰 합 (#406). **숫자 둘만 나른다** — 원본 필드명
        # (`promptTokenCount` · `prompt_eval_count`)도, raw 응답도 오지 않는다.
        # ★ 값이 `None` 이어도 키는 **뺴지 않고 null 로 남긴다.** 관측의 key 집합이
        #   실행마다 달라지면 읽는 쪽이 "필드가 없다"와 "값이 없다"를 구별하지 못하고,
        #   `_LLM_TRACE_KEYS` 의 `==` 잠금도 성립하지 않는다.
        "observed_input_tokens": llm.llm_observed_input_tokens,
        "observed_output_tokens": llm.llm_observed_output_tokens,
        "context_fact_ids": [fact.fact_id for fact in llm.llm_context_facts],
    }


def _inventory_dept_meta(
    mode: str,
    payload: Mapping[str, Any],
    tools: Sequence[str],
) -> dict[str, Any] | None:
    """이번 실행이 읽은 입력과 낸 산출을 **물류 자신이** 기계가 읽을 형태로 낸다.

    Critic 의 `E-AUTHORITY`(부서가 S3 전속 판정을 냈나)와 `E-SCENARIO-LEAK`(밴드 검사가
    매입 시나리오를 읽었나)은 이것이 없으면 아예 돌지 않는다 — **통과가 아니라 생략**
    이다. 마스터는 Tool 이름이나 payload 키를 보고 물류가 무엇을 읽었는지 알 수 없어
    추측하지 않는다.

    `PRE_PURCHASE` 만 `inputs_used` 를 낸다. 마스터가 밴드 check 를 합성하는 입력은
    `constraints` 이고 그것은 PRE_PURCHASE 회신만 모으므로
    (`master/flow.py::_collect_constraints`), SCENARIO_VALIDATION 에는 대응하는 cap
    검사 축이 없다. 없는 검사에 가짜 `inputs_used` 를 지어내지 않고 **실제 산출 필드만**
    낸다 — `E-AUTHORITY` 는 그것으로 돈다. 마스터가 두 mode 의 관측을 **합쳐서** 나르므로
    빈 `inputs_used` 가 경계 관측을 덮지 않는다 (`critic_bridge._dept_meta_in`).

    🔴 **`PRE_SALES` 는 관측 자체를 내지 않는다 — `None` 이 답이다** (#346).
      `_dept_meta_in` 을 부르는 것은 매입 Flow(`master/flow.py` → `critic_bridge`)뿐이고
      판매 Flow(`master/sales_flow.py`)에는 Critic 경로가 아예 없다. 소비자가 없는데
      관측을 내면 두 가지가 동시에 틀린다 — `_CAP_CHECK_ID` 는
      `DEPT_CAP_CHECK_ID["inventory"]`, 즉 **매입 밴드 전용 이름**이라 판매 회신의
      입력이 그 이름으로 실리면 매입의 밴드 검사가 판매 입력을 읽고, 새 check_id 를
      지어내면 **마스터가 만들지 않은 계약**을 물류가 먼저 만드는 것이 된다.
      마스터가 판매용 check 계약을 내는 날 여기에 분기를 추가한다.
    """
    if mode == "SCENARIO_VALIDATION":
        return {
            "observation_type": "inventory_dept_meta",
            "inputs_used": {},
            "produced_fields": _produced_fields(payload),
        }
    if mode != "PRE_PURCHASE":
        return None
    inputs: list[str] = list(_ADAPTER_BAND_INPUTS)
    for tool in tools:
        if tool not in _TOOL_INPUTS:
            raise _ToolInputContractMissing(tool)
        for name in _TOOL_INPUTS[tool]:
            if name not in inputs:
                inputs.append(name)
    return {
        "observation_type": "inventory_dept_meta",
        "inputs_used": {_CAP_CHECK_ID: inputs},
        "produced_fields": _produced_fields(payload),
    }


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


#: 실행 축을 **실제로 읽는** mode — 이 넷만 `sim_run_id` 문을 지난다 (#345 · #346).
#
# 🔴 **미구현 mode 를 이 문 앞에 세우지 않는다.** 실행 축이 비었다고 *"번역이 없다"* 를
#   `sim_run_id` 누락으로 바꾸면 **없는 구현을 값 탓으로 돌리는 거짓**이 되고, 마스터는
#   그 말을 듣고 사용자에게 줄 수 없는 것을 달라고 한다 (M-1 §5.1).
#
#   ★ **`PRE_SALES` 가 이 문 뒤로 들어온 것은 #346 이 그 번역을 실제로 구현했기
#     때문이다.** 종전 주석은 *"`PRE_SALES` 는 아직 `_not_implemented` 가 받는 자리"* 라고
#     적혀 있었는데 그 문장은 이제 사실이 아니다. 규율은 그대로다 — **바뀐 것은 예시가
#     아니라 사실이고, 규율은 아래 불변식이 지킨다.**
#
#   ★ **불변식: 이 집합 ⊆ `logistics_port` 가 실제 handler 로 보내는 mode.**
#     구현보다 문이 먼저 서면 그 순간 위 거짓이 되살아난다.
#     `tests/logistics/test_logistics_adapter.py` 가 구조로 잠근다.
#
# ★ 아래 네 handler 가 전부 같은 `_load_read` 를 지나므로 문은 하나면 된다.
_RUNTIME_AXIS_MODES: frozenset[str] = frozenset(
    {"PRE_PURCHASE", "PRE_SALES", "SCENARIO_VALIDATION", "STATUS_QUERY"}
)


def logistics_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 접점."""
    # 🔴 **어느 실행의 장부인지 모르면 읽지 않는다** (#345). 물류는 이 값을 지어내지
    #    않는다 — 마스터가 소유한 값이고, 없으면 없다고 답하는 것이 답이다.
    if request.mode in _RUNTIME_AXIS_MODES and not request.context.sim_run_id.strip():
        return _no_run_axis(request)
    if request.mode == "PRE_PURCHASE":
        return _pre_purchase(request)
    if request.mode == "PRE_SALES":
        return _pre_sales(request)
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

    ★ **운영 Fact 를 함께 싣는다** (#396). 사용률·신선도 비율과 **그 임계 정책값**을
      나란히 두어 사람이 스스로 판단하게 한다.

      ```text
      capacity_window_usage_ratio      Tool 산출 — 창 안에서 가장 빡빡한 날의 사용률
      capacity_tight_ratio             정책 원값 (PROVISIONAL)
      freshness_min_remaining_ratio    Tool 산출 — 셈할 수 있었던 ACTIVE Lot 의 최소 비율
      freshness_pressure_ratio         정책 원값 (PROVISIONAL)
      freshness_risk_lot_count         Rule 비교식 재사용 — 임계 이하 Lot 수
      freshness_unresolved_lot_count   잔여·한계 미확인 Lot 수 (0 이 아니라 '모른다')
      freshness_expired_lot_count      잔여 0 이하로 **확인된** Lot 수
      ```

      🔴 **재기만 하고 판정하지 않는다.** 임계를 실어도 비교는 하지 않는다 —
         `CAPACITY_TIGHT` · `INVENTORY_FRESHNESS_PRESSURE` · `FRESHNESS_QUALITY_RISK` 는
         Rule 소유 signal 이고 조회는 그것도 `judgment_fields` 도 내지 않는다.

      🔴 **`freshness_expired_lot_count` 는 폐기 대상 수가 아니다.** 폐기대기 판정과
         실행은 `turnover` · `disposal` 소유이고 사람이 확정한다 — 어댑터는 그 모듈을
         부르지도, `disposal_candidate` 어휘를 쓰지도 않는다.

      🔴 **신선도 넷의 모집단은 `ACTIVE` Lot 이라 위 `lot_count` 와 다르다.**
         `lot_count` 는 창고에 남아 있는 Lot 전부다 — 비-ACTIVE 도 반출 전이면 공간을
         차지하므로 Repository 가 status 로 거르지 않는다. 두 수의 합은 안 맞는다.
    """
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_LOTS]

    try:
        read = _load_read(as_of=as_of, sim_run_id=request.context.sim_run_id)
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
    # 신선도 비율의 **분모**(유효 보관한계)는 품목 정책에서 왔다 — Lot 참조만 달면
    # "이 비율이 어디서 왔나" 를 따라갔을 때 분모의 출처가 빠진다 (PRE_SALES 와 같은 규율).
    policies_ref = _policies_ref(snapshot)
    freshness_refs = (policies_ref,) if policies_ref != lots_ref else ()
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

    # ── 창고 사용률 (측정) ───────────────────────────────────────
    #
    # ★ **여전히 임계를 지어내지 않는다** (#396). 위 신선도 주석의 규율은 그대로다 —
    #   달라진 것은 *"등록된 정책값을 그대로 나른다"* 이지 *"어댑터가 기준을 고른다"* 가
    #   아니다. 사용률과 임계를 나란히 싣되 **비교는 하지 않는다**: `CAPACITY_TIGHT` 는
    #   Rule 소유 signal 이고 조회는 그것을 내지 않는다.
    #
    # ★ `cap_by_date` 표는 여전히 안 싣는다. 사람이 읽을 것은 *"가장 빡빡한 날이 몇 %"*
    #   한 값이고, 창의 정의(시작일·길이)는 Tool 소유라 어댑터가 다시 나열하지 않는다.
    tools.append(_T_USAGE)
    usage = calculate_window_capacity_usage(snapshot, as_of)
    if usage is None:
        # 리드타임·확정 일정이 없어 창을 못 세운 것이다. `PRE_PURCHASE` 는 같은 사실을
        # hard_constraints 로 나르지만 **조회에는 그 칸이 없어** 여기서 이름을 밝힌다.
        missing.append("capacity_window_usage_ratio")
    else:
        payload["capacity_window_usage_ratio"] = _num(usage)

    capacity_tight_ratio = snapshot.capacity_tight_ratio
    if capacity_tight_ratio is None:
        # 선택 정책 미등재 — 값을 지어내지 않는다. "기준이 없어 못 쟀다" 와
        # "재 봤더니 안전하다" 는 다르다 (LLM 정책 결정서 §4 와 같은 태도).
        missing.append("capacity_tight_ratio")
    else:
        payload["capacity_tight_ratio"] = _num(capacity_tight_ratio)
        if "capacity_tight_ratio" not in policy.source_refs:
            missing.append("capacity_tight_ratio@policy_source_ref")

    # ── 신선도 측정 ─────────────────────────────────────────────
    #
    # 🔴 **모집단이 위 `lot_count` 와 다르다.** 아래 넷은 전부 **ACTIVE Lot** 만 보고
    #    (`tools._AVAILABLE_LOT_STATUS`), `lot_count` 는 창고에 남아 있는 Lot 전부다 —
    #    비-ACTIVE(격리·검수)도 반출 전이면 공간을 차지하므로 Repository 가 status 로
    #    거르지 않는다. 두 수의 합이 안 맞는 것이 정상이라 근거 문장에 모집단을 적는다.
    #
    # 🔴 **비교식을 어댑터가 만들지 않는다.** 위험 Lot 수의 `<= 임계` 는
    #    `rules.count_freshness_risk_lots` 소유이고 signal 판정이 쓰는 그 함수다.
    #    여기서 다시 세면 *"signal 은 섰는데 위험 Lot 0건"* 이 성립할 수 있다.
    tools.append(_T_FRESHNESS)
    freshness = measure_freshness_facts(snapshot=snapshot)
    # 🔴 **건수는 `int` 로 둔다.** `_num()`(= `float()`) 을 태우면 `3` 이 `3.0` 으로
    #    나간다 — `inbound_lead_days` 가 정확히 그 경로로 새어 나갔다 (#221).
    payload["freshness_unresolved_lot_count"] = freshness["freshness_unresolved_lot_count"]
    payload["freshness_expired_lot_count"] = freshness["freshness_expired_lot_count"]

    min_freshness_ratio = freshness.get("freshness_min_remaining_ratio")
    if min_freshness_ratio is None:
        # 비율을 셈할 수 있는 ACTIVE Lot 이 하나도 없다 — 0 으로 덮지 않는다.
        missing.append("freshness_min_remaining_ratio")
    else:
        payload["freshness_min_remaining_ratio"] = _num(min_freshness_ratio)

    freshness_risk_lot_count = freshness.get("freshness_risk_lot_count")
    if freshness_risk_lot_count is None:
        # 임계 정책이 없어 세지 않았다. 기준 없는 `0` 은 "확인했고 없음" 으로 읽힌다.
        missing.append("freshness_risk_lot_count")
    else:
        payload["freshness_risk_lot_count"] = freshness_risk_lot_count

    freshness_pressure_ratio = snapshot.freshness_pressure_ratio
    if freshness_pressure_ratio is None:
        missing.append("freshness_pressure_ratio")
    else:
        payload["freshness_pressure_ratio"] = _num(freshness_pressure_ratio)
        if "freshness_pressure_ratio" not in policy.source_refs:
            missing.append("freshness_pressure_ratio@policy_source_ref")

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

    # ── 운영 Fact 근거 (#396) ────────────────────────────────────
    #
    # ★ 넷은 Tool 산출(`tool_calc`)이고 둘은 정책 원값(`SIM_FIXED`)이다. 표기를 나누는
    #   이유는 읽는 사람이 *"잰 값"* 과 *"기준"* 을 구별해야 하기 때문이다.
    if "capacity_window_usage_ratio" in payload:
        evidences.append(
            _ev(
                "capacity_window_usage_ratio",
                payload["capacity_window_usage_ratio"],
                "ratio",
                ref,
                "고정 조회 창에서 가장 빡빡한 날의 창고 사용률 — "
                "1 − (창 내 최소 여유 ÷ 보장 Capacity). 확정 입·출고만 반영하며 "
                "임계와 비교하지 않는다",
                source="tool_calc",
            )
        )
    if "capacity_tight_ratio" in payload:
        evidences.append(
            _ev(
                "capacity_tight_ratio",
                payload["capacity_tight_ratio"],
                "ratio",
                _policy_ref(policy, "capacity_tight_ratio", ref),
                f"창고 압박 판정 임계 정책값 ({policy.policy_version}) — 실업계 기준이 아니라 "
                "시뮬레이션·Agent 검증용 PROVISIONAL 운영값이다. 이 회신은 나란히 싣기만 "
                "하고 비교하지 않는다",
                grade="SIM_FIXED",
            )
        )

    evidences.append(
        _ev(
            "freshness_unresolved_lot_count",
            payload["freshness_unresolved_lot_count"],
            "count",
            lots_ref,
            "ACTIVE Lot 중 잔여 신선도 또는 유효 보관한계를 확인하지 못해 비율 계산에서 "
            "뺀 건수 — 위 lot_count 와 모집단이 다르고, 0 취급이 아니라 '모른다' 의 건수다",
            source="tool_calc",
            extra_ref_ids=freshness_refs,
        )
    )
    evidences.append(
        _ev(
            "freshness_expired_lot_count",
            payload["freshness_expired_lot_count"],
            "count",
            lots_ref,
            "ACTIVE Lot 중 잔여 신선도가 0 이하로 확인된 건수 — 판매 가용에서 빠지는 상태 "
            "사실이다. 폐기 판정도 폐기 대상 수도 아니며, 폐기는 turnover·disposal 이 "
            "소유하고 사람이 확정한다",
            source="tool_calc",
            extra_ref_ids=freshness_refs,
        )
    )
    if "freshness_min_remaining_ratio" in payload:
        evidences.append(
            _ev(
                "freshness_min_remaining_ratio",
                payload["freshness_min_remaining_ratio"],
                "ratio",
                lots_ref,
                "비율을 셈할 수 있었던 ACTIVE Lot 의 최소 잔여 비율 — "
                "잔여 신선도 ÷ 유효 보관한계. 분모는 중 등급 계수가 반영된 값이라 "
                "품목 보관 정책 원값과 다를 수 있다",
                source="tool_calc",
                extra_ref_ids=freshness_refs,
            )
        )
    if "freshness_risk_lot_count" in payload:
        evidences.append(
            _ev(
                "freshness_risk_lot_count",
                payload["freshness_risk_lot_count"],
                "count",
                lots_ref,
                "비율을 셈할 수 있었던 ACTIVE Lot 중 잔여 비율이 정책 임계 이하인 건수 — "
                "Rule 이 소유한 비교식을 그대로 쓴다. 이 건수는 signal 을 만들지 않는다",
                source="tool_calc",
                extra_ref_ids=freshness_refs,
            )
        )
    if "freshness_pressure_ratio" in payload:
        evidences.append(
            _ev(
                "freshness_pressure_ratio",
                payload["freshness_pressure_ratio"],
                "ratio",
                _policy_ref(policy, "freshness_pressure_ratio", ref),
                f"신선도 압박 판정 임계 정책값 ({policy.policy_version}) — 실업계 기준이 아니라 "
                "시뮬레이션·Agent 검증용 PROVISIONAL 운영값이다",
                grade="SIM_FIXED",
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
    return reply, _meta(request, run_id, tools, reply)


# ---------------------------------------------------------------------------
# PRE_PURCHASE — 경계 제공
# ---------------------------------------------------------------------------


def _pre_purchase(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_RULES]

    try:
        read = _load_read(as_of=as_of, sim_run_id=request.context.sim_run_id)
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
        "daily_inbound_capacity_kg",
        "inbound_transport_capacity_kg",
        "shared_daily_outbound_capacity_kg",
    ):
        value = getattr(snapshot, name)
        if value is None:
            missing.append(name)
        else:
            payload[name] = _num(value)

    # 🔴 `inbound_lead_days` 는 **위 루프에 넣지 않는다** (#221 · 매입 지적 2026-09-03).
    #
    #   위 다섯은 kg(`Decimal`)이고 이것 하나가 **일수(`int`)** 다. 한 루프로 묶여
    #   `_num()` = `float()` 을 타면서 `2` 가 `2.0` 으로 나갔다.
    #
    #       물류 내부   schemas.py  inbound_lead_days: int = Field(ge=0)
    #       봉투        2.0                                         ← 이탈자
    #       IO Contract §3  "inbound_lead_days": 2
    #
    # ★ 비용이 매입 하나가 아니었다 — 마스터가 `critic_bridge.py` `_int_of` 와
    #   `commitment.py` 의 `lead != int(lead)` 로 방어를 둘 만들어 뒀고, 매입도
    #   `purchase_agent/adapter.py` 에서 `lead != int(lead)` 를 세운다. 생산자가
    #   맞게 보내면 그 방어들이 **무해해진다** — 지우지는 않는다. 다른 생산자가
    #   붙을 수 있고, 그때도 같은 자리에서 막혀야 한다.
    if snapshot.inbound_lead_days is None:
        missing.append("inbound_lead_days")
    else:
        payload["inbound_lead_days"] = int(snapshot.inbound_lead_days)

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
    return reply, _meta(request, run_id, tools, reply)


# ---------------------------------------------------------------------------
# PRE_SALES — 판매 제안 전 "지금 팔 수 있는 것" 컨텍스트 (#346)
# ---------------------------------------------------------------------------

#: ★ **WP-4 가 셋을 실제로 계산하게 되면서 세 «모른다» 이름이 사라졌다.**
#:
#: ```text
#: ~WP-3  SUPPLY_CAPACITY_BY_DATE_UNRESOLVED   권위 계산이 없다
#:        TRANSPORT_LEAD_TIME_UNRESOLVED       standard_minutes 칸이 없다
#:        EARLIEST_DELIVERY_DATE_UNRESOLVED    가장 이른 납기일을 내는 함수가 없다
#: WP-4~  tools.supply_capacity_by_date · TRANSPORT_LEAD_DAYS · earliest_delivery_date_for
#: ```
#:
#: 🔴 **값을 냈는데 «모른다» 로도 적으면 계약이 스스로 모순된다.** 그래서 이름만 지운
#:    것이 아니라 **낼 수 있게 된 다음에** 지웠다.
#:
#: ⚠️ **`DELIVERY_ROUTE_UNRESOLVED` 하나는 살아 있다** —
#:    `tools.evaluate_delivery_feasibility` 가 계약 표를 못 읽었을 때 쓴다. 계약 행이
#:    없는 것은 회사 상태라 그때는 판정을 안 낸다.


def _pre_sales(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """판매가 후보를 만들기 **전에** 묻는 것 — *"지금 무엇을 얼마나 팔 수 있나."*

    ★ **새 판매가능량 엔진이 아니다.** 숫자는 전부 `tools` · `rules` 의 결정론 함수가
      만들고 여기는 번역만 한다 (이 파일의 다른 handler 와 같은 규율).

    🔴 **기존 `/logistics/sales` 경로를 부르지 않는다** (#346 분석 결과). 닮은 이름이라
       재사용처럼 보이지만 **목적이 다른 사이클**이다.

    ```text
    /logistics/sales   H1 **승인 매입**을 미래 입고로 Overlay 한 뒤의 창고 판정
    PRE_SALES          판매 제안 **전**의 초기 컨텍스트 — 승인 매입이 아직 없다
    ```

      ★ 셋이 각각 다른 이유로 막힌다.

      ```text
      run_logistics_sales()               _get_snapshot_or_none(as_of) → sim_run_id 축 소실
                                          + save_logistics_agent_run() → DB write
      run_logistics_sales_with_snapshot()  enrich_logistics_response() → LLM 경로
      run_logistics_sales_scenario()       request.approved_purchase 를 반드시 읽는다
      evaluate_sales_rules()               future_occupancy_by_date(=Overlay 산출)를 전제한다
      ```

      🔴 **가짜 승인 매입을 만들어 통과시키지 않는다.**
         `LogisticsApprovedPurchaseCommitment` 은 `total_qty_kg > 0` ·
         `arrival_schedule` 최소 1건이라 **빈 값도 0 도 넣을 수 없다** — 넣으려면
         없는 입고를 지어내야 하고, 그 지어낸 입고가 `LOG-H01`(미래 점유 ≤ 보장 capacity)
         판정을 그대로 바꾼다. 숫자는 나오고 에러도 안 나며 봉투도 통과한다.

    ★ **그래서 재사용 단위는 함수다** — `build_inventory_by_item` ·
      `build_lot_constraints` · `evaluate_sales_business_signals` 셋은 승인 매입 없이
      돌고, 세 함수가 PRE_SALES 가 답할 수 있는 것의 전부다.

    ★ **payload 는 판매 계약(`app.sales.schemas.SalesLogisticsContext`)의 낱말을 쓴다.**
      🔴 **그 모듈을 import 하지 않는다** — 조정자를 건너뛰고 두 부서를 실행 계층에서
      붙이면 판매가 자기 파일을 고치는 날 물류가 같이 깨진다 (마스터가 판매 어휘를
      베껴 두고 테스트로만 대조하는 것과 같은 판단, `master/envelope.Capability`).
      맞추는 것은 **JSON 모양뿐**이다.

      🔴 **숫자를 실은 셋만 payload 최상위에 둔다.**

      ```text
      inventory_by_item                  → sellable_supply.inventory_by_item
      lot_constraints                    → sellable_supply.lot_constraints
      shared_daily_outbound_capacity_kg  → delivery_feasibility.daily_outbound_capacity_kg
      ```

         나머지는 판매 계약 그대로 중첩인데, 이 셋만 올라온 것은 취향이 아니라
         **봉투가 중첩 안의 숫자를 주소지정하지 못하기 때문**이다.

      ```text
      envelope._CLAIM_PATH   ^(?P<key>[^\\[\\].]+)\\[(?P<sel>[^\\]]+)\\]\\.(?P<sub>.+)$
                             key 에 점을 못 쓴다 → 한 겹만 판다
      envelope.required_claims   Mapping 값은 통째로 건너뛴다
      ```

         중첩해 두면 두 가지가 동시에 일어난다 — 판매가능 수량·Lot 수량·신선도·출고
         여력에 **근거가 하나도 요구되지 않고**(검사가 없어서 통과), 그렇다고 근거를
         달면 `sellable_supply.inventory_by_item[배추].available_qty_kg` 가 어디도 못
         가리켜 `E-EVIDENCE-ORPHAN` 이 된다.

         ★ **가장 가까운 조상에 다는 것도 답이 아니다.** 출고 여력을
           `delivery_feasibility` 안에 두고 근거를 그 블록 이름에 달아 봤더니
           *"`delivery_feasibility` 라는 판정의 값이 5,000kg"* 으로 읽혔다 — 그 판정은
           `UNRESOLVED` 라 **근거와 대상의 뜻이 어긋난 채 봉투를 통과했다.**
           그래서 그 숫자도 정책 이름 그대로 최상위로 올렸다.

         **근거를 붙일 수 있는 자리가 최상위뿐**이라 숫자를 실은 셋만 올린다.
         받는 쪽이 제자리로 옮기는 것은 위 표대로 **키 셋을 옮기는 일**이다.

    ★ **판정하지 않는다.** 판매 승인·거절은 판매와 재무가 하고 물류는 사실만 낸다 —
      `judgment_fields` 가 비어 있고 새 verdict 도 만들지 않는다 (`_status_query` 와 같다).
    """
    as_of = request.context.as_of
    run_id = _run_id(request)
    tools: list[str] = [_T_INVENTORY]

    try:
        read = _load_read(as_of=as_of, sim_run_id=request.context.sim_run_id)
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
    #
    # ★ **사유에 날짜를 적지 않는다.** `check_reasoning` 의 `E-REASONING-NUMERIC` 은
    #   `contributes_to_band` 와 무관하게 돌아서 `2025-12-31` 같은 문자열이 걸린다.
    #   어긋난 기준일은 `missing_data` 이름이 이미 나른다.
    if snapshot.as_of != as_of:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=(f"logistics_snapshot@{as_of.isoformat()}",),
            reason="물류 스냅샷 기준일이 요청 기준일과 다르다",
        )

    # ── 현재 확정 판매가능량 ─────────────────────────────────────
    #
    # 🔴 **이 한 함수가 confirmed sellable 의 유일한 주인이다.** 새 계산식을 만들지
    #    않는다 — `outbound.item_free_stock_qty`(예약이 실제로 잡을 수 있는 양)와 같은
    #    답을 내도록 차감 규칙이 글자 그대로 맞춰져 있고, 그 둘이 갈리면 매입·판매는
    #    팔 수 있다고 보는데 예약은 못 잡는 상태가 된다.
    #
    # ★ **`None` 은 fail-closed 다.** 예약·할당 축(`outbound_commitments`)이나 확정
    #   출고의 품목 축을 못 읽었다는 뜻인데, 그때 `lot_constraints` 합계로 대신 답하면
    #   **이미 팔린 재고를 다시 팔 수 있다고 답하게 된다.** 판매는 밴드가 없어 이
    #   회신 없이도 시작하지만(`sales_flow._collect_supply_context`), 시작하는 것과
    #   틀린 수량을 주는 것은 다르다.
    inventory_by_item = build_inventory_by_item(snapshot)
    if inventory_by_item is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("inventory_by_item",),
            reason=(
                "현재 판매 가능 재고를 확정하지 못했다 — "
                "예약·할당 축이나 확정 출고의 품목 축을 읽지 못했다."
            ),
        )

    # ── 출고 여력 ────────────────────────────────────────────────
    #
    # ★ **없으면 READY 를 내지 않는다.** 판매 사이클의 기존 Rule 이 같은 기준이다 —
    #   `evaluate_sales_rules` 의 `calculation_ready` 가 `N17`
    #   (`shared_daily_outbound_capacity_kg`)을 필수로 세고 있다. 기준을 새로 정하는
    #   것이 아니라 **그 Rule 이 이미 정해 둔 것을 따른다.**
    outbound_capacity = snapshot.shared_daily_outbound_capacity_kg
    if outbound_capacity is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("shared_daily_outbound_capacity_kg",),
            reason="공용 일일 출고 여력 정책이 없어 물류 상태를 답할 수 없다",
        )

    # ── Lot 근거 ─────────────────────────────────────────────────
    tools.append(_T_LOTS)
    lots = build_lot_constraints(snapshot)

    # 🔴 **잔여 신선도를 낸 그 분모**를 함께 나른다. `remaining_freshness_days` 만 주면
    #    받는 쪽이 *"며칠 중 며칠이 남았나"* 를 알 수 없어 `operational_limit_days`
    #    원값으로 역산하는데, `중` 등급은 유효 한계가 `operational × medium_factor` 라
    #    그 역산이 **갓 입고된 Lot 을 임박으로 만든다** (`InventoryLotSnapshot`
    #    `effective_freshness_limit_days` 주석이 지적한 그 자리).
    #
    # ★ **다시 계산하지 않는다** — Repository 가 remaining 을 만들 때 실제로 쓴 값을
    #   `lot_id` 로 그대로 집어 온다. `build_lot_constraints` 가 이 칸을 안 나르는 것은
    #   `LotConstraint` 계약이라 물류 스키마를 여기서 넓히지 않는다 (#346 범위 밖).
    freshness_limits = {
        lot.lot_id: lot.effective_freshness_limit_days for lot in snapshot.on_hand_by_lot
    }

    # ── 신선도 업무 위험 ─────────────────────────────────────────
    #
    # ★ **승인 매입 없이 도는 유일한 판매 Rule 이다.** `evaluate_sales_rules` 와 달리
    #   스냅샷만 읽는다.
    #
    # 🔴 **`FRESHNESS_QUALITY_RISK` 는 `SELL_PRIORITY` 가 아니다.** 이름이 비슷해 섞기
    #    쉬운데 축이 다르다 — 이쪽은 *"지금 팔 수 있는 재고인가"*(물리 신선도)이고
    #    `SELL_PRIORITY` 는 *"언제 팔고 싶은가"*(회전관리 · `item_turnover_policies`)다.
    #    `turnover.py` 모듈 주석이 **둘은 동시에 다른 답을 낼 수 있어야 한다**고 못박고
    #    있다. 이름을 바꿔 대신 쓰지 않는다.
    tools.append(_T_SALES_SIGNALS)
    business = evaluate_sales_business_signals(snapshot=snapshot)

    # ── 사용자가 물은 것 ─────────────────────────────────────────
    #
    # 🔴 **없는 값을 지어내지 않는다.** 수량도 날짜도 마스터가 실어 준 것만 읽는다 —
    #    없으면 그 축의 판정을 안 한다 (`_query_scope` 와 같은 규율).
    asked = _sales_ask(request)

    # ── 미래 확정 출고 ───────────────────────────────────────────
    #
    # 🔴 **스냅샷이 이미 들고 있는 한 벌을 쓴다. 다시 읽지 않는다 (WP-4B).**
    #
    #    `snapshot.confirmed_outbound_schedule` 은 Repository 가
    #    `outbound_schedules.confirmed_outbound_at` 으로 채운 **WP-3 정본**이고
    #    (`confirmed_outbound_json` 은 죽은 칸이다), Capacity 축이 이미 그 값을 쓴다.
    #
    #    ⚠️ **종전에는 어댑터가 같은 질의를 자기 커넥션으로 한 번 더 돌렸다.** 두 읽기
    #       사이에 판매가 확정·취소되면 **같은 회신 안에서** Capacity 와 납기 판정이
    #       서로 다른 «미래 출고» 를 보고 답한다 — `as_of` 가 같아도 DB 행은 그 사이에
    #       바뀔 수 있다. 한 벌만 읽으면 그 갈림이 구조적으로 없다.
    #
    # ★ **`None` 은 0 이 아니다.** fixture 가 그 축을 `UNRESOLVED` 로 적었다는 뜻이라
    #   (`repository._schedule_source`) 확인 못 한 것을 «출고 0kg» 으로 놓으면 하루
    #   여력을 통째로 비어 있다고 답하게 된다. 그때는 납기도 날짜별 공급량도 안 낸다.
    outbound_rows = snapshot.confirmed_outbound_schedule
    outbound_by_date: dict[date, Decimal] = {}
    for row in outbound_rows or ():
        outbound_by_date[row.date] = outbound_by_date.get(row.date, Decimal(0)) + row.quantity_kg

    # ── 운송 계약 ────────────────────────────────────────────────
    #
    # 🔴 **여기서 DB 를 열지 않는다.** 계약은 `_load_read` 가 같은 읽기 한 벌에 담아
    #    왔다 (`repository.LogisticsRead.delivery_route`) — 어댑터는 번역만 한다.
    if read.delivery_route_error:
        return _delivery_input_error(request, run_id, tools)
    route = read.delivery_route

    delivery = evaluate_delivery_feasibility(
        as_of=as_of,
        daily_outbound_capacity_kg=outbound_capacity,
        outbound_prep_lead_days=read.policy.outbound_prep_lead_days,
        delivery_route=route,
        confirmed_outbound_known=outbound_rows is not None,
        requested_quantity_kg=asked.quantity_kg,
        preferred_delivery_date=asked.delivery_date,
        confirmed_outbound_on_preferred_kg=(
            None if asked.delivery_date is None else outbound_by_date.get(asked.delivery_date)
        ),
    )

    # ── 날짜별 공급량 ────────────────────────────────────────────
    #
    # 🔴 **창을 물류가 만들지 않는다.** 사용자가 물은 납기일 하나가 답할 날짜이고,
    #    안 물었으면 답할 날짜가 없다. 여기서 임의 창(예: 18일)을 만들면 **묻지도 않은
    #    날짜의 공급량**이 판매 근거로 나간다.
    #
    # ★ **빈 목록은 «못 냈다» 가 아니다.** 그래서 `SUPPLY_CAPACITY_BY_DATE_UNRESOLVED`
    #   를 안 단다 — 계산에 실패한 것과 물어본 날짜가 없는 것은 다른 사실이다 (§1.2-10).
    #
    # 🔴 **미래 확정 출고를 못 읽었으면 날짜별 공급량도 안 낸다.** 그 값의 상한 절반이
    #    그 축이라(§확정 판매가능량 = min(재고, 남은 출고 여력)) 모르는 채로 내면
    #    **재고 축만 본 수치**가 확정 공급량 행세를 한다.
    supply_dates = (
        [asked.delivery_date]
        if asked.delivery_date is not None and outbound_rows is not None
        else []
    )
    supply_rows = supply_capacity_by_date(
        snapshot,
        dates=supply_dates,
        inventory_by_item=inventory_by_item,
        confirmed_outbound_by_date=outbound_by_date,
        daily_outbound_capacity_kg=outbound_capacity,
        item=asked.item,
    )

    # ── 확정 물량의 재고 취득원가 ────────────────────────────────
    #
    # 🔴 **원가의 주인은 창고다.** 어느 Lot 이 얼마에 들어왔는지는 물류 장부의 사실이고,
    #    판매도 재무도 그것을 다시 셈할 근거가 없다. 종전에는 아무도 내지 않아 재무가
    #    매번 `authoritative_inventory_cost_basis` 없음으로 판정을 닫았다.
    #
    # ★ **못 내면 안 낸다 — READY 는 그대로다.** 재고원가는 판매 제안 자체의 전제가
    #   아니라 재무 판정의 재료다. 없으면 재무가 `RUNTIME_NOT_READY` 로 멈추고,
    #   그 이름(`authoritative_inventory_cost_basis`)은 재무가 이미 부른다 — 여기서
    #   같은 사실에 두 번째 이름을 붙이지 않는다.
    #
    # 🔴 **Lot 선택 순서는 실제 자동 출고와 같은 FEFO 다** (`turnover.fefo_sort_key`).
    #    이 시점에는 이 판매의 할당이 아직 없어서 «출고된 Lot» 을 못 적는다 —
    #    적는 것은 *"지금 출고한다면 FEFO 가 집을 Lot"* 이고, 그래서 **순서만이라도**
    #    실제와 같아야 한다. 규칙이 다르면 예상이 빗나가는 것이 아니라 처음부터
    #    다른 것을 재는 것이 된다.
    cost_basis = _confirmed_inventory_cost_basis(
        snapshot,
        asked=asked,
        inventory_by_item=inventory_by_item,
        supply_rows=supply_rows,
    )

    # ── payload ──────────────────────────────────────────────────
    ref = _ref(snapshot)
    lots_ref = _lots_ref(snapshot)
    policies_ref = _policies_ref(snapshot)

    # 🔴 **구조적으로 못 내는 것의 이름.** READY 를 막지는 않지만 조용히 빠지지도
    #    않는다 (§1.2-10).
    #
    # ★ **낸 것은 여기서 뺀다.** WP-4 가 `supply_capacity_by_date` · `delivery_route` ·
    #   `transport_lead_time` · `earliest_delivery_date` 를 실제로 계산하게 됐다 —
    #   값을 냈는데 *"모른다"* 로도 적으면 계약이 스스로 모순된다.
    #
    # 🔴 **`delivery_feasibility` 를 여기 적지 않는다.** 그 블록은 **있다** — 판정이
    #    무엇이든 있는 것을 없다고 적으면 마스터가 *"물류가 납기 블록을 안 보냈다"* 로
    #    읽고 사용자에게 엉뚱한 것을 달라고 한다 (M-1 §5.1).
    missing: list[str] = []
    if delivery.status == "UNRESOLVED":
        # ★ 물류 내부 어휘(`*_UNRESOLVED`)를 마스터가 읽는 이름으로 옮긴다 —
        #   `interpretation._MISSING_DATA_NAMES` 와 같은 층 구분이다.
        missing.extend(_DELIVERY_MISSING_NAMES[code] for code in delivery.uncertainties)

    payload: dict[str, Any] = {
        "query_scope": _query_scope(request, as_of),
        # ★ **중첩 한 벌이 정본이다 (WP-4).** 종전에는 숫자를 실은 셋을 payload 최상위로
        #   끌어올려 중복시켰다 — 봉투가 중첩 안의 숫자를 주소지정하지 못해서였다
        #   (`envelope._CLAIM_PATH` 가 한 겹만 팠다). WP-4 가 그 봉투를 고쳤으므로
        #   **판매 계약 모양 그대로** 낸다. 한 사실이 두 자리에 있으면 받는 쪽이
        #   어느 것을 볼지 갈린다.
        "sellable_supply": {
            "status": "READY",
            "inventory_by_item": [
                {"item": entry.item, "available_qty_kg": _num(entry.available_qty_kg)}
                for entry in inventory_by_item
            ],
            "lot_constraints": [
                {
                    "lot_id": lot.lot_id,
                    "item": lot.item,
                    # 🔴 **예약·할당 차감 전 raw 다.** 위 `inventory_by_item` 과 **다른 뜻**
                    #    이라 합산해서 판매가능량을 다시 만들면 안 된다 — 근거 컨텍스트다.
                    "available_qty_kg": _num(lot.available_qty_kg),
                    # 신선도는 **없을 수 있고 음수일 수 있다** — 둘 다 그대로 둔다.
                    # 음수는 *"신선도 기준을 지난 실제 일수"* 라는 사실이고, 0 으로
                    # 접으면 **기준일 당일**과 **닷새 지난 Lot** 이 같은 값이 된다.
                    "remaining_freshness_days": lot.remaining_freshness_days,
                    "effective_freshness_limit_days": freshness_limits.get(lot.lot_id),
                    "grade": lot.grade,
                    "status": lot.status,
                }
                for lot in lots
            ],
            # ★ **묻지 않은 날짜는 안 만든다.** 빈 목록은 *"물어본 날짜가 없다"* 다.
            "supply_capacity_by_date": [
                {
                    "date": row.date.isoformat(),
                    # 🔴 **`None` 이 정상값인 자리다.** 그날 이미 확정된 출고가 하루
                    #    여력을 넘으면(`OUTBOUND_CAPACITY_OVERCOMMITTED`) 확정 공급량을
                    #    낼 수 없다 — 0 으로 접으면 정책·데이터 이상이 «오늘은 더 못
                    #    판다» 는 정상 사실로 보인다.
                    "confirmed_sellable_quantity_kg": _num_or_none(
                        row.confirmed_sellable_quantity_kg
                    ),
                    "freshness_unresolved_inbound_quantity_kg": _num(
                        row.freshness_unresolved_inbound_quantity_kg
                    ),
                    "uncertainties": list(row.uncertainties),
                }
                for row in supply_rows
            ],
            # 🔴 **`None` 이 정상값인 자리다.** 물은 품목·수량이 없거나, 그 물량을 FEFO 로
            #    다 덮지 못하거나, 헐어야 할 Lot 의 입고일·단가를 못 읽었다는 사실이다.
            #    0원으로 메우면 «원가 0원짜리 판매» 가 마진 판정을 통과한다.
            "inventory_cost_basis": (
                None
                if cost_basis is None
                else {
                    "item": cost_basis.item,
                    "quantity_kg": _num(cost_basis.quantity_kg),
                    "amount_krw": _num(cost_basis.amount_krw),
                    "allocation_method": cost_basis.allocation_method,
                    "cost_method": cost_basis.cost_method,
                    "included_components": list(cost_basis.included_components),
                    # ★ 하위 호환용 대표 하나. 계보는 아래 `source_refs` 다.
                    "source_ref": cost_basis.source_ref,
                    "source_refs": list(cost_basis.source_refs),
                    "evidence_grade": cost_basis.evidence_grade,
                }
            ),
            "uncertainties": [],
        },
        "delivery_feasibility": {
            "status": delivery.status,
            # ★ **정책 원값을 옮긴 것이다** (`snapshot.shared_daily_outbound_capacity_kg`).
            #   여기서 계산한 무엇이 아니라 3PL 공용 정책값이고, 근거가 그렇게 말한다.
            "daily_outbound_capacity_kg": _num(delivery.daily_outbound_capacity_kg),
            # ★ **계약 표에서 읽은 값이다** — 문자열을 코드에 박지 않았다
            #   (`transport.resolve_fixed_route`).
            "delivery_route": delivery.delivery_route,
            # 🔴 **준비일과 다른 값이다.** 합치지 않는다 (`tools.TRANSPORT_LEAD_DAYS`).
            #
            # ★ **이름에 단위를 안 붙인다 (WP-4B).** 판매 계약의 정본 이름이
            #   `transport_lead_time` 이다. 단위를 이름에 붙인 종전 표기는 물류가
            #   지은 것이었고, 호환 alias 를 같이 내면 **같은 사실이 두 주소**로
            #   다니게 된다 — 받는 쪽이 어느 것을 볼지 갈린다. 단위는 Evidence 의
            #   `unit="days"` 가 나른다.
            "transport_lead_time": delivery.transport_lead_time,
            "earliest_delivery_date": (
                None
                if delivery.earliest_delivery_date is None
                else delivery.earliest_delivery_date.isoformat()
            ),
            # 업무 사유다 — 축을 못 읽은 것은 아래 uncertainties 다.
            "reason_codes": list(delivery.reason_codes),
            "uncertainties": list(delivery.uncertainties),
        },
        # ★ **없는 판정을 지어내지 않는다.** 판매 사이클의 하드 제약은
        #   `evaluate_sales_rules` 소유인데 그것은 승인 매입 Overlay 를 전제한다.
        "hard_constraints": [],
        # ★ **기존 코드명을 그대로 보존한다.** severity 도 점수도 새로 만들지 않는다.
        "soft_warnings": [
            {"code": code} for code in dict.fromkeys([*business["signals"], *business["warnings"]])
        ],
        "missing_data": list(missing),
        # ★ **ref 를 발명하지 않는다** — Repository 가 스냅샷에 실어 둔 것 그대로다.
        "evidence_refs": list(snapshot.evidence_refs),
    }

    # ── 근거 ─────────────────────────────────────────────────────
    #
    # 🔴 **값도 출처도 그대로다. 바뀐 것은 주소뿐이다 (WP-4).**
    #
    # ```text
    # inventory_by_item[배추].available_qty_kg
    #   → sellable_supply.inventory_by_item[배추].available_qty_kg
    # lot_constraints[LOT-X].*    → sellable_supply.lot_constraints[LOT-X].*
    # shared_daily_outbound_capacity_kg
    #   → delivery_feasibility.daily_outbound_capacity_kg
    # ```
    supply = payload["sellable_supply"]
    evidences = _inventory_by_item_evidences(
        supply["inventory_by_item"], snapshot, prefix="sellable_supply."
    )
    for row in supply["lot_constraints"]:
        base = f"sellable_supply.lot_constraints[{row['lot_id']}]"
        evidences.append(
            _ev(
                f"{base}.available_qty_kg",
                row["available_qty_kg"],
                "kg",
                lots_ref,
                f"{row['item']} · 상태 {row['status']} — Lot 물리 잔량이다. "
                "예약·할당 차감 전이라 판매가능량이 아니다",
            )
        )
        if row["remaining_freshness_days"] is not None:
            evidences.append(
                _ev(
                    f"{base}.remaining_freshness_days",
                    row["remaining_freshness_days"],
                    "days",
                    lots_ref,
                    "유효 보관한계 − 입고 후 경과일. 음수는 한계를 지난 실제 일수다",
                    extra_ref_ids=(policies_ref,) if policies_ref != lots_ref else (),
                )
            )
        if row["effective_freshness_limit_days"] is not None:
            evidences.append(
                _ev(
                    f"{base}.effective_freshness_limit_days",
                    row["effective_freshness_limit_days"],
                    "days",
                    policies_ref,
                    "잔여 신선도를 낸 분모. `중` 등급은 운영 보관한계에 계수가 곱해진 값이라 "
                    "품목 정책 원값과 다를 수 있다",
                )
            )
    for row in supply["supply_capacity_by_date"]:
        base = f"sellable_supply.supply_capacity_by_date[{row['date']}]"
        if row["confirmed_sellable_quantity_kg"] is not None:
            evidences.append(
                _ev(
                    f"{base}.confirmed_sellable_quantity_kg",
                    row["confirmed_sellable_quantity_kg"],
                    "kg",
                    lots_ref,
                    "그날까지 신선한 판매가능 재고와 그날 남은 출고 여력 중 작은 값. "
                    "입고 예정은 신선도가 안 정해져 더하지 않았다",
                    source="tool_calc",
                    extra_ref_ids=(ref,) if ref != lots_ref else (),
                )
            )
        evidences.append(
            _ev(
                f"{base}.freshness_unresolved_inbound_quantity_kg",
                row["freshness_unresolved_inbound_quantity_kg"],
                "kg",
                ref,
                "그날까지 들어올 확정 입고량 — Lot 이 아직 없어 신선도가 정해지지 않았다. "
                "판매 근거로 쓰는 양이 아니다",
                source="tool_calc",
            )
        )
    evidences.append(
        _ev(
            "delivery_feasibility.daily_outbound_capacity_kg",
            outbound_capacity,
            "kg",
            _policy_ref(read.policy, "shared_daily_outbound_capacity_kg", ref),
            f"3PL 공용 일일 출고 여력 정책값 ({read.policy.policy_version}) — "
            "하루에 내보낼 수 있는 총량이지 납기 가능성 판정이 아니다",
            grade="SIM_FIXED",
        )
    )
    if delivery.transport_lead_time is not None:
        evidences.append(
            _ev(
                "delivery_feasibility.transport_lead_time",
                delivery.transport_lead_time,
                "days",
                ref,
                "MVP 확정 가정 — 운송 소요시간의 정본 칸이 스키마에 없어 측정값이 아니다",
                source="tool_calc",
                grade="SIM_FIXED",
            )
        )
    if payload["missing_data"]:
        evidences.append(
            _ev(
                "missing_data",
                len(payload["missing_data"]),
                "name_count",
                ref,
                "이번 회신이 내지 못한 것의 이름 수 — 값이 아니라 세어 본 것이다",
                source="tool_calc",
            )
        )
    if payload["evidence_refs"]:
        evidences.append(
            _ev(
                "evidence_refs",
                len(payload["evidence_refs"]),
                "ref_count",
                ref,
                "이 회신이 읽은 출처의 건수 — 값이 아니라 세어 본 것이다",
                source="tool_calc",
            )
        )

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
        # 판매 승인·거절을 내지 않는다 — 낸 것이 없으니 근거를 요구할 판정도 없다
        judgment_fields=(),
        missing_data=tuple(dict.fromkeys(missing)),
        # 🔴 **숫자·날짜를 적지 않는다** — `check_reasoning` 의 `E-REASONING-NUMERIC` 이
        #    `contributes_to_band` 와 무관하게 돌아 `5,000` 이나 `2026-09-10` 을 잡는다.
        reasoning=_PRE_SALES_REASONING[delivery.status],
    )
    return reply, _meta(request, run_id, tools, reply)


#: 납기 판정별 사유 문장. 🔴 **숫자도 날짜도 안 적는다** (`E-REASONING-NUMERIC`).
_PRE_SALES_REASONING: dict[str, str] = {
    "READY": (
        "현재 판매 가능 재고와 출고 여력을 조회했다. "
        "가장 이른 납기일과 요청 납기일의 출고 여력까지 판정했다."
    ),
    "FAIL": (
        "현재 판매 가능 재고와 출고 여력을 조회했다. "
        "요청한 납기 조건은 준비 리드 또는 하루 출고 여력을 넘어 낼 수 없다."
    ),
    "UNRESOLVED": (
        "현재 판매 가능 재고와 출고 여력을 조회했다. "
        "납기 판정에 필요한 정책 또는 운송 계약을 읽지 못해 납기는 내지 않았다."
    ),
}

#: 물류 내부 어휘 → 마스터가 읽는 사실 이름. 하나가 사라지면 다른 하나도 사라진다.
_DELIVERY_MISSING_NAMES: dict[str, str] = {
    "OUTBOUND_PREP_LEAD_DAYS_UNRESOLVED": "earliest_delivery_date",
    "DELIVERY_ROUTE_UNRESOLVED": "delivery_route",
}


@dataclass(frozen=True)
class _SalesAsk:
    """사용자가 실제로 물은 것. **없는 칸은 `None` 이고 물류가 채우지 않는다.**"""

    item: str | None
    quantity_kg: Decimal | None
    delivery_date: date | None


def _sales_ask(request: AgentRequest) -> _SalesAsk:
    """`user_request` 에서 **온 것만** 읽는다 (`_query_scope` 와 같은 규율).

    🔴 **수량도 날짜도 지어내지 않는다.** 없으면 그 축의 판정을 안 하고, 안 한 것을
       `READY` 로도 `FAIL` 로도 적지 않는다.
    """
    raw = request.payload.get("user_request")
    if not isinstance(raw, Mapping):
        return _SalesAsk(item=None, quantity_kg=None, delivery_date=None)
    item = raw.get("item")
    return _SalesAsk(
        item=item if isinstance(item, str) and item.strip() else None,
        quantity_kg=_ask_quantity(raw.get("requested_quantity_kg")),
        delivery_date=_ask_date(raw.get("preferred_delivery_date")),
    )


def _ask_quantity(value: Any) -> Decimal | None:
    """숫자면 `Decimal`, 아니면 `None`. 🔴 **못 읽은 값을 0 으로 바꾸지 않는다.**"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None
    return None


def _ask_date(value: Any) -> date | None:
    """`date` 거나 ISO 문자열이면 날짜, 아니면 `None`. **오늘로 메우지 않는다.**"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _confirmed_inventory_cost_basis(
    snapshot: InventoryLogisticsSnapshot,
    *,
    asked: _SalesAsk,
    inventory_by_item: Sequence[InventoryByItem],
    supply_rows: Sequence[SupplyByDate],
) -> InventoryCostBasisSnapshot | None:
    """확정 물량에 FEFO 로 배부된 **예상** 재고 취득원가. **없으면 `None` 이다.**

    ```text
    덮을 물량 = min(사용자가 물은 수량, 그 날짜(또는 현재)의 확정 판매가능량)
    ```

    🔴 **판매가능량을 여기서 다시 셈하지 않는다.** 위에서 이미 낸
       `inventory_by_item` · `supply_capacity_by_date` 를 그대로 읽는다 — 같은 회신
       안에서 *"팔 수 있다고 답한 양"* 과 *"원가를 배부한 양"* 이 갈리면 그 회신은
       스스로 모순된다.

    ★ 수량을 안 물었으면 확정 판매가능량 전체가 대상이다 — 판매가 사람 없는 자동
      걷기에서 그 값을 그대로 제안 수량으로 쓴다 (`proposal._confirmed_sellable_qty`).
    """
    if asked.item is None:
        return None
    if asked.delivery_date is not None:
        row = next((row for row in supply_rows if row.date == asked.delivery_date), None)
        confirmed = None if row is None else row.confirmed_sellable_quantity_kg
    else:
        entry = next((entry for entry in inventory_by_item if entry.item == asked.item), None)
        confirmed = None if entry is None else entry.available_qty_kg
    if confirmed is None:
        return None
    quantity = confirmed if asked.quantity_kg is None else min(asked.quantity_kg, confirmed)
    return fefo_inventory_cost_basis(snapshot, item=asked.item, quantity_kg=quantity)


def _delivery_input_error(
    request: AgentRequest, run_id: str, tools: Sequence[str]
) -> tuple[AgentReply, ExecutionMetadata]:
    """납기 입력 조회의 **실행 실패** — `RUNTIME_NOT_READY` 가 아니다.

    `_snapshot_error` 와 같은 판단이다 (M-1 §5.1): 데이터 부재가 아니라 다시 부르면
    성공할 수 있는 쪽이라 마스터가 재시도할 수 있어야 한다. 예외 원문은 싣지 않는다 —
    숫자가 섞이면 `E-REASONING-NUMERIC` 에 걸린다.
    """
    reply = AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=_AGENT,
        mode=request.mode,
        run_id=run_id,
        runtime_status="ERROR",
        business_status="skipped",
        payload={"failed_operation": "load_delivery_inputs"},
        reasoning=(
            "납기 판정 입력 조회가 실행 오류로 실패했다 — "
            "데이터 부재가 아니라 재시도 가치가 있는 실패다."
        ),
    )
    return reply, _meta(request, run_id, tools, reply)


#: 운송 계약 조회가 **실행 오류**로 끝났다는 표시. 🔴 `None` 을 안 쓴다 —
#: `None` 은 *"계약 행이 없다"* 라는 정상 사실이고 이것은 *"못 읽었다"* 다.
def _query_scope(request: AgentRequest, as_of: date) -> dict[str, Any]:
    """이 회신이 **무엇을 기준으로 답했나.** 마스터가 보낸 것만 읽는다.

    ★ **추론하지 않는다.** 마스터가 ②에 싣는 것은 사용자 조건 그대로이고
      (`sales_flow._context_input`), 거기 없는 것은 물류도 모른다.
      `delivery_window_start/end` 도 `max_confirmed_sellable_quantity_kg` 도 만들지
      않는다 — 특히 뒤엣것은 판매가 **쓰지 않기로 못박은** 값이다
      (`test_delivery_date_uses_exact_logistics_vector_not_query_scope_max`).

    ★ 품목이 없으면 **칸을 만들지 않는다.** `item: None` 을 실으면 받는 쪽이
      *"품목 지정이 없었다"* 와 *"물류가 안 읽었다"* 를 구별할 수 없다 (§1.2-10).
    """
    scope: dict[str, Any] = {"as_of": as_of.isoformat()}
    user_request = request.payload.get("user_request")
    if isinstance(user_request, Mapping):
        item = user_request.get("item")
        if isinstance(item, str) and item.strip():
            scope["item"] = item
    return scope


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
    # ★ 해석 서비스는 **설정만 읽는다** — 여기서 네트워크가 열리지 않는다 (#385).
    #   opt-in 이 없으면 enabled=False 인 서비스가 온다. 못 낸 회신에도 이 서비스가
    #   상태 어휘(DISABLED · SKIPPED_TEMPLATE)를 정한다 — 어댑터가 하드코딩하지 않는다.
    llm = master_interpretation_service()

    proposal = _as_proposal(request.payload)
    if proposal is None:
        return _not_ready(
            request,
            run_id,
            [],
            missing=("purchase_proposal",),
            reason="매입 제안을 물류 입력 모델로 되살리지 못했다",
            llm=uncalled_interpretation(llm),
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
        return reply, _meta(request, run_id, [], llm=uncalled_interpretation(llm))

    try:
        read = _load_read(as_of=as_of, sim_run_id=request.context.sim_run_id)
    except _SnapshotLoadError:
        return _snapshot_error(request, run_id, tools, llm=uncalled_interpretation(llm))
    snapshot = read.snapshot if read is not None else None
    if snapshot is None:
        return _not_ready(
            request,
            run_id,
            tools,
            missing=("logistics_snapshot",),
            reason="물류 스냅샷을 읽지 못했다",
            llm=uncalled_interpretation(llm),
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

    # ── 해석 (LLM) — 결정론 결과가 **다 선 뒤에만** 돈다 (#385) ─────────────
    #
    # ★ 새 계산이 없다. signals · measurements 는 위 `evaluate_procurement_business_signals`
    #   가 낸 것이고, preferred 는 `derive_preferred_adjustment` 가 정한 것이다. 조립기는
    #   독립 Service 와 같은 함수다 — Context 에 실리는 것은 signal 코드 · 판정 수치의 확정
    #   표기 · 허용/우선 조정 · 번역된 미확정 이름뿐이고, Lot · 날짜 · kg · 거래처 · 이
    #   payload 는 넘어가지 않는다.
    # ★ missing 원재료는 독립 Service `_missing_data` 와 같은 모집단이다 — 비-PASS 하드 제약
    #   코드 + Rule 경고 + 판정 스킵 사실. M-1 `missing_data`(`logistics_rule/LOG-H02` 같은
    #   네임스페이스 이름)를 넘기지 않는다 — 숫자가 든 채로 무숫자 경계를 우회한다.
    # 🔴 LLM 은 아래 어느 값도 바꾸지 않는다 — verdict · evidences · suggested_adjustments ·
    #    preferred_adjustment · missing 은 이 블록 앞에서 이미 확정됐고, 해석은 payload 의
    #    **별도 중첩 칸** 하나에만 실린다. 실패 · timeout · 검증 탈락은 Template 로 접힌다.
    llm_context, facts_incomplete = build_sanitized_context(
        cycle="PROCUREMENT",
        signals=business["signals"],
        measurements=business["measurements"],
        preferred_adjustment=preferred,
        missing_data=[
            *(c.code for c in rules["hard_constraints"] if c.status != "PASS"),
            *rules["soft_warnings"],
            *business["warnings"],
        ],
    )
    llm_result = llm.interpret(
        llm_context,
        runtime_ready=rules["runtime_status"] == "READY",
        # FAIL 만 차단한다 — UNRESOLVED 는 호출을 막지 않는다 (독립 경로와 같은 17-A).
        has_blocking_constraints=any(c.status == "FAIL" for c in rules["hard_constraints"]),
        facts_incomplete=facts_incomplete,
    )
    # 사람이 읽는 해석 — summary · risks · suggested_adjustment. 봉투 검증은 중첩 Mapping 의
    # 값에 근거를 요구하지 않고(`required_claims`), 마스터는 부서 payload 를 파싱하지 않는다.
    payload["interpretation"] = llm_result.interpretation.model_dump(mode="json")

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
    # 🔴 **두 단계로 나눈다** (#209 · 되먹임 ④). 전에는 중복 키를 만나면 그 자리에서
    #   `continue` 해서 **두 번째 시나리오의 라벨이 사라졌다.** 같은 조정이 세 안에서
    #   나와도 첫 라벨 하나만 손에 남는다. 키별로 라벨을 먼저 모으고, 다 모은 뒤
    #   표준형을 만든다. 중복 제거의 뜻(같은 key 는 하나)은 그대로이고 라벨만 합친다.
    collected: dict[tuple[str, date, float], dict[str, Any]] = {}
    for result in scenario["scenario_results"]:
        # ★ reject 안의 adjustment 는 승격하지 않는다 (#121 2단계). multi-split 에서
        #   앞 회차의 조정이 남은 채 전체가 reject 될 수 있는데, 구제 불가 판정한 안의
        #   조정을 행동 제안으로 내보내면 "reject 는 조정으로 구제 불가"와 모순된다.
        #   진단 기록은 payload.scenario_results 에 그대로 남는다 — 사실이 사라지는
        #   것이 아니라 제안으로 격상되지 않을 뿐이다. needs_followup 도 이에 따라
        #   reject 만으로는 서지 않는다. 라벨 수집 대상도 아니다.
        if result.verdict == "reject":
            continue
        for adjustment in result.adjustments:
            if adjustment.axis == "quantity" and adjustment.suggested_qty_kg is not None:
                target, unit = _num(adjustment.suggested_qty_kg), "kg"
                what = f"수량을 {target:g}kg 로 조정 제안"
            elif adjustment.axis == "timing" and adjustment.suggested_arrival_date is not None:
                # 날짜는 float 로 실을 수 없어 as_of 기준 D+N 으로 옮긴다.
                # ⚠️ 전에 이 자리에 "손실 없는 표기 변환" 이라고 적어 뒀는데 **틀렸다**
                #   (#209). 사람에게는 손실이 없지만 **기계가 읽을 수 있는 형태로는
                #   손실**이다 — 목표 날짜가 reason 문자열 안에만 남는다. 그래서 목표
                #   도착일은 아래 reason 에 그대로 남긴다. 절대 날짜 칸이 생기면
                #   그때 문장에서도 뺀다 (마스터가 정해 통보).
                target = float((adjustment.suggested_arrival_date - as_of).days)
                unit = "d"
                what = f"도착일을 {adjustment.suggested_arrival_date.isoformat()} 로 조정 제안"
            else:
                # 값 없는 제안은 전용 채널로 못 옮긴다 — payload.scenario_results 에는
                # 그대로 남아 있어 사실이 사라지지는 않는다.
                continue
            key = (adjustment.axis, adjustment.split_date, target)
            entry = collected.get(key)
            if entry is None:
                # dict 는 삽입 순서를 보존하므로 시나리오 등장 순서가 그대로 남는다.
                collected[key] = {
                    "axis": adjustment.axis,
                    "split_date": adjustment.split_date,
                    "target": target,
                    "unit": unit,
                    "what": what,
                    "labels": [result.label],
                }
            elif result.label not in entry["labels"]:
                entry["labels"].append(result.label)

    # ★ `reason` 에서 라벨·회차 앞머리를 뺐다 (미결 §0-6 갈래 ㄱ · 2026-09-03 마스터
    #   통보). 같은 사실이 칸과 문장 두 곳에 있으면 한쪽만 고쳐지는 날이 오고, 그 날이
    #   이미 왔다 — 화면(`master/answer.py:295`)은 칸을 읽게 고쳐졌는데 물류가 안 채워
    #   문장에만 남아 "N 회차" 가 한 번도 안 떴다.
    #   ⚠️ **빼는 것은 라벨과 대상 회차뿐이다.** 목표 도착일은 `what` 안에 남긴다.
    suggested: list[SuggestedAdjustment] = [
        SuggestedAdjustment(
            dept="inventory",
            axis=entry["axis"],
            target_value=entry["target"],
            unit=entry["unit"],
            reason=entry["what"],
            ref_ids=(ref,),
            scenario_labels=tuple(entry["labels"]),
            split_date=entry["split_date"],
        )
        for entry in collected.values()
    ]

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
    return reply, _meta(request, run_id, tools, reply, llm=llm_result)


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


def _no_run_axis(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """봉투에 실행 축이 안 실려 왔다 — **`ERROR` 가 아니다** (#345).

    🔴 **`ERROR` 로 접으면 두 실패가 한 이름이 된다.** `_snapshot_error` 가 말하는 것은
       *"DB 가 깨졌으니 다시 불러 봐라"* 인데(`worth_retry`), 값을 못 받은 것은 다시
       불러도 같다. 재시도하면 호출 예산만 탄다 (M-1 §5.1 · 정의서 §1.2-12).
       `failed_operation` 을 실을 수도 없다 — **조회를 시도조차 안 했다.**

    ★ **예외로 올리지 않는다.** 올리면 `MasterRunner._invoke` 가 잡아 `error_reply` 로
      바꾸므로 결국 `ERROR` 가 되고, 예외 원문이 `reasoning` 에 실려
      `E-REASONING-NUMERIC` 함정까지 같이 온다 — 부서 회신은 값으로 답한다.

    ★ **이름은 `sim_run_id` 하나다.** `logistics_runtime_fixture` 를 같이 싣지 않는다 —
      fixture 는 **없는 것이 아니라 어느 것인지 못 고르는 것**이고, 그 이름은 진짜
      부재(`_status_query` · `_pre_purchase`)가 이미 쓰고 있어 섞으면 마스터가 두 상황을
      못 가린다.

    ★ `tools` 가 비는 것은 사실이다 — **Tool 을 하나도 안 돌렸다.**
    """
    run_id = _run_id(request)
    return _not_ready(
        request,
        run_id,
        [],
        missing=("sim_run_id",),
        reason="어느 실행의 물류 장부인지 확인할 수 없다",
    )


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


class _SnapshotLoadError(Exception):
    """스냅샷 조회가 **실행 오류**로 실패했다 — 데이터 부재(LookupError)와 다르다."""


def _load_read(*, as_of: date, sim_run_id: str) -> LogisticsRead | None:
    """부재만 None 으로, 실행 오류는 구분해 올린다 (#121 4단계).

    🔴 **`sim_run_id` 는 봉투에서 온다. 여기서 지어내지 않는다** (#345).
       *"어느 실행의 장부인가"* 는 물류 사실이 아니라 마스터가 소유한 값이다
       (`master/envelope.ExecutionContext`). 그래서 `BURN_IN_SIM_RUN_ID` 로 메우지도,
       최신 실행을 고르지도, DB 에서 되짚지도 않는다 — 그 전부가 fail-open 이다.

    ★ **선택 인자로 두지 않았다.** 어댑터 경로에는 값이 없는 경우가 없다 —
      `logistics_port` 의 문이 이미 막는다(`_no_run_axis`). 여기에 `None` 을 남기면
      *"안 주면 조용히 넓어지는"* 자리가 다시 생기고, 그건 이 이슈가 닫은 자리다.
      Repository 쪽 `sim_run_id=None` 은 **독립 Service 경로 때문에 남는 것**이지
      어댑터 때문이 아니다 (`service._get_snapshot_or_none` — 별도 안건).

    Repository 예외 계약 전수 확인(2026-09-01) — 정상적인 "데이터 없음/미확정"은
    **LookupError 둘**이다(runtime fixture 0건 · 필수 정책 미등재). 독립 Service
    경로(`service._get_snapshot_or_none`)가 같은 기준선이다.

    그 외 — ValueError/TypeError(데이터는 있는데 모양이 깨졌거나 활성 fixture 가
    중복인 무결성 위반), RuntimeError(env 부재), psycopg 오류(DB 장애) — 는 회사
    상태가 아니라 **실행 실패**다. 종전처럼 RUNTIME_NOT_READY 로 뭉개면 마스터가
    재시도하지 않고 "데이터를 달라"로 오독한다 (M-1 §5.1 — ERROR 가 재시도 가치가
    있는 쪽).

    ★ **알려져 있던 예외 하나는 닫혔다** (#366) — `item_storage_policies.
      operational_limit_days` 는 DB 가 nullable 인데 `_inventory_lot_from_row` 만
      NULL 을 TypeError 로 냈다. 그 NULL 은 "보관한계 미등록"(부재)이라 여기서
      ERROR 로 분류되면 재시도해도 풀리지 않는다.

      🔴 **어댑터 특례로 막지 않았다.** 여기서 그 TypeError 만 걸러내면 *"어떤
         TypeError 는 상태"* 라는 예외의 예외가 생기고, 같은 NULL 을 읽는 독립
         Service 경로는 그대로 죽는다. 고친 자리는 Repository 어휘 한 곳이고,
         이제 부재는 `remaining_freshness_days=None` 으로 나와 `evaluate_sales_rules`
         의 `N17-LOT`(`N17_LOT_FRESHNESS_UNRESOLVED`)이 받는다 — **없는 사실은
         UNRESOLVED 로, 실행 실패만 ERROR 로** 라는 이 함수의 구분이 그대로 산다.
    """
    try:
        return get_current_logistics_read(as_of=as_of, sim_run_id=sim_run_id)
    except LookupError:
        return None  # 없는 것은 예외가 아니라 상태다
    except Exception as error:
        # 원문 메시지는 로그로만 남긴다 — reasoning 에 그대로 실으면 숫자가 섞여
        # E-REASONING-NUMERIC 에 걸린다 (2026-09-01 재무 400 회신에서 실측된 함정).
        logger.exception("Logistics read failed (as_of=%s, sim_run_id=%s)", as_of, sim_run_id)
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


def _num_or_none(value: Decimal | float | None) -> float | None:
    """`None` 은 `None` 으로 둔다. 🔴 **0 으로 바꾸지 않는다** (§1.2-10).

    ★ `_num` 과 나눠 두는 이유는 «있어야 하는 값» 과 «없을 수 있는 값» 을 호출부에서
      구분하기 위해서다 — `_num(None)` 이 `TypeError` 로 죽는 것이 옳고, null 이
      정상인 자리만 이 함수를 쓴다.
    """
    return None if value is None else float(value)


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
    *,
    prefix: str = "",
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
            f"{prefix}inventory_by_item[{row['item']}].available_qty_kg",
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


def _meta(
    request: AgentRequest,
    run_id: str,
    tools: Sequence[str],
    reply: AgentReply | None = None,
    *,
    llm: InterpretationResult | None = None,
) -> ExecutionMetadata:
    """실행 흔적. **Business Reply 와 섞지 않는다.**

    `reply` 를 받으면 `runtime_status == "READY"` 일 때만 DeptMeta 관측을 붙인다.
    ★ **못 낸 회신에 관측을 달지 않는다** — *"안 돌았는데 무엇을 읽었다"* 가 된다.

    `llm` 은 해석 서비스가 낸 결과다 (#385). 받으면 그 상태를 그대로 적고, 안 받으면
    **그 mode 에 LLM 경로가 없다**는 뜻이라 `DISABLED` 다 — 지금은 `SCENARIO_VALIDATION`
    만 준다. 어댑터가 상태 어휘를 지어내지 않는다.

    `ExecutionMetadata` 는 LLM 칸이 넷뿐이라(status · model · attempts · fallback)
    provider · error_kind · Provider latency · Context fact 는 여기서 유실됐다.
    공통 봉투를 넓히는 대신 `inventory_llm_trace` 관측 하나로 나른다 (#402).
    ★ `elapsed_ms` 는 **건드리지 않는다** — 그 칸은 Agent 전체 실행시간이고(재무가
      채운다) LLM 시간을 넣으면 한 컬럼에 비교 불가능한 두 값이 섞인다.
    """
    observations: list[dict[str, Any]] = []
    if reply is not None and reply.runtime_status == "READY":
        dept_meta = _inventory_dept_meta(request.mode, reply.payload, tools)
        if dept_meta is not None:
            observations.append(dept_meta)
    # ★ 맨 뒤에 붙인다 — 앞자리는 DeptMeta 가 쓰던 자리이고 읽는 쪽이 그것을 전제로
    #   붙어 있다 (재무가 harness trace 를 뒤에 붙인 것과 같은 판단).
    # ★ `reply` 의 READY 를 다시 묻지 않는다. Gate 가 `runtime_ready=True` 일 때만
    #   호출을 허용하므로 attempts>0 이면 이미 READY 다 — 같은 조건을 두 번 적으면
    #   한쪽이 낡는 날 둘이 어긋난다.
    llm_trace = _inventory_llm_trace(llm)
    if llm_trace is not None:
        observations.append(llm_trace)
    return ExecutionMetadata(
        run_id=run_id,
        request_id=request.context.request_id,
        agent=_AGENT,
        used_tools=tuple(tools),
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status=llm.llm_status if llm is not None else "DISABLED",
        llm_model=(llm.llm_model or "") if llm is not None else "",
        llm_attempts=llm.llm_attempts if llm is not None else 0,
        llm_fallback_used=llm.llm_fallback_used if llm is not None else False,
        observations=tuple(json.dumps(o, default=str, sort_keys=True) for o in observations),
    )


def _snapshot_error(
    request: AgentRequest,
    run_id: str,
    tools: Sequence[str],
    *,
    llm: InterpretationResult | None = None,
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
    return reply, _meta(request, run_id, tools, reply, llm=llm)


def _not_ready(
    request: AgentRequest,
    run_id: str,
    tools: Sequence[str],
    *,
    missing: tuple[str, ...],
    reason: str,
    llm: InterpretationResult | None = None,
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
    return reply, _meta(request, run_id, tools, reply, llm=llm)
