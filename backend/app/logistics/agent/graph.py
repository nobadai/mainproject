"""LangGraph 조사 Runtime — Exception 하나를 읽고 Tool 로 사실을 모아 조사 결과를 낸다.

```text
START
  ↓
load_exception     결정론  그날 살아 있던 Exception 을 찾는다. 없으면 NOT_FOUND 로 끝
  ↓
load_context       결정론  창고 전역 정책을 먼저 읽는다 (품목을 몰라도 읽을 수 있다)
  ↓
preload_subject    결정론  LOT      → get_lot · get_sales_commitments
                           WAREHOUSE→ get_capacity_context · get_inbound_schedule
  ↓
plan_step ◀──────────────────────────┐  LLM · 다음 한 수를 고른다 (정확히 하나)
  ↓                                   │
guard              결정론  허용·인자·범위·중복·예산 → 통과/거부
  ├─ 통과 ─────────▶ execute_tool ───┘  결정론  Tool 실행 → observations
  ├─ FINISH ───────▶ finalize            LLM · 조사 정리
  ├─ 거부(예산 남음)  ───────────────────┘  재계획
  └─ 거부(예산 소진) ▶ fallback_rule     결정론  code 별 규칙 제안 하나
                          ↓
finalize ──────────▶ evaluate_options  결정론  카탈로그·근거 검증 · 숫자는 Tool 로 덮는다
                          ↓
                      finish            결정론  결과 조립 · 관측일 셈
                          ↓
                        END
```

🔴 **이번 Commit 에 `persist` 가 없다.** Proposal 저장·사람 승인은 Commit 5 의 몫이고,
   여기서는 DB 에 **아무것도 쓰지 않는다.** 마지막 노드 이름이 `finish` 인 것이 그
   범위를 그대로 말한다 (설계 문서 §8 의 TARGET 은 `persist` 로 끝난다 — CURRENT 와
   TARGET 의 차이다).

🔴 **숫자의 주인은 Tool 이다.** 모델이 `summary` 에 «500kg» 이라고 적어도 그 값은 어떤
   권위 있는 칸으로도 올라가지 않는다. 올라가는 숫자는 `ToolCallRecord.answer` 와
   `EvaluatedOption.impact` 뿐이다.

⚠️ **걷기(scheduler)에 연결하지 않는다** (§30). 하루 걷기는 Observe → Detect 결정론이어야
   하고, 조사는 사람이 부를 때만 돈다.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.logistics.agent.investigation import (
    ACTION_DECISION_OWNERS,
    DEADLINE_EXCEEDED,
    DEFAULT_BUDGET,
    EXCEPTION_NOT_FOUND,
    EXCEPTION_STATUS_UNRESOLVED,
    REPLAN_BUDGET_EXCEEDED,
    TOOL_BUDGET_EXCEEDED,
    AgentLLMBudgetExceeded,
    AgentLLMDisabled,
    EvaluatedOption,
    FinalizeFn,
    FinishReason,
    InvestigationBudget,
    InvestigationOption,
    InvestigationReport,
    InvestigationRequest,
    InvestigationResult,
    InvestigationState,
    InvestigationStep,
    InvestigationView,
    PlanFn,
    ToolCallRecord,
    ToolCallStatus,
    collect_ids,
    jsonable,
)
from app.logistics.agent.schemas import (
    CAPACITY_PRESSURE,
    FRESHNESS_PRESSURE,
    derive_observed_as_of,
)
from app.logistics.agent.tool_dispatch import call_key, guard_step, run_tool
from app.logistics.agent.tools import SUPPORTED_ACTIONS, ActionImpact
from app.logistics.llm.runtime import classify_llm_error
from app.logistics.llm.schemas import LLMStatus

__all__ = [
    "build_investigation_graph",
    "run_investigation",
]

#: 범위를 넓히는 식별자 칸 이름. Tool 답 안에 **실제로 실린 것만** 센다 (§17).
_SCOPE_LOT_FIELDS = ("lot_id",)
_SCOPE_ITEM_FIELDS = ("item_id",)

# ══════════════════════════════════════════════════════════════════════════
#  시간
# ══════════════════════════════════════════════════════════════════════════


def _remaining(state: InvestigationState) -> float:
    """마감까지 **남은 초**. 무한대면 마감이 없다는 뜻이다. **단조시계**를 쓴다.

    🔴 이 값이 `0` 이하면 **새 실행을 시작하지 않는다.** 이미 도는 호출을 끊지는
       못한다 — 그 한계는 §8.5 에 그대로 적어 뒀다. 숨기지 않는다.
    """
    clock = state.get("clock") or time.monotonic
    deadline = state.get("deadline_at")
    if deadline is None:
        return float("inf")
    return deadline - clock()


def _expired(state: InvestigationState) -> bool:
    """마감을 넘겼나."""
    return _remaining(state) <= 0


# ══════════════════════════════════════════════════════════════════════════
#  저울 — Tool 이 실제로 도는 **유일한** 자리
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class _Ledger:
    """이번 조사의 **실제 Tool 실행량**을 드는 하나뿐인 저울 (v1.1 신설).

    🔴 **노드마다 따로 세면 반드시 한 군데가 빠진다.** 실제로 그랬다 — 예산이 «모델이
       고른 성공 호출» 만 세는 바람에 선행 조회 4회와 후보 검증 n회가 계약 밖에서
       돌았고, *"Tool 최대 8회"* 가 실제로는 14회였다.

    ```text
    세는 것   Tool 함수에 **들어갔다**            SUCCESS · FAILED   예산 −1
    안 세는 것 guard/예산/마감이 막아 못 들어갔다  REJECTED           예산 0
    ```

    ★ 실패도 센다. 들어간 순간 DB 왕복이 일어났고, 안 세면 터지는 호출을 무한히
      반복하는 경로가 열린다.

    ⚠️ 여기를 **우회해서** `run_tool` 을 직접 부르는 자리를 만들지 마라 — 그 순간
       계약이 다시 거짓이 된다 (`test_logistics_agent_graph` 가 소스로 막는다).
    """

    state: InvestigationState
    records: list[ToolCallRecord]
    sequence: int
    spent: int
    planned: int
    keys: set[str]
    lots: set[str]
    items: set[str]

    @classmethod
    def of(cls, state: InvestigationState) -> _Ledger:
        return cls(
            state=state,
            records=list(state.get("observations") or ()),
            sequence=int(state.get("sequence") or 0),
            spent=int(state.get("tool_call_count") or 0),
            planned=int(state.get("planned_tool_call_count") or 0),
            keys=set(state.get("executed_keys") or ()),
            lots=set(state.get("allowed_lot_ids") or ()),
            items=set(state.get("allowed_item_ids") or ()),
        )

    @property
    def budget_left(self) -> int:
        """남은 실행 횟수."""
        return self.state["request"].budget.max_tool_calls - self.spent

    def blocked(self) -> str | None:
        """지금 새 실행을 시작해도 되나. 안 되면 그 사유."""
        budget = self.state["request"].budget
        if self.spent >= budget.max_tool_calls:
            return f"{TOOL_BUDGET_EXCEEDED}:{budget.max_tool_calls}"
        if _remaining(self.state) <= 0:
            return f"{DEADLINE_EXCEEDED}:{budget.timeout_seconds:g}s"
        return None

    def run(
        self,
        tool_name: str,
        *,
        arguments: Mapping[str, Any],
        reason: str,
        planned: bool = False,
        key: str | None = None,
    ) -> Any:
        """Tool 하나를 돌린다 — **예산 · 마감 · 기록이 전부 여기 한 곳에 있다.**

        :param planned: 모델이 스스로 고른 호출인가 (통계용). 예산은 똑같이 쓴다.
        :returns: 성공하면 Tool 답, 막히거나 터지면 `None`.
        """
        request: InvestigationRequest = self.state["request"]
        self.sequence += 1
        stopped = self.blocked()
        if stopped is not None:
            # 🔴 Tool 함수에 **안 들어갔다** — 예산을 안 쓴다.
            self.records.append(
                ToolCallRecord(
                    sequence=self.sequence,
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    status=ToolCallStatus.REJECTED,
                    detail=stopped,
                    reason=reason,
                )
            )
            return None

        # 🔴 **들어가기 전에 센다.** 터져도 실행량은 쓴 것이다.
        self.spent += 1
        if planned:
            self.planned += 1
        try:
            answer = run_tool(
                self.state["conn"],
                tool_name=tool_name,
                arguments=arguments,
                sim_run_id=request.sim_run_id,
                as_of=request.as_of,
            )
        except Exception as error:  # noqa: BLE001 - Tool 실패가 조사를 멈추면 안 된다
            self.records.append(
                ToolCallRecord(
                    sequence=self.sequence,
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    status=ToolCallStatus.FAILED,
                    detail=type(error).__name__,
                    reason=reason,
                )
            )
            return None

        self.records.append(
            ToolCallRecord(
                sequence=self.sequence,
                tool_name=tool_name,
                arguments=dict(arguments),
                status=ToolCallStatus.SUCCESS,
                answer=answer,
                observed_as_of=getattr(answer, "observed_as_of", None),
                uncertainties=tuple(getattr(answer, "uncertainties", ()) or ()),
                reason=reason,
            )
        )
        # ★ 중복 판정 등록은 **성공한 실행만** — 실패한 호출은 다시 걸 수 있어야 한다.
        self.keys.add(key or call_key(tool_name, arguments))
        self.lots |= collect_ids(answer, field_names=_SCOPE_LOT_FIELDS)
        self.items |= collect_ids(answer, field_names=_SCOPE_ITEM_FIELDS)
        return answer

    @property
    def last(self) -> ToolCallRecord | None:
        return self.records[-1] if self.records else None

    def freeze(self) -> dict[str, Any]:
        """상태로 되돌릴 칸들. 노드는 이걸 펴서 반환한다."""
        return {
            "observations": tuple(self.records),
            "sequence": self.sequence,
            "tool_call_count": self.spent,
            "planned_tool_call_count": self.planned,
            "executed_keys": frozenset(self.keys),
            "allowed_lot_ids": frozenset(self.lots),
            "allowed_item_ids": frozenset(self.items),
        }


# ══════════════════════════════════════════════════════════════════════════
#  결정론 노드 ①②③
# ══════════════════════════════════════════════════════════════════════════


def load_exception(state: InvestigationState) -> dict[str, Any]:
    """🔴 **첫 노드는 반드시 결정론이다.** 모델에게 *"이 Exception 이 있을까"* 를 묻지 않는다.

    그날 살아 있던 목록(`get_open_exceptions`)에서 요청한 id 를 찾는다. 없으면 조사
    자체가 성립하지 않으므로 `NOT_FOUND` 로 즉시 끝낸다.

    ★ 이 노드만 Exception 을 안다. Interactive 조사(§3.2)는 같은 골격에서 **이 노드만**
      «질문·범위» 를 읽는 노드로 갈아 끼우면 된다 — 두 벌로 만들지 않는다.
    """
    request: InvestigationRequest = state["request"]
    ledger = _Ledger.of(state)
    # 🔴 **이것도 Tool 실행이다.** 예산을 쓰고, 중복 판정에도 등록된다 (v1.1 정정) —
    #    안 하면 모델이 곧바로 `get_open_exceptions {}` 를 다시 물어 한 번 더 돈다.
    answer = ledger.run(
        "get_open_exceptions",
        arguments={},
        reason="조사의 출발점 — 결정론으로 연다",
    )
    if answer is None:
        # 🔴 **«없다» 와 «못 봤다» 를 가른다.** 목록을 못 읽은 것을 `NOT_FOUND` 로 적으면
        #    *"그날 그 Exception 이 없었다"* 는 **거짓 사실**이 결과에 남는다.
        last = ledger.last
        detail = (last.detail if last else "") or ""
        if detail.startswith(DEADLINE_EXCEEDED):
            stopped = FinishReason.TIMEOUT
        elif detail.startswith(TOOL_BUDGET_EXCEEDED):
            stopped = FinishReason.BUDGET_EXCEEDED
        else:
            # Tool 이 터졌다 — 목록을 못 봤으니 «없다» 고 말할 수 없다.
            stopped = FinishReason.NOT_FOUND
        return {
            **ledger.freeze(),
            "finish_reason": stopped,
            "llm_status": "SKIPPED_TEMPLATE",
            "uncertainties": (
                (f"{EXCEPTION_NOT_FOUND}:{request.exception_id}",)
                if stopped is FinishReason.NOT_FOUND
                else (detail,)
            ),
        }

    found = next(
        (fact for fact in answer.exceptions if fact.exception_id == request.exception_id), None
    )
    if found is None:
        return {
            **ledger.freeze(),
            "finish_reason": FinishReason.NOT_FOUND,
            # 해석할 것이 없었다 — 공급자를 부르지 않았다는 뜻이다.
            "llm_status": "SKIPPED_TEMPLATE",
            "uncertainties": (f"{EXCEPTION_NOT_FOUND}:{request.exception_id}",),
        }

    uncertainties: tuple[str, ...] = ()
    if found.status != "OPEN":
        # 그날 무엇이었는지 못 댄다 — `PROPOSED` 로 넘어간 날을 적는 칸이 없다(§26).
        # 조사는 계속하되 **모른다는 사실을 남긴다.**
        uncertainties = (f"{EXCEPTION_STATUS_UNRESOLVED}:{found.exception_id}",)

    frozen = ledger.freeze()
    if found.subject_type == "LOT":
        frozen["allowed_lot_ids"] = frozenset({*frozen["allowed_lot_ids"], found.subject_id})
    return {
        **frozen,
        "exception": found,
        "subject_type": found.subject_type,
        "subject_id": found.subject_id,
        "uncertainties": uncertainties,
    }


def load_context(state: InvestigationState) -> dict[str, Any]:
    """창고 **전역 정책**을 먼저 읽는다 — 품목을 몰라도 읽을 수 있는 문맥이다.

    ⚠️ 품목별 정책은 `preload_subject` 가 Lot 의 품목을 알아낸 뒤에야 의미가 있다.
       여기서 미리 8개 Tool 을 다 돌지 않는다 (§11) — 필요한 최소 사실만 연다.
    """
    return _preload(state, [("get_policy", {})])


def preload_subject(state: InvestigationState) -> dict[str, Any]:
    """subject 종류에 따라 **최소 사실 두 가지**를 미리 연다.

    ```text
    LOT        get_lot(대상 Lot)  →  get_sales_commitments(그 Lot 의 품목)
    WAREHOUSE  get_capacity_context()  ·  get_inbound_schedule()
    ```

    🔴 **모델을 부르기 전에 한다.** Exception 종류조차 모르는 상태로 모델에게 Tool 을
       찍게 하면 예산이 탐색에만 녹는다.

    ★ 여기서 읽은 것이 조사 **범위의 씨앗**이다 — `get_lot` 이 품목을 알려 주면 그
      품목이 열리고, `get_inbound_schedule` 이 품목을 알려 주면 그것들이 열린다.
    """
    if state.get("subject_type") == "LOT":
        subject_id = state.get("subject_id") or ""
        first = _preload(state, [("get_lot", {"lot_id": subject_id})])
        item_id = _subject_item_id(first.get("observations") or ())
        if not item_id:
            # Lot 을 못 읽었다 — 품목을 모르니 약정도 못 연다. 그 사실만 남기고 넘어간다.
            return first
        # ★ `_preload` 는 **누적**해서 돌려준다. 그래서 앞 결과를 합친 상태를 넘기고
        #   나온 것을 그대로 쓴다 — 두 번 이어 붙이면 같은 관찰이 두 줄로 남는다.
        return _preload({**state, **first}, [("get_sales_commitments", {"item_id": item_id})])
    return _preload(state, [("get_capacity_context", {}), ("get_inbound_schedule", {})])


def _subject_item_id(records: Sequence[ToolCallRecord]) -> str | None:
    for record in records:
        lot = getattr(record.answer, "lot", None)
        if lot is not None and getattr(lot, "item_id", None):
            return str(lot.item_id)
    return None


def _preload(
    state: InvestigationState, calls: Sequence[tuple[str, Mapping[str, Any]]]
) -> dict[str, Any]:
    """결정론 선행 호출들. 🔴 **이것도 예산을 쓴다** (v1.1 정정).

    예산 8회는 *"이번 조사에서 실제로 돈 Tool"* 을 세는 수다. 선행 조회를 빼고 세면
    계약이 «Tool 최대 8회» 인데 실제로는 12회가 도는 일이 생긴다.

    ⚠️ Tool 예외는 **조사를 멈추지 않는다** — 관찰에 실패로 적고 계속한다. 그 사실이
       없어지는 것보다 «못 읽었다» 가 남는 편이 낫다. 다만 실행량은 쓴 것으로 센다.
    """
    ledger = _Ledger.of(state)
    for name, arguments in calls:
        ledger.run(name, arguments=arguments, reason="선행 조회")
    return ledger.freeze()


# ══════════════════════════════════════════════════════════════════════════
#  plan_step — 유일하게 «다음 무엇» 을 고르는 자리
# ══════════════════════════════════════════════════════════════════════════


def plan_step(state: InvestigationState) -> dict[str, Any]:
    """LLM 이 **Tool 하나와 인자**를 고른다. 숫자를 만들지는 않는다.

    🔴 공급자 실패는 **그래프를 멈추지 않는다** — `llm_status` 를 바꾸고 규칙 경로로
       내려간다 (매입 `allocate_sourcing` 과 같은 규율). DB 는 어차피 아무것도 안 바뀐다.
    """
    if _expired(state):
        return {"step": None, "finish_reason": FinishReason.TIMEOUT}

    budget = state["request"].budget
    if int(state.get("llm_call_count") or 0) >= budget.max_llm_calls:
        return {"step": None, "finish_reason": FinishReason.BUDGET_EXCEEDED}

    plan_fn: PlanFn = state["plan_fn"]
    try:
        step = plan_fn(_view(state))
    except Exception as error:  # noqa: BLE001 - 공급자 실패가 조사를 멈추면 안 된다
        return {
            "step": None,
            "llm_call_count": int(state.get("llm_call_count") or 0) + 1,
            **_llm_failure(error),
        }
    return {"step": step, "llm_call_count": int(state.get("llm_call_count") or 0) + 1}


def _llm_failure(error: Exception) -> dict[str, Any]:
    """공급자 실패를 **프로젝트 공통 어휘**로 적는다.

    ```text
    꺼져 있음   llm_status=DISABLED   · finish_reason 은 건드리지 않는다 (실패가 아니다)
    전송 실패   llm_status=FALLBACK   · finish_reason=LLM_FAILED
    ```
    """
    if isinstance(error, AgentLLMDisabled):
        # 🔴 꺼 둔 것은 **장애가 아니다.** `finish_reason` 을 건드리지 않는다 —
        #    그래프는 결정론으로 끝까지 가고 규칙 제안 하나를 낸다.
        return {"llm_status": "DISABLED", "llm_error_kind": None}
    if isinstance(error, AgentLLMBudgetExceeded):
        # 예산을 다 쓴 것도 장애가 아니다 — **우리가 막은 것이다.** 원인 분류를 안 붙인다.
        return {
            "llm_status": "FALLBACK",
            "llm_error_kind": None,
            "finish_reason": FinishReason.BUDGET_EXCEEDED,
        }
    _, kind = classify_llm_error(error)
    return {
        "llm_status": "FALLBACK",
        "llm_error_kind": kind,
        "finish_reason": FinishReason.LLM_FAILED,
    }


def _view(state: InvestigationState) -> InvestigationView:
    """모델이 보는 전부를 조립한다. 🔴 **여기 없는 것은 모델이 못 본다.**"""
    request: InvestigationRequest = state["request"]
    budget = request.budget
    observations = state.get("observations") or ()
    return InvestigationView(
        sim_run_id=request.sim_run_id,
        as_of=request.as_of,
        exception=jsonable(state.get("exception")),
        scope={
            "subject_type": state.get("subject_type"),
            "subject_id": state.get("subject_id"),
            "allowed_lot_ids": sorted(state.get("allowed_lot_ids") or ()),
            "allowed_item_ids": sorted(state.get("allowed_item_ids") or ()),
        },
        observations=tuple(jsonable(record) for record in observations),
        budget={
            # 🔴 **실행 총량**이다 — 선행 조회와 후보 검증까지 포함한 수 (v1.1).
            "tool_calls_used": int(state.get("tool_call_count") or 0),
            "tool_calls_left": budget.max_tool_calls - int(state.get("tool_call_count") or 0),
            "tool_calls_you_chose": int(state.get("planned_tool_call_count") or 0),
            "replans_used": int(state.get("replan_count") or 0),
            "replans_left": budget.max_replans - int(state.get("replan_count") or 0),
        },
        last_rejection=state.get("last_rejection"),
        # 🔴 공급자 전송 timeout 을 여기에 맞춘다 — 30초짜리 호출 하나가 120초 계약을
        #    혼자 넘기면 안 된다 (§8.5).
        remaining_seconds=_finite(_remaining(state)),
    )


def _finite(seconds: float) -> float | None:
    """마감이 없으면 `None` — «남은 시간 0» 과 «마감 없음» 은 다른 사실이다."""
    return None if seconds == float("inf") else seconds


# ══════════════════════════════════════════════════════════════════════════
#  guard — 결정론. 여기서 막히면 Tool 까지 가지도 않는다
# ══════════════════════════════════════════════════════════════════════════


def guard(state: InvestigationState) -> dict[str, Any]:
    """모델의 한 수를 판정하고 **다음 어디로 갈지**까지 정한다.

    ★ 판정을 노드 안에 두고 라우터는 `state["route"]` 만 읽는다 — 그래야 분기 규칙을
      노드 단위로 시험할 수 있다.
    """
    budget = state["request"].budget
    llm_left = budget.max_llm_calls - int(state.get("llm_call_count") or 0)

    if state.get("finish_reason") in {FinishReason.TIMEOUT, FinishReason.NOT_FOUND}:
        return {"route": "finish"}

    step: InvestigationStep | None = state.get("step")
    if step is None:
        # 공급자 실패 · LLM 예산 소진 — 규칙 제안으로 닫는다.
        return {"route": "fallback"}

    if step.action == "FINISH":
        return {"route": "finalize" if llm_left >= 1 else "fallback"}

    if int(state.get("tool_call_count") or 0) >= budget.max_tool_calls:
        # 🔴 Tool 예산이 끝났다고 조사를 버리지 않는다 — 모은 사실로 정리는 한다.
        rejected = _rejection_record(
            state, step, f"{TOOL_BUDGET_EXCEEDED}:{budget.max_tool_calls}"
        )
        return {
            **rejected,
            "route": "finalize" if llm_left >= 1 else "fallback",
            "finish_reason": FinishReason.BUDGET_EXCEEDED,
        }

    verdict = guard_step(
        step,
        allowed_lot_ids=frozenset(state.get("allowed_lot_ids") or ()),
        allowed_item_ids=frozenset(state.get("allowed_item_ids") or ()),
        executed_keys=frozenset(state.get("executed_keys") or ()),
        tool_call_count=int(state.get("tool_call_count") or 0),
        max_tool_calls=budget.max_tool_calls,
    )
    if verdict.approved:
        return {"route": "execute", "verdict": verdict, "last_rejection": None}

    replans = int(state.get("replan_count") or 0) + 1
    rejected = _rejection_record(state, step, verdict.rejection or "")
    if replans > budget.max_replans:
        return {
            **rejected,
            "replan_count": replans,
            "route": "fallback",
            "finish_reason": FinishReason.GUARD_EXHAUSTED,
            "uncertainties": (
                *(state.get("uncertainties") or ()),
                f"{REPLAN_BUDGET_EXCEEDED}:{budget.max_replans}",
            ),
        }
    # ★ 이전 결과를 다시 주고 재계획하게 한다 — 거부 사유를 `last_rejection` 으로 싣는다.
    return {**rejected, "replan_count": replans, "route": "plan"}


def _rejection_record(
    state: InvestigationState, step: InvestigationStep, rejection: str
) -> dict[str, Any]:
    """거부도 **관찰로 남긴다.** 왜 이 판단을 했는가에 답하려면 막힌 수도 보여야 한다."""
    sequence = int(state.get("sequence") or 0) + 1
    record = ToolCallRecord(
        sequence=sequence,
        tool_name=step.tool_name or "",
        arguments=dict(step.arguments),
        status=ToolCallStatus.REJECTED,
        detail=rejection,
        reason=step.reason,
    )
    return {
        "observations": (*(state.get("observations") or ()), record),
        "sequence": sequence,
        "last_rejection": rejection,
    }


def route_after_guard(state: InvestigationState) -> str:
    return str(state.get("route") or "fallback")


# ══════════════════════════════════════════════════════════════════════════
#  execute_tool — 평범한 파이썬 호출이다
# ══════════════════════════════════════════════════════════════════════════


def execute_tool(state: InvestigationState) -> dict[str, Any]:
    """승인된 한 수를 실행한다. 🔴 **`sim_run_id` · `as_of` 는 요청 값으로 못 박힌다.**

    ⚠️ 실패해도 **실행량은 쓴 것**이다 (v1.1 정정). 안 세면 터지는 Tool 을 무한히 다시
       고르는 경로가 열린다 — 중복 키는 성공했을 때만 등록되므로 guard 도 못 막는다.
    """
    verdict = state["verdict"]
    step: InvestigationStep | None = state.get("step")
    ledger = _Ledger.of(state)
    ledger.run(
        str(verdict.tool_name),
        arguments=dict(verdict.arguments or {}),
        reason=step.reason if step else "",
        planned=True,
        key=str(verdict.key),
    )
    return {**ledger.freeze(), "last_rejection": None}


# ══════════════════════════════════════════════════════════════════════════
#  finalize · fallback_rule
# ══════════════════════════════════════════════════════════════════════════


def finalize(state: InvestigationState) -> dict[str, Any]:
    """모은 사실을 문장으로 잇는다. 🔴 **숫자를 새로 만들 칸이 스키마에 없다.**"""
    if _expired(state):
        return {"report": None, "finish_reason": FinishReason.TIMEOUT, "route": "fallback"}
    finalize_fn: FinalizeFn = state["finalize_fn"]
    try:
        report = finalize_fn(_view(state))
    except Exception as error:  # noqa: BLE001 - 공급자 실패가 조사를 멈추면 안 된다
        return {
            "report": None,
            "route": "fallback",
            "llm_call_count": int(state.get("llm_call_count") or 0) + 1,
            **_llm_failure(error),
        }
    finished = state.get("finish_reason") or FinishReason.FINISHED
    return {
        "report": report,
        "route": "evaluate",
        "llm_call_count": int(state.get("llm_call_count") or 0) + 1,
        "llm_status": "SUCCESS",
        "finish_reason": finished,
    }


def route_after_finalize(state: InvestigationState) -> str:
    return str(state.get("route") or "fallback")


def fallback_rule(state: InvestigationState) -> dict[str, Any]:
    """code 별 **규칙 제안 하나**. 🔴 **숫자를 새로 만들지 않는다** (§39).

    ```text
    FRESHNESS_PRESSURE  →  SALES_PRIORITY_REQUEST(lot_id)
                           ⚠️ qty_kg 를 넣지 않는다 — 판매 수량은 영업이 정한다(§9.1).
                              Tool 이 candidate_kg 로 «팔 수 있는 후보» 만 돌려준다.
    CAPACITY_PRESSURE   →  PURCHASE_ADJUST_REQUEST(가장 큰 예정 입고를 줄인다)
                           ⚠️ 수량·도착일은 Tool 이 낸 그 일정의 값 그대로다.
    그 밖                →  ACCEPT_RISK
    ```

    ★ 값이 모자라면 **비워 둔다.** `estimate_action_impact` 가 `UNRESOLVED` 와
      `IMPACT_INPUT_MISSING:…` 로 정확히 말해 준다 — 빈 칸을 지어내 채우는 것보다 낫다.
    """
    exception = state.get("exception")
    code = getattr(exception, "code", "") or ""
    subject_type = state.get("subject_type") or ""
    subject_id = state.get("subject_id") or ""
    observations = state.get("observations") or ()

    if code == FRESHNESS_PRESSURE and subject_type == "LOT":
        option = InvestigationOption(
            action="SALES_PRIORITY_REQUEST",
            parameters={"lot_id": subject_id},
            rationale=(
                "신선도 압박 Lot 이다. 우선 판매 후보로 올린다 — "
                "판매 수량은 영업이 정한다(물류는 댈 수 있는 양만 확인한다)."
            ),
            evidence_refs=_sequences(observations, "get_lot"),
        )
    elif code == CAPACITY_PRESSURE:
        option = InvestigationOption(
            action="PURCHASE_ADJUST_REQUEST",
            parameters=_largest_inbound_parameters(observations),
            rationale=(
                "용량 압박이다. 예정 입고 중 가장 큰 건을 줄이는 안을 올린다 — "
                "실제 회차·도착일 조정은 매입이 정한다."
            ),
            evidence_refs=_sequences(observations, "get_capacity_context", "get_inbound_schedule"),
        )
    else:
        option = InvestigationOption(
            action="ACCEPT_RISK",
            parameters={"lot_id": subject_id} if subject_type == "LOT" else {},
            rationale="규칙이 아는 대응이 없다. 위험을 안고 가는 안만 남긴다.",
            evidence_refs=_sequences(observations, "get_lot", "get_capacity_context"),
        )

    report = InvestigationReport(
        summary=f"AI 판단 없이 규칙으로 정리했다 ({code or '알 수 없는 코드'}).",
        findings=[],
        missing_or_uncertain=["AI 조사가 수행되지 않아 대안 비교가 없다."],
        options=[option],
        recommended_index=0,
    )
    status: LLMStatus = state.get("llm_status") or "FALLBACK"
    if status == "SUCCESS":
        # finalize 가 터진 경우다 — 성공으로 남겨 두면 규칙 제안이 AI 판단으로 보인다.
        status = "FALLBACK"
    finished = state.get("finish_reason")
    if finished in {None, FinishReason.FINISHED}:
        finished = FinishReason.FINISHED if status == "DISABLED" else FinishReason.LLM_FAILED
    return {"report": report, "llm_status": status, "finish_reason": finished}


def _sequences(records: Sequence[ToolCallRecord], *names: str) -> list[int]:
    wanted = set(names)
    return [
        record.sequence
        for record in records
        if record.tool_name in wanted and record.status is ToolCallStatus.SUCCESS
    ]


def _largest_inbound_parameters(records: Sequence[ToolCallRecord]) -> dict[str, Any]:
    """가장 큰 예정 입고 **그대로**. 🔴 초과분을 따로 셈하지 않는다 — 새 계산기 금지.

    수량은 그 일정의 `quantity_kg` 값이고 부호만 «줄인다» 는 방향으로 뒤집는다. 여유가
    실제로 얼마나 생기는지는 `estimate_action_impact` 가 답한다.
    """
    best: Any = None
    for record in records:
        for schedule in getattr(record.answer, "schedules", ()) or ():
            if best is None or schedule.quantity_kg > best.quantity_kg:
                best = schedule
    if best is None:
        return {}
    return {
        "qty_delta_kg": -Decimal(best.quantity_kg),
        "arrival_date": best.expected_arrival_date,
    }


# ══════════════════════════════════════════════════════════════════════════
#  evaluate_options — LLM 이 적은 숫자는 여기서 Tool 값으로 덮인다
# ══════════════════════════════════════════════════════════════════════════


def evaluate_options(state: InvestigationState) -> dict[str, Any]:
    """후보를 결정론으로 검증한다. 🔴 **`UNRESOLVED` 를 억지로 `FEASIBLE` 로 바꾸지 않는다.**

    ```text
    카탈로그       action ∈ SUPPORTED_ACTIONS            아니면 버린다
    근거           evidence_refs 가 이번 run 에 있는 번호  없는 번호는 지운다
    범위           parameters.lot_id 가 조사 범위 안       아니면 버린다
    숫자           estimate_action_impact 로 **덮어쓴다**  모델이 적은 값은 검토값일 뿐
    ```

    🔴 **여기서 부르는 `estimate_action_impact` 도 Tool 실행이다** (v1.1 정정). 후보
       수만큼 DB 를 왕복하므로 **남은 예산 안에서만** 검증한다.

    ```text
    남은 예산 2 · 후보 4개  →  앞의 2개만 검증
                            →  나머지는 TOOL_BUDGET_EXCEEDED 사유로 남는다
    ```

    ★ 못 검증한 후보를 «아마 될 것» 으로 채우지 않는다. 숫자를 지어내 메우는 것보다
      *"예산이 없어 못 쟀다"* 가 정직하다.
    """
    report: InvestigationReport | None = state.get("report")
    if report is None:
        return {"options": ()}
    observations = state.get("observations") or ()
    known = {
        record.sequence for record in observations if record.status is ToolCallStatus.SUCCESS
    }
    allowed_lots = frozenset(state.get("allowed_lot_ids") or ())
    ledger = _Ledger.of(state)

    evaluated: list[EvaluatedOption] = []
    for option in report.options:
        owner = ACTION_DECISION_OWNERS.get(option.action, "LOGISTICS")
        base = EvaluatedOption(
            action=option.action,
            parameters=dict(option.parameters),
            rationale=option.rationale,
            evidence_refs=tuple(ref for ref in option.evidence_refs if ref in known),
            decision_owner=owner,
            # 🔴 영업·매입 소관이면 `parameters` 의 수량·도착일은 **검토값**이다 (§26).
            parameters_are_hypothesis=owner != "LOGISTICS",
        )
        if option.action not in SUPPORTED_ACTIONS:
            evaluated.append(replace(base, rejected_reason=f"ACTION_UNSUPPORTED:{option.action}"))
            continue
        impact, rejection = _impact_for(
            ledger,
            action=option.action,
            parameters=option.parameters,
            allowed_lot_ids=allowed_lots,
        )
        evaluated.append(replace(base, impact=impact, rejected_reason=rejection))

    index = report.recommended_index
    if index is None or not 0 <= index < len(evaluated) or not evaluated[index].accepted:
        index = next((i for i, item in enumerate(evaluated) if item.accepted), None)
    return {**ledger.freeze(), "options": tuple(evaluated), "recommended_index": index}


def _impact_for(
    ledger: _Ledger,
    *,
    action: str,
    parameters: Mapping[str, Any],
    allowed_lot_ids: frozenset[str],
) -> tuple[ActionImpact | None, str | None]:
    """후보 하나의 영향. 인자 검증은 `guard` 와 **같은 규칙**을 쓴다 — 두 벌로 두지 않는다.

    🔴 실행은 저울을 지난다 — 예산이 없으면 **부르지 않고** 그 사유를 돌려준다.
    """
    stopped = ledger.blocked()
    if stopped is not None:
        return None, stopped
    probe = InvestigationStep(
        action="CALL_TOOL",
        tool_name="estimate_action_impact",
        arguments={"action": action, "parameters": dict(parameters)},
    )
    verdict = guard_step(
        probe,
        allowed_lot_ids=allowed_lot_ids,
        allowed_item_ids=frozenset(),
        # ★ 중복은 여기서 보지 않는다 — 모델의 탐색이 아니라 결정론 검증이다. 같은 후보를
        #   두 번 내면 두 번 재는 것이 맞고, 그 횟수는 **예산**이 막는다.
        executed_keys=frozenset(),
        tool_call_count=ledger.spent,
        max_tool_calls=ledger.state["request"].budget.max_tool_calls,
    )
    if not verdict.approved:
        return None, verdict.rejection
    impact = ledger.run(
        "estimate_action_impact",
        arguments=verdict.arguments or {},
        reason="후보 검증 — 숫자는 Tool 이 낸다",
    )
    if impact is None:
        last = ledger.last
        return None, (last.detail if last else None) or "IMPACT_FAILED"
    return impact, None


# ══════════════════════════════════════════════════════════════════════════
#  finish — 결과 조립. DB 에 아무것도 안 쓴다
# ══════════════════════════════════════════════════════════════════════════


def finish(state: InvestigationState) -> dict[str, Any]:
    """조사 결과를 만든다. 🔴 **`persist` 가 아니다** — Proposal 저장은 Commit 5 다.

    관측일 규칙 (§24): 성공한 Tool 답의 `observed_as_of` 를 모아 `max`, **하나라도 모르면
    `None`.** `as_of` · 오늘 날짜 · 모델 응답 시각으로 절대 메우지 않는다.

    🔴 **인용된 근거만 세지 않고 성공한 Tool 전부를 센다 (fail-closed).** 인용 목록은
       모델이 고르는 칸이라, 거기에 관측일을 걸면 **모델이 근거를 좁혀 날짜를 만들어
       낼 수 있다.** 넓게 세면 값은 더 보수적(=더 자주 `None`)이 될 뿐이다.
    """
    request: InvestigationRequest = state["request"]
    report: InvestigationReport | None = state.get("report")
    observations = state.get("observations") or ()
    successes = [record for record in observations if record.status is ToolCallStatus.SUCCESS]

    uncertainties = [*(state.get("uncertainties") or ())]
    for record in successes:
        uncertainties.extend(record.uncertainties)

    result = InvestigationResult(
        sim_run_id=request.sim_run_id,
        as_of=request.as_of,
        exception_id=request.exception_id,
        finish_reason=state.get("finish_reason") or FinishReason.FINISHED,
        llm_status=state.get("llm_status") or "SKIPPED_TEMPLATE",
        llm_error_kind=state.get("llm_error_kind"),
        summary=report.summary if report else "",
        findings=tuple(report.findings) if report else (),
        missing_or_uncertain=tuple(report.missing_or_uncertain) if report else (),
        options=tuple(state.get("options") or ()),
        recommended_index=state.get("recommended_index"),
        tool_calls=tuple(observations),
        observed_as_of=derive_observed_as_of([record.observed_as_of for record in successes]),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
        tool_call_count=int(state.get("tool_call_count") or 0),
        planned_tool_call_count=int(state.get("planned_tool_call_count") or 0),
        replan_count=int(state.get("replan_count") or 0),
        llm_call_count=int(state.get("llm_call_count") or 0),
    )
    return {"result": result}


#: `load_exception` 에서 **조사 자체가 성립하지 않는** 끝들. 더 캐지 않고 바로 닫는다.
_STOPPED_AT_LOAD = frozenset(
    {FinishReason.NOT_FOUND, FinishReason.TIMEOUT, FinishReason.BUDGET_EXCEEDED}
)


def route_after_load(state: InvestigationState) -> str:
    return "finish" if state.get("finish_reason") in _STOPPED_AT_LOAD else "context"


# ══════════════════════════════════════════════════════════════════════════
#  조립
# ══════════════════════════════════════════════════════════════════════════


def build_investigation_graph() -> Any:
    """그래프 하나. 🔴 **checkpointer 도 interrupt 도 없다** — 사람 게이트는 Commit 5 다.

    ★ 저장소 관습 그대로다: `TypedDict` State · 노드는 바꾼 키만 반환 · 조건부 간선은
      `path_map` 을 명시한다 (`purchase_agent.graph` · `sales.graph`).
    """
    builder = StateGraph(InvestigationState)
    builder.add_node("load_exception", load_exception)
    builder.add_node("load_context", load_context)
    builder.add_node("preload_subject", preload_subject)
    builder.add_node("plan_step", plan_step)
    builder.add_node("guard", guard)
    builder.add_node("execute_tool", execute_tool)
    builder.add_node("finalize", finalize)
    builder.add_node("fallback_rule", fallback_rule)
    builder.add_node("evaluate_options", evaluate_options)
    builder.add_node("finish", finish)

    builder.add_edge(START, "load_exception")
    builder.add_conditional_edges(
        "load_exception",
        route_after_load,
        {"finish": "finish", "context": "load_context"},
    )
    builder.add_edge("load_context", "preload_subject")
    builder.add_edge("preload_subject", "plan_step")
    builder.add_edge("plan_step", "guard")
    builder.add_conditional_edges(
        "guard",
        route_after_guard,
        {
            "execute": "execute_tool",
            "plan": "plan_step",
            "finalize": "finalize",
            "fallback": "fallback_rule",
            "finish": "finish",
        },
    )
    builder.add_edge("execute_tool", "plan_step")
    builder.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {"evaluate": "evaluate_options", "fallback": "fallback_rule"},
    )
    builder.add_edge("fallback_rule", "evaluate_options")
    builder.add_edge("evaluate_options", "finish")
    builder.add_edge("finish", END)
    return builder.compile()


def _recursion_limit(budget: InvestigationBudget) -> int:
    """🔴 LangGraph 기본값 25 는 **우리 예산보다 작다.**

    ```text
    한 바퀴      plan_step + guard (+ execute_tool)   ≤ 3 걸음
    바퀴 수      LLM 호출 상한만큼                     max_llm_calls
    바깥         load × 3 · finalize · fallback · evaluate · finish
    ```

    상한을 예산에서 **계산해서** 준다 — 상수로 박으면 예산을 늘렸을 때 조용히
    `GraphRecursionError` 로 죽는다.
    """
    return 2 * budget.max_llm_calls + budget.max_tool_calls + 16


def run_investigation(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    exception_id: str,
    plan_fn: PlanFn | None = None,
    finalize_fn: FinalizeFn | None = None,
    budget: InvestigationBudget | None = None,
    clock: Any = None,
) -> InvestigationResult:
    """조사 한 번. **Logistics 가 소유하는 파이썬 진입점**이다.

    🔴 **HTTP 도 Scheduler 도 STATUS_QUERY 도 여기 안 붙는다** (§30 · §31). 붙이는 것은
       Commit 7 이고, Master `status_flow.py` 는 이번 Commit 에서 손대지 않는다.

    ★ `plan_fn` · `finalize_fn` 을 비우면 실제 공급자를 쓴다. 테스트는 평범한 함수를
      꽂아 **LLM 없이 그래프 전체를 결정적으로** 돌린다 (매입 `selector` 와 같은 규율).
    """
    if plan_fn is None or finalize_fn is None:
        from app.logistics.agent.llm_client import build_agent_llm

        # ⚠️ 조사 한 번에 client 하나 — 전송 예산이 인스턴스에 산다.
        default_plan, default_finalize = build_agent_llm(
            max_sends=(budget or DEFAULT_BUDGET).max_llm_calls, clock=clock
        )
        plan_fn = plan_fn or default_plan
        finalize_fn = finalize_fn or default_finalize

    limits = budget or DEFAULT_BUDGET
    tick = clock or time.monotonic
    request = InvestigationRequest(
        sim_run_id=sim_run_id, as_of=as_of, exception_id=exception_id, budget=limits
    )
    state: InvestigationState = {
        "conn": conn,
        "request": request,
        "plan_fn": plan_fn,
        "finalize_fn": finalize_fn,
        "clock": tick,
        "deadline_at": tick() + limits.timeout_seconds,
        "observations": (),
        "executed_keys": frozenset(),
        "allowed_lot_ids": frozenset(),
        "allowed_item_ids": frozenset(),
        "tool_call_count": 0,
        "planned_tool_call_count": 0,
        "replan_count": 0,
        "llm_call_count": 0,
        "sequence": 0,
        "uncertainties": (),
    }
    final = build_investigation_graph().invoke(
        state, config={"recursion_limit": _recursion_limit(limits)}
    )
    return final["result"]

