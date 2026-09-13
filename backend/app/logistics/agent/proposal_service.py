"""조사 결과를 **사람이 결정할 수 있는 한 건**으로 바꾸고, 그 결정을 받는다.

```text
InvestigationResult          create_proposal   PROPOSED
        ↓                          ↓
  recommended option      Exception OPEN → PROPOSED   (한 트랜잭션)
        ↓
     사람 ──┬── approve_proposal ──▶ APPROVED
            ├── reject_proposal  ──▶ REJECTED   → 살아 있는 제안 0 이면 Exception 다시 OPEN
            └── expire_proposal  ──▶ EXPIRED    → 〃
```

🔴 **`APPROVED` 는 `EXECUTED` 가 아니다.** *"이 대응안으로 진행해도 좋다"* 는 사람의
   결정일 뿐이고, 승인 뒤에도 재고·판매·매입·용량은 **한 줄도 안 바뀐다.** 실제 실행은
   Commit 6 이다. 이 파일에서 `inventory_*` · `sales` · 매입 표를 쓰는 SQL 은 하나도 없다.

🔴 **LLM 이 여기 없다.** 승인·거절은 사람이 이미 내린 결정이다 — *"이 승인 괜찮을까요"*
   를 모델에게 되묻지 않고, 모델이 승인자 이름이나 거절 사유를 짓지도 않는다 (§35).

🔴 **트랜잭션의 주인이 이 파일이다.** `proposals.py` 는 커밋도 롤백도 안 한다고
   못박았고(`agent/exceptions.py` ↔ `master/inspection.py` 와 같은 나눔) 그 약속의
   반대편이 여기다. **제안 INSERT 와 Exception UPDATE 는 함께 성공하거나 함께 없다.**

⚠️ **숫자를 여기서 만들지 않는다.** `candidate_kg` · `capacity_delta_kg` ·
   `estimated_loss_krw` · `feasibility` 는 Commit 3 의 `estimate_action_impact` 가 이미
   낸 값이고, 제안은 그것을 **옮기기만** 한다 (§51).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import ValidationError

from app.logistics.agent import investigation_repository, investigation_service
from app.logistics.agent import proposals as repository
from app.logistics.agent.investigation import (
    ACTION_DECISION_OWNERS,
    EvaluatedOption,
    InvestigationResult,
    jsonable,
)
from app.logistics.agent.proposals import (
    ProposalRow,
    project_proposal_at,
    proposal_key_for,
)
from app.logistics.agent.schemas import LIVE_STATUSES as LIVE_EXCEPTION_STATUSES
from app.logistics.agent.tool_dispatch import ActionImpactArguments
from app.logistics.agent.tools import SUPPORTED_ACTIONS

__all__ = [
    "ACTION_UNSUPPORTED",
    "EVIDENCE_FACTS_UNAVAILABLE",
    "EXCEPTION_MISMATCH",
    "EXCEPTION_NOT_FOUND",
    "EXCEPTION_NOT_LIVE",
    "IMPACT_INFEASIBLE",
    "IMPACT_MISSING",
    "INVESTIGATION_ALREADY_USED",
    "INVESTIGATION_NOT_FOUND",
    "INVESTIGATION_RESULT_MISMATCH",
    "LIVE_PROPOSAL_EXISTS",
    "NO_RECOMMENDED_OPTION",
    "OPTION_REJECTED",
    "PARAMETERS_INVALID",
    "PROPOSAL_HISTORY_CONFLICT",
    "STALE_PROPOSAL",
    "SUPERSEDE_TARGET_MISMATCH",
    "ProposalNotFound",
    "ProposalOutcome",
    "ProposalStateConflict",
    "approve_proposal",
    "create_proposal",
    "expire_proposal",
    "get_proposal",
    "list_proposals",
    "proposal_payload",
    "reject_proposal",
    "select_proposal_option",
]


# ══════════════════════════════════════════════════════════════════════════
#  «제안을 안 만든 이유» 의 어휘 — 🔴 조용히 0 건으로 끝내지 않는다
# ══════════════════════════════════════════════════════════════════════════

#: 조사가 추천할 대안을 하나도 못 골랐다 (`recommended_index is None`).
NO_RECOMMENDED_OPTION = "NO_RECOMMENDED_OPTION"
#: 추천 번호가 후보 목록 밖이다.
RECOMMENDED_INDEX_OUT_OF_RANGE = "RECOMMENDED_INDEX_OUT_OF_RANGE"
#: 추천된 후보가 이미 결정론에 의해 버려진 것이다 (§15).
OPTION_REJECTED = "OPTION_REJECTED"
#: 카탈로그 밖 행동이다 (§10.1).
ACTION_UNSUPPORTED = "ACTION_UNSUPPORTED"
#: 영향을 못 쟀다 — `estimate_action_impact` 를 아예 못 불렀다.
IMPACT_MISSING = "IMPACT_MISSING"
#: 결정론 계산기가 **«불가»** 라고 답했다. 🔴 «못 쟀다»(UNRESOLVED) 와 다르다.
IMPACT_INFEASIBLE = "IMPACT_INFEASIBLE"
#: 인자가 행동 스키마를 못 지난다 (§50 — Commit 4 의 모델을 그대로 쓴다).
PARAMETERS_INVALID = "PARAMETERS_INVALID"
#: 그 Exception 이 이 실행에 없다.
EXCEPTION_NOT_FOUND = "EXCEPTION_NOT_FOUND"
#: 조사 결과가 가리키는 Exception 과 요청이 다르다.
EXCEPTION_MISMATCH = "EXCEPTION_MISMATCH"
#: 이미 닫힌(RESOLVED · DISMISSED) 문제에는 대응안을 안 세운다.
EXCEPTION_NOT_LIVE = "EXCEPTION_NOT_LIVE"
#: 같은 문제에 살아 있는 대응안이 **이미 있다** (§47 · §48).
LIVE_PROPOSAL_EXISTS = "LIVE_PROPOSAL_EXISTS"
#: 인용된 Tool 답에서 **핵심 사실을 못 뽑았다.** 🔴 그렇다고 제안을 버리지 않고,
#:    없는 값을 지어내지도 않는다 — 못 뽑았다는 사실만 근거에 남긴다.
EVIDENCE_FACTS_UNAVAILABLE = "EVIDENCE_FACTS_UNAVAILABLE"


# ── 상태 충돌 어휘 (`ProposalStateConflict.code`) ────────────────────────

#: 🔴 대체하려는 제안이 **이 문제의 살아 있는 제안이 아니다.**
#:    다른 Exception · 다른 실행 · 이미 끝난 제안 · 없는 ID 를 모두 덮는다.
SUPERSEDE_TARGET_MISMATCH = "SUPERSEDE_TARGET_MISMATCH"
#: 🔴 승인하려는데 **그 문제가 이미 다른 상태다.** 사람이 보던 화면이 낡았다.
STALE_PROPOSAL = "STALE_PROPOSAL"

#: 🔴 가리키라고 준 조사가 **DB 에 없다.** 저장되지 않은 조사에 제안을 매달지 않는다.
INVESTIGATION_NOT_FOUND = "INVESTIGATION_NOT_FOUND"
#: 🔴 그 조사는 **이 결과의 저장본이 아니다.** 축(실행·문제)은 맞는데 내용이 다르다 —
#:    복합 FK 가 통과시키는 바로 그 자리다.
INVESTIGATION_RESULT_MISMATCH = "INVESTIGATION_RESULT_MISMATCH"
#: 🔴 그 조사는 **이미 제안 하나를 냈다.** 조사 하나에 제안은 최대 하나다 — 새 안이
#:    필요하면 새 조사를 돌려야 한다. ⚠️ 그 제안이 끝났는지(거절·만료)와 무관하다.
INVESTIGATION_ALREADY_USED = "INVESTIGATION_ALREADY_USED"
#: 🔴 이전 제안이 끝난 날보다 **앞선 날짜**로 새 제안을 세우려 한다.
PROPOSAL_HISTORY_CONFLICT = "PROPOSAL_HISTORY_CONFLICT"


class _ConcurrentInsert(RuntimeError):
    """누가 먼저 같은 자리를 잡았다. 🔴 **밖으로 안 나간다** — 재조회가 뜻을 정한다."""


class ProposalNotFound(LookupError):
    """그 실행에 그 제안이 없다."""


class ProposalStateConflict(RuntimeError):
    """지금 상태에서 할 수 없는 결정이다.

    ```text
    ALREADY_APPROVED            이미 승인됐다 — 다른 사람이거나 다른 날이다
    ALREADY_REJECTED            이미 거절됐다 — 〃
    ALREADY_EXPIRED             이미 만료됐다 — 〃
    STALE_PROPOSAL              그 문제가 이미 다른 상태다 (닫혔거나 대응 대기가 아니다)
    SUPERSEDE_TARGET_MISMATCH   대체 대상이 이 문제의 살아 있는 제안이 아니다
    PROPOSAL_HISTORY_CONFLICT   이전 제안이 끝난 날보다 앞선 날짜로 세우려 한다
    STATE_CONFLICT              그 밖 (거절된 것을 승인하려는 등)
    ```

    ★ **같은 사람이 같은 날 같은 내용으로** 다시 부른 것은 충돌이 아니라 **재시도**다 —
      그때는 예외가 아니라 기존 행이 그대로 돌아온다 (§38 · §39).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


OutcomeStatus = Literal["CREATED", "REUSED", "SKIPPED"]


@dataclass(frozen=True)
class ProposalOutcome:
    """제안을 만들려 한 결과. **예외 대신 이것을 돌려준다.**

    ```text
    CREATED   새 행이 섰다
    REUSED    같은 뜻의 살아 있는 제안이 이미 있었다 — 재시도다 (§12)
    SKIPPED   안 만들었다. 왜 안 만들었는지는 `reason` 에 있다
    ```

    🔴 **`SKIPPED` 를 «실패» 로 읽지 않는다.** 추천할 대안이 없는 조사는 정상이고, 그때
       제안 0 건은 **옳은 답**이다 (§53). 다만 *"왜 0 건인가"* 를 못 대면 안 된다 —
       `InspectionOut.NOTHING_DUE` 와 `FAILED` 를 안 접는 것과 같은 규율이다.
    """

    status: OutcomeStatus
    reason: str = ""
    proposal: ProposalRow | None = None

    @property
    def created(self) -> bool:
        return self.status == "CREATED"


# ══════════════════════════════════════════════════════════════════════════
#  어떤 후보가 제안이 되나 — **순수 함수다. DB 를 안 본다**
# ══════════════════════════════════════════════════════════════════════════


def select_proposal_option(
    result: InvestigationResult,
) -> tuple[EvaluatedOption | None, str]:
    """조사 결과에서 저장할 후보 하나. 없으면 `(None, 사유)`.

    ```text
    recommended_index 가 없다            → 제안 0 건
    추천 후보가 결정론에 버려졌다          → 제안 0 건 (§15)
    카탈로그 밖 행동이다                  → 제안 0 건
    영향을 못 불렀다                      → 제안 0 건
    결정론이 «불가» 라고 답했다            → 제안 0 건
    영향이 UNRESOLVED 다                 → ✅ **제안한다** (§15)
    ```

    🔴 **«못 쟀다» 와 «불가» 를 가른다.** `UNRESOLVED` 는 입력이 모자라 못 쟀다는
       뜻이고, 그 사실을 그대로 실어 사람에게 보이는 것이 정직하다 — 숫자를 지어내
       `FEASIBLE` 로 바꾸지 않는다. 반대로 `INFEASIBLE` 은 계산기가 **재 보고 «안
       된다»** 고 답한 것이다. 그것을 승인 화면에 올리면 사람이 불가능한 일을 승인한다.

    ⚠️ **두 번째 후보로 미끄러지지 않는다.** 추천이 버려졌으면 제안 0 건이다 —
       조사가 고르지 않은 안을 저장이 대신 고르면, 그 선택의 주인이 아무도 아니게 된다.
    """
    index = result.recommended_index
    if index is None:
        return None, NO_RECOMMENDED_OPTION
    if not 0 <= index < len(result.options):
        return None, f"{RECOMMENDED_INDEX_OUT_OF_RANGE}:{index}"

    option = result.options[index]
    if not option.accepted:
        return None, f"{OPTION_REJECTED}:{option.rejected_reason}"
    if option.action not in SUPPORTED_ACTIONS:
        return None, f"{ACTION_UNSUPPORTED}:{option.action}"
    if option.impact is None:
        return None, IMPACT_MISSING
    if option.impact.feasibility == "UNSUPPORTED":
        return None, f"{ACTION_UNSUPPORTED}:{option.action}"
    if option.impact.feasibility == "INFEASIBLE":
        return None, IMPACT_INFEASIBLE
    return option, ""


# ══════════════════════════════════════════════════════════════════════════
#  근거 추림 — 🔴 **Proposal 은 조사 로그 저장소가 아니다**
# ══════════════════════════════════════════════════════════════════════════
#
#  Tool 답 **전체**를 제안마다 복사하면 네 가지가 한꺼번에 나빠진다.
#
#  ```text
#  같은 데이터가 제안 수만큼 늘어난다
#  payload 가 비대해진다 (용량 문맥은 18일 창을 통째로 들고 온다)
#  Tool 스키마가 바뀌면 과거 payload 해석이 얽힌다
#  «조사 기록» 과 «승인 대상» 의 책임이 한 칸에 섞인다
#  ```
#
#  그래서 **승인 판단에 실제로 쓴 핵심 사실만** 남긴다. 조사 전체는 Commit 7 의
#  `logistics_investigations` 가 들고, 이 제안은 `investigation_id` 로 그쪽을 가리킨다 —
#  🔴 두 칸의 **질문이 다르다**: 여기는 *"왜 이 대응안을 승인 대상으로 올렸나"* 이고
#  저기는 *"어떤 Tool 을 어떤 순서로 조사했나"* 다. 이 칸을 로그로 키우지 않는다.


@dataclass(frozen=True)
class _Subject:
    """근거를 추릴 때 **«무엇에 대한 제안인가»**. 🔴 여기서 값을 만들지 않는다.

    ★ 전부 이미 확정된 입력에서 온다 — 조사의 `exception_id` 와, 스키마를 지난
      `parameters`. 이 값들로 «어느 Lot · 어느 날 · 어느 입고» 를 고를 뿐이다.
    """

    exception_id: str
    lot_id: str | None
    item_id: str | None
    arrival_date: date | None
    inbound_id: str | None

    @classmethod
    def of(cls, *, exception_id: str, parameters: Mapping[str, Any]) -> _Subject:
        arrival = parameters.get("arrival_date")
        return cls(
            exception_id=exception_id,
            lot_id=_identifier(parameters.get("lot_id")),
            item_id=_identifier(parameters.get("item_id")),
            arrival_date=arrival if isinstance(arrival, date) else None,
            inbound_id=_identifier(parameters.get("inbound_id")),
        )


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _pick(source: Any, *names: str) -> dict[str, Any] | None:
    """그 객체에 **있는 칸만** 뽑는다. 하나도 없으면 `None` (모양이 달라졌다).

    ⚠️ 값이 `None` 인 칸은 **뺀 것이 아니라 담는다** — *"봤는데 못 댔다"* 와
       *"안 봤다"* 는 다른 사실이고, 키가 사라지면 그 구별이 사라진다.
    """
    if source is None:
        return None
    picked = {name: getattr(source, name) for name in names if hasattr(source, name)}
    return picked or None


def _exception_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """조사 대상 문제 **한 줄만.** 그날 목록 전체를 복사하지 않는다."""
    found = getattr(answer, "exceptions", None)
    if found is None:
        return None
    chosen = next(
        (one for one in found if getattr(one, "exception_id", None) == subject.exception_id),
        None,
    )
    if chosen is None:
        return {"exception_count": len(found)}
    return _pick(
        chosen, "exception_id", "code", "subject_type", "subject_id", "opened_as_of",
        "open_days",
    )


def _lot_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    if not hasattr(answer, "lot"):
        return None
    if answer.lot is None:
        # Tool 이 «없다» 고 답했다 — 그 사유는 이미 `uncertainties` 에 있다.
        return {}
    return _pick(
        answer.lot, "lot_id", "item_id", "status", "remaining_qty_kg",
        "remaining_freshness_days", "uncommitted_kg",
    )


def _item_lots_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """🔴 **Lot 배열 전체를 복사하지 않는다.** 몇 개였는지 + 대상 Lot 한 줄이다."""
    lots = getattr(answer, "lots", None)
    if lots is None:
        return None
    facts: dict[str, Any] = {
        "item_id": getattr(answer, "item_id", None),
        "lot_count": len(lots),
    }
    chosen = next(
        (one for one in lots if getattr(one, "lot_id", None) == subject.lot_id), None
    )
    facts.update(
        _pick(
            chosen, "lot_id", "remaining_qty_kg", "remaining_freshness_days",
            "uncommitted_kg",
        )
        or {}
    )
    return facts


def _commitment_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """🔴 **예약 객체 전체를 복사하지 않는다.** 몇 건이었나 + 합계 · 가장 이른 납기."""
    facts = _pick(answer, "item_id", "unallocated_kg", "next_due_date")
    if facts is None:
        return None
    reservations = getattr(answer, "live_reservations", None)
    if reservations is not None:
        facts["live_reservation_count"] = len(reservations)
    return facts


def _policy_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """🔴 **정책 객체 전체를 복사하지 않는다.** 판본과 이 Lot 에 걸리는 임계값뿐이다.

    ⚠️ 전역 `agent_policy` 지도는 안 담는다 — 임계가 어떻게 걸렸는지는 Exception 의
       근거와 `impact_json` 에 이미 있고, 여기 또 두면 같은 값이 세 곳에 산다.
    """
    facts = _pick(answer, "item_id", "policy_version")
    if facts is None:
        return None
    facts.update(
        _pick(
            getattr(answer, "item_policy", None),
            "operational_limit_days",
            "sell_priority_remaining_days",
        )
        or {}
    )
    return facts


def _capacity_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """🔴 **`cap_by_date` 18일 창을 통째로 복사하지 않는다.**

    ```text
    언제나        점유 · 보장 · 여유 · 창 사용률 · 기준
    도착일이 있으면  **그 하루**의 여유만                 ← PURCHASE_ADJUST_REQUEST
    ```
    """
    facts = _pick(
        answer, "used_kg", "guaranteed_kg", "available_kg", "window_usage_ratio",
        "capacity_basis",
    )
    if facts is None:
        return None
    window = getattr(answer, "cap_by_date", None)
    if isinstance(window, Mapping):
        facts["cap_window_days"] = len(window)
        if subject.arrival_date is not None and subject.arrival_date in window:
            facts["arrival_date"] = subject.arrival_date
            facts["available_capacity_kg"] = window[subject.arrival_date]
    return facts


def _inbound_facts(answer: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """🔴 **예정 목록 전체를 복사하지 않는다.** 이 제안이 건드리는 한 건이다."""
    schedules = getattr(answer, "schedules", None)
    if schedules is None:
        return None
    facts: dict[str, Any] = {
        "days": getattr(answer, "days", None),
        "schedule_count": len(schedules),
    }
    chosen = next(
        (
            one
            for one in schedules
            if (
                subject.inbound_id is not None
                and getattr(one, "inbound_id", None) == subject.inbound_id
            )
            or (
                subject.arrival_date is not None
                and getattr(one, "expected_arrival_date", None) == subject.arrival_date
            )
        ),
        None,
    )
    facts.update(
        _pick(chosen, "inbound_id", "item_id", "quantity_kg", "expected_arrival_date") or {}
    )
    return facts


def _impact_facts(answer: Any, subject: _Subject) -> Mapping[str, Any]:
    """🔴 **비워 둔다.** 이 Tool 의 답은 `impact_json` 이 통째로 들고 있다.

    같은 숫자를 두 칸에 두면 둘이 갈라졌을 때 어느 쪽이 정본인지 아무도 못 댄다.
    업무 숫자의 정본은 `impact_json` 하나다 (§51).
    """
    del answer, subject
    return {}


#: Tool 이름 → **핵심 사실 추림**. 🔴 전부 결정론 매핑이다 — LLM 에게 «중요한 것만
#: 골라 줘» 라고 묻지 않고, Tool 을 다시 부르지도 않는다.
_EVIDENCE_FACTS: Mapping[str, Any] = {
    "get_open_exceptions": _exception_facts,
    "get_lot": _lot_facts,
    "get_item_lots": _item_lots_facts,
    "get_sales_commitments": _commitment_facts,
    "get_policy": _policy_facts,
    "get_capacity_context": _capacity_facts,
    "get_inbound_schedule": _inbound_facts,
    "estimate_action_impact": _impact_facts,
}


def _facts_of(record: Any, subject: _Subject) -> Mapping[str, Any] | None:
    """한 호출에서 핵심 사실을 뽑는다. 못 뽑으면 `None`.

    🔴 **터져도 제안을 버리지 않는다** — Tool 모양이 예상과 달라진 것은 그 제안이
       틀렸다는 뜻이 아니다. 못 뽑았다는 사실만 근거에 남긴다.
    """
    project = _EVIDENCE_FACTS.get(record.tool_name)
    if project is None:
        return None
    try:
        return project(record.answer, subject)
    except Exception:  # noqa: BLE001 - 근거 추림 실패가 제안을 죽이지 않는다.
        return None


def _evidence_snapshot(
    result: InvestigationResult, option: EvaluatedOption, *, subject: _Subject
) -> list[Any]:
    """인용된 호출을 **승인에 필요한 만큼만** 남긴다.

    ```text
    sequence · tool_name · arguments   무엇을 어떻게 물었나
    facts                              🔴 그 답에서 **판단에 쓴 핵심 사실만**
    observed_as_of · uncertainties     그 답이 언제 것이고 무엇을 못 봤나
    ```

    🔴 **번호만 적지 않는다.** `evidence_refs` 는 그 조사 안의 순번이다. Commit 7 이
       조사를 표로 남긴 뒤에도 그렇다 — 승인 화면이 «왜 이 안인가» 를 보이려고 매번
       조사 기록을 따라가야 한다면, 그 조사 행이 지워지거나 못 읽히는 날 근거가 통째로
       사라진다. 🔴 **제안은 자기 근거를 자기가 든다.**

    🔴 **Tool 답 전체도 적지 않는다.** 제안은 조사 로그가 아니다 — 전체를 복사하면 같은
       데이터가 제안 수만큼 늘고, 조사 기록과 승인 대상의 책임이 한 칸에 섞인다.

    🔴 **여기서 Tool 을 다시 부르지 않는다.** 사실은 전부 조사 때 받아 둔
       `ToolCallRecord.answer` 에서 나온다 — 지금 DB 를 다시 읽으면 **오늘 값**을 그날의
       근거처럼 보여 주게 된다. LLM 에게 요약시키지도 않는다 (§4 · 결정론 매핑이다).

    ⚠️ 못 뽑으면 `facts` 는 비고 `EVIDENCE_FACTS_UNAVAILABLE` 이 붙는다 — **없는 값을
       지어내 채우지 않는다.**
    """
    cited = set(option.evidence_refs)
    entries: list[Any] = []
    for record in result.tool_calls:
        if record.sequence not in cited:
            continue
        facts = _facts_of(record, subject)
        uncertainties = list(record.uncertainties)
        if facts is None:
            facts = {}
            if EVIDENCE_FACTS_UNAVAILABLE not in uncertainties:
                uncertainties.append(EVIDENCE_FACTS_UNAVAILABLE)
        entries.append(
            {
                "sequence": record.sequence,
                "tool_name": record.tool_name,
                "arguments": jsonable(dict(record.arguments)),
                "facts": jsonable(facts),
                "observed_as_of": jsonable(record.observed_as_of),
                "uncertainties": jsonable(tuple(uncertainties)),
            }
        )
    return entries


# ══════════════════════════════════════════════════════════════════════════
#  생성 — 🔴 제안 INSERT 와 Exception UPDATE 는 **함께** 성공한다
# ══════════════════════════════════════════════════════════════════════════


def create_proposal(
    conn: Any,
    *,
    result: InvestigationResult,
    as_of: date,
    proposed_by: str,
    investigation_id: str | None = None,
    supersedes: str | None = None,
) -> ProposalOutcome:
    """조사 결과 하나를 **저장 가능한 대응안 한 건**으로 바꾼다.

    ```text
    ① 어떤 후보인가        select_proposal_option (순수)
    ② 누가 결정하나        🔴 ACTION_DECISION_OWNERS 에서 **다시 계산** (§49)
    ③ 인자가 맞나          Commit 4 의 argument model 그대로 (§50)
    ④ 그 조사가 맞나        🔴 **저장된 조사가 이 결과의 저장본인가** ← 모든 쓰기보다 앞
    ⑤ 문제가 살아 있나      RESOLVED · DISMISSED 에는 안 세운다
    ⑥ 그 조사를 이미 썼나    🔴 조사 하나에 제안은 **최대 하나**다
    ⑦ 이미 있나            같은 지문이면 재시도 · 다른 안이면 안 세운다 (§47 · §48)
    ⑧ 대체 대상이 맞나      🔴 **이 문제의 살아 있는 제안**인가 — 남의 문제를 안 닫는다
    ⑨ 날짜가 앞서지 않나    🔴 이전 제안이 끝난 날보다 **뒤**여야 한다
    ⑩ 한 트랜잭션          INSERT + Exception OPEN→PROPOSED  ← 반쪽 상태를 안 남긴다
    ```

    🔴 **`decision_owner` 를 모델 값으로 저장하지 않는다.** 누가 결정하는가는 역할 경계
       (§9.1)이고, 모델이 *"이건 물류가 정하면 됩니다"* 라고 적어도 그 말이 표에 실리면
       안 된다.

    🔴 **`observed_as_of` 는 조사 값 그대로 옮긴다.** `None` 이면 `None` 이다 — 제안일 ·
       승인일 · 현재시각으로 메우지 않는다 (§22).

    :param as_of: 시뮬레이션 영업일. 🔴 `date.today()` 를 안 쓴다 (§20).
    :param investigation_id: 이 제안을 낸 조사 (`logistics_investigations` · Commit 7).
        🔴 **주면 그 조사가 «이 결과의 저장본» 인지 실제로 대조한다.** 복합 FK 는
        «같은 실행 · 같은 문제» 까지만 보므로, 같은 문제를 두 번 조사한 뒤 **A 의 ID 에
        B 의 결과**를 매다는 호출을 못 막는다 — 그러면 장부가 *"이 대응안은 어느 조사에서
        나왔나"* 에 **거짓으로 답한다.** 안 주면 `None` 이고(손으로 세운 제안) 그때는
        대조도 FK 도 하지 않는다.
    :param supersedes: 이 제안이 **대체할** 살아 있는 제안 (§29). 주면 그것을 먼저
        `SUPERSEDED` 로 닫고 새 행이 `previous_proposal_id` 로 가리킨다.
        🔴 **반드시 이 조사의 Exception 것이어야 한다** — 아니면 아무것도 안 쓰고
        `SUPERSEDE_TARGET_MISMATCH` 로 멈춘다. 남의 문제의 대응안을 닫지 않는다.
        ⚠️ **자동 대체는 없다** — 부르는 쪽이 명시할 때만 일어난다.
    :returns: 만들었나 · 재시도였나 · 안 만들었나 + 그 사유.
        ⚠️ **«안 만들었다» 는 값으로 돌려주고**(추천이 없다 · 이미 있다 …),
        **«그렇게 부르면 안 된다» 는 예외로 올린다**(대체 대상이 틀렸다 · 날짜가
        앞선다). 앞엣것은 정상적인 답이고 뒤엣것은 부르는 쪽의 실수다 — 둘을 한
        갈래로 접으면 잘못된 호출이 «오늘은 제안할 것이 없었다» 로 조용히 묻힌다.
    """
    if not proposed_by.strip():
        raise ValueError("proposed_by 가 비었다 — 누가 올렸는지 못 대는 제안은 만들지 않는다")
    if as_of != result.as_of:
        raise ValueError(
            f"as_of 가 조사와 다르다 (요청 {as_of} · 조사 {result.as_of}) —"
            " 다른 영업일의 조사로 오늘 제안을 세우지 않는다"
        )

    option, reason = select_proposal_option(result)
    if option is None:
        return ProposalOutcome(status="SKIPPED", reason=reason)

    # 🔴 모델이 적어 준 소유 부서를 안 믿는다. 카탈로그가 정한다.
    owner = ACTION_DECISION_OWNERS.get(option.action)
    if owner is None:
        return ProposalOutcome(
            status="SKIPPED", reason=f"{ACTION_UNSUPPORTED}:{option.action}"
        )

    try:
        # ★ 새 스키마를 만들지 않는다 — guard 가 쓰는 그 모델이다 (§50).
        checked = ActionImpactArguments.model_validate(
            {"action": option.action, "parameters": dict(option.parameters)}
        )
    except ValidationError as error:
        return ProposalOutcome(
            status="SKIPPED", reason=f"{PARAMETERS_INVALID}:{error.error_count()}건"
        )
    parameters = checked.parameters

    # 🔴 **어떤 쓰기보다도 먼저** 조사를 대조한다. 뒤로 밀면 `_supersede` 가 남의 제안을
    #    먼저 닫아 놓고 나서 «그 조사가 아니다» 를 알게 된다.
    if investigation_id is not None:
        _check_investigation(conn, result=result, investigation_id=investigation_id)

    status = repository.exception_status(
        conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id
    )
    if status is None:
        return ProposalOutcome(
            status="SKIPPED", reason=f"{EXCEPTION_NOT_FOUND}:{result.exception_id}"
        )
    if status not in LIVE_EXCEPTION_STATUSES:
        # 🔴 Exception 의 «살아 있다» 는 OPEN·PROPOSED 다 — 제안의 것(PROPOSED·APPROVED)과
        #    **값이 다르다.** 두 상태기계를 섞으면 RESOLVED 된 문제에 제안이 선다.
        return ProposalOutcome(status="SKIPPED", reason=f"{EXCEPTION_NOT_LIVE}:{status}")

    # ★ 이 문제의 제안을 **한 번만** 읽어 대체 대상 · 날짜 · 중복 셋을 모두 가린다.
    history = repository.select_proposals(
        conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id
    )
    key = proposal_key_for(
        sim_run_id=result.sim_run_id,
        exception_id=result.exception_id,
        action_type=option.action,
        parameters=parameters,
    )
    # 🔴 **조사 재사용 판정이 «살아 있는 제안» 판정보다 먼저다.** 아래 반복문은 살아
    #    있는 제안만 보므로, 그 조사의 제안이 이미 **거절되어 끝났으면** 아무것도 못 보고
    #    새 행을 만든다 — 끝난 판단 하나로 제안을 둘 만드는 자리가 거기다.
    settled = _settle_investigation_reuse(
        history, key=key, investigation_id=investigation_id
    )
    if settled is not None:
        return settled

    for existing in (one for one in history if one.live):
        if existing.proposal_id == supersedes:
            continue
        if existing.proposal_key == key:
            # 같은 뜻의 제안이 이미 서 있다 — 네트워크 재시도다. 새 행을 안 만든다.
            return ProposalOutcome(status="REUSED", reason="", proposal=existing)
        return ProposalOutcome(
            status="SKIPPED", reason=f"{LIVE_PROPOSAL_EXISTS}:{existing.proposal_id}"
        )

    # ★ **대체 대상 검증과 날짜 검사는 둘 다 중복 판정 뒤다.** 순수한 재시도(REUSED)는
    #   새 행을 안 만드니 따질 것이 없고, 앞에서 막으면 **성공한 요청의 재시도가 예외로
    #   튄다** — 첫 호출이 대체를 이미 끝냈으므로 대상은 그때 `SUPERSEDED` 가 돼 있다.
    #   ⚠️ 위 반복문이 `supersedes` 와 같은 ID 를 건너뛰는 것은 안전하다: `history` 는
    #      **이 Exception 의** 제안뿐이라, 남의 제안 ID 로는 아무것도 안 건너뛴다.
    if supersedes is not None:
        _check_supersede_target(
            conn, history=history, result=result, supersedes=supersedes, as_of=as_of
        )
    _check_chronology(history, as_of=as_of, exception_id=result.exception_id)

    row = ProposalRow(
        proposal_id=repository.next_proposal_id(conn, exception_id=result.exception_id),
        sim_run_id=result.sim_run_id,
        exception_id=result.exception_id,
        investigation_id=investigation_id,
        proposal_key=key,
        status="PROPOSED",
        action_type=option.action,
        decision_owner=owner,
        parameters=jsonable(parameters),
        impact=jsonable(option.impact),
        evidence_refs=tuple(
            _evidence_snapshot(
                result,
                option,
                subject=_Subject.of(
                    exception_id=result.exception_id, parameters=parameters
                ),
            )
        ),
        rationale=option.rationale,
        source_finish_reason=result.finish_reason.value,
        source_llm_status=result.llm_status,
        proposed_as_of=as_of,
        proposed_by=proposed_by,
        # 🔴 조사가 낸 값 그대로. 여기서 만들지 않는다.
        observed_as_of=result.observed_as_of,
        previous_proposal_id=supersedes,
    )

    try:
        if supersedes is not None:
            _supersede(conn, sim_run_id=result.sim_run_id, proposal_id=supersedes, as_of=as_of)
        repository.insert_proposal(conn, row=row)
        moved = repository.mark_exception_proposed(
            conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id, as_of=as_of
        )
        if moved != 1:
            # 🔴 읽고 나서 쓰는 사이에 누가 문제를 닫았다. **제안만 남기지 않는다.**
            raise ProposalStateConflict(
                "STATE_CONFLICT",
                f"{result.exception_id} 가 제안을 세우는 사이에 살아 있는 상태가 아니게 됐다",
            )
        conn.commit()
    except Exception as error:
        conn.rollback()
        # 🔴 **동시에 들어온 같은 요청**이었을 수 있다. 그때는 실패가 아니라 재시도다 —
        #    다만 «무엇과 부딪혔는지» 는 예외 이름이 아니라 **다시 읽어서** 정한다.
        if isinstance(error, _ConcurrentInsert) or repository.is_unique_violation(error):
            return _settle_after_race(
                conn, result=result, key=key, investigation_id=investigation_id
            )
        raise
    return ProposalOutcome(status="CREATED", proposal=row)


def _check_investigation(
    conn: Any, *, result: InvestigationResult, investigation_id: str
) -> None:
    """가리키라고 준 조사가 **정말 이 결과의 저장본**인가. 아니면 아무것도 안 쓰고 멈춘다.

    ```text
    복합 FK 가 막는 것     없는 ID · 남의 실행의 조사 · 남의 문제의 조사
    FK 가 **못** 막는 것   같은 실행 · 같은 문제의 **다른 조사**   ← 여기를 막는다
    ```

    🔴 **같은 문제를 두 번 조사하는 것은 정상이다** (§35). 그래서 `INV-A`(우선판매로 끝난
       조사)와 `INV-B`(폐기로 끝난 조사)가 나란히 설 수 있고, 둘은 실행도 문제도 같다 —
       호출자가 `result=B` 를 들고 `investigation_id="INV-A"` 라고 적어도 **FK 는 통과한다.**
       그러면 제안의 action·parameters·impact·근거는 B 에서 왔는데 장부에는 A 라고 적히고,
       *"이 대응안은 어느 조사에서 나왔나"* 에 DB 가 **거짓으로 답하게 된다.**

    ★ **감사 저장과 같은 함수로 대조한다** (`investigation_row_for` ·
      `same_investigation_audit`). 제안 전용 지문을 새로 셈하지 않는다 — 규칙이 두 벌이면
      «다르다» 가 실제 차이인지 직렬화 차이인지 아무도 못 댄다.

    🔴 **Tool 도 LLM 도 안 부른다.** 저장된 감사 행을 한 번 읽을 뿐이다.
    """
    stored = investigation_repository.select_investigation(
        conn, sim_run_id=result.sim_run_id, investigation_id=investigation_id
    )
    if stored is None:
        raise ProposalStateConflict(
            INVESTIGATION_NOT_FOUND,
            f"{investigation_id} 는 이 실행({result.sim_run_id})에 저장된 조사가 아니다"
            " — 저장되지 않은 조사에 제안을 매달지 않는다",
        )
    fresh = investigation_service.investigation_row_for(
        result, investigation_id=investigation_id
    )
    if not investigation_service.same_investigation_audit(stored, fresh):
        raise ProposalStateConflict(
            INVESTIGATION_RESULT_MISMATCH,
            f"{investigation_id} 에 저장된 조사와 넘어온 조사 결과가 다르다"
            f" (문제 {result.exception_id} · {result.as_of}) — 같은 실행·같은 문제의"
            " **다른 조사**를 가리키고 있다",
        )


def _settle_investigation_reuse(
    history: Sequence[ProposalRow], *, key: str, investigation_id: str | None
) -> ProposalOutcome | None:
    """그 조사를 이미 쓴 제안이 있나. **조사 하나에 제안은 최대 하나다.**

    ```text
    그 조사를 쓴 제안이 없다           →  None            계속 진행한다
    살아 있고 지문이 같다              →  REUSED          같은 요청의 재시도다
    그 밖(끝났다 · 다른 안이다)        →  🔴 ALREADY_USED  새 조사를 돌려야 한다
    ```

    🔴 **끝난 제안도 «썼다» 이다** (§24). `INV-A → PRP-A → REJECTED` 뒤에 다시 `INV-A` 로
       `PRP-B` 를 만들면, **한 번 끝난 판단 하나가 승인 대상 둘을 낳는다.** 거절은 그
       조사의 결론이 받아들여지지 않았다는 뜻이지 조사를 안 한 것이 아니다 — 새 안이
       필요하면 **새 조사**를 돌린다.

    ⚠️ 그래서 «살아 있는 제안» 목록이 아니라 **이력 전체**를 본다.

    ★ 재시도(같은 안을 한 번 더 보냄)만 예외다 — 그때 돌려주는 것은 **이미 선 그 행**이고
      새 행은 안 만든다. 기존 멱등 계약(`REUSED`) 그대로다.
    """
    if investigation_id is None:
        return None
    earlier = next(
        (one for one in history if one.investigation_id == investigation_id), None
    )
    if earlier is None:
        return None
    if earlier.live and earlier.proposal_key == key:
        return ProposalOutcome(status="REUSED", reason="", proposal=earlier)
    raise ProposalStateConflict(
        INVESTIGATION_ALREADY_USED,
        f"{investigation_id} 는 이미 {earlier.proposal_id}({earlier.status}) 를 냈다"
        " — 조사 하나에 대응안은 하나다. 새 안이 필요하면 새 조사를 돌린다",
    )


def _settle_after_race(
    conn: Any, *, result: InvestigationResult, key: str, investigation_id: str | None = None
) -> ProposalOutcome:
    """부딪힌 뒤 **롤백하고 다시 읽어** 무슨 일이었는지로 답을 정한다.

    ```text
    살아 있는 제안의 지문이 같다   →  REUSED              같은 요청이 먼저 들어갔다
    살아 있는 제안의 지문이 다르다  →  LIVE_PROPOSAL_EXISTS 남이 **다른 안**을 세웠다
    살아 있는 제안이 없다          →  🔴 답하지 않는다     무엇과 부딪혔는지 못 댄다
    ```

    🔴 **유일 제약 위반을 곧바로 `REUSED` 로 옮기지 않는다.** PK 충돌과 «살아 있는 제안
       하나» 충돌은 같은 종류의 예외로 오고, 그 이름만 보고 답하면 **남의 제안을 내
       것이라고 답하게 된다.**

    ⚠️ PostgreSQL 은 제약 위반 뒤 트랜잭션이 abort 상태라 **롤백 없이는 다시 못 읽는다.**
       부르는 쪽이 이미 롤백한 뒤에 들어온다.

    🔴 **부딪힌 자리가 둘이다** (Commit 7 보정). «한 문제에 살아 있는 제안 하나» 말고
       «한 조사에 제안 하나» 로도 부딪힌다 — 같은 조사로 두 worker 가 동시에 들어온
       경우다. 그래서 조사 축을 **먼저** 가린다: 조사 축 충돌을 «살아 있는 제안이 있다»
       로 답하면 원인이 흐려지고, 무엇보다 그 조사의 제안이 이미 끝난 경우를 못 댄다.
    """
    if investigation_id is not None:
        settled = _settle_investigation_reuse(
            repository.select_proposals(
                conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id
            ),
            key=key,
            investigation_id=investigation_id,
        )
        if settled is not None:
            return settled
    for existing in repository.live_proposals_for(
        conn, sim_run_id=result.sim_run_id, exception_id=result.exception_id
    ):
        if existing.proposal_key == key:
            return ProposalOutcome(status="REUSED", proposal=existing)
        return ProposalOutcome(
            status="SKIPPED", reason=f"{LIVE_PROPOSAL_EXISTS}:{existing.proposal_id}"
        )
    raise ProposalStateConflict(
        "STATE_CONFLICT",
        f"{result.exception_id} 에 제안을 넣다 부딪혔는데 다시 읽으니 살아 있는 제안이"
        " 없다 — 무엇과 부딪혔는지 못 대므로 답하지 않는다",
    )


def _check_supersede_target(
    conn: Any,
    *,
    history: Sequence[ProposalRow],
    result: InvestigationResult,
    supersedes: str,
    as_of: date,
) -> None:
    """대체 대상이 **이 문제의 살아 있는 제안**인지 본다. 🔴 아니면 아무것도 안 쓴다.

    ```text
    이 문제의 PROPOSED        ✅ 대체한다
    다른 Exception 의 제안    🔴 SUPERSEDE_TARGET_MISMATCH
    다른 실행(sim_run) 의 제안 🔴 〃
    없는 ID                   🔴 〃
    이미 끝난 제안            🔴 〃  (닫을 것이 없다)
    대상보다 앞선 날짜         🔴 〃  (대체된 날이 제안된 날보다 앞설 수 없다)
    ```

    🔴 **이 검사가 없으면 남의 문제의 대응안을 닫는다.** `_supersede` 는 `sim_run_id` 와
       `proposal_id` 만 보고 `UPDATE` 하므로, 같은 실행 안의 **다른 Exception** 제안을
       그대로 `SUPERSEDED` 로 만들어 버린다 — 그 문제는 대응안을 잃고, 잃었다는 사실이
       어디에도 안 적힌다.

    ★ `history` 는 **이 Exception 의** 제안 전부다. 거기 없으면 그 자체가 «남의 것»
      이라는 증거라, 대부분의 경우 DB 를 더 안 읽는다.
    """
    for one in history:
        if one.proposal_id != supersedes:
            continue
        if one.status != "PROPOSED":
            raise ProposalStateConflict(
                SUPERSEDE_TARGET_MISMATCH,
                f"{supersedes} 는 {one.status} 라 대체할 수 없다 — 이미 끝난 제안이다",
            )
        if as_of < one.proposed_as_of:
            # ⚠️ DB CHECK(`decided_order`)도 막지만, 그쪽은 제약 위반 문자열만 남긴다.
            #    여기서 막아야 «왜» 안 되는지가 사람에게 간다.
            raise ProposalStateConflict(
                SUPERSEDE_TARGET_MISMATCH,
                f"{supersedes} 는 {one.proposed_as_of} 에 섰는데 {as_of} 로 대체하려 한다"
                " — 제안된 날보다 앞서 대체될 수 없다",
            )
        return

    # 여기까지 왔으면 이 Exception 의 것이 아니다. **왜** 아닌지를 정확히 적는다.
    elsewhere = repository.select_proposal(
        conn, sim_run_id=result.sim_run_id, proposal_id=supersedes
    )
    if elsewhere is not None:
        raise ProposalStateConflict(
            SUPERSEDE_TARGET_MISMATCH,
            f"{supersedes} 는 {elsewhere.exception_id} 의 제안이다 —"
            f" {result.exception_id} 의 제안을 세우며 남의 문제의 대응안을 닫지 않는다",
        )
    raise ProposalStateConflict(
        SUPERSEDE_TARGET_MISMATCH,
        f"{result.sim_run_id} 에 {supersedes} 가 없다 — 대체할 대상이 없다",
    )


def _check_chronology(
    history: Sequence[ProposalRow], *, as_of: date, exception_id: str
) -> None:
    """새 제안이 **이전 제안이 끝난 날보다 뒤**인지 본다.

    ```text
    이전 제안  D5 제안 · D6 거절
    새 제안    D5  🔴 거부 — D5 로 접으면 «그날 살아 있던 제안» 이 둘이다
               D6  ✅ 허용 — 그날 이전 것은 이미 거절이다
               D7  ✅ 허용
    ```

    🔴 **부분 유일 인덱스가 이것을 못 막는다.** 그 인덱스는 «지금» 상태만 보는데,
       겹침은 **과거로 접었을 때만** 드러난다 — D6 에 거절된 제안은 지금 살아 있지
       않으므로 인덱스는 조용하고, D5 조회에서야 둘 다 `PROPOSED` 로 나타난다.

    ⚠️ 끝난 날이 **같은 날**인 것은 허용한다. 그날 이전 제안은 이미 끝났고 새 제안이
       그날 섰다 — 겹치지 않는다.
    """
    latest, unorderable = repository.latest_terminal_as_of(history)
    if unorderable:
        # 🔴 순서를 못 세우면 **통과시키지 않는다** (Commit 6 이 열어야 할 칸이다).
        raise ProposalStateConflict(
            PROPOSAL_HISTORY_CONFLICT,
            f"{exception_id} 에 끝난 날을 못 대는 제안이 있다 ({', '.join(unorderable)}) —"
            " 그 앞뒤를 모르면 새 제안의 날짜가 겹치는지 알 수 없다",
        )
    if latest is not None and as_of < latest:
        raise ProposalStateConflict(
            PROPOSAL_HISTORY_CONFLICT,
            f"{exception_id} 의 이전 제안이 {latest} 에 끝났는데 새 제안을 {as_of} 로"
            " 세우려 한다 — 그날로 접으면 살아 있던 제안이 둘이 된다",
        )


def _supersede(conn: Any, *, sim_run_id: str, proposal_id: str, as_of: date) -> None:
    """이전 제안을 닫아 자리를 비운다. **새 제안이 서기 전에** 해야 한다.

    ⚠️ 부분 유일 인덱스가 «한 문제에 살아 있는 제안 하나» 를 강제하므로, 닫지 않고
       새 행을 넣으면 DB 가 막는다.
    """
    closed = repository.transition_proposal(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="SUPERSEDED",
        as_of=as_of,
        from_status="PROPOSED",
    )
    if closed != 1:
        # ⚠️ 여기 오는 것은 **경합뿐이다** — 대상이 `PROPOSED` 인 것은 방금 확인했다.
        #    그러니 «부를 수 없는 요청» 이 아니라 «남이 먼저 움직였다» 로 다룬다.
        raise _ConcurrentInsert(
            f"{proposal_id} 를 대체하는 사이에 남이 먼저 그 제안을 옮겼다"
        )


# ══════════════════════════════════════════════════════════════════════════
#  사람의 결정 — 🔴 LLM 을 부르지 않는다
# ══════════════════════════════════════════════════════════════════════════


def approve_proposal(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    as_of: date,
    approved_by: str,
    note: str | None = None,
) -> ProposalRow:
    """사람이 **«진행해도 좋다»** 고 정했다. 🔴 **실행이 아니다.**

    ```text
    APPROVED  =  이 대응안을 진행해도 좋다는 사람의 결정
    ≠            판매가 일어났다 · 매입 일정이 바뀌었다 · 재고가 폐기됐다
    ```

    🔴 **Exception 을 닫지 않는다** (§26 · §28). 아직 아무 행동도 안 했고, Exception 은
       *문제* 의 상태이지 *대응* 의 상태가 아니다 — 두 상태기계를 섞지 않는다.
       (`logistics_exceptions.status` 에 `APPROVED` 라는 값이 애초에 없다.)

    🔴 **타 부서를 부르지 않는다.** `SALES_PRIORITY_REQUEST` 승인은 *"영업에 우선판매를
       요청해도 좋다"* 까지다 — Sales · Purchase 모듈 호출은 Commit 6 이다.

    🔴 **이미 닫힌 문제의 제안은 승인하지 않는다** (stale approval). 사람이 보던 목록이
       낡았을 수 있다 — 그 사이에 재탐지가 문제를 `RESOLVED` 로 닫았는데 승인이 그대로
       들어가면, *"없어진 문제에 대응하기로 했다"* 가 장부에 남는다.

    ```text
    Exception PROPOSED   ✅ 승인한다 — 대응을 기다리는 상태다
    RESOLVED · DISMISSED 🔴 STALE_PROPOSAL — 대응할 문제가 이미 닫혔다
    OPEN                 🔴 STALE_PROPOSAL — 대응 대기 상태가 아니다(장부가 어긋나 있다)
    없다                 🔴 STALE_PROPOSAL
    ```

    ⚠️ **거절·만료는 이 검사를 안 한다.** 닫힌 문제에 남은 제안을 사람이 치우는 것은
       정상이고, 막으면 그 제안이 영원히 `PROPOSED` 로 남는다.
    """
    return _decide(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="APPROVED",
        as_of=as_of,
        actor=approved_by,
        note=note,
        # ⚠️ 승인은 Exception 을 안 건드린다 — 되돌릴 것도 없다.
        reopen_exception=False,
        # 🔴 다만 **지금도 대응 대기인지**는 본다 (stale approval).
        require_exception_status="PROPOSED",
    )


def reject_proposal(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    as_of: date,
    rejected_by: str,
    rejection_reason: str,
) -> ProposalRow:
    """사람이 **«이 안은 아니다»** 라고 정했다.

    🔴 **사유가 비면 안 받는다.** *"왜 아닌가"* 가 없으면 다음 조사가 같은 안을 또 올린다.

    ★ 거절 뒤 그 문제에 살아 있는 대응안이 하나도 안 남으면 Exception 은 다시
      `OPEN` 이다 (§27) — *"문제는 그대로인데 대응안이 없다"* 가 정확한 상태다.
      ⚠️ 다른 살아 있는 제안이 있으면 `PROPOSED` 를 유지한다.
    """
    if not rejection_reason.strip():
        raise ValueError("rejection_reason 이 비었다 — 사유 없는 거절은 다음 조사에 안 남는다")
    return _decide(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="REJECTED",
        as_of=as_of,
        actor=rejected_by,
        note=rejection_reason,
        reopen_exception=True,
    )


def expire_proposal(
    conn: Any, *, sim_run_id: str, proposal_id: str, as_of: date
) -> ProposalRow:
    """기한이 지나 **사람이 볼 필요가 없어졌다.**

    🔴 **시간 기반 자동 작업이 아니다** (§30). 부르는 쪽이 «오늘은 며칠» 을 들고 와서
       명시적으로 닫는다 — Scheduler 에 붙는 만료 task 는 이번 Commit 에 없다.

    ⚠️ 행위자 칸이 없다. 사람이 내린 결정이 아니라 기한 경과이고, 없는 사람 이름을
       지어내 적는 것보다 **비워 두는 것**이 정직하다.

    ★ 승인과 달리 **문제가 닫혔는지 안 본다** — 닫힌 문제에 남은 제안을 치우는 것이
      바로 이 함수의 쓸모다. 다만 그 문제를 `OPEN` 으로 되살리지는 않는다.
    """
    return _decide(
        conn,
        sim_run_id=sim_run_id,
        proposal_id=proposal_id,
        to_status="EXPIRED",
        as_of=as_of,
        actor=None,
        note=None,
        reopen_exception=True,
    )


def _decide(
    conn: Any,
    *,
    sim_run_id: str,
    proposal_id: str,
    to_status: str,
    as_of: date,
    actor: str | None,
    note: str | None,
    reopen_exception: bool,
    require_exception_status: str | None = None,
) -> ProposalRow:
    """결정 하나. **네 갈래를 가린다: 재시도 · 충돌 · stale · 정상.**

    ```text
    이미 그 상태 + 같은 사람 + 같은 날   →  기존 행 그대로 (재시도 · §38 · §39)
    이미 그 상태 + 다른 사람/다른 날      →  ALREADY_{상태}
    아예 다른 상태                        →  STATE_CONFLICT
    문제가 요구 상태가 아니다              →  STALE_PROPOSAL
    PROPOSED                              →  옮긴다
    ```

    🔴 **옮기는 UPDATE 에 `AND status = 'PROPOSED'` 가 있다** (§37). 두 사람이 동시에
       눌러도 한 번만 먹고, 늦은 쪽은 `0` 을 받아 위의 갈래로 다시 내려간다 —
       읽고 나서 쓰는 사이의 lost update 를 응용 코드로는 못 막는다.

    🔴 **`require_exception_status` 도 같은 UPDATE 안에 실린다.** 먼저 읽어서 보는 것은
       *"사람에게 왜 안 되는지 정확히 말하기 위해서"* 이고, **막는 것은 SQL 조건**이다 —
       읽기 쪽에만 두면 **먼저 읽고 나중에 쓰는 사이에 재탐지가 커밋한** 변경을 못 보고
       승인이 들어간다.

    ⚠️ **닫는 틈은 «커밋된 변경» 까지다.** 이 조건은 잠금을 안 걸므로, 아직 커밋 안 된
       남의 트랜잭션이 그 문제를 닫는 중이면 막지 못한다 — 그때 결과는 «승인 먼저,
       해소 나중» 이라는 **정상적인 순서**와 같다(승인은 Exception 을 안 닫으므로 §7.1
       의 재탐지가 그 뒤에 닫는 것이 원래 흐름이다). 승인 시점의 잠금으로 막을 수 있는
       종류의 문제가 아니고, *"승인된 대응을 실행해도 되나"* 는 **실행 시점**(Commit 6)에
       다시 물어야 한다.

    ⚠️ **재시도는 이 검사보다 앞선다.** 이미 같은 사람이 같은 날 승인해 둔 것을 다시
       보냈다면, 그 사이 문제가 닫혔더라도 **새로 쓰는 것이 없으므로** 기존 행을 그대로
       돌려준다. 여기서 막으면 *"승인이 안 됐나"* 하고 사람이 또 누른다.
    """
    if actor is not None and not actor.strip():
        raise ValueError("결정한 사람이 비었다 — 빈 문자열은 «모른다» 를 «있다» 로 위장한다")

    row = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    if row is None:
        raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
    if as_of < row.proposed_as_of:
        raise ValueError(
            f"as_of({as_of}) 가 제안일({row.proposed_as_of}) 보다 앞선다 —"
            " 제안되기 전에 결정할 수 없다"
        )

    settled = _settled(row, to_status=to_status, as_of=as_of, actor=actor, note=note)
    if settled is not None:
        return settled
    if row.status != "PROPOSED":
        raise _conflict(row, to_status=to_status)
    if require_exception_status is not None:
        _refuse_if_stale(
            conn,
            sim_run_id=sim_run_id,
            row=row,
            required=require_exception_status,
        )

    try:
        changed = repository.transition_proposal(
            conn,
            sim_run_id=sim_run_id,
            proposal_id=proposal_id,
            to_status=to_status,
            as_of=as_of,
            from_status="PROPOSED",
            actor=actor,
            note=note,
            require_exception_status=require_exception_status,
        )
        if changed == 0:
            # 🔴 읽고 나서 쓰는 사이에 남이 먼저 옮겼다. 바뀐 행이 0 이라 되돌릴
            #    것은 없고, 다시 읽어 **재시도인지 충돌인지** 를 가린다.
            fresh = repository.select_proposal(
                conn, sim_run_id=sim_run_id, proposal_id=proposal_id
            )
            if fresh is None:
                raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
            raced = _settled(fresh, to_status=to_status, as_of=as_of, actor=actor, note=note)
            if raced is not None:
                return raced
            if fresh.status == "PROPOSED" and require_exception_status is not None:
                # 제안은 그대로다 → 막은 것은 **Exception 조건**이다. 그 사이에 닫혔다.
                _refuse_if_stale(
                    conn, sim_run_id=sim_run_id, row=fresh, required=require_exception_status
                )
            raise _conflict(fresh, to_status=to_status)

        if reopen_exception:
            _reopen_if_nothing_lives(conn, sim_run_id=sim_run_id, exception_id=row.exception_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    decided = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    if decided is None:  # pragma: no cover - 방금 1 행을 바꾸고 커밋했다.
        raise ProposalNotFound(f"{sim_run_id} 에 {proposal_id} 가 없다")
    return decided


def _refuse_if_stale(
    conn: Any, *, sim_run_id: str, row: ProposalRow, required: str
) -> None:
    """그 문제가 지금도 요구 상태인가. 🔴 아니면 **아무것도 안 쓰고** 멈춘다.

    ★ 실제로 막는 것은 `transition_proposal` 의 SQL 조건이다. 이 함수는 *"왜 안 되는지"*
      를 사람이 읽을 수 있게 만든다 — `rowcount 0` 만으로는 «남이 먼저 눌렀다» 와
      «문제가 닫혔다» 를 구별할 수 없다.
    """
    current = repository.exception_status(
        conn, sim_run_id=sim_run_id, exception_id=row.exception_id
    )
    if current == required:
        return
    raise ProposalStateConflict(
        STALE_PROPOSAL,
        f"{row.exception_id} 가 {current or '없음'} 이라 {row.proposal_id} 를 승인할 수 없다"
        f" — 승인은 그 문제가 {required} 일 때만이다 (보던 화면이 낡았다)",
    )


def _reopen_if_nothing_lives(conn: Any, *, sim_run_id: str, exception_id: str) -> None:
    """살아 있는 대응안이 0 이면 Exception 을 다시 `OPEN` 으로 (§27).

    ⚠️ **`RESOLVED` · `DISMISSED` 를 되살리지 않는다** — 재탐지가 이미 닫은 문제를 제안
       거절이 다시 열면, 조건이 사라진 문제가 장부에 남는다. 그 방어는
       `reopen_exception` 의 `WHERE status = 'PROPOSED'` 에 있다.
    """
    if repository.live_proposals_for(conn, sim_run_id=sim_run_id, exception_id=exception_id):
        return
    repository.reopen_exception(conn, sim_run_id=sim_run_id, exception_id=exception_id)


def _settled(
    row: ProposalRow, *, to_status: str, as_of: date, actor: str | None, note: str | None
) -> ProposalRow | None:
    """**같은 결정을 한 번 더 보낸 것**인가. 아니면 `None`.

    ★ 네트워크가 끊겨 같은 요청이 두 번 들어오는 일은 흔하다. 그때 두 번째를 충돌로
      돌려보내면 사람은 *"승인이 안 됐나"* 하고 또 누른다.
    """
    if row.status != to_status:
        return None
    dates = {
        "APPROVED": row.approved_as_of,
        "REJECTED": row.rejected_as_of,
        "EXPIRED": row.expired_as_of,
        "SUPERSEDED": row.superseded_as_of,
    }
    if dates.get(to_status) != as_of:
        return None
    if to_status == "APPROVED" and row.approved_by != actor:
        return None
    if to_status == "REJECTED" and (row.rejected_by != actor or row.rejection_reason != note):
        return None
    return row


def _conflict(row: ProposalRow, *, to_status: str) -> ProposalStateConflict:
    """🔴 **이미 끝난 결정을 뒤집지 않는다** (§6 · §58).

    ⚠️ 어휘가 둘이다: 이미 그 상태면 `ALREADY_{상태}`(다른 사람 · 다른 날이 이미 정했다),
       그 밖이면 `STATE_CONFLICT`(거절된 것을 승인하려는 등). 둘 다 같은 예외 종류다.
    """
    if row.status == to_status:
        return ProposalStateConflict(
            f"ALREADY_{to_status}",
            f"{row.proposal_id} 는 이미 {to_status} 다 — 다른 사람이거나 다른 날의 결정이다",
        )
    return ProposalStateConflict(
        "STATE_CONFLICT",
        f"{row.proposal_id} 는 {row.status} 라 {to_status} 로 갈 수 없다",
    )


# ══════════════════════════════════════════════════════════════════════════
#  조회 — 🔴 **그날** 값이다
# ══════════════════════════════════════════════════════════════════════════


def get_proposal(
    conn: Any, *, sim_run_id: str, proposal_id: str, as_of: date
) -> ProposalRow | None:
    """**그날** 이 제안. 그날 아직 없었으면 `None`.

    🔴 지금 상태를 과거로 쓰지 않고, 미래 detail 도 안 흘린다 (§41 · §42).
    """
    row = repository.select_proposal(conn, sim_run_id=sim_run_id, proposal_id=proposal_id)
    return None if row is None else project_proposal_at(row, as_of=as_of)


def list_proposals(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    exception_id: str | None = None,
    statuses: Sequence[str] | None = None,
) -> tuple[ProposalRow, ...]:
    """**그날** 살아 있던(=이미 제안된) 대응안들.

    :param statuses: **그날 상태** 로 거른다 — 지금 상태가 아니다. 오늘 거절된 제안도
        D6 조회에서는 `PROPOSED` 다.
    """
    wanted = None if statuses is None else set(statuses)
    projected: list[ProposalRow] = []
    for row in repository.select_proposals(
        conn, sim_run_id=sim_run_id, exception_id=exception_id
    ):
        at_date = project_proposal_at(row, as_of=as_of)
        if at_date is None:
            continue
        if wanted is not None and at_date.status not in wanted:
            continue
        projected.append(at_date)
    return tuple(projected)


def proposal_payload(row: ProposalRow) -> Mapping[str, Any]:
    """전선에 실을 수 있는 모양 하나. **여기서 값을 바꾸지 않는다.**

    ★ Commit 7 의 API 가 자기 직렬화를 따로 짜다가 `Decimal` 을 `float` 으로 낮추는 일을
      막으려고 미리 한 자리에 둔다 — `jsonable` 이 그것을 문자열로 낸다.
    """
    return jsonable(row)
